import logging
from datetime import date
from zoneinfo import ZoneInfo

import yfinance as yf
import exchange_calendars as xcals
from apscheduler.schedulers.blocking import BlockingScheduler

from bot.analytics import log_weekly_report
from bot.researcher import gather_research
from bot.scraper import run_scraper
from bot.signal_engine import filter_disclosures, get_sector_for_ticker, compute_lag_days
from bot.committee import get_committees_for_politician
from bot.ai_analyst import score_entry, review_exit, EntryScore
from bot.db import get_open_positions, insert_signal
from bot.universe import refresh_universe
from bot.portfolio import Portfolio

log = logging.getLogger(__name__)
_AMS = ZoneInfo("Europe/Amsterdam")
_NYSE = xcals.get_calendar("XNYS")
_ESTIMATED_COST_PCT = 0.05

def _is_trading_day() -> bool:
    return _NYSE.is_session(date.today().isoformat())

def run_morning_pipeline(portfolio: Portfolio) -> None:
    if not _is_trading_day():
        log.info("Market closed — skipping morning pipeline")
        return
    log.info("Morning pipeline started")
    portfolio.reset_daily_counter()
    portfolio.enforce_stop_losses()
    new_disclosures = run_scraper()
    qualified = filter_disclosures(new_disclosures)
    log.info(f"Disclosures: {len(new_disclosures)} new, {len(qualified)} qualified")
    for disc in qualified:
        if not portfolio.can_open_new_position():
            log.info("Daily or total position limit reached — stopping")
            break
        try:
            committees = get_committees_for_politician(disc["politician"])
            sector = get_sector_for_ticker(disc["ticker"])
            lag = compute_lag_days(disc["transaction_date"], disc["disclosure_date"])
            research = gather_research(disc["ticker"])
            score: EntryScore = score_entry(
                disc, committees=committees, sector=sector,
                lag_days=lag, estimated_cost_pct=_ESTIMATED_COST_PCT,
                research=research,
            )
            if score.entry != "buy":
                log.info(f"Skipping {disc['ticker']}: conviction {score.conviction}")
                continue
            entry_price = yf.Ticker(disc["ticker"]).info.get("regularMarketPrice", 0)
            if not entry_price:
                log.warning(f"No price for {disc['ticker']} — skipping")
                continue
            signal_id = insert_signal(
                disc["id"], disc["ticker"], score.conviction,
                score.position_pct, score.rationale, list(score.risk_flags),
            )
            portfolio.open_position(
                ticker=disc["ticker"], position_pct=score.position_pct,
                signal_id=signal_id, rationale=score.rationale, entry_price=entry_price,
            )
            log.info(f"Opened {disc['ticker']} conviction={score.conviction}")
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
