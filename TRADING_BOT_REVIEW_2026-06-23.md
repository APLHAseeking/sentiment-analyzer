# Trading Bot Review — 2026-06-23

**Scope:** full fresh review of `trading bot/`, superseding `TRADING_BOT_REVIEW.md` (2026-06-14), `TRADING_BOT_FULL_REVIEW_BUNDLE.md` (2026-06-14), `TRADING_BOT_REVIEW_PLAN.md` (2026-05-28), and `TRADING_BOT_REVIEW_2026-06-22.md` (2026-06-22).
**Method:** two stages. Stage A re-verified all 18 items flagged by the 2026-06-22 audit directly against current HEAD (file:line, not commit messages). Stage B ran 5 parallel domain-scoped fresh sweeps (Risk+Execution; Regime+Features+Hedge; AI/LLM; Backtesting+Performance; Data/Scrapers/Orchestration) covering every module in the bot, each including a "does this test assert broken behavior as correct" audit.
**Test count:** 708 tests collected (`pytest --collect-only -q`), matching the bot's own `CLAUDE.md` banner — not stale.

## Executive summary

The 2026-06-22 remediation pass was thorough: **all 18** previously-flagged items (5 "not fixed" + 9 "partial" + 4 new/residual) are now genuinely fixed in current HEAD, verified by direct source inspection, not by trusting commit messages or the prior doc.

The fresh sweep found **5 new Critical bugs**, all pre-existing (not introduced by the 2026-06-22 pass) and none previously documented:

1. A sign/log-domain math error in the HMM's Baum-Welch E-step that silently degrades learned regime persistence on realistic (noisy/overlapping) data.
2. The new trail-stop ordering (place-then-cancel) cancels the **wrong** stop on both real broker implementations — the position ends up with **zero resting stops** after every trail-up, the exact failure mode the 2026-06-22 fix for item #9 was meant to prevent, reintroduced by a different mechanism.
3. A `str(enum)` comparison bug means `cancel_stop_order`/`get_stop_orders` never match anything against the real Alpaca SDK, so stop cancellation is silently inert in live/paper trading (compounds with #2).
4. A circuit-breaker ordering bug: once a weekly halt is active, the deleverage check is never reached again that week, even if a later day independently crosses the deleverage threshold.
5. The HTML-fallback scraper never normalizes `transaction_type` to the `buy`/`sell` vocabulary the signal pipeline requires — during exactly the failure mode (JSON API down) this fallback exists to handle, congressional signal is silently and totally lost with no alert.

Findings by severity: **5 Critical, 7 High, 13 Medium, 13 Low** (new findings only; the 18 previously-flagged items are re-verified fixed, not recounted here).

**Most valuable cross-cutting pattern, again:** several Critical/High bugs have zero or actively-misleading test coverage — `RiskState.DELEVERAGE` has no test at all; the trail-stop mock doesn't reproduce either real broker's cancel semantics; `test_broker.py` passes raw strings instead of real SDK enums into mocks, masking the `str(enum)` bug; and `test_scraper.py` asserts `"purchase"` as correct output for the HTML path, the same vocabulary bug already fixed on the JSON path two reviews ago.

---

## Part 1 — Re-verification of all 18 items from TRADING_BOT_REVIEW_2026-06-22.md

All 18 confirmed **FIXED** in current HEAD by direct source inspection (file:line evidence below), consistent with the bot's own `CLAUDE.md` status banner.

| # | Finding | Evidence of fix |
|---|---|---|
| 1 | Sector cap off-by-one | `risk/risk_manager.py:277-278` — `sector_allocation.get(sector, 0.0) + position_pct > max_sector_pct` |
| 2 | ADV gate never blocks | `risk/risk_manager.py:298-315` — hard-rejects on missing/zero ADV (hedge sector exempt) |
| 3 | Stop fills never booked | `bot/portfolio.py:212-235, 265-287` — `_find_matching_fill()` (uses `get_order_history()`) is called from `reconcile_with_broker`, books via `_book_closed_position` before any delete |
| 4 | Feature padding positional | `features/feature_pipeline.py:146-190` (`align_features_to_scaler`) + `regime/hmm_engine.py:40-56` (`_pad_features_to_scaler` delegates to it) — single shared column-name-aware path |
| 5 | Single-restart EM | `regime/gaussian_hmm.py:26,39,59-85` — `DEFAULT_N_RESTARTS=5`, best-of-N by log-likelihood, derived seeds |
| 6 | ATR fallback thin-history path | `risk/position_sizing.py:99` default `fallback=10.0`, matches `main_loop.py`'s `_ATR_FALLBACK_PCT=10.0` (docstring states the alignment explicitly) |
| 7 | Paper guard single site | `orchestration/main_loop.py:164-172` — `initialize()` independently raises `RuntimeError` if `not broker.is_paper` |
| 8 | Fill timeout silent fallback | `bot/broker.py:145-159` — emits `SLIPPAGE_HIGH` alert before falling back to quote price |
| 9 | Stop replace failure unhandled | `bot/portfolio.py:392-411` — checks `place_stop_order`'s return value; keeps old stop + alerts if new placement failed |
| 10 | Dashboard paths raw literals | `dashboard/app.py:28,34-35` — `DB_PATH`/`STATE_PATH` routed through `system.paths.resolve()` |
| 11 | Stability tracking silent default | `regime/hmm_engine.py:255-260` — `update_recent_labels` is now a required keyword-only param, no default |
| 12 | LLM refusal/empty-content uncaught | `bot/ai_analyst.py:419-443` — both OpenAI (`content is None`) and Anthropic (empty `content` list) raise `ValueError`, caught/retried by `_call_with_retry` |
| 13 | Russell 1000 ticker normalization missed | `bot/universe.py:55` — same `_normalize_ticker()` applied to both S&P 500 and Russell 1000 |
| 14 | `compute_rsi` flat series → 100 | `technical/indicators.py:71-88` — flat (`avg_loss==0 and avg_gain==0`) → 50.0, distinct from all-gains → 100.0 |
| 15 | `rs_line_slope` no zero-guard | `technical/indicators.py:298-306` — guards `bench_past==0 or bench_now==0` → `"flat"` |
| 16 | `peak_price` uses `or` not `is not None` | `bot/portfolio.py:377-378` — explicit `is not None` check |
| 17 | Hardcoded `"Hedge"` string duplicated | `risk/risk_manager.py:33` defines `HEDGE_SECTOR_LABEL`; `main_loop.py:61` imports the same constant |
| 18 | `gpt-5.4` temp/seed reasoning-tier | `bot/ai_analyst.py:399-418` — reactive retry-without-params on `BadRequestError`; unchanged by design (Low/forward-looking, failure mode is a loud safe error) |

---

## Part 2 — New findings from the fresh sweep

### Critical

- **risk/risk_manager.py:125-151** — `check_circuit_breakers`'s `WEEKLY_HALT` branch returns unconditionally before the `DELEVERAGE` branch is reached; once active, it stays active all week (cleared only at `start_of_day`), so a later day's independent deleverage-level loss never triggers `RiskState.DELEVERAGE`. *Trigger:* weekly loss crosses the weekly-halt threshold early in the week, then a later day in the same week independently loses ≥ the deleverage threshold — the force-close-all-positions path in `main_loop.py` (gated on `state == DELEVERAGE`) never runs, leaving the book fully exposed through a catastrophic day.
- **bot/portfolio.py:392-400 + execution/paper_broker.py:190-198 + bot/broker.py:227-247** — The place-new-before-cancel-old stop ordering assumes the broker can tell the new stop apart from the old one; neither can. `SimulatedBroker` overwrites a single per-ticker slot, so `cancel_stop_order` deletes the just-placed stop. `AlpacaBroker.cancel_stop_order` cancels *every* matching new/accepted stop order for that symbol, including the one just submitted. *Trigger:* any trail-up against a real (non-mocked) broker — the position ends every poll with zero resting stops.
- **bot/broker.py:232-233,243** — `cancel_stop_order`/`get_stop_orders` compare `str(order_type)`/`str(status)` against real `alpaca-py` enum members, which `str()` to `"OrderType.STOP"`/`"OrderStatus.NEW"`, not `"stop"`/`"new"` — confirmed empirically against the installed SDK. Both comparisons always evaluate `False`. *Trigger:* any real (non-`MagicMock`) Alpaca session — `get_stop_orders()` always returns `{}`, `cancel_stop_order()` never cancels anything; combined with the bug above, stops accumulate at the broker indefinitely.
- **regime/gaussian_hmm.py:112** — The Baum-Welch E-step's `log_xi` adds the raw (linear-scale) `transmat_` to log-domain terms instead of `np.log(transmat_ + eps)`, which the file's own `_forward`/`_backward` correctly use elsewhere. Verified numerically: on noisy/overlapping synthetic regime data (the realistic case for daily equity returns), this collapses a true sticky transition matrix toward near-uniform, destroying learned regime persistence; masked on well-separated toy data, which is why no existing test caught it. Predates the 2026-06-22 pass. *Trigger:* any `GaussianHMM.fit()` call where regime emission distributions overlap meaningfully — i.e. every fit on real financial data.
- **bot/scraper.py:135** — `_parse_trades_page` (the HTML-fallback path) emits raw `"purchase"`/`"sale"` text, never normalized to the `"buy"`/`"sell"` vocabulary `bot/signal_engine.py` requires. The `DEAD_FEED` check only fires on an *empty* parse, not a non-empty-but-wrong-vocabulary one. *Trigger:* Capitol Trades' JSON API fails (an explicitly anticipated scenario) — the HTML fallback "succeeds" but every disclosure it sources is silently and permanently disqualified downstream, with no alert.

### High

- **bot/portfolio.py (`enforce_take_profits`)** — marks `take_profit_taken=1` and reports the ticker as reduced unconditionally after calling `reduce_position`, even when the underlying sell is `REJECTED` (a documented no-op). *Trigger:* a take-profit sell gets rejected — the position can never trigger take-profit again, even after the rejection cause clears.
- **bot/portfolio.py (`enforce_stop_losses`/`enforce_take_profits`)** — both return `closed`/`reduced` ticker lists unconditionally, with no check that the sell actually filled; a `REJECTED` sell still appears in the list. *Trigger:* any rejected stop/take-profit sell — currently latent since no caller consumes the return value for control flow, but a trap for any future one that does.
- **bot/portfolio.py (`close_position`/`reduce_position`)** — only `OrderStatus.REJECTED` is excluded before booking; `CANCELLED` and `SUBMITTED` (poll-timeout, still pending) both fall through to booking a sale using caller-supplied shares/price. *Trigger:* an Alpaca sell that gets cancelled or whose fill-poll times out while still pending — DB believes the position was sold while the broker may still hold the shares, a self-inflicted version of the exact desync `reconcile_with_broker` exists to catch.
- **bot/ai_analyst.py:271-294,303-344** (`parse_entry_response`/`parse_exit_response`/`parse_technical_response`) — required-key access (`data["conviction"]`, etc.) raises `KeyError`/`TypeError` on a missing/null field, not `ValueError` — `_call_with_retry` only catches `ValueError`, so this bypasses the entire retry budget. Demonstrated directly: a response missing only `rationale` crashes on attempt 1 of 3. *Trigger:* any syntactically-valid LLM response missing or null-ing a required field (realistic under truncation/schema drift).
- **bot/researcher.py + bot/ai_analyst.py** — raw, attacker-influenceable headline text (yfinance news titles/summaries) is spliced into entry/exit-scoring prompts with only a cosmetic `"---"` separator, not a real instruction boundary. *Trigger:* a headline crafted with fake delimiter text could attempt to steer `entry`/`conviction`/`action`; capped by downstream risk-manager vetoes and non-LLM-driven sizing, so not Critical, but a real signal-integrity gap.
- **backtesting/benchmarks.py:131** (`random_allocation`) — deducts the full pre-commission allocation from cash, destroying the commission amount rather than recording it as a cost, double-penalizing the benchmark and overstating strategy edge vs. the random baseline.
- **performance/tracker.py:27-36** (`trade_returns`) — computes raw `(exit-entry)/entry`, ignoring commission, while `backtesting/simulation.py`'s equivalent is commission-net — breaks the documented live-vs-backtest comparability.

### Medium

- **bot/portfolio.py** — partial fills are booked as full closes/reduces using caller-supplied shares rather than `order.filled_qty`, overstating realized P&L on a partial fill.
- **bot/portfolio.py** — `position_pct` is stored pre-trade (pre-slippage) and never recomputed from the actual fill; downstream consumers of stored `position_pct` work off a stale estimate after material slippage.
- **features/feature_pipeline.py:72-74** (`vol_z`) — hardcodes a 20-bar window instead of using `cfg.vol_window` like the adjacent `vol_20d`; masked today because the production default also happens to be 20.
- **regime/hmm_engine.py / tests/test_regime.py:78-94** — `test_no_look_ahead_bias` ends in a bare `assert True`, asserting nothing; the real causality check only covers the raw HMM, not the full `classify()` pipeline this test claims to verify. (No active bug found — feature windows are trailing-only — but the test is vacuous.)
- **bot/ai_analyst.py:439-443** — Anthropic response parsing assumes `content[0]` is always a text block; dormant risk if extended/interleaved thinking is ever enabled (no current call site does), no defensive fallback to find the first text block.
- **bot/researcher.py:235-236,247-249** — `item.get("content", {}).get("title", "")` assumes `item["content"]` is a dict; a `None` value raises `AttributeError`, caught only by an outer blanket `except Exception` that drops the *entire* ticker's research, not just the one bad headline.
- **bot/ai_analyst.py:303,310-312** — `int()`/`float()` coercion of LLM-returned conviction/prices silently truncates/accepts off-type values (e.g. `7.9999` truncates to `7`, a string or bool conviction is accepted) with no validation or logging.
- **backtesting/metrics.py:50-61** (`sortino_ratio`) — downside deviation computed as sample std of the negative-return subset centered on its own mean, not the textbook `sqrt(mean(min(r-target,0)^2))` over all observations; hand-computed on a synthetic 5-day series this inflates Sortino ~40%. Zero test assertions on this function.
- **backtesting/run_strategy_backtest.py:48-69** — `mom_12m` uses `prices.iloc[0]` of whatever window is returned rather than a date-anchored ~252-day lookback; the history-length guard checks total bars, not the 12-month leg specifically.
- **backtesting/attribution.py:101** — pooled-attribution gate requires only `len(common_dates) >= 10` for one-factor OLS+HAC (dof=8), reported with the same apparent structure as a far larger sample.
- **bot/committee.py:148-152** — fallback name-match compares only first/last whitespace-split tokens; breaks on suffixes ("Jr.", "III") or middle names/initials common in the underlying YAML data, silently disqualifying an otherwise-valid signal.
- **screener/factor_scorer.py:217-222** — missing/NaN `mom_12m` (thin-history names) is filled with `0` before percentile ranking, landing in the worst momentum percentile rather than being excluded or neutrally imputed; the completeness gate doesn't cover this column.
- **bot/db.py:191-209** — partial-migration recovery depends on string-matching `"duplicate column"` in a future exception message — locale/SQLite-version-text-dependent, untested.

### Low

- **risk/risk_manager.py:237** — dead-code `new_pct` computed but unused (all callers correctly use `size_multiplier`).
- **bot/portfolio.py:490-500** (`is_sector_capped`/`is_liquid_enough`) — stale duplicate of the now-fixed `risk_manager.validate_order` logic; only reachable from the already-deprecated `bot/scheduler.py`, but the live class still carries it.
- **bot/broker.py:98** — cosmetic: a rejected invalid-`side` order hardcodes `side=OrderSide("buy")` regardless of the actual input.
- **bot/ai_analyst.py** (several) — `risk_flags`/`conflicts` elements not type-coerced; `setup_type` not validated against its documented enum; an `LLM_PROVIDER` typo silently falls through to Anthropic with no normalization/error in non-orchestrator call paths; the `gpt-5.4` param-retry logs only via `log.warning`, no structured/persisted event; `bot/researcher.py`'s threaded `sys.path` check-then-act has a benign race; the param-retry call has no inter-call throttle.
- **backtesting/simulation.py:53-63** (`_get_price`) — silently returns `None` on a duplicate-index lookup via a blanket `except`, masking a data-quality issue as an ordinary missing bar.
- **backtesting/stress_test.py:108-138,169-196** — combining `crash_pct` and `vol_multiplier` in one scenario compounds order-dependently; dead in practice since `DEFAULT_STRESS_SCENARIOS` never combines both.
- **backtesting/simulation.py vs bot/portfolio.py** — backtest commission model (% of value) and live default (per-share, $0 default) are structurally different units; backtest P&L isn't a direct stand-in for live paper P&L.
- **monitoring/alerts.py:59** (`LogAlertSender.send`) — unguarded `json.dumps(data)`, unlike the webhook sender's wrapped call; latent until a non-JSON-serializable payload is passed.
- **bot/scheduler.py** — deprecated, unused dead module still carries the same vocabulary bug as the Critical scraper finding, plus is still imported/tested; no production risk but maintenance confusion.
- **orchestration/main_loop.py:1066** (`_run_hedge_pass`) — NAV computed once before iterating a multi-order hedge pass, so later orders in the same pass size off stale NAV; minor drift, not a correctness break.

---

## Part 3 — Test-suite audit ("asserts broken behavior as correct")

- **`tests/conftest.py` `mock_broker` fixture + `tests/test_portfolio.py`** (trail-stop tests) — the mock treats `place_stop_order`/`cancel_stop_order` as independent stateless calls, not reproducing either real broker's actual cancel semantics — masks the Critical trail-stop bug above.
- **`tests/test_broker.py`** — passes plain Python strings (e.g. `status="filled"`) into mocks instead of real `alpaca.trading.enums` instances, masking the `str(enum)` comparison bug above. No test exercises `place_stop_order`/`cancel_stop_order`/`get_stop_orders` at all.
- **`tests/test_risk_manager.py`** — zero coverage of `RiskState.DELEVERAGE` and zero coverage of the weekly-halt-then-deleverage interaction — the most safety-critical circuit breaker in the file has no direct test.
- **`tests/test_regime.py:78-94`** (`test_no_look_ahead_bias`) — vacuous `assert True`; the function name and docstring claim a property the test body never checks.
- **`tests/test_metrics.py`** — imports `sortino_ratio` but never asserts on it, despite the formula being wrong (see Medium finding above).
- **`tests/test_simulation.py:454-492`** — varies two variables (`position_pct` and `adv_usd`) simultaneously, so it would pass under several incorrect slippage formulas, not just the correct one.
- **`tests/test_walk_forward_analysis.py`** — name implies walk-forward coverage but only tests `backtesting/analysis.py`; no direct unit test of `_build_windows`'s train/test boundary (inclusive vs. exclusive).
- **`tests/test_scraper.py`** (lines 44,59,71,83,93,103) — asserts `"purchase"` as the *correct* output of the HTML-fallback parser — the same vocabulary bug already fixed on the JSON path two reviews ago, here baked in as the expected value.
- **`tests/test_scheduler.py` / `tests/test_integration.py`** — use `"purchase"` fixtures but mock around the real vocabulary gate entirely, so they neither catch nor mask the bug — just don't test it.

---

## Appendix

- Real test count: **708 tests** collected via `pytest --collect-only -q` (trading bot/), consistent with the bot's own `CLAUDE.md` banner.
- This document supersedes and replaces `TRADING_BOT_REVIEW.md`, `TRADING_BOT_FULL_REVIEW_BUNDLE.md`, `TRADING_BOT_REVIEW_PLAN.md`, and `TRADING_BOT_REVIEW_2026-06-22.md`, which are removed in a follow-up commit.
