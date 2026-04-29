from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

from bot.researcher import gather_research, ResearchReport

log = logging.getLogger(__name__)

_MIN_VALID_METRICS = 4


@dataclass(frozen=True)
class FactorCandidate:
    ticker: str
    composite_score: int
    value_score: int
    momentum_score: int
    quality_score: int
    research: ResearchReport | None


def _fetch_info(ticker: str) -> tuple[str, dict | None]:
    try:
        return ticker, yf.Ticker(ticker).info
    except Exception:
        return ticker, None


def _fetch_momentum_batch(
    tickers: list[str],
) -> dict[str, tuple[float | None, float | None]]:
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers, period="3mo", auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        elif "Close" in raw.columns:
            close = raw[["Close"]].rename(columns={"Close": tickers[0]})
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
            def _f(v: object) -> float | None:
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            pe = _f(info.get("trailingPE"))
            pb = _f(info.get("priceToBook"))
            fcf = _f(info.get("freeCashflow"))
            mcap = _f(info.get("marketCap"))
            roe = _f(info.get("returnOnEquity"))
            margin = _f(info.get("profitMargins"))
            de = _f(info.get("debtToEquity"))
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
            continue

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("ticker")


def _compute_composite(df: pd.DataFrame) -> pd.DataFrame:
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
    df["composite_score"] = (
        df["value_score"] + df["momentum_score"] + df["quality_score"]
    ).clip(0, 99)
    return df


def run_factor_screen(tickers: list[str], top_n: int = 12) -> list[FactorCandidate]:
    if not tickers:
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
        results = list(pool.map(_fetch_info, tickers))
    infos = dict(results)

    momentum = _fetch_momentum_batch(tickers)

    df = _build_factor_df(infos, momentum)
    if df.empty:
        return []

    scored = _compute_composite(df)
    if scored.empty:
        return []

    top = scored.nlargest(top_n, "composite_score")

    candidates: list[FactorCandidate] = []
    for ticker_idx, row in top.iterrows():
        t = str(ticker_idx)
        research = gather_research(t)
        candidates.append(FactorCandidate(
            ticker=t,
            composite_score=int(row["composite_score"]),
            value_score=int(row["value_score"]),
            momentum_score=int(row["momentum_score"]),
            quality_score=int(row["quality_score"]),
            research=research,
        ))
    return candidates
