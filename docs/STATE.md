# STATE

## Goal
Fix why the live paper bot isn't trading and keep it trading reliably. Root-caused across
several sessions (dead-for-3-days hang, phantom fills, wrong pipeline timing, NAV-baseline
bug — see Done); bot is currently live, restarted, healthy.

## Now
"Fix everything" follow-up complete, including the launchd investigation to its actual end.
Full suite green, 909 passed, zero known failures. Dead-man's-switch is ACTIVE (confirmed
running via launchd). Main bot's launchd auto-restart is a closed decision, not active — see
Done. Russell 1000 remains genuinely blocked on the user obtaining an API key — see Next.

## Next
- Once user adds `FMP_API_KEY` to trading bot/.env: live-test FMP's russell1000_constituent
  endpoint (existence unconfirmed — see Open items), wire up if it works. Re-checked
  2026-07-14: `.env` still has no `FMP_API_KEY`. Tried 3 more free/no-signup sources this
  session (FTSE Russell, stockanalysis.com, SlickCharts) — none viable, see Open items.
- requirements.txt pinning/lockfile: still not started.

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
- Test command: cd "trading bot" && pytest — 909 tests green as of 2026-07-14, 0 known failures
- Branch: feature/profitable-strategies-lowvol-residmom-insider; task commits 6c77981..868aa2d + docs commit
- New module: screener/xbrl_fundamentals.py (fetch_xbrl_factors, sue_from_quarterly_eps, accruals_ratio)
- RiskManager.restore_baselines() (risk/risk_manager.py) + db.get_nav_baselines (bot/db.py); wired in main_loop initialize()
- Insider: bot/insider.py parse_form_idx/_fetch_daily_form4_index; dedup by accession + id
- docs/guardrails/MIGRATION-LOG.md has PRE-EXISTING uncommitted changes (not this task's) — do not commit blindly

## Done
Full narrative for every entry below: trading bot/docs/CLAUDE-REFERENCE.md#history (this
project's permanent changelog — pointers only here per SESSION.md S3).
- 2026-07-13/14 (this session): found bot dead ~3 days (zero job_runs since Fri 14:09 CEST);
  restarted; root-caused and fixed a missing-timeout hang across every reachable
  yf.Ticker(...) call site + the Alpaca broker client (commit ebc0951, 8 new tests, RESULT:
  full suite green). Then fixed a Monday-only bug in `get_nav_baselines` that could seed the
  weekly-loss circuit-breaker baseline from today's NAV instead of the prior week's true
  close (commit 9a82022) — proven via git-stash red/green. RESULT: full suite 904 passed,
  0 known failures.
- 2026-07-13 (concurrent session, same evening as the network-timeout fix above): root-caused
  why the bot has made zero real trades ever, not just why it goes silent — entry orders were
  being placed when NYSE wasn't actually open (14:00 CEST cron fired 1.5h pre-open; catch-up
  had no intraday-open check, so tonight's 22:09 post-close restart placed 7 doomed orders).
  Fixed with a `_nyse_is_open_now()` guard + corrected schedule. Sweep found the same
  open_position-bool-ignored bug class on the exit side (close_position/reduce_position
  ignored at all 4 exit call sites) — fixed. 6 new regression tests, full suite 903 passed.
- 2026-07-10: test-DB-pollution (8509964), cross-model debate (f372606), open_position
  return-value fix (72ed02e), scheduler catch-up (0d93f8b), iShares hardening (76f0da9).
- 2026-07-06/07: Phase 1 review A1-A8, B1-B3 XBRL/short-interest/insider (6c77981..868aa2d).
- 2026-07-14 ("fix everything" follow-up, this session): fixed `test_db.py`'s hardcoded-date
  drift (relative to `date.today()` now). Built the dead-man's-switch
  (`monitoring/dead_mans_switch.py`, `bot.db.get_last_job_run_date`, separate LaunchAgent
  `tradingbot-deadmansswitch.plist`, `docs/RUNBOOK.md#dead-mans-switch`) — 4 new tests, one
  proven red via a deliberate `sed` weakening before restoring. **Confirmed active**: ran
  successfully via launchd on first bootstrap (real log output, exit 0). Then finished the
  launchd auto-restart investigation for the main bot: user enabled both `python3` entries
  under System Settings -> Allow in the Background, but the main bot's LaunchAgent still hit
  exit 78 — isolated via a throwaway no-`KeepAlive` test plist (same binary, ran instantly)
  that the block is specifically `KeepAlive` (persistent restart-forever daemons), not the
  binary or the toggle; almost certainly because Homebrew's unsigned `python3` can't register
  as that class of background daemon at all. Proposed a periodic-supervisor workaround
  (sidesteps `KeepAlive` via a `StartInterval` check-and-restart script) — user declined,
  paper-trading stakes don't justify it, dead-man's-switch + manual restart is enough.
  Unloaded the main bot's failing LaunchAgent registration to stop the retry loop; it still
  runs via manual `nohup`. Tried 3 more Russell 1000 sources, none viable. Full suite 909
  passed, 0 known failures.

## Open items
- SUE PIT backtest (companyfacts filed dates) before raising its 0.15 weight — recorded in EDGE_BACKLOG.md
- eps_trend daily snapshot collection (estimate revisions) — recorded in EDGE_BACKLOG.md, not built
- Insider routine-buyer filter — recorded in EDGE_BACKLOG.md, viable after ~1yr of insider history
- 2026-07-10: `docs/guardrails/MIGRATION-LOG.md` still shows as modified in `git status` —
  pre-existing uncommitted drift from before this session, not touched this session either.
- Russell 1000 universe unresolved: iShares WAF-blocks both its CSV endpoints regardless of
  headers (`bot/universe.py`, hardened to fail diagnosably, commit 76f0da9). No free/no-signup
  alternate found (Vanguard = JS app shell, GitHub community CSV = dead since 2013, Wikipedia
  has no constituent table, FTSE Russell redirects, stockanalysis.com is a JS shell with no
  exposed API, SlickCharts 403s). Genuinely blocked on the user obtaining `FMP_API_KEY` (or a
  paid data source) — nothing more to try without one.
- launchd auto-restart: CLOSED, not pursuing further (user decision 2026-07-14). Root cause
  confirmed to be `KeepAlive` specifically (persistent restart-forever daemons blocked by
  macOS Background Task Management for an unsigned Homebrew binary), not the System Settings
  toggle (both `python3` entries are enabled) and not a plist bug. A `StartInterval`-based
  supervisor workaround was proposed and declined — paper-trading stakes don't justify it.
  Registration unloaded to stop the retry loop. Bot runs via manual `nohup` indefinitely
  unless this becomes a real pain point later.

## Failed attempts
(none — two test failures during work were fixture/expectation updates, resolved same turn)
