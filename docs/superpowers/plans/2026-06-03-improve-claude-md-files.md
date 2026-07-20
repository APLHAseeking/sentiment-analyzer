# Improve CLAUDE.md Files — Round 2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix eight remaining issues across four CLAUDE.md files — stale banner, undocumented modules, missing docs reference, stale data caveats, missing .gitignore, untracked plan file, global commit style, and worktrees mention.

**Architecture:** Five independent file edits; all are documentation and config only — no code changes.

**Tech Stack:** Shell, Git, Markdown

---

## File Map

| Action | Path |
|--------|------|
| Modify | `trading bot/CLAUDE.md` (banner, architecture, docs section, data caveats) |
| Create | `trading bot/.gitignore` |
| Track (git add + commit) | `TRADING_BOT_REVIEW_PLAN.md` (project root) |
| Modify | `~/.claude/CLAUDE.md` (push instruction, commit format) |
| Modify | `CLAUDE.md` (project root — add worktrees) |

---

## Task 1: Update `trading bot/CLAUDE.md`

Four targeted edits in one commit. All paths below are inside `/Users/thomasvromen/Documents/Claude code test/trading bot/`.

**Files:**
- Modify: `trading bot/CLAUDE.md`

### Edit A — Fix stale ⚠️ banner (lines 3–9)

- [ ] **Step 1: Replace the banner block**

Find this exact text (lines 3–9):

```
> **⚠️ ACTIVE IMPLEMENTATION PLAN — READ FIRST**
> A reviewed, phased implementation plan lives at `../TRADING_BOT_REVIEW_PLAN.md`
> (i.e. `Claude code test/TRADING_BOT_REVIEW_PLAN.md`, one level up from this repo).
> It is the source of truth for current work: risk-adjusted metrics & factor attribution,
> a point-in-time/survivorship-free backtest, deterministic (non-LLM) position sizing,
> realistic backtest costs, broker stop orders, and dead-feed alerts. Start there before
> making changes. **Phase 0 has a hard gate** — measure factor-adjusted alpha before adding complexity.
```

Replace with:

```
> **⚠️ IMPLEMENTATION PLAN & PHASE STATUS — READ FIRST**
> Full plan: `../TRADING_BOT_REVIEW_PLAN.md`. Phase 0 gate: **BLOCKED ON DATA** — real point-in-time
> data not yet acquired; all historical performance numbers are look-ahead biased until then.
> See `docs/PHASE0_FINDINGS.md` for gate decision rules and required datasets.
> Phases 1–3 are fully implemented; paper trading is operational.
```

### Edit B — Add undocumented modules to Architecture section (after line 70)

- [ ] **Step 2: After the Monitoring line, insert three new module descriptions**

Find this exact line (line 70):

```
**Monitoring (`monitoring/`):** structured JSON logging with an `EventType` enum (`emit_event`) and pluggable alert senders (`fire_alert`, webhook/log).
```

Replace with:

```
**Monitoring (`monitoring/`):** structured JSON logging with an `EventType` enum (`emit_event`) and pluggable alert senders (`fire_alert`, webhook/log).

**Feature engineering (`features/feature_pipeline.py`):** `FeatureConfig` dataclass + causal feature computation (vol, trend, momentum, drawdown, VIX) consumed by the regime engine. All features strictly forward-only — no look-ahead.

**Market data (`market_data/market_feed.py`):** fetches daily SPY/VIX bars via yfinance for the regime engine. Separate from individual-stock data in `bot/researcher.py`.

**Performance tracking (`performance/tracker.py`):** `PerformanceTracker` reads live `trading.db` and computes the same metrics as `backtesting.metrics.compute_all` — enabling direct live vs backtest comparison.
```

### Edit C — Add `## Key documents` section (insert before `## Scheduler`)

- [ ] **Step 3: Insert a new section before the Scheduler section**

Find this exact line (line 72):

```
## Scheduler (Amsterdam time, NYSE-session guarded)
```

Insert this block immediately before it:

```
## Key documents (`docs/`)

- `PHASE0_FINDINGS.md` — Phase 0 gate status (BLOCKED ON DATA); required datasets and pass/fail rules
- `DATA_SOURCES.md` — all external data sources, current status, and fallback behaviour
- `PIT_DATA_REQUIREMENTS.md` — schemas for point-in-time data needed to unblock Phase 0
- `CONGRESSIONAL_EDGE.md` — congressional trading edge analysis
- `HEDGE_ANALYSIS.md` — inverse-ETF hedge analysis

## Scheduler (Amsterdam time, NYSE-session guarded)
```

(Keep the `## Scheduler` heading — you're inserting before it, not replacing it.)

### Edit D — Update stale data-source caveats (lines 97–98)

- [ ] **Step 4: Replace the two stale caveat bullets**

Find these two lines (lines 97–98):

```
- **Capitol Trades is a JavaScript SPA.** The static-HTML scraper in `bot/scraper.py` likely returns **0 rows** in production — the congressional feed can be silently empty. `run_1year_backtest.py` therefore reads a cached JSON snapshot (`capitol_trades_merged.json`, Oct 2025→May 2026 only).
- **The ProPublica Congress API used by `bot/committee.py` is discontinued.** Committee lookups may fail and fall back to a stale `propublica_committee_cache` shelve.
```

Replace with:

```
- **Capitol Trades is a JavaScript SPA.** `bot/scraper.py` tries the JSON API endpoint first (`_fetch_page_json`); HTML scraper is the fallback. If both fail, a `DEAD_FEED` alert fires and the congressional pipeline receives zero inputs for that run. `run_1year_backtest.py` reads a cached JSON snapshot (`capitol_trades_merged.json`, Oct 2025→May 2026 only). See `docs/DATA_SOURCES.md`.
- **ProPublica Congress API is discontinued.** `bot/committee.py` now uses the `unitedstates/congress-legislators` GitHub YAML files (no API key). A 30-day shelve disk cache insulates against transient GitHub outages.
```

### Verify and commit

- [ ] **Step 5: Verify all four edits**

```bash
grep -n "BLOCKED ON DATA\|feature_pipeline\|market_feed\|PerformanceTracker\|Key documents\|_fetch_page_json\|unitedstates" \
  "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md"
```

Expected: each of those strings appears exactly once.

- [ ] **Step 6: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: update CLAUDE.md — phase status, undocumented modules, docs section, data caveats

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `.gitignore` to the trading bot directory

No `.gitignore` exists. Runtime files (`trading.db`, `.env`, cached JSON, `regime_model.joblib`) are unprotected.

**Files:**
- Create: `trading bot/.gitignore`

- [ ] **Step 1: Create the file**

Write this exact content to `/Users/thomasvromen/Documents/Claude code test/trading bot/.gitignore`:

```gitignore
# Runtime / generated
trading.db
regime_model.joblib
dashboard_state.json

# Data caches (large, generated — not source)
universe_cache.json
capitol_trades_*.json
propublica_committee_cache*
pilot_cache/
pilot_cache_*/

# Secrets
.env

# Python
__pycache__/
*.py[cod]
*.pyo
.pytest_cache/
*.egg-info/
dist/
build/
.coverage
htmlcov/

# OS
.DS_Store
```

- [ ] **Step 2: Verify the file was created**

```bash
cat "/Users/thomasvromen/Documents/Claude code test/trading bot/.gitignore"
```

- [ ] **Step 3: Verify git now ignores the right files**

```bash
git -C "/Users/thomasvromen/Documents/Claude code test/trading bot" check-ignore -v \
  trading.db regime_model.joblib .env capitol_trades_merged.json universe_cache.json
```

Expected: each file is listed as ignored (shows the `.gitignore` rule that matched).

- [ ] **Step 4: Confirm tests still pass (gitignore must not accidentally exclude source files)**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && python3 -m pytest -q --tb=short 2>&1 | tail -5
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add .gitignore
git commit -m "$(cat <<'EOF'
chore: add .gitignore — exclude runtime, cache, and secret files

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Commit `TRADING_BOT_REVIEW_PLAN.md`

The plan file is referenced as "source of truth" in the CLAUDE.md banner but has never been committed — it would be lost if the file were deleted or the repo cloned.

**Files:**
- Track (git add): `TRADING_BOT_REVIEW_PLAN.md` (at project root `/Users/thomasvromen/Documents/Claude code test/`)

- [ ] **Step 1: Verify the file exists and is untracked**

```bash
git -C "/Users/thomasvromen/Documents/Claude code test" status --short TRADING_BOT_REVIEW_PLAN.md
```

Expected: `?? TRADING_BOT_REVIEW_PLAN.md`

- [ ] **Step 2: Commit it**

```bash
cd "/Users/thomasvromen/Documents/Claude code test"
git add TRADING_BOT_REVIEW_PLAN.md
git commit -m "$(cat <<'EOF'
docs: track TRADING_BOT_REVIEW_PLAN.md — source of truth for phased implementation

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Verify it's now tracked**

```bash
git -C "/Users/thomasvromen/Documents/Claude code test" status --short TRADING_BOT_REVIEW_PLAN.md
```

Expected: no output (file is clean and tracked).

---

## Task 4: Fix `~/.claude/CLAUDE.md`

Two issues: unconditional `git push` (fails with no remote), and commit message examples use non-conventional format while all actual commits use `feat:`/`fix:`/`docs:`.

**Files:**
- Modify: `~/.claude/CLAUDE.md`

- [ ] **Step 1: Fix the push instruction**

Find this line:

```
- Run `git push` after every commit.
```

Replace with:

```
- Push after committing if a remote is configured (`git remote -v` to check before pushing).
```

- [ ] **Step 2: Fix commit message examples**

Find this line:

```
- Clean, imperative commit messages (`Add export filter for date range`, `Fix pagination off-by-one`).
```

Replace with:

```
- Conventional commit messages (`feat: add export filter for date range`, `fix: pagination off-by-one`, `docs: update readme`).
```

- [ ] **Step 3: Verify both changes**

```bash
grep -n "remote\|Conventional\|push\|imperative" ~/.claude/CLAUDE.md
```

Expected: shows "Conventional commit messages" and "remote is configured" lines; old "Run \`git push\`" and "Clean, imperative" lines are gone.

> No git commit — `~/.claude/CLAUDE.md` is outside all repos.

---

## Task 5: Update root `CLAUDE.md` — add worktrees mention

The root CLAUDE.md doesn't mention the two active git worktrees, so anyone opening a session here has no idea the branches exist.

**Files:**
- Modify: `/Users/thomasvromen/Documents/Claude code test/CLAUDE.md`

- [ ] **Step 1: Append worktrees section**

Find this exact line (currently the last line):

```
Global preferences (communication style, git workflow) are in `~/.claude/CLAUDE.md`.
```

Replace with:

```
Global preferences (communication style, git workflow) are in `~/.claude/CLAUDE.md`.

## Worktrees

Two active git worktrees under `.worktrees/`:

- `.worktrees/trading-bot-fixes/` — branch `trading-bot-fixes`
- `.worktrees/congressional-bot/` — branch `feature/congressional-bot`
```

- [ ] **Step 2: Verify**

```bash
cat "/Users/thomasvromen/Documents/Claude code test/CLAUDE.md"
```

Expected: file ends with the two worktree bullet points.

- [ ] **Step 3: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test"
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: add worktrees section to root CLAUDE.md

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Verification Checklist

After all tasks:

- [ ] `grep "BLOCKED ON DATA" "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md"` → found
- [ ] `grep "feature_pipeline\|market_feed\|PerformanceTracker" "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md"` → all three found
- [ ] `grep "Key documents" "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md"` → found
- [ ] `grep "_fetch_page_json" "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md"` → found
- [ ] `grep "unitedstates" "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md"` → found
- [ ] `cat "/Users/thomasvromen/Documents/Claude code test/trading bot/.gitignore"` → file exists
- [ ] `git -C "/Users/thomasvromen/Documents/Claude code test" log --oneline -6` → shows all four new commits
- [ ] `git -C "/Users/thomasvromen/Documents/Claude code test" status --short TRADING_BOT_REVIEW_PLAN.md` → no output (tracked, clean)
- [ ] `grep "Conventional commit\|remote is configured" ~/.claude/CLAUDE.md` → both found
- [ ] `grep "worktrees\|trading-bot-fixes\|congressional-bot" "/Users/thomasvromen/Documents/Claude code test/CLAUDE.md"` → all found
