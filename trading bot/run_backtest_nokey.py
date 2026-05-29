"""Zero-API-key historical backtest.

Scrapes Capitol Trades history, applies basic filters (purchase / >= $15k /
lag <= 45 days), assigns a fixed conviction score, then runs the full
walk-forward backtesting engine against historical prices from yfinance.

No Anthropic, Alpaca, or ProPublica keys required.
"""
from __future__ import annotations

import logging
import re
import sys
import time
from datetime import date, datetime, UTC

import requests
import yfinance as yf
import pandas as pd
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TRADES_URL = "https://capitoltrades.com/trades"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; congress-backtest/1.0; research-only)"}
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")
MAX_LAG_DAYS = 45
MIN_TRADE_USD = 15_000
FIXED_CONVICTION = 65       # baseline conviction for all qualifying trades
FIXED_POSITION_PCT = 5.0    # % of NAV per signal
MAX_PAGES = 60              # 60 × 100 trades/page = up to 6,000 trades
MAX_PRICE_TICKERS = 60      # cap yfinance downloads to keep runtime reasonable

# ---------------------------------------------------------------------------
# Step 1: Scrape Capitol Trades
# ---------------------------------------------------------------------------

def _parse_amount(amount_range: str) -> int:
    digits = re.sub(r"[^\d]", "", amount_range.split("-")[0].split("–")[0])
    return int(digits) if digits else 0


def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.q-table tbody tr")
    trades = []
    for row in rows:
        cells = row.select("td")
        if len(cells) < 7:
            continue
        trade_id = row.get("data-id", "").strip()
        ticker = cells[2].get_text(strip=True).upper()
        if not trade_id or not ticker:
            continue
        trade = {
            "id": trade_id,
            "politician": cells[0].get_text(strip=True),
            "ticker": ticker,
            "transaction_type": cells[3].get_text(strip=True).lower(),
            "transaction_date": cells[4].get_text(strip=True),
            "disclosure_date": cells[5].get_text(strip=True),
            "amount_range": cells[6].get_text(strip=True),
        }
        if (
            _TICKER_RE.match(ticker)
            and _ISO_DATE_RE.match(trade.get("transaction_date", ""))
            and _ISO_DATE_RE.match(trade.get("disclosure_date", ""))
            and trade_id
        ):
            trades.append(trade)
    return trades


def scrape_history(max_pages: int = MAX_PAGES) -> list[dict]:
    all_trades: list[dict] = []
    seen_ids: set[str] = set()
    log.info("Scraping Capitol Trades (up to %d pages)...", max_pages)
    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(
                TRADES_URL,
                params={"page": page, "pageSize": 100},
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Page %d fetch failed: %s — stopping", page, exc)
            break

        trades = _parse_page(resp.text)
        if not trades:
            log.info("Page %d empty — reached end of data", page)
            break

        fresh = [t for t in trades if t["id"] not in seen_ids]
        for t in fresh:
            seen_ids.add(t["id"])
        all_trades.extend(fresh)

        if page % 10 == 0:
            oldest = min(t["transaction_date"] for t in all_trades)
            log.info("  Page %d — %d trades so far, oldest: %s", page, len(all_trades), oldest)

        time.sleep(0.6)  # polite delay

    log.info("Scraped %d total trades", len(all_trades))
    return all_trades


# ---------------------------------------------------------------------------
# Step 2: Filter to qualified buy signals
# ---------------------------------------------------------------------------

def filter_signals(trades: list[dict]) -> list[dict]:
    signals = []
    for t in trades:
        if t["transaction_type"] != "purchase":
            continue
        if _parse_amount(t.get("amount_range", "")) < MIN_TRADE_USD:
            continue
        try:
            tx_date = date.fromisoformat(t["transaction_date"])
            disc_date = date.fromisoformat(t["disclosure_date"])
        except ValueError:
            continue
        lag = (disc_date - tx_date).days
        if lag > MAX_LAG_DAYS or lag < 0:
            continue
        signals.append({
            "date": t["disclosure_date"],  # use disclosure date as signal date
            "ticker": t["ticker"],
            "conviction": FIXED_CONVICTION,
            "position_pct": FIXED_POSITION_PCT,
            "politician": t["politician"],
        })
    signals.sort(key=lambda s: s["date"])
    log.info("Qualified buy signals after filter: %d", len(signals))
    return signals


# ---------------------------------------------------------------------------
# Step 3: Fetch regime data (SPY + VIX)
# ---------------------------------------------------------------------------

def fetch_regime_data(years: int = 5) -> pd.DataFrame:
    log.info("Fetching SPY + VIX regime data (%d years)...", years)
    from market_data.market_feed import get_regime_data
    return get_regime_data(regime_ticker="SPY", vix_ticker="^VIX", years=years)


# ---------------------------------------------------------------------------
# Step 4: Fetch per-stock price data
# ---------------------------------------------------------------------------

def fetch_price_data(
    signals: list[dict],
    years: int = 5,
    max_tickers: int = MAX_PRICE_TICKERS,
) -> dict[str, pd.Series]:
    from collections import Counter
    ticker_counts = Counter(s["ticker"] for s in signals)
    tickers = [t for t, _ in ticker_counts.most_common(max_tickers)]
    log.info("Fetching price data for %d tickers (most signalled)...", len(tickers))
    price_data: dict[str, pd.Series] = {}
    for i, ticker in enumerate(tickers, 1):
        try:
            df = yf.download(ticker, period=f"{years+1}y", progress=False, auto_adjust=True)
            if df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            price_data[ticker] = df["Close"].dropna()
        except Exception as exc:
            log.warning("  Price fetch failed for %s: %s", ticker, exc)
        if i % 20 == 0:
            log.info("  %d/%d tickers fetched", i, len(tickers))
    log.info("Price data ready for %d tickers", len(price_data))
    return price_data


# ---------------------------------------------------------------------------
# Step 5: Run walk-forward backtest
# ---------------------------------------------------------------------------

def run_backtest(
    market_data: pd.DataFrame,
    signals: list[dict],
    price_data: dict[str, pd.Series],
) -> None:
    from backtesting.walk_forward import run_walk_forward
    from features.feature_pipeline import FeatureConfig
    from system.config import settings

    log.info("Starting walk-forward backtest...")
    result = run_walk_forward(
        market_data=market_data,
        signal_data=signals,
        price_data=price_data,
        regime_cfg=settings.regime,
        backtest_cfg=settings.backtest,
        alloc_cfg=settings.allocation,
        feature_cfg=FeatureConfig(
            vol_window=settings.features.vol_window,
            trend_window=settings.features.trend_window,
            momentum_window=settings.features.momentum_window,
            use_vix=settings.features.use_vix,
            use_momentum=settings.features.use_momentum,
            use_drawdown=settings.features.use_drawdown,
            min_history_bars=settings.features.min_history_bars,
        ),
        persist_to_db=False,
    )

    if not result.windows:
        log.error("No windows completed — not enough historical data or signals.")
        return

    # ── Per-window results ───────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"  WALK-FORWARD BACKTEST RESULTS  |  starting capital: $100,000")
    print("=" * 72)
    print(f"{'Window':<8} {'Test period':<24} {'Return':>8} {'Sharpe':>7} "
          f"{'MaxDD':>7} {'Trades':>7} {'vs SPY':>7}")
    print("-" * 72)

    for i, w in enumerate(result.windows, 1):
        ret  = w.metrics.get("total_return_pct", 0.0)
        sh   = w.metrics.get("sharpe", 0.0)
        mdd  = w.metrics.get("max_drawdown_pct", 0.0)
        n    = w.metrics.get("n_trades", 0)
        bah  = w.benchmarks.get("buy_and_hold", 0.0)
        excess = ret - bah
        print(f"  {i:<6} {w.test_start} → {w.test_end}  "
              f"{ret:>+7.1f}%  {sh:>6.2f}  {mdd:>6.1f}%  {n:>6}  {excess:>+6.1f}%")

    # ── Aggregated summary ───────────────────────────────────────────────────
    agg = result.aggregated_metrics
    n_windows = agg.get("n_windows", len(result.windows))

    print("-" * 72)
    print(f"\n  SUMMARY across {n_windows} out-of-sample windows")
    print(f"  {'Avg total return':<32} {agg.get('avg_total_return_pct', 0):>+7.1f}%")
    print(f"  {'Avg Sharpe ratio':<32} {agg.get('avg_sharpe', 0):>8.2f}")
    print(f"  {'Avg max drawdown':<32} {agg.get('avg_max_drawdown_pct', 0):>7.1f}%")
    print(f"  {'Avg win rate':<32} {agg.get('avg_win_rate', 0)*100:>7.1f}%")
    print(f"  {'Avg excess return vs SPY':<32} {agg.get('avg_excess_return_vs_buy_and_hold_pct', 0):>+7.1f}%")
    if "avg_beta" in agg:
        print(f"  {'Avg beta (vs SPY)':.<32} {agg.get('avg_beta', 0):>8.2f}")
        print(f"  {'Avg annualised alpha':.<32} {agg.get('avg_alpha_annualized_pct', 0):>+7.1f}%")
        print(f"  {'Avg information ratio':.<32} {agg.get('avg_information_ratio', 0):>8.2f}")
        print(f"  {'Avg downside beta':.<32} {agg.get('avg_downside_beta', 0):>8.2f}")

    # Implied $100k ending value (rough — uses avg return per window)
    avg_ret = agg.get("avg_total_return_pct", 0) / 100
    implied_end = 100_000 * (1 + avg_ret) ** n_windows
    print(f"\n  Implied $100k after {n_windows} × 6-month windows: "
          f"${implied_end:,.0f}")

    print("=" * 72)
    print("\nNote: signals use a fixed conviction score (no AI scoring).")
    print("This tests the raw congressional trade signal quality.")
    print("The live bot adds committee-sector filtering and AI scoring on top.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    os.environ.setdefault("OPENAI_API_KEY", "backtest-dummy")

    trades = scrape_history()
    if not trades:
        log.error("No trades scraped — check network access to capitoltrades.com")
        sys.exit(1)

    signals = filter_signals(trades)
    if not signals:
        log.error("No qualifying signals found after filtering")
        sys.exit(1)

    market_data = fetch_regime_data(years=5)
    if market_data.empty:
        log.error("Could not fetch SPY regime data")
        sys.exit(1)

    price_data = fetch_price_data(signals, years=5)
    if not price_data:
        log.error("No price data fetched")
        sys.exit(1)

    run_backtest(market_data, signals, price_data)
