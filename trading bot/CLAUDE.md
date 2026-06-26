# CLAUDE.md

> **⚠️ PHASE STATUS — READ FIRST**
> Phase 0 gate: **BLOCKED ON DATA** — real point-in-time data not yet acquired; all historical
> performance numbers are look-ahead biased until then.
> See `docs/PHASE0_FINDINGS.md` for gate decision rules and required datasets.
> Phases 1–3 are fully implemented; paper trading is operational.
> Hardening plans A–E are complete; the technical-analysis gate (config-gated, default
> off) landed 2026-06-17. A code-review pass on that gate (stop-geometry, fail-open
> fallback, data-completeness check, both-signal-type resolution, indicator edge
> cases, sizing-block dedup) landed 2026-06-19. OpenAI (`gpt-5.4`) became the default
> scoring provider 2026-06-22 (Anthropic Claude still available via
> `LLM_PROVIDER=anthropic`). A follow-up independent re-audit on 2026-06-22 found several
> "fixed" issues from the above passes were fix-in-name-only (sector/ADV caps, stop-fill
> booking, feature padding, and others) — see `TRADING_BOT_REVIEW_2026-06-22.md` at repo
> root for the full list. The remediation pass for that audit landed the same day: sector
> cap and ADV gate now real vetoes; ghost-position stop fills are booked into
> `closed_positions`; fill-poll timeout and failed stop-replace now alert; feature padding
> aligns by scaler column name; `GaussianHMM.fit()` runs best-of-`n_restarts` EM;
> `classify()`'s stability tracking is no longer silently skippable; ATR/RSI/rs_line_slope
> indicator edge cases fixed; `_llm_call` raises a retryable `ValueError` on missing/empty
> response content and retries once without `temperature`/`seed` on a reasoning-tier-model
> 400; Russell 1000 ticker fetch now normalizes class-share tickers too.
> A fresh full re-review on 2026-06-23 re-verified all 18 items above as genuinely fixed,
> then found and fixed 5 new pre-existing Critical bugs the same day: trail-stop cancel now
> targets the old stop's specific order id (both brokers were cancelling/leaving the wrong
> stop); `AlpacaBroker`'s stop cancel/lookup compared `str(enum)` against real `alpaca-py`
> enums and never matched anything; the weekly-loss circuit breaker could suppress same-week
> `DELEVERAGE` detection (now tracked as an independent flag, see `RiskManager.state`);
> `GaussianHMM.fit()`'s Baum-Welch E-step added `transmat_` in linear scale instead of log
> scale, degrading regime persistence on realistic overlapping data; the HTML scraper
> fallback never normalized `transaction_type` to `buy`/`sell`, silently losing all
> congressional signal during a JSON-API outage. See `TRADING_BOT_REVIEW_2026-06-23.md` at
> repo root for full findings.
> 2026-06-24: fixed Phase 1 momentum — `screener/factor_scorer.py` filled missing/NaN
> `mom_12m` with `0` (worst percentile); now imputed at the neutral midpoint (`fillna(0.5)`).
> 2026-06-26: full remediation pass — all 7 High, 12 Medium, and 13 Low findings from the
> 2026-06-23 review are now fixed. Test count: **755** (was 721; +34 new tests). Key fixes:
> `close/reduce_position` treats CANCELLED/SUBMITTED as no-ops (like REJECTED); partial fills
> use `order.filled_qty`; `position_pct` recomputed from actual fill; `parse_*_response`
> KeyError/TypeError re-raised as `ValueError` so `_call_with_retry` retries them; external
> headlines wrapped in `<external_data>` XML for prompt-injection defence; Anthropic response
> parser finds first text block instead of assuming `content[0]`; textbook Sortino formula
> (`sqrt(mean(min(r,0)²))` over all obs); `random_allocation` commission no longer
> double-deducted; `tracker.py` returns commission-net; `mom_12m` uses 252-bar lookback;
> attribution sample gate raised to 30; `vol_z` uses `cfg.vol_window`; committee name-match
> strips suffixes; DB migrations use `PRAGMA table_info`; DELEVERAGE circuit-breaker tests
> added; broker tests use real alpaca enums; regime causal-test made non-vacuous. No open
> findings remain from any prior review.

This file gives Claude Code (claude.ai/code) project-specific guidance for this repository. Personal cross-project preferences (communication style, git habits, general working style) live in the global `~/.claude/CLAUDE.md` and apply on top of this.

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

## Architecture

`orchestration/main_loop.py` → `RegimeAwareOrchestrator` is the spine; everything else is a layer it wires together.

**Signal hierarchy (per morning pipeline):**
1. **Phase 1 — fundamental factor screener** (`screener/factor_scorer.py`): *primary* signal. Sector-neutral value/momentum/quality composite over the universe; top N go to AI scoring. **Momentum = 12-month return** (`mom_12m`); 1-month is display-only.
2. **Phase 2 — congressional disclosures** (`bot/scraper.py` → `bot/signal_engine.py`): *supplementary*. Capped at `_CONGRESSIONAL_MAX_PCT = 3%` NAV and `_CONGRESSIONAL_MAX_PER_DAY = 1`. A ticker in **both** screener and disclosures gets the `"both"` signal type, full conviction credit, and no congressional size cap.
3. **Phase 3 — inverse-ETF hedge** (`hedge/hedge_engine.py`): opens/closes inverse ETFs (SH/PSQ/RWM/…) when the regime is bear/crash.

**Decision flow for a candidate:** event-calendar gate (`utils/event_calendar.py`) → `gather_research` → AI entry score (`bot/ai_analyst.score_entry_with_debate`; bull/bear debate for conviction ≥ 7) → regime allocation scaling (`regime/allocation_engine.py`) → correlation filter (`risk/correlation.py`) → risk-manager veto (`risk/risk_manager.py`) → `Portfolio.open_position`.

**Regime engine (`regime/`):** `hmm_engine.HMMRegimeEngine` fits candidate HMMs (n=3–7) and selects by BIC; regimes are labeled by ascending mean return (crash…euphoria). Inference is **causal/forward-only** (`predict_proba_filtered`, frozen training scaler) — this is the part of the system most careful about look-ahead. `initialize_incremental` + `update_single` give O(K²) daily updates; `rolling_refit` every `refit_interval_days` (30). `gaussian_hmm.GaussianHMM.fit()` runs `n_restarts` (`RegimeConfig.n_restarts`, default 5) random k-means-style inits + full EM each, keeping the best by final log-likelihood — guards against a single bad local optimum destabilizing regime labels across rolling refits. Each restart uses a derived seed (`random_state + i`), so the result stays reproducible for a fixed `random_state`. Tests use a smaller `n_restarts` (mock config) to keep the function-scoped `fitted_engine` fixture fast.

**Risk manager (`risk/risk_manager.py`):** independent, hard veto. Circuit breakers: daily reduce (3%) / halt (4%) / deleverage (6%), weekly halt (8%), max-drawdown lockout (15%) which writes a `RISK_LOCKOUT` file requiring **manual** deletion. Also enforces per-position cap (8%), sector cap (30%), and ADV cap (5%).

**Portfolio (`bot/portfolio.py`):** position open/close/reduce, soft stop-loss / take-profit enforcement, **NAV-based sizing** (cash + mark-to-market), and `reconcile_with_broker` (books ghost positions with a matching broker fill into `closed_positions`, otherwise deletes-and-alerts; alerts on untracked ones).

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
pytest                                 # 755 tests; keep green (run from inside trading bot/)
pytest tests/test_simulation.py -q    # example: a single module
```

- Tests must run **offline** — mock yfinance / Alpaca / scraper / LLM calls (see `tests/conftest.py`). New code needs offline unit tests.
- Match existing style; do not reformat untouched code.
- Set `temperature=0` on any LLM call you add or touch (reproducibility).

## Known data-source caveats (important)

- **Capitol Trades is a JavaScript SPA.** `bot/scraper.py` tries the JSON API endpoint first (`_fetch_page_json`); HTML scraper is the fallback. If both fail, a `DEAD_FEED` alert fires and the congressional pipeline receives zero inputs for that run. `run_1year_backtest.py` reads a cached JSON snapshot (`capitol_trades_merged.json`, Oct 2025→May 2026 only). See `docs/DATA_SOURCES.md`.
- **ProPublica Congress API is discontinued.** `bot/committee.py` now uses the `unitedstates/congress-legislators` GitHub YAML files (no API key). A 30-day shelve disk cache insulates against transient GitHub outages.
- **`bot/universe.py` uses the *current* S&P 500 + Russell 1000.** Backtests over this set are survivorship-biased; the factor screener reads *current* `yfinance .info` fundamentals (look-ahead) and so is **not** historically reconstructable from yfinance. See `docs/PHASE0_FINDINGS.md`.
- **`yfinance` is a single point of failure** (prices, fundamentals, regime data); many call sites fall back to `0.0`/skip silently. Treat missing data as a first-class failure when you touch these paths.
- **`WalkForwardResult.pooled_attribution` (HAC/Newey-West) is still built from overlapping rolling windows.** The Newey-West standard errors correct for autocorrelation *within* the pooled return series, but the pooled sample itself is assembled from walk-forward windows that share dates by construction (`step_months < test_months`). Read `pooled_attribution["alpha_tstat"]`/`alpha_se` as indicative of a strategy-level alpha estimate, not a formal i.i.d. hypothesis test, until non-overlapping OOS windows are used.

## Gotchas

- **`dashboard/app.py` routes `DB_PATH`/`STATE_PATH` through `system.paths.resolve()`,** same as `system/config.py` — `streamlit run dashboard/app.py` from any cwd reads/writes the same `trading.db`/`dashboard_state.json` under `PROJECT_ROOT`. An absolute `DB_PATH`/`DASHBOARD_STATE` env override still passes through unchanged; only the relative default (or a relative env override) gets anchored.
- **Paper-only guard is defense-in-depth, not single-point:** `run_bot.py`'s `_make_broker` refuses to return a non-paper `AlpacaBroker`, and `RegimeAwareOrchestrator.initialize()` independently re-checks `broker.is_paper` before doing any setup work, raising `RuntimeError` if false. Both checks must stay in sync — the orchestrator must never trust a caller blindly.
- **NAV-based sizing everywhere:** live `Portfolio.open_position`, `backtesting/simulation.py`, the walk-forward and PIT runners all size off NAV via `risk.position_sizing.vol_target_size_pct` (deterministic ATR/vol targeting). Position size is **not** LLM-driven; the LLM only gates buy/skip + a bounded conviction tilt. `per_trade_risk_pct` (in `SizingConfig`) is the gross-exposure knob.
- **MA50/MA200 conviction modifier:** `_ma_conviction_delta()` in `orchestration/main_loop.py` adjusts LLM conviction by −2 to +1 based on price vs 50- and 200-day SMAs (golden cross: +1; price between MAs: 0 or −1; below MA200: −2). Applied to both `_process_signal` and `_process_fundamental_candidate` after AI scoring, before `apply_conviction_tilt`. Requires 200 bars; history is fetched with `period="1y"`. Congressional-only entries are still capped at `_CONGRESSIONAL_MAX_PCT = 3%`.
- **Stops are resting broker orders (Alpaca) plus a polled backstop.** `enforce_stop_losses` trails the resting stop up (place-new-THEN-cancel-old, so no gap in coverage) and only touches positions in its source scope. Resting stops are cancelled on close/reduce. `SimulatedBroker` only enforces stops via the poll.
- **Technical-analysis gate (config-gated, default off):** `SizingConfig.enable_technical_gate`
  (default `False`) inserts a deterministic indicator pipeline (`technical/indicators.py`,
  `TechnicalSnapshot`/`compute_snapshot`) plus one extra Claude call
  (`bot/ai_analyst.score_technical`) after the existing AI entry score, in both
  `_process_signal` and `_process_fundamental_candidate`. When off, behavior is
  byte-for-byte identical to before (the `hist` fetch widened from `period="1y"` to
  `period="2y"` is the only universal change, reused by both the existing ATR/MA-delta
  code and the new gate). When on: a `"skip"` or `reward_risk < SizingConfig.min_reward_risk`
  (default 2.0) rejects the candidate; a `"buy"` switches sizing from `vol_target_size_pct`
  to `risk.position_sizing.structure_stop_size_pct` (risk-budget ÷ stop-distance, using the
  model's `invalidation_price`) and passes a per-position `initial_stop_pct` through to
  `Portfolio.open_position`. Technical conviction never blends into `EntryScore.conviction`
  or `ma_delta` — it only gates pass/fail and drives sizing/stop inputs, kept separate for
  auditability.
- **`positions.stop_pct` column (schema v5):** every position now carries its own stop
  width (`NOT NULL DEFAULT 15.0`), set at open time by `Portfolio.open_position`'s
  `initial_stop_pct` param (falls back to `RiskConfig.trailing_stop_pct` when not given —
  today's default path). `enforce_stop_losses()` with **no** explicit `stop_loss_pct`
  override now reads each position's own stored `stop_pct`, not the live
  `RiskConfig.trailing_stop_pct` — a position's stop width is fixed at entry, not
  retroactively changed by later config edits. An explicit `stop_loss_pct` override (used
  by hedge-scoped polling) still applies uniformly and ignores the per-position value,
  exactly as before.
- **`technical/` package:** hand-rolled indicators only (no TA-Lib/pandas-ta) —
  `technical/indicators.py` (pure functions + `TechnicalSnapshot`/`compute_snapshot`) and
  `technical/sector_map.py` (GICS sector string → sector ETF ticker, used for
  relative-strength vs. sector; unmapped sectors are treated as neutral, not an error).
- **Rejected sells are no-ops at the DB layer:** `close_position`/`reduce_position` book nothing and mutate nothing on a REJECTED order — they alert and leave the position for the next reconcile/poll.
- The regime lock file (`RISK_LOCKOUT`) is **not** auto-cleared; trading stays halted until a human deletes it.
- Dates are ISO `YYYY-MM-DD` strings throughout; regime/DB joins assume this.

## Security & data

- API keys live in environment / `.env` only — never commit `.env`, `trading.db`, `regime_model.joblib`, the cached JSON/shelve data, or `dashboard_state.json`. Never log secrets.
