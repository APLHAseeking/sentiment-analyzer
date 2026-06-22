"""Focused backtest: Oct 2025 → May 2026 (7 months of cached Capitol Trades data).

Note: Capitol Trades is now a JavaScript SPA — HTML scraping is broken.
This script uses the locally cached trade data (capitol_trades_merged.json),
which covers Oct 2025 → May 2026. The May–Oct 2025 period is not in cache.

Trains HMM regime model on 3 years of prior SPY/VIX data (available from
yfinance), simulates the bot on the test period, and compares vs SPY.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import date

import pandas as pd
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

os.environ.setdefault("OPENAI_API_KEY", "backtest-dummy")

# ---------------------------------------------------------------------------
# Parameters — limited to what the cache covers
# ---------------------------------------------------------------------------
TEST_START   = "2025-10-13"   # oldest disclosure date in cache
TEST_END     = "2026-05-16"   # last full trading week
CACHE_FILE   = "capitol_trades_merged.json"
MAX_LAG_DAYS = 45
MIN_TRADE_USD = 15_000
FIXED_CONVICTION = 65
FIXED_POSITION_PCT = 5.0
MAX_PRICE_TICKERS = 60
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_amount(amount_range: str) -> int:
    """Parse '15K–50K' or '250,000–500,000' to lower-bound int."""
    part = amount_range.split("–")[0].split("-")[0].strip()
    m = re.match(r"([\d,.]+)\s*([KkMm]?)", part.replace(",", ""))
    if not m:
        return 0
    num = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix == "K":
        num *= 1_000
    elif suffix == "M":
        num *= 1_000_000
    return int(num)


# ---------------------------------------------------------------------------
# Step 1: Load & filter cached signals
# ---------------------------------------------------------------------------
def load_signals() -> list[dict]:
    with open(CACHE_FILE) as f:
        raw = json.load(f)
    trades = raw.get("trades", raw) if isinstance(raw, dict) else raw
    log.info("Loaded %d raw trades from cache (%s)", len(trades), CACHE_FILE)

    signals = []
    for t in trades:
        if t.get("transaction_type", "").lower() not in ("purchase", "buy"):
            continue
        ticker = t.get("ticker", "").upper()
        if not _TICKER_RE.match(ticker):
            continue
        disc_date = t.get("disclosure_date", "")
        if not _ISO_DATE_RE.match(disc_date):
            continue
        if not (TEST_START <= disc_date <= TEST_END):
            continue
        if _parse_amount(t.get("amount_range", "")) < MIN_TRADE_USD:
            continue
        lag = t.get("lag")
        if lag is None:
            try:
                tx = date.fromisoformat(t["transaction_date"])
                dc = date.fromisoformat(disc_date)
                lag = (dc - tx).days
            except (KeyError, ValueError):
                continue
        if not (0 <= int(lag) <= MAX_LAG_DAYS):
            continue
        signals.append({
            "date": disc_date,
            "ticker": ticker,
            "conviction": FIXED_CONVICTION,
            "position_pct": FIXED_POSITION_PCT,
            "politician": t.get("politician", ""),
        })

    signals.sort(key=lambda s: s["date"])
    log.info("Qualifying buy signals in %s → %s: %d", TEST_START, TEST_END, len(signals))
    return signals


# ---------------------------------------------------------------------------
# Step 2: Fetch market + price data
# ---------------------------------------------------------------------------
def fetch_market_data() -> pd.DataFrame:
    log.info("Fetching SPY + VIX data (5 years)...")
    from market_data.market_feed import get_regime_data
    return get_regime_data(regime_ticker="SPY", vix_ticker="^VIX", years=5)


def fetch_price_data(signals: list[dict]) -> dict[str, pd.Series]:
    ticker_counts = Counter(s["ticker"] for s in signals)
    tickers = [t for t, _ in ticker_counts.most_common(MAX_PRICE_TICKERS)]
    log.info("Downloading prices for %d tickers...", len(tickers))
    price_data: dict[str, pd.Series] = {}
    for i, ticker in enumerate(tickers, 1):
        try:
            df = yf.download(ticker, start="2020-01-01", end="2026-05-20",
                             progress=False, auto_adjust=True)
            if df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            price_data[ticker] = df["Close"].dropna()
        except Exception as exc:
            log.warning("  Price fetch failed for %s: %s", ticker, exc)
        if i % 20 == 0:
            log.info("  %d/%d tickers done", i, len(tickers))
    log.info("Price data ready for %d tickers", len(price_data))
    return price_data


# ---------------------------------------------------------------------------
# Step 3: Train regime model on pre-test data
# ---------------------------------------------------------------------------
def build_regime_map(market_data: pd.DataFrame) -> dict[str, str]:
    from features.feature_pipeline import FeatureConfig
    from regime.hmm_engine import HMMRegimeEngine
    from system.config import settings

    train_mask = market_data.index < pd.Timestamp(TEST_START)
    train_data = market_data.loc[train_mask]
    log.info("Training HMM on %d bars (through %s)...", len(train_data), TEST_START)

    engine = HMMRegimeEngine(settings.regime)
    feat_cfg = FeatureConfig(
        vol_window=settings.features.vol_window,
        trend_window=settings.features.trend_window,
        momentum_window=settings.features.momentum_window,
        use_vix=settings.features.use_vix,
        use_momentum=settings.features.use_momentum,
        use_drawdown=settings.features.use_drawdown,
        min_history_bars=settings.features.min_history_bars,
    )
    engine.fit(train_data, feat_cfg)

    # Classify full period so test-period signals get a regime label.
    # update_recent_labels=True so is_stable reflects real bar-by-bar tracking
    # over this single full-history call (this script does not otherwise build
    # _recent_labels incrementally the way the live orchestrator does).
    all_states = engine.classify(market_data, feat_cfg, update_recent_labels=True)
    log.info("HMM classified %d states, n_regimes=%d", len(all_states), engine.n_regimes)

    regime_map = {s.date: (s.regime_label, s.confidence) for s in all_states}
    return regime_map


# ---------------------------------------------------------------------------
# Step 4: Enrich signals with regime
# ---------------------------------------------------------------------------
def enrich_signals(signals: list[dict], regime_map: dict) -> list[dict]:
    from system.config import settings
    alloc_cfg = settings.allocation
    enriched = []
    skipped_conf = 0
    sorted_dates = sorted(regime_map.keys())

    for sig in signals:
        d = sig["date"]
        if d in regime_map:
            label, conf = regime_map[d]
        else:
            prev = [k for k in sorted_dates if k <= d]
            if not prev:
                continue
            label, conf = regime_map[prev[-1]]

        if conf < alloc_cfg.min_confidence_to_trade:
            skipped_conf += 1
            continue

        mult = alloc_cfg.regime_size_multiplier.get(label, 1.0)
        if alloc_cfg.confidence_scale:
            lo = alloc_cfg.min_confidence_to_trade
            conf_mult = 0.5 + 0.5 * (conf - lo) / max(1.0 - lo, 1e-6)
            mult *= min(conf_mult, 1.0)

        enriched.append({
            **sig,
            "position_pct": sig["position_pct"] * mult,
            "regime_label": label,
            "regime_confidence": conf,
        })

    log.info("Enriched signals: %d (skipped %d low-confidence)", len(enriched), skipped_conf)
    return enriched


# ---------------------------------------------------------------------------
# Step 5: Simulate & compute metrics
# ---------------------------------------------------------------------------
def run_simulation(signals: list[dict], price_data: dict[str, pd.Series],
                   initial_cash: float = 100_000.0) -> None:
    from backtesting.simulation import simulate_portfolio, equity_series, trade_returns
    from backtesting.metrics import compute_all
    from backtesting.benchmarks import buy_and_hold, trend_following
    from backtesting.metrics import total_return as _total_return
    from backtesting.stress_test import DEFAULT_STRESS_SCENARIOS, apply_stress_scenario
    from system.config import settings

    # Restrict price data to test window
    test_prices = {
        ticker: s.loc[
            (s.index >= pd.Timestamp(TEST_START)) &
            (s.index <= pd.Timestamp(TEST_END))
        ]
        for ticker, s in price_data.items()
    }
    test_prices = {t: s for t, s in test_prices.items() if not s.empty}

    sim = simulate_portfolio(
        signals=signals,
        price_data=test_prices,
        initial_cash=initial_cash,
        slippage_bps=settings.backtest.slippage_bps,
        commission_pct=settings.backtest.commission_pct,
    )

    eq = equity_series(sim)
    tr = trade_returns(sim)

    # SPY benchmarks over same period
    spy_raw = yf.download("SPY", start=TEST_START, end="2026-05-20",
                          progress=False, auto_adjust=True)
    if isinstance(spy_raw.columns, pd.MultiIndex):
        spy_raw.columns = spy_raw.columns.droplevel(1)
    spy_close = spy_raw["Close"].dropna()
    spy_returns = spy_close.pct_change().dropna()

    m = compute_all(eq, tr, trades=sim.trades,
                    total_volume_traded=sim.total_volume_traded,
                    benchmark_returns=spy_returns)

    bah_eq = buy_and_hold(spy_close, initial_cash=initial_cash,
                          slippage_bps=settings.backtest.slippage_bps,
                          commission_pct=settings.backtest.commission_pct)
    bah_ret = _total_return(bah_eq) * 100

    tf_eq = trend_following(spy_close, initial_cash=initial_cash,
                            slippage_bps=settings.backtest.slippage_bps,
                            commission_pct=settings.backtest.commission_pct)
    tf_ret = _total_return(tf_eq) * 100 if not tf_eq.empty else None

    # ── Print results ────────────────────────────────────────────────────────
    strat_ret = m.get("total_return_pct", 0.0)
    n_months = round((pd.Timestamp(TEST_END) - pd.Timestamp(TEST_START)).days / 30.4)
    print("\n" + "=" * 70)
    print(f"  {n_months}-MONTH BACKTEST  |  {TEST_START}  →  {TEST_END}")
    print(f"  (Capitol Trades cache covers Oct 2025 onward; May–Oct 2025 not available)")
    print(f"  Starting capital: ${initial_cash:,.0f}")
    print("=" * 70)

    end_val = initial_cash * (1 + strat_ret / 100)
    print(f"\n  {'Strategy (congressional signals)':.<40} ${end_val:>10,.0f}")
    print(f"  {'  Total return':.<40} {strat_ret:>+9.2f}%")
    print(f"  {'  Sharpe ratio':.<40} {m.get('sharpe', 0):>10.2f}")
    print(f"  {'  Sortino ratio':.<40} {m.get('sortino', 0):>10.2f}")
    print(f"  {'  Max drawdown':.<40} {m.get('max_drawdown_pct', 0):>9.1f}%")
    print(f"  {'  Win rate':.<40} {m.get('win_rate', 0)*100:>9.1f}%")
    print(f"  {'  Trades executed':.<40} {m.get('n_trades', 0):>10}")

    print(f"\n  {'SPY buy-and-hold':.<40} {bah_ret:>+9.2f}%")
    if tf_ret is not None:
        print(f"  {'SPY trend-following (200d MA)':.<40} {tf_ret:>+9.2f}%")

    excess = strat_ret - bah_ret
    print(f"\n  {'Excess return vs SPY (unadjusted)':.<40} {excess:>+9.2f}%")

    # ── Risk-adjusted attribution ─────────────────────────────────────────────
    b_val  = m.get("beta")
    a_val  = m.get("alpha_annualized_pct")
    ir_val = m.get("information_ratio")
    db_val = m.get("downside_beta")
    if b_val is not None:
        print(f"\n  {'Beta (vs SPY)':.<40} {b_val:>10.2f}")
        print(f"  {'Downside beta':.<40} {db_val:>10.2f}")
        print(f"  {'Annualised alpha':.<40} {a_val:>+9.2f}%")
        print(f"  {'Information ratio':.<40} {ir_val:>10.2f}")
        spy_ann = spy_returns.mean() * 252 * 100
        note = (
            "Note: book is mostly cash — total-return vs SPY is not a "
            "risk-adjusted comparison. Use alpha/IR above."
            if m.get("n_trades", 0) < 10 else ""
        )
        if note:
            print(f"\n  {note}")

    # Regime breakdown
    if sim.trades:
        from collections import defaultdict
        regime_pnl: dict[str, list[float]] = defaultdict(list)
        for t in sim.trades:
            regime_pnl[t.regime_at_entry].append(t.pnl_pct)

        print("\n  Performance by regime at entry:")
        print(f"  {'Regime':<16} {'Trades':>7} {'Avg P&L%':>10} {'Win%':>8}")
        print("  " + "-" * 44)
        for regime, pnls in sorted(regime_pnl.items()):
            wins = sum(1 for p in pnls if p > 0)
            print(f"  {regime:<16} {len(pnls):>7} {sum(pnls)/len(pnls):>+9.1f}% "
                  f"{wins/len(pnls)*100:>7.0f}%")

    # Top / bottom trades
    if sim.trades:
        by_pnl = sorted(sim.trades, key=lambda t: t.pnl_pct, reverse=True)
        print("\n  Top 5 trades:")
        for t in by_pnl[:5]:
            print(f"  {'':2}{t.ticker:<6} {t.entry_date} → {t.exit_date}  "
                  f"{t.pnl_pct:>+6.1f}%  exit: {t.exit_reason}")
        print("\n  Worst 5 trades:")
        for t in by_pnl[-5:]:
            print(f"  {'':2}{t.ticker:<6} {t.entry_date} → {t.exit_date}  "
                  f"{t.pnl_pct:>+6.1f}%  exit: {t.exit_reason}")

    # ── Stress tests ──────────────────────────────────────────────────────────
    print("\n  Stress tests (top 3 scenarios):")
    for scenario in DEFAULT_STRESS_SCENARIOS[:3]:
        try:
            s_prices, s_slip, s_delay = apply_stress_scenario(
                test_prices, settings.backtest.slippage_bps, scenario
            )
            s_sim = simulate_portfolio(
                signals=signals,
                price_data=s_prices,
                initial_cash=initial_cash,
                slippage_bps=s_slip,
                commission_pct=settings.backtest.commission_pct,
                fill_delay_bars=s_delay,
            )
            s_eq = equity_series(s_sim)
            s_tr = trade_returns(s_sim)
            s_m = compute_all(s_eq, s_tr)
            s_sharpe = s_m.get("sharpe", float("nan"))
            s_dd = s_m.get("max_drawdown_pct", float("nan"))
            s_alpha = s_m.get("alpha_annualized_pct")
            if s_alpha is not None:
                print(f"  [{scenario.name}] Sharpe={s_sharpe:.2f}  "
                      f"MaxDD={s_dd:.1f}%  Alpha={s_alpha:+.1f}%/yr")
            else:
                print(f"  [{scenario.name}] Sharpe={s_sharpe:.2f}  MaxDD={s_dd:.1f}%")
        except Exception as exc:
            print(f"  [{scenario.name}] failed: {exc}")

    print("\n" + "=" * 70)
    # Beta-adjusted verdict: use alpha rather than raw excess return
    if a_val is not None:
        if a_val > 2.0 and (ir_val or 0) > 0.3:
            verdict_line = f"  ALPHA: {a_val:+.1f}%/yr  |  IR: {ir_val:.2f}  — positive factor-adjusted edge"
        elif a_val < -2.0:
            verdict_line = f"  ALPHA: {a_val:+.1f}%/yr  |  IR: {ir_val:.2f}  — negative factor-adjusted alpha"
        else:
            verdict_line = f"  ALPHA: {a_val:+.1f}%/yr  |  IR: {ir_val:.2f}  — inconclusive (magnitude too small)"
    else:
        verdict_line = f"  Excess return vs SPY: {excess:+.1f}pp (not beta-adjusted — no benchmark data)"
    print(verdict_line)
    if m.get("n_trades", 0) < 10:
        print("  WARNING: Very few trades — results not statistically reliable.")
    print("=" * 70)
    print("\nNote: fixed conviction (no AI scoring), no committee-sector filter.")
    print("This is the raw congressional signal. The live bot layers on top of this.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    signals = load_signals()
    if not signals:
        log.error("No qualifying signals in test window — check cache file.")
        sys.exit(1)

    market_data = fetch_market_data()
    if market_data.empty:
        log.error("No market data fetched.")
        sys.exit(1)

    price_data = fetch_price_data(signals)
    if not price_data:
        log.error("No price data fetched.")
        sys.exit(1)

    regime_map = build_regime_map(market_data)
    enriched = enrich_signals(signals, regime_map)
    if not enriched:
        log.error("No signals survived regime enrichment.")
        sys.exit(1)

    run_simulation(enriched, price_data)
