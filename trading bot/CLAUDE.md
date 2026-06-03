# CLAUDE.md

> **⚠️ IMPLEMENTATION PLAN & PHASE STATUS — READ FIRST**
> Full plan: `../TRADING_BOT_REVIEW_PLAN.md`. Phase 0 gate: **BLOCKED ON DATA** — real point-in-time
> data not yet acquired; all historical performance numbers are look-ahead biased until then.
> See `docs/PHASE0_FINDINGS.md` for gate decision rules and required datasets.
> Phases 1–3 are fully implemented; paper trading is operational.

This file gives Claude Code (claude.ai/code) project-specific guidance for this repository. Personal cross-project preferences (communication style, git habits, general working style) live in the global `~/.claude/CLAUDE.md` and apply on top of this.

**Purpose:** a regime-aware, paper-only systematic equity trading bot. It combines a fundamental factor screener (primary signal), congressional-disclosure trades (supplementary signal), an HMM market-regime overlay, and an independent risk manager. Built as research/paper-trading for a finance thesis. **Live (real-money) order execution is intentionally disabled — paper and simulated only.**

## Stack at a glance

- **Python** (3.11+; uses `from __future__ import annotations`, `zoneinfo`, `datetime.UTC`).
- **No web framework** for the bot itself; a separate **Streamlit** dashboard (`dashboard/app.py`) reads a JSON state file.
- **Data:** `yfinance` (prices, fundamentals, VIX), `requests`/`beautifulsoup4` (Capitol Trades scraper, universe lists).
- **Broker:** `alpaca-py` paper API (`bot/broker.py`) or a fully offline `SimulatedBroker` (`execution/paper_broker.py`).
- **AI:** Anthropic Claude (`claude-sonnet-4-6`) for entry/exit scoring with prompt caching (`bot/ai_analyst.py`); OpenAI for news sentiment in `bot/researcher.py`.
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

Secrets come from environment / `.env` (see `.env.example`): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `PROPUBLICA_API_KEY`, optional `ALERT_WEBHOOK_URL`, `DB_PATH`, `LOG_LEVEL`. `--simulated` mode runs without broker/LLM keys for the parts that don't call out.

## Architecture

`orchestration/main_loop.py` → `RegimeAwareOrchestrator` is the spine; everything else is a layer it wires together.

**Signal hierarchy (per morning pipeline):**
1. **Phase 1 — fundamental factor screener** (`screener/factor_scorer.py`): *primary* signal. Sector-neutral value/momentum/quality composite over the universe; top N go to AI scoring. **Momentum = 12-month return** (`mom_12m`); 1-month is display-only.
2. **Phase 2 — congressional disclosures** (`bot/scraper.py` → `bot/signal_engine.py`): *supplementary*. Capped at `_CONGRESSIONAL_MAX_PCT = 3%` NAV and `_CONGRESSIONAL_MAX_PER_DAY = 1`. A ticker in **both** screener and disclosures gets the `"both"` signal type, full conviction credit, and no congressional size cap.
3. **Phase 3 — inverse-ETF hedge** (`hedge/hedge_engine.py`): opens/closes inverse ETFs (SH/PSQ/RWM/…) when the regime is bear/crash.

**Decision flow for a candidate:** event-calendar gate (`utils/event_calendar.py`) → `gather_research` → AI entry score (`bot/ai_analyst.score_entry_with_debate`; bull/bear debate for conviction ≥ 7) → regime allocation scaling (`regime/allocation_engine.py`) → correlation filter (`risk/correlation.py`) → risk-manager veto (`risk/risk_manager.py`) → `Portfolio.open_position`.

**Regime engine (`regime/`):** `hmm_engine.HMMRegimeEngine` fits candidate HMMs (n=3–7) and selects by BIC; regimes are labeled by ascending mean return (crash…euphoria). Inference is **causal/forward-only** (`predict_proba_filtered`, frozen training scaler) — this is the part of the system most careful about look-ahead. `initialize_incremental` + `update_single` give O(K²) daily updates; `rolling_refit` every `refit_interval_days` (30).

**Risk manager (`risk/risk_manager.py`):** independent, hard veto. Circuit breakers: daily reduce (3%) / halt (4%) / deleverage (6%), weekly halt (8%), max-drawdown lockout (15%) which writes a `RISK_LOCKOUT` file requiring **manual** deletion. Also enforces per-position cap (8%), sector cap (30%), and ADV cap (5%).

**Portfolio (`bot/portfolio.py`):** position open/close/reduce, soft stop-loss / take-profit enforcement, **NAV-based sizing** (cash + mark-to-market), and `reconcile_with_broker` (removes ghost positions, alerts on untracked ones).

**Config (`system/config.py`):** one frozen `Settings` dataclass with nested typed configs (`RegimeConfig`, `RiskConfig`, `AllocationConfig`, `HedgeConfig`, `CorrelationConfig`, `ExecutionConfig`, `BacktestConfig`, …). Import the module-level `settings` singleton everywhere. All tunable parameters live here — do not hardcode constants in logic modules. `Settings.validate()` enforces circuit-breaker ordering.

**Backtesting (`backtesting/`):** `walk_forward.py` (rolling train/test, frozen scaler, forward-only classification), `simulation.py` (`simulate_portfolio`), `metrics.py`, `benchmarks.py`, `analysis.py`, `stress_test.py`.

**Persistence (`bot/db.py`):** SQLite tables — `disclosures`, `signals`, `fundamental_signals`, `positions`, `closed_positions`, `portfolio_log`, `regime_log`, `regime_transitions`, `risk_events`, `backtest_results`, `schema_version`. WAL mode; `foreign_keys=ON`; migrations in `_MIGRATIONS` keyed by `schema_version`. `realized_pnl` is net of both-side commissions.

**Monitoring (`monitoring/`):** structured JSON logging with an `EventType` enum (`emit_event`) and pluggable alert senders (`fire_alert`, webhook/log).

**Feature engineering (`features/feature_pipeline.py`):** `FeatureConfig` dataclass + causal feature computation (vol, trend, momentum, drawdown, VIX) consumed by the regime engine. All features strictly forward-only — no look-ahead.

**Market data (`market_data/market_feed.py`):** fetches daily SPY/VIX bars via yfinance for the regime engine. Separate from individual-stock data in `bot/researcher.py`.

**Performance tracking (`performance/tracker.py`):** `PerformanceTracker` reads live `trading.db` and computes the same metrics as `backtesting.metrics.compute_all` — enabling direct live vs backtest comparison.

## Key documents (`docs/`)

- `PHASE0_FINDINGS.md` — Phase 0 gate status (BLOCKED ON DATA); required datasets and pass/fail rules
- `DATA_SOURCES.md` — all external data sources, current status, and fallback behaviour
- `PIT_DATA_REQUIREMENTS.md` — schemas for point-in-time data needed to unblock Phase 0
- `CONGRESSIONAL_EDGE.md` — congressional trading edge analysis
- `HEDGE_ANALYSIS.md` — inverse-ETF hedge analysis

## Scheduler (Amsterdam time, NYSE-session guarded)

Defined in `RegimeAwareOrchestrator.start()`. Jobs run on a **single-thread executor** so the pipeline and exit review never touch the DB/portfolio concurrently.

- Mon 07:00 — `refresh_universe()`
- 13:00 — `run_screener_prefetch()` (pre-fetch fundamentals ~1h before pipeline)
- 14:00 — `run_morning_pipeline()` (Phase 1 + 2 + 3)
- 16:00 — `run_exit_review()`
- 15:45 / 17:00 / 20:00 — `run_intraday_check()` (stop-loss + circuit breakers; tighter misfire grace)
- 22:30 — `run_eod()`
- Fri 22:45 — `log_weekly_report()`

## Verifying changes

```bash
pytest                                 # ~488 tests; keep green (run from inside trading bot/)
pytest tests/test_simulation.py -q    # example: a single module
```

- Tests must run **offline** — mock yfinance / Alpaca / scraper / LLM calls (see `tests/conftest.py`). New code needs offline unit tests.
- Match existing style; do not reformat untouched code.
- Set `temperature=0` on any LLM call you add or touch (reproducibility).

## Known data-source caveats (important)

- **Capitol Trades is a JavaScript SPA.** `bot/scraper.py` tries the JSON API endpoint first (`_fetch_page_json`); HTML scraper is the fallback. If both fail, a `DEAD_FEED` alert fires and the congressional pipeline receives zero inputs for that run. `run_1year_backtest.py` reads a cached JSON snapshot (`capitol_trades_merged.json`, Oct 2025→May 2026 only). See `docs/DATA_SOURCES.md`.
- **ProPublica Congress API is discontinued.** `bot/committee.py` now uses the `unitedstates/congress-legislators` GitHub YAML files (no API key). A 30-day shelve disk cache insulates against transient GitHub outages.
- **`bot/universe.py` uses the *current* S&P 500 + Russell 1000.** Backtests over this set are survivorship-biased; the factor screener reads *current* `yfinance .info` fundamentals (look-ahead) and so is **not** historically reconstructable from yfinance. See `../TRADING_BOT_REVIEW_PLAN.md` Phase 0.
- **`yfinance` is a single point of failure** (prices, fundamentals, regime data); many call sites fall back to `0.0`/skip silently. Treat missing data as a first-class failure when you touch these paths.

## Gotchas

- **NAV vs cash sizing:** live `Portfolio.open_position` sizes off NAV, but `backtesting/simulation.py` currently sizes off *remaining cash* — they disagree (a Phase 1 fix in the plan).
- **Stops are soft/polled**, not resting broker orders — overnight and between-check gaps are unprotected (Phase 2 fix).
- **Position size is currently driven by the LLM's `conviction`/`position_pct`** — non-deterministic and unvalidated (Phase 1 replaces this with volatility targeting).
- The regime lock file (`RISK_LOCKOUT`) is **not** auto-cleared; trading stays halted until a human deletes it.
- Dates are ISO `YYYY-MM-DD` strings throughout; regime/DB joins assume this.

## Security & data

- API keys live in environment / `.env` only — never commit `.env`, `trading.db`, `regime_model.joblib`, the cached JSON/shelve data, or `dashboard_state.json`. Never log secrets.
