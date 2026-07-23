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
- 13:00 / 17:00 — `run_screener_prefetch()` (pre-fetch fundamentals; the first no longer needs
  a 1h lead-time relationship to the pipeline since it fetches no live market data)
- 15:40 / 18:00 — `run_morning_pipeline()` (Phase 1 + 2 + 3; runs twice daily since 2026-07-09).
  First window moved from 14:00 on 2026-07-13 — see `#history`.
- 16:00 — `run_exit_review()`
- 15:45 / 17:00 / 20:00 — `run_intraday_check()` (stop-loss + circuit breakers; tighter misfire grace)
- 22:30 — `run_eod()`
- Fri 22:45 — `log_weekly_report()`

**Catch-up-on-restart** (added 2026-07-10): `BlockingScheduler` is in-memory
only — a process restart after 15:40 permanently drops that day's remaining
cron windows (`misfire_grace_time` only covers a live-but-blocked scheduler,
not a process that wasn't running). `run_morning_pipeline()` records its own
completion via `db.record_job_run("run_morning_pipeline", today)`; `start()`
checks `db.job_ran_today(...)` before entering the blocking loop and, if
today's first window has passed with no completed run recorded, runs the
pipeline once immediately. Safe to call any time — both pipeline methods
no-op on non-trading days and `run_morning_pipeline` already dedupes against
open tickers/capacity. `run_morning_pipeline` also refuses to run when
`_nyse_is_open_now()` is False (added 2026-07-13, see `#history`) — day-level
`is_session` alone can't tell pre-open/post-close from a live session, which
is exactly what let a post-close catch-up run place real orders into a
closed market.

<a id="data-caveats"></a>
## Known data-source caveats (important)

- **Capitol Trades is a JavaScript SPA.** `bot/scraper.py` tries the JSON API endpoint first (`_fetch_page_json`); HTML scraper is the fallback. If both fail, a `DEAD_FEED` alert fires and the congressional pipeline receives zero inputs for that run. `run_1year_backtest.py` reads a cached JSON snapshot (`capitol_trades_merged.json`, Oct 2025→May 2026 only). See `docs/DATA_SOURCES.md`.
- **ProPublica Congress API is discontinued.** `bot/committee.py` now uses the `unitedstates/congress-legislators` GitHub YAML files (no API key). A 30-day shelve disk cache insulates against transient GitHub outages.
- **`bot/universe.py` uses the *current* S&P 500 + Russell 1000.** Backtests over this set are survivorship-biased; the factor screener reads *current* `yfinance .info` fundamentals (look-ahead) and so is **not** historically reconstructable from yfinance. See `docs/PHASE0_FINDINGS.md`.
- **Russell 1000 coverage is currently broken (live-verified 2026-07-10) — universe is S&P-500-only in practice.** Both iShares CSV endpoints `_fetch_russell1000` and `_fetch_sp500_ishares` hit (IWB holdings and the IVV S&P 500 fallback) now return a 200-OK bot-protection/interstitial HTML page instead of CSV, regardless of `User-Agent`/`Accept`/`Referer` headers — this is a WAF-style change on iShares' side, not a missing-header issue. Both functions now sniff for an HTML response and raise a clear `ValueError` instead of letting pandas's CSV parser fail cryptically, but that only makes the existing S&P-500-only fallback (`_build_universe`) diagnosable — it does not restore Russell 1000 coverage. S&P 500 itself is unaffected because Wikipedia (the primary source) still works; only the IVV fallback path (Wikipedia-blocked case) would also be hit by this. Needs a non-iShares data source for Russell 1000 constituents — not investigated further this session (CLAUDE.md forbids adding a headless browser, which would otherwise be the obvious workaround for a JS/WAF challenge).
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
> table (`db.record_job_run`/`db.job_ran_today`). `bot/portfolio.py.open_position`'s
> *initial* stop placement (as opposed to the trailing-stop call site fixed in the
> 07-07 phantom-position pass) discarded `place_stop_order`'s return value with no
> check — now alerts if a fresh position opens with zero resting stop. Also loosened
> the AI entry hurdle from 5x-cost/1.5%-absolute to 3x/1.0% (user decision — trades were
> already rare from the scheduler bug) and added `EntryScore.expected_return_pct`
> (observability only, not decision-critical, default `0.0`) persisted on
> `signals`/`fundamental_signals` so the hurdle's real bite is measurable going forward
> instead of only visible in free-text rationale strings.
> `bot/universe.py`'s Russell 1000 fetch sent no `User-Agent` and 503'd on every run —
> added the same header the S&P 500 iShares fallback already used, matching the
> live-log evidence at the time. **Live-verified after landing: this did NOT restore
> full coverage** — both iShares CSV endpoints (Russell 1000 IWB *and* the S&P 500 IVV
> fallback) now return a 200-OK bot-protection/interstitial HTML page instead of CSV,
> regardless of headers (tried two different realistic browser header sets). The
> universe is still S&P-500-only (~503 tickers, via the working Wikipedia primary
> source) — same symptom as before, different root cause (WAF-style gating, not a
> missing header), and not fixable by a header tweak. Added a clear HTML-sniff guard
> (`resp.text` starting `<!doctype`/`<html`) to both `_fetch_russell1000` and
> `_fetch_sp500_ishares` so this fails with a diagnosable message instead of a cryptic
> pandas tokenizer error — **but full Russell 1000 coverage remains unresolved**, needs
> a different data source (out of scope for this session; CLAUDE.md still says no
> headless browser). Attempted macOS launchd supervision
> (`~/Library/LaunchAgents/com.thomasvromen.tradingbot.plist`, outside the repo — see
> `docs/RUNBOOK.md`): the plist is correct (the exact command runs fine invoked
> directly) but `launchctl`/`bootstrap` registers the job and it then exits immediately
> with code 78 (`EX_CONFIG`) on every attempt, no process output at all — almost
> certainly macOS's Background Task Management gate blocking a newly-registered
> LaunchAgent pending approval in System Settings → General → Login Items &
> Extensions, which needs the user's GUI interaction. **Not currently active** — the
> bot runs via the existing manual `nohup` path. Test count: **877** (full suite
> green, one pre-existing unrelated failure noted below, not fixed —
> `test_insert_and_get_disclosure` hardcodes a `disclosure_date` that ages out of
> `get_existing_ids()`'s 90-day window as real time advances; needs a relative-date
> fixture, tracked as a follow-up, not part of this review).
> 2026-07-10 (later, momentum/LLM/Russell-1000 follow-up review): while investigating why the
> bot had never opened a real position despite `fundamental_signals` showing candidates on
> 07-06/07/09/10, found the actual cause — `tests/test_orchestrator.py` was writing fake rows
> into the **live** `trading.db` on every `pytest` run. `_process_fundamental_candidate`'s
> `insert_fundamental_signal` call (`orchestration/main_loop.py:1163`) used a local
> (function-scoped) `from bot.db import ...`, invisible to the `mocker.patch(
> "orchestration.main_loop.X")` pattern every other DB write in the file uses (13 other
> call sites correctly mock `insert_signal` this way) — and no orchestrator test sets
> `DB_PATH`, so the write landed on the real production file via `bot/db.py`'s relative-path
> default. 287 of 289 `fundamental_signals` rows (`ticker='MSFT', composite_score=80,
> rationale='good'` — a literal `tests/test_orchestrator.py` fixture string) were this leaked
> data, accumulated across every pytest run touching that code path. Did **not** affect real
> trading — `self._portfolio` is a `MagicMock` in the same tests, so no fake order/position
> was ever placed, only the signal audit table. Fixed: import moved to module level, mocked
> in both `orch`/`orch_fitted` fixtures (matching the existing `record_job_run` precedent);
> confirmed via `trading.db` mtime no longer changing across a test run. The 287 fake rows
> deleted from the live DB (2 real rows remain: CF/VTRS, both 07-07, the already-documented
> phantom-position trades). **Corrected picture:** no real fundamental candidate has been
> generated on 07-06, 07-08, 07-09, or 07-10 — consistent with the scheduler-dropping-days
> bug fixed earlier the same day; today's fix was not yet live-tested as of this review (bot
> process restarted 12:46 CEST, after the 12:36 fix, but `run_morning_pipeline` doesn't fire
> until 14:00 CEST). Also added an on-demand `weekly-factor-review` project skill
> (`.claude/skills/weekly-factor-review/`, report-only, never auto-edits `_REGIME_WEIGHTS`/
> `_MOMENTUM_WEIGHTS`) plus its first baseline report (`docs/factor-reviews/2026-07-10.md`):
> confirmed momentum's 2026 YTD outperformance is real (MTUM +26-30% vs SPY, WebSearch-
> sourced) and consistent with the bot's current `melt-up`-regime momentum overweight, but
> flagged a regime whipsaw (`euphoria`→`melt-up`→`deep-bear`→`melt-up` within 5 days,
> `regime_log`) as the more urgent live risk than the momentum weighting itself. Test count:
> **877** (full suite green, same one pre-existing unrelated failure as above).
> Later same day: added `Settings.sizing.enable_cross_model_debate` (default off) —
> `score_entry_with_debate`'s bear argument can run on the OTHER configured provider instead
> of the same model arguing with itself, via a new `provider` param threaded through
> `_llm_call`/`_bear_argument`; `Settings.validate()` requires both API keys when on.
> Then the 14:00 CEST `run_morning_pipeline` (first live run since the scheduler fix) exposed
> a new Critical bug: the regime whipsaw continued (rolling HMM refit reclassified
> `melt-up`→`bear`, its first refit since deployment) and **8/8 buy orders timed out on fill
> confirmation** — correctly cancelled per the 07-07 phantom-position fix (`positions` stayed
> empty), but all 4 of `main_loop.py`'s `open_position()` call sites (`_process_signal`,
> `_process_insider_signal`, `_process_fundamental_candidate`, hedge entry) discarded its
> `bool` return value, so every failed fill still logged "Opened ..." and fired an
> `ORDER_PLACED`/`HEDGE_ENTRY` alert claiming success. Worse: that same return value gates
> `all_open_tickers` and the congressional/insider daily caps, so a fill-timeout day silently
> marks every real candidate "handled" and can exhaust the day's quota on phantom failures
> alone, blocking retries at the 18:00 window. Not caught by tests — every `orch`/
> `orch_fitted` fixture mocks `_portfolio` as a `MagicMock`, whose default truthy return value
> matched the "success" path in every existing test; no test ever set
> `open_position.return_value`. Fixed: all 4 sites now check the return value (fundamental
> path keeps `insert_fundamental_signal` firing unconditionally — separate, intentional
> signal-audit behavior). 4 new regression tests, proven red/green via `git stash` of just the
> fix. Test count: **886** (full suite green, same pre-existing unrelated failure).
> 2026-07-13: bot found completely dead for ~3 days (`job_runs` had zero rows after Fri
> 07-10 14:09 CEST, through a full Monday trading day) — a prior fix that day
> (`fba2143`, shared curl_cffi session in `screener/factor_scorer.py`) stopped the
> file-descriptor leak but never added a request timeout anywhere, so a single stalled
> yfinance/curl_cffi call could still block the caller — and, on the single-thread
> APScheduler executor, every subsequent scheduled job — forever; confirmed via a live
> `sample` stack trace (executor thread idle, main scheduler thread parked on a
> lock/semaphore, ~2.5s CPU over 3 real days) and cross-referenced against two earlier,
> shorter versions of the same silent-hang signature (2026-07-06→07, ~11.5h; 2026-07-10→11,
> ~13h — this time nothing recovered it). Restarted the bot; root-caused and fixed: added
> `market_data/yf_session.py` (a shared, 10s-timeout curl_cffi session — `make_shared_yf_session()`
> for batch callers that explicitly close it, `get_shared_yf_session()` a process-lifetime
> singleton for scattered one-off calls) and wired it into every reachable `yf.Ticker(...)`
> construction that had none: `orchestration/main_loop.py` (8 sites), `bot/researcher.py`,
> `bot/signal_engine.py` (both `@lru_cache`'d), `utils/event_calendar.py`.
> `screener/factor_scorer.py`'s own `_make_shared_yf_session` now re-exports the shared
> module's factory (existing mock-patch tests keep working unchanged — `unittest.mock.patch`
> targets the importing module's attribute). `yf.download(...)` call sites (`risk/correlation.py`,
> `market_data/market_feed.py`, factor_scorer's own momentum/price fetches) were NOT touched —
> confirmed via `inspect.signature(yf.download)` that it already defaults `timeout=10`,
> unlike bare `Ticker()`. Separately, alpaca-py's `TradingClient`/`RESTClient` exposes no
> timeout parameter at all and funnels every call through one `requests.Session()` with none
> set (confirmed via `inspect.getsource(RESTClient._one_request)`) — same latent-hang class,
> reachable on every scheduled job regardless of the yfinance fix. Fixed in `bot/broker.py`
> via `_apply_request_timeout()`, which patches the client's `_session.request` to default in
> a 15s timeout without overriding an explicit one. 8 new regression tests
> (`tests/test_yf_session.py`, plus session-propagation assertions in
> `test_researcher.py`/`test_signal_engine.py`/`test_event_calendar.py`/`test_broker.py`).
> Also found, NOT fixed (reported to user, needs a design decision, unrelated bug kind):
> `bot/db.py::get_nav_baselines` can't distinguish week-start from day-start NAV when both
> fall on the same calendar date (every Monday) — see `docs/STATE.md#open-items`. Test
> count: **899** (full suite green; the 2 known pre-existing unrelated failures — see
> `docs/STATE.md#open-items` — are date-dependent, not this session's).
>
> Concurrent session, same evening: root-caused the underlying reason every fill attempt
> times out (the timeout fix above explains why the process goes *silent* for days; this
> explains why it never actually trades even while awake). `run_morning_pipeline`'s cron was
> at 14:00 Amsterdam — 1.5h *before* NYSE's 15:30 CEST (09:30 EDT) open, confirmed directly
> via `exchange_calendars.get_calendar("XNYS").schedule` — and the catch-up-on-restart trigger
> (`main_loop.py`, added 2026-07-10) checked only `hour >= 14` plus day-level `_NYSE.is_session()`,
> never whether NYSE was *currently* open. Tonight's 22:09 restart landed 9 min *after* the
> 22:00 close, tripped catch-up, and placed 7 real orders (BIIB, HIG, NEM, SH, PSQ, RWM, EFZ)
> into a closed market — all 7 timed out on fill confirmation exactly like every prior
> fill-timeout incident this month (07-07 phantom fills, 07-10 8/8 timeouts): the fix in each
> case patched the symptom (poll width, bool-return) and never the reason fills structurally
> could not happen. `is_stable: false` / bear regime was checked and ruled out — confirmed via
> `regime/allocation_engine.py` it is only a 0.5× sizing multiplier, not a hard veto (no
> `is_stable` check in `risk/risk_manager.py`; the log shows orders reaching the broker, past
> `validate_order`). Fixed: new `_nyse_is_open_now()` (`main_loop.py`, uses
> `exchange_calendars`' `is_open_on_minute`) gating `run_morning_pipeline`; first entry window
> moved 14:00→15:40 (10 min after open); catch-up threshold 14→16. Live-verified: called
> `_nyse_is_open_now()` directly against the real post-close wall clock, got `False` as
> expected. Sweep of the order-execution path for the same silent-failure class (the
> `open_position()` bool-ignored bug fixed 2026-07-10, commit 72ed02e) found it on the exit
> side too: `close_position()`/`reduce_position()` also return `False` on a no-fill sell, but
> all 4 call sites (`_run_hedge_exits`, `run_exit_review`'s exit and reduce branches,
> `_close_all_positions`'s deleverage force-close) ignored the return value and logged
> "Closed"/"Force-closed"/called `mark_take_profit_taken` unconditionally — a no-fill sell on
> the *most* safety-critical path (deleverage force-close, triggered by the circuit breaker)
> would have logged risk as reduced while the position stayed fully open. Fixed all 4. 6 new
> regression tests total (2 schedule/guard, 4 exit-path), each proven red before the fix and
> green after. Test count: **903** (full suite green; same 2 pre-existing failures, still
> unrelated — see `docs/STATE.md#open-items`).
>
> 2026-07-14 ("fix everything" follow-up): closed out both remaining pre-existing failures and
> the deferred dead-man's-switch item.
> - **NAV baseline collision** — already fixed by a concurrent session (commit `9a82022`)
>   moments before this session investigated it independently (traced the exact mechanism via
>   an instrumented repro before discovering the commit — should have checked `git log` first).
>   Asked the user two design questions on `start_of_day()`/`get_nav_baselines()` semantics
>   before realizing the fix already existed; their answers (protect the restored baseline,
>   earliest-row-wins for day_start) turned out consistent with what was actually shipped
>   (`week_start_nav` now prefers the last NAV *strictly before* `week_start` instead of
>   "on/after", so it stops colliding with `day_start_nav`'s query on Mondays; `day_start_nav`
>   itself untouched). The test's own fixture had the identical flaw one level up (computed
>   `week_start` as literally `date.today()`, so it only failed when the suite ran on a
>   Monday) — rewritten to seed a genuinely earlier prior-week-close row.
> - **`test_db.py::test_insert_and_get_disclosure`** — hardcoded `disclosure_date: "2026-04-10"`
>   had drifted outside `get_existing_ids()`'s rolling 90-day window as real time advanced past
>   ~07-09. Made all three dates relative to `date.today()`.
> - **Dead-man's switch, built**: `monitoring/dead_mans_switch.py` + `bot.db.get_last_job_run_date()`
>   — fires the existing webhook alert (`monitoring/alerts.py`) if no `job_runs` row exists for
>   the most recently completed NYSE session (via `exchange_calendars`, so weekends/holidays
>   never false-alarm). Must run as a **separate** process from `run_bot.py` — nothing inside
>   the bot's own process can detect its own death, which is exactly what happened 07-10→07-13.
>   New, separate LaunchAgent `com.thomasvromen.tradingbot-deadmansswitch.plist` (`StartInterval`
>   14400s + `RunAtLoad`), logs to `dead_mans_switch.log`. 4 new regression tests, one proven
>   red via a deliberate `sed` weakening of the comparison before restoring. See
>   `docs/RUNBOOK.md#dead-mans-switch`.
> - **launchd, root cause now confirmed (not just hypothesized)**: `launchctl bootstrap` now
>   succeeds (previously "Bad request") and the job reaches `state = spawn scheduled`, but the
>   spawned process still exits immediately (`last exit code = 78`) with zero log output.
>   `log show --predicate 'eventMessage contains "tradingbot"'` shows `backgroundtaskmanagementd`
>   registered this exact LaunchAgent's identifier at the moment of bootstrap, and `launchd`
>   logging "service inactive" every ~30s (matching `ThrottleInterval`) after — i.e. launchd
>   keeps retrying and macOS Background Task Management keeps blocking it pending the user's
>   approval in System Settings. Manually reproduced with `env -i` (no HOME, minimal PATH) to
>   rule out an environment-variable cause first — ran fine, isolating it to the OS-level gate.
>   Did not pursue `sudo sfltool dumpbtm` for a definitive per-user disposition read after it
>   triggered an authorization prompt — stopped and asked first, per explicit user feedback.
>   Both this bot's LaunchAgent and the new dead-man's-switch one need the same one-time
>   System Settings approval.
> - **Russell 1000, still blocked**: no `FMP_API_KEY` added yet. Tried three more free/no-signup
>   sources beyond the ones already ruled out: FTSE Russell (redirects, not a direct download),
>   stockanalysis.com (JS-rendered shell, no exposed JSON API found in its static HTML),
>   SlickCharts (403, same WAF-style block as iShares). No viable alternative found — genuinely
>   needs either the FMP key or a heavier scraping investment not justified here.
>
> Test count: **909** (full suite green, zero known failures — first time in this project's
> history the suite has had none).
>
> **2026-07-14 (independent verification session)**: re-ran the full suite (909 passed,
> matches the count above) and live-verified health rather than trusting the banner —
> found the running bot process (PID 38576) had started at 22:09:09, 37 minutes *before*
> commit b4938bb (22:46:52) landed the NYSE-hours fix, so it was still running the
> pre-fix in-memory schedule (14:00 entry, no `_nyse_is_open_now()` guard) and would have
> repeated the prior night's all-orders-timeout incident at 14:00 CEST today. User
> approved a restart; stopped PID 38576 (nothing mid-run — last completed job was 22:30
> EOD) and relaunched via the documented `nohup` command (new PID 51755, confirmed
> `hour=15, minute=40` schedule and the guard active in the fresh process). Also
> confirmed: dead-man's-switch logging "Pipeline healthy" via launchd on schedule,
> `RISK_LOCKOUT` absent, `job_runs` current through the last completed session
> (2026-07-13), `requirements.txt` still unpinned and Russell 1000 still blocked on
> `FMP_API_KEY` (both unchanged, by design — not touched).
>
> **2026-07-14 (SUE PIT backtest, same session)**: built a point-in-time-correct backtest
> of the SUE signal (`docs/SUE_PIT_BACKTEST_2026-07-14.md`), per a plan confirmed with the
> user before building (`docs/superpowers/plans/2026-07-14-sue-pit-backtest.md`) —
> per-horizon independent gate (not pooled), HAC/Newey-West not naive i.i.d., real PIT
> S&P 500 universe (fja05680/sp500, not current constituents projected backward), d+1
> drift anchor. New modules: `screener/xbrl_pit_sue.py` (companyfacts fetch/cache,
> earliest-original-filing quarterly EPS, PIT SUE reusing the unmodified production
> formula), `backtesting/pit_constituents.py`, `backtesting/backtest_sue_pit.py`. Found and
> fixed two real bugs against real data: `original_quarterly_eps`'s calendar-quarter
> bucketing (SEC buckets by the calendar quarter-end boundary NEAREST a fact's `end` date,
> not a fixed end-month or start-month rule — took 3 iterations to get right, needed a
> collision-exclusion rule for 52/53-week retail fiscal calendars like Costco where the
> per-fact rule breaks down; verified against 7 real tickers plus a full Costco 55-quarter
> history scan) and a history-truncation bug in `build_pit_sue_events` that starved the SUE
> seasonal-random-walk denominator and produced absurd outlier values (7.2e15 on one real
> PTC event) — fixed by passing the full company history into the SUE computation and using
> the sample window only to decide which events are output. Also deduped same-day
> multi-quarter filings (993 duplicate-valued rows). Full real-data run: 18,708 events,
> 570 tickers, 2012-2026. **Result: gate failed** on both horizons (20d t=0.87/IR=0.24, 60d
> t=1.41/IR=0.30) and on the 60d first/second-half stability condition (sign flip); the
> regime-consistency condition passed (positive mean drift in all 7 regimes, no sign
> flips); the PIT-vs-naive honesty check confirmed no residual look-ahead (PIT reads weaker
> than a naive T+0 anchor at both horizons, as expected). Per the pre-committed decision
> rule, the SUE sub-weight stays at 0.15 — `screener/factor_scorer.py` untouched. Test
> count: **942** (full suite green, zero known failures).
>
> **2026-07-14 (live dig-in session)**: user reported the bot "not trading" again. Root
> caused two bugs, live-verified a restart, then a third bug surfaced live. (1)
> `_process_fundamental_candidate` (`orchestration/main_loop.py`) called
> `insert_fundamental_signal()` unconditionally, before checking `open_position`'s return
> value — every candidate scored outside NYSE hours (the closed-market pipeline-timing bug
> fixed earlier the same day, commit `b4938bb`) still landed a `fundamental_signals` row
> with a real conviction/expected-return score despite the order never filling. That's
> why several days of "candidates" (CF/VZ 07-10, NEM/HIG/BIIB 07-13) showed zero matching
> `positions` rows — traced each one's timestamp to outside the 15:30-22:00 CEST session.
> Also matters beyond diagnostics: `run_bot.py --backtest` feeds `get_fundamental_signals()`
> straight into its signal set, so unfillable candidates were silently inflating the
> backtest's opportunity set. Fixed: move the insert after the `opened` check (commit
> `a0cd1c4`). (2) The live process (PID 51755, already running the correct 15:40-cron code)
> had gone completely idle since 13:57 — 2+ hours with zero cron jobs dispatched, no error
> logged, no `job_runs` row for the day. A process `sample` (macOS) showed both the
> scheduler's main thread and its single worker thread genuinely parked (empty queue, timed
> wait) — not stuck inside a job. Checked and ruled out: system sleep (`pmset -g log`, none
> in the window), `CronTrigger` computation (tested standalone, correct), APScheduler's own
> `_process_jobs`/`MemoryJobStore` source (read directly, no obvious bug for this shape).
> No definitive code-level cause found — flagged as a live wedge distinct from the
> previously-fixed missing-timeout hang (that pattern shows the worker thread stuck inside
> a job's stack; this was idle). Restarted with user approval (new PID 62191) — cleared it
> immediately; the catch-up-on-restart path fired and completed the pipeline, producing the
> bot's **first-ever real fundamental fills**: VICI (1.3% NAV) and PFE (1.1% NAV). (3) That
> surfaced a third bug: both fills' initial stop-loss placement was rejected by Alpaca —
> `code 40310000, "potential wash trade detected... opposite side market/stop order
> exists"` — identical on both tickers, leaving two real (paper) positions naked. Root
> cause: Alpaca's wash-trade check still sees the just-filled buy as an opposite-side order
> for that symbol at the moment the stop is submitted — its fill-state propagation lags our
> own `_poll_order_fill` confirmation. The existing `enforce_stop_losses()` poll (next
> `run_intraday_check`, 20:00 CEST) caught and fixed both ~2h later, exactly as its own
> backstop comment describes — confirmed via `bot.log` (`Stop order placed PFE ...`,
> `Stop order placed VICI ...` at 20:00:00-01). Fixed the root cause for future entries
> too: `Portfolio.open_position`'s initial stop placement now retries 3x with 1s/2s backoff
> before alerting, mirroring the existing `_place_sell_with_retry` convention (commit
> `91607a5`); `enforce_stop_losses`' own trail-up placement is a separate, untouched call
> site. Both (1) and (3) proven via git-stash red/green (test failed against pre-fix code,
> passed after restore). A manual one-shot `enforce_stop_losses()` trigger to close the
> live gap immediately was proposed and user-approved, but by execution time the scheduled
> 20:00 run had already resolved it — skipped as no longer necessary rather than taking a
> live-trading action with no remaining justification. Full suite: **942 passed** (fresh
> re-run after both fixes, matches the SUE-PIT-backtest count above — that work landed on
> disk from a concurrent session mid-way through this one).
>
> **2026-07-16/17 (reliability watchdog, commits `654eb49`/`41691ae`/`c43bd66`):** after two
> more scheduler wedges in 24h — 07-16 sat undetected from ~22:30 to 18:51 the next day
> (found and restarted manually), then wedged *again* overnight 07-16 20:00 -> 07-17 10:44
> (caught live during this exact session) — the user asked for a permanent fix, citing ~10
> cumulative "not trading"/downtime incidents and explicitly rejecting another one-off
> patch. `pmset -g log` on the second wedge again showed the caffeinate assertion active the
> whole time, with repeated "Sleep Service Back to Sleep" cycling on battery — Power Nap
> (`powernap=1` in `pmset -g custom`), a distinct mechanism from the idle/system sleep
> `caffeinate -i -s` actually prevents.
>
> Research (delegated to an Explore subagent to keep the incident table out of main
> context) built a full chronological inventory from `docs/STATE.md` and this file's own
> history and found the incidents are genuinely **not** one recurring bug — at least 15
> structurally distinct classes (LLM param compat, stop-order TIF mismatch, two separate
> rate-limit bugs, unconditional-insert-on-unknown-status, insufficient poll window,
> scheduler-state-not-persisted, missing-timeout hangs, wrong pipeline timing window, three
> separate bool-return-ignored manifestations, NAV baseline collision, wash-trade race,
> sqlite3.Row/dict type mismatch, stale-deploy-running-old-code (recurred twice), and two
> distinct OS-level sleep gaps). Fixing the latest one every time was never going to reach
> "never again" — the plan instead targeted **bounded auto-recovery regardless of cause**.
>
> Built: (1) `job_runs` coverage extended from just `run_morning_pipeline` to all three core
> cron jobs (`run_intraday_check`, `run_eod` now call `db.record_job_run` too) — previously
> a wedge occurring after the morning pipeline already succeeded that day was invisible to
> any `job_runs`-based freshness check; (2) `monitoring/status_file.py` — writes
> `bot_status.json` (pid, git commit, started_at) at every `initialize()` call, closing the
> stale-deploy gap that caused the 07-14 and 07-15 sessions to each need a human to notice
> the running process was older than the latest fix; (3) `monitoring/watchdog.py` — a new
> `StartInterval` LaunchAgent (900s / 15 min, `~/Library/LaunchAgents/
> com.thomasvromen.tradingbot-watchdog.plist`) that checks process liveness, per-job
> staleness (30 min grace past each cron time), and deploy freshness, auto-restarting on any
> of them. This **reverses** the 2026-07-14 decision to stay alert-only
> (`docs/STATE.md` Decisions/Constraints — that decision assumed a human noticing within
> hours was an acceptable tradeoff; a ~20h undetected wedge showed it wasn't). Every restart
> is gated on 10 minutes of `bot.log` quiet, so a legitimately long-running catch-up
> pipeline (observed to take ~10 min end-to-end) is never mistaken for a wedge — the only
> unconditional check is process liveness, since a dead PID cannot be "mid-job". The kill
> itself is verified via `ps -p <pid> -o command=` containing `run_bot.py` before acting,
> never by PID alone, guarding against PID reuse.
>
> Caught a real bug before it shipped: the `orch`/`orch_fitted` fixtures in
> `test_orchestrator.py` and two direct `RegimeAwareOrchestrator(settings)` constructions in
> `test_event_calendar.py` build real orchestrator instances — without mocking the new
> `write_status_file`, every test run would have overwritten the live bot's actual
> `bot_status.json` with a fake test PID. Fixed by mocking it at all 4 construction sites
> before any test ran against the new code.
>
> Live-verified end-to-end, not just unit-tested: the 07-16/17 overnight wedge was
> discovered live during this session (bot quiet since 20:00 the prior evening, no `job_runs`
> row for 07-17), restarted (safe per `docs/RUNBOOK.md#safe-restart` — 14+ hours quiet, not
> mid-job) to deploy the new code, and a forced watchdog cycle
> (`launchctl kickstart -k ...`) correctly read the fresh `bot_status.json` and reported
> `healthy:recent_activity`. Separately, per the user's explicit choice between the
> documented sleep-mitigation options, `sudo pmset -a powernap 0` was handed to the user to
> run themselves (agent cannot run `sudo` interactively) as a targeted fix for the specific
> Power-Nap symptom observed, rather than full `disablesleep` or a laptop migration — see
> `docs/RUNBOOK.md#sleep-wedges`. Full suite: **975 passed** (was 947 at session start), zero
> known failures.
>
> **2026-07-17 (strategy/profitability review + full remediation):** first full review of the
> bot's trading LOGIC since live launch — explicitly not reliability/uptime (that's the
> concurrent thread above). Scoped via three parallel Explore passes (docs survey, code
> structure map, live parameter sheet), then four tracks: wiring/correctness verification,
> code quality, strategy/profitability (including bounded new empirical work against
> already-cached PIT data, respecting the Phase 0 BLOCKED-ON-DATA gate — no new scraping),
> and process. Findings written to a standalone report, reviewed and signed off by the user,
> then fully remediated in the same session — report retired per this repo's convention
> (`docs/guardrails/PROJECT.md`: standalone `TRADING_BOT_REVIEW_*.md` docs are removed once
> fully remediated so they don't go stale).
>
> Findings and fixes, ranked by profitability/risk impact:
> 1. The real live entry hurdle was **4.5%, not the documented 1.0% floor** — `bot/ai_analyst.py`'s
>    `_ENTRY_SCHEMA` states "buy only if expected_return ≥ 3× estimated_cost_pct AND ≥ 1.0%
>    absolute," but `orchestration/main_loop.py`'s `_ESTIMATED_COST_PCT = 1.5` meant the 3×
>    term (4.5%) always dominated the 1.0% floor — the 2026-07-10 hurdle loosening (5x/1.5%→
>    3x/1.0%, commit `0d93f8b`) only edited prompt text and never touched this constant, so it
>    never took effect. Real modeled round-trip cost (`slippage_bps=5.0`, zero Alpaca
>    commission) ≈ 0.10%, so the constant overstated cost ~15x. Live evidence: all 17 non-zero
>    `expected_return_pct` rows in `fundamental_signals` (07-07→07-16) clustered at 4.9-7.8%,
>    consistent with the LLM anchoring against the real 4.5% wall. Fixed: `_ESTIMATED_COST_PCT`
>    1.5 → 0.4 (gives a ~1.2% floor, just above the real cost and the intended 1.0% floor). 2
>    new regression tests.
> 2. **First real (not synthetic) backtest of the congressional signal shows a negative
>    excess return.** `backtesting/analyze_congressional_edge.py`/`analyze_hedge_drag.py` had
>    only ever run against synthetic fixtures despite real cached data existing
>    (`capitol_trades_merged.json`, 5,406 trades, Oct 2025-May 2026). Computed forward excess
>    returns directly: congressional buys showed significantly negative excess return at both
>    1mo (-0.636%, t=-2.57) and 3mo (-2.538%, t=-4.93) horizons (caveats: overlapping windows,
>    single ~7-month/one-regime sample — sign taken seriously, magnitude cautiously). Per this
>    repo's convention of never silently auto-editing a signal's weight (see the
>    `weekly-factor-review` skill), left `_CONGRESSIONAL_MAX_PCT`/caps unchanged — this is a
>    finding for a future explicit decision, not applied.
> 3. **`run_scraper()` in `run_morning_pipeline` had no try/except**, unlike every phase after
>    it — a congressional-scraper exception (this scraper has broken this way twice before)
>    propagated out of the whole function, silently zeroing Phase 1 (fundamental, the primary
>    signal)/2.5/3 for the entire day, same failure shape as several already-fixed incidents.
>    Fixed: wrapped in try/except, degrades to `qualified=[]`, fires a `DEAD_FEED` alert. 1 new
>    regression test.
> 4. **`regime/hmm_engine.py`'s `update_single` could silently classify off a stale feature
>    row.** `dropna().iloc[-1:]` drops any row with a NaN in any selected feature column, not
>    just the newest one — a single-column NaN on today's bar (e.g. a `volume`→`vol_z` gap)
>    let it silently fall back to an earlier cached day's row while still labeling the result
>    with today's date (the `date_str` param is a caller-supplied display label, never checked
>    against which row was actually used). Manually reproduced before fixing (TDD red/green).
>    Since `RegimeState` drives `AllocationEngine.compute()`'s sizing for every position that
>    day, a silent stale read would have mis-scaled every trade without any signal. Fixed:
>    raises on a date mismatch between the selected row and the intended new bar — its only
>    caller (`_update_regime()` in `main_loop.py`) already wraps it in try/except with a
>    graceful fallback to `current_regime()`, so raising degrades safely rather than crashing
>    the pipeline or silently mis-sizing. 1 new regression test.
> 5. **Confirmed `AllocationEngine`'s regime-based position sizing is genuinely live, not dead
>    code** — the single biggest suspected bug going into this review. Traced real call paths:
>    `AllocationEngine.compute()` is called from all 3 signal-processing sites
>    (`main_loop.py:860,991,1119`) and its `final_position_pct` really flows into
>    `Portfolio.open_position`. No fix needed — this rules out a critical bug, not creates one.
> 6. **Deleted confirmed-dead `bot/scheduler.py`** (247 lines, explicitly self-labeled
>    DEPRECATED, zero non-test references) **and its now-redundant test files.** First-pass
>    dead-code grep excluded all `test_*.py` files and missed that `tests/test_integration.py`
>    (3/3 tests, 100% of the file) still imported and exercised `bot.scheduler.
>    run_morning_pipeline`/`run_eod_snapshot` directly — caught by a remediation subagent
>    before deleting. Verified `orchestration/main_loop.py`'s `run_eod`/`run_morning_pipeline`
>    already have their own current, dedicated coverage in `test_orchestrator.py` (3+ tests
>    each) — `test_integration.py`'s coverage was fully superseded, not unique, so deleted all
>    three files together (`bot/scheduler.py`, `tests/test_scheduler.py`,
>    `tests/test_integration.py`).
> 7. `screener/factor_scorer.py`: `fcf_yield`/`pe_inv`/`pb_inv`/`evebitda_inv` used truthy
>    checks (`fcf and mcap and mcap > 0`) instead of explicit `is not None` checks, silently
>    treating a legitimate `0.0` (e.g. a real zero-FCF company) the same as missing data.
>    Fixed to match the already-correct `de_inv` pattern nearby. 1 new test.
> 8. `bot/portfolio.py`'s `open_position` discarded `cancel_order`'s return value on a
>    non-FILLED order — if the cancel itself failed (not just "already filled"), a stray
>    resting order could become invisible to both the DB and `reconcile_with_broker` (which
>    only diffs *positions*, never outstanding *orders*). Fixed: a failed cancel now fires a
>    distinct CRITICAL alert instead of looking identical to the successful-cancel path. 1 new
>    test.
> 9. `performance/tracker.py`'s `PerformanceTracker` — built specifically to compare live
>    `trading.db` performance against backtest expectations via the same `compute_all` metrics
>    — was defined and exported but never instantiated anywhere outside its own test file.
>    Wired into `bot/analytics.py`'s existing Friday `log_weekly_report()` job so live-vs-
>    backtest comparison now actually runs on a cadence. 2 new tests.
> 10. `tests/test_run_bot.py` expanded (+5 tests) to cover `main()`'s CLI flag dispatch
>     (`--simulated`/`--backtest`/`--test-alerts`) and `run_paper()`'s call sequencing —
>     previously only `_make_broker()` (the paper-only guard) was tested; `run_bot.py` itself
>     untouched (test-only change).
> 11. Two stale-doc fixes found during the review: `system/config.py`'s
>     `target_portfolio_vol_pct` comment said "(informational)" but `main_loop.py` actively
>     multiplies every trade's size by it; `FACTOR_BACKTEST_2026-06-28.md`'s example
>     `_MOMENTUM_WEIGHTS` table was pre-SUE (4 components) while live code has had 5 since
>     2026-07-07 — annotated with a correction rather than rewritten, matching this doc's own
>     style of append-only corrections.
>
> Deliberately not done: `main_loop.py`'s four signal-processing methods (`_process_signal`,
> `_process_insider_signal`, `_process_fundamental_candidate`, hedge entry) are ~70-80%
> byte-for-byte duplicated — a real refactor opportunity (this exact "same fix needed in 3
> copies, forgotten in 2" shape produced the `open_position`/`close_position`/`reduce_position`
> bool-ignored bug class three separate times in this project's history), but too large/risky
> to fold into a blanket fix pass on the live order-placement core; flagged for a dedicated
> session.
>
> Work done via three parallel Explore/general-purpose scoping agents, then 7 parallel
> general-purpose subagents for remediation (plus 2 fixes applied directly). Process note:
> several remediation subagents fell into a self-invented "wait for a background pytest
> monitor" pattern that doesn't exist and had to be explicitly resumed with a synchronous-only
> instruction — worth watching for in future fan-out dispatches, since it silently stalls a
> task rather than erroring. One subagent (dead-code removal) correctly stopped mid-task
> rather than delete on an ambiguous hit, per this repo's own dead-code-check convention.
>
> Full suite after all fixes: **972 passed, 0 failed** (was 975 at session start; the −3 is
> the three deleted dead-code test files, individually accounted for above — no other
> regressions). Commit `e9e0ee7`.
>
> **2026-07-17 (watchdog residual gaps, commits `95ec69f`/`d5d2526`/`d7db50b`/`ac70595`/
> `859b134`):** a follow-up to running `sudo pmset -a powernap 0` (via the osascript
> admin-privileges dialog, faster than opening Terminal) turned into finding and fixing a
> live outage in the watchdog itself. At 11:00:09 the watchdog correctly detected a stale
> deploy and tried to auto-restart — but `restart_bot()`'s launch command used the bare
> string `"python3"`, which under a LaunchAgent's minimal subprocess PATH
> (`/usr/bin:/bin:/usr/sbin:/sbin`, confirmed via `launchctl print`) resolved to the system
> CommandLineTools 3.9 interpreter instead of the Homebrew 3.11+ this project requires,
> crashing immediately on `ImportError: cannot import name 'UTC' from 'datetime'`. The bot
> sat down from 11:00 to 11:13, caught only when a user "is it done?" check ran fresh live
> verification instead of trusting the earlier "done" claim. Fixed: `sys.executable` (the
> watchdog's own already-correct interpreter) instead of the ambiguous string. Verified two
> ways: a new regression test asserting the literal `"python3"` never appears in the launch
> command, and — since the same PATH assumption had just failed once — a REAL (unmocked)
> `restart_bot()` call against the live process, confirming the new PID launched cleanly with
> no traceback and `bot_status.json` updated to the correct commit. Also explicitly checked
> (not assumed) that `nohup`/`caffeinate` DO resolve correctly under the same minimal PATH
> (`ls /usr/bin/nohup /usr/bin/caffeinate` — both present), since guessing on the adjacent
> commands after already being wrong once would have been the same mistake twice.
>
> That incident prompted the user to ask directly: "will it now ever happen again without
> intervention?" The honest answer wasn't "yes" — four residual gaps were named explicitly
> rather than papered over: (1) a full machine reboot with nobody logged in (LaunchAgents
> need an active login session; `defaults read .../autoLoginUser` confirmed auto-login isn't
> configured); (2) an undetected bug in the watchdog's own code (the interpreter bug above
> being proof this isn't hypothetical); (3) a persistent code bug that crashes the bot on
> every restart, which the watchdog would retry forever without ever actually fixing; (4) a
> "stuck but still logging" scenario that never trips the 10-min quiet-gate. The user asked
> for a plan to close all four (`docs/superpowers/plans/2026-07-17-watchdog-residual-gaps.md`).
>
> Before building gap 1's fix, checked `fdesetup status` — FileVault is On, which Apple
> disables the auto-login toggle for outright, ruling out the simplest fix. Presented the
> real alternative (a LaunchDaemon) with its own honest caveat (doesn't cover a truly cold
> boot — FileVault's pre-boot password gate runs before any launchd domain starts, system or
> gui, and nothing in software can skip it) and got explicit user sign-off before building,
> since it's a real architecture change with a security-adjacent tradeoff, not a pure
> engineering call.
>
> Built all four: (1) `monitoring/watchdog.py`'s LaunchAgent replaced with a LaunchDaemon at
> `/Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist` (root-owned, `chmod
> 644`, `UserName: thomasvromen` so it still runs as the user, not root), installed via the
> same osascript admin-privileges pattern as the powernap fix (no Terminal/TTY needed); the
> old per-login LaunchAgent was unloaded and its plist deleted (running both would have
> double-fired on the same trigger). Live-verified during install, not just loaded-and-hoped:
> its `RunAtLoad` cycle immediately found a real stale deploy (a concurrent session's
> commits) and successfully auto-restarted the bot end-to-end, confirmed via `bot_status.json`
> showing the new PID and matching commit, and file ownership confirmed `thomasvromen:staff`
> (not root), proving the `UserName` key worked as intended.
> (2) `main()` wrapped in try/except that fires a `watchdog_crashed` alert on any unhandled
> exception instead of failing silently until the next 15-min cycle; `monitoring/
> dead_mans_switch.py` (an independent process, so it can't share a blind spot with the
> watchdog) gained `check_watchdog_freshness()`, alerting `dead_mans_switch_watchdog` if
> `watchdog.log` goes stale beyond ~2.5x the watchdog's own interval (40 min) — this is the
> layer that catches the watchdog itself failing to fail loudly.
> (3) a `watchdog_restart_history.json`-backed circuit breaker: `_recent_restart_count()`
> checks how many restarts happened in the trailing 60 minutes before `restart_bot()` acts;
> 3 or more suppresses further auto-restart entirely and fires a distinct
> `watchdog_crash_loop` alert, because retrying every 15 minutes forever against a genuine
> code bug wastes the alerting channel and can't fix anything a restart doesn't fix.
> (4) `find_overdue_job()` gained a `grace_minutes` parameter (default unchanged); a new
> 120-minute hard ceiling is checked in `check_and_recover()` BEFORE the quiet-gate, so
> something that keeps producing log output without ever completing a real job for 2+ hours
> gets restarted anyway instead of silently exploiting the quiet-gate's safety intent forever.
>
> Caught the same test-isolation bug class twice more while implementing, both flagged in the
> plan's self-review before they were hit rather than discovered by surprise: 3 existing
> `restart_bot` tests didn't mock the new `_record_restart` and would have written real
> entries into the repo's actual `watchdog_restart_history.json` on every test run (same
> class of bug as the earlier `write_status_file` test-isolation fix); and 2 existing
> `check_and_recover` tests mocked `find_overdue_job` with a bare `return_value` that would
> have silently returned the same result for both the new hard-ceiling call and the original
> soft-grace call, breaking their intended semantics — fixed with `grace_minutes`-aware
> `side_effect` callables instead.
>
> Final live state, checked fresh after all four gap-fixes landed: full suite **985 passed**
> (was 975 before this work began; net includes both the interpreter-bug regression test and
> all four gap-closing test suites), `powernap` confirmed `0` on both power sources, watchdog
> LaunchDaemon confirmed registered in the `system` domain via `launchctl print`. One thing
> deliberately NOT forced: a manual `launchctl kickstart` to prove the final code was live was
> offered via the osascript dialog and the user cancelled it — respected without retrying;
> the daemon's own natural 15-minute cycle will pick up the latest commit on its own, which is
> the entire point of the system being built. What's still honestly open, stated plainly
> rather than hedged: a cold boot from fully powered off (FileVault, unavoidable) and the
> possibility of a not-yet-found bug in the watchdog's own code (one was already found and
> fixed live; the process that caught it — actual verification instead of trusting a "done"
> claim — is the real mitigation, not a promise of zero remaining bugs).
>
> **2026-07-17 (second live outage, "update the whole folder" close-out, commits
> `a9a7313`/`79f9b0b`/`c0ad57a`):** proof the previous entry's caution was warranted — a
> SECOND bug in the watchdog's own restart mechanism surfaced live within the same session,
> this time while responding to the user's request to update all documentation and confirm
> readiness for an imminent full reboot. The 15:37 auto-restart attempt failed with `nohup:
> can't detach from console: Inappropriate ioctl for device` — a genuine `LaunchDaemon`
> invocation has no controlling terminal at all, and `nohup` needs one to detach FROM; it
> exits before ever exec'ing python. The first `RunAtLoad` fire during Task 1's earlier
> install had apparently inherited enough of a console from the interactive osascript
> installation flow to mask this — the bug only showed up on a genuinely cold, natural
> `StartInterval` cycle. The bot sat down through two full watchdog cycles (15:37, 15:52,
> each retrying and failing identically) before this was caught. Fixed: dropped `nohup`
> entirely from the launch command — `start_new_session=True` already performs its actual
> function (`os.setsid()` detaches from any controlling terminal, so there is no terminal
> left to send `SIGHUP` from in the first place). Verified two ways: a new regression test
> asserting `"nohup"` never appears in the launch command, and direct production evidence —
> the very next natural cycle (16:07) launched the bot cleanly with a full startup log and no
> `nohup` error.
>
> That 16:07 process then hit a cluster of real errors — `sqlite3.OperationalError: unable
> to open database file`, `[Errno 24] Too many open files`, and DNS resolution failing
> simultaneously for sec.gov, Alpaca, and Slack. Diagnosed as transient resource exhaustion
> from the day's accumulated restart churn (also correlated with the coding agent's own tool
> layer intermittently failing to execute commands around the same window) rather than a new
> code bug — confirmed resolved minutes later (`host www.google.com` resolved cleanly, a
> direct `sqlite3` query against `trading.db` succeeded) and expected to clear unconditionally
> on the user's planned reboot regardless, since that resets the file-descriptor table and
> network stack from scratch. Not treated as "fixed" because nothing was changed to fix it —
> reported as a transient condition with direct evidence it had already cleared.
>
> Caught the same test-isolation bug class for a third time this session, on the read side
> this time: 3 `restart_bot` tests didn't mock `_recent_restart_count`, so once
> `watchdog_restart_history.json` accumulated real entries from today's actual production
> restarts, the tests started hitting the real crash-loop-suppression path — 2 failed
> outright, a third passed for the wrong reason (`kill` not called because of suppression,
> not because of the cmdline-mismatch safety check it claims to verify). Fixed by mocking the
> read side (`_recent_restart_count`) alongside the write side (`_record_restart`) already
> fixed earlier in the session.
>
> Converted `monitoring/dead_mans_switch.py` from a `LaunchAgent` to a `LaunchDaemon` too —
> same install pattern, same reboot-survival reasoning as the watchdog. Necessary for
> consistency, not optional polish: the watchdog surviving reboot while its own independent
> backstop (built specifically to catch a bug IN the watchdog) did not would have reopened
> exactly the kind of gap this whole session was closing. Live-verified via its own fresh
> `RunAtLoad` cycle immediately reporting "Pipeline healthy" after install, and the old
> LaunchAgent unloaded and its plist deleted to prevent double-firing, same as the watchdog's
> own conversion.
>
> Also, sweeping the rest of the repo per the user's "don't forget any folder" request:
> corrected a stale claim in `docs/guardrails/PROJECT.md` (said `ALERT_WEBHOOK_URL` was
> still outstanding; it has been set in `.env` the whole time — nobody had corrected the note
> after it was actually configured), added an explicit `docs/RUNBOOK.md#after-a-reboot`
> checklist naming exactly what to expect and verify post-restart, and documented all 4 new
> watchdog/dead-man's-switch alert types (`watchdog_restart`, `watchdog_crashed`,
> `watchdog_crash_loop`, `dead_mans_switch_watchdog`) in the existing alert-meanings
> reference so they don't read as unexplained noise the first time they fire. Left
> deliberately untouched and flagged instead: a second, separate `trading bot/docs/STATE.md`
> created by the concurrent strategy-review session, which duplicates this repo's
> single-root-STATE.md convention — not this thread's file to merge or delete unilaterally.
> Full suite: **985 passed** (unchanged count — this pass was fixes and documentation, not
> new features).
> 2026-07-20 (short-selling holistic branch review + remediation): first whole-diff review of
> the completed short-selling branch (`3f77432..HEAD`, 29 commits, still flag-off) as one
> unit, after 13 task-scoped implementer→spec→quality review cycles never looked at it that
> way. Confirmed the core safety guarantee (flag off → zero behavior change) holds under an
> independent trace, and that the direction-blind-close bug class already fixed twice in this
> branch (`997fe35`/`6bb12f9`) does not recur a third time anywhere in the diff. Found and
> fixed 5 issues, none of them flag-off-behavior-changing: (1) `orchestration/main_loop.py`'s
> sector-allocation seeding (two call sites: the entry-pipeline seed and the hedge-plan seed)
> summed signed `qty * price` per sector — pre-existing code untouched by any short-selling
> task, so no per-task review caught it — meaning a short's negative qty netted against a
> same-sector long and silently masked true concentration from `max_sector_pct`; both now use
> `abs(qty)` for gross exposure. (2) `_process_fundamental_short_candidate` opened positions
> without ever calling `insert_fundamental_signal`, unlike its long-side counterpart — added a
> `direction` column to `fundamental_signals` (migration 10) and threaded it through
> `insert_fundamental_signal`/`get_fundamental_signals`, filtering the latter to
> `direction='long'` so `run_bot.py --backtest` (the sole consumer) keeps its exact current
> result set and a short row's inverted `expected_return_pct` sign convention can never
> silently corrupt it. (3) `reconcile_with_broker`'s `auto_flatten_untracked=True` branch
> correctly skipped selling into an untracked short (guarded on `qty > 0`) but still emitted
> an "auto-flattened" alert even when nothing was closed — split the branch so the alert only
> claims success when a sell actually fired; the untracked-short case now says "left OPEN...
> manual review required" at CRITICAL. (4) `RiskManager.validate_order`'s per-position cap
> check used the long `max_position_pct` unconditionally — harmless today only because callers
> pre-clamp to the short cap and `Settings.validate()` enforces short≤long, but not
> semantically short-aware; added a `direction` param (default `"long"`, every existing caller
> unaffected) and use `max_short_position_pct` for `direction="short"`. (5) The design spec's
> explicit requirement to live-verify Alpaca's negative-qty-for-shorts sign convention against
> a real paper account was never done/recorded — since that verification itself needs live
> credentials this session doesn't have, converted the assumption into a standing runtime
> self-check instead: `enforce_stop_losses` now compares each tracked position's DB `direction`
> against the broker's live qty sign every poll and fires a CRITICAL alert on any disagreement
> (proceeding with the DB-recorded direction), rather than trusting the sign forever unverified.
> **The live-account verification itself is still not done** — this only guarantees it fails
> loud instead of silently the first time a real short position exists. All 5 fixes are inert
> while `enable_short_selling=False` (verified: none of them can fire without a short position
> existing, and none can exist with the flag off). 10 new regression tests, one per fix (two
> for #4 and #5 each — cap-exceeded/cap-respected, mismatch-fires/no-false-positive). Test
> count: **1055 passed** (was 1045; +10, zero regressions, zero failures).
> 2026-07-20/21 (live P&L review + capital-deployment fix + reliability mitigation): reviewed
> the live paper account's trade history at the user's request. No closed trades exist yet —
> the sole `closed_positions` row (`ZERO`, entry $0, exit_reason `test`) predates the 07-06
> live launch and is leftover test data, not a real trade (flagged, not deleted). All P&L is
> unrealized: 20 open positions, +$445.22 on $37,207.64 deployed (+1.20%). Portfolio vs. SPY
> since inception (2026-07-07, $100k): bot ≈ -2.75%, SPY -0.75% — bot trailing by ~2pts; too
> early (13 days, 0 closed trades) to call it an edge either way. Found the bot was hitting
> `max_positions` (20/20) while only ~39% of NAV was deployed — well under `max_invested_pct`
> (80%) — so the position **count**, not capital, was the binding constraint. Fix: raised
> `max_positions` 20→30 and `per_trade_risk_pct` 0.15→0.20 (`system/config.py`) — kept below
> 0.30 because that collided with the separate `_CONGRESSIONAL_MAX_PCT=3.0` cap on
> congressional-sourced signals in 2 test fixtures (recalibrated their ATR/mock inputs to
> realistic values below the cap; no assertions weakened). Also found and fixed a real,
> independent bug while re-verifying: `backtesting/simulation.py`'s `simulate_portfolio()` had
> its own hardcoded `max_positions=20` default, completely decoupled from
> `system.config.RiskConfig` — a backtest run would have silently ignored the config change
> (unlike `per_trade_risk_pct`/`max_position_pct`, which already correctly fell back to live
> `_settings`). Threaded `max_positions` through the same optional-override pattern at all 3
> `simulate_portfolio()` call sites (`run_strategy_backtest.py`, `walk_forward.py` ×2). Ran a
> live-monitoring check the same session and found the bot had silently missed 2 scheduled
> jobs overnight (`run_intraday_check` 20:00, `run_eod` 22:30 CEST, 2026-07-20) — zero
> `job_runs` row for either, zero alert, matches this project's long-documented scheduler-wedge
> pattern (likely a sleep event; `pmset`: 148 sleep/wakes since boot). The watchdog that used
> to auto-recover from this was turned off 2026-07-20 (see the entry above), so nothing caught
> it until a manual check the next morning. User explicitly chose NOT to re-attempt
> `sudo sfltool resetbtm`/launchd this session (lower-risk option instead) — so fixed only the
> narrower gap: `_on_job_missed` (`orchestration/main_loop.py`) now calls `fire_alert()`,
> mirroring `_on_job_error`'s already-working pattern, so a missed job posts to the configured
> Slack webhook (`ALERT_WEBHOOK_URL`) instead of only logging a WARNING line nobody was
> reading. This is alert-only — nothing auto-restarts; a wedge still needs a manual restart.
> Concurrently, the user (separately) found and fixed a real live bug: `gpt-5.4` rejects the
> legacy `max_tokens` param 100% of the time (confirmed in 07-20/21 logs, every OpenAI call ate
> a guaranteed 400 before succeeding on retry) — now sends `max_completion_tokens` directly,
> dead retry path removed (`bot/ai_analyst.py`, commit `d5a7a6b`). Bot restarted twice this
> session to load these changes in sequence; final restart confirmed on commit `cb88ce5`
> (PID 54604), scheduler started clean, nothing mid-job at restart time either time. Test
> count: **1057 passed** (was 1056 at session start; +1, the new job-missed-alert test).
> 2026-07-21 (two ORDER_REJECTED bugs found and fixed, live-reproduced): user reported a batch
> of Slack `ORDER_REJECTED` alerts from the prior evening's `enforce_stop_losses()` poll
> (T, VICI, VZ, PSQ, RWM, SH); `bot.log`'s history of the actual Alpaca error text was already
> gone (no rotation — every restart truncates it), but the identical alert class fired again
> live the same day, giving real evidence. Two distinct root causes, not one: (A) trailing-stop
> qty ratchet — `bot/portfolio.py`'s trail-up always requested the position's full CURRENT qty
> for the new stop before cancelling the old one (by design, to avoid a stop-free gap), but if
> the live qty had grown past what the still-resting old stop already reserved (confirmed via
> Alpaca's own error JSON: HIG `requested: 11.79, available: 1.79, held_for_orders: 10`), the
> new order could never get enough "available" qty — a permanent per-ticker failure loop, since
> the old stop is GTC/never expires and is only ever cancelled AFTER a successful new
> placement, which can now never happen. Fixed: on a placement failure with a known old stop
> id, cancel it and retry once (frees the held qty); if the retry also fails, re-place a stop
> at the OLD price so the position is never left fully naked, and alert either way. (B) initial
> stop wash-trade rejections (`EXE`/`APA`/`ADBE`, 3 of 5 new entries the same day) — the
> existing 3x/~3s retry (added 2026-07-14 for this exact Alpaca fill-state-propagation lag) is
> proving insufficient in practice; widened `_place_stop_with_retry` to 5 attempts / ~20s total
> (2+4+6+8s). 2 new regression tests for (A), proven red against the pre-fix code then green;
> 1 existing test for (B) updated to the new retry count (intended change, not a weakened
> assertion). Test count: **1058 passed** (was 1057; +1 net — one old test replaced by two new
> ones for (A), one existing test's call-count updated for (B)).
> 2026-07-22 (congressional signal disabled — item 1 of 6 from `docs/BOT_REVIEW_2026-07-20.md`):
> unlike insider signals (`InsiderConfig.enabled`), the congressional signal had no on/off
> switch — `run_scraper()` and the Phase 2 entry block in `orchestration/main_loop.py` ran
> unconditionally, gated only by size/frequency caps (`_CONGRESSIONAL_MAX_PCT`/
> `_CONGRESSIONAL_MAX_PER_DAY`). User decision: fully disable, given the 2026-07-17 real-data
> finding (excess return -0.636% at 1mo, t=-2.57; -2.538% at 3mo, t=-4.93) satisfies
> `docs/CONGRESSIONAL_EDGE.md`'s own pre-written decision rule ("Incremental alpha < 0 after
> costs → Drop congressional layer"). Added `CongressionalConfig(enabled: bool = False)` to
> `system/config.py`, mirroring `InsiderConfig`'s shape. In `main_loop.py`: `congress_tickers`
> (feeds Phase 1's "both"-signal-type conviction boost) is now forced to an empty set when the
> flag is off, and the Phase 2 congressional-entry loop is wrapped in the same `if enabled:`
> gate `InsiderConfig` already uses for Phase 2.5 — both effects needed gating, not just one,
> since a partial disable (Phase 2 only) would have left the Phase 1 conviction boost live.
> `run_scraper()` itself stays unconditional — disclosures keep getting persisted for possible
> future re-evaluation, only their use as a trading signal is gated. One pre-existing test
> (`test_congressional_phase_receives_fundamental_ticker_set`) assumed Phase 2 ran by default;
> updated to explicitly opt in via `dataclasses.replace`, matching the existing
> `enable_short_selling` on/off test pattern — not a weakened assertion, its premise changed.
> Two new orchestrator tests added (flag off: Phase 2 skipped AND `congress_tickers` empty;
> flag on: Phase 2 runs AND `congress_tickers` populated), plus a config default-value test.
> `docs/CONGRESSIONAL_EDGE.md` updated with a "Decision (2026-07-22)" section recording the
> outcome against its own pre-written rule. Test count: **1061 passed** (was 1058; +3 new).
> 2026-07-22 (short-selling prep, item 2a of 6 from `docs/BOT_REVIEW_2026-07-20.md`: aggregate
> gross/net exposure cap): the design spec's 3rd open question — per-position caps were already
> direction-aware and the sector-concentration cap was already fixed to use gross (`abs(qty)`)
> exposure (2026-07-20 short-selling review), but the one remaining aggregate check —
> `RiskConfig.max_invested_pct` (80%) — was not. All 5 call sites in
> `orchestration/main_loop.py` (`run_morning_pipeline`'s top-level `_at_capacity` gate, plus
> `_process_signal`/`_process_insider_signal`/`_process_fundamental_candidate`/
> `_process_fundamental_short_candidate`'s per-order veto checks) computed
> `sum(p["qty"] * p["current_price"] for p in positions)` — signed. A short's negative qty
> subtracted from this sum instead of adding to it, so e.g. a 100%-long+90%-short book (NAV
> $110k on $100k cash, from a $100k long and a $90k short) would compute ~9% net "invested"
> against the 80% cap instead of the true ~173% gross exposure — nowhere close to tripping a
> cap meant to bound total capital at risk. Fixed: each site now computes a separate
> `_gross_invested_usd`/gross sum using `abs(qty)` for the cap-check ratio; NAV itself is left
> signed (correct net-equity accounting — a short's mark-to-market liability genuinely does
> reduce NAV as price rises, unlike the exposure-measurement bug this fixes). Dormant today
> (`enable_short_selling` still `False`, so no live position can currently be short), but this
> was a real gap that would have applied from the moment the flag is ever flipped on — fixed
> as a prerequisite before that, per the user's explicit decision to close all 5 open
> short-selling design questions before considering activation. One new orchestrator test
> (`test_invested_pct_gate_uses_gross_not_net_exposure`) — first version was accidentally
> vacuous (the fixture's default empty `run_factor_screen` mock meant the assertion held
> regardless of the gate's correctness); caught by the red/green proof discipline, fixed by
> mocking a non-empty candidate list, then proven red against the pre-fix line and green
> after. Test count: **1062 passed** (was 1061; +1 new).
> 2026-07-22 (short-selling prep, item 2b of 6: regime-aware short sizing): the design spec's
> 1st open question. `_process_fundamental_short_candidate` (`orchestration/main_loop.py`)
> sized shorts as `min(score.position_pct, max_short_position_pct)` — the LLM's suggested
> size, capped, with zero regime input at all (comment: "deliberately simpler than the long
> path's ATR/regime/correlation/portfolio-vol gate stack, per the design spec's scope" — an
> intentional v1 simplification, not an oversight). `regime/allocation_engine.py`'s
> `AllocationEngine.compute()` had no `direction` parameter, so even a direct call would have
> applied the long-tuned `regime_size_multiplier` table (0.3x in crash) — the opposite of the
> spec's stated intent ("shorts arguably should size up in bearish/crash regimes and down in
> bullish ones"). Added `direction: str = "long"` to `compute()`; when `"short"`, it now reads
> a new `AllocationConfig.short_regime_size_multiplier` table (crash/deep-bear=1.0,
> bear=0.75, neutral=0.5, euphoria=0.5, bull/melt-up=0.3 — inverse-shaped vs. the long table)
> and caps against `RiskConfig.max_short_position_pct` instead of `max_position_pct`. Wired
> into `_process_fundamental_short_candidate`: when `self._regime_state` is set, calls
> `self._alloc.compute(ticker, score.position_pct, self._regime_state, direction="short")`
> and rejects (SIGNAL_REJECTED) if the regime-scaled size drops below the economic-size floor,
> mirroring the long paths' existing pattern exactly; falls back to the raw LLM size when no
> regime state exists yet (same fallback the long paths use). These multiplier values are an
> explicitly-flagged principled default, not a backtested calibration — Phase 0's PIT-data
> gate blocks any real short-side backtest today (`docs/PHASE0_FINDINGS.md`), so there is no
> data to calibrate against yet; revisit once that gate clears. 5 new tests
> (`tests/test_allocation_seasonality.py` x3 — inverse-table selection, short-specific cap,
> unchanged default-long behavior; `tests/test_config.py` x1 — default table shape;
> `tests/test_orchestrator.py` x1 — crash sizes a short short-candidate larger than bull,
> proven red against the pre-fix code then green). Test count: **1067 passed** (was 1062;
> +5 new).
> 2026-07-22 (short-selling prep, item 2c of 6: hedge-mechanism overlap): the design spec's
> 2nd open question. `hedge/hedge_engine.py`'s `compute_hedge_plan()` sized the broad
> inverse-ETF hedge purely off the regime's `max_inverse_pct_by_regime` cap — zero awareness
> of open per-stock short positions, even though both mechanisms express the same bearish
> thesis and could double-count exposure. Added `existing_short_pct: float = 0.0` param;
> `max_alloc = max(max_alloc - existing_short_pct, 0.0)`, and returns `[]` outright (with a
> log line) rather than attempting near-zero per-ETF orders when existing shorts already
> cover the regime's cap. `orchestration/main_loop.py`'s `_run_hedge_pass` computes
> `existing_short_pct` in the same broker-positions-cross-referenced-with-DB-`direction` loop
> already building `sector_allocation` — a short's negative qty is summed via `abs()` (gross,
> matching the existing sector/invested-pct precedent) and expressed as % of NAV; hedge ETF
> positions are naturally excluded since they're bought long (`direction="long"`) even though
> they're inverse instruments. 4 new tests (`tests/test_hedge_engine.py` x3 — reduces alloc
> correctly, returns `[]` when fully covered, unchanged when the param is omitted;
> `tests/test_orchestrator.py` x1 — real DB position row, direction="short", correct % of
> NAV computed and passed through). 3 of the 4 proven red (old code ignored the reduction)
> then green; the 4th (default-zero) is itself the no-behavior-change regression guard. Test
> count: **1071 passed** (was 1067; +4 new).
> 2026-07-22 (short-selling prep, item 2d of 6: short borrow fees): the design spec's 4th open
> question. `_process_fundamental_short_candidate` passed `score_entry_short` the bare
> `_ESTIMATED_COST_PCT` module constant — identical to every long call site, with no borrow-fee
> term anywhere despite the spec explicitly flagging that short borrow costs can be materially
> higher than the ~0.10% long round-trip estimate for some names. Added
> `ExecutionConfig.short_borrow_cost_pct` (default 0.5, a flat addend) applied only at the
> short candidate's `score_entry_short` call site:
> `estimated_cost_pct=_ESTIMATED_COST_PCT + self._cfg.execution.short_borrow_cost_pct`. Real
> per-name stock-borrow rates vary widely and Alpaca's paper API doesn't expose them, so this
> is documented explicitly as a conservative flat approximation, not a real per-name model —
> consistent with this repo's convention of being honest about what's calibrated vs. assumed
> (e.g. the SUE PIT backtest's honest null result). 2 new tests (`tests/test_orchestrator.py`
> — the short candidate's cost hurdle equals constant+addend, proven red against the pre-fix
> line then green; `tests/test_config.py` — default is a positive addend). Test count: **1073
> passed** (was 1071; +2 new).
> 2026-07-23 (short-selling prep, item 2e of 6: SimulatedBroker sell-to-open support): the
> design spec's 5th and final open question. `execution/paper_broker.py`'s `SimulatedBroker`
> had no way to open a short at all — `_apply_fill`'s SELL branch unconditionally raised
> `"Cannot sell {ticker}: no position held"` for any ticker not already held, and the class had
> neither `shorting_enabled()` nor `is_shortable()` (only `AlpacaBroker` did) — so
> `Portfolio.open_position`'s short branch (which calls both unconditionally at
> `bot/portfolio.py:61-64`) would `AttributeError` immediately against a `SimulatedBroker`,
> confirming the spec's claim exactly. Added a `shorting_enabled: bool = False` constructor
> flag — default `False` preserves every existing caller's behavior byte-for-byte (a sell with
> no position still raises exactly as before) — and `shorting_enabled()`/`is_shortable()`
> methods that both just return the flag (this simulator doesn't model per-name
> hard-to-borrow/restricted-list status, the same simplification already applied to short
> borrow fees in item 2d). Rewrote `_apply_fill` to branch on the *existing position's sign*
> rather than just BUY/SELL: BUY-with-existing-short is now buy-to-cover (reduces the negative
> qty toward zero, deletes the position once fully covered — mirrors AlpacaBroker's convention
> that a short's close is a buy); SELL-with-no-existing-position now opens a short (negative
> qty) when `shorting_enabled` is True, still raising the original error when False;
> SELL-with-existing-short adds to it (weighted-average entry price, same formula shape as the
> existing add-to-long branch generalized to `abs()` magnitudes). The pre-existing
> BUY-no-existing / BUY-existing-long / SELL-existing-long branches keep their exact original
> formulas — verified by reading the full diff line-by-line before landing, not just by tests
> passing. Proceeds from a short sale are credited to cash immediately; this simulator does not
> model margin/collateral requirements for short positions (documented explicitly, not a silent
> gap). `run_bot.py`'s `_make_broker` now passes
> `shorting_enabled=settings.strategy.enable_short_selling` when constructing `SimulatedBroker`
> for `--simulated` mode, mirroring the same flag's live-mode gating. 12 new tests in
> `tests/test_paper_broker.py` (sell-to-open, cash crediting, equity direction on a price move,
> full and partial buy-to-cover, capping filled_qty at the held short size, adding to an
> existing short, and an explicit check that ordinary long buy/sell is byte-identical on a
> shorting-enabled broker) — 7 proven red against the pre-fix code then green; the rest
> (shorting-disabled-by-default preservation, `is_shortable`/`shorting_enabled` getters,
> long-side unaffected) are regression/interface guards. Full suite as run this session:
> **1087 passed, 1 deselected** — the deselect is
> `tests/test_heartbeat.py::test_start_heartbeat_writes_thread_dump_file`, a real-time
> background-thread-timing test in a concurrent, uncommitted session's new
> `monitoring/heartbeat.py` (unrelated to this work, passes 3/3 standalone, flaky only under
> full-suite load — not this task's file to fix). No clean pre-heartbeat checkpoint exists to
> state an exact delta against, since that file landed on disk concurrently with this step.
> 2026-07-23 (Russell 1000 closed out as accepted scope, item 4 of 6 from
> `docs/BOT_REVIEW_2026-07-20.md`, doc-only): broadening past S&P 500 needs a new data-provider
> integration (FMP was the candidate) with zero existing code today, plus a paid/unknown-cost
> account — no free/no-signup source was found viable after ~7 attempts across sessions
> (iShares CSV serves bot-protection HTML, FTSE Russell redirects, stockanalysis.com is a
> JS-rendered shell with no exposed API, SlickCharts 403s). User decided to accept S&P-500-only
> scope rather than keep pursuing this — closed out as a deliberate choice, not a lingering open
> bug. `docs/DATA_SOURCES.md`'s Russell 1000 row (stale "Active" status, contradicted the
> logged live failure) corrected with the decision and reasoning; root `docs/STATE.md`'s
> corresponding `## Next`/`## Open items` bullets closed. No code changed —
> `bot/universe.py::_build_universe`'s existing S&P-500 fallback is now the bot's permanent
> behavior, not a degraded state pending a fix.
