<!-- Reference material moved out of trading bot/CLAUDE.md on 2026-07-06 to keep the always-loaded file lean. Content transported verbatim; append history entries under #history. Pointers live in trading bot/CLAUDE.md. -->

<a id="architecture"></a>
## Architecture

`orchestration/main_loop.py` → `RegimeAwareOrchestrator` is the spine; everything else is a layer it wires together.

**Signal hierarchy (per morning pipeline):**
1. **Phase 1 — fundamental factor screener** (`screener/factor_scorer.py`): *primary* signal. Sector-neutral composite of **five** sleeves — value, momentum, quality, **low-vol/BAB**, and **short-term reversal (mean reversion)** — over the universe; top N go to AI scoring. **Momentum = 12-month return** (`mom_12m`) plus 6-1m, 52w-high ratio, and **residual (idiosyncratic) momentum** (`resid_mom`); 1-month is display-only **but feeds the reversal sleeve** (`reversal = -mom_1m`, most-oversold ranks highest). Within the momentum sleeve the sub-signals are a **weighted blend** (`_MOMENTUM_WEIGHTS`, renormalised over present signals): residual momentum carries the **largest weight (0.40)** — it was the strongest single sleeve in the backtest — followed by `mom_12m` 0.25, **`sue` 0.15 (XBRL earnings surprise / PEAD, deliberately small pending a PIT backtest — see `docs/EDGE_BACKLOG.md`)**, `mom_6m` 0.12, `high52_ratio` 0.08. **SEC XBRL signals** (`screener/xbrl_fundamentals.py`, free frames API, ~20 cacheable requests/screen, zero LLM cost): `sue` (momentum), inverted accruals (quality), net payout yield (value); missing data ranks neutral and an SEC outage degrades to pre-XBRL behaviour. A **short-interest negative screen** excludes names above `UniverseConfig.max_short_pct_float` (default 20% of float, 0 disables) from the composite outright. The **low-vol sleeve** (`vol_inv`, `beta_inv`) ranks low realized-vol / low-beta names higher. `_REGIME_WEIGHTS` are **5-tuples** (value, momentum, quality, low_vol, reversal) summing to 1.0, **tuned from the PIT backtest** (`docs/FACTOR_BACKTEST_2026-06-28.md`): momentum heavy in trends (residual momentum was the strongest sleeve), low-vol up in bear/crash (defensive), reversal up only in neutral/range-bound + bear and small elsewhere (naive reversal was weak standalone — kept small and blended). Low-vol/BAB, residual momentum, and reversal are price-derived (one shared SPY download), so they add **zero LLM cost**.
2. **Phase 2 — congressional disclosures** (`bot/scraper.py` → `bot/signal_engine.py`): *supplementary*. Capped at `_CONGRESSIONAL_MAX_PCT = 3%` NAV and `_CONGRESSIONAL_MAX_PER_DAY = 1`. A ticker in **both** screener and disclosures gets the `"both"` signal type, full conviction credit, and no congressional size cap.
3. **Phase 2.5 — insider buys** (`bot/insider.py` → `bot/insider_signal.py`): *supplementary*. SEC EDGAR Form 4 open-market purchases (transaction code `P`). **Primary source is the EDGAR daily form index** (`form.idx`, full-day coverage, up to 2 newest published indexes); the getcurrent Atom feed (most-recent ~100 entries only) is the fallback. Entries dedup by accession (EDGAR lists each filing once per associated CIK, usually twice); per-run fetch budget `InsiderConfig.max_filings_per_run` (300). Capped via `InsiderConfig` (`max_pct` 3% NAV, `max_per_day` 2) so its bounded LLM-scoring cost stays small. Routed through `_process_insider_signal` (mirrors `_process_signal`) with `signal_type="insider"`; a ticker also in the screener resolves to `"both"`. SEC requires a `User-Agent` with contact email (`InsiderConfig.sec_user_agent`, env `SEC_USER_AGENT`).
4. **Phase 3 — inverse-ETF hedge** (`hedge/hedge_engine.py`): opens/closes inverse ETFs (SH/PSQ/RWM/…) when the regime is bear/crash.

**Decision flow for a candidate:** event-calendar gate (`utils/event_calendar.py`) → `gather_research` → AI entry score (`bot/ai_analyst.score_entry_with_debate`; bull/bear debate for conviction ≥ 7) → regime allocation scaling (`regime/allocation_engine.py`) → correlation filter (`risk/correlation.py`) → risk-manager veto (`risk/risk_manager.py`) → `Portfolio.open_position`.

**Regime engine (`regime/`):** `hmm_engine.HMMRegimeEngine` fits candidate HMMs (n=3–7) and selects by BIC; regimes are labeled by ascending mean return (crash…euphoria). Inference is **causal/forward-only** (`predict_proba_filtered`, frozen training scaler) — this is the part of the system most careful about look-ahead. `initialize_incremental` + `update_single` give O(K²) daily updates; `rolling_refit` every `refit_interval_days` (30). `gaussian_hmm.GaussianHMM.fit()` runs `n_restarts` (`RegimeConfig.n_restarts`, default 5) random k-means-style inits + full EM each, keeping the best by final log-likelihood — guards against a single bad local optimum destabilizing regime labels across rolling refits. Each restart uses a derived seed (`random_state + i`), so the result stays reproducible for a fixed `random_state`. Tests use a smaller `n_restarts` (mock config) to keep the function-scoped `fitted_engine` fixture fast.

**Risk manager (`risk/risk_manager.py`):** independent, hard veto. Circuit breakers: daily reduce (3%) / halt (4%) / deleverage (6%), weekly halt (8%), max-drawdown lockout (15%) which writes a `RISK_LOCKOUT` file requiring **manual** deletion. Also enforces per-position cap (8%), sector cap (30%), and ADV cap (5%).

**Portfolio (`bot/portfolio.py`):** position open/close/reduce, soft stop-loss / take-profit enforcement, **NAV-based sizing** (cash + mark-to-market), and `reconcile_with_broker` (books ghost positions with a matching broker fill into `closed_positions`, otherwise deletes-and-alerts; alerts on untracked ones).

**Config (`system/config.py`):** one frozen `Settings` dataclass with nested typed configs (`RegimeConfig`, `RiskConfig`, `AllocationConfig`, `HedgeConfig`, `CorrelationConfig`, `ExecutionConfig`, `BacktestConfig`, `InsiderConfig`, …). Import the module-level `settings` singleton everywhere. All tunable parameters live here — do not hardcode constants in logic modules. `Settings.validate()` enforces circuit-breaker ordering.

**Backtesting (`backtesting/`):** `walk_forward.py` (rolling train/test, frozen scaler, forward-only classification), `simulation.py` (`simulate_portfolio`), `metrics.py`, `benchmarks.py`, `analysis.py`, `stress_test.py`.

**Persistence (`bot/db.py`):** SQLite tables — `disclosures`, `insider_disclosures`, `signals`, `fundamental_signals`, `positions`, `closed_positions`, `portfolio_log`, `regime_log`, `regime_transitions`, `risk_events`, `backtest_results`, `schema_version`. Insider positions store `signal_id=NULL` (no FK into the congressional `signals` table); the Form 4 buy is persisted in `insider_disclosures` for audit + cluster counting. WAL mode; `foreign_keys=ON`; migrations in `_MIGRATIONS` keyed by `schema_version`. `realized_pnl` is net of both-side commissions.

**Monitoring (`monitoring/`):** structured JSON logging with an `EventType` enum (`emit_event`) and pluggable alert senders (`fire_alert`, webhook/log).

**Feature engineering (`features/feature_pipeline.py`):** `FeatureConfig` dataclass + causal feature computation (vol, trend, momentum, drawdown, VIX) consumed by the regime engine. All features strictly forward-only — no look-ahead.

**Market data (`market_data/market_feed.py`):** fetches daily SPY/VIX bars via yfinance for the regime engine. Separate from individual-stock data in `bot/researcher.py`.

**Performance tracking (`performance/tracker.py`):** `PerformanceTracker` reads live `trading.db` and computes the same metrics as `backtesting.metrics.compute_all` — enabling direct live vs backtest comparison.

<a id="key-documents"></a>
## Key documents (`docs/`)

- `PHASE0_FINDINGS.md` — Phase 0 gate status (BLOCKED ON DATA); required datasets and pass/fail rules
- `DATA_SOURCES.md` — all external data sources, current status, and fallback behaviour
- `PIT_DATA_REQUIREMENTS.md` — schemas for point-in-time data needed to unblock Phase 0
- `CONGRESSIONAL_EDGE.md` — congressional trading edge analysis
- `HEDGE_ANALYSIS.md` — inverse-ETF hedge analysis
- `FACTOR_BACKTEST_2026-06-28.md` — PIT backtest of the price-based sleeves (low-vol/BAB, residual momentum, mean reversion); rationale behind `_REGIME_WEIGHTS`
- `EDGE_BACKLOG.md` — evaluated-but-deferred/rejected signals from the 2026-07-07 edge review (estimate revisions, 13F, options-implied, CEO-insider verdict, routine-buyer filter, SUE PIT-backtest follow-up) with the conditions under which each becomes worth implementing

<a id="scheduler"></a>
## Scheduler (Amsterdam time, NYSE-session guarded)

Defined in `RegimeAwareOrchestrator.start()`. Jobs run on a **single-thread executor** so the pipeline and exit review never touch the DB/portfolio concurrently.

- Mon 07:00 — `refresh_universe()`
- 13:00 / 17:00 — `run_screener_prefetch()` (pre-fetch fundamentals ~1h before each pipeline run)
- 14:00 / 18:00 — `run_morning_pipeline()` (Phase 1 + 2 + 3; runs twice daily since 2026-07-09)
- 16:00 — `run_exit_review()`
- 15:45 / 17:00 / 20:00 — `run_intraday_check()` (stop-loss + circuit breakers; tighter misfire grace)
- 22:30 — `run_eod()`
- Fri 22:45 — `log_weekly_report()`

**Catch-up-on-restart** (added 2026-07-10): `BlockingScheduler` is in-memory
only — a process restart after 14:00 permanently drops that day's remaining
cron windows (`misfire_grace_time` only covers a live-but-blocked scheduler,
not a process that wasn't running). `run_morning_pipeline()` records its own
completion via `db.record_job_run("run_morning_pipeline", today)`; `start()`
checks `db.job_ran_today(...)` before entering the blocking loop and, if
today's first window has passed with no completed run recorded, runs the
pipeline once immediately. Safe to call any time — both pipeline methods
no-op on non-trading days and `run_morning_pipeline` already dedupes against
open tickers/capacity.

<a id="data-caveats"></a>
## Known data-source caveats (important)

- **Capitol Trades is a JavaScript SPA.** `bot/scraper.py` tries the JSON API endpoint first (`_fetch_page_json`); HTML scraper is the fallback. If both fail, a `DEAD_FEED` alert fires and the congressional pipeline receives zero inputs for that run. `run_1year_backtest.py` reads a cached JSON snapshot (`capitol_trades_merged.json`, Oct 2025→May 2026 only). See `docs/DATA_SOURCES.md`.
- **ProPublica Congress API is discontinued.** `bot/committee.py` now uses the `unitedstates/congress-legislators` GitHub YAML files (no API key). A 30-day shelve disk cache insulates against transient GitHub outages.
- **`bot/universe.py` uses the *current* S&P 500 + Russell 1000.** Backtests over this set are survivorship-biased; the factor screener reads *current* `yfinance .info` fundamentals (look-ahead) and so is **not** historically reconstructable from yfinance. See `docs/PHASE0_FINDINGS.md`.
- **`yfinance` is a single point of failure** (prices, fundamentals, regime data); many call sites fall back to `0.0`/skip silently. Treat missing data as a first-class failure when you touch these paths.
- **`WalkForwardResult.pooled_attribution` (HAC/Newey-West) is still built from overlapping rolling windows.** The Newey-West standard errors correct for autocorrelation *within* the pooled return series, but the pooled sample itself is assembled from walk-forward windows that share dates by construction (`step_months < test_months`). Read `pooled_attribution["alpha_tstat"]`/`alpha_se` as indicative of a strategy-level alpha estimate, not a formal i.i.d. hypothesis test, until non-overlapping OOS windows are used.

<a id="gotchas"></a>
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
- **Entry hurdle is prompt text, not a code-level filter:** `bot/ai_analyst.py`'s `_ENTRY_SCHEMA` tells the LLM the buy/skip rule (currently 3x cost / 1.0% absolute, loosened from 5x/1.5% on 2026-07-10) — there is no separate deterministic check in Python. `EntryScore.expected_return_pct` (added 2026-07-10, default `0.0` for backward compat) is observability only, populated from the LLM's own self-reported estimate and persisted on `signals`/`fundamental_signals`; it does not gate anything in code.

<a id="history"></a>
## Review & change history (moved verbatim from the CLAUDE.md status banner, 2026-07-06 — append new entries here)

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
> 2026-06-28: added three profitable strategies (cost-conscious; see
> `docs/superpowers/plans/` review). Phase 1 screener gained a 4th **low-vol/BAB** sleeve
> and a **residual-momentum** sub-signal (both price-derived, zero LLM cost;
> `_REGIME_WEIGHTS` are now 4-tuples). New **Phase 2.5 insider** signal source: SEC EDGAR
> Form 4 open-market buys (`bot/insider.py`, `bot/insider_signal.py`, `InsiderConfig`,
> `insider_disclosures` table, `_process_insider_signal`, `signal_type="insider"`), capped
> like congressional so added API cost is bounded (≤2 scored candidates/day). PIT backtest
> (`backtesting/backtest_price_factors.py`) over a fixed/survivorship-biased universe:
> residual momentum Sharpe ~0.88 / +5.8%/yr alpha vs SPY ~0.66; low-vol/BAB cuts beta to
> ~0.6 and drawdown below SPY (defensive). Findings encoded into `_REGIME_WEIGHTS` and
> recorded in `docs/FACTOR_BACKTEST_2026-06-28.md`.
> 2026-06-28 (follow-up): added a 5th **short-term reversal (mean-reversion)** sleeve
> (`reversal = -mom_1m`); `_REGIME_WEIGHTS` are now 5-tuples. Backtest showed naive
> 1-month reversal is **weak standalone** on this large-cap monthly universe (Sharpe 0.53,
> −1.2%/yr alpha, deepest drawdown), so it is weighted small and regime-gated (up in
> neutral/range-bound + bear, ~0.05 in trends/crashes) and blended with quality/value/
> low-vol so the bot prefers *oversold-but-sound* names (mitigates falling knives).
> Test count: **806** (+51).
> 2026-06-29: emphasised residual momentum further — the momentum sleeve is now a
> weighted blend (`_MOMENTUM_WEIGHTS`) with `resid_mom` the largest sub-weight (0.45 vs
> the prior equal 0.25). Caveat made explicit in `docs/FACTOR_BACKTEST_2026-06-28.md`: the
> backtest's headline "+5.8%/yr alpha" is beta-adjusted *and* survivorship-inflated; net of
> the equal-weight baseline (+2.3%/yr alpha, same biased universe) the factor-specific edge
> is ~3–4%/yr, before trading costs. Test count: **808** (full suite green).
> 2026-07-02: pre-launch full review ahead of live paper trading. Re-verified all 5
> Critical fixes from `TRADING_BOT_REVIEW_2026-06-23.md` are still correct after the
> 2026-06-28/29 strategy commits (no regressions). Fresh audit of the strategy code added
> since that review (low-vol/BAB, residual momentum, mean-reversion, insider) found and
> fixed 2 new bugs: `run_pit_backtest` was force-closing and immediately reopening tickers
> that persisted across rebalance windows, paying phantom double commission and skewing
> the Sharpe/alpha numbers behind `_REGIME_WEIGHTS`; `_fetch_sp500_ishares`' fallback
> validated tickers before normalization, silently dropping class-share names (e.g.
> `BRK.B`) again. Also fixed a lookback-window mismatch in `_compute_pit_price_factors`
> (was using the full ~278-bar fetch window instead of the trailing 252 bars `mom_12m`
> anchors to) and added missing regression coverage for `_fetch_price_factors_batch`'s
> real beta/resid_mom math and `_process_insider_signal`'s position-size cap (both were
> previously only exercised via mocks). Operational readiness added: `ALERT_WEBHOOK_URL`
> documented in `.env.example`, `docs/RUNBOOK.md` (start/monitor/restart guide),
> `regime_model.joblib`/`dashboard_state.json` added to `.gitignore`, and
> `docs/FACTOR_BACKTEST_2026-06-28.md` now notes where the backtest's low-vol and
> reversal sleeves diverge from the live composite's scoring. `TRADING_BOT_REVIEW_2026-06-23.md`
> and `TRADING_BOT_FULL_REVIEW_BUNDLE.md` removed — all findings from both are now fixed
> and folded into this history. Test count: **818** (full suite green).
> 2026-07-06: live paper trading started. First real run hit a Critical bug: `_llm_call`'s
> OpenAI retry path (`bot/ai_analyst.py`) assumed any "unsupported parameter" 400 was about
> `temperature`/`seed` (true for an earlier reasoning-tier model) and retried by dropping
> those — but kept `max_tokens`, which `gpt-5.4` actually rejects (needs
> `max_completion_tokens`). The retry failed identically, so every candidate
> (screener/congressional/insider alike, all share this call) errored and was skipped —
> 100% entry-scoring failure, zero trades possible. Fixed: retry now inspects which param
> was rejected and swaps only that one; confirmed live that `temperature`/`seed` are fine
> for `gpt-5.4`. Test count: **819** (full suite green).
> 2026-07-07: full review remediation + approved edge package. Part A fixes: (A1)
> `RiskManager.restore_baselines()` seeds peak/week/day NAV from `portfolio_log` on
> startup — a restart no longer resets the drawdown lockout and loss breakers; (A2/A3)
> insider feed switched to the EDGAR daily form index (full-day coverage) with
> getcurrent fallback, accession + in-run id dedup, getcurrent count clamped to 100,
> `max_filings_per_run` 120→300; (A4) sector ranks centered ((rank−0.5)/n) removing
> small-sector score inflation; (A5) value/quality all-missing now imputes neutral 16;
> (A6) `mom_12m` anchors to trailing 252 bars; (A7) `GaussianHMM.fit` raises a clear
> error on all-NaN restarts; (A8) conventions (dead branch, `_ESTIMATED_COST_PCT`
> hoist, `veto_new_entry` param, weekly-halt validate ordering). New signals (edges
> B1–B3): SEC XBRL `screener/xbrl_fundamentals.py` — SUE/PEAD into the momentum blend
> (0.15, conservative pending PIT backtest), Sloan accruals into quality, net payout
> yield into value (free frames API, zero LLM cost, live-verified); short-interest
> negative screen (>20% of float excluded, `UniverseConfig.max_short_pct_float`).
> Deferred/rejected edges + revisit conditions recorded in `docs/EDGE_BACKLOG.md`.
> 2026-07-07 (later same day): first two live trades opened (CF, VTRS, both
> fundamental-screener signals) exposed 3 more Critical/High bugs. (1) `AlpacaBroker.
> place_stop_order` always sent `TimeInForce.GTC`; Alpaca rejects GTC on fractional-share
> qty ("fractional orders must be DAY orders") — NAV-based sizing produces fractional
> qty routinely, so both new positions opened with **zero resting stop** (the price-poll
> backstop in `enforce_stop_losses` still protected them each intraday check, but with no
> continuously-resting order in between). Fixed: DAY for fractional qty, GTC for whole
> shares; DAY stops expire at session end but the existing trail-up poll already re-arms
> them each intraday check. (2) `bot/insider.py._fetch_daily_form4_index` only slept
> between *successful* daily-index fetches, not between consecutive misses
> (weekend/holiday/not-yet-published days) — a burst of back-to-back requests with zero
> delay (4 in <1s in the live log) tripped SEC's WAF into 403s regardless of the compliant
> User-Agent. Fixed: sleep before every attempt after the first. (3) `bot/scraper.
> _fetch_page_json` treated *any* sub-500 HTTP status as "endpoint doesn't exist," so a 429
> (rate limit) short-circuited straight to the HTML fallback instead of backing off and
> retrying — and neither fetch path honoured a `Retry-After` header. Fixed: 429 now retries
> (respecting `Retry-After` if present); genuine 4xx (401/403/404) still falls back
> immediately. Capitol Trades itself was still down after the fix (external 429s, 2 days
> running) — worth rechecking if it persists past day 3.
> Deeper look at the same two trades found the real root cause: both were **phantom
> positions**. `Portfolio.open_position` only bailed out on `OrderStatus.REJECTED` — any
> other status (including the `SUBMITTED` state left by a fill-poll timeout) fell through
> to the "use fill data if available, else NAV estimate" path and booked the position
> regardless of whether the order ever actually filled. Both CF and VTRS buy orders were
> still `NEW`/unfilled at Alpaca **~53 minutes later** during active market hours — not a
> slow-fractional-fill quirk, a genuinely stuck order — yet the local DB had booked full
> positions, run allocation sizing, and attempted (and failed, per the stop-order bug
> above) to protect them with stops. `reconcile_with_broker` only diffs *positions*, never
> looks at outstanding *orders*, so the two dangling live orders weren't visible to any
> existing safety net — if they'd filled later they would have surfaced as untracked broker
> positions with zero local record. Fixed: `open_position` now requires
> `order.status == OrderStatus.FILLED` before booking anything; any other terminal/non-
> terminal status cancels the order (a no-op if it raced to FILLED in the interim —
> `reconcile_with_broker`'s untracked-position alert is the backstop for that race) and
> returns `False` instead of guessing. The two real dangling orders were cancelled by hand
> after diagnosis. Test count: **860** (full suite green).
> Test count: **853** (full suite green).
> 2026-07-09: root-caused why CF/VTRS's fills were never confirmed in the first place —
> `AlpacaBroker._poll_order_fill` only polled 3 times at 0.2s apart (~0.4s total), nowhere
> near enough for Alpaca's paper API to confirm a market-order fill. Even with the
> `OrderStatus.FILLED`-required fix above, that short a window means most orders would still
> time out and get cancelled instead of landing as real trades. Widened to 15 attempts at 1s
> apart (~14s). Three existing `test_broker.py` tests that exercised the never-terminal poll
> path without mocking `time.sleep` started sleeping for real once the window widened
> (14s each); added the same `monkeypatch.setattr(time, "sleep", ...)` pattern already used
> elsewhere in that file to keep the suite fast. Test count: **861** (full suite green,
> ~97s).
> 2026-07-09 (later same day): volatile markets prompted scanning for new entries twice a
> day instead of once (user explicitly chose scan-frequency over larger position sizes —
> the regime/instability sizing multipliers in `regime/allocation_engine.py` were left
> untouched). Added a second `run_screener_prefetch`/`run_morning_pipeline` pair at
> 17:00/18:00 CEST (11:00/12:00 EST), mirroring the existing 13:00/14:00 pre-market pair.
> `run_morning_pipeline` already dedupes against currently-open tickers and respects
> `can_open_new_position()`/invested-pct capacity, so a second run in one day is safe — it
> can only skip or add, never double-open. Test count: **862** (full suite green, ~98s).
> 2026-07-10: user review — the bot had generated **zero** signals/positions since
> 2026-07-07 14:01 despite being "live." Root cause: `BlockingScheduler` is in-memory
> only, so 5 process restarts in 3.5 days (chasing the fixes above) each lost that
> calendar day's already-passed cron windows — `misfire_grace_time` only covers a
> live-but-blocked scheduler, not a process that wasn't running yet. Fixed with a
> catch-up-on-restart check in `start()` (see `#scheduler`) backed by a new `job_runs`
> table (`db.record_job_run`/`db.job_ran_today`). Also fixed: `bot/universe.py`'s
> Russell 1000 fetch sent no `User-Agent` and was 503ing on every single run (silently
> degrading the universe to S&P-500-only, ~503 vs ~1400 names) — added the same header
> the S&P 500 iShares fallback already used. `bot/portfolio.py.open_position`'s
> *initial* stop placement (as opposed to the trailing-stop call site fixed in the
> 07-07 phantom-position pass) discarded `place_stop_order`'s return value with no
> check — now alerts if a fresh position opens with zero resting stop. Also loosened
> the AI entry hurdle from 5x-cost/1.5%-absolute to 3x/1.0% (user decision — trades were
> already rare from the scheduler bug) and added `EntryScore.expected_return_pct`
> (observability only, not decision-critical, default `0.0`) persisted on
> `signals`/`fundamental_signals` so the hurdle's real bite is measurable going forward
> instead of only visible in free-text rationale strings. Added macOS launchd
> supervision (`~/Library/LaunchAgents/com.thomasvromen.tradingbot.plist`, outside the
> repo — see `docs/RUNBOOK.md`) so a crash auto-restarts within ~30s instead of waiting
> for manual intervention. Test count: **875** (full suite green, one pre-existing
> unrelated failure noted below, not fixed — `test_insert_and_get_disclosure` hardcodes
> a `disclosure_date` that ages out of `get_existing_ids()`'s 90-day window as real time
> advances; needs a relative-date fixture, tracked as a follow-up, not part of this
> review).
