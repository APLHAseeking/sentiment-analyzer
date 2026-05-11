# Parallel Research Gathering — Design Spec

**Date:** 2026-05-11
**Status:** Approved

## Goal

Replace sequential `gather_research()` loops in the factor screener and exit review with a parallel batch fetch, cutting wall-clock time roughly proportional to the number of candidates. Concurrency is capped to avoid yfinance and Claude Haiku rate limits.

---

## Architecture

A new `gather_research_batch()` function in `bot/researcher.py` centralises all parallel research logic. Both call sites import and use it; no parallel boilerplate is duplicated.

### Files changed

| File | Change |
|---|---|
| `bot/researcher.py` | Add `gather_research_batch(tickers, max_workers) -> dict[str, ResearchReport \| None]` |
| `screener/factor_scorer.py` | Replace sequential loop with `gather_research_batch`; accept `research_workers` param |
| `orchestration/main_loop.py` | Pre-fetch all research in `run_exit_review` before the decision loop |
| `system/config.py` | Add `research_concurrency: int = 5` to `UniverseConfig` |

No DB schema changes.

---

## `gather_research_batch`

```python
import concurrent.futures

def gather_research_batch(
    tickers: list[str],
    max_workers: int = 5,
) -> dict[str, ResearchReport | None]:
    """Fetch ResearchReport for multiple tickers concurrently.

    Returns a dict keyed by ticker. Failed tickers map to None.
    Iteration order matches the input list.
    """
    if not tickers:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {t: pool.submit(gather_research, t) for t in tickers}
    return {t: futures[t].result() for t in tickers}
```

`gather_research` already catches all exceptions and returns `None`, so `.result()` never raises. The `with` block ensures all futures complete before results are read. Dict insertion order (Python 3.7+) preserves input ordering.

---

## Config

In `UniverseConfig`:

```python
research_concurrency: int = 5   # max parallel gather_research calls
```

---

## Call site 1: `screener/factor_scorer.py`

`run_factor_screen` gains a new parameter:

```python
def run_factor_screen(
    tickers: list[str],
    top_n: int = 12,
    research_workers: int = 5,
) -> list[FactorCandidate]:
```

Replace the sequential loop at the bottom of `run_factor_screen`:

```python
# Before
for ticker_idx, row in top.iterrows():
    t = str(ticker_idx)
    research = gather_research(t)
    candidates.append(FactorCandidate(..., research=research))

# After
top_tickers = [str(t) for t in top.index]
research_map = gather_research_batch(top_tickers, max_workers=research_workers)
for ticker_idx, row in top.iterrows():
    t = str(ticker_idx)
    candidates.append(FactorCandidate(..., research=research_map.get(t)))
```

The orchestrator passes `research_workers=self._cfg.universe.research_concurrency` when calling `run_factor_screen`.

---

## Call site 2: `orchestration/main_loop.py` — `run_exit_review`

Pre-fetch all research before the decision loop:

```python
def run_exit_review(self) -> None:
    if not _NYSE.is_session(date.today().isoformat()):
        return
    log.info("Exit review started")
    positions = get_open_positions()
    if not positions:
        return

    # Parallel research fetch
    tickers = [pos["ticker"] for pos in positions
               if pos.get("signal_source") != "hedge"]
    research_map = gather_research_batch(
        tickers,
        max_workers=self._cfg.universe.research_concurrency,
    )

    for pos in positions:
        if pos.get("signal_source") == "hedge":
            continue
        try:
            info = yf.Ticker(pos["ticker"]).info
            current_price = info.get("regularMarketPrice", pos["entry_price"])
            days_held = (date.today() - date.fromisoformat(pos["entry_date"])).days
            research = research_map.get(pos["ticker"])
            decision = review_exit(pos["ticker"], pos["entry_price"],
                                   current_price, days_held, research=research)
            if decision.action == "exit":
                self._portfolio.close_position(...)
            elif decision.action == "reduce":
                self._portfolio.reduce_position(...)
        except Exception:
            log.exception("Exit review failed for %s", pos.get("ticker", "?"))
```

The hedge-position guard (`signal_source != "hedge"`) is already present from the Tier 1 fixes; it is preserved here.

---

## Testing

`tests/test_researcher.py` additions:
- `test_gather_research_batch_returns_dict_keyed_by_ticker` — 2 tickers, mocked, verify keys
- `test_gather_research_batch_preserves_order` — verify dict iteration order matches input
- `test_gather_research_batch_returns_none_for_failed_ticker` — one ticker raises, result is None
- `test_gather_research_batch_empty_input_returns_empty_dict`

`tests/test_factor_scorer.py` addition:
- `test_run_factor_screen_calls_gather_research_batch` — patch `gather_research_batch`, verify called once with the top-N tickers

`tests/test_orchestrator.py` addition:
- `test_run_exit_review_pre_fetches_research_in_batch` — patch `gather_research_batch`, verify called once before position loop
