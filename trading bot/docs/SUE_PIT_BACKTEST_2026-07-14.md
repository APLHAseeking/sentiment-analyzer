# SUE PIT Backtest — Report (2026-07-14)

Implements the plan at `docs/superpowers/plans/2026-07-14-sue-pit-backtest.md`.
Recommendation only — the live `_MOMENTUM_WEIGHTS["sue"] = 0.15` weight in
`screener/factor_scorer.py` was **not** touched by this work.

## Confirmed spec (recap — see conversation history for the full back-and-forth)

- **PIT date:** tradable date = original (non-`/A`) filing's `filed` date + 1
  NYSE trading day. Anchor selection mirrors production's `_completed_quarters`
  exactly, gated by true `filed <= as_of` (not merely calendar-completed).
- **Amendments:** earliest-filed, non-`/A` form only. No fallback to amendments.
- **SUE formula:** `sue_from_quarterly_eps` reused unmodified from
  `screener/xbrl_fundamentals.py` — zero redefinition.
- **Gate (pre-committed before results were seen):** at **20d AND independently
  at 60d**: t-stat > 2 AND IR > 0.5 (gross of costs), **and** sign-consistent
  positive drift in first-half vs second-half of the sample, **and** no sign
  flip in any regime bucket with ≥30 distinct earnings events.
- **Decision rule:** gate fails → weight stays 0.15 (null result). Gate clears
  → recommend 0.15 → 0.25 (single fixed step, not fit to the measured IR).
- **Universe:** PIT S&P 500 membership from `fja05680/sp500`, not current
  constituents projected backward.
- **Sample window:** 2012-01-01 to 2026-04-15 (past the XBRL-adoption ramp;
  leaves ≥60 trading days of runway for the last signals' 60d drift to be
  observable as of 2026-07-14).
- **Drift anchor:** `close(d+1+horizon) / close(d+1) − 1`, announcement-day
  jump excluded by construction.
- **IR:** gross of transaction costs — validates signal content, not a
  live-achievable number.
- **Regime buckets:** counted by distinct earnings events, not firm-days.

## Sample construction

| Stage | Events | Distinct tickers |
|---|---|---|
| Raw PIT SUE events (companyfacts, full history) | 24,552 | 590 |
| After PIT S&P 500 universe restriction | 18,708 | 570 |
| After drift computation (20d & 60d) | 18,708 | 570 |

Date range: 2012-01-04 to 2026-04-10. Universe restriction dropped 6,253
events (24.5%) — the largest single case was a ticker (Comfort Systems USA,
FIX) that only entered the S&P 500 in March 2026; its entire 2012–2025 filing
history predates real index membership and is correctly excluded rather than
credited to the strategy. All 570 tickers had usable yfinance price coverage
(zero dropped for missing prices).

Two real bugs were found and fixed while building this (see commit history
on `screener/xbrl_pit_sue.py` and `backtesting/backtest_sue_pit.py`):
1. **Quarter-bucketing** (`original_quarterly_eps`): SEC's frame convention
   buckets by the calendar quarter-end boundary nearest a fact's `end` date,
   not a fixed end-month or start-month rule — found via the Task 6 validation
   checkpoint against real WMT/AAPL/JNJ data, took three iterations to get
   right, and required a collision-exclusion rule for 52/53-week retail
   fiscal calendars (e.g. Costco) where the per-fact rule breaks down.
2. **History truncation**: an early version of `build_pit_sue_events` fed
   only the sample-window-truncated EPS history into `sue_from_quarterly_eps`,
   starving the seasonal-random-walk denominator and occasionally producing
   absurd SUE values (7.2e15 on one real PTC event) from a near-zero-but-
   nonzero variance. Fixed by passing the full company history to the SUE
   computation and using the sample window only to decide which events are
   output — using genuinely pre-window history isn't look-ahead, since it was
   real, filed information as of any later `as_of` date.
3. A third issue (not a bug, a modeling correction): some filings report
   several historical quarters on the same filed date (an annual report's
   "selected quarterly data" footnote) — collapsed to one event per
   (ticker, filed_date), removing 993 duplicate-valued rows.

## Results

### Gate — pooled, per horizon (HAC/Newey-West, bandwidth = horizon)

| Horizon | Mean daily excess | t-stat | IR (annualized, gross) | Gate (t>2 AND IR>0.5) |
|---|---|---|---|---|
| 20d | 8.43e-05 | **0.87** | **0.24** | **FAIL** |
| 60d | 6.31e-05 | **1.41** | **0.30** | **FAIL** |

Neither horizon clears the pre-committed bar independently.

### Stability — first-half vs second-half of the sample (split by event count)

| Horizon | First half t-stat | Second half t-stat | Sign-consistent? |
|---|---|---|---|
| 20d | 0.37 | 0.94 | Yes (both positive) |
| 60d | **-0.19** | 1.68 | **No — sign flip** |

The 60d horizon fails the stability condition independently of the pooled
t-stat/IR shortfall: the effect was mildly negative in the first half of the
sample and positive in the second half.

### Regime breakdown (top-quintile-SUE events only; diagnostic, not a second
PIT reconstruction — see caveat below)

| Regime | Events | Mean drift 20d | Mean drift 60d | ≥30 events (carries veto weight) |
|---|---|---|---|---|
| bull | 790 | +0.88% | +2.24% | Yes |
| neutral | 693 | +2.65% | +5.77% | Yes |
| melt-up | 684 | +0.54% | +1.65% | Yes |
| euphoria | 580 | +1.44% | +3.66% | Yes |
| bear | 461 | +0.73% | +1.81% | Yes |
| deep-bear | 302 | +4.21% | +6.96% | Yes |
| crash | 232 | +0.56% | +4.37% | Yes |

Every regime bucket shows positive mean drift at both horizons — no sign
flips anywhere. The regime-consistency condition of the gate is **satisfied**;
it's the pooled significance and the time-stability conditions that fail, not
this one. This directly answers the "does the edge survive in the current
regime" question the regime-weighted sleeve structure motivated: directionally
yes, but the effect isn't statistically distinguishable from zero at the
pooled level regardless of regime.

**Caveat on this table**: regime labels come from the already-fit production
`HMMRegimeEngine` (`regime_model.joblib`) run in forward-only/filtered-
posterior mode over the full historical window. The model's own parameters
were fit on the complete sample (not walk-forward re-fit at each historical
point), so this is a descriptive stratification for reporting, not an
independent PIT reconstruction — it does not affect the already-PIT-correct
drift series itself, only how those numbers are grouped here.

### PIT-vs-naive honesty check

| Horizon | PIT (d+1) t-stat | Naive (d+0) t-stat | PIT (d+1) IR | Naive (d+0) IR |
|---|---|---|---|---|
| 20d | 0.87 | 1.31 | 0.24 | 0.36 |
| 60d | 1.41 | **2.01** | 0.30 | 0.47 |

PIT reads weaker than naive at both horizons — the expected, correct
direction, confirming no residual look-ahead leaked back into the PIT
construction. Notably, the naive 60d t-stat (2.01) would have just cleared
the t>2 bar the honest PIT version (1.41) does not — a concrete illustration
of why the filed-date lag matters, not just a theoretical concern.

## Recommendation

**Gate fails** — on pooled significance at both horizons, and additionally on
the first/second-half stability condition at 60d. Per the pre-committed
decision rule:

> **The SUE sub-weight stays at 0.15. No weight change is recommended.**

This is a genuine null result, not a data problem: the universe and price
data are complete (zero missing-price drops, 570/570 tickers), the honesty
check confirms the PIT construction isn't leaking look-ahead, and the effect
is directionally consistent across all seven market regimes — it simply isn't
statistically distinguishable from zero at the pooled level, and reverses
sign between the first and second half of the sample at the 60d horizon.

No code change follows from this report. `_MOMENTUM_WEIGHTS["sue"]` remains
0.15 in `screener/factor_scorer.py`, untouched.
