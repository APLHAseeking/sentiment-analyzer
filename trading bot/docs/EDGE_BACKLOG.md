# Edge Backlog — evaluated signals NOT (yet) implemented

Source: full edge-research review, 2026-07-07 (Part B of the pre-launch review;
Phase 1 report). Items 1–3 of that review (PEAD/SUE, short-interest negative
screen, accruals + net payout) were implemented 2026-07-07 — see
`CLAUDE-REFERENCE.md#history`. Everything below was **deliberately deferred or
rejected**; re-evaluate against the stated conditions before implementing.

Baseline for cost deltas: ~15 scored candidates/day × 1–3 LLM calls each
(≈20–45 calls/day). Prices are mid-2026 estimates — re-verify before buying.

## Deferred (conditions could be met later)

### Analyst estimate revisions — CONDITIONAL
- Live signal is free: yfinance `eps_trend` (consensus now vs 7/30/60/90d ago).
- **Blocker:** no free historical consensus snapshots (IBES-class data), so it
  cannot be PIT-backtested — fails the Phase 0 gate rule "backtest before trust".
- **Agreed path:** start snapshotting `eps_trend` daily into SQLite now (S
  effort, zero risk), activate as a signal only after 12+ months of
  self-collected point-in-time history. Snapshot collection itself is still
  NOT implemented.
- Correlation caution: high overlap with momentum + PEAD/SUE.

### SUE weight increase — RESOLVED: gate failed, weight stays 0.15 (2026-07-14)
- SUE entered the momentum sleeve at a deliberately small 0.15 sub-weight.
- Built a PIT backtest using the SEC **companyfacts** API's `filed` dates
  (frames API lacks them), reusing `sue_from_quarterly_eps` from
  `screener/xbrl_fundamentals.py` unmodified. Full report:
  `docs/SUE_PIT_BACKTEST_2026-07-14.md`.
- **Result: gate failed on both horizons (20d t=0.87/IR=0.24, 60d
  t=1.41/IR=0.30, need t>2 AND IR>0.5 independently), and additionally on
  the first/second-half stability condition at 60d (sign flip). Per the
  pre-committed decision rule, the weight stays at 0.15 — no change made.**
  Honesty check confirmed no residual look-ahead (PIT reads weaker than a
  naive T+0 anchor at both horizons, as expected). Effect is directionally
  positive across all 7 market regimes (no sign flips, satisfies that gate
  condition) but not statistically distinguishable from zero pooled, and
  reverses sign between sample halves at 60d. Two real bugs found and fixed
  during the build (quarter-bucketing calendar-boundary logic; a history-
  truncation bug that produced absurd SUE outliers) — see report and commit
  history on `screener/xbrl_pit_sue.py` / `backtesting/backtest_sue_pit.py`.
  Not revisiting without a genuinely new argument — this was a clean,
  honest null result, not a data or methodology gap.
- Related open decision: the event-calendar gate blocks entries within 2 days
  of earnings — exactly when PEAD fires. A carve-out was consciously NOT made;
  decide explicitly if drift capture matters more than event risk.

### Insider routine-buyer filter — cheap upgrade, not yet built
- Drop insiders who buy in the same calendar month ≥2 consecutive years
  (Cohen–Malloy–Pomorski "opportunistic vs routine"). Needs only the
  accumulating `insider_disclosures` table; S complexity, zero LLM cost.
  Becomes viable once ~1+ year of insider history has accumulated.

## Rejected (with reasons — revisit only if the reason changes)

| Signal | Why rejected | Would reconsider if |
|---|---|---|
| CEO-specific insider weighting | Bot already ranks C-suite first at zero cost (`bot/insider_signal.py` `_strength`); literature says opportunistic-vs-routine and cluster buys dominate title effects; LLM enrichment ≈ doubles daily call volume for a queue whose top-2 already prefer C-suite | Never on current evidence — prefer the routine-buyer filter above |
| 13F institutional flow | L engineering for 45-day-stale quarterly data on a daily-rebalance bot; clean panel ~$500+/yr | A research question specifically needs holdings data |
| Options-implied (put-call skew, IV rank) | No affordable IV history (ORATS ~$99+/mo); live-only signal is unverifiable under the Phase 0 gate; high parameter-overfit risk | Phase 0 gate opens AND a paid data budget exists |
| Extra seasonality (turn-of-month, pre-holiday) | Data-mined zoo; Halloween overlay (`AllocationConfig`) already covers the one effect with decent OOS evidence | Never — adding more is overfitting-by-config |
| Stat-arb / pairs trading | Needs shorting the rich leg + tighter-than-daily monitoring; long-only residue ≈ the existing (weak) reversal sleeve | Architecture becomes long/short intraday |
| ETF/fund flows | Clean data (EPFR-class) $10k+/yr; redundant with the HMM regime overlay for risk-on/off | Budget appears AND regime layer proves insufficient |
| Alternative data (satellite, card spend, web traffic) | $10k–$100k+/yr; capacity-constrained single-name signals need per-name validation; economics require far more capital than a paper-trading thesis bot | Institutional-scale capital |

## Also fixed in the same review (context for future readers)

Part A of the same review found and fixed: circuit-breaker baselines not
surviving restarts (A1), EDGAR getcurrent duplicate/coverage issues in the
insider feed (A2/A3), small-sector rank inflation + missing-data asymmetry +
mom_12m window drift in the screener (A4–A6), an HMM degenerate-input crash
(A7), and assorted convention cleanups (A8). Details in
`CLAUDE-REFERENCE.md#history` (2026-07-07 entry).
