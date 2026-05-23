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


def _fetch_momentum_batch(
    tickers: list[str],
) -> dict[str, tuple[float | None, float | None]]:
    """Fetch momentum signals. Returns (mom_1m, mom_12m) per ticker.

    mom_12m is 12-month total return — the academically valid momentum signal.
    mom_1m is 1-month return (mean-reverting; kept for research display only,
    NOT used in the composite score). Tickers with fewer than _MIN_MOMENTUM_BARS
    of history are excluded (momentum is unreliable on thin data).
    """
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers, period="12mo", auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        else:
            return {t: (None, None) for t in tickers}

        result: dict[str, tuple[float | None, float | None]] = {}
        for t in tickers:
            try:
                col = close[t].dropna() if t in close.columns else pd.Series(dtype=float)
                if len(col) < _MIN_MOMENTUM_BARS:
                    result[t] = (None, None)
                    continue
                current = float(col.iloc[-1])
                p1m = float(col.iloc[max(0, len(col) - 21)])
                p12m = float(col.iloc[0])  # ~12 months ago
                result[t] = (
                    (current / p1m - 1) * 100 if p1m > 0 else None,   # 1m (display only)
                    (current / p12m - 1) * 100 if p12m > 0 else None,  # 12m (used in score)
                )
            except Exception:
                result[t] = (None, None)
        return result
    except Exception:
        return {t: (None, None) for t in tickers}


def _build_factor_df(
    infos: dict[str, dict | None],
    momentum: dict[str, tuple[float | None, float | None]],
) -> pd.DataFrame:
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
            mom1m, mom12m = momentum.get(ticker, (None, None))
            sector = (info.get("sector") or "Unknown").strip() or "Unknown"

            rows.append({
                "ticker": ticker,
                "sector": sector,
                "pe_inv": -pe if pe and pe > 0 else None,
                "pb_inv": -pb if pb and pb > 0 else None,
                "fcf_yield": fcf_yield,
                "roe": roe,
                "margin": margin,
                "de_inv": -de if de is not None and de >= 0 else None,
                "mom_1m": mom1m,
                "mom_12m": mom12m,
            })
        except Exception:
            log.debug("Skipping %s in factor_df build", ticker, exc_info=True)
            continue

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.set_index("ticker")
    return df


def _sector_rank(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Rank each column within sector for sectors >= _MIN_SECTOR_SIZE members.

    Sectors that are too small (< _MIN_SECTOR_SIZE tickers with data) fall back
    to the universe-wide percentile rank. This prevents a utility company's low
    P/E from outranking a genuinely cheap tech company across sectors.
    """
    # Universe-wide fallback ranks
    universe_ranked = df[cols].rank(pct=True, na_option="keep")

    if "sector" not in df.columns:
        return universe_ranked

    result = universe_ranked.copy()
    sector_counts = df["sector"].value_counts()
    large_sectors = sector_counts[sector_counts >= _MIN_SECTOR_SIZE].index

    for sector in large_sectors:
        mask = df["sector"] == sector
        sector_slice = df.loc[mask, cols]
        result.loc[mask, cols] = sector_slice.rank(pct=True, na_option="keep")

    return result


# Regime-specific factor weights (value, momentum, quality must sum to 1.0).
# In bear/crash: favour value + quality (defensive, mean-reversion).
# In bull/euphoria: favour momentum (trend-following works in rising markets).
_REGIME_WEIGHTS: dict[str, tuple[float, float, float]] = {
    "crash":     (0.45, 0.10, 0.45),
    "deep-bear": (0.45, 0.10, 0.45),
    "bear":      (0.40, 0.15, 0.45),
    "neutral":   (0.33, 0.33, 0.34),
    "bull":      (0.20, 0.50, 0.30),
    "euphoria":  (0.25, 0.45, 0.30),
    "melt-up":   (0.25, 0.50, 0.25),
}
_DEFAULT_WEIGHTS: tuple[float, float, float] = (0.33, 0.33, 0.34)


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
    score_cols = ["pe_inv", "pb_inv", "fcf_yield", "roe", "margin", "de_inv", "mom_12m"]
    available_cols = [c for c in score_cols if c in df.columns]
    ranked = _sector_rank(df, available_cols)

    df["value_score"] = (
        ranked[["pe_inv", "pb_inv", "fcf_yield"]].mean(axis=1, skipna=True) * 33
    ).fillna(0).clip(0, 33).astype(int)
    # Use 12-month momentum only. 1-month is a mean-reversion effect.
    df["momentum_score"] = (
        ranked["mom_12m"].fillna(0) * 33
    ).clip(0, 33).astype(int)
    df["quality_score"] = (
        ranked[["roe", "margin", "de_inv"]].mean(axis=1, skipna=True) * 33
    ).fillna(0).clip(0, 33).astype(int)

    wv, wm, wq = _REGIME_WEIGHTS.get(regime_label or "", _DEFAULT_WEIGHTS)
    # Each component is 0-33; multiply by 3*weight so equal-weight still gives 0-99
    df["composite_score"] = (
        df["value_score"] * 3 * wv
        + df["momentum_score"] * 3 * wm
        + df["quality_score"] * 3 * wq
    ).round().clip(0, 99).astype(int)
    return df


def _gather_research_with_momentum(
    ticker: str,
    mom1m: float | None,
    mom12m: float | None,
) -> tuple[str, ResearchReport | None]:
    """Call gather_research and override momentum if precomputed values are available."""
    # mom12m is 12-month momentum passed as momentum_3m_override for research display.
    report = gather_research(ticker, momentum_1m_override=mom1m, momentum_3m_override=mom12m)
    return ticker, report


def prefetch_screener_data(tickers: list[str]) -> dict:
    """Fetch all slow data (ticker infos + momentum) for the full universe.

    Returns a dict with keys 'infos', 'momentum', 'timestamp'. Pass this dict
    to run_factor_screen() as the `prefetched` argument to skip re-fetching.
    Designed to run before market open so the morning pipeline is fast.
    """
    log.info("Pre-fetching screener data for %d tickers...", len(tickers))
    infos = _fetch_all_infos(tickers)
    momentum = _fetch_momentum_batch(tickers)
    result = {
        "infos": infos,
        "momentum": momentum,
        "timestamp": datetime.now(UTC).isoformat(),
        "ticker_count": len(tickers),
    }
    log.info(
        "Pre-fetch complete: %d infos, %d momentum entries",
        sum(1 for v in infos.values() if v is not None),
        sum(1 for v in momentum.values() if v != (None, None)),
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
        log.info(
            "Using pre-fetched screener data from %s (%d tickers)",
            prefetched.get("timestamp", "?"), prefetched.get("ticker_count", 0),
        )
    else:
        infos = _fetch_all_infos(tickers)
        momentum = _fetch_momentum_batch(tickers)

    if momentum and all(v == (None, None) for v in momentum.values()):
        log.error(
            "Momentum batch fetch returned (None, None) for all %d tickers — "
            "momentum_score will be zero for every candidate. Check yfinance connectivity.",
            len(tickers),
        )

    df = _build_factor_df(infos, momentum)
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
            research=research_map.get(t),
        ))
    return candidates
