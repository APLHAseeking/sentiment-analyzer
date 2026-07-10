# CLAUDE.md

> **⚠️ PHASE STATUS — READ FIRST** (current status only; full review/change history lives in
> `docs/CLAUDE-REFERENCE.md#history` — append new entries THERE, keep this banner short)
> Phase 0 gate: **BLOCKED ON DATA** — real point-in-time data not yet acquired; all historical
> performance numbers are look-ahead biased until then. See `docs/PHASE0_FINDINGS.md` for gate
> decision rules and required datasets.
> Phases 1–3 fully implemented; paper trading operational; live (paper-money) Alpaca launch
> started 2026-07-06. First live run (2026-07-06) hit and fixed a Critical bug in
> `_llm_call`'s OpenAI retry path. 2026-07-07: full review remediation (8 findings, incl.
> restart-safe circuit-breaker baselines + full-day insider feed) and new zero-LLM-cost
> signals (XBRL SUE/accruals/net-payout, short-interest screen). Later same day: first two
> "opened" trades (CF, VTRS) turned out to be **phantom positions** — `open_position` booked
> them from an unconfirmed (fill-poll-timeout) order status, but neither ever actually filled
> at Alpaca; a restart's reconcile caught them as ghosts. Root-caused to 4 bugs, all fixed:
> (1) `open_position` now requires `OrderStatus.FILLED` before booking, cancelling the
> dangling order otherwise; (2) `place_stop_order` always sent GTC, which Alpaca rejects for
> fractional qty; (3) SEC EDGAR daily-index fetch had no delay between consecutive misses,
> bursting into 403s; (4) the Capitol Trades JSON path treated 429 (rate limit) the same as a
> genuine 404 and gave up instead of backing off. See `docs/CLAUDE-REFERENCE.md#history` for
> detail; deferred edge ideas live in `docs/EDGE_BACKLOG.md`. 2026-07-09: the underlying
> cause of the CF/VTRS fill-poll timeout — `AlpacaBroker._poll_order_fill` only allowed
> ~0.4s (3 attempts × 0.2s) to confirm a fill — is fixed; widened to ~14s (15 × 1s). Later
> same day: entry-scan pipeline now runs twice daily (13:00/14:00 and 17:00/18:00 CEST)
> instead of once, per user request to react faster in volatile markets — position sizing
> multipliers unchanged. 2026-07-10: the in-memory scheduler was silently losing entire
> trading days across process restarts (zero signals generated 07-07 through 07-10) —
> fixed with a DB-backed catch-up-on-restart check, a Russell-1000-fetch 503 bug, an
> unchecked initial-stop-placement gap, a loosened AI entry hurdle (5x/1.5% → 3x/1.0%,
> now logged via `EntryScore.expected_return_pct`), and macOS launchd auto-restart
> supervision. See `docs/CLAUDE-REFERENCE.md#history` for detail. Test count: **875**
> (full suite green; one pre-existing unrelated date-dependent test failure, not fixed —
> see history).

**Purpose:** a regime-aware, paper-only systematic equity trading bot. It combines a fundamental factor screener (primary signal), congressional-disclosure trades (supplementary signal), an HMM market-regime overlay, and an independent risk manager. Built as research/paper-trading for a finance thesis. **Live (real-money) order execution is intentionally disabled — paper and simulated only.**

## Stack at a glance

- **Python** (3.11+; uses `from __future__ import annotations`, `zoneinfo`, `datetime.UTC`).
- **No web framework** for the bot itself; a separate **Streamlit** dashboard (`dashboard/app.py`) reads a JSON state file.
- **Data:** `yfinance` (prices, fundamentals, VIX), `requests`/`beautifulsoup4` (Capitol Trades scraper, universe lists).
- **Broker:** `alpaca-py` paper API (`bot/broker.py`) or a fully offline `SimulatedBroker` (`execution/paper_broker.py`).
- **AI:** OpenAI (`gpt-5.4`) is the default provider for entry/exit/technical scoring (`bot/ai_analyst.py`); switch back to Anthropic Claude (`claude-sonnet-4-6`, with prompt caching) via `Settings.llm_provider = "anthropic"` (env: `LLM_PROVIDER=anthropic`). OpenAI is also used separately for news sentiment in `bot/researcher.py` (`gpt-4o-mini`, unrelated to this switch).
- **Regime model:** pure-NumPy Gaussian HMM (`regime/gaussian_hmm.py`) + `scikit-learn` `StandardScaler`, persisted with `joblib`. No `hmmlearn`.
- **Scheduling:** `APScheduler` (`BlockingScheduler`) with an `exchange_calendars` NYSE guard, Amsterdam timezone.
- **Persistence:** SQLite (`trading.db`), WAL mode, versioned migrations.
- Dependencies in `requirements.txt`. Keep this shape; do not add a frontend framework, ORM, or build tooling, and do not add a headless browser or `statsmodels`/`hmmlearn` without flagging it first.

## Running

```bash
pip install -r requirements.txt

python run_bot.py              # live paper mode (Alpaca paper API; needs ALPACA_* keys)
python run_bot.py --simulated  # fully offline SimulatedBroker (no broker keys needed)
python run_bot.py --backtest   # walk-forward backtest from DB signals, then exit
python run_bot.py --test-alerts # fire one test alert via the configured sender, then exit

python run_1year_backtest.py   # focused congressional-only backtest off cached JSON (see caveats)
python run_backtest_nokey.py   # backtest variant requiring no API keys

streamlit run dashboard/app.py # dashboard (reads dashboard_state.json)
```

First run creates `trading.db` (`bot.db.init_db`) and fetches the universe. The HMM model is cached to `regime_model.joblib` and reloaded on restart.

Secrets come from environment / `.env` (see `.env.example`): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `PROPUBLICA_API_KEY`, optional `ALERT_WEBHOOK_URL`, `DB_PATH`, `LOG_LEVEL`, `LLM_PROVIDER` (`openai` default, or `anthropic`). `OPENAI_API_KEY` is required by default now (entry/exit/technical scoring); `ANTHROPIC_API_KEY` is only required if `LLM_PROVIDER=anthropic`. `--simulated` mode runs without broker/LLM keys for the parts that don't call out.

## Verifying changes

```bash
pytest                                 # 875 tests; keep green (run from inside trading bot/)
python backtesting/backtest_price_factors.py  # PIT backtest of low-vol/BAB + residual momentum
pytest tests/test_simulation.py -q    # example: a single module
```

- Tests must run **offline** — mock yfinance / Alpaca / scraper / LLM calls (see `tests/conftest.py`). New code needs offline unit tests.
- Set `temperature=0` on any LLM call you add or touch (reproducibility).

## Reference (on-demand — Read the anchor before touching the named area)

Before changing orchestration, signal flow, regime/risk/portfolio/config/DB semantics, or any subsystem wiring -> Read docs/CLAUDE-REFERENCE.md#architecture
Before editing stops, sizing, the technical gate, the paper-only guard, or the dashboard paths -> Read docs/CLAUDE-REFERENCE.md#gotchas
Before touching scraper, committee, universe, yfinance paths, or interpreting backtest attribution -> Read docs/CLAUDE-REFERENCE.md#data-caveats
Before changing scheduled jobs or their times -> Read docs/CLAUDE-REFERENCE.md#scheduler
Looking for the analysis docs (Phase 0 gate, data sources, backtests, hedge, congressional edge) -> Read docs/CLAUDE-REFERENCE.md#key-documents
After completing a review/remediation/strategy change worth recording -> append it to docs/CLAUDE-REFERENCE.md#history and update this banner's status line

## Security & data

- API keys live in environment / `.env` only — never commit `.env`, `trading.db`, `regime_model.joblib`, the cached JSON/shelve data, or `dashboard_state.json`. Never log secrets.
