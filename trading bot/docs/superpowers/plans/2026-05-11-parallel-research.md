# Parallel Research Gathering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace sequential `gather_research()` loops in the factor screener and exit review with a parallel batch fetch, cutting wall-clock time proportional to candidate count.

**Architecture:** A new `gather_research_batch()` function in `bot/researcher.py` uses `concurrent.futures.ThreadPoolExecutor` with a configurable worker cap. The factor screener and exit review each call it once before their processing loops. The concurrency limit lives in `UniverseConfig.research_concurrency`.

**Tech Stack:** Python 3.14, `concurrent.futures` (stdlib), yfinance, pytest, pytest-mock.

---

## File Map

| File | Task | Change |
|---|---|---|
| `bot/researcher.py` | 1 | Add `import concurrent.futures`; add `gather_research_batch()` |
| `system/config.py` | 1 | Add `research_concurrency: int = 5` to `UniverseConfig` |
| `tests/test_researcher.py` | 1 | Add 4 batch tests |
| `screener/factor_scorer.py` | 2 | Import `gather_research_batch`; add `research_workers` param to `run_factor_screen`; replace sequential loop |
| `tests/test_factor_scorer.py` | 2 | Add 1 test confirming batch is used |
| `orchestration/main_loop.py` | 3 | Import `gather_research_batch`; pre-fetch in `run_exit_review`; pass `research_workers` to `run_factor_screen` |
| `tests/test_orchestrator.py` | 3 | Add 1 test confirming batch pre-fetch in exit review |

---

## Task 1: `gather_research_batch` + Config

**Files:**
- Modify: `bot/researcher.py`
- Modify: `system/config.py`
- Modify: `tests/test_researcher.py`

- [ ] **Step 1: Write failing tests**

Add to the bottom of `tests/test_researcher.py`:

```python
def test_gather_research_batch_returns_dict_keyed_by_ticker(mocker):
    from bot.researcher import gather_research_batch
    mocker.patch("bot.researcher.gather_research", return_value=None)
    result = gather_research_batch(["AAPL", "MSFT"])
    assert set(result.keys()) == {"AAPL", "MSFT"}


def test_gather_research_batch_preserves_input_order(mocker):
    from bot.researcher import gather_research_batch
    mocker.patch("bot.researcher.gather_research", return_value=None)
    result = gather_research_batch(["AAPL", "MSFT", "GOOG"])
    assert list(result.keys()) == ["AAPL", "MSFT", "GOOG"]


def test_gather_research_batch_empty_input_returns_empty_dict():
    from bot.researcher import gather_research_batch
    assert gather_research_batch([]) == {}


def test_gather_research_batch_handles_unexpected_exception(mocker):
    from bot.researcher import gather_research_batch
    def _raise(ticker):
        if ticker == "FAIL":
            raise RuntimeError("unexpected")
        return None
    mocker.patch("bot.researcher.gather_research", side_effect=_raise)
    result = gather_research_batch(["AAPL", "FAIL"])
    assert result["FAIL"] is None
    assert "AAPL" in result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_researcher.py::test_gather_research_batch_returns_dict_keyed_by_ticker tests/test_researcher.py::test_gather_research_batch_preserves_input_order tests/test_researcher.py::test_gather_research_batch_empty_input_returns_empty_dict tests/test_researcher.py::test_gather_research_batch_handles_unexpected_exception -v
```

Expected: 4 FAILED (ImportError: cannot import name `gather_research_batch`)

- [ ] **Step 3: Add `import concurrent.futures` to `bot/researcher.py`**

Add after the existing stdlib imports at the top of the file (after `import sys`, before `from dataclasses import dataclass`):

```python
import concurrent.futures
```

- [ ] **Step 4: Add `gather_research_batch` to `bot/researcher.py`**

Add immediately after the `gather_research` function (before the end of the file):

```python
def gather_research_batch(
    tickers: list[str],
    max_workers: int = 5,
) -> dict[str, "ResearchReport | None"]:
    """Fetch ResearchReport for multiple tickers concurrently.

    Returns a dict keyed by ticker in the same order as the input list.
    Tickers that fail (or where gather_research returns None) map to None.
    """
    if not tickers:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {t: pool.submit(gather_research, t) for t in tickers}
    result: dict[str, "ResearchReport | None"] = {}
    for t in tickers:
        try:
            result[t] = futures[t].result()
        except Exception:
            result[t] = None
    return result
```

- [ ] **Step 5: Add `research_concurrency` to `UniverseConfig` in `system/config.py`**

In `UniverseConfig`, add after `event_exclusion_window_days`:

```python
    research_concurrency: int = 5   # max parallel gather_research calls
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd "trading bot" && python3 -m pytest tests/test_researcher.py::test_gather_research_batch_returns_dict_keyed_by_ticker tests/test_researcher.py::test_gather_research_batch_preserves_input_order tests/test_researcher.py::test_gather_research_batch_empty_input_returns_empty_dict tests/test_researcher.py::test_gather_research_batch_handles_unexpected_exception -v
```

Expected: 4 PASSED

- [ ] **Step 7: Run full suite for regressions**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 8: Commit**

```bash
cd "trading bot" && git add bot/researcher.py system/config.py tests/test_researcher.py && git commit -m "$(cat <<'EOF'
feat: add gather_research_batch for parallel research fetching

Uses ThreadPoolExecutor with configurable max_workers (default 5).
Returns dict keyed by ticker preserving input order. Unexpected
exceptions per-ticker are caught and mapped to None. Adds
research_concurrency: int = 5 to UniverseConfig.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Factor Screener Call Site

**Context:** `screener/factor_scorer.py` already imports `concurrent.futures` and uses it for `_fetch_info`. Lines 161–172 contain the sequential `gather_research(t)` loop that this task replaces.

**Files:**
- Modify: `screener/factor_scorer.py`
- Modify: `tests/test_factor_scorer.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_factor_scorer.py`:

```python
def test_run_factor_screen_uses_gather_research_batch(mocker):
    mocker.patch(
        "screener.factor_scorer._fetch_info",
        side_effect=lambda t: (t, _make_info()),
    )
    mocker.patch(
        "screener.factor_scorer._fetch_momentum_batch",
        return_value={"AAPL": (5.0, 10.0), "MSFT": (3.0, 8.0)},
    )
    batch_spy = mocker.patch(
        "screener.factor_scorer.gather_research_batch",
        return_value={"AAPL": None, "MSFT": None},
    )
    run_factor_screen(["AAPL", "MSFT"], top_n=2)
    batch_spy.assert_called_once()
    tickers_arg = batch_spy.call_args[0][0]
    assert set(tickers_arg) == {"AAPL", "MSFT"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "trading bot" && python3 -m pytest tests/test_factor_scorer.py::test_run_factor_screen_uses_gather_research_batch -v
```

Expected: FAILED (the test patches `gather_research_batch` but the screener still calls `gather_research` directly, so the spy is never called)

- [ ] **Step 3: Update `screener/factor_scorer.py` imports**

Find:
```python
from bot.researcher import gather_research, ResearchReport
```

Replace with:
```python
from bot.researcher import gather_research, gather_research_batch, ResearchReport
```

- [ ] **Step 4: Add `research_workers` parameter to `run_factor_screen`**

Find:
```python
def run_factor_screen(tickers: list[str], top_n: int = 12) -> list[FactorCandidate]:
```

Replace with:
```python
def run_factor_screen(
    tickers: list[str],
    top_n: int = 12,
    research_workers: int = 5,
) -> list[FactorCandidate]:
```

- [ ] **Step 5: Replace sequential research loop in `run_factor_screen`**

Find (at the bottom of `run_factor_screen`):

```python
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
```

Replace with:

```python
    top_tickers = [str(t) for t in top.index]
    research_map = gather_research_batch(top_tickers, max_workers=research_workers)

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
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd "trading bot" && python3 -m pytest tests/test_factor_scorer.py::test_run_factor_screen_uses_gather_research_batch -v
```

Expected: PASSED

- [ ] **Step 7: Run full suite**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 8: Commit**

```bash
cd "trading bot" && git add screener/factor_scorer.py tests/test_factor_scorer.py && git commit -m "$(cat <<'EOF'
feat: parallelize research in factor screener

Replaces sequential gather_research loop with gather_research_batch.
Adds research_workers parameter to run_factor_screen (default 5).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Exit Review + Orchestrator Wiring

**Context:** `run_exit_review` in `orchestration/main_loop.py` (line 657) currently calls `gather_research(pos["ticker"])` inside the loop for each position. Also, `run_factor_screen` is called in `run_morning_pipeline` without passing `research_workers` — this task adds that.

**Files:**
- Modify: `orchestration/main_loop.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_orchestrator.py`:

```python
def test_run_exit_review_pre_fetches_research_in_batch(mocker, orch):
    orch._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[
        {
            "ticker": "AAPL", "entry_price": 100.0, "entry_date": "2026-04-01",
            "shares": 10.0, "signal_id": 1, "signal_source": "congressional",
        },
        {
            "ticker": "MSFT", "entry_price": 200.0, "entry_date": "2026-04-01",
            "shares": 5.0, "signal_id": 2, "signal_source": "congressional",
        },
    ])
    batch_spy = mocker.patch(
        "orchestration.main_loop.gather_research_batch",
        return_value={"AAPL": None, "MSFT": None},
    )
    mocker.patch(
        "orchestration.main_loop.yf.Ticker",
        return_value=MagicMock(info={"regularMarketPrice": 110.0}),
    )
    mocker.patch(
        "orchestration.main_loop.review_exit",
        return_value=MagicMock(action="hold", rationale="hold"),
    )
    orch.run_exit_review()
    batch_spy.assert_called_once()
    tickers_fetched = set(batch_spy.call_args[0][0])
    assert tickers_fetched == {"AAPL", "MSFT"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "trading bot" && python3 -m pytest tests/test_orchestrator.py::test_run_exit_review_pre_fetches_research_in_batch -v
```

Expected: FAILED (`gather_research_batch` not called — the method still uses `gather_research` per position)

- [ ] **Step 3: Update the import in `orchestration/main_loop.py`**

Find:
```python
from bot.researcher import gather_research
```

Replace with:
```python
from bot.researcher import gather_research, gather_research_batch
```

- [ ] **Step 4: Rewrite `run_exit_review` to pre-fetch research in batch**

Replace the entire `run_exit_review` method with:

```python
    def run_exit_review(self) -> None:
        if not _NYSE.is_session(date.today().isoformat()):
            return
        log.info("Exit review started")
        positions = [
            pos for pos in get_open_positions()
            if pos.get("signal_source") != "hedge"
        ]
        if not positions:
            return

        tickers = [pos["ticker"] for pos in positions]
        research_map = gather_research_batch(
            tickers,
            max_workers=self._cfg.universe.research_concurrency,
        )

        for pos in positions:
            try:
                info = yf.Ticker(pos["ticker"]).info
                current_price = info.get("regularMarketPrice", pos["entry_price"])
                days_held = (date.today() - date.fromisoformat(pos["entry_date"])).days
                research = research_map.get(pos["ticker"])
                decision = review_exit(pos["ticker"], pos["entry_price"],
                                       current_price, days_held, research=research)
                if decision.action == "exit":
                    self._portfolio.close_position(
                        pos["ticker"], pos["shares"], exit_price=current_price,
                        exit_reason="ai_exit", signal_id=pos["signal_id"] or 0,
                        entry_price=pos["entry_price"], entry_date=pos["entry_date"],
                    )
                    log.info("Closed %s: %s", pos["ticker"], decision.rationale)
                elif decision.action == "reduce":
                    self._portfolio.reduce_position(
                        pos["ticker"], pos["shares"], exit_price=current_price,
                        signal_id=pos["signal_id"] or 0, entry_price=pos["entry_price"],
                        entry_date=pos["entry_date"],
                    )
                    log.info("Reduced %s: %s", pos["ticker"], decision.rationale)
            except Exception:
                log.exception("Exit review failed for %s", pos.get("ticker", "?"))
```

- [ ] **Step 5: Pass `research_workers` to `run_factor_screen` in `run_morning_pipeline`**

Find (inside the `if not _at_capacity:` block):
```python
                candidates = run_factor_screen(universe, top_n=_SCREENER_TOP_N)
```

Replace with:
```python
                candidates = run_factor_screen(
                    universe,
                    top_n=_SCREENER_TOP_N,
                    research_workers=self._cfg.universe.research_concurrency,
                )
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd "trading bot" && python3 -m pytest tests/test_orchestrator.py::test_run_exit_review_pre_fetches_research_in_batch -v
```

Expected: PASSED

- [ ] **Step 7: Run full suite**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 8: Commit**

```bash
cd "trading bot" && git add orchestration/main_loop.py tests/test_orchestrator.py && git commit -m "$(cat <<'EOF'
feat: parallelize research in exit review; pass research_workers to screener

run_exit_review now pre-fetches research for all non-hedge positions
in a single gather_research_batch call before the decision loop.
run_factor_screen receives research_workers=cfg.universe.research_concurrency.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Final Check

```bash
cd "trading bot" && python3 -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: all green.
