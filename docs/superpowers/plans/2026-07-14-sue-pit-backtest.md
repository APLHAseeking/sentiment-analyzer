# SUE PIT Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a point-in-time-correct backtest of the SUE (standardized unexpected earnings) signal already live in `screener/factor_scorer.py`'s momentum sleeve at a 0.15 sub-weight, to decide — via a pre-committed gate, not reverse-engineered from results — whether to recommend raising it to 0.25.

**Architecture:** New companyfacts-based PIT fetcher (`screener/xbrl_pit_sue.py`) reuses the unmodified `sue_from_quarterly_eps` formula from `screener/xbrl_fundamentals.py` but sources EPS values+dates from SEC's per-company `companyfacts` API (which carries `filed` dates) instead of the cross-sectional `frames` API (which doesn't and can silently reflect later amendments — confirmed empirically: AAPL's FY2007 EPS frame value is sourced from a 2010-01-25 10-K/A, not the original 2009-10-27 10-K). A new PIT S&P-500 constituents module (`backtesting/pit_constituents.py`) converts the free `fja05680/sp500` historical-membership CSV into the long-format schema `backtesting/pit_data.py`'s `CSVPITProvider` already expects. A new backtest driver (`backtesting/backtest_sue_pit.py`) builds PIT-dated SUE events, computes d+1-anchored 20d/60d drift via calendar-time portfolios, and gets Newey-West HAC t-stats by reusing `backtesting/attribution.py`'s existing `_hac_standard_errors` (bandwidth = horizon length, no naive i.i.d. test). Regime breakdown reuses the already-fit production `HMMRegimeEngine` (`regime_model.joblib`) in causal/filtered-posterior classify mode over the full historical window — flagged explicitly as a diagnostic-only compromise (the model's own parameters were fit on full-sample data; only the per-date classification within a single `classify()` call is forward-only).

**Tech Stack:** Python 3.11+, pandas/numpy, `requests` (SEC companyfacts), existing `_hac_standard_errors` (Newey-West/Bartlett), existing `HMMRegimeEngine`, pytest (offline, mocked network).

---

## Confirmed spec (do not reopen — from conversation history)

- **PIT date:** tradable date = original (non-`/A`) filing's `filed` date + 1 trading day. Anchor selection mirrors `_completed_quarters` exactly, capped at `_MAX_SUE_STALENESS_QUARTERS = 2`.
- **Amendments:** earliest-filed, non-`/A` form (`10-Q`/`10-K`) only. No fallback to amendments if no original exists — exclude instead.
- **Missing `filed`:** exclude, never impute.
- **SUE formula:** `sue_from_quarterly_eps` reused unmodified — zero redefinition.
- **Gate (pre-committed, per-horizon, not pooled):** at **20d AND independently at 60d**: t-stat > 2 AND IR > 0.5 (gross of costs), PLUS sign-consistent positive drift in first-half vs second-half of sample, PLUS no sign flip in any regime bucket with ≥30 distinct earnings events.
- **SE method:** calendar-time portfolio of daily returns per horizon, Newey-West HAC via `backtesting/attribution.py::_hac_standard_errors` with bandwidth = horizon (20 or 60). Not naive i.i.d.
- **Decision rule:** gate fails → weight stays 0.15 (report as null result). Gate clears → recommend 0.15 → **0.25** (single fixed step, not fit to the measured IR). Recommendation only — this plan does not touch `_MOMENTUM_WEIGHTS`.
- **Universe:** PIT S&P 500 membership from `fja05680/sp500` (verified live: `date,tickers` snapshots 1996-01-02 → 2026-06-30, actively maintained) — not current constituents projected backward.
- **Sample window:** 2012-01-01 (past the 2009–2011 XBRL-adoption ramp) through last signal date ≈ 2026-04-15 (needs ≥60 trading days of runway for the 60d drift to be observable as of today, 2026-07-14).
- **Drift anchor:** `close(d+1+horizon) / close(d+1) − 1`, d+1 = tradable date. Announcement-day jump excluded by construction.

## File structure

- Create: `screener/xbrl_pit_sue.py` — companyfacts fetch/cache + PIT EPS series + PIT SUE computation.
- Create: `backtesting/pit_constituents.py` — fja05680/sp500 fetch/cache/parse into long-format PIT membership.
- Create: `backtesting/backtest_sue_pit.py` — orchestration: events → drift → HAC stats → regime breakdown → report.
- Create: `tests/test_xbrl_pit_sue.py`
- Create: `tests/test_pit_constituents.py`
- Create: `tests/test_backtest_sue_pit_stats.py`
- Create: `docs/SUE_PIT_BACKTEST_2026-07-14.md` — final report (populated with real run output in the last task).
- Modify: `docs/EDGE_BACKLOG.md` — final status update after the backtest completes.

Cache layout (new, gitignored — matches `xbrl_frames_cache`'s existing gitignore treatment):
- `pit_cache/companyfacts/<CIK>.parquet` — raw fact rows per company.
- `pit_cache/sp500_constituents.parquet` — long-format PIT membership.

---

### Task 1: Companyfacts fetch + cache

**Files:**
- Create: `screener/xbrl_pit_sue.py`
- Test: `tests/test_xbrl_pit_sue.py`

- [ ] **Step 1: Write the failing test for the fetch/cache wrapper**

```python
# tests/test_xbrl_pit_sue.py
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from screener.xbrl_pit_sue import fetch_companyfacts_eps

_SAMPLE_PAYLOAD = {
    "facts": {
        "us-gaap": {
            "EarningsPerShareDiluted": {
                "units": {
                    "USD/shares": [
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 1.10,
                         "accn": "0001-1", "fy": 2022, "fp": "Q1", "form": "10-Q",
                         "filed": "2022-05-01"},
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 1.10,
                         "accn": "0001-2", "fy": 2023, "fp": "Q1", "form": "10-Q",
                         "filed": "2023-05-01", "frame": "CY2022Q1"},
                    ]
                }
            }
        }
    }
}


def test_fetch_companyfacts_eps_parses_facts_to_dataframe(tmp_path):
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = _SAMPLE_PAYLOAD
        mock_get.return_value.raise_for_status.return_value = None

        df = fetch_companyfacts_eps(cik=320193, cache_dir=tmp_path)

    assert len(df) == 2
    assert set(df.columns) == {"start", "end", "val", "form", "filed", "accn"}
    assert df.iloc[0]["val"] == 1.10
    mock_get.assert_called_once()
    url = mock_get.call_args[0][0]
    assert url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


def test_fetch_companyfacts_eps_uses_parquet_cache(tmp_path):
    cache_file = tmp_path / "0000320193.parquet"
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = _SAMPLE_PAYLOAD
        mock_get.return_value.raise_for_status.return_value = None
        fetch_companyfacts_eps(cik=320193, cache_dir=tmp_path)
        assert cache_file.exists()

        # Second call must hit the parquet cache, not the network.
        mock_get.reset_mock()
        df2 = fetch_companyfacts_eps(cik=320193, cache_dir=tmp_path)
        mock_get.assert_not_called()
        assert len(df2) == 2


def test_fetch_companyfacts_eps_missing_concept_returns_empty(tmp_path):
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"facts": {"us-gaap": {}}}
        mock_get.return_value.raise_for_status.return_value = None
        df = fetch_companyfacts_eps(cik=1, cache_dir=tmp_path)
    assert df.empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "trading bot" && pytest tests/test_xbrl_pit_sue.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.xbrl_pit_sue'`

- [ ] **Step 3: Write the fetch/cache implementation**

```python
# screener/xbrl_pit_sue.py
"""Point-in-time SUE: SEC companyfacts-sourced EPS with true `filed` dates.

Companion to screener/xbrl_fundamentals.py, which is the LIVE production
fetcher (frames API — universe-wide, but carries no filing date and can
silently reflect later amendments; confirmed empirically for AAPL FY2007:
the frames value traces to a 2010-01-25 10-K/A, not the original
2009-10-27 10-K). This module exists ONLY to backtest the SUE signal with
correct point-in-time dating — it is not used by the live pipeline.

The SUE formula itself is not redefined here: `pit_sue_asof` calls the
unmodified `sue_from_quarterly_eps` from xbrl_fundamentals.py.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from screener.xbrl_fundamentals import _headers, _INTER_REQUEST_SLEEP

log = logging.getLogger(__name__)

_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_EPS_CONCEPT = "EarningsPerShareDiluted"
_EPS_UNIT = "USD/shares"  # companyfacts' unit key differs from frames' "USD-per-shares"


def fetch_companyfacts_eps(cik: int, cache_dir: Path) -> pd.DataFrame:
    """Fetch (or load from parquet cache) every EPS fact SEC has for `cik`.

    Returns columns [start, end, val, form, filed, accn] — one row per
    filed fact instance, newest and oldest, original filings AND
    amendments (callers filter). Empty DataFrame if the concept/company
    has no data.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{cik:010d}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    import time
    url = _COMPANYFACTS_URL.format(cik=cik)
    try:
        resp = requests.get(url, headers=_headers(), timeout=30)
        time.sleep(_INTER_REQUEST_SLEEP)
        if resp.status_code == 404:
            df = pd.DataFrame(columns=["start", "end", "val", "form", "filed", "accn"])
            df.to_parquet(cache_path)
            return df
        resp.raise_for_status()
        payload = resp.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        log.warning("companyfacts fetch failed (CIK %010d): %s", cik, exc)
        return pd.DataFrame(columns=["start", "end", "val", "form", "filed", "accn"])

    facts = (
        payload.get("facts", {})
        .get("us-gaap", {})
        .get(_EPS_CONCEPT, {})
        .get("units", {})
        .get(_EPS_UNIT, [])
    )
    if not facts:
        df = pd.DataFrame(columns=["start", "end", "val", "form", "filed", "accn"])
        df.to_parquet(cache_path)
        return df

    df = pd.DataFrame(facts)[["start", "end", "val", "form", "filed", "accn"]]
    df.to_parquet(cache_path)
    return df
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "trading bot" && pytest tests/test_xbrl_pit_sue.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add "trading bot/screener/xbrl_pit_sue.py" "trading bot/tests/test_xbrl_pit_sue.py"
git commit -m "feat: add SEC companyfacts fetch/cache for PIT SUE backtest"
```

---

### Task 2: Earliest-original-filing quarterly EPS series

**Files:**
- Modify: `screener/xbrl_pit_sue.py`
- Test: `tests/test_xbrl_pit_sue.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_xbrl_pit_sue.py
from screener.xbrl_pit_sue import original_quarterly_eps


def test_original_quarterly_eps_picks_earliest_non_amendment():
    facts = pd.DataFrame([
        # Original 10-Q: single quarter, filed first.
        {"start": "2022-01-01", "end": "2022-03-31", "val": 1.10,
         "form": "10-Q", "filed": "2022-05-01", "accn": "a1"},
        # Same period re-reported as comparative data in next year's 10-Q — later filed.
        {"start": "2022-01-01", "end": "2022-03-31", "val": 1.10,
         "form": "10-Q", "filed": "2023-05-01", "accn": "a2"},
        # 9-month cumulative fact for a DIFFERENT period — must be excluded (not ~1 quarter).
        {"start": "2022-01-01", "end": "2022-09-30", "val": 3.40,
         "form": "10-Q", "filed": "2022-11-01", "accn": "a3"},
    ])
    result = original_quarterly_eps(facts)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["val"] == 1.10
    assert str(row["filed"]) == "2022-05-01"
    assert row["cy_year"] == 2022
    assert row["cy_quarter"] == 1


def test_original_quarterly_eps_excludes_amendments_even_if_earlier():
    facts = pd.DataFrame([
        # A 10-K/A filed BEFORE the (hypothetically late-filed) original — still excluded.
        {"start": "2021-01-01", "end": "2021-03-31", "val": 2.00,
         "form": "10-Q/A", "filed": "2021-05-01", "accn": "b1"},
        {"start": "2021-01-01", "end": "2021-03-31", "val": 1.95,
         "form": "10-Q", "filed": "2021-05-10", "accn": "b2"},
    ])
    result = original_quarterly_eps(facts)
    assert len(result) == 1
    assert result.iloc[0]["val"] == 1.95
    assert str(result.iloc[0]["filed"]) == "2021-05-10"


def test_original_quarterly_eps_no_original_excludes_period():
    facts = pd.DataFrame([
        {"start": "2021-01-01", "end": "2021-03-31", "val": 2.00,
         "form": "10-Q/A", "filed": "2021-05-01", "accn": "c1"},
    ])
    result = original_quarterly_eps(facts)
    assert result.empty


def test_original_quarterly_eps_empty_input():
    result = original_quarterly_eps(pd.DataFrame(columns=["start", "end", "val", "form", "filed", "accn"]))
    assert result.empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "trading bot" && pytest tests/test_xbrl_pit_sue.py -v -k original_quarterly_eps`
Expected: FAIL with `ImportError: cannot import name 'original_quarterly_eps'`

- [ ] **Step 3: Implement**

```python
# append to screener/xbrl_pit_sue.py
_MIN_QUARTER_DAYS = 80
_MAX_QUARTER_DAYS = 100


def original_quarterly_eps(facts: pd.DataFrame) -> pd.DataFrame:
    """Reduce raw companyfacts EPS facts to one row per single fiscal quarter:
    the value+date as ORIGINALLY reported (earliest-filed, non-`/A` form).

    Calendar-quarter label assignment mirrors SEC frames' own "CYyyyyQq"
    convention closely enough for backtest purposes: single-quarter duration
    (80-100 days) facts are bucketed by their `end` month. Not used to
    reproduce the frames VALUE (companyfacts is the sole source of truth
    here) — only to align with `_completed_quarters`'s calendar-quarter walk.

    Returns columns [cy_year, cy_quarter, val, filed], one row per quarter,
    sorted by filed date. Periods with no non-amendment filing are dropped —
    never fall back to an amendment.
    """
    if facts.empty:
        return pd.DataFrame(columns=["cy_year", "cy_quarter", "val", "filed"])

    df = facts.copy()
    df["start"] = pd.to_datetime(df["start"])
    df["end"] = pd.to_datetime(df["end"])
    df["filed"] = pd.to_datetime(df["filed"])
    duration_days = (df["end"] - df["start"]).dt.days
    df = df[(duration_days >= _MIN_QUARTER_DAYS) & (duration_days <= _MAX_QUARTER_DAYS)]
    df = df[~df["form"].str.contains("/A", na=False)]
    if df.empty:
        return pd.DataFrame(columns=["cy_year", "cy_quarter", "val", "filed"])

    df = df.sort_values("filed")
    earliest = df.groupby(["start", "end"], as_index=False).first()

    # Bucket by the calendar quarter-end boundary (3/31, 6/30, 9/30, 12/31)
    # NEAREST to the fact's `end` date — neither end-month nor start-month
    # bucketing survived empirical testing against real SEC frames for
    # non-calendar-fiscal-year filers. Verified against 7 real data points
    # (AAPL, JNJ, WMT x2, HD x2, TGT, MSFT, JPM) spanning both "start
    # crosses into the next calendar quarter" (WMT) and "start sits in the
    # PRIOR calendar quarter" (AAPL/JNJ, fiscal quarter starts late Dec,
    # ends late Mar) shapes — all correctly resolved by nearest-end-boundary.
    #
    # For companies on a 52/53-week RETAIL-style fiscal calendar (e.g.
    # Costco), this per-fact rule is not enough: two DIFFERENT, non-
    # overlapping fiscal quarters can both be nearest to the SAME calendar
    # boundary (verified: Costco's fiscal Q2 ending 2024-02-18 and fiscal Q3
    # ending 2024-05-12 are BOTH nearest to 2024-03-31, 42 days away each —
    # not a per-fact tie, a genuine collision across the company's quarter
    # sequence; SEC's real frame picks the 05-12 one for CY2024Q1, but nothing
    # in the (start,end,val) data alone distinguishes which is "right" without
    # replicating SEC's undocumented internal assignment further). Rather than
    # guess, detect this as a COLLISION — two distinct (start,end) periods for
    # the same company mapping to the same (cy_year, cy_quarter) — and exclude
    # both, consistent with this module's "unknown is not neutral" convention
    # elsewhere. This means some 52/53-week-fiscal-calendar retailers will
    # have sparse or no PIT SUE coverage in the backtest — a documented,
    # honest limitation, not silent corruption. Verified empirically: this
    # does NOT affect any of the 5 validation-checkpoint tickers (AAPL,
    # MSFT, JPM, WMT, JNJ) at their CY2025Q1 quarter — zero collisions there.
    quarter_ends = pd.DatetimeIndex(sorted({
        pd.Timestamp(year=y, month=m, day=d)
        for y in range(earliest["end"].dt.year.min() - 1, earliest["end"].dt.year.max() + 2)
        for m, d in [(3, 31), (6, 30), (9, 30), (12, 31)]
    }))

    def _nearest_quarter(end: pd.Timestamp) -> tuple[int, int]:
        deltas = np.abs((quarter_ends - end).days.to_numpy())
        qe = quarter_ends[np.argmin(deltas)]
        return (qe.year, (qe.month - 1) // 3 + 1)

    earliest["cy_year"], earliest["cy_quarter"] = zip(*earliest["end"].apply(_nearest_quarter))
    label_counts = earliest.groupby(["cy_year", "cy_quarter"])["start"].transform("count")
    earliest = earliest[label_counts == 1]

    result = earliest[["cy_year", "cy_quarter", "val", "filed"]].sort_values("filed")
    return result.reset_index(drop=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "trading bot" && pytest tests/test_xbrl_pit_sue.py -v -k original_quarterly_eps`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add "trading bot/screener/xbrl_pit_sue.py" "trading bot/tests/test_xbrl_pit_sue.py"
git commit -m "feat: earliest-original-filing quarterly EPS reduction for PIT SUE"
```

---

### Task 3: PIT-dated SUE at an as-of date (mirrors production anchor logic exactly)

**Files:**
- Modify: `screener/xbrl_pit_sue.py`
- Test: `tests/test_xbrl_pit_sue.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_xbrl_pit_sue.py
from datetime import date
from screener.xbrl_pit_sue import pit_eps_asof, pit_sue_asof


def _quarterly(rows):
    return pd.DataFrame(rows, columns=["cy_year", "cy_quarter", "val", "filed"])


def test_pit_eps_asof_excludes_not_yet_filed_quarters():
    quarterly = _quarterly([
        (2023, 4, 1.50, "2024-02-01"),
        (2023, 3, 1.40, "2023-11-01"),
        (2023, 2, 1.30, "2023-08-01"),
        (2023, 1, 1.20, "2023-05-01"),
        (2022, 4, 1.45, "2023-02-01"),
    ])
    # as_of is BEFORE the 2023Q4 filing date -> that quarter must not appear.
    series = pit_eps_asof(quarterly, as_of=date(2024, 1, 15), n_quarters=6)
    assert series[0] is None  # 2023Q4 (today's/newest calendar quarter) not yet filed
    # 2023Q3 should be the first populated slot.
    assert series[1] == 1.40


def test_pit_sue_asof_delegates_to_unmodified_formula():
    quarterly = _quarterly([
        (2023, 4, 1.50, "2024-02-01"),
        (2023, 3, 1.40, "2023-11-01"),
        (2023, 2, 1.30, "2023-08-01"),
        (2023, 1, 1.20, "2023-05-01"),
        (2022, 4, 1.30, "2023-02-01"),
        (2022, 3, 1.25, "2022-11-01"),
        (2022, 2, 1.15, "2022-08-01"),
        (2022, 1, 1.05, "2022-05-01"),
        (2021, 4, 1.20, "2022-02-01"),
    ])
    result = pit_sue_asof(quarterly, as_of=date(2024, 2, 5))
    # Anchor = 2023Q4 (1.50), t-4 = 2022Q4 (1.30) -> latest_change = 0.20
    from screener.xbrl_fundamentals import sue_from_quarterly_eps
    expected = sue_from_quarterly_eps(
        pit_eps_asof(quarterly, as_of=date(2024, 2, 5), n_quarters=14)
    )
    assert result == expected
    assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "trading bot" && pytest tests/test_xbrl_pit_sue.py -v -k "pit_eps_asof or pit_sue_asof"`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement**

```python
# append to screener/xbrl_pit_sue.py
from screener.xbrl_fundamentals import _completed_quarters, sue_from_quarterly_eps


def pit_eps_asof(quarterly: pd.DataFrame, as_of: date, n_quarters: int) -> list[float | None]:
    """Build the `eps_newest_first` list `sue_from_quarterly_eps` expects, as
    it would have looked to someone standing on `as_of` — mirrors
    `_completed_quarters(as_of, n_quarters)`'s calendar-quarter walk exactly,
    but a quarter's value is only visible if its true `filed` date <= as_of
    (not merely calendar-completed, which is what `_completed_quarters`
    alone assumes for the LIVE frames-sourced path).

    NOTE: `quarterly["filed"]` is a raw ISO date STRING (e.g. "2022-05-01"),
    not a Timestamp — `original_quarterly_eps` (Task 2) deliberately keeps
    the original string rather than a stringified Timestamp so its own
    output is directly comparable/printable. Parse explicitly here with
    `date.fromisoformat`; do not call `.date()` on it directly (str has no
    such method) and do not assume `.dt` accessors work on this column.
    """
    quarters = _completed_quarters(as_of, n_quarters)
    lookup = {
        (int(r.cy_year), int(r.cy_quarter)): r
        for r in quarterly.itertuples()
        if date.fromisoformat(str(r.filed)) <= as_of
    }
    return [
        float(lookup[(y, q)].val) if (y, q) in lookup else None
        for y, q in quarters
    ]


def pit_sue_asof(quarterly: pd.DataFrame, as_of: date) -> float | None:
    """PIT-correct SUE as of `as_of`, using the unmodified production formula."""
    from screener.xbrl_fundamentals import _EPS_QUARTERS
    series = pit_eps_asof(quarterly, as_of, n_quarters=_EPS_QUARTERS)
    return sue_from_quarterly_eps(series)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "trading bot" && pytest tests/test_xbrl_pit_sue.py -v`
Expected: PASS (all tests in file, ~11)

- [ ] **Step 5: Commit**

```bash
git add "trading bot/screener/xbrl_pit_sue.py" "trading bot/tests/test_xbrl_pit_sue.py"
git commit -m "feat: PIT-dated SUE computation reusing unmodified production formula"
```

---

### Task 4: PIT S&P 500 constituents

**Files:**
- Create: `backtesting/pit_constituents.py`
- Test: `tests/test_pit_constituents.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pit_constituents.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backtesting.pit_constituents import fetch_sp500_pit_constituents

_SAMPLE_CSV = (
    "date,tickers\n"
    '2012-01-03,"AAPL,MSFT,XOM"\n'
    '2012-02-01,"AAPL,MSFT,XOM,GOOGL"\n'
)


def test_fetch_sp500_pit_constituents_melts_wide_to_long(tmp_path):
    with patch("backtesting.pit_constituents._download_raw_csv", return_value=_SAMPLE_CSV):
        df = fetch_sp500_pit_constituents(cache_path=tmp_path / "constituents.parquet")

    assert list(df.columns) == ["date", "ticker"]
    assert len(df) == 7  # 3 + 4
    row = df[(df["ticker"] == "GOOGL")]
    assert str(row.iloc[0]["date"]) == "2012-02-01"
    assert "GOOGL" not in set(df[df["date"] == pd.Timestamp("2012-01-03").date()]["ticker"])


def test_fetch_sp500_pit_constituents_uses_cache(tmp_path):
    cache_path = tmp_path / "constituents.parquet"
    with patch("backtesting.pit_constituents._download_raw_csv", return_value=_SAMPLE_CSV) as mock_dl:
        fetch_sp500_pit_constituents(cache_path=cache_path)
        mock_dl.assert_called_once()
        mock_dl.reset_mock()
        fetch_sp500_pit_constituents(cache_path=cache_path)
        mock_dl.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "trading bot" && pytest tests/test_pit_constituents.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# backtesting/pit_constituents.py
"""PIT S&P 500 membership from the free, actively-maintained fja05680/sp500
historical-components dataset (verified live 2026-07-14: date,tickers
snapshots 1996-01-02 through 2026-06-30). Converts to the long-format
schema backtesting/pit_data.py's CSVPITProvider expects (date, ticker).

This is the constituents piece of the Phase 0 PIT-data blocker
(docs/PHASE0_FINDINGS.md) — used here for the SUE PIT backtest only.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

_RAW_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes(Updated).csv"
)


def _download_raw_csv() -> str:
    resp = requests.get(_RAW_URL, timeout=60)
    resp.raise_for_status()
    return resp.text


def fetch_sp500_pit_constituents(cache_path: Path) -> pd.DataFrame:
    """Return long-format (date, ticker) PIT membership, cached to parquet."""
    cache_path = Path(cache_path)
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    raw = _download_raw_csv()
    wide = pd.read_csv(io.StringIO(raw), parse_dates=["date"])
    rows = []
    for _, row in wide.iterrows():
        d = row["date"].date()
        for ticker in str(row["tickers"]).split(","):
            ticker = ticker.strip().upper()
            if ticker:
                rows.append((d, ticker))
    long_df = pd.DataFrame(rows, columns=["date", "ticker"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    long_df.to_parquet(cache_path)
    log.info("PIT S&P 500 constituents: %d (date,ticker) rows cached to %s", len(long_df), cache_path)
    return long_df
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "trading bot" && pytest tests/test_pit_constituents.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add "trading bot/backtesting/pit_constituents.py" "trading bot/tests/test_pit_constituents.py"
git commit -m "feat: PIT S&P 500 constituent membership from fja05680/sp500"
```

---

### Task 5: Newey-West HAC mean/t-stat helper (reuses existing Bartlett-kernel code)

**Files:**
- Create: `backtesting/backtest_sue_pit.py` (stats helpers first; orchestration in Task 7)
- Test: `tests/test_backtest_sue_pit_stats.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_sue_pit_stats.py
from __future__ import annotations

import numpy as np
import pandas as pd

from backtesting.backtest_sue_pit import hac_mean_tstat


def test_hac_mean_tstat_matches_sign_and_rough_magnitude():
    rng = np.random.default_rng(42)
    returns = pd.Series(rng.normal(loc=0.001, scale=0.01, size=500))
    mean, tstat = hac_mean_tstat(returns, bandwidth=20)
    assert mean == pytest_approx_close(returns.mean())
    assert tstat > 0  # positive mean -> positive t


def pytest_approx_close(x, tol=1e-9):
    class _Approx:
        def __eq__(self, other):
            return abs(other - x) < tol
    return _Approx()


def test_hac_tstat_smaller_than_naive_iid_under_induced_autocorrelation():
    """Overlapping-window returns are serially correlated by construction —
    HAC SE must be larger (t-stat smaller in magnitude) than a naive i.i.d.
    SE computed on the same series, or the reused Newey-West wiring is broken.
    """
    rng = np.random.default_rng(7)
    shocks = rng.normal(0, 0.01, size=520)
    # 20-day rolling sum induces strong positive serial correlation, like an
    # overlapping-holding-period calendar-time portfolio.
    overlapping = pd.Series(shocks).rolling(20).sum().dropna().reset_index(drop=True)

    _, hac_t = hac_mean_tstat(overlapping, bandwidth=20)
    naive_se = overlapping.std(ddof=1) / np.sqrt(len(overlapping))
    naive_t = overlapping.mean() / naive_se

    assert abs(hac_t) < abs(naive_t)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "trading bot" && pytest tests/test_backtest_sue_pit_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backtesting.backtest_sue_pit'`

- [ ] **Step 3: Implement**

```python
# backtesting/backtest_sue_pit.py
"""SUE PIT backtest driver — see docs/superpowers/plans/2026-07-14-sue-pit-backtest.md
and docs/EDGE_BACKLOG.md for the confirmed spec (PIT semantics, pre-committed
gate). Recommendation-only: never writes to screener/factor_scorer.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtesting.attribution import _hac_standard_errors


def hac_mean_tstat(returns: pd.Series, bandwidth: int) -> tuple[float, float]:
    """Mean and Newey-West HAC t-stat of a return series, reusing the exact
    Bartlett-kernel estimator already in backtesting/attribution.py (mean-only
    regression: X = a column of ones).
    """
    r = returns.dropna().to_numpy()
    n = len(r)
    X = np.ones((n, 1))
    mean = float(r.mean())
    resid = r - mean
    XtX_inv = np.array([[1.0 / n]])
    se = _hac_standard_errors(X, resid, XtX_inv, bandwidth)[0]
    tstat = mean / se if se > 0 else 0.0
    return mean, tstat
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "trading bot" && pytest tests/test_backtest_sue_pit_stats.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add "trading bot/backtesting/backtest_sue_pit.py" "trading bot/tests/test_backtest_sue_pit_stats.py"
git commit -m "feat: HAC mean/t-stat helper for SUE PIT drift, reusing existing Newey-West code"
```

---

### Task 6: Validation checkpoint — sanity-check PIT EPS against production frames

**Files:**
- Create: `backtesting/validate_pit_sue.py` (throwaway-but-committed diagnostic script, not a test)

**Why this task exists:** Task 2's calendar-quarter assignment (duration 80-100 days, bucket by `end` month) approximates SEC frames' own period-alignment logic rather than replicating it exactly. Before spending the SEC request budget on ~500 tickers × 14 years, spot-check that the approximation agrees with production's frames-sourced values for a handful of large, liquid, unlikely-to-be-restated recent quarters.

- [ ] **Step 1: Write the validation script**

```python
# backtesting/validate_pit_sue.py
"""One-off validation: compare PIT-reconstructed quarterly EPS (companyfacts,
earliest-original-filing) against the live frames-sourced value, for a small
sample of large-cap tickers and recent (unlikely-restated) quarters. Run
manually before the full backtest; not part of the pytest suite (hits the
network).
"""
from __future__ import annotations

from pathlib import Path

from screener.xbrl_fundamentals import _fetch_ticker_cik_map, _fetch_frame
from screener.xbrl_pit_sue import fetch_companyfacts_eps, original_quarterly_eps

_SAMPLE_TICKERS = ["AAPL", "MSFT", "JPM", "XOM", "JNJ"]
_CACHE_DIR = Path("pit_cache/companyfacts")


def main() -> None:
    cik_map = _fetch_ticker_cik_map(cache=None)
    frame = _fetch_frame("EarningsPerShareDiluted", "USD-per-shares", "CY2025Q1", cache=None)

    for ticker in _SAMPLE_TICKERS:
        cik = cik_map.get(ticker)
        if cik is None:
            print(f"{ticker}: no CIK found")
            continue
        facts = fetch_companyfacts_eps(cik, _CACHE_DIR)
        quarterly = original_quarterly_eps(facts)
        pit_row = quarterly[(quarterly["cy_year"] == 2025) & (quarterly["cy_quarter"] == 1)]
        pit_val = float(pit_row.iloc[0]["val"]) if not pit_row.empty else None
        frame_val = frame.get(cik)
        match = "OK" if pit_val is not None and frame_val is not None and abs(pit_val - frame_val) < 0.01 else "MISMATCH"
        print(f"{ticker}: PIT={pit_val}  frame={frame_val}  {match}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it and record the output**

Run: `cd "trading bot" && python -m backtesting.validate_pit_sue`
Expected: 5 lines, all `OK` (values within 1 cent). Any `MISMATCH` — stop and investigate the period-alignment logic in `original_quarterly_eps` before proceeding to Task 7; do not silently widen the tolerance.

- [ ] **Step 3: Commit (with the recorded output pasted into the commit body)**

```bash
git add "trading bot/backtesting/validate_pit_sue.py"
git commit -m "$(cat <<'EOF'
chore: validate PIT quarterly EPS alignment against production frames

Spot-check on 5 large caps, CY2025Q1 (recent, unlikely restated):
<paste the 5 OK/MISMATCH lines from the actual run here>
EOF
)"
```

---

### Task 7: Drift events + calendar-time portfolio + regime breakdown + report

**Files:**
- Modify: `backtesting/backtest_sue_pit.py`
- Create: `docs/SUE_PIT_BACKTEST_2026-07-14.md`
- Modify: `docs/EDGE_BACKLOG.md`

This is the orchestration task — it is long by nature (it is the actual backtest), so it is split into sub-steps with their own checks rather than one TDD red/green cycle. Each sub-step is runnable and inspectable on its own before moving to the next.

- [ ] **Step 1: Build the S&P-500-eligible-ticker × CIK map, restricted to the PIT universe ever seen 2012-2026**

```python
# append to backtesting/backtest_sue_pit.py
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from backtesting.pit_constituents import fetch_sp500_pit_constituents
from screener.xbrl_fundamentals import _fetch_ticker_cik_map
from screener.xbrl_pit_sue import fetch_companyfacts_eps, original_quarterly_eps, pit_sue_asof

SAMPLE_START = date(2012, 1, 1)
SAMPLE_END = date(2026, 4, 15)
_CACHE_DIR = Path("pit_cache/companyfacts")
_CONSTITUENTS_CACHE = Path("pit_cache/sp500_constituents.parquet")


def universe_tickers() -> set[str]:
    """Every ticker that was ever an S&P 500 PIT member during the sample window."""
    constituents = fetch_sp500_pit_constituents(_CONSTITUENTS_CACHE)
    mask = (constituents["date"] >= SAMPLE_START) & (constituents["date"] <= SAMPLE_END)
    return set(constituents.loc[mask, "ticker"])
```

Run: `cd "trading bot" && python -c "from backtesting.backtest_sue_pit import universe_tickers; u = universe_tickers(); print(len(u)); print(sorted(u)[:10])"`
Expected: a count in the 700-900 range (S&P 500 turnover over 14 years) and a sample of real tickers.

- [ ] **Step 2: Build PIT SUE events for the whole universe (long-running — expect ~10-15 min at the existing 0.12s inter-request sleep for ~800 companyfacts calls)**

```python
# append to backtesting/backtest_sue_pit.py
def build_pit_sue_events(tickers: set[str]) -> pd.DataFrame:
    """One row per (ticker, quarter) with a PIT SUE value: ticker, tradable_date
    (filed + 1 trading day), sue. `tradable_date` is the actual signal date —
    every downstream drift/regime computation anchors here.
    """
    # quarterly["filed"] is a raw ISO date string (see pit_eps_asof's note in
    # Task 3) — parse explicitly with date.fromisoformat, not .dt/.date().
    cik_map = _fetch_ticker_cik_map(cache=None)
    rows = []
    for ticker in sorted(tickers):
        cik = cik_map.get(ticker)
        if cik is None:
            continue
        facts = fetch_companyfacts_eps(cik, _CACHE_DIR)
        quarterly = original_quarterly_eps(facts)
        quarterly = quarterly[
            quarterly["filed"].apply(lambda f: SAMPLE_START <= date.fromisoformat(str(f)) <= SAMPLE_END)
        ]
        for _, q_row in quarterly.iterrows():
            as_of = date.fromisoformat(str(q_row["filed"]))
            sue = pit_sue_asof(quarterly, as_of)
            if sue is not None:
                rows.append({"ticker": ticker, "filed_date": as_of, "sue": sue})
    return pd.DataFrame(rows, columns=["ticker", "filed_date", "sue"])
```

Run (writes a checkpoint parquet so the next steps don't refetch): 
```bash
cd "trading bot" && python -c "
from backtesting.backtest_sue_pit import universe_tickers, build_pit_sue_events
events = build_pit_sue_events(universe_tickers())
events.to_parquet('pit_cache/sue_events_raw.parquet')
print(len(events), 'events')
print(events.head())
"
```
Expected: several thousand events (roughly 4 quarters/yr × ~700-900 tickers over ~14 years, reduced by staleness/data gaps — sanity floor: at least 2000).

- [ ] **Step 3: Add trading-day calendar helper and compute tradable_date = filed_date + 1 trading day**

```python
# append to backtesting/backtest_sue_pit.py
import exchange_calendars as xcals


def add_tradable_date(events: pd.DataFrame) -> pd.DataFrame:
    nyse = xcals.get_calendar("XNYS")
    sessions = nyse.sessions_in_range(str(SAMPLE_START), str(SAMPLE_END + timedelta(days=10)))
    sessions = pd.DatetimeIndex(sessions).normalize()

    def _next_session(d: date) -> date | None:
        after = sessions[sessions > pd.Timestamp(d)]
        return after[0].date() if len(after) else None

    events = events.copy()
    events["tradable_date"] = events["filed_date"].apply(_next_session)
    return events.dropna(subset=["tradable_date"])
```

Run:
```bash
cd "trading bot" && python -c "
import pandas as pd
from backtesting.backtest_sue_pit import add_tradable_date
events = pd.read_parquet('pit_cache/sue_events_raw.parquet')
events = add_tradable_date(events)
events.to_parquet('pit_cache/sue_events_dated.parquet')
print(events[['filed_date','tradable_date']].head())
"
```
Expected: `tradable_date` is always the first NYSE session strictly after `filed_date` (e.g. a Friday filing → following Monday, weekends/holidays skipped).

- [ ] **Step 4: Fetch prices, restrict to PIT-eligible (ticker, tradable_date) pairs, compute per-event drift**

```python
# append to backtesting/backtest_sue_pit.py
import yfinance as yf

HORIZONS = (20, 60)


def restrict_to_pit_universe(events: pd.DataFrame) -> pd.DataFrame:
    """Drop events where the ticker was not an S&P 500 PIT member on tradable_date."""
    constituents = fetch_sp500_pit_constituents(_CONSTITUENTS_CACHE)
    member_dates = constituents.groupby("ticker")["date"].apply(set).to_dict()
    kept = []
    for _, row in events.iterrows():
        dates = member_dates.get(row["ticker"])
        if dates and any(abs((row["tradable_date"] - d).days) <= 45 for d in dates):
            kept.append(row)
    return pd.DataFrame(kept)


def fetch_prices(tickers: list[str]) -> pd.DataFrame:
    raw = yf.download(tickers, start=str(SAMPLE_START), end=str(SAMPLE_END + timedelta(days=90)),
                       auto_adjust=True, progress=False)
    return raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]].rename(
        columns={"Close": tickers[0]}
    )


def compute_drift(events: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    out = events.copy()
    for h in HORIZONS:
        col = []
        for _, row in events.iterrows():
            ticker = row["ticker"]
            if ticker not in prices.columns:
                col.append(None)
                continue
            series = prices[ticker].dropna()
            entry = series[series.index >= pd.Timestamp(row["tradable_date"])]
            if entry.empty:
                col.append(None)
                continue
            entry_idx = series.index.get_loc(entry.index[0])
            exit_idx = entry_idx + h
            if exit_idx >= len(series):
                col.append(None)
                continue
            col.append(series.iloc[exit_idx] / series.iloc[entry_idx] - 1.0)
        out[f"drift_{h}d"] = col
    return out.dropna(subset=[f"drift_{h}d" for h in HORIZONS], how="all")
```

Run:
```bash
cd "trading bot" && python -c "
import pandas as pd
from backtesting.backtest_sue_pit import restrict_to_pit_universe, fetch_prices, compute_drift
events = pd.read_parquet('pit_cache/sue_events_dated.parquet')
events = restrict_to_pit_universe(events)
prices = fetch_prices(sorted(events['ticker'].unique()))
prices.to_parquet('pit_cache/prices.parquet')
events = compute_drift(events, prices)
events.to_parquet('pit_cache/sue_events_final.parquet')
print(len(events), 'events with drift')
print(events[['ticker','tradable_date','sue','drift_20d','drift_60d']].head())
"
```
Expected: event count somewhat below Step 2's (drops: no-longer-PIT-member, price gaps, insufficient runway near the sample end), still well above the ≥30-per-regime-bucket floor needed for the gate.

- [ ] **Step 5: Quintile-bucket, benchmark-adjust, and run the HAC gate per horizon**

```python
# append to backtesting/backtest_sue_pit.py
def top_quintile_excess_returns(events: pd.DataFrame, horizon: int) -> pd.Series:
    """Per calendar tradable_date, excess return of top-quintile-SUE events
    over the equal-weighted mean of ALL PIT-eligible events dated that day —
    a calendar-time long-only portfolio return series suitable for HAC."""
    col = f"drift_{horizon}d"
    valid = events.dropna(subset=[col])
    by_date_mean = valid.groupby("tradable_date")[col].transform("mean")
    valid = valid.assign(excess=valid[col] - by_date_mean)
    threshold = valid["sue"].quantile(0.8)
    top = valid[valid["sue"] >= threshold]
    daily = top.groupby("tradable_date")["excess"].mean()
    return daily.sort_index()


def run_gate(events: pd.DataFrame) -> dict:
    results = {}
    for h in HORIZONS:
        daily = top_quintile_excess_returns(events, h)
        mean, tstat = hac_mean_tstat(daily, bandwidth=h)
        annualization = (252 / h) ** 0.5
        ir = (mean / daily.std(ddof=1)) * annualization if daily.std(ddof=1) > 0 else 0.0
        results[h] = {"mean": mean, "tstat": tstat, "ir": ir, "n_dates": len(daily)}
    return results
```

Run:
```bash
cd "trading bot" && python -c "
import pandas as pd
from backtesting.backtest_sue_pit import run_gate
events = pd.read_parquet('pit_cache/sue_events_final.parquet')
results = run_gate(events)
for h, r in results.items():
    print(h, r)
"
```
Expected: real printed numbers — record them verbatim, do not round favorably.

- [ ] **Step 6: First-half/second-half stability + regime breakdown**

```python
# append to backtesting/backtest_sue_pit.py
from system.config import settings
from regime.hmm_engine import HMMRegimeEngine
from features.feature_pipeline import FeatureConfig
from market_data.market_feed import get_regime_data


def stability_split(events: pd.DataFrame) -> dict:
    mid = events["tradable_date"].median()
    first_half = events[events["tradable_date"] <= mid]
    second_half = events[events["tradable_date"] > mid]
    return {"first_half": run_gate(first_half), "second_half": run_gate(second_half)}


def regime_breakdown(events: pd.DataFrame) -> pd.DataFrame:
    """Diagnostic only — NOT used to gate the drift t-stats themselves, which
    are already PIT-correct regardless of how dates get grouped here. Uses
    the already-fit production regime model (regime_model.joblib) in
    forward-only/filtered-posterior classify mode; the model's own
    parameters were fit on the full historical sample, so this is a
    descriptive stratification, not a walk-forward reconstruction."""
    market_data = get_regime_data(years=15)
    engine = HMMRegimeEngine(settings.regime)
    engine.load(settings.regime.model_path)
    states = engine.classify(market_data, FeatureConfig(), update_recent_labels=True)
    regime_by_date = {s.date: s.regime_label for s in states}

    events = events.copy()
    events["regime"] = events["tradable_date"].apply(lambda d: regime_by_date.get(str(d)))
    counts = events.groupby("regime").size().rename("n_events")
    mean_drift = events.groupby("regime")[["drift_20d", "drift_60d"]].mean()
    return pd.concat([counts, mean_drift], axis=1)
```

Run:
```bash
cd "trading bot" && python -c "
import pandas as pd
from backtesting.backtest_sue_pit import stability_split, regime_breakdown
events = pd.read_parquet('pit_cache/sue_events_final.parquet')
print(stability_split(events))
print(regime_breakdown(events))
"
```
Expected: real printed numbers for both. Note any regime bucket with <30 events (per the confirmed rule, those don't veto the gate even on a sign flip) versus ≥30 (a sign flip there does veto).

- [ ] **Step 7: PIT-vs-status-quo comparison (the honesty check)**

```python
# append to backtesting/backtest_sue_pit.py
def naive_frames_comparison(events: pd.DataFrame) -> dict:
    """Re-run the SAME events but with tradable_date = filed_date (T+0, the
    naive non-PIT anchor) instead of filed_date+1-trading-day, to quantify
    how much of any measured drift depends on the PIT lag. This is a
    diagnostic re-anchoring, not a second data source — if PIT (d+1) reads
    WEAKER than this naive (d+0) version, that's the expected/correct
    direction. If PIT reads STRONGER, stop and investigate before writing
    the report — that would mean look-ahead leaked back in somewhere.
    """
    naive = events.copy()
    naive["tradable_date"] = naive["filed_date"]
    return run_gate(naive)
```

Run:
```bash
cd "trading bot" && python -c "
import pandas as pd
from backtesting.backtest_sue_pit import naive_frames_comparison, run_gate
events = pd.read_parquet('pit_cache/sue_events_final.parquet')
print('PIT (d+1):', run_gate(events))
print('naive (d+0):', naive_frames_comparison(events))
"
```
Expected: PIT numbers weaker (lower |t|, lower IR) than naive. If PIT is stronger, STOP — do not write the report — dig into why (per the user's explicit instruction) before proceeding to Step 8.

- [ ] **Step 8: Write the report and apply the pre-committed decision rule**

Populate `docs/SUE_PIT_BACKTEST_2026-07-14.md` with the actual Step 5-7 output (real numbers, not placeholders), structured as: PIT semantics recap, gate recap, results table (mean/t/IR per horizon), stability split, regime breakdown table, PIT-vs-naive comparison, and a **Recommendation** section that mechanically applies the pre-committed rule from this plan's header — 0.15 stays if any per-horizon or stability/regime condition fails, 0.15→0.25 only if all conditions in the confirmed gate pass. No new thresholds invented at this step.

- [ ] **Step 9: Update EDGE_BACKLOG.md's SUE section from IN PROGRESS to the actual outcome, and commit everything**

```bash
git add "trading bot/backtesting/backtest_sue_pit.py" "trading bot/docs/SUE_PIT_BACKTEST_2026-07-14.md" "trading bot/docs/EDGE_BACKLOG.md"
git commit -m "$(cat <<'EOF'
research: SUE PIT backtest complete — <PASS/FAIL the gate, one line>

<one-line headline number, e.g. "20d t=X.XX IR=X.XX, 60d t=X.XX IR=X.XX">
Recommendation: <keep 0.15 | raise to 0.25> per the pre-committed gate.
Weight NOT changed in this commit — separate reviewed step per user instruction.
EOF
)"
```

---

## Self-review notes

- **Spec coverage:** PIT date semantics (Task 1-3), decision rule (Task 7 Step 8), PIT universe (Task 4), XBRL-era window (constant in Task 7 Step 1: `SAMPLE_START = 2012-01-01`), per-horizon independent gate + HAC not naive-iid (Task 5, Task 7 Step 5), regime buckets by event count (Task 7 Step 6), IR gross-of-cost (labeled explicitly in Task 7 Step 8), drift anchored at d+1 excluding the jump (Task 7 Step 3-4), amendment/missing-filed handling (Task 2), zero new LLM calls (no LLM import anywhere in this plan), parquet caching (Tasks 1, 4, and the `pit_cache/` checkpoints in Task 7), reuse-not-redefine of the SUE formula (Task 1/3 import from `xbrl_fundamentals.py` rather than reimplementing), live weight untouched (Task 7 Step 9's commit message states this explicitly; no edit to `screener/factor_scorer.py` anywhere in this plan).
- **Placeholder scan:** no TBD/TODO; Task 7's steps 1-7 print real numbers at run time rather than asserting expected values, which is correct for a research backtest (unlike Tasks 1-5, which are pure functions with deterministic expected test output).
- **Type consistency:** `pit_sue_asof`/`pit_eps_asof`/`original_quarterly_eps` signatures match between their Task 2/3 definitions and their Task 6/7 call sites; `hac_mean_tstat(returns, bandwidth) -> (mean, tstat)` used identically in Task 5's test and Task 7 Step 5's `run_gate`.
