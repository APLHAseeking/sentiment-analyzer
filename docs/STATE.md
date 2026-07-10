# STATE

## Goal
Fix why the live paper bot (2026-07-10 review) isn't trading: in-memory scheduler drops missed
cron windows on restart, Russell 1000 universe fetch 503s (missing User-Agent), initial stop
placement return value unchecked, entry hurdle too strict. Plan at
/Users/thomasvromen/.claude/plans/so-i-m-running-this-giggly-church.md.

## Now
Task complete — all P0/P1 steps landed on feature/profitable-strategies-lowvol-residmom-insider
(not yet committed — awaiting user go-ahead to commit). Full suite: 875 passed, 1 pre-existing
unrelated failure (test_insert_and_get_disclosure, see Open items).

## Next
- Awaiting user: commit? Then decide whether to `launchctl load` the plist now (a manual bot
  process is currently running — coordinate stop/start to avoid double-running).
- P2 (deferred per plan, not started): dead-man's-switch alert for missed pipeline runs,
  requirements.txt pinning/lockfile.
- Live verification once running: confirm trading.db signals/positions populate again,
  dashboard_state.json timestamp advances, expected_return_pct shows sane values.

## Constraints
- User 2026-07-10: add launchd auto-restart supervision (KeepAlive) alongside the code-level catch-up fix.
- User 2026-07-10: include an entry-hurdle loosening proposal now (3x cost / 1.0% absolute), evaluated via observability not a pre-hoc backtest (not feasible — no stored expected_return field).
- Original task: keep test suite green; follow repo CLAUDE.md/CODE.md conventions; log changes in docs/CLAUDE-REFERENCE.md#history
- Repo: tests offline; temperature=0 on LLM calls; no new deps/frameworks without flagging
- Global: never git push unless asked; commit per meaningful unit

## Decisions
- DECISION: SUE in momentum sleeve via _MOMENTUM_WEIGHTS at 0.15 (resid_mom stays largest 0.40) — avoids 6-tuple regime-weight ripple; increase only after PIT backtest (EDGE_BACKLOG).
- DECISION: B2 = hard exclude when shortPercentOfFloat > UniverseConfig.max_short_pct_float (20%, 0 disables); missing passes.
- DECISION: insider feed = EDGAR daily form.idx primary (2 newest published, newest first), getcurrent fallback, max_filings_per_run 300 budget.
- DECISION: event-calendar gate NOT carved out for PEAD — explicit future decision (EDGE_BACKLOG).
- DECISION: XBRL via frames API (~20 req/screen, 20h shelve cache xbrl_frames_cache, gitignored); SUE anchor shifts to filer's newest available quarter (max 2 stale) — slot 0 empty ~40 days post-quarter, Q4 frames sparse.

## Facts
- Repo root: /Users/thomasvromen/Documents/Claude code test; bot in "trading bot/" (space — quote it)
- Test command: cd "trading bot" && pytest — 853 tests green as of 2026-07-07
- Branch: feature/profitable-strategies-lowvol-residmom-insider; task commits 6c77981..868aa2d + docs commit
- New module: screener/xbrl_fundamentals.py (fetch_xbrl_factors, sue_from_quarterly_eps, accruals_ratio)
- RiskManager.restore_baselines() (risk/risk_manager.py) + db.get_nav_baselines (bot/db.py); wired in main_loop initialize()
- Insider: bot/insider.py parse_form_idx/_fetch_daily_form4_index; dedup by accession + id
- docs/guardrails/MIGRATION-LOG.md has PRE-EXISTING uncommitted changes (not this task's) — do not commit blindly

## Done
- 2026-07-10 trade-frequency review + fixes — RESULT: scheduler catch-up-on-restart (main_loop.py
  start() + db.py job_runs table), universe.py Russell-1000 User-Agent fix, portfolio.py initial
  stop-placement alert, ai_analyst.py entry hurdle 5x/1.5%->3x/1.0% + expected_return_pct
  observability field (db.py migrations 6/7), launchd plist + RUNBOOK.md/CLAUDE.md/
  CLAUDE-REFERENCE.md docs. Full suite: 875 passed (was 862), 1 pre-existing unrelated failure.
  Plan file: /Users/thomasvromen/.claude/plans/so-i-m-running-this-giggly-church.md
- 2026-07-06 Phase 1 review report — RESULT: 8 Part A findings, ranked Part B, CEO-insider rejected (plan file iterative-hugging-quasar.md)
- 2026-07-07 Part A fixes A1-A8 — RESULT: 4 commits (6c77981, c8257cc, 6e2b901, 018f205), targeted suites green
- 2026-07-07 B2 short-interest screen — RESULT: commit 1c77967
- 2026-07-07 B1+B3 XBRL signals — RESULT: commit 868aa2d; live-verified vs real SEC frames (AAPL SUE +2.74, net payout $106B)
- 2026-07-07 full suite — RESULT: "853 passed, 1 warning in 109.15s" (was 819; +34)
- 2026-07-07 deferred edges saved — RESULT: trading bot/docs/EDGE_BACKLOG.md, linked from CLAUDE-REFERENCE.md#key-documents

## Open items
- SUE PIT backtest (companyfacts filed dates) before raising its 0.15 weight — recorded in EDGE_BACKLOG.md
- eps_trend daily snapshot collection (estimate revisions) — recorded in EDGE_BACKLOG.md, not built
- Insider routine-buyer filter — recorded in EDGE_BACKLOG.md, viable after ~1yr of insider history
- 2026-07-10: `tests/test_db.py::test_insert_and_get_disclosure` fails — hardcodes
  `disclosure_date: "2026-04-10"`, now outside `get_existing_ids()`'s 90-day window as real
  time advances. Pre-existing, confirmed via `git stash` on clean tree, unrelated to this
  session's changes — NOT fixed (out of scope). Needs a relative-date fixture.
- 2026-07-10: `docs/guardrails/MIGRATION-LOG.md` still shows as modified in `git status` —
  pre-existing uncommitted drift from before this session, not touched this session either.

## Failed attempts
(none — two test failures during work were fixture/expectation updates, resolved same turn)
