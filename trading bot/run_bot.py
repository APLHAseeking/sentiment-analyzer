"""Entry point for the regime-aware congressional trading bot.

Two modes:
  python run_bot.py              — live paper mode (Alpaca paper API)
  python run_bot.py --simulated  — fully offline simulated mode (no API keys)
  python run_bot.py --backtest   — run walk-forward backtesting and exit

Safety:
  Live order execution is DISABLED. Only paper/simulated execution is supported.
  The AlpacaBroker is connected to the Alpaca PAPER endpoint by default.
"""
import argparse
import logging
import sys

from bot.db import init_db
from bot.universe import refresh_universe
from monitoring.logger import setup_logging
from system.config import settings


def _make_broker(simulated: bool):
    if simulated:
        from execution.paper_broker import SimulatedBroker
        return SimulatedBroker(
            initial_cash=settings.execution.initial_simulated_cash,
            slippage_bps=settings.execution.slippage_bps,
        )
    else:
        from bot.broker import AlpacaBroker
        return AlpacaBroker()


def run_paper(simulated: bool = False) -> None:
    setup_logging(settings.log_level)
    log = logging.getLogger(__name__)

    log.info("=== Congressional Trading Bot — Paper Mode ===")
    log.info("Broker: %s", "SimulatedBroker" if simulated else "AlpacaBroker (paper)")

    init_db()
    refresh_universe()

    broker = _make_broker(simulated)

    from orchestration.main_loop import RegimeAwareOrchestrator
    orchestrator = RegimeAwareOrchestrator(settings)
    orchestrator.initialize(broker)
    orchestrator.start()


def run_backtest() -> None:
    setup_logging(settings.log_level)
    log = logging.getLogger(__name__)
    log.info("=== Walk-Forward Backtest ===")

    import pandas as pd
    from market_data.market_feed import get_regime_data
    from backtesting.walk_forward import run_walk_forward
    from features.feature_pipeline import FeatureConfig

    market_data = get_regime_data(
        regime_ticker=settings.market_data.regime_ticker,
        vix_ticker=settings.market_data.vix_ticker,
        years=settings.market_data.history_years,
    )
    log.info("Market data loaded: %d bars", len(market_data))

    # Signal data: load from DB or CSV
    from bot.db import get_conn
    signals = []
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT d.disclosure_date as date, d.ticker,
                          s.conviction, s.position_pct
                   FROM signals s JOIN disclosures d ON s.disclosure_id = d.id
                   ORDER BY d.disclosure_date ASC"""
            ).fetchall()
            signals = [dict(r) for r in rows]
    except Exception as exc:
        log.warning("Could not load signals from DB: %s — using empty signal list", exc)

    log.info("Loaded %d historical signals", len(signals))

    # Price data for simulation: only tickers we have signals for
    tickers = list({s["ticker"] for s in signals})
    log.info("Fetching price data for %d tickers...", len(tickers))
    import yfinance as yf
    price_data = {}
    for ticker in tickers[:50]:  # limit for speed; increase as needed
        try:
            df = yf.download(ticker, period=f"{settings.market_data.history_years+1}y",
                             progress=False, auto_adjust=True)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
                price_data[ticker] = df["Close"]
        except Exception:
            pass

    log.info("Price data loaded for %d tickers", len(price_data))

    result = run_walk_forward(
        market_data=market_data,
        signal_data=signals,
        price_data=price_data,
        regime_cfg=settings.regime,
        backtest_cfg=settings.backtest,
        feature_cfg=FeatureConfig(
            vol_window=settings.features.vol_window,
            trend_window=settings.features.trend_window,
        ),
    )

    log.info("=== Backtest Results ===")
    for k, v in result.aggregated_metrics.items():
        log.info("  %s: %s", k, v)
    log.info("Detailed results saved to trading.db (backtest_results table)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Congressional Trading Bot")
    parser.add_argument("--simulated", action="store_true",
                        help="Use SimulatedBroker instead of Alpaca paper API")
    parser.add_argument("--backtest", action="store_true",
                        help="Run walk-forward backtest and exit")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    else:
        run_paper(simulated=args.simulated)


if __name__ == "__main__":
    main()
