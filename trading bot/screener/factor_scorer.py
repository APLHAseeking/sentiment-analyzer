from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

from bot.researcher import gather_research, gather_research_batch, ResearchReport

log = logging.getLogger(__name__)

_MIN_VALID_METRICS = 4
_CHUNK_SIZE = 50    # tickers per chunk
_CHUNK_DELAY = 1.0  # seconds between chunks
_MAX_RETRIES = 2


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
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers, period="3mo", auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        else:
            return {t: (None, None) for t in tickers}

        result: dict[str, tuple[float | None, float | None]] = {}
        for t in tickers:
            try:
                col = close[t].dropna() if t in close.columns else pd.Series(dtype=float)
                if len(col) < 2:
                    result[t] = (None, None)
                    continue
                current = float(col.iloc[-1])
                p1m = float(col.iloc[max(0, len(col) - 21)])
                p3m = float(col.iloc[0])
                result[t] = (
                    (current / p1m - 1) * 100 if p1m > 0 else None,
                    (current / p3m - 1) * 100 if p3m > 0 else None,
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
            mom1m, mom3m = momentum.get(ticker, (None, None))

            rows.append({
                "ticker": ticker,
                "pe_inv": -pe if pe and pe > 0 else None,
                "pb_inv": -pb if pb and pb > 0 else None,
                "fcf_yield": fcf_yield,
                "roe": roe,
                "margin": margin,
                "de_inv": -de if de is not None and de >= 0 else None,
                "mom_1m": mom1m,
                "mom_3m": mom3m,
            })
        except Exception:
            log.debug("Skipping %s in factor_df build", ticker, exc_info=True)
            continue

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("ticker")


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

    ranked = df.rank(pct=True, na_option="keep")

    df["value_score"] = (
        ranked[["pe_inv", "pb_inv", "fcf_yield"]].mean(axis=1, skipna=True) * 33
    ).fillna(0).clip(0, 33).astype(int)
    df["momentum_score"] = (
        ranked[["mom_1m", "mom_3m"]].mean(axis=1, skipna=True) * 33
    ).fillna(0).clip(0, 33).astype(int)
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
    mom3m: float | None,
) -> tuple[str, ResearchReport | None]:
    """Call gather_research and override momentum if precomputed values are available."""
    report = gather_research(ticker, momentum_1m_override=mom1m, momentum_3m_override=mom3m)
    return ticker, report


def run_factor_screen(
    tickers: list[str],
    top_n: int = 12,
    research_workers: int = 5,
    regime_label: str | None = None,
) -> list[FactorCandidate]:
    if not tickers:
        return []

    # Bug 2 fix: chunked fetching with retry instead of 30-worker bulk fetch
    infos = _fetch_all_infos(tickers)

    momentum = _fetch_momentum_batch(tickers)

    df = _build_factor_df(infos, momentum)
    if df.empty:
        return []

    scored = _compute_composite(df, regime_label=regime_label)
    if scored.empty:
        return []

    top = scored.nlargest(top_n, "composite_score")

    # Bug 3 fix: pass precomputed momentum to researcher to avoid double download
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
