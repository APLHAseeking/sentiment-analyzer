# STATE

## Goal
Fix why the live paper bot isn't trading (root-caused, mostly fixed — see Done); now executing
follow-up plan compiled-spinning-stardust.md (weekly-factor-review, cross-model debate,
Russell 1000, guardrails-kit STATE.md/SESSION.md cost cleanup).

## Now
14:00 CEST run_morning_pipeline completed — found + fixed a new Critical bug (72ed02e, see
Done). Russell 1000 (Workstream C) blocked on user adding FMP_API_KEY. Guardrails-kit cost
cleanup (this Now/Done rewrite + pending SESSION.md/README.md edit) in progress. Full history:
CLAUDE-REFERENCE.md#history.

## Next
- Confirm the in-progress 14:00 CEST `run_morning_pipeline` result (background monitor armed)
  — check `bot.log` / `positions` table once it lands.
- Once user adds `FMP_API_KEY` to trading bot/.env: live-test FMP's russell1000_constituent
  endpoint (existence unconfirmed — see Open items), wire up if it works.
- User: approve the pending LaunchAgent in System Settings (if one appears), then retry
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.thomasvromen.tradingbot.plist`.
- P2 (deferred per plan, not started): dead-man's-switch alert for missed pipeline runs,
  requirements.txt pinning/lockfile.

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
Full narrative for every entry below: trading bot/docs/CLAUDE-REFERENCE.md#history (this
project's permanent changelog — pointers only here per SESSION.md S3).
- 2026-07-10 (session 3): test-DB-pollution fix (8509964), weekly-factor-review skill (0c6d25e),
  cross-model debate (f372606), open_position-return-value fix (72ed02e). Full suite 886
  passed, 1 pre-existing unrelated failure.
- 2026-07-10 (session 2): scheduler catch-up, universe/stop/entry-hurdle fixes (0d93f8b),
  iShares WAF hardening (76f0da9). Plan: so-i-m-running-this-giggly-church.md.
- 2026-07-06/07: Phase 1 review + remediation A1-A8, B1-B3 XBRL/short-interest/insider —
  commits 6c77981..868aa2d, EDGE_BACKLOG.md. Plan: iterative-hugging-quasar.md.

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
- Russell 1000 universe unresolved: iShares WAF-blocks both its CSV endpoints regardless of
  headers (`bot/universe.py`, hardened to fail diagnosably, commit 76f0da9). No free/no-signup
  alternate found (Vanguard = JS app shell, GitHub community CSV = dead since 2013, Wikipedia
  has no constituent table). User signing up for a free FMP API key to live-test their
  constituents endpoint next — existence unconfirmed, not in FMP's documented S&P500/Nasdaq100/
  Dow-only index list.
- launchd auto-restart unresolved: plist correct, but every `launchctl bootstrap` exits
  immediately (code 78/EX_CONFIG, zero output) — likely macOS Background Task Management
  blocking a new LaunchAgent pending approval in System Settings -> General -> Login Items &
  Extensions (needs user GUI action). Bot runs via manual `nohup` in the meantime.

## Failed attempts
(none — two test failures during work were fixture/expectation updates, resolved same turn)
