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

One active git worktree under `.worktrees/`:

- `.worktrees/congressional-bot/` — branch `feature/congressional-bot`
