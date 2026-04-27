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
from bot.universe import refresh_universe
from bot.portfolio import Portfolio

log = logging.getLogger(__name__)
_AMS = ZoneInfo("Europe/Amsterdam")
_NYSE = xcals.get_calendar("XNYS")
_ESTIMATED_COST_PCT = 0.05
_MAX_SECTOR_PCT = 30.0   # max % of NAV in any single sector
_MAX_ADV_PCT = 10.0      # max % of avg daily dollar volume per position

def _is_trading_day() -> bool:
    return _NYSE.is_session(date.today().isoformat())

def _compute_sector_allocation(portfolio: Portfolio) -> dict[str, float]:
    """Returns {sector: pct_of_nav} for all open broker positions."""
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
    log.info(f"Disclosures: {len(new_disclosures)} new, {len(qualified)} qualified")

    sector_allocation = _compute_sector_allocation(portfolio)

    for disc in qualified:
        if not portfolio.can_open_new_position():
            log.info("Daily or total position limit reached — stopping")
            break
        try:
            committees = get_committees_for_politician(disc["politician"])
            sector = get_sector_for_ticker(disc["ticker"])

            if portfolio.is_sector_capped(sector, sector_allocation, cap_pct=_MAX_SECTOR_PCT):
                log.info(
                    f"Skipping {disc['ticker']}: sector {sector!r} at "
                    f"{sector_allocation.get(sector, 0):.1f}% (cap {_MAX_SECTOR_PCT}%)"
                )
                continue

            lag = compute_lag_days(disc["transaction_date"], disc["disclosure_date"])
            since = (date.today() - timedelta(days=30)).isoformat()
            cluster_count = get_cluster_count(disc["ticker"], since)
            research = gather_research(disc["ticker"])

            score: EntryScore = score_entry(
                disc, committees=committees, sector=sector,
                lag_days=lag, estimated_cost_pct=_ESTIMATED_COST_PCT,
                research=research, cluster_count=cluster_count,
            )
            if score.entry != "buy":
                log.info(f"Skipping {disc['ticker']}: conviction {score.conviction}")
                continue

            entry_price = yf.Ticker(disc["ticker"]).info.get("regularMarketPrice", 0)
            if not entry_price:
                log.warning(f"No price for {disc['ticker']} — skipping")
                continue

            position_size_usd = portfolio.get_cash() * score.position_pct / 100
            adv_usd = research.avg_daily_volume_usd if research else None
            if adv_usd and not portfolio.is_liquid_enough(position_size_usd, adv_usd, _MAX_ADV_PCT):
                log.info(
                    f"Skipping {disc['ticker']}: position ${position_size_usd:,.0f} "
                    f"is >{_MAX_ADV_PCT}% of ADV ${adv_usd:,.0f}"
                )
                continue

            signal_id = insert_signal(
                disc["id"], disc["ticker"], score.conviction,
                score.position_pct, score.rationale, list(score.risk_flags),
            )
            portfolio.open_position(
                ticker=disc["ticker"], position_pct=score.position_pct,
                signal_id=signal_id, rationale=score.rationale, entry_price=entry_price,
            )
            sector_allocation[sector] = sector_allocation.get(sector, 0.0) + score.position_pct
            log.info(f"Opened {disc['ticker']} conviction={score.conviction} cluster={cluster_count}")
        except Exception:
            log.exception(f"Failed processing {disc.get('ticker', '?')} — skipping")

def run_exit_review(portfolio: Portfolio) -> None:
    if not _is_trading_day():
        return
    log.info("Exit review started")
    for pos in get_open_positions():
        try:
            info = yf.Ticker(pos["ticker"]).info
            current_price = info.get("regularMarketPrice", pos["entry_price"])
            days_held = (date.today() - date.fromisoformat(pos["entry_date"])).days
            research = gather_research(pos["ticker"])
            decision = review_exit(
                pos["ticker"], pos["entry_price"], current_price, days_held,
                research=research,
            )
            if decision.action == "exit":
                portfolio.close_position(
                    pos["ticker"], pos["shares"],
                    exit_price=current_price,
                    exit_reason="ai_exit",
                    signal_id=pos["signal_id"] or 0,
                    entry_price=pos["entry_price"],
                    entry_date=pos["entry_date"],
                )
                log.info(f"Closed {pos['ticker']}: {decision.rationale}")
            elif decision.action == "reduce":
                portfolio.reduce_position(
                    pos["ticker"], pos["shares"],
                    exit_price=current_price,
                    signal_id=pos["signal_id"] or 0,
                    entry_price=pos["entry_price"],
                    entry_date=pos["entry_date"],
                )
                log.info(f"Reduced {pos['ticker']}: {decision.rationale}")
        except Exception:
            log.exception(f"Exit review failed for {pos.get('ticker', '?')} — skipping")

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
