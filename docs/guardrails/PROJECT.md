<!-- Project-specific reference material, transported verbatim from the pre-migration CLAUDE.md during the guardrails-kit migration (see MIGRATION-LOG.md). Never reformat this content; it is exempt from _FORMAT.md doc-shape rules. -->

<a id="project-map"></a>
## Project map

- **`app.py` + `static/` + `templates/` + `signals.db`** — Flask **sentiment
  analyzer** (the repo's original project). Classifies news headlines via the
  Anthropic API; results cached in `signals.db` (SQLite, gitignored). Deps in
  root `requirements.txt` (flask, pandas, openpyxl, anthropic). Run: `python app.py`.
- **`trading bot/`** — regime-aware paper trading system (primary active work).
  Full guidance and status/change history in `trading bot/CLAUDE.md`. Run tests
  with `pytest` from inside `trading bot/` (818 tests). Has its own deps and
  data caches. Preparing for live (paper-money) Alpaca trading starting the
  week of 2026-07-06; a full pre-launch review (2026-07-02) fixed 2 new bugs
  found in strategy code added since the last standalone review, added missing
  test coverage, and added operational readiness docs (`docs/RUNBOOK.md`,
  alert webhook config) — see `trading bot/CLAUDE.md`'s status banner for
  details. Outstanding: user must set `ALERT_WEBHOOK_URL` before relying on
  unattended alerting.
- **`docs/superpowers/`** — `plans/` and `specs/` for past and current work.

Thesis inputs at root: `stoxx600_constituents.csv`, `Thesis STOXX, and time
series (1).xlsx`. Standalone trading-bot bug-audit docs (`TRADING_BOT_REVIEW_*.md`,
`TRADING_BOT_FULL_REVIEW_BUNDLE.md`) are removed once fully remediated — the
running history lives in `trading bot/CLAUDE.md`'s status banner instead of
separate files, so it doesn't go stale.

<a id="data-not-in-this-repo"></a>
## Data not in this repo

Headline-pull artifacts were moved out of the repo (too large / disposable):

- `~/Documents/Thesis Headline Run/` — the filtered headline datasets used in
  the thesis, plus the scripts that produced them (`extract_headlines.py`,
  `apply-filter_v3_8_1.py`, `run_extraction.sh`).
- `~/Documents/Not Claude Code Test/` — raw per-ticker LSEG pull caches
  (`pilot_cache/`, `pilot_cache_2026_jan_feb/`) and extraction logs. Disposable.

Trading-bot scraper caches (`capitol_trades_*.json`, `universe_cache.json`,
`propublica_committee_cache/`) are gitignored — they regenerate, not source.

<a id="worktrees"></a>
## Worktrees

19 worktrees exist (verified via `git worktree list`, 2026-07-06):

- `.worktrees/congressional-bot/` — branch `feature/congressional-bot` (the one deliberate worktree)
- 18 under `.claude/worktrees/` — auto-created by past Claude Code agent sessions (`agent-a*`, `worktree-*` names) plus fix branches (`fix-circuit-breaker`, `fix-hmm-baumwelch`, `fix-scraper-vocab`, `fix-stop-cancellation`). Prune with `git worktree remove <path>`, never by deleting directories by hand.

<a id="flask-app"></a>
## Flask sentiment app (moved here from ~/Downloads/CLAUDE.md, 2026-07-06 — content as of 2026-05-28)

**Purpose:** local web app that classifies financial-news headlines as buy/neutral/sell using the Anthropic API, over a user-uploaded CSV/Excel dataset.

**Stack:** single-file Flask backend (`app.py`); vanilla ES2020 frontend (`static/app.js`) with Bootstrap 5 — no build step, no bundler; SQLite (`signals.db`); deps in root `requirements.txt`. Keep this shape: no frontend framework, no build tooling, no ORM.

**Run:** `pip install -r requirements.txt && python app.py` → http://localhost:8080. First run creates `signals.db`. Reset local state: stop app, delete `signals.db`, restart (drops classifications + settings; uploaded data file untouched). Inspect: `sqlite3 signals.db ".tables"`.

**Data flow:** `POST /api/upload` (save file, return columns) → `POST /api/settings` (column mapping as JSON) → `GET /api/data` (pandas read + SQLite join, paginated) → `POST /api/process` (batch classify via Anthropic API, persist) → `GET /api/export` (merged CSV download).

**Persistence gotchas:**
- Uploaded file stays at its original disk path; only the path lives in `settings`. Move the file and the app breaks.
- `row_id` = SHA-256 of `ticker|date|headline` — dedup key AND join key. Never change the hash inputs or format: it orphans every existing classification.
- `_df_cache` (in-memory DataFrame cache) must be invalidated whenever file path or column mapping changes, or you serve stale data. Any new df-loading code path must invalidate it too.

**Frontend:** all state in the `state` object; processing runs in chunks of 50 row IDs; the stop button sets `state.stopProcessing`, checked between chunks — keep that check intact.

**Classification (`/api/process`):** structured system prompt enforcing `{"signal": "buy"|"neutral"|"sell", "reason": "..."}`; retries 3x on RateLimitError with 5s/10s/20s back-off; 0.4s sleep between rows — keep the back-off and sleep, removing them trips rate limits. Model string read from `settings`; don't hardcode a new one without confirming it's a current Anthropic model.

**Verifying:** no test suite. Smoke test after backend changes: upload small CSV → map columns → load data (pagination) → process a few rows (result persists across reload) → export. New tests go under `tests/`, pytest.

**Security:** API key lives in the `settings` table, server-side only; settings GET returns `api_key_set: bool`, never the key. Never expose the key to the frontend or logs. Never commit `signals.db`, uploaded data files, or `.env`.

**Gotchas:** date filtering is lexicographic string comparison — date columns must be ISO (YYYY-MM-DD) or filtering breaks silently. SQLite is single-writer — serialize writes, no concurrent processing.
