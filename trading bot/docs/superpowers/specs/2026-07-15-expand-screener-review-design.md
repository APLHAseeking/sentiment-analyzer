# Expand fundamental-screener review pool + daily trade cap

## Problem

The bot is live and trading, but the user observed it only reviews "about 5
companies at a time" and wants more trades. Investigation found two real
bottlenecks, independent of each other:

1. `_SCREENER_TOP_N = 12` (module constant, `orchestration/main_loop.py`) —
   the fundamental screener factor-scores the full ~503-ticker S&P 500
   universe every run (cheap, price/fundamental-derived), but only the top
   12 by composite score are passed to the paid AI entry-scoring step
   (`score_entry_with_debate`, OpenAI). Everything outside the top 12 is
   never reviewed at all, regardless of how many pipeline runs happen per
   day.
2. `RiskConfig.max_positions_per_day = 3` (`system/config.py`) — even if
   more candidates pass AI scoring, only 3 new positions can open per day.
   Widening the review pool alone would not increase actual trade count
   without also raising this.

## Decision (confirmed with user)

- Raise the AI-review pool: 12 → 30 candidates per pipeline run.
- Raise the daily new-position cap: 3 → 5 per day.
- Promote `_SCREENER_TOP_N` from a hardcoded module constant into a proper
  config field, matching the existing pattern (`UniverseConfig`,
  `RiskConfig`, `SizingConfig`, `InsiderConfig` are all already
  dataclass-based tunables) rather than leaving it as a bare constant.

## Changes

### `system/config.py`

- `UniverseConfig`: add `screener_top_n: int = 30`, placed alongside the
  existing `research_concurrency` field, same comment style as its
  neighbors.
- `RiskConfig.max_positions_per_day`: `3` → `5`.

### `orchestration/main_loop.py`

- Remove the module-level `_SCREENER_TOP_N = 12` constant.
- The one call site, `run_factor_screen(universe, top_n=_SCREENER_TOP_N,
  ...)`, becomes `top_n=self._cfg.universe.screener_top_n`.

## Data flow / behavior

No new code paths. `run_factor_screen` (`screener/factor_scorer.py`)
already accepts `top_n` as a parameter and slices the factor-scored
universe with `scored.nlargest(top_n, "composite_score")` — today's call
just hardcodes 12. Raising it to 30 means up to ~2.5x more candidates reach
the AI entry-scoring step per run, with the same conviction/entry-hurdle
logic unchanged (no loosening of the quality bar, only the funnel width).
`Portfolio.can_open_new_position()` already reads `max_positions_per_day`
live from config each call — bumping 3 → 5 requires no other code change.

Cost/runtime impact: each additional AI-reviewed candidate is one more
OpenAI API call (more if conviction ≥ 7 triggers the bull/bear debate).
Yesterday's run AI-scored ~10 candidates in about a minute; scaling to 30
is expected to roughly triple that per-run cost and runtime, still well
within the time available before the entry window closes.

## Testing

- Add a regression test asserting `run_factor_screen` is called with
  `top_n=self._cfg.universe.screener_top_n` — proves the config value is
  actually wired through the call site, not just declared and unused.
- Add/verify a config test confirming the new defaults
  (`UniverseConfig().screener_top_n == 30`,
  `RiskConfig().max_positions_per_day == 5`).
- No existing tests assert a specific `top_n=` value or the old
  `max_positions_per_day` default (checked via grep), so no other test
  updates are required.
- Full suite must stay green.

## Explicitly out of scope

- Russell 1000 universe expansion — still blocked on the user obtaining
  `FMP_API_KEY`; tracked separately in `docs/STATE.md`. Widening the
  review pool operates on the existing S&P-500-only universe.
- Congressional (`_CONGRESSIONAL_MAX_PER_DAY = 1`) and insider
  (`InsiderConfig.max_per_day = 2`) caps — separate, event-driven signal
  sources gated by disclosure volume, not a "top N" cutoff. Not touched.
- Any change to the AI entry-scoring conviction/cost hurdle
  (`_ESTIMATED_COST_PCT`, the 3x-cost/1.0%-absolute rule) — this design
  only widens how many candidates get evaluated, not the bar they're
  evaluated against.
- Dynamic/adaptive top-N (e.g. scaling by regime or day-of-week) —
  considered and rejected as unrequested complexity; a fixed config value
  is what was asked for.
