# Real Short-Selling Capability — Design Spec

**Date:** 2026-07-17
**Status:** Design approved. **Implementation deferred by user request** — build it feature-flagged OFF ("one click away"), do not activate. User wants to see live paper returns from the long-only book first before turning shorts on.

## Goal

Give the bot the ability to open real short positions on individual stocks (sell borrowed shares via Alpaca, profit when the price falls), driven by the existing fundamental factor screener's worst-ranked names — the bearish mirror of today's long-only pipeline. Gated behind a config flag that defaults off; flipping it is meant to be the entire activation step, no further coding required. Distinct from the existing "hedge" positions (PSQ/RWM/SH/EFZ), which are long buys of inverse ETFs and stay unchanged.

## Design Principle

Unified position representation (one `positions`/`closed_positions` schema, one NAV calculation spanning both long and short books — required so the existing portfolio-level circuit breakers, `RISK_LOCKOUT`, daily/weekly drawdown checks, keep seeing the whole picture, not two reconciled halves) + a small set of centralized direction-aware math helpers (P&L, stop-trigger, take-profit-trigger) so direction-conditional logic lives in one place instead of scattered branches. The *entry side* (screener candidate selection, risk caps, AI prompt) is built as sibling functions alongside the existing long-only ones rather than branching inside them, so the already-live long-only code paths get touched as little as possible.

---

## Component 1: Activation flag

`Settings.strategy.enable_short_selling: bool = False` — same pattern as the existing `Settings.sizing.enable_cross_model_debate`. Any config validation this flag needs (e.g. confirming the Alpaca account actually supports shorting, see Component 4) only runs `if enable_short_selling`, so it has zero effect while off. While `False`: the screener never computes short candidates, `run_morning_pipeline` never calls short entry logic, no short order can ever be placed — verified by a dedicated regression test (Component 9).

## Component 2: Data model

Versioned migration (`bot/db.py`'s existing `schema_version` mechanism) adds `direction TEXT NOT NULL DEFAULT 'long'` to `positions` and `closed_positions`. Existing rows default to `'long'`, no backfill needed.

**Explicit invariant:** a ticker can have at most one open position, regardless of direction. `db.position_exists(ticker)` stays ticker-based (not ticker+direction) — a name already long can never simultaneously be shorted, and vice versa. This is what naturally prevents the long screener's top-N and the short screener's bottom-N from ever colliding on the same symbol.

## Component 3: Direction-aware math helpers

New small pure-function module (e.g. `bot/direction_math.py`):

- `pnl_pct(direction, entry_price, current_price)` — long: `(current - entry) / entry`; short: `(entry - current) / entry`.
- `stop_trigger_price(direction, extreme_price, stop_pct)` — long trails the peak downward; short trails the trough upward.
- `is_stop_triggered(direction, current_price, stop_price)` — long: current ≤ stop; short: current ≥ stop.
- `is_take_profit_triggered(direction, current_price, target_price)` — same inversion, covers the existing `take_profit_taken` column's logic for shorts.

These are the only places direction-conditional math lives. Exhaustively unit-testable as pure functions (Component 9).

## Component 4: Broker & portfolio changes

- `Portfolio.open_position` gains a `direction` param. Short open: `broker.place_order(ticker, side="sell", qty=...)` with no prior long position — Alpaca opens this as a short (assuming the account supports it, see below).
- **`close_position`/`reduce_position` currently hardcode sell-to-close** (`_place_sell_with_retry`, `bot/portfolio.py:158`) — this is a real gap found during design review, not something the original sketch handled. Both need a direction branch: long close = sell (existing path, unchanged); short close = buy-to-cover (new path, same retry/wash-trade-collision handling as the existing sell-retry logic).
- **`db.log_closed_position`'s `realized_pnl = gross_proceeds - entry_cost`** (`bot/db.py:383`) assumes long (profit when exit > entry). Needs a `direction` param that flips the sign for shorts (profit when cover price < entry price).
- **NAV/exposure formula.** `Portfolio.open_position`'s `nav = cash + sum(qty * current_price for positions)` needs to correctly account for short liabilities (a short's mark-to-market value is a liability against NAV, and opening a short adds cash equal to the sale proceeds at entry). The exact formula depends on how `AlpacaBroker.get_positions()` actually represents a short (signed qty vs. a separate side field) — **this must be verified against the live paper account's actual response shape before writing the NAV code**, not assumed. Getting this wrong would corrupt position-sizing for the long book too, since NAV is shared.
- **Unverified assumption: does the account support shorting at all.** Alpaca shorting requires a margin-enabled account; nothing in `bot/broker.py` today checks account type. Add a check (e.g. `get_account().shorting_enabled` or equivalent field) that fails loud at flag-activation time rather than silently rejecting every short order forever. This should be verified once, even before the flag is ever flipped, so the spec isn't resting on an unconfirmed assumption.
- **No shortable / hard-to-borrow check today.** A bottom-ranked screener candidate could be HTB or non-shortable at Alpaca. Add a pre-flight check (mirrors the existing duplicate-position pre-flight pattern in `open_position`) so the bot skips gracefully instead of retry-looping a rejected order.

## Component 5: Risk config (short-specific, tighter than long)

New `RiskConfig` fields, independent of the long-side caps/counters:
- `max_short_position_pct: float = 4.0` (vs. long's 8.0)
- `max_short_positions: int = 5` (vs. long's 20)
- `max_short_positions_per_day: int = 2` (vs. long's 5)
- `short_trailing_stop_pct: float = 8.0` (vs. long's 15.0 — tighter because a rising price hurts a short faster than a falling price hurts a long, and the loss is theoretically uncapped)

`Portfolio.can_open_new_position` gains a parallel short-side check against these, independent of the long counters.

## Component 6: Screener (short candidates)

`run_factor_screen_short()` — sibling to the existing `run_factor_screen`. Reuses `_compute_composite`'s scores (same factor model, no new factors), but takes `scored.nsmallest(short_top_n, "composite_score")` instead of `nlargest`. New `UniverseConfig.screener_short_top_n` config.

## Component 7: AI gate (mirrored)

`score_entry_short` / `score_exit_short` in `bot/ai_analyst.py` — mirrored prompt schema with bearish framing ("expected DECLINE exceeds estimated cost by at least 3x AND 1.0% absolute" in place of the long side's "expected gain..."). Same `conviction`/`risk_flags`/`expected_return_pct`-shaped output as the existing `EntryScore`, so downstream logging and the pipeline's data shape don't need to change.

## Component 8: Orchestration wiring

In `run_morning_pipeline`, a new short-candidate phase runs immediately after the existing long phase, entirely wrapped in `if settings.strategy.enable_short_selling:` — the phase does not execute at all while the flag is off.

## Component 9: Testing

Offline, mocked broker/LLM per this repo's existing convention (`tests/conftest.py`).

**Named risk:** this repo has hit the *same* bug class three separate times per `CLAUDE.md`'s history — unmocked test code writing into the real `trading.db` or `watchdog_restart_history.json`. New short-side tests must be checked specifically for this, not just generically "mocked" — any fixture touching `positions`/`closed_positions` needs an isolated test DB, verified by inspection before merging, given the track record.

New tests needed:
- Direction-math helpers (Component 3) tested exhaustively as pure functions — no mocking needed, cheap to cover every branch.
- Short-side risk caps enforced independently of long caps (opening 5 shorts doesn't block long entries and vice versa).
- **Flag-off regression test**: asserts today's long-only behavior and code paths are provably unchanged when `enable_short_selling=False` — this is the test that lets us trust "one click away" actually means zero side effects today.
- Flag-on happy path: candidate → AI gate → broker sell-to-open → DB row with `direction='short'` → stop/take-profit triggers correctly inverted → buy-to-cover close → correctly-signed `realized_pnl`.
- Shortable/HTB pre-flight check skips gracefully rather than retry-looping.

---

## Open Questions / Known Gaps (deliberately unresolved — folded in as-is per user decision, not blocking the spec, but must be revisited before the flag is ever flipped on)

| # | Gap | Why it's open |
|---|---|---|
| 1 | Regime-aware sizing isn't addressed for shorts | Given the user's actual motivation (profiting from a down day), shorts arguably should size up in bearish/crash regimes and down in bullish ones — the opposite tilt from longs' existing regime multiplier. Undecided. |
| 2 | Overlap with the existing hedge mechanism (PSQ/RWM/SH/EFZ) | Real per-stock shorts on top of the existing broad inverse-ETF hedge could double-count bearish exposure / over-hedge. No interaction is modeled in v1 — the two mechanisms are independent. |
| 3 | No aggregate gross/net exposure cap across long + short + hedge combined | Per-book caps (8% long / 4% short) don't prevent total gross exposure from stacking to something excessive. Not addressed. |
| 4 | Short borrow fees not modeled in the cost hurdle | The AI's `estimated_cost_pct` calibration is for long round-trip costs (~0.10%); short borrow fees can be materially higher for some names and aren't reflected. |
| 5 | `SimulatedBroker` (`execution/paper_broker.py`) cannot execute a short at all | Found during Task 5's code review, commit `b293bfa`+review. It has no `shorting_enabled()`/`is_shortable()`, and its fill logic rejects a sell with no existing position ("Cannot sell {ticker}: no position held") — sell-to-open isn't modeled. This means the short-selling path can only ever be exercised against the real Alpaca paper broker, never via `--simulated` mode, this repo's normal offline dev/test path. Deliberately not building full margin/short-fill simulation into `SimulatedBroker` — that's a substantially larger undertaking (borrow mechanics, short P&L marking) the original design never scoped. Accepted as-is: short-selling is an Alpaca-only capability. |

These are recorded here, not solved — the feature stays behind the flag until they're either addressed or explicitly accepted as-is.

---

## What Is NOT Changed / Out of Scope

| Item | Status |
|---|---|
| Existing hedge positions (PSQ/RWM/SH/EFZ) | Unchanged — still long buys of inverse ETFs, independent mechanism |
| Long-side screener, risk caps, AI prompts, stop/take-profit logic | Unchanged — new short-side logic is additive (sibling functions), not a rewrite |
| Congressional/insider signal sources | Not used for short candidates (user's explicit choice: inverted fundamental screener only) |
| Real-money execution | Still fully disabled — this is paper-only, same as everything else in the bot |
| Activation | Not happening as part of this spec — flag ships `False`, stays `False` until the user decides otherwise |

## Modified / New Files

| File | Change |
|---|---|
| `trading bot/system/config.py` | New `Settings.strategy.enable_short_selling` flag; new `RiskConfig` short-side fields; new `UniverseConfig.screener_short_top_n` |
| `trading bot/bot/direction_math.py` | **New file** — `pnl_pct`, `stop_trigger_price`, `is_stop_triggered`, `is_take_profit_triggered` |
| `trading bot/bot/portfolio.py` | `open_position`/`close_position`/`reduce_position`/`enforce_stop_losses` gain `direction` handling; NAV formula updated; shortable/account-type pre-flight checks added |
| `trading bot/bot/db.py` | Migration adding `direction` column; `log_closed_position` direction-aware `realized_pnl` |
| `trading bot/bot/broker.py` | Account-type/shorting-enabled check |
| `trading bot/screener/factor_scorer.py` | New `run_factor_screen_short()` |
| `trading bot/bot/ai_analyst.py` | New `score_entry_short`, `score_exit_short`, mirrored bearish prompt schema |
| `trading bot/orchestration/main_loop.py` | New short-candidate phase in `run_morning_pipeline`, gated on the flag |
| `trading bot/tests/` | New tests per Component 9, across `test_direction_math.py` (new), `test_portfolio.py`, `test_db.py`, `test_factor_scorer.py`, `test_ai_analyst.py`, `test_orchestrator.py` |
| `trading bot/CLAUDE.md` | Status banner entry once this ships, noting the flag and its default-off state |
