## Surfaces

- `CLAUDE.md` (root, 50 lines / 38 non-blank) — MIGRATE (target file)
- `trading bot/CLAUDE.md` — LEAVE (nested project CLAUDE.md, per MIGRATE.md M1: nested files stay in place and are never edited). FLAG-to-user: this is a substantial, actively-maintained file governing a paper-trading system nearing a live-money launch (week of 2026-07-06) — well outside the scope of this migration, correctly left untouched.
- `.claude/settings.local.json` — LEAVE (permissions allowlist; not inspected further — out of scope for a root-CLAUDE.md-only migration)
- `.claude/settings.json` — none exists
- `~/Downloads/CLAUDE.md` — FLAG-to-user: a second, different, older (2026-05-28) CLAUDE.md exists at a different path with overlapping subject matter (Flask sentiment app). Not touched by this migration; the user's selected target was `~/Documents/Claude code test/CLAUDE.md` (2026-07-02, matches what the thesis repo's CLAUDE.md points to). Worth reconciling or deleting the stale one separately, outside this migration's scope.
- **~18 git worktrees found on disk** under `.worktrees/` and `.claude/worktrees/` (e.g. `fix-circuit-breaker`, `openai-primary-provider`, `fix-hmm-baumwelch`, several `agent-a*` auto-named ones), each containing its own copy of `CLAUDE.md` and `trading bot/CLAUDE.md`. FLAG-to-user: the root CLAUDE.md's own "## Worktrees" section documents only ONE active worktree (`congressional-bot`) — this is stale/inaccurate versus the filesystem. Per this migration's agreed scope (root CLAUDE.md only), none of these worktree copies are touched, and the stale "## Worktrees" text is carried verbatim (transport, not authorship) rather than corrected — flagging so the user can reconcile or clean up stale worktrees separately.
- No `^@` import lines found in CLAUDE.md.

## Snapshot

- `CLAUDE.md.pre-migration-20260705-2224`, 50 lines, hash `7a51a612c7e919b4e937129a46ea908cff02c3fc`
- `git status --porcelain` (excluding the snapshot) is NOT empty (untracked thesis workbook, two superpowers plan docs, `mattpocock-skills/`) — **SNAPSHOT-UNCOMMITTED**, no commit made.

## Disposition table

| # | original text (verbatim) | disposition | destination | note |
|---|---|---|---|---|
| 001 | # CLAUDE.md | DROPPED | - | decoration (contentless title heading) |
| 002 | Project root for Thomas Vromen's finance thesis tooling. Git remote is | KEPT-VERBATIM | CLAUDE.md ## Project | repo-identity fact |
| 003 | `sentiment-analyzer`: this repo began as the Flask sentiment app, with the | KEPT-VERBATIM | CLAUDE.md ## Project | repo-identity fact |
| 004 | trading bot and thesis scripts layered on top. Three things live here. | KEPT-VERBATIM | CLAUDE.md ## Project | repo-identity fact |
| 005 | ## Project map | MOVED | docs/guardrails/PROJECT.md#project-map | anchor heading |
| 006 | - **`app.py` + `static/` + `templates/` + `signals.db`** — Flask **sentiment | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 007 |   analyzer** (the repo's original project). Classifies news headlines via the | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 008 |   Anthropic API; results cached in `signals.db` (SQLite, gitignored). Deps in | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 009 |   root `requirements.txt` (flask, pandas, openpyxl, anthropic). Run: `python app.py`. | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 010 | - **`trading bot/`** — regime-aware paper trading system (primary active work). | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 011 |   Full guidance and status/change history in `trading bot/CLAUDE.md`. Run tests | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 012 |   with `pytest` from inside `trading bot/` (818 tests). Has its own deps and | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 013 |   data caches. Preparing for live (paper-money) Alpaca trading starting the | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 014 |   week of 2026-07-06; a full pre-launch review (2026-07-02) fixed 2 new bugs | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 015 |   found in strategy code added since the last standalone review, added missing | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 016 |   test coverage, and added operational readiness docs (`docs/RUNBOOK.md`, | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 017 |   alert webhook config) — see `trading bot/CLAUDE.md`'s status banner for | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 018 |   details. Outstanding: user must set `ALERT_WEBHOOK_URL` before relying on | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 019 |   unattended alerting. | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 020 | - **`docs/superpowers/`** — `plans/` and `specs/` for past and current work. | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 021 | Thesis inputs at root: `stoxx600_constituents.csv`, `Thesis STOXX, and time | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 022 | series (1).xlsx`. Standalone trading-bot bug-audit docs (`TRADING_BOT_REVIEW_*.md`, | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 023 | `TRADING_BOT_FULL_REVIEW_BUNDLE.md`) are removed once fully remediated — the | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 024 | running history lives in `trading bot/CLAUDE.md`'s status banner instead of | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 025 | separate files, so it doesn't go stale. | MOVED | docs/guardrails/PROJECT.md#project-map | |
| 026 | ## Data not in this repo | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | anchor heading |
| 027 | Headline-pull artifacts were moved out of the repo (too large / disposable): | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | |
| 028 | - `~/Documents/Thesis Headline Run/` — the filtered headline datasets used in | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | |
| 029 |   the thesis, plus the scripts that produced them (`extract_headlines.py`, | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | |
| 030 |   `apply-filter_v3_8_1.py`, `run_extraction.sh`). | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | |
| 031 | - `~/Documents/Not Claude Code Test/` — raw per-ticker LSEG pull caches | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | |
| 032 |   (`pilot_cache/`, `pilot_cache_2026_jan_feb/`) and extraction logs. Disposable. | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | |
| 033 | Trading-bot scraper caches (`capitol_trades_*.json`, `universe_cache.json`, | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | |
| 034 | `propublica_committee_cache/`) are gitignored — they regenerate, not source. | MOVED | docs/guardrails/PROJECT.md#data-not-in-this-repo | |
| 035 | Global preferences (communication style, git workflow) are in `~/.claude/CLAUDE.md`. | KEPT-VERBATIM | CLAUDE.md ## Project | short high-value cross-reference, kept always-loaded |
| 036 | ## Worktrees | MOVED | docs/guardrails/PROJECT.md#worktrees | anchor heading |
| 037 | One active git worktree under `.worktrees/`: | MOVED | docs/guardrails/PROJECT.md#worktrees | stale vs filesystem (see Surfaces) — carried verbatim, not corrected |
| 038 | - `.worktrees/congressional-bot/` — branch `feature/congressional-bot` | MOVED | docs/guardrails/PROJECT.md#worktrees | |

Row count check: 38 numbered original non-blank lines, 38 table rows. EQUAL.

Disposition counts: MOVED = 33, KEPT-VERBATIM = 4, DROPPED = 1 (title heading, pure decoration), CONFLICT-PENDING = 0, UNSORTED = 0.

## CONFLICTS

Scanned with `grep -inE "\b(must|never|always|don'?t|do not|only|forbidden|not)\b"` and cross-checked subject matter against kit docs. This file is pure project-identity/map content (no process/behavioral rules), so no conflicts with kit process rules are possible. None found.

## Kit-doc collisions

_FORMAT.md: installed (hash 831d91e4f7adfea1ee574f5abd9a880c463b6672)
PLAN.md: installed (hash ae17b8e12e047d890a4d3ea7205d5a74801e2047)
CODE.md: installed (hash 65c2542895a9c9b13175985702cbd2a53df38b22)
DEBUG.md: installed (hash e3edc7baba5eefe0e73f4f08a6d6840e1ec38a33)
VERIFY.md: installed (hash d13f688ec24f4e2d3f563783388d8a4b1e60d591)
EFFICIENCY.md: installed (hash ca31f598e5b4d2b2925f059d7b58e256a7021a38)
SESSION.md: installed (hash 9feacdfadfbf24b20c57c612bccdd07c4f6bc65b)
TRAPS.md: installed (hash 6ac22082de76ba5d51a8c36a7041f6d8809d14d7)
All 8 hash pairs (installed vs kit source) verified equal at install time.

## Project (proposed content for new CLAUDE.md)

See draft posted in chat at the M5 checkpoint. Full transported reference material lives in `docs/guardrails/PROJECT.md` (3 anchors: project-map, data-not-in-this-repo, worktrees), each with a one-line zone-2 pointer.

Fable review pass, 2026-07-05: tightened zone-2 intro, dropped the global-preferences pointer (global CLAUDE.md is auto-loaded), fixed docs.superpowers typo in the project-map pointer; PROJECT.md content untouched (worktrees staleness re-flagged in chat).

Dedupe, 2026-07-05 (user request, after review pass): KIT CORE+FOOTER removed from this repo's CLAUDE.md — the kit's always-loaded rules now come solely from ~/.claude/CLAUDE.md (loaded in every session on this machine). This file's ## Project zone + docs/guardrails/ on-demand docs are unchanged. If this repo is ever used without that global file, re-copy CORE+FOOTER from ~/.claude/CLAUDE.md or any .pre-fable-review-20260705-2250 backup.
