# Deep Research Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `bot/researcher.py` that pulls analyst-grade fundamental, valuation, momentum and news data for each ticker via FinceptTerminal's Python scripts, and injects that context into every Claude AI call.

**Architecture:** A new `ResearchReport` dataclass is assembled by `gather_research(ticker)` using FinceptTerminal's `YahooFinanceProvider` (imported via `sys.path` injection) plus raw `yfinance` calls for momentum and headlines. `score_entry()` and `review_exit()` in `ai_analyst.py` accept an optional `ResearchReport`; `scheduler.py` calls `gather_research()` before each AI scoring call and passes the result through.

**Tech Stack:** Python 3.11+, yfinance, FinceptTerminal analytics scripts (`equityInvestment.base.data_providers.YahooFinanceProvider`), pytest, pytest-mock.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `trading bot/bot/researcher.py` | **CREATE** | `ResearchReport` dataclass, `format_research_for_prompt()`, `gather_research()` |
| `trading bot/tests/test_researcher.py` | **CREATE** | Tests for all three functions above |
| `trading bot/bot/config.py` | **MODIFY** | Add `FINCEPT_SCRIPTS_PATH` env var |
| `trading bot/.env.example` | **MODIFY** | Document `FINCEPT_SCRIPTS_PATH` |
| `trading bot/bot/ai_analyst.py` | **MODIFY** | `score_entry()` + `review_exit()` accept optional `ResearchReport` |
| `trading bot/bot/scheduler.py` | **MODIFY** | Call `gather_research()` before each AI call; remove old headlines extraction |
| `trading bot/tests/test_ai_analyst.py` | **MODIFY** | Update `test_review_exit_returns_exit_decision` signature; add research tests |
| `trading bot/tests/test_scheduler.py` | **MODIFY** | Mock `gather_research` in existing tests; add new assertion test |

---

## Task 1: Add `FINCEPT_SCRIPTS_PATH` to config and `.env.example`

**Files:**
- Modify: `trading bot/bot/config.py`
- Modify: `trading bot/.env.example`

No test needed — this is a pure config read with a hardcoded default.

- [ ] **Step 1: Add env var to config.py**

Open `trading bot/bot/config.py`. Append after the last line:

```python
FINCEPT_SCRIPTS_PATH: str = os.environ.get(
    "FINCEPT_SCRIPTS_PATH",
    "/Users/thomasvromen/Documents/FinceptTerminal/fincept-qt/scripts/Analytics",
)
```

- [ ] **Step 2: Document in .env.example**

Open `trading bot/.env.example`. Append:

```
FINCEPT_SCRIPTS_PATH=/Users/thomasvromen/Documents/FinceptTerminal/fincept-qt/scripts/Analytics
```

- [ ] **Step 3: Commit**

```bash
cd "trading bot"
git add bot/config.py .env.example
git commit -m "feat: add FINCEPT_SCRIPTS_PATH config for deep research module"
```

---

## Task 2: Create `ResearchReport` dataclass and `format_research_for_prompt()`

**Files:**
- Create: `trading bot/bot/researcher.py`
- Create: `trading bot/tests/test_researcher.py`

These are pure Python — no FinceptTerminal or yfinance imports needed yet.

- [ ] **Step 1: Write the failing tests**

Create `trading bot/tests/test_researcher.py`:

```python
import pytest
from bot.researcher import ResearchReport, format_research_for_prompt


def _make_report(**overrides) -> ResearchReport:
    defaults = dict(
        ticker="AAPL", company_name="Apple Inc.", sector="Technology",
        market_cap=3_000_000_000_000,
        pe_trailing=28.5, pe_forward=24.0, pb_ratio=45.0, ps_ratio=7.5,
        peg_ratio=1.8, ev_ebitda=22.0,
        roe=1.60, roa=0.25, profit_margin=0.26, debt_to_equity=1.7,
        current_ratio=0.9, free_cash_flow=100_000_000_000,
        revenue_growth=0.05, earnings_growth=0.10,
        beta=1.2, week52_high=260.0, week52_low=165.0,
        momentum_1m=3.5, momentum_3m=12.0,
        analyst_target=230.0, analyst_rating="Buy",
        headlines=("Strong earnings", "New product launch"),
    )
    defaults.update(overrides)
    return ResearchReport(**defaults)


def test_format_includes_company_and_sector():
    text = format_research_for_prompt(_make_report())
    assert "Apple Inc." in text
    assert "Technology" in text


def test_format_includes_valuation_multiples():
    text = format_research_for_prompt(_make_report())
    assert "P/E" in text
    assert "EV/EBITDA" in text
    assert "28.5" in text


def test_format_none_fields_render_as_na():
    text = format_research_for_prompt(
        _make_report(pe_trailing=None, ev_ebitda=None, analyst_rating=None)
    )
    assert "n/a" in text


def test_format_none_fields_do_not_raise():
    # All optional fields None — should not crash
    format_research_for_prompt(
        _make_report(
            pe_trailing=None, pe_forward=None, pb_ratio=None, ps_ratio=None,
            peg_ratio=None, ev_ebitda=None, roe=None, roa=None,
            profit_margin=None, debt_to_equity=None, current_ratio=None,
            free_cash_flow=None, revenue_growth=None, earnings_growth=None,
            beta=None, week52_high=None, week52_low=None,
            momentum_1m=None, momentum_3m=None,
            analyst_target=None, analyst_rating=None,
        )
    )


def test_format_includes_headlines():
    text = format_research_for_prompt(
        _make_report(headlines=("Earnings beat", "New CEO appointed"))
    )
    assert "Earnings beat" in text
    assert "New CEO appointed" in text


def test_format_empty_headlines_shows_none_line():
    text = format_research_for_prompt(_make_report(headlines=()))
    assert "- None" in text


def test_format_includes_momentum_with_sign():
    text = format_research_for_prompt(_make_report(momentum_1m=3.5, momentum_3m=-2.1))
    assert "+3.5%" in text
    assert "-2.1%" in text


def test_format_wrapped_in_research_markers():
    text = format_research_for_prompt(_make_report())
    assert text.startswith("--- INDEPENDENT RESEARCH ---")
    assert text.endswith("---")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot"
pytest tests/test_researcher.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'bot.researcher'`

- [ ] **Step 3: Create `bot/researcher.py` with dataclass and format function**

Create `trading bot/bot/researcher.py`:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchReport:
    ticker: str
    company_name: str
    sector: str
    market_cap: float
    # Valuation multiples
    pe_trailing: float | None
    pe_forward: float | None
    pb_ratio: float | None
    ps_ratio: float | None
    peg_ratio: float | None
    ev_ebitda: float | None
    # Financial health
    roe: float | None
    roa: float | None
    profit_margin: float | None
    debt_to_equity: float | None
    current_ratio: float | None
    free_cash_flow: float | None
    revenue_growth: float | None
    earnings_growth: float | None
    # Market context
    beta: float | None
    week52_high: float | None
    week52_low: float | None
    momentum_1m: float | None
    momentum_3m: float | None
    # Analyst consensus
    analyst_target: float | None
    analyst_rating: str | None
    # News
    headlines: tuple[str, ...]


def _fmt(value: float | None, spec: str = ".2f", suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:{spec}}{suffix}"


def format_research_for_prompt(report: ResearchReport) -> str:
    mcap = report.market_cap / 1e9 if report.market_cap else None
    fcf = report.free_cash_flow / 1e9 if report.free_cash_flow else None

    mom_1m = f"{report.momentum_1m:+.1f}%" if report.momentum_1m is not None else "n/a"
    mom_3m = f"{report.momentum_3m:+.1f}%" if report.momentum_3m is not None else "n/a"
    rev_g = f"{report.revenue_growth * 100:+.1f}%" if report.revenue_growth is not None else "n/a"
    earn_g = f"{report.earnings_growth * 100:+.1f}%" if report.earnings_growth is not None else "n/a"
    roe_s = f"{report.roe * 100:.1f}%" if report.roe is not None else "n/a"
    margin_s = f"{report.profit_margin * 100:.1f}%" if report.profit_margin is not None else "n/a"

    headline_lines = "\n".join(f"- {h}" for h in report.headlines) or "- None"

    return (
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
        f"Target ${_fmt(report.analyst_target, '.2f')}\n"
        f"Recent headlines:\n{headline_lines}\n"
        "---"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "trading bot"
pytest tests/test_researcher.py -v
```

Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add bot/researcher.py tests/test_researcher.py
git commit -m "feat: add ResearchReport dataclass and format_research_for_prompt"
```

---

## Task 3: Implement `gather_research()`

**Files:**
- Modify: `trading bot/bot/researcher.py` — add `_setup_fincept_path()` and `gather_research()`
- Modify: `trading bot/tests/test_researcher.py` — add gather_research tests

- [ ] **Step 1: Write the failing tests**

Append to `trading bot/tests/test_researcher.py`:

```python
import sys
import pandas as pd


def _mock_fincept(mocker):
    """Patch sys.path setup and FinceptTerminal imports."""
    mocker.patch("bot.researcher._setup_fincept_path")

    mock_company = mocker.MagicMock()
    mock_company.name = "Exxon Mobil"
    mock_company.sector = "Energy"
    mock_company.market_cap = 5e11
    mock_company.financial_data = {
        "roe": 0.15, "roa": 0.08, "profit_margin": 0.10,
        "debt_to_equity": 0.3, "current_ratio": 1.2,
        "free_cash_flow": 2e10,
    }
    mock_company.market_data = {
        "pe_ratio": 12.0, "forward_pe": 10.0, "pb_ratio": 2.0,
        "ps_ratio": 1.5, "peg_ratio": 1.2, "beta": 0.9,
        "52_week_high": 120.0, "52_week_low": 85.0,
        "revenue_growth": 0.05, "earnings_growth": 0.08,
    }

    mock_provider_cls = mocker.MagicMock()
    mock_provider_cls.return_value.get_company_data.return_value = mock_company

    mock_dp_module = mocker.MagicMock()
    mock_dp_module.YahooFinanceProvider = mock_provider_cls

    mocker.patch.dict("sys.modules", {
        "equityInvestment": mocker.MagicMock(),
        "equityInvestment.base": mocker.MagicMock(),
        "equityInvestment.base.data_providers": mock_dp_module,
    })
    return mock_provider_cls


def _mock_yf_ticker(mocker, *, rating="buy", target=115.0, ev_ebitda=8.0,
                    news=None, prices=None):
    """Patch yfinance Ticker used inside gather_research."""
    if prices is None:
        prices = [100.0] * 63  # ~3 months of trading days
    if news is None:
        news = [
            {"content": {"title": "Strong earnings"}},
            {"content": {"title": "New contract won"}},
        ]
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = pd.DataFrame({"Close": prices})
    mock_ticker.info = {
        "recommendationKey": rating,
        "targetMeanPrice": target,
        "enterpriseToEbitda": ev_ebitda,
    }
    mock_ticker.news = news
    mocker.patch("bot.researcher.yf.Ticker", return_value=mock_ticker)
    return mock_ticker


def test_gather_research_returns_report(mocker):
    _mock_fincept(mocker)
    _mock_yf_ticker(mocker)

    from bot.researcher import gather_research
    report = gather_research("XOM")

    assert report is not None
    assert report.ticker == "XOM"
    assert report.company_name == "Exxon Mobil"
    assert report.sector == "Energy"
    assert report.pe_trailing == 12.0
    assert report.ev_ebitda == 8.0
    assert report.analyst_rating == "Buy"
    assert report.analyst_target == 115.0
    assert "Strong earnings" in report.headlines


def test_gather_research_normalises_strong_buy_to_buy(mocker):
    _mock_fincept(mocker)
    _mock_yf_ticker(mocker, rating="strong_buy")

    from bot.researcher import gather_research
    report = gather_research("XOM")
    assert report.analyst_rating == "Buy"


def test_gather_research_normalises_strong_sell_to_sell(mocker):
    _mock_fincept(mocker)
    _mock_yf_ticker(mocker, rating="strong_sell")

    from bot.researcher import gather_research
    report = gather_research("XOM")
    assert report.analyst_rating == "Sell"


def test_gather_research_normalises_unknown_rating_to_none(mocker):
    _mock_fincept(mocker)
    _mock_yf_ticker(mocker, rating="underperform")

    from bot.researcher import gather_research
    report = gather_research("XOM")
    assert report.analyst_rating is None


def test_gather_research_computes_momentum(mocker):
    _mock_fincept(mocker)
    # 63 prices: starts at 100, ends at 110 — 3m momentum ≈ +10%
    prices = [100.0] * 42 + [110.0] * 21
    _mock_yf_ticker(mocker, prices=prices)

    from bot.researcher import gather_research
    report = gather_research("XOM")
    assert report.momentum_3m is not None
    assert report.momentum_3m > 0


def test_gather_research_returns_none_on_provider_error(mocker):
    mocker.patch("bot.researcher._setup_fincept_path")
    mock_dp_module = mocker.MagicMock()
    mock_dp_module.YahooFinanceProvider.side_effect = Exception("Network error")
    mocker.patch.dict("sys.modules", {
        "equityInvestment": mocker.MagicMock(),
        "equityInvestment.base": mocker.MagicMock(),
        "equityInvestment.base.data_providers": mock_dp_module,
    })

    from bot.researcher import gather_research
    result = gather_research("BADTICKER")
    assert result is None


def test_gather_research_returns_none_on_yf_error(mocker):
    _mock_fincept(mocker)
    mocker.patch("bot.researcher.yf.Ticker", side_effect=Exception("yf down"))

    from bot.researcher import gather_research
    result = gather_research("XOM")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot"
pytest tests/test_researcher.py::test_gather_research_returns_report -v
```

Expected: `AttributeError` or `ImportError` — `gather_research` does not exist yet.

- [ ] **Step 3: Add `_setup_fincept_path()` and `gather_research()` to `bot/researcher.py`**

Append to `trading bot/bot/researcher.py` (after the `format_research_for_prompt` function):

```python
import sys
import os
import yfinance as yf


def _setup_fincept_path() -> None:
    from bot.config import FINCEPT_SCRIPTS_PATH
    if FINCEPT_SCRIPTS_PATH not in sys.path:
        sys.path.insert(0, FINCEPT_SCRIPTS_PATH)


_RATING_MAP: dict[str, str] = {
    "strong_buy": "Buy", "buy": "Buy",
    "hold": "Hold", "neutral": "Hold",
    "sell": "Sell", "strong_sell": "Sell",
}


def gather_research(ticker: str) -> ResearchReport | None:
    try:
        _setup_fincept_path()
        from equityInvestment.base.data_providers import YahooFinanceProvider

        company = YahooFinanceProvider().get_company_data(ticker)
        fd = company.financial_data
        md = company.market_data

        # Price momentum from 3-month history
        hist = yf.Ticker(ticker).history(period="3mo")
        momentum_1m = momentum_3m = None
        if not hist.empty and len(hist) >= 2:
            current = hist["Close"].iloc[-1]
            price_1m = hist["Close"].iloc[max(0, len(hist) - 21)]
            price_3m = hist["Close"].iloc[0]
            momentum_1m = (current / price_1m - 1) * 100
            momentum_3m = (current / price_3m - 1) * 100

        # Analyst consensus + EV/EBITDA from yfinance info
        info = yf.Ticker(ticker).info
        raw_rating = (info.get("recommendationKey") or "").lower()
        analyst_rating = _RATING_MAP.get(raw_rating)
        analyst_target = info.get("targetMeanPrice")
        ev_ebitda_raw = info.get("enterpriseToEbitda")

        # Headlines
        news_items = yf.Ticker(ticker).news[:8]
        headlines = tuple(
            item.get("content", {}).get("title", "")
            for item in news_items
            if item.get("content", {}).get("title")
        )

        def _f(val: object) -> float | None:
            return float(val) if val else None

        return ResearchReport(
            ticker=ticker.upper(),
            company_name=company.name,
            sector=company.sector,
            market_cap=company.market_cap,
            pe_trailing=_f(md.get("pe_ratio")),
            pe_forward=_f(md.get("forward_pe")),
            pb_ratio=_f(md.get("pb_ratio")),
            ps_ratio=_f(md.get("ps_ratio")),
            peg_ratio=_f(md.get("peg_ratio")),
            ev_ebitda=_f(ev_ebitda_raw),
            roe=_f(fd.get("roe")),
            roa=_f(fd.get("roa")),
            profit_margin=_f(fd.get("profit_margin")),
            debt_to_equity=_f(fd.get("debt_to_equity")),
            current_ratio=_f(fd.get("current_ratio")),
            free_cash_flow=_f(fd.get("free_cash_flow")),
            revenue_growth=_f(md.get("revenue_growth")),
            earnings_growth=_f(md.get("earnings_growth")),
            beta=_f(md.get("beta")),
            week52_high=_f(md.get("52_week_high")),
            week52_low=_f(md.get("52_week_low")),
            momentum_1m=momentum_1m,
            momentum_3m=momentum_3m,
            analyst_target=_f(analyst_target),
            analyst_rating=analyst_rating,
            headlines=headlines,
        )

    except Exception as exc:
        log.warning("gather_research(%s) failed — skipping research: %s", ticker, exc)
        return None
```

- [ ] **Step 4: Run all researcher tests**

```bash
cd "trading bot"
pytest tests/test_researcher.py -v
```

Expected: all tests pass (8 format tests + 7 gather_research tests = 15 total).

- [ ] **Step 5: Commit**

```bash
git add bot/researcher.py tests/test_researcher.py
git commit -m "feat: implement gather_research() with FinceptTerminal integration"
```

---

## Task 4: Update `score_entry()` to accept `ResearchReport`

**Files:**
- Modify: `trading bot/bot/ai_analyst.py`
- Modify: `trading bot/tests/test_ai_analyst.py`

- [ ] **Step 1: Write the failing test**

Append to `trading bot/tests/test_ai_analyst.py`:

```python
from bot.researcher import ResearchReport

def _make_research(**overrides) -> ResearchReport:
    defaults = dict(
        ticker="XOM", company_name="Exxon Mobil", sector="Energy",
        market_cap=5e11, pe_trailing=12.0, pe_forward=10.0, pb_ratio=2.0,
        ps_ratio=1.5, peg_ratio=1.2, ev_ebitda=8.0,
        roe=0.15, roa=0.08, profit_margin=0.10, debt_to_equity=0.3,
        current_ratio=1.2, free_cash_flow=2e10, revenue_growth=0.05,
        earnings_growth=0.08, beta=0.9, week52_high=120.0, week52_low=85.0,
        momentum_1m=2.0, momentum_3m=8.0, analyst_target=115.0,
        analyst_rating="Buy", headlines=("Dividend raised",),
    )
    defaults.update(overrides)
    return ResearchReport(**defaults)


def test_score_entry_with_research_injects_research_block(mocker):
    payload = json.dumps({"conviction": 8, "position_pct": 5.0,
                          "rationale": "Good", "entry": "buy", "risk_flags": []})
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}

    score_entry(disc, committees=["House Energy and Commerce"],
                sector="Energy", lag_days=2, estimated_cost_pct=0.05,
                research=_make_research())

    call_kwargs = mock_client.messages.create.call_args[1]
    user_content = call_kwargs["messages"][0]["content"]
    assert "INDEPENDENT RESEARCH" in user_content
    assert "Exxon Mobil" in user_content
    assert "Dividend raised" in user_content


def test_score_entry_without_research_omits_research_block(mocker):
    payload = json.dumps({"conviction": 7, "position_pct": 4.0,
                          "rationale": "Ok", "entry": "buy", "risk_flags": []})
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}

    score_entry(disc, committees=["House Energy and Commerce"],
                sector="Energy", lag_days=2, estimated_cost_pct=0.05)

    call_kwargs = mock_client.messages.create.call_args[1]
    user_content = call_kwargs["messages"][0]["content"]
    assert "INDEPENDENT RESEARCH" not in user_content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot"
pytest tests/test_ai_analyst.py::test_score_entry_with_research_injects_research_block -v
```

Expected: `TypeError: score_entry() got an unexpected keyword argument 'research'`

- [ ] **Step 3: Update `_ENTRY_SYSTEM` and `score_entry()` in `bot/ai_analyst.py`**

Replace the current `_ENTRY_SYSTEM` string with:

```python
_ENTRY_SYSTEM = """You are a quantitative analyst evaluating congressional stock trade signals.
Respond with ONLY valid JSON matching this exact schema:
{"conviction": <int 1-10>, "position_pct": <float>, "rationale": <str>, "entry": <"buy"|"skip">, "risk_flags": [<str>]}

Rules:
- conviction 1-4: entry="skip", position_pct=0
- conviction 5-6: position_pct 1.0-2.0
- conviction 7-8: position_pct 3.0-5.0
- conviction 9-10: position_pct 6.0-8.0
- Only set entry="buy" if expected return exceeds estimated_cost_pct by at least 2x
- Penalise conviction -2 if lag_days is 15-30
- Penalise conviction -3 and cap position_pct at 2.0 if lag_days is 31-45
- Raise conviction for larger transaction sizes or multiple members buying same stock
- If independent research is provided, weigh it alongside the congressional signal. Penalise conviction 1-2 points for a fundamentally weak or clearly overvalued company. Raise conviction 1-2 points for a financially healthy, undervalued company with positive momentum."""
```

Replace the `score_entry` function with:

```python
def score_entry(disclosure: dict, committees: list[str], sector: str,
                lag_days: int, estimated_cost_pct: float,
                research: "ResearchReport | None" = None) -> EntryScore:
    from bot.researcher import ResearchReport, format_research_for_prompt
    prompt = (
        f"Politician: {disclosure['politician']}\n"
        f"Ticker: {disclosure['ticker']} | Sector: {sector}\n"
        f"Transaction date: {disclosure['transaction_date']} | "
        f"Disclosure date: {disclosure['disclosure_date']}\n"
        f"Lag days: {lag_days}\n"
        f"Amount range: {disclosure['amount_range']}\n"
        f"Committees held: {', '.join(committees)}\n"
        f"Estimated round-trip cost: {estimated_cost_pct:.2f}% of position\n"
    )
    if research is not None:
        prompt += "\n" + format_research_for_prompt(research) + "\n"
    prompt += "Score this signal."
    client = _get_client()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[{"type": "text", "text": _ENTRY_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_entry_response(resp.content[0].text)
```

- [ ] **Step 4: Run all ai_analyst tests**

```bash
cd "trading bot"
pytest tests/test_ai_analyst.py -v
```

Expected: all tests pass (existing 5 + new 2 = 7 total).

- [ ] **Step 5: Commit**

```bash
git add bot/ai_analyst.py tests/test_ai_analyst.py
git commit -m "feat: score_entry() accepts optional ResearchReport for richer AI context"
```

---

## Task 5: Update `review_exit()` to accept `ResearchReport` (remove `recent_headlines`)

**Files:**
- Modify: `trading bot/bot/ai_analyst.py`
- Modify: `trading bot/tests/test_ai_analyst.py`

- [ ] **Step 1: Write the failing test**

Append to `trading bot/tests/test_ai_analyst.py`:

```python
def test_review_exit_with_research_injects_research_block(mocker):
    payload = json.dumps({"action": "hold", "rationale": "Fundamentals strong"})
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    review_exit("AAPL", 150.0, 160.0, 10, research=_make_research(
        ticker="AAPL", company_name="Apple Inc.", sector="Technology",
        headlines=("Record quarter",),
    ))

    call_kwargs = mock_client.messages.create.call_args[1]
    user_content = call_kwargs["messages"][0]["content"]
    assert "INDEPENDENT RESEARCH" in user_content
    assert "Record quarter" in user_content


def test_review_exit_without_research_still_works(mocker):
    payload = json.dumps({"action": "exit", "rationale": "Stop loss"})
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    result = review_exit("AAPL", 150.0, 125.0, 20)
    assert result.action == "exit"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot"
pytest tests/test_ai_analyst.py::test_review_exit_with_research_injects_research_block -v
```

Expected: `TypeError: review_exit() got an unexpected keyword argument 'research'`

- [ ] **Step 3: Replace `review_exit()` in `bot/ai_analyst.py`**

Replace the existing `review_exit` function with:

```python
def review_exit(ticker: str, entry_price: float, current_price: float,
                days_held: int, research: "ResearchReport | None" = None) -> ExitDecision:
    from bot.researcher import ResearchReport, format_research_for_prompt
    pnl_pct = (current_price - entry_price) / entry_price * 100
    prompt = (
        f"Ticker: {ticker}\n"
        f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
        f"P&L: {pnl_pct:+.1f}%\n"
        f"Days held: {days_held}\n"
    )
    if research is not None:
        prompt += "\n" + format_research_for_prompt(research) + "\n"
    prompt += "Hold, reduce, or exit?"
    client = _get_client()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=[{"type": "text", "text": _EXIT_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_exit_response(resp.content[0].text)
```

- [ ] **Step 4: Fix the existing `test_review_exit_returns_exit_decision` test**

The existing test passes `["Bad news"]` as a positional argument (the old `recent_headlines` param). Update it to use the new signature:

```python
def test_review_exit_returns_exit_decision(mocker):
    payload = json.dumps({"action": "exit", "rationale": "Stop loss near"})
    _mock_claude(mocker, payload)
    result = review_exit("AAPL", 150.0, 125.0, 20)   # no research arg
    assert isinstance(result, ExitDecision)
    assert result.action == "exit"
```

- [ ] **Step 5: Run all ai_analyst tests**

```bash
cd "trading bot"
pytest tests/test_ai_analyst.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 6: Commit**

```bash
git add bot/ai_analyst.py tests/test_ai_analyst.py
git commit -m "feat: review_exit() replaces headlines param with ResearchReport"
```

---

## Task 6: Update `scheduler.py` to call `gather_research()`

**Files:**
- Modify: `trading bot/bot/scheduler.py`
- Modify: `trading bot/tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

Append to `trading bot/tests/test_scheduler.py`:

```python
def test_morning_calls_gather_research_per_qualified_signal(mocker, db):
    disc = {
        "id": "x1", "politician": "Jane Doe", "ticker": "XOM",
        "transaction_type": "purchase",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["Energy"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Energy")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=8, position_pct=5.0, rationale="Good", entry="buy", risk_flags=()
    ))
    mocker.patch("bot.scheduler.insert_signal", return_value=1)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 100.0}
    mock_research = mocker.patch("bot.scheduler.gather_research", return_value=None)
    portfolio = _make_portfolio(mocker)

    run_morning_pipeline(portfolio)

    mock_research.assert_called_once_with("XOM")


def test_exit_review_calls_gather_research(mocker, db):
    db.insert_disclosures([{
        "id": "pos1", "politician": "Jane", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }])
    sid = db.insert_signal("pos1", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 150.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 155.0}
    mocker.patch("bot.scheduler.review_exit", return_value=ExitDecision("hold", "Ok"))
    mock_research = mocker.patch("bot.scheduler.gather_research", return_value=None)
    portfolio = _make_portfolio(mocker)

    run_exit_review(portfolio)

    mock_research.assert_called_once_with("AAPL")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot"
pytest tests/test_scheduler.py::test_morning_calls_gather_research_per_qualified_signal -v
```

Expected: `AttributeError` — `gather_research` not imported in scheduler yet.

- [ ] **Step 3: Update imports in `bot/scheduler.py`**

Add to the existing imports at the top of `trading bot/bot/scheduler.py`:

```python
from bot.researcher import gather_research
```

- [ ] **Step 4: Update `run_morning_pipeline()` in `bot/scheduler.py`**

Inside the `for disc in qualified:` loop, replace:

```python
            score: EntryScore = score_entry(
                disc, committees=committees, sector=sector,
                lag_days=lag, estimated_cost_pct=_ESTIMATED_COST_PCT,
            )
```

with:

```python
            research = gather_research(disc["ticker"])
            score: EntryScore = score_entry(
                disc, committees=committees, sector=sector,
                lag_days=lag, estimated_cost_pct=_ESTIMATED_COST_PCT,
                research=research,
            )
```

- [ ] **Step 5: Update `run_exit_review()` in `bot/scheduler.py`**

Inside the `for pos in get_open_positions():` loop, replace:

```python
            headlines = [h.get("content", {}).get("title", "") for h in yf.Ticker(pos["ticker"]).news[:5]]
            decision = review_exit(
                pos["ticker"], pos["entry_price"], current_price, days_held, headlines
            )
```

with:

```python
            research = gather_research(pos["ticker"])
            decision = review_exit(
                pos["ticker"], pos["entry_price"], current_price, days_held,
                research=research,
            )
```

- [ ] **Step 6: Fix the three existing scheduler tests that need `gather_research` mocked**

In `trading bot/tests/test_scheduler.py`, add `mocker.patch("bot.scheduler.gather_research", return_value=None)` to these three existing tests:

**`test_morning_opens_on_buy_signal`** — add after the `mocker.patch("bot.scheduler.compute_lag_days", ...)` line:
```python
    mocker.patch("bot.scheduler.gather_research", return_value=None)
```

**`test_exit_review_closes_on_exit`** — remove the line:
```python
    mocker.patch("bot.scheduler.yf.Ticker").return_value.news = []
```
and add:
```python
    mocker.patch("bot.scheduler.gather_research", return_value=None)
```

**`test_exit_review_reduces_on_reduce`** (if it exists — check the file) — same treatment as `test_exit_review_closes_on_exit`.

- [ ] **Step 7: Run all scheduler tests**

```bash
cd "trading bot"
pytest tests/test_scheduler.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Run the full test suite**

```bash
cd "trading bot"
pytest -v
```

Expected: all tests pass (55 existing + new researcher and ai_analyst tests).

- [ ] **Step 9: Commit**

```bash
git add bot/scheduler.py tests/test_scheduler.py
git commit -m "feat: call gather_research() before every AI scoring call in scheduler"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| `bot/researcher.py` with `ResearchReport` | Task 2 |
| `gather_research()` via FinceptTerminal `sys.path` injection | Task 3 |
| `format_research_for_prompt()` | Task 2 |
| `FINCEPT_SCRIPTS_PATH` in config + `.env.example` | Task 1 |
| `score_entry()` accepts optional `ResearchReport` | Task 4 |
| `review_exit()` replaces `recent_headlines` with `ResearchReport` | Task 5 |
| `scheduler.py` calls `gather_research()` before each AI call | Task 6 |
| `tests/test_researcher.py` with mocked provider | Tasks 2 & 3 |
| Graceful degradation: `None` on any failure | Task 3 (`test_gather_research_returns_none_on_*`) |
| Rating normalisation: `strong_buy` → `Buy` etc. | Task 3 |
| `ev_ebitda` from `yf.info["enterpriseToEbitda"]` | Task 3 |
| All 55 existing tests continue to pass | Task 6, Step 8 |

All requirements covered. No gaps.
