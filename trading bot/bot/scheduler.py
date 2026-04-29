import logging
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf
import exchange_calendars as xcals
from apscheduler.schedulers.blocking import BlockingScheduler

from bot.analytics import log_weekly_report
from bot.researcher import gather_research
from bot.scraper import run_scraper
from bot.signal_engine import filter_disclosures, get_sector_for_ticker, compute_lag_days, get_cluster_count
from bot.committee import get_committees_for_politician
from bot.ai_analyst import score_entry, review_exit, EntryScore
from bot.db import get_open_positions, insert_signal
from bot.universe import refresh_universe, get_universe
from bot.portfolio import Portfolio
from screener.factor_scorer import run_factor_screen

log = logging.getLogger(__name__)
_AMS = ZoneInfo("Europe/Amsterdam")
_NYSE = xcals.get_calendar("XNYS")
_ESTIMATED_COST_PCT = 0.05
_MAX_SECTOR_PCT = 30.0
_MAX_ADV_PCT = 10.0
_SCREENER_TOP_N = 12


def _is_trading_day() -> bool:
    return _NYSE.is_session(date.today().isoformat())


def _compute_sector_allocation(portfolio: Portfolio) -> dict[str, float]:
    positions = portfolio.broker.get_positions()
    if not positions:
        return {}
    nav = portfolio.get_cash() + sum(p["qty"] * p["current_price"] for p in positions)
    if nav <= 0:
        return {}
    allocation: dict[str, float] = {}
    for pos in positions:
        sector = get_sector_for_ticker(pos["ticker"])
        position_value = pos["qty"] * pos["current_price"]
        allocation[sector] = allocation.get(sector, 0.0) + (position_value / nav * 100)
    return allocation


def _try_open(portfolio: Portfolio, ticker: str, score: EntryScore,
              signal_id: int | None, research, sector_allocation: dict[str, float],
              sector: str, signal_source: str) -> bool:
    """Apply all risk checks then open a position. Returns True if opened."""
    if not portfolio.can_open_new_position():
        return False
    if portfolio.is_sector_capped(sector, sector_allocation, cap_pct=_MAX_SECTOR_PCT):
        log.info("Skipping %s: sector %r capped at %.1f%%", ticker, sector,
                 sector_allocation.get(sector, 0))
        return False
    if score.entry != "buy":
        return False
    entry_price = yf.Ticker(ticker).info.get("regularMarketPrice", 0)
    if not entry_price:
        log.warning("No price for %s — skipping", ticker)
        return False
    position_size_usd = portfolio.get_cash() * score.position_pct / 100
    adv_usd = research.avg_daily_volume_usd if research else None
    if adv_usd and not portfolio.is_liquid_enough(position_size_usd, adv_usd, _MAX_ADV_PCT):
        log.info("Skipping %s: illiquid (position $%.0f vs ADV $%.0f)",
                 ticker, position_size_usd, adv_usd)
        return False
    portfolio.open_position(
        ticker=ticker,
        position_pct=score.position_pct,
        signal_id=signal_id,
        rationale=score.rationale,
        entry_price=entry_price,
        signal_source=signal_source,
    )
    return True


def run_morning_pipeline(portfolio: Portfolio) -> None:
    if not _is_trading_day():
        log.info("Market closed — skipping morning pipeline")
        return
    log.info("Morning pipeline started")
    portfolio.reset_daily_counter()
    portfolio.enforce_stop_losses()
    portfolio.enforce_take_profits()

    new_disclosures = run_scraper()
    qualified = filter_disclosures(new_disclosures)
    log.info("Disclosures: %d new, %d qualified", len(new_disclosures), len(qualified))

    sector_allocation = _compute_sector_allocation(portfolio)
    congress_skipped: set[str] = set()

    # ── Phase 1: congressional signals ───────────────────────────────────────
    for disc in qualified:
        if not portfolio.can_open_new_position():
            log.info("Position limit reached — stopping Phase 1")
            break
        try:
            ticker = disc["ticker"]
            committees = get_committees_for_politician(disc["politician"])
            sector = get_sector_for_ticker(ticker)
            lag = compute_lag_days(disc["transaction_date"], disc["disclosure_date"])
            since = (date.today() - timedelta(days=30)).isoformat()
            cluster_count = get_cluster_count(ticker, since)
            research = gather_research(ticker)

            score: EntryScore = score_entry(
                disc, committees=committees, sector=sector,
                lag_days=lag, estimated_cost_pct=_ESTIMATED_COST_PCT,
                research=research, cluster_count=cluster_count,
                signal_type="congressional",
            )

            if score.entry != "buy":
                congress_skipped.add(ticker)
                log.info("Skipping %s (congressional): conviction %d", ticker, score.conviction)
                continue

            signal_id = insert_signal(
                disc["id"], ticker, score.conviction,
                score.position_pct, score.rationale, list(score.risk_flags),
            )
            opened = _try_open(portfolio, ticker, score, signal_id, research,
                               sector_allocation, sector, "congressional")
            if opened:
                sector_allocation = _compute_sector_allocation(portfolio)
                log.info("Opened %s (congressional) conviction=%d cluster=%d",
                         ticker, score.conviction, cluster_count)

        except Exception:
            log.exception("Failed processing congressional signal %s — skipping",
                          disc.get("ticker", "?"))

    # ── Phase 2: fundamental screener ────────────────────────────────────────
    try:
        universe = list(get_universe())
        candidates = run_factor_screen(universe, top_n=_SCREENER_TOP_N)
        already_open = {pos["ticker"] for pos in get_open_positions()}

        for candidate in candidates:
            if not portfolio.can_open_new_position():
                log.info("Position limit reached — stopping Phase 2")
                break
            ticker = candidate.ticker
            if ticker in already_open:
                continue

            signal_type = "both" if ticker in congress_skipped else "fundamental"
            sector = get_sector_for_ticker(ticker)

            try:
                score = score_entry(
                    disclosure=None,
                    committees=[],
                    sector=sector,
                    lag_days=0,
                    estimated_cost_pct=_ESTIMATED_COST_PCT,
                    research=candidate.research,
                    signal_type=signal_type,
                    factor_score=candidate.composite_score,
                    ticker=ticker,
                )

                if score.entry != "buy":
                    log.info("Skipping %s (%s): conviction %d", ticker, signal_type, score.conviction)
                    continue

                opened = _try_open(portfolio, ticker, score, None, candidate.research,
                                   sector_allocation, sector, signal_type)
                if opened:
                    already_open.add(ticker)
                    sector_allocation = _compute_sector_allocation(portfolio)
                    log.info("Opened %s (%s) conviction=%d factor=%d",
                             ticker, signal_type, score.conviction, candidate.composite_score)

            except Exception:
                log.exception("Failed processing fundamental candidate %s — skipping", ticker)

    except Exception:
        log.exception("Phase 2 fundamental screener failed — skipping")


def run_exit_review(portfolio: Portfolio) -> None:
    if not _is_trading_day():
        return
    log.info("Exit review started")
    for pos in get_open_positions():
        ticker = pos["ticker"]
        try:
            info = yf.Ticker(ticker).info
            current_price = info.get("regularMarketPrice", pos["entry_price"])
            days_held = (date.today() - date.fromisoformat(pos["entry_date"])).days
            research = gather_research(ticker)
            decision = review_exit(
                ticker, pos["entry_price"], current_price, days_held,
                research=research,
            )
            signal_source = pos["signal_source"] if pos["signal_source"] else "congressional"
            if decision.action == "exit":
                portfolio.close_position(
                    ticker, pos["shares"],
                    exit_price=current_price,
                    exit_reason="ai_exit",
                    signal_id=pos["signal_id"],
                    entry_price=pos["entry_price"],
                    entry_date=pos["entry_date"],
                    signal_source=signal_source,
                )
                log.info("Closed %s: %s", ticker, decision.rationale)
            elif decision.action == "reduce":
                portfolio.reduce_position(
                    ticker, pos["shares"],
                    exit_price=current_price,
                    signal_id=pos["signal_id"],
                    entry_price=pos["entry_price"],
                    entry_date=pos["entry_date"],
                    signal_source=signal_source,
                )
                log.info("Reduced %s: %s", ticker, decision.rationale)
        except Exception:
            log.exception("Exit review failed for %s — skipping", ticker)


def run_eod_snapshot(portfolio: Portfolio) -> None:
    portfolio.log_snapshot()
    log.info("EOD snapshot logged")


def start(portfolio: Portfolio) -> None:
    scheduler = BlockingScheduler(timezone=_AMS)
    scheduler.add_job(refresh_universe, "cron", day_of_week="mon", hour=7, minute=0)
    scheduler.add_job(lambda: run_morning_pipeline(portfolio), "cron", hour=14, minute=0)
    scheduler.add_job(lambda: run_exit_review(portfolio), "cron", hour=15, minute=0)
    scheduler.add_job(lambda: run_eod_snapshot(portfolio), "cron", hour=22, minute=30)
    scheduler.add_job(log_weekly_report, "cron", day_of_week="fri", hour=22, minute=45)
    log.info("Scheduler started — running in Amsterdam time (Europe/Amsterdam)")
    scheduler.start()
