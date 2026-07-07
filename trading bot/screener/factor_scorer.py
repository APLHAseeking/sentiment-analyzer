from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass
from datetime import datetime, UTC

import pandas as pd
import yfinance as yf

from bot.researcher import gather_research, ResearchReport

log = logging.getLogger(__name__)

_MIN_VALID_METRICS = 4
_CHUNK_SIZE = 50    # tickers per chunk
_CHUNK_DELAY = 1.0  # seconds between chunks
_MAX_RETRIES = 2
_MIN_MOMENTUM_BARS = 200  # require at least 200 trading days in the 12-month window
_MIN_SECTOR_SIZE = 5      # sectors smaller than this fall back to universe-wide ranking


def _to_float(v: object) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class FactorCandidate:
    ticker: str
    composite_score: int
    value_score: int
    momentum_score: int
    quality_score: int
    research: ResearchReport | None
    low_vol_score: int = 0
    reversal_score: int = 0


def _fetch_info_with_retry(ticker: str) -> tuple[str, dict | None]:
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return ticker, yf.Ticker(ticker).info
        except Exception as exc:
            if attempt < _MAX_RETRIES:
                time.sleep(1.0 * (attempt + 1))
            else:
                return ticker, None
    return ticker, None


# Keep _fetch_info as an alias so existing tests that mock it still work.
_fetch_info = _fetch_info_with_retry


def _fetch_all_infos(tickers: list[str]) -> dict[str, dict | None]:
    """Fetch yfinance info in chunks to avoid rate limits."""
    results: dict[str, dict | None] = {}
    for i in range(0, len(tickers), _CHUNK_SIZE):
        chunk = tickers[i:i + _CHUNK_SIZE]
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            chunk_results = list(pool.map(_fetch_info_with_retry, chunk))
        for ticker, info in chunk_results:
            results[ticker] = info
        if i + _CHUNK_SIZE < len(tickers):
            time.sleep(_CHUNK_DELAY)
    return results


def _extract_close(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame | None:
    """Pull a Close DataFrame (columns = tickers) from a yf.download result.

    Handles both the MultiIndex layout (multi-ticker) and the flat layout
    (single-ticker / older yfinance). Returns None if no Close data is present.
    """
    if isinstance(raw.columns, pd.MultiIndex):
        return raw["Close"]
    if "Close" in raw.columns:
        close = raw[["Close"]]
        if len(tickers) == 1:
            close.columns = tickers  # name the single column with the ticker
        return close
    log.warning("_extract_close: unexpected columns %s", raw.columns.tolist())
    return None


def _fetch_spy_close() -> pd.Series | None:
    """Download SPY daily close (12mo) for beta / residual-momentum computation.

    One download per screen, shared across the whole universe. Returns None on
    failure (callers degrade to neutral for the beta-based factors)."""
    try:
        raw = yf.download("SPY", period="12mo", auto_adjust=True, progress=False)
        close = _extract_close(raw, ["SPY"])
        if close is None or close.empty:
            return None
        col = close["SPY"] if "SPY" in close.columns else close.iloc[:, 0]
        return col.dropna()
    except Exception:
        return None


def _fetch_momentum_batch(
    tickers: list[str],
    close: pd.DataFrame | None = None,
) -> dict[str, tuple[float | None, float | None, float | None, float | None]]:
    """Fetch momentum signals. Returns (mom_1m, mom_12m, mom_6m, high52_ratio) per ticker.

    mom_12m is 12-month total return — the academically valid momentum signal.
    mom_6m is 6-month return skipping the last month (avoids short-term reversal).
    high52_ratio is current price / 52-week high (George & Hwang 2004 breakout signal).
    mom_1m is 1-month return (mean-reverting; kept for research display only,
    NOT used in the composite score). Tickers with fewer than _MIN_MOMENTUM_BARS
    of history are excluded (momentum is unreliable on thin data).

    `close` may be a pre-downloaded Close DataFrame (shared with the price-factor
    fetch to avoid downloading the universe twice); when None it is fetched here.
    """
    if not tickers:
        return {}
    try:
        if close is None:
            raw = yf.download(tickers, period="12mo", auto_adjust=True, progress=False)
            close = _extract_close(raw, tickers)
        if close is None:
            return {t: (None, None, None, None) for t in tickers}

        result: dict[str, tuple[float | None, float | None, float | None, float | None]] = {}
        for t in tickers:
            try:
                col = close[t].dropna() if t in close.columns else pd.Series(dtype=float)
                if len(col) < _MIN_MOMENTUM_BARS:
                    result[t] = (None, None, None, None)
                    continue
                current = float(col.iloc[-1])
                p1m = float(col.iloc[max(0, len(col) - 21)])
                # Anchor to the trailing 252 bars (not the whole fetch window) so
                # mom_12m means the same thing regardless of how much history came back.
                p12m = float(col.iloc[max(0, len(col) - 252)])
                # 6-1 momentum: return from ~6 months ago to ~1 month ago (skip last month)
                p6m_start = float(col.iloc[max(0, len(col) - 126)])
                p6m_end = float(col.iloc[max(0, len(col) - 21)])
                mom_6m = (p6m_end / p6m_start - 1) * 100 if p6m_start > 0 else None
                # 52-week high ratio: how close current price is to the 52-week high
                high52 = float(col.max())
                high52_ratio = current / high52 if high52 > 0 else None
                result[t] = (
                    (current / p1m - 1) * 100 if p1m > 0 else None,   # 1m (display only)
                    (current / p12m - 1) * 100 if p12m > 0 else None,  # 12m (used in score)
                    mom_6m,                                              # 6-1m (used in score)
                    high52_ratio,                                        # proximity to 52w high
                )
            except Exception:
                result[t] = (None, None, None, None)
        return result
    except Exception:
        return {t: (None, None, None, None) for t in tickers}


def _fetch_price_factors_batch(
    tickers: list[str],
    close: pd.DataFrame | None = None,
    spy_close: pd.Series | None = None,
) -> dict[str, tuple[float | None, float | None, float | None]]:
    """Fetch price-based factor signals. Returns (realized_vol, beta, resid_mom).

    realized_vol — annualized stdev of daily returns (%), the low-volatility signal
        (lower is better). beta — OLS slope of stock daily returns vs SPY, the
        Betting-Against-Beta signal (lower/negative is better). resid_mom — cumulative
        residual return (%) of (stock_ret − beta·spy_ret) over months −12..−1 (skips
        the last ~21 bars to avoid short-term reversal), the idiosyncratic-momentum
        signal. All zero-cost: computed from the same price history as momentum plus a
        single SPY download. Missing/thin data yields None (ranked neutrally upstream).
    """
    if not tickers:
        return {}
    try:
        if close is None:
            raw = yf.download(tickers, period="12mo", auto_adjust=True, progress=False)
            close = _extract_close(raw, tickers)
        if close is None:
            return {t: (None, None, None) for t in tickers}
        if spy_close is None:
            spy_close = _fetch_spy_close()
        spy_ret = spy_close.pct_change().dropna() if spy_close is not None else None

        result: dict[str, tuple[float | None, float | None, float | None]] = {}
        for t in tickers:
            try:
                col = close[t].dropna() if t in close.columns else pd.Series(dtype=float)
                if len(col) < _MIN_MOMENTUM_BARS:
                    result[t] = (None, None, None)
                    continue
                ret = col.pct_change().dropna()
                vol = float(ret.std() * (252 ** 0.5) * 100) if ret.std() and ret.std() > 0 else None
                beta: float | None = None
                resid_mom: float | None = None
                if spy_ret is not None and len(spy_ret) > 0:
                    aligned = pd.concat([ret, spy_ret], axis=1, join="inner").dropna()
                    aligned.columns = ["stock", "spy"]
                    spy_var = aligned["spy"].var()
                    if len(aligned) >= _MIN_MOMENTUM_BARS and spy_var and spy_var > 0:
                        beta = float(aligned["stock"].cov(aligned["spy"]) / spy_var)
                        resid = aligned["stock"] - beta * aligned["spy"]
                        # Skip the last ~21 bars (1 month) to avoid short-term reversal,
                        # mirroring the 6-1 momentum convention.
                        resid_window = resid.iloc[:max(0, len(resid) - 21)]
                        if len(resid_window) > 0:
                            resid_mom = float(((1 + resid_window).prod() - 1) * 100)
                result[t] = (vol, beta, resid_mom)
            except Exception:
                result[t] = (None, None, None)
        return result
    except Exception:
        return {t: (None, None, None) for t in tickers}


def _build_factor_df(
    infos: dict[str, dict | None],
    momentum: dict[str, tuple[float | None, float | None, float | None, float | None]],
    price_factors: dict[str, tuple[float | None, float | None, float | None]] | None = None,
) -> pd.DataFrame:
    price_factors = price_factors or {}
    rows = []
    for ticker, info in infos.items():
        if info is None:
            continue
        try:
            pe = _to_float(info.get("trailingPE"))
            pb = _to_float(info.get("priceToBook"))
            fcf = _to_float(info.get("freeCashflow"))
            mcap = _to_float(info.get("marketCap"))
            roe = _to_float(info.get("returnOnEquity"))
            margin = _to_float(info.get("profitMargins"))
            de = _to_float(info.get("debtToEquity"))
            fcf_yield = fcf / mcap if fcf and mcap and mcap > 0 else None
            # New signals from yfinance.info
            evebitda = _to_float(info.get("enterpriseToEbitda"))
            gross_margin = _to_float(info.get("grossMargins"))
            current_ratio = _to_float(info.get("currentRatio"))
            earnings_growth = _to_float(info.get("earningsGrowth"))
            mom1m, mom12m, mom6m, high52 = momentum.get(ticker, (None, None, None, None))
            vol, beta, resid_mom = price_factors.get(ticker, (None, None, None))
            sector = (info.get("sector") or "Unknown").strip() or "Unknown"

            rows.append({
                "ticker": ticker,
                "sector": sector,
                "pe_inv": -pe if pe and pe > 0 else None,
                "pb_inv": -pb if pb and pb > 0 else None,
                "fcf_yield": fcf_yield,
                "evebitda_inv": -evebitda if evebitda and evebitda > 0 else None,
                "roe": roe,
                "gross_margin": gross_margin,
                "margin": margin,
                "de_inv": -de if de is not None and de >= 0 else None,
                "current_ratio": current_ratio,
                "earnings_growth": earnings_growth,
                "mom_1m": mom1m,
                "mom_12m": mom12m,
                "mom_6m": mom6m,
                "high52_ratio": high52,
                # Price-based factors: invert so "lower is better" ranks higher.
                "vol_inv": -vol if vol is not None and vol > 0 else None,
                "beta_inv": -beta if beta is not None else None,
                "resid_mom": resid_mom,
                # Short-term reversal (mean reversion): last-month losers tend to
                # bounce. -mom_1m so the most oversold names rank highest. Regime-
                # gated in the composite (favoured in neutral/range-bound, suppressed
                # in strong trends and crashes — see _REGIME_WEIGHTS).
                "reversal": -mom1m if mom1m is not None else None,
            })
        except Exception:
            log.debug("Skipping %s in factor_df build", ticker, exc_info=True)
            continue

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.set_index("ticker")
    return df


def _centered_rank(frame: pd.DataFrame) -> pd.DataFrame:
    """Percentile rank centered so every group averages 0.5 regardless of size.

    Plain ``rank(pct=True)`` over n members yields ranks {1/n .. 1} with mean
    (n+1)/2n, which systematically inflates scores for names in small sectors
    (a 5-member sector's average name would score 0.6). The centered variant
    ((rank − 0.5) / n, per column over non-NaN members) averages exactly 0.5
    for any group size, keeping sleeve scores comparable across sectors.
    """
    ranks = frame.rank(na_option="keep")
    return (ranks - 0.5).div(frame.count())


def _sector_rank(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Rank each column within sector for sectors >= _MIN_SECTOR_SIZE members.

    Sectors that are too small (< _MIN_SECTOR_SIZE tickers with data) fall back
    to the universe-wide percentile rank. This prevents a utility company's low
    P/E from outranking a genuinely cheap tech company across sectors.
    Ranks are centered (see _centered_rank) so small sectors get no size bonus.
    """
    # Universe-wide fallback ranks
    universe_ranked = _centered_rank(df[cols])

    if "sector" not in df.columns:
        return universe_ranked

    result = universe_ranked.copy()
    sector_counts = df["sector"].value_counts()
    large_sectors = sector_counts[sector_counts >= _MIN_SECTOR_SIZE].index

    for sector in large_sectors:
        mask = df["sector"] == sector
        sector_slice = df.loc[mask, cols]
        result.loc[mask, cols] = _centered_rank(sector_slice)

    return result


# Regime-specific factor weights (value, momentum, quality, low_vol, reversal — sum to 1.0).
# Tuned from the PIT backtest in backtesting/backtest_price_factors.py:
#   - Residual momentum (inside the momentum sleeve) was the strongest sleeve
#     (Sharpe ~0.88, +5.8%/yr alpha vs SPY) -> momentum is weighted heavily in
#     trending regimes (bull/euphoria/melt-up).
#   - Low-vol/BAB was defensive (beta ~0.6, drawdown below SPY) -> weighted up in
#     bear/crash where capital preservation matters.
#   - Short-term reversal (mean reversion) works best in choppy/range-bound tape and
#     for oversold bounces -> weighted up in neutral/bear, suppressed in strong trends
#     (momentum dominates) and crashes (avoid catching falling knives).
_REGIME_WEIGHTS: dict[str, tuple[float, float, float, float, float]] = {
    "crash":     (0.35, 0.05, 0.35, 0.20, 0.05),
    "deep-bear": (0.35, 0.05, 0.35, 0.20, 0.05),
    "bear":      (0.30, 0.10, 0.30, 0.15, 0.15),
    "neutral":   (0.25, 0.25, 0.25, 0.10, 0.15),
    "bull":      (0.20, 0.40, 0.25, 0.10, 0.05),
    "euphoria":  (0.25, 0.40, 0.25, 0.05, 0.05),
    "melt-up":   (0.25, 0.40, 0.25, 0.05, 0.05),
}
_DEFAULT_WEIGHTS: tuple[float, float, float, float, float] = (0.25, 0.25, 0.25, 0.10, 0.15)

# Sub-signal weights inside the momentum sleeve (renormalised over present signals).
# Residual (idiosyncratic) momentum is the largest weight by design — it was the
# strongest single sleeve in the PIT backtest (docs/FACTOR_BACKTEST_2026-06-28.md),
# so it drives the momentum signal more than the raw price-momentum sub-signals.
_MOMENTUM_WEIGHTS: dict[str, float] = {
    "resid_mom": 0.45, "mom_12m": 0.30, "mom_6m": 0.15, "high52_ratio": 0.10,
}


def _compute_composite(df: pd.DataFrame, regime_label: str | None = None) -> pd.DataFrame:
    if df.empty:
        return df

    primary = ["pe_inv", "pb_inv", "fcf_yield", "roe", "margin", "de_inv"]
    valid_mask = df[primary].notna().sum(axis=1) >= _MIN_VALID_METRICS
    df = df[valid_mask].copy()
    if df.empty:
        return df

    # Sector-normalized ranking: removes cross-sector bias (low P/E utilities vs
    # growth tech). Sectors with < _MIN_SECTOR_SIZE members fall back to universe rank.
    score_cols = [
        "pe_inv", "pb_inv", "fcf_yield", "evebitda_inv",          # value
        "roe", "gross_margin", "margin", "de_inv",                  # quality
        "current_ratio", "earnings_growth",                          # quality (cont.)
        "mom_12m", "mom_6m", "high52_ratio", "resid_mom",          # momentum
        "vol_inv", "beta_inv",                                       # low-vol / BAB
        "reversal",                                                  # short-term reversal
    ]
    available_cols = [c for c in score_cols if c in df.columns]
    ranked = _sector_rank(df, available_cols)

    def _ranked(cols: list[str]) -> pd.DataFrame:
        """Select only the rank columns that exist (keeps backward compatibility
        when price-based factors weren't fetched)."""
        present = [c for c in cols if c in ranked.columns]
        return ranked[present] if present else pd.DataFrame(index=ranked.index)

    def _weighted_blend(weights: dict[str, float], fill: float = 0.5) -> pd.Series:
        """Weighted average of rank columns (missing imputed at `fill`), with weights
        renormalised over the columns actually present. Returns a 0-1 Series."""
        present = {c: w for c, w in weights.items() if c in ranked.columns}
        if not present:
            return pd.Series(fill, index=ranked.index)
        total = sum(present.values())
        block = _ranked(list(present)).fillna(fill)
        return sum(block[c] * (w / total) for c, w in present.items())

    # Value: P/E, P/B, FCF yield, EV/EBITDA (4 sub-signals; skipna so missing evebitda ok).
    # All-missing imputes at neutral 16 (0.5×33), matching the other sleeves — a name
    # with no value data is unknown, not worst.
    df["value_score"] = (
        _ranked(["pe_inv", "pb_inv", "fcf_yield", "evebitda_inv"]).mean(axis=1, skipna=True) * 33
    ).fillna(16).clip(0, 33).astype(int)
    # Momentum: 12-1m, 6-1m, 52-week-high ratio, residual (idiosyncratic) momentum.
    # Residual momentum is up-weighted within the sleeve — it was the strongest single
    # sleeve in the PIT backtest (docs/FACTOR_BACKTEST_2026-06-28.md), so it drives the
    # momentum signal more than the raw price-momentum sub-signals. Missing imputed at 0.5.
    df["momentum_score"] = (
        _weighted_blend(_MOMENTUM_WEIGHTS) * 33
    ).clip(0, 33).astype(int)
    # Quality: ROE, gross margin (Novy-Marx), net margin, D/E, current ratio, earnings growth.
    # All-missing imputes at neutral 16, same as value.
    df["quality_score"] = (
        _ranked(["roe", "gross_margin", "margin", "de_inv", "current_ratio", "earnings_growth"])
        .mean(axis=1, skipna=True) * 33
    ).fillna(16).clip(0, 33).astype(int)
    # Low-vol / BAB: inverse realized volatility + inverse beta (2 sub-signals).
    # Missing imputed at neutral 0.5 (don't penalise thin-history names to worst).
    df["low_vol_score"] = (
        _ranked(["vol_inv", "beta_inv"]).fillna(0.5).mean(axis=1) * 33
    ).fillna(16).clip(0, 33).astype(int)
    # Short-term reversal (mean reversion): single oversold signal (-1m return).
    df["reversal_score"] = (
        _ranked(["reversal"]).fillna(0.5).mean(axis=1) * 33
    ).fillna(16).clip(0, 33).astype(int)

    wv, wm, wq, wl, wr = _REGIME_WEIGHTS.get(regime_label or "", _DEFAULT_WEIGHTS)
    # Each component is 0-33; multiply by 3*weight so weights summing to 1 give 0-99
    # regardless of the number of sleeves.
    df["composite_score"] = (
        df["value_score"] * 3 * wv
        + df["momentum_score"] * 3 * wm
        + df["quality_score"] * 3 * wq
        + df["low_vol_score"] * 3 * wl
        + df["reversal_score"] * 3 * wr
    ).round().clip(0, 99).astype(int)
    return df


def _gather_research_with_momentum(
    ticker: str,
    mom1m: float | None,
    mom12m: float | None,
) -> tuple[str, ResearchReport | None]:
    """Call gather_research and override momentum if precomputed values are available."""
    # mom12m is 12-month momentum passed as momentum_12m_override for research display.
    report = gather_research(ticker, momentum_1m_override=mom1m, momentum_12m_override=mom12m)
    return ticker, report


def prefetch_screener_data(tickers: list[str]) -> dict:
    """Fetch all slow data (ticker infos + momentum) for the full universe.

    Returns a dict with keys 'infos', 'momentum', 'timestamp'. Pass this dict
    to run_factor_screen() as the `prefetched` argument to skip re-fetching.
    Designed to run before market open so the morning pipeline is fast.
    """
    log.info("Pre-fetching screener data for %d tickers...", len(tickers))
    infos = _fetch_all_infos(tickers)
    # Download the universe close once and share it across momentum + price factors.
    close: pd.DataFrame | None = None
    try:
        raw = yf.download(tickers, period="12mo", auto_adjust=True, progress=False)
        close = _extract_close(raw, tickers)
    except Exception:
        log.warning("Pre-fetch: universe close download failed", exc_info=True)
        close = None
    spy_close = _fetch_spy_close()
    momentum = _fetch_momentum_batch(tickers, close=close)
    price_factors = _fetch_price_factors_batch(tickers, close=close, spy_close=spy_close)
    result = {
        "infos": infos,
        "momentum": momentum,
        "price_factors": price_factors,
        "timestamp": datetime.now(UTC).isoformat(),
        "ticker_count": len(tickers),
    }
    log.info(
        "Pre-fetch complete: %d infos, %d momentum entries, %d price-factor entries",
        sum(1 for v in infos.values() if v is not None),
        sum(1 for v in momentum.values() if v != (None, None, None, None)),
        sum(1 for v in price_factors.values() if v != (None, None, None)),
    )
    return result


def run_factor_screen(
    tickers: list[str],
    top_n: int = 12,
    research_workers: int = 5,
    regime_label: str | None = None,
    prefetched: dict | None = None,
) -> list[FactorCandidate]:
    """Screen the universe and return the top_n factor candidates.

    Parameters
    ----------
    prefetched : optional dict from prefetch_screener_data(). When provided,
                 the expensive info + momentum fetch is skipped entirely.
    """
    if not tickers:
        return []

    if prefetched is not None:
        infos = prefetched["infos"]
        momentum = prefetched["momentum"]
        price_factors = prefetched.get("price_factors", {})
        log.info(
            "Using pre-fetched screener data from %s (%d tickers)",
            prefetched.get("timestamp", "?"), prefetched.get("ticker_count", 0),
        )
    else:
        infos = _fetch_all_infos(tickers)
        momentum = _fetch_momentum_batch(tickers)
        price_factors = _fetch_price_factors_batch(tickers)

    if momentum and all(v == (None, None, None, None) for v in momentum.values()):
        log.error(
            "Momentum batch fetch returned (None, None, None, None) for all %d tickers — "
            "momentum_score will be zero for every candidate. Check yfinance connectivity.",
            len(tickers),
        )

    df = _build_factor_df(infos, momentum, price_factors)
    if df.empty:
        return []

    scored = _compute_composite(df, regime_label=regime_label)
    if scored.empty:
        return []

    top = scored.nlargest(top_n, "composite_score")

    research_map: dict[str, ResearchReport | None] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=research_workers) as pool:
        futures = {
            str(ticker_idx): pool.submit(
                _gather_research_with_momentum,
                str(ticker_idx),
                momentum.get(str(ticker_idx), (None, None))[0],
                momentum.get(str(ticker_idx), (None, None))[1],
            )
            for ticker_idx in top.index
        }
    for t, fut in futures.items():
        try:
            _, report = fut.result()
            research_map[t] = report
        except Exception as exc:
            log.warning("research failed for %s: %s", t, exc)
            research_map[t] = None

    candidates: list[FactorCandidate] = []
    for ticker_idx, row in top.iterrows():
        t = str(ticker_idx)
        candidates.append(FactorCandidate(
            ticker=t,
            composite_score=int(row["composite_score"]),
            value_score=int(row["value_score"]),
            momentum_score=int(row["momentum_score"]),
            quality_score=int(row["quality_score"]),
            low_vol_score=int(row["low_vol_score"]),
            reversal_score=int(row["reversal_score"]),
            research=research_map.get(t),
        ))
    return candidates
