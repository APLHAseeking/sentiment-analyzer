# CLAUDE.md

Project root for Thomas Vromen's finance thesis tooling. Git remote is
`sentiment-analyzer`: this repo began as the Flask sentiment app, with the
trading bot and thesis scripts layered on top. Three things live here.

## Project map

- **`app.py` + `static/` + `templates/` + `signals.db`** — Flask **sentiment
  analyzer** (the repo's original project). Classifies news headlines via the
  Anthropic API; results cached in `signals.db` (SQLite, gitignored). Deps in
  root `requirements.txt` (flask, pandas, openpyxl, anthropic). Run: `python app.py`.
- **`trading bot/`** — regime-aware paper trading system (primary active work).
  Full guidance in `trading bot/CLAUDE.md`. Run tests with `pytest` from inside
  `trading bot/` (721 tests). Has its own deps and data caches.
- **`docs/superpowers/`** — `plans/` and `specs/` for past and current work.

Thesis inputs at root: `stoxx600_constituents.csv`, `Thesis STOXX, and time
series (1).xlsx`. `TRADING_BOT_REVIEW_2026-06-23.md` is the latest full bug-audit
of the bot (supersedes earlier review/plan docs, which were removed).

## Data not in this repo

Headline-pull artifacts were moved out of the repo (too large / disposable):

- `~/Documents/Thesis Headline Run/` — the filtered headline datasets used in
  the thesis, plus the scripts that produced them (`extract_headlines.py`,
  `apply-filter_v3_8_1.py`, `run_extraction.sh`).
- `~/Documents/Not Claude Code Test/` — raw per-ticker LSEG pull caches
  (`pilot_cache/`, `pilot_cache_2026_jan_feb/`) and extraction logs. Disposable.

Trading-bot scraper caches (`capitol_trades_*.json`, `universe_cache.json`,
`propublica_committee_cache/`) are gitignored — they regenerate, not source.

Global preferences (communication style, git workflow) are in `~/.claude/CLAUDE.md`.

## Worktrees

One active git worktree under `.worktrees/`:

- `.worktrees/congressional-bot/` — branch `feature/congressional-bot`
