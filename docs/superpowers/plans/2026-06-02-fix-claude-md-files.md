# Fix CLAUDE.md Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four identified problems in the project's CLAUDE.md files so Claude Code loads the right guidance in every context.

**Architecture:** Four independent fixes — move the global config to the right location, patch stale data in the trading bot CLAUDE.md, replace wrong content in two worktree branches, and add a root-level entry-point file.

**Tech Stack:** Shell, Git, Markdown

---

## File Map

| Action | Path |
|--------|------|
| Move (rename) | `CLAUDE.global.md` → `~/.claude/CLAUDE.md` |
| Modify | `trading bot/CLAUDE.md` (test count, co-author trailer) |
| Modify (on branch `trading-bot-fixes`) | `.worktrees/trading-bot-fixes/CLAUDE.md` |
| Modify (on branch `feature/congressional-bot`) | `.worktrees/congressional-bot/CLAUDE.md` |
| Create | `CLAUDE.md` (project root) |

---

## Task 1: Move global CLAUDE.md to the correct location

Claude Code only auto-loads files named exactly `CLAUDE.md`. The file currently named `CLAUDE.global.md` in the project root is silently ignored. It also has a slightly different co-author trailer than the trading bot file.

**Files:**
- Delete: `CLAUDE.global.md` (project root)
- Create: `~/.claude/CLAUDE.md`

- [ ] **Step 1: Copy the content to the real global location**

```bash
cp "/Users/thomasvromen/Documents/Claude code test/CLAUDE.global.md" ~/.claude/CLAUDE.md
```

- [ ] **Step 2: Fix the co-author trailer inside `~/.claude/CLAUDE.md`**

Open `~/.claude/CLAUDE.md`. Find this line in the Git workflow section:

```
Co-Authored-By: Claude <noreply@anthropic.com>
```

Replace it with:

```
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

- [ ] **Step 3: Verify the file looks correct**

```bash
cat ~/.claude/CLAUDE.md
```

Expected: full file with `# CLAUDE.md (global)` header, communication style, working style, git workflow sections, and the updated co-author trailer.

- [ ] **Step 4: Delete the now-redundant `CLAUDE.global.md` from the project root**

```bash
rm "/Users/thomasvromen/Documents/Claude code test/CLAUDE.global.md"
```

- [ ] **Step 5: Verify it's gone**

```bash
ls "/Users/thomasvromen/Documents/Claude code test/CLAUDE.global.md" 2>&1
```

Expected: `No such file or directory`

> No git commit for this task — `~/.claude/CLAUDE.md` is outside all repos, and `CLAUDE.global.md` was untracked so deleting it leaves the working tree clean.

---

## Task 2: Update `trading bot/CLAUDE.md`

The file has a stale test count (says ~389, actual is 488). The reference to `~/.claude/CLAUDE.md` is now correct after Task 1 and needs no change.

**Files:**
- Modify: `trading bot/CLAUDE.md` (line 87)

- [ ] **Step 1: Open `trading bot/CLAUDE.md` and find the test count line**

Look for this line (around line 87):

```
cd "trading bot" && pytest            # ~389 tests; keep green
```

- [ ] **Step 2: Update the test count and fix the command**

Replace that line with:

```
pytest                                 # ~488 tests; keep green (run from inside trading bot/)
```

The `cd "trading bot" &&` prefix was fragile — if you're already inside the directory it errors. Just `pytest` is correct when working from within `trading bot/`.

- [ ] **Step 3: Verify the change looks right**

```bash
grep -n "488\|pytest" "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md"
```

Expected output includes a line like:
```
87:pytest                                 # ~488 tests; keep green (run from inside trading bot/)
```

- [ ] **Step 4: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: update test count to 488, fix pytest command

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Fix `trading-bot-fixes` worktree CLAUDE.md

The `trading-bot-fixes` branch has a CLAUDE.md with Flask/sentiment-analyzer content — completely wrong for a trading bot worktree. Replace it with the correct trading bot content.

**Files:**
- Modify (on branch `trading-bot-fixes`): `.worktrees/trading-bot-fixes/CLAUDE.md`

- [ ] **Step 1: Copy the up-to-date trading bot CLAUDE.md into the worktree**

```bash
cp "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md" \
   "/Users/thomasvromen/Documents/Claude code test/.worktrees/trading-bot-fixes/CLAUDE.md"
```

- [ ] **Step 2: Verify the first few lines are the trading bot content, not Flask**

```bash
head -15 "/Users/thomasvromen/Documents/Claude code test/.worktrees/trading-bot-fixes/CLAUDE.md"
```

Expected: starts with `# CLAUDE.md` followed by the `⚠️ ACTIVE IMPLEMENTATION PLAN` block and trading bot purpose statement. Must NOT contain `Flask backend` or `app.py`.

- [ ] **Step 3: Commit from inside the worktree**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/.worktrees/trading-bot-fixes"
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: replace Flask app CLAUDE.md with correct trading bot content

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify the commit landed on the right branch**

```bash
git -C "/Users/thomasvromen/Documents/Claude code test/.worktrees/trading-bot-fixes" log --oneline -3
```

Expected: the new commit is at the top, on branch `trading-bot-fixes`.

---

## Task 4: Fix `feature/congressional-bot` worktree CLAUDE.md

Same problem as Task 3 — this branch also has the Flask app CLAUDE.md.

**Files:**
- Modify (on branch `feature/congressional-bot`): `.worktrees/congressional-bot/CLAUDE.md`

- [ ] **Step 1: Copy the trading bot CLAUDE.md into this worktree**

```bash
cp "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md" \
   "/Users/thomasvromen/Documents/Claude code test/.worktrees/congressional-bot/CLAUDE.md"
```

- [ ] **Step 2: Verify content**

```bash
head -15 "/Users/thomasvromen/Documents/Claude code test/.worktrees/congressional-bot/CLAUDE.md"
```

Expected: same trading bot header. Must NOT contain `Flask backend` or `app.py`.

- [ ] **Step 3: Commit from inside the worktree**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/.worktrees/congressional-bot"
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: replace Flask app CLAUDE.md with correct trading bot content

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify commit is on `feature/congressional-bot`**

```bash
git -C "/Users/thomasvromen/Documents/Claude code test/.worktrees/congressional-bot" log --oneline -3
```

Expected: new commit at top, on branch `feature/congressional-bot`.

---

## Task 5: Add root-level CLAUDE.md

The project root (`Claude code test/`) has no CLAUDE.md. Sessions started here get zero context. Add a short entry-point file that orients Claude without duplicating the bot's detailed docs.

**Files:**
- Create: `CLAUDE.md` (project root — `Claude code test/CLAUDE.md`)

- [ ] **Step 1: Create the file with this exact content**

```markdown
# CLAUDE.md

Project root for Thomas Vromen's finance thesis tooling. Two active sub-projects:

- **`trading bot/`** — regime-aware paper trading system (primary). Full guidance in `trading bot/CLAUDE.md`. Run tests with `cd "trading bot" && pytest` (~488 tests).
- **`docs/superpowers/plans/`** — implementation plans for past and current work.

Miscellaneous analysis scripts (headline filter, STOXX600 extraction) live at the root.

Global preferences (communication style, git workflow) are in `~/.claude/CLAUDE.md`.
```

Save to: `/Users/thomasvromen/Documents/Claude code test/CLAUDE.md`

- [ ] **Step 2: Verify the file exists and reads correctly**

```bash
cat "/Users/thomasvromen/Documents/Claude code test/CLAUDE.md"
```

Expected: the content above, no truncation.

- [ ] **Step 3: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test"
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: add root-level CLAUDE.md entry point

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Verification Checklist

After all tasks are done, confirm:

- [ ] `~/.claude/CLAUDE.md` exists and contains the communication/working style/git sections
- [ ] `cat ~/.claude/CLAUDE.md | grep "Sonnet 4.6"` returns the co-author line
- [ ] `grep "488" "/Users/thomasvromen/Documents/Claude code test/trading bot/CLAUDE.md"` returns the test count line
- [ ] `head -5 "/Users/thomasvromen/Documents/Claude code test/.worktrees/trading-bot-fixes/CLAUDE.md"` shows trading bot content
- [ ] `head -5 "/Users/thomasvromen/Documents/Claude code test/.worktrees/congressional-bot/CLAUDE.md"` shows trading bot content
- [ ] `cat "/Users/thomasvromen/Documents/Claude code test/CLAUDE.md"` shows the entry-point file
- [ ] `ls "/Users/thomasvromen/Documents/Claude code test/CLAUDE.global.md" 2>&1` shows "No such file or directory"
