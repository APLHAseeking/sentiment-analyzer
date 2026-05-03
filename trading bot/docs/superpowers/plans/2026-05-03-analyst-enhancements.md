# Analyst Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bull/bear debate to high-conviction AI scoring, news sentiment scoring to the research pipeline, and recalibrate three risk thresholds.

**Architecture:** Three independent features touching five files. Risk changes are config-only plus two small additions. Sentiment scoring adds a Claude Haiku call inside `gather_research()`. Debate wraps the existing `score_entry()` in a 4-call pipeline gated at conviction ≥ 7.

**Tech Stack:** Python 3.14, Anthropic SDK, pytest, pytest-mock, SQLite (no new dependencies).

---

## File Map

| File | Change |
|---|---|
| `system/config.py` | Adjust 2 thresholds; add `max_invested_pct` field and validation |
| `risk/risk_manager.py` | Add `max_invested_pct` to `status_dict()` |
| `orchestration/main_loop.py` | Add invested-pct gate to `run_morning_pipeline()`; swap `score_entry` → `score_entry_with_debate` |
| `bot/researcher.py` | Add `import json`; add 3 sentiment fields to `ResearchReport`; add `_get_sentiment_client()`, `_score_sentiment()`; expand news fetch; update `format_research_for_prompt()` |
| `bot/ai_analyst.py` | Add `debate_context` param to `score_entry()`; extract `_build_entry_prompt()`; add `_BULL_SYSTEM`, `_BEAR_SYSTEM`, `_bull_argument()`, `_bear_argument()`, `score_entry_with_debate()` |
| `tests/test_config.py` | Add 1 test for `max_invested_pct` validation |
| `tests/test_risk_manager.py` | Add 1 test for `status_dict` |
| `tests/test_orchestrator.py` | New file — 2 orchestrator tests for invested-pct gate |
| `tests/test_researcher.py` | Add 5 sentiment tests |
| `tests/test_ai_analyst.py` | Add 4 debate tests |

**Run all tests:** `cd "trading bot" && python -m pytest tests/ -v`

---

## Task 1: Risk Config Changes

**Files:**
- Modify: `system/config.py`
- Modify: `risk/risk_manager.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_risk_manager.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_validate_rejects_invalid_max_invested_pct():
    risk = RiskConfig(max_invested_pct=0.0)
    s = Settings(risk=risk)
    with pytest.raises(ValueError, match="max_invested_pct"):
        s.validate()


def test_validate_accepts_valid_max_invested_pct():
    risk = RiskConfig(max_invested_pct=80.0)
    s = Settings(risk=risk)
    s.validate()  # should not raise
```

Add to `tests/test_risk_manager.py`:

```python
def test_status_dict_includes_max_invested_pct(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    d = mgr.status_dict()
    assert "max_invested_pct" in d
    assert d["max_invested_pct"] == 80.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python -m pytest tests/test_config.py::test_validate_rejects_invalid_max_invested_pct tests/test_config.py::test_validate_accepts_valid_max_invested_pct tests/test_risk_manager.py::test_status_dict_includes_max_invested_pct -v
```

Expected: 3 FAILED (attribute doesn't exist yet)

- [ ] **Step 3: Update `system/config.py`**

In `RiskConfig`, make three changes:

```python
# Change 1: daily_loss_reduce_pct default  2.0 → 3.0
daily_loss_reduce_pct: float = 3.0       # was 2.0

# Change 2: max_adv_pct default  10.0 → 5.0
max_adv_pct: float = 5.0                 # was 10.0

# Change 3: add new field at the end of RiskConfig (after lock_file_path)
max_invested_pct: float = 80.0           # cap total deployed NAV %
```

In `Settings.validate()`, add after the existing checks:

```python
        if self.risk.max_invested_pct <= 0 or self.risk.max_invested_pct > 100:
            raise ValueError("max_invested_pct must be in (0, 100]")
```

- [ ] **Step 4: Update `risk/risk_manager.py` — `status_dict()`**

Locate `status_dict()` (currently returns 4 keys). Add one entry:

```python
    def status_dict(self) -> dict:
        return {
            "state": self._state.value,
            "peak_nav": self._peak_nav,
            "day_start_nav": self._day_start_nav,
            "week_start_nav": self._week_start_nav,
            "lock_file_exists": os.path.exists(self._risk.lock_file_path),
            "max_invested_pct": self._risk.max_invested_pct,
        }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd "trading bot" && python -m pytest tests/test_config.py tests/test_risk_manager.py -v
```

Expected: all green. The existing `_make_manager` helper in `test_risk_manager.py` doesn't pass `max_invested_pct`, so it uses the new default of 80.0 — the new status_dict test will see that value. The existing threshold tests still pass because they pass explicit values for `daily_loss_reduce_pct` and `max_adv_pct`.

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add system/config.py risk/risk_manager.py tests/test_config.py tests/test_risk_manager.py
git commit -m "feat: recalibrate risk thresholds and add max_invested_pct

- daily_loss_reduce_pct 2% → 3% (reduce false triggers in bull regime)
- max_adv_pct 10% → 5% (tighter liquidity gate)
- max_invested_pct = 80% (new cap on total deployed NAV)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Orchestrator Invested-Pct Gate

**Files:**
- Modify: `orchestration/main_loop.py`
- Create: `tests/test_orchestrator.py`

- [ ] **Step 1: Create `tests/test_orchestrator.py` with failing tests**

```python
"""Tests for RegimeAwareOrchestrator — invested-pct capacity gate."""
from unittest.mock import MagicMock
import pytest
from orchestration.main_loop import RegimeAwareOrchestrator


def _mock_broker(cash: float, position_value: float) -> MagicMock:
    broker = MagicMock()
    broker.get_cash.return_value = cash
    broker.get_equity.return_value = cash + position_value
    if position_value > 0:
        broker.get_positions.return_value = [
            {"ticker": "SPY", "qty": 1, "current_price": position_value}
        ]
    else:
        broker.get_positions.return_value = []
    return broker


@pytest.fixture
def orch(mocker):
    mocker.patch("orchestration.main_loop._NYSE.is_session", return_value=True)
    mocker.patch("orchestration.main_loop.get_regime_data", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.run_scraper", return_value=[])
    mocker.patch("orchestration.main_loop.filter_disclosures", return_value=[])
    mocker.patch("orchestration.main_loop.get_universe", return_value=[])
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[])
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])

    from system.config import settings
    o = RegimeAwareOrchestrator(settings)
    o._portfolio = MagicMock()
    o._risk = MagicMock()
    o._store = MagicMock()
    o._market_data = MagicMock()
    o._regime_state = None
    o._engine = MagicMock()
    o._engine.is_fitted = False
    return o


def test_pipeline_skips_entries_when_at_capacity(mocker, orch):
    orch._broker = _mock_broker(cash=15_000, position_value=85_000)  # 85% invested
    process_spy = mocker.patch.object(orch, "_process_signal")
    fundamental_spy = mocker.patch.object(orch, "_process_fundamental_candidate")

    orch.run_morning_pipeline()

    process_spy.assert_not_called()
    fundamental_spy.assert_not_called()
    orch._portfolio.enforce_stop_losses.assert_called_once()


def test_pipeline_enforces_stop_losses_even_at_capacity(mocker, orch):
    orch._broker = _mock_broker(cash=5_000, position_value=95_000)  # 95% invested
    mocker.patch.object(orch, "_process_signal")

    orch.run_morning_pipeline()

    orch._portfolio.enforce_stop_losses.assert_called_once()
    orch._portfolio.enforce_take_profits.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python -m pytest tests/test_orchestrator.py -v
```

Expected: 2 FAILED (gate logic not implemented yet)

- [ ] **Step 3: Edit `orchestration/main_loop.py` — add invested-pct gate**

In `run_morning_pipeline()`, insert the capacity check AFTER the risk manager update block and BEFORE the scraping call. The full pipeline section changes from:

```python
        # --- Scrape and filter congressional signals ---------------------
        new_disclosures = run_scraper()
        qualified = filter_disclosures(new_disclosures)
        log.info("Disclosures: %d new, %d qualified", len(new_disclosures), len(qualified))

        # --- Regime state as gate ---------------------------------------
        if self._regime_state is None:
            log.warning("No regime state — processing signals without regime filter")

        sector_allocation: dict[str, float] = {}
        try:
            positions = self._broker.get_positions()
            if positions:
                nav = self._broker.get_cash() + sum(
                    p["qty"] * p["current_price"] for p in positions
                )
                if nav > 0:
                    for pos in positions:
                        sector = get_sector_for_ticker(pos["ticker"])
                        pv = pos["qty"] * pos["current_price"]
                        sector_allocation[sector] = sector_allocation.get(sector, 0.0) + pv / nav * 100
        except Exception as exc:
            log.warning("Sector allocation computation failed: %s", exc)

        congress_tickers: set[str] = {disc["ticker"] for disc in qualified}

        for disc in qualified:
            if not self._portfolio.can_open_new_position():
                log.info("Position limit reached — stopping")
                break
            try:
                self._process_signal(disc, sector_allocation)
            except Exception:
                log.exception("Failed processing %s — skipping", disc.get("ticker", "?"))

        # ── Phase 2: fundamental screener (regime-aware) ─────────────────────────
        try:
            universe = list(get_universe())
            candidates = run_factor_screen(universe, top_n=_SCREENER_TOP_N)
            already_open = (
                {p["ticker"] for p in self._broker.get_positions()}
                | {pos["ticker"] for pos in get_open_positions()}
            )

            for candidate in candidates:
                if not self._portfolio.can_open_new_position():
                    log.info("Position limit reached — stopping Phase 2")
                    break
                if candidate.ticker in already_open:
                    continue
                try:
                    opened = self._process_fundamental_candidate(
                        candidate, sector_allocation, congress_tickers
                    )
                    if opened:
                        already_open.add(candidate.ticker)
                except Exception:
                    log.exception(
                        "Failed processing fundamental candidate %s — skipping",
                        candidate.ticker,
                    )
        except Exception:
            log.exception("Phase 2 fundamental screener failed — skipping")
```

To:

```python
        # --- Invested-pct capacity check --------------------------------
        _position_list = self._broker.get_positions()
        if _position_list:
            _nav = self._broker.get_cash() + sum(
                p["qty"] * p["current_price"] for p in _position_list
            )
            _invested_pct = (
                sum(p["qty"] * p["current_price"] for p in _position_list)
                / _nav * 100 if _nav > 0 else 0.0
            )
        else:
            _invested_pct = 0.0
        _at_capacity = _invested_pct >= self._cfg.risk.max_invested_pct
        if _at_capacity:
            log.info(
                "Portfolio at %.1f%% invested (cap %.1f%%) — skipping new entries",
                _invested_pct, self._cfg.risk.max_invested_pct,
            )

        # --- Scrape (always, for DB persistence) ------------------------
        new_disclosures = run_scraper()
        qualified = filter_disclosures(new_disclosures)
        log.info("Disclosures: %d new, %d qualified", len(new_disclosures), len(qualified))

        if not _at_capacity:
            # --- Regime state as gate -----------------------------------
            if self._regime_state is None:
                log.warning("No regime state — processing signals without regime filter")

            sector_allocation: dict[str, float] = {}
            try:
                positions = self._broker.get_positions()
                if positions:
                    nav = self._broker.get_cash() + sum(
                        p["qty"] * p["current_price"] for p in positions
                    )
                    if nav > 0:
                        for pos in positions:
                            sector = get_sector_for_ticker(pos["ticker"])
                            pv = pos["qty"] * pos["current_price"]
                            sector_allocation[sector] = sector_allocation.get(sector, 0.0) + pv / nav * 100
            except Exception as exc:
                log.warning("Sector allocation computation failed: %s", exc)

            congress_tickers: set[str] = {disc["ticker"] for disc in qualified}

            for disc in qualified:
                if not self._portfolio.can_open_new_position():
                    log.info("Position limit reached — stopping")
                    break
                try:
                    self._process_signal(disc, sector_allocation)
                except Exception:
                    log.exception("Failed processing %s — skipping", disc.get("ticker", "?"))

            # ── Phase 2: fundamental screener (regime-aware) ─────────────────
            try:
                universe = list(get_universe())
                candidates = run_factor_screen(universe, top_n=_SCREENER_TOP_N)
                already_open = (
                    {p["ticker"] for p in self._broker.get_positions()}
                    | {pos["ticker"] for pos in get_open_positions()}
                )

                for candidate in candidates:
                    if not self._portfolio.can_open_new_position():
                        log.info("Position limit reached — stopping Phase 2")
                        break
                    if candidate.ticker in already_open:
                        continue
                    try:
                        opened = self._process_fundamental_candidate(
                            candidate, sector_allocation, congress_tickers
                        )
                        if opened:
                            already_open.add(candidate.ticker)
                    except Exception:
                        log.exception(
                            "Failed processing fundamental candidate %s — skipping",
                            candidate.ticker,
                        )
            except Exception:
                log.exception("Phase 2 fundamental screener failed — skipping")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "trading bot" && python -m pytest tests/test_orchestrator.py -v
```

Expected: 2 PASSED

- [ ] **Step 5: Run full suite to check for regressions**

```bash
cd "trading bot" && python -m pytest tests/ -v
```

Expected: all green

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add orchestration/main_loop.py tests/test_orchestrator.py
git commit -m "feat: add invested-pct capacity gate to morning pipeline

Skips Phase 1 and Phase 2 entry loops when portfolio exceeds
max_invested_pct (default 80%). Stop-losses and take-profits
still run. Scraping always runs for DB persistence.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Sentiment Scoring

**Files:**
- Modify: `bot/researcher.py`
- Modify: `tests/test_researcher.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_researcher.py`:

```python
import json as _json


def test_score_sentiment_returns_none_tuple_on_api_failure(mocker):
    from bot.researcher import _score_sentiment
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("network error")
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    assert _score_sentiment("Some headlines") == (None, None, ())


def test_score_sentiment_parses_valid_response(mocker):
    from bot.researcher import _score_sentiment
    payload = _json.dumps({
        "sentiment": "bullish", "strength": 2, "key_themes": ["earnings beat", "guidance raised"]
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    label, strength, themes = _score_sentiment("headlines block")
    assert label == "bullish"
    assert strength == 2
    assert "earnings beat" in themes


def test_gather_research_populates_sentiment_fields(mocker):
    _make_mock_ticker(mocker)
    payload = _json.dumps({
        "sentiment": "bearish", "strength": 3, "key_themes": ["guidance cut"]
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    report = gather_research("AAPL")
    assert report.sentiment_label == "bearish"
    assert report.sentiment_strength == 3
    assert "guidance cut" in report.sentiment_themes


def test_gather_research_handles_sentiment_failure_gracefully(mocker):
    _make_mock_ticker(mocker)
    mocker.patch("bot.researcher._get_sentiment_client", side_effect=RuntimeError("no key"))
    report = gather_research("AAPL")
    assert report is not None
    assert report.sentiment_label is None
    assert report.sentiment_strength is None
    assert report.sentiment_themes == ()


def test_format_research_includes_sentiment_line_when_present(mocker):
    _make_mock_ticker(mocker)
    payload = _json.dumps({
        "sentiment": "bullish", "strength": 2, "key_themes": ["revenue growth", "margin expansion"]
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    report = gather_research("AAPL")
    formatted = format_research_for_prompt(report)
    assert "Sentiment" in formatted
    assert "bullish/2" in formatted
    assert "revenue growth" in formatted


def test_format_research_omits_sentiment_line_when_none(mocker):
    _make_mock_ticker(mocker)
    mocker.patch("bot.researcher._get_sentiment_client", side_effect=RuntimeError("no key"))
    report = gather_research("AAPL")
    formatted = format_research_for_prompt(report)
    assert "Sentiment" not in formatted
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python -m pytest tests/test_researcher.py::test_score_sentiment_returns_none_tuple_on_api_failure tests/test_researcher.py::test_score_sentiment_parses_valid_response tests/test_researcher.py::test_gather_research_populates_sentiment_fields tests/test_researcher.py::test_gather_research_handles_sentiment_failure_gracefully tests/test_researcher.py::test_format_research_includes_sentiment_line_when_present tests/test_researcher.py::test_format_research_omits_sentiment_line_when_none -v
```

Expected: 6 FAILED

- [ ] **Step 3: Update `bot/researcher.py`**

**3a.** Add `import json` at the top (after `from __future__ import annotations`):

```python
import json
```

**3b.** Add three new fields to `ResearchReport` after the `headlines` field:

```python
    headlines: tuple[str, ...]
    # Sentiment (populated by _score_sentiment; None if scoring failed)
    sentiment_label: str | None = None       # "bullish" | "neutral" | "bearish"
    sentiment_strength: int | None = None    # 1 (weak) | 2 (moderate) | 3 (strong)
    sentiment_themes: tuple[str, ...] = ()
```

**3c.** Add the sentinel client and `_score_sentiment` helper after the `_RATING_MAP` constant:

```python
_sentiment_client: "Anthropic | None" = None


def _get_sentiment_client():
    global _sentiment_client
    if _sentiment_client is None:
        import os
        from anthropic import Anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY")
        _sentiment_client = Anthropic(api_key=api_key)
    return _sentiment_client


_SENTIMENT_SYSTEM = (
    "You are a financial news sentiment analyzer. "
    "Respond with ONLY valid JSON matching exactly: "
    '{"sentiment": "bullish"|"neutral"|"bearish", "strength": 1|2|3, '
    '"key_themes": ["theme1", "theme2"]}'
)


def _score_sentiment(
    news_block: str,
) -> tuple[str | None, int | None, tuple[str, ...]]:
    try:
        client = _get_sentiment_client()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=_SENTIMENT_SYSTEM,
            messages=[{"role": "user", "content": news_block}],
        )
        data = json.loads(resp.content[0].text)
        label = data.get("sentiment")
        strength = int(data.get("strength", 1))
        themes = tuple(str(t) for t in data.get("key_themes", [])[:3])
        if label not in ("bullish", "neutral", "bearish"):
            return None, None, ()
        if strength not in (1, 2, 3):
            return None, None, ()
        return label, strength, themes
    except Exception:
        return None, None, ()
```

**3d.** In `gather_research()`, replace:

```python
        news_items = t.news[:8]
        headlines = tuple(
            item.get("content", {}).get("title", "")
            for item in news_items
            if item.get("content", {}).get("title")
        )
```

With:

```python
        news_items = t.news[:28]

        # Build rich block (title + summary) for sentiment scoring
        news_texts = []
        for item in news_items:
            title = item.get("content", {}).get("title", "")
            summary = item.get("content", {}).get("summary", "")
            if title:
                entry = f"- {title}"
                if summary:
                    entry += f": {summary}"
                news_texts.append(entry)
        news_block = "\n".join(news_texts)

        # headlines field: titles only (first 8, for prompt display)
        headlines = tuple(
            item.get("content", {}).get("title", "")
            for item in news_items[:8]
            if item.get("content", {}).get("title")
        )

        sentiment_label, sentiment_strength, sentiment_themes = (
            _score_sentiment(news_block) if news_block else (None, None, ())
        )
```

**3e.** In `gather_research()`, update the `ResearchReport(...)` constructor call to include the new fields. Add these three lines at the end before the closing `)`:

```python
            sentiment_label=sentiment_label,
            sentiment_strength=sentiment_strength,
            sentiment_themes=sentiment_themes,
```

**3f.** Update `format_research_for_prompt()`. Replace the `return (...)` block with:

```python
    out = (
        "--- INDEPENDENT RESEARCH ---\n"
        f"Company: {report.company_name} | Sector: {report.sector} | "
        f"Market cap: ${_fmt(mcap, '.1f')}B\n"
        f"Valuation: P/E {_fmt(report.pe_trailing, '.1f')}x "
        f"(fwd {_fmt(report.pe_forward, '.1f')}x) | "
        f"P/B {_fmt(report.pb_ratio, '.1f')}x | "
        f"EV/EBITDA {_fmt(report.ev_ebitda, '.1f')}x | "
        f"PEG {_fmt(report.peg_ratio, '.2f')}\n"
        f"Financial health: ROE {roe_s} | Margin {margin_s} | "
        f"D/E {_fmt(report.debt_to_equity, '.2f')} | "
        f"FCF ${_fmt(fcf, '.1f')}B\n"
        f"Momentum: {mom_1m} (1m) | {mom_3m} (3m) | "
        f"52w ${_fmt(report.week52_low, '.2f')}–${_fmt(report.week52_high, '.2f')} | "
        f"Beta {_fmt(report.beta, '.2f')}\n"
        f"Growth: Revenue {rev_g} YoY | Earnings {earn_g} YoY\n"
        f"Analyst consensus: {report.analyst_rating or 'n/a'} | "
        f"Target ${_fmt(report.analyst_target, '.2f')} | "
        f"Coverage: {report.num_analysts or 'n/a'} analysts\n"
        f"Short interest: {si_s} of float | ADV: ${_fmt(adv, '.0f')}M/day\n"
    )
    if report.sentiment_label is not None:
        sentiment_str = f"{report.sentiment_label}/{report.sentiment_strength}"
        if report.sentiment_themes:
            sentiment_str += f" — themes: {', '.join(report.sentiment_themes)}"
        out += f"Sentiment ({len(report.headlines)} headlines, AI-scored): {sentiment_str}\n"
    out += f"Recent headlines:\n{headline_lines}\n---"
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "trading bot" && python -m pytest tests/test_researcher.py -v
```

Expected: all green

- [ ] **Step 5: Run full suite**

```bash
cd "trading bot" && python -m pytest tests/ -v
```

Expected: all green. Existing tests that construct `ResearchReport(...)` without sentiment fields continue to work because the new fields have defaults.

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add bot/researcher.py tests/test_researcher.py
git commit -m "feat: add news sentiment scoring to ResearchReport

Fetches up to 28 news items (title + summary) per ticker and scores
them with Claude Haiku. Adds sentiment_label, sentiment_strength,
sentiment_themes to ResearchReport. Gracefully returns None fields
on any API failure. Renders as a single line in the prompt context.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Bull/Bear Debate

**Files:**
- Modify: `bot/ai_analyst.py`
- Modify: `tests/test_ai_analyst.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ai_analyst.py`:

```python
from bot.ai_analyst import score_entry_with_debate


def _make_resp(text: str):
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


def _low_conviction_payload():
    return json.dumps({
        "conviction": 5, "position_pct": 1.5,
        "rationale": "Weak signal", "entry": "skip", "risk_flags": [],
    })


def _high_conviction_payload():
    return json.dumps({
        "conviction": 8, "position_pct": 5.0,
        "rationale": "Strong signal", "entry": "buy", "risk_flags": [],
    })


def _disc():
    return {
        "id": "d1", "politician": "Jane Doe", "ticker": "MSFT",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }


def test_debate_makes_one_call_when_conviction_below_7(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_resp(_low_conviction_payload())
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    result = score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )

    assert mock_client.messages.create.call_count == 1
    assert isinstance(result, EntryScore)
    assert result.conviction == 5


def test_debate_makes_four_calls_when_conviction_gte_7(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _make_resp(_high_conviction_payload()),  # call 1: initial score
        _make_resp("Strong revenue growth, market leadership..."),  # call 2: bull
        _make_resp("High valuation, margin pressure risk..."),       # call 3: bear
        _make_resp(_high_conviction_payload()),  # call 4: final score
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    result = score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )

    assert mock_client.messages.create.call_count == 4
    assert isinstance(result, EntryScore)


def test_debate_call4_prompt_includes_debate_block(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _make_resp(_high_conviction_payload()),
        _make_resp("Bull: Strong fundamentals"),
        _make_resp("Bear: Elevated risk"),
        _make_resp(_high_conviction_payload()),
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )

    call4_content = mock_client.messages.create.call_args_list[3][1]["messages"][0]["content"]
    assert "DEBATE" in call4_content
    assert "Bull case" in call4_content
    assert "Bear case" in call4_content


def test_debate_returns_entry_score_type(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _make_resp(_high_conviction_payload()),
        _make_resp("Bull: margins expanding, market share gains"),
        _make_resp("Bear: valuation stretched, macro headwinds"),
        _make_resp(_high_conviction_payload()),
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)
    result = score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )
    assert isinstance(result, EntryScore)
    assert result.entry == "buy"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python -m pytest tests/test_ai_analyst.py::test_debate_makes_one_call_when_conviction_below_7 tests/test_ai_analyst.py::test_debate_makes_four_calls_when_conviction_gte_7 tests/test_ai_analyst.py::test_debate_call4_prompt_includes_debate_block tests/test_ai_analyst.py::test_debate_returns_entry_score_type -v
```

Expected: 4 FAILED (ImportError: cannot import name `score_entry_with_debate`)

- [ ] **Step 3: Update `bot/ai_analyst.py`**

**3a.** Add the two new system prompt constants after `_EXIT_SYSTEM`:

```python
_BULL_SYSTEM = (
    "You are a buy-side equity analyst building an investment case. "
    "Given the signal context and research below, argue the strongest possible bull case. "
    "Cite specific metrics — P/E, revenue growth, margin trends, competitive position. "
    "Do not hedge or present risks. Be direct and evidence-based."
)

_BEAR_SYSTEM = (
    "You are a short-seller reviewing an investment thesis. "
    "The bull case for this stock is presented below. "
    "Identify the most serious flaws, risks, and overlooked negatives. "
    "Counter specific claims with evidence. Do not repeat the bull's points back — challenge them."
)
```

**3b.** Extract a `_build_entry_prompt()` helper. This replaces the inline prompt-building code in `score_entry()`. Insert this function before `score_entry()`:

```python
def _build_entry_prompt(
    disclosure: dict | None,
    committees: list[str],
    sector: str,
    lag_days: int,
    estimated_cost_pct: float,
    research: "ResearchReport | None",
    cluster_count: int,
    signal_type: str,
    factor_score: int | None,
    ticker: str | None,
    debate_context: str | None = None,
) -> str:
    from bot.researcher import format_research_for_prompt
    _ticker = (disclosure["ticker"] if disclosure else ticker) or "UNKNOWN"
    lines = [f"Ticker: {_ticker} | Sector: {sector}"]
    if signal_type in ("congressional", "both") and disclosure:
        lines += [
            f"Politician: {disclosure['politician']}",
            f"Transaction date: {disclosure['transaction_date']} | "
            f"Disclosure date: {disclosure['disclosure_date']}",
            f"Lag days: {lag_days}",
            f"Amount range: {disclosure['amount_range']}",
            f"Committees held: {', '.join(committees)}",
            f"Cluster count (other members buying same stock last 30d): {cluster_count}",
        ]
    if signal_type in ("fundamental", "both") and factor_score is not None:
        lines.append(f"Composite factor score: {factor_score}/99")
    lines.append(f"Estimated round-trip cost: {estimated_cost_pct:.2f}% of position")
    if research is not None:
        lines.append("\n" + format_research_for_prompt(research))
    if debate_context is not None:
        lines.append(f"\n--- DEBATE ---\n{debate_context}")
    lines.append("Score this signal.")
    return "\n".join(lines)
```

**3c.** Update `score_entry()` to use `_build_entry_prompt()` and accept the new `debate_context` parameter. Replace the body of `score_entry()` from the prompt-building section onward:

Old signature (last param):
```python
    ticker: str | None = None,
) -> EntryScore:
```

New signature:
```python
    ticker: str | None = None,
    debate_context: str | None = None,
) -> EntryScore:
```

Replace the prompt-building block in `score_entry()` (the `lines = [...]` section through `prompt = "\n".join(lines)`) with:

```python
    prompt = _build_entry_prompt(
        disclosure=disclosure,
        committees=committees,
        sector=sector,
        lag_days=lag_days,
        estimated_cost_pct=estimated_cost_pct,
        research=research,
        cluster_count=cluster_count,
        signal_type=signal_type,
        factor_score=factor_score,
        ticker=ticker,
        debate_context=debate_context,
    )
```

**3d.** Add the two debate helper functions after `_build_entry_prompt()`:

```python
def _bull_argument(prompt: str) -> str:
    client = _get_client()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_BULL_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def _bear_argument(prompt: str, bull_text: str) -> str:
    client = _get_client()
    combined = f"{prompt}\n\nBull case:\n{bull_text}"
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_BEAR_SYSTEM,
        messages=[{"role": "user", "content": combined}],
    )
    return resp.content[0].text
```

**3e.** Add `score_entry_with_debate()` after `score_entry()`:

```python
def score_entry_with_debate(
    disclosure: dict | None,
    committees: list[str],
    sector: str,
    lag_days: int,
    estimated_cost_pct: float,
    research: "ResearchReport | None" = None,
    cluster_count: int = 1,
    signal_type: str = "congressional",
    factor_score: int | None = None,
    ticker: str | None = None,
) -> EntryScore:
    """score_entry() with adversarial bull/bear deliberation for conviction >= 7.

    For conviction < 7: identical to score_entry() (1 API call).
    For conviction >= 7: runs bull argument, bear counter-argument, then
    re-scores with the debate appended (4 API calls total).
    """
    initial = score_entry(
        disclosure=disclosure,
        committees=committees,
        sector=sector,
        lag_days=lag_days,
        estimated_cost_pct=estimated_cost_pct,
        research=research,
        cluster_count=cluster_count,
        signal_type=signal_type,
        factor_score=factor_score,
        ticker=ticker,
    )
    if initial.conviction < 7:
        return initial

    prompt = _build_entry_prompt(
        disclosure=disclosure,
        committees=committees,
        sector=sector,
        lag_days=lag_days,
        estimated_cost_pct=estimated_cost_pct,
        research=research,
        cluster_count=cluster_count,
        signal_type=signal_type,
        factor_score=factor_score,
        ticker=ticker,
    )
    bull = _bull_argument(prompt)
    bear = _bear_argument(prompt, bull)
    debate = f"Bull case:\n{bull}\n\nBear case:\n{bear}"

    return score_entry(
        disclosure=disclosure,
        committees=committees,
        sector=sector,
        lag_days=lag_days,
        estimated_cost_pct=estimated_cost_pct,
        research=research,
        cluster_count=cluster_count,
        signal_type=signal_type,
        factor_score=factor_score,
        ticker=ticker,
        debate_context=debate,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "trading bot" && python -m pytest tests/test_ai_analyst.py -v
```

Expected: all green. The existing tests for `score_entry` still pass because `_build_entry_prompt` produces the same prompt — verify with `test_score_entry_with_research_injects_research_block` and `test_score_entry_without_research_omits_research_block`.

- [ ] **Step 5: Run full suite**

```bash
cd "trading bot" && python -m pytest tests/ -v
```

Expected: all green

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add bot/ai_analyst.py tests/test_ai_analyst.py
git commit -m "feat: add bull/bear debate to high-conviction entry scoring

score_entry_with_debate() wraps score_entry(). For conviction >= 7,
runs a bull researcher (Haiku) then bear researcher (Haiku) then
re-scores with the debate appended (4 calls total). Conviction < 7
costs 1 call, unchanged.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Wire Debate into Orchestrator

**Files:**
- Modify: `orchestration/main_loop.py`

- [ ] **Step 1: Update the import in `orchestration/main_loop.py`**

Locate the import line:

```python
from bot.ai_analyst import score_entry, review_exit, EntryScore
```

Replace with:

```python
from bot.ai_analyst import score_entry_with_debate, review_exit, EntryScore
```

- [ ] **Step 2: Replace the two `score_entry` call sites**

In `_process_signal()` (around line 299), replace:

```python
        score: EntryScore = score_entry(
            disc, committees=committees, sector=sector,
            lag_days=lag, estimated_cost_pct=0.05,
            research=research, cluster_count=cluster_count,
        )
```

With:

```python
        score: EntryScore = score_entry_with_debate(
            disc, committees=committees, sector=sector,
            lag_days=lag, estimated_cost_pct=0.05,
            research=research, cluster_count=cluster_count,
        )
```

In `_process_fundamental_candidate()` (around line 373), replace:

```python
        score: EntryScore = score_entry(
            disclosure=None,
            committees=[],
            sector=sector,
            lag_days=0,
            estimated_cost_pct=0.05,
            research=candidate.research,
            signal_type=signal_type,
            factor_score=candidate.composite_score,
            ticker=ticker,
        )
```

With:

```python
        score: EntryScore = score_entry_with_debate(
            disclosure=None,
            committees=[],
            sector=sector,
            lag_days=0,
            estimated_cost_pct=0.05,
            research=candidate.research,
            signal_type=signal_type,
            factor_score=candidate.composite_score,
            ticker=ticker,
        )
```

- [ ] **Step 3: Run full suite**

```bash
cd "trading bot" && python -m pytest tests/ -v
```

Expected: all green

- [ ] **Step 4: Commit**

```bash
cd "trading bot" && git add orchestration/main_loop.py
git commit -m "feat: wire score_entry_with_debate into orchestrator

Both congressional and fundamental signal paths now use the debate
wrapper. Low-conviction signals (< 7) are unaffected (1 call).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Final Check

- [ ] **Run the full test suite one more time**

```bash
cd "trading bot" && python -m pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected output ends with something like: `264+ passed in X.XXs`
