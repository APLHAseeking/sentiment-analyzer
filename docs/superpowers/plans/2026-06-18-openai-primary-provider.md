# OpenAI Primary Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenAI (`gpt-5.4`) the default LLM provider for entry/exit/technical scoring in `bot/ai_analyst.py`, with Anthropic Claude kept available via a `Settings.llm_provider` config switch.

**Architecture:** A single low-level function (`_claude_call`, renamed `_llm_call`) branches on `settings.llm_provider`. All five existing callers (`_bull_argument`, `_bear_argument`, `score_entry`, `review_exit`, `score_technical`) call through it unchanged. The default flips from `"anthropic"` to `"openai"` only in the final task, after both paths are built and tested — every commit before that leaves the test suite green with today's exact behavior.

**Tech Stack:** Python 3.11+, `anthropic>=0.40.0` and `openai>=1.30.0` (both already in `requirements.txt`), pytest + pytest-mock (offline, no network).

Full design context: `docs/superpowers/specs/2026-06-18-openai-primary-provider-design.md` (read before executing — this plan implements it task-by-task and does not re-litigate any design decision; one placement correction was made during planning — `llm_provider` lives on top-level `Settings`, not inside `Credentials` — see that file for the updated rationale).

---

## File Structure

**Modified files only — no new files:**
- `system/config.py` — `Settings` gains `llm_provider`; `Settings.validate()` gains a check.
- `bot/ai_analyst.py` — `import openai as _openai`; new `_get_openai_client()`; `_claude_call` → `_llm_call` with provider branch; `_call_with_retry` catches `openai.RateLimitError` too; five call sites updated.
- `tests/test_config.py` — two new tests.
- `tests/test_ai_analyst.py` — one new autouse fixture; new OpenAI-path tests.
- `trading bot/CLAUDE.md` — stack line updated.

All file paths below are relative to `trading bot/`, except git commands which give full paths from the repo root `/Users/thomasvromen/Documents/Claude code test/` (pytest runs from inside `trading bot/`, git runs from the repo root).

---

## Task 1: Config — `llm_provider` (defaults to `"anthropic"` for now)

**Files:**
- Modify: `system/config.py:280-298` (`Settings` dataclass), `:301-316` (`validate`)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_settings_llm_provider_defaults_to_anthropic_for_now():
    s = Settings()
    assert s.llm_provider == "anthropic"


def test_validate_rejects_unknown_llm_provider():
    from dataclasses import replace
    s = replace(Settings(), llm_provider="cohere")
    with pytest.raises(ValueError, match="llm_provider"):
        s.validate()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_config.py -v -k llm_provider`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'llm_provider'`

- [ ] **Step 3: Implement**

In `system/config.py`, change the `Settings` dataclass (currently lines 280-285) from:

```python
@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: resolve(_env("DB_PATH", "trading.db")))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    timezone: str = "Europe/Amsterdam"
```

to:

```python
@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: resolve(_env("DB_PATH", "trading.db")))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    timezone: str = "Europe/Amsterdam"
    # TODO(Task 7 of the openai-primary-provider plan): flip default to "openai".
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "anthropic"))
```

Then change `validate()` (currently lines 301-316) by adding one check. Find:

```python
        if not (0 < self.sizing.per_trade_risk_pct <= self.risk.max_position_pct):
            raise ValueError("per_trade_risk_pct must be in (0, max_position_pct]")
```

and add directly after it (still inside `validate`, before the closing of the method):

```python
        if self.llm_provider not in ("anthropic", "openai"):
            raise ValueError(f"llm_provider must be 'anthropic' or 'openai', got {self.llm_provider!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_config.py -v`
Expected: PASS (all tests in the file, no regressions)

- [ ] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/system/config.py" "trading bot/tests/test_config.py" && git commit -m "$(cat <<'EOF'
feat: add llm_provider setting (defaults to anthropic for now)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 2: OpenAI client getter (`bot/ai_analyst.py`)

**Files:**
- Modify: `bot/ai_analyst.py:1-8` (imports), `:164-175` (near `_get_client`)
- Test: `tests/test_ai_analyst.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ai_analyst.py`:

```python
def test_get_openai_client_raises_without_key(mocker):
    import dataclasses
    from system.config import settings as real_settings
    bad_settings = dataclasses.replace(
        real_settings,
        credentials=dataclasses.replace(real_settings.credentials, openai_api_key=""),
    )
    mocker.patch("system.config.settings", bad_settings)
    mocker.patch("bot.ai_analyst._openai_client", None)

    from bot.ai_analyst import _get_openai_client
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _get_openai_client()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v -k test_get_openai_client_raises_without_key`
Expected: FAIL with `ImportError: cannot import name '_get_openai_client' from 'bot.ai_analyst'`

- [ ] **Step 3: Implement**

In `bot/ai_analyst.py`, change the import block (currently lines 1-8):

```python
import json
import logging
import time
from dataclasses import dataclass

import anthropic as _anthropic

log = logging.getLogger(__name__)
```

to:

```python
import json
import logging
import time
from dataclasses import dataclass

import anthropic as _anthropic
import openai as _openai

log = logging.getLogger(__name__)
```

Then, directly after the existing `_get_client` function (currently lines 164-175):

```python
_client: _anthropic.Anthropic | None = None


def _get_client() -> _anthropic.Anthropic:
    global _client
    if _client is None:
        from system.config import settings
        api_key = settings.credentials.anthropic_api_key
        if not api_key:
            raise RuntimeError("Missing required env var: ANTHROPIC_API_KEY")
        _client = _anthropic.Anthropic(api_key=api_key)
    return _client
```

add:

```python
_openai_client: _openai.OpenAI | None = None


def _get_openai_client() -> _openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        from system.config import settings
        api_key = settings.credentials.openai_api_key
        if not api_key:
            raise RuntimeError("Missing required env var: OPENAI_API_KEY")
        _openai_client = _openai.OpenAI(api_key=api_key)
    return _openai_client
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v -k test_get_openai_client_raises_without_key`
Expected: PASS

- [ ] **Step 5: Run the full file to confirm no regressions, then commit**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v`
Expected: PASS (all tests — this task only adds code, nothing existing calls it yet)

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/ai_analyst.py" "trading bot/tests/test_ai_analyst.py" && git commit -m "$(cat <<'EOF'
feat: add OpenAI client getter to ai_analyst

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 3: `_llm_call` — rename `_claude_call`, add OpenAI branch

**Files:**
- Modify: `bot/ai_analyst.py:371-385` (`_claude_call`)
- Test: `tests/test_ai_analyst.py`

This task only renames the function and adds the branch. The five call sites still say `_claude_call` until Task 5 — to keep this task small, add a thin backward-compatible alias so nothing breaks mid-task.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ai_analyst.py`:

```python
def _make_openai_resp(text: str):
    """Build a mock that mimics an OpenAI ChatCompletion with .choices[0].message.content."""
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


def test_llm_call_uses_openai_when_provider_is_openai(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="openai"))

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_openai_resp("openai response text")
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    result = _llm_call("system prompt", "user prompt", max_tokens=256)

    assert result == "openai response text"
    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert call_kwargs["model"] == "gpt-5.4"
    assert call_kwargs["temperature"] == 0
    assert call_kwargs["seed"] == 0
    assert call_kwargs["max_tokens"] == 256
    assert call_kwargs["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]


def test_llm_call_uses_anthropic_when_provider_is_anthropic(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="anthropic"))
    _mock_claude(mocker, "anthropic response text")

    from bot.ai_analyst import _llm_call
    result = _llm_call("system prompt", "user prompt", max_tokens=256)

    assert result == "anthropic response text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v -k test_llm_call`
Expected: FAIL with `ImportError: cannot import name '_llm_call' from 'bot.ai_analyst'`

- [ ] **Step 3: Implement**

In `bot/ai_analyst.py`, replace (currently lines 371-385):

```python
def _claude_call(system_text: str, user_text: str, max_tokens: int = 512) -> str:
    """Single Claude API call with prompt caching on the system prompt."""
    client = _get_client()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=0,
        system=[{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},  # prompt caching
        }],
        messages=[{"role": "user", "content": user_text}],
    )
    return msg.content[0].text
```

with:

```python
def _llm_call(system_text: str, user_text: str, max_tokens: int = 512) -> str:
    """Single LLM call, routed to the configured provider.

    OpenAI's prompt caching is automatic on repeated prefixes >=1024 tokens —
    no cache_control equivalent needed on that path.
    """
    from system.config import settings
    if settings.llm_provider == "openai":
        client = _get_openai_client()
        resp = client.chat.completions.create(
            model="gpt-5.4",
            max_tokens=max_tokens,
            temperature=0,
            seed=0,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
        )
        return resp.choices[0].message.content

    client = _get_client()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=0,
        system=[{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},  # prompt caching
        }],
        messages=[{"role": "user", "content": user_text}],
    )
    return msg.content[0].text


def _claude_call(system_text: str, user_text: str, max_tokens: int = 512) -> str:
    """Deprecated alias — removed in Task 5 once all call sites use _llm_call directly."""
    return _llm_call(system_text, user_text, max_tokens)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v -k test_llm_call`
Expected: PASS

- [ ] **Step 5: Run the full file to confirm no regressions, then commit**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v`
Expected: PASS (all tests — `_claude_call` still exists as an alias, default provider is still `"anthropic"`, so every existing test's Anthropic-path assertions are unaffected)

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/ai_analyst.py" "trading bot/tests/test_ai_analyst.py" && git commit -m "$(cat <<'EOF'
feat: add _llm_call with provider branch (openai/anthropic)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 4: Retry wrapper catches `openai.RateLimitError`

**Files:**
- Modify: `bot/ai_analyst.py:178-200` (`_call_with_retry`)
- Test: `tests/test_ai_analyst.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ai_analyst.py`:

```python
def test_call_with_retry_retries_on_openai_rate_limit(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="openai"))
    mocker.patch("bot.ai_analyst.time.sleep")

    rate_err = _openai.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={}),
        body={},
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        rate_err, rate_err, _make_openai_resp("worked on third try"),
    ]
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call, _call_with_retry
    result = _call_with_retry(lambda: _llm_call("sys", "user"))

    assert result == "worked on third try"
    assert mock_client.chat.completions.create.call_count == 3
```

Add the import at the top of `tests/test_ai_analyst.py` (alongside the existing `import json` / `import pytest` block):

```python
import openai as _openai
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v -k test_call_with_retry_retries_on_openai_rate_limit`
Expected: FAIL — `openai.RateLimitError` is raised and propagates uncaught (only `_anthropic.RateLimitError` is currently retried), so the test fails on the unhandled exception rather than reaching the assertions.

- [ ] **Step 3: Implement**

In `bot/ai_analyst.py`, change `_call_with_retry` (currently lines 178-200) from:

```python
def _call_with_retry(fn):
    """Retry fn() up to _MAX_RETRIES times on RateLimitError or JSON parse failure."""
    last_exc = None
    delay = _RATE_LIMIT_SLEEP_INITIAL
    for attempt in range(_MAX_RETRIES):
        try:
            result = fn()
            time.sleep(_INTER_CALL_SLEEP)  # throttle: max 2 calls/second
            return result
        except _anthropic.RateLimitError as exc:
            last_exc = exc
            log.warning("Rate limit hit (attempt %d/%d) — retrying in %.0fs",
                        attempt + 1, _MAX_RETRIES, delay)
            time.sleep(delay)
            delay *= 2
        except ValueError as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                log.warning("Parse error (attempt %d/%d): %s — retrying",
                            attempt + 1, _MAX_RETRIES, exc)
            else:
                raise
    raise last_exc
```

to (only the `except` line for rate limits changes):

```python
def _call_with_retry(fn):
    """Retry fn() up to _MAX_RETRIES times on RateLimitError or JSON parse failure."""
    last_exc = None
    delay = _RATE_LIMIT_SLEEP_INITIAL
    for attempt in range(_MAX_RETRIES):
        try:
            result = fn()
            time.sleep(_INTER_CALL_SLEEP)  # throttle: max 2 calls/second
            return result
        except (_anthropic.RateLimitError, _openai.RateLimitError) as exc:
            last_exc = exc
            log.warning("Rate limit hit (attempt %d/%d) — retrying in %.0fs",
                        attempt + 1, _MAX_RETRIES, delay)
            time.sleep(delay)
            delay *= 2
        except ValueError as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                log.warning("Parse error (attempt %d/%d): %s — retrying",
                            attempt + 1, _MAX_RETRIES, exc)
            else:
                raise
    raise last_exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v -k test_call_with_retry_retries_on_openai_rate_limit`
Expected: PASS

- [ ] **Step 5: Run the full file to confirm no regressions, then commit**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v`
Expected: PASS

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/ai_analyst.py" "trading bot/tests/test_ai_analyst.py" && git commit -m "$(cat <<'EOF'
feat: retry on openai.RateLimitError alongside anthropic's

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 5: Switch all five call sites to `_llm_call`, remove the alias

**Files:**
- Modify: `bot/ai_analyst.py:388-397` (`_bull_argument`, `_bear_argument`), `:401-426` (`score_entry`), `:472-489` (`review_exit`), `:529-542` (`score_technical`)

No test changes — this task is a pure rename at each call site, and the existing suite (now exercising both providers per Tasks 3-4) is the regression check.

- [ ] **Step 1: Update the five call sites**

In `bot/ai_analyst.py`, change each of the following (find-and-replace `_claude_call(` → `_llm_call(` at exactly these five call sites — do not touch the function definitions added in Task 3):

```python
        return _claude_call(_BULL_SYSTEM, prompt, max_tokens=512)
```
→
```python
        return _llm_call(_BULL_SYSTEM, prompt, max_tokens=512)
```

```python
        return _claude_call(_BEAR_SYSTEM, combined, max_tokens=512)
```
→
```python
        return _llm_call(_BEAR_SYSTEM, combined, max_tokens=512)
```

```python
        return parse_entry_response(_claude_call(system_text, prompt))
```
→
```python
        return parse_entry_response(_llm_call(system_text, prompt))
```

```python
        return parse_exit_response(_claude_call(_EXIT_SYSTEM, prompt, max_tokens=256))
```
→
```python
        return parse_exit_response(_llm_call(_EXIT_SYSTEM, prompt, max_tokens=256))
```

```python
        return parse_technical_response(_claude_call(system_text, prompt), last_close=snapshot.last_close)
```
→
```python
        return parse_technical_response(_llm_call(system_text, prompt), last_close=snapshot.last_close)
```

Then delete the now-unused alias added in Task 3:

```python
def _claude_call(system_text: str, user_text: str, max_tokens: int = 512) -> str:
    """Deprecated alias — removed in Task 5 once all call sites use _llm_call directly."""
    return _llm_call(system_text, user_text, max_tokens)
```

- [ ] **Step 2: Run the full file to confirm zero regressions**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v`
Expected: PASS — every test still passes because `settings.llm_provider` still defaults to `"anthropic"` (flipped in Task 7), so every call site routes to the exact same Anthropic code path as before this task.

- [ ] **Step 3: Run the full project test suite**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest -q`
Expected: PASS, no regressions anywhere (orchestration tests that exercise `score_entry`/`review_exit`/`score_technical` indirectly should be unaffected — they all mock at the `_get_client`/`_llm_call` boundary or above).

- [ ] **Step 4: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/ai_analyst.py" && git commit -m "$(cat <<'EOF'
refactor: route all five scoring call sites through _llm_call

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 6: Force-Anthropic fixture for the existing test file

**Files:**
- Modify: `tests/test_ai_analyst.py` (top of file)

This task lands *before* the default flips (Task 7), so it's a no-op today — but it must be in place in the same commit as the flip, and reviewing it on its own first keeps Task 7 a one-line diff.

- [ ] **Step 1: Add the fixture**

At the top of `tests/test_ai_analyst.py`, directly after the existing imports (after the `from bot.ai_analyst import (...)` block, before the `_make_anthropic_resp` helper), add:

```python
import dataclasses
from system.config import settings as _real_settings


@pytest.fixture(autouse=True)
def _force_anthropic_provider(mocker):
    """This file's tests assert Anthropic-specific behavior (model strings, error
    types, cache_control). Force the provider regardless of Settings' real default
    so they keep testing what they've always tested. OpenAI-path tests in this file
    explicitly re-patch to "openai" inside their own body, which overrides this."""
    mocker.patch("system.config.settings", dataclasses.replace(_real_settings, llm_provider="anthropic"))
```

- [ ] **Step 2: Run the full file**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v`
Expected: PASS — including the OpenAI-path tests from Tasks 3-4, since each of those re-patches `system.config.settings` again inside its own body (the later `mocker.patch` call wins over the autouse fixture's earlier one within the same test).

- [ ] **Step 3: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/tests/test_ai_analyst.py" && git commit -m "$(cat <<'EOF'
test: force anthropic provider by default in test_ai_analyst.py

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 7: Flip the default to `"openai"`

**Files:**
- Modify: `system/config.py` (the `llm_provider` field added in Task 1)
- Test: `tests/test_config.py`

- [ ] **Step 1: Update the test to match the new intended default**

In `tests/test_config.py`, replace the test added in Task 1:

```python
def test_settings_llm_provider_defaults_to_anthropic_for_now():
    s = Settings()
    assert s.llm_provider == "anthropic"
```

with:

```python
def test_settings_llm_provider_defaults_to_openai():
    s = Settings()
    assert s.llm_provider == "openai"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_config.py -v -k llm_provider`
Expected: FAIL — `assert s.llm_provider == "openai"` fails because the field still defaults to `"anthropic"`.

- [ ] **Step 3: Implement**

In `system/config.py`, change:

```python
    # TODO(Task 7 of the openai-primary-provider plan): flip default to "openai".
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "anthropic"))
```

to:

```python
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "openai"))
```

- [ ] **Step 4: Run test to verify it passes, then run the full suite**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_config.py -v && pytest -q`
Expected: Both PASS. The second command is the critical check — confirms Task 6's fixture correctly insulates `test_ai_analyst.py` from this default flip, and that no other test file anywhere in the suite assumes `Settings().llm_provider` resolves to Anthropic.

- [ ] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/system/config.py" "trading bot/tests/test_config.py" && git commit -m "$(cat <<'EOF'
feat: make openai the default llm_provider

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 8: Update `trading bot/CLAUDE.md`, `.env.example`, and startup log

> **Scope note added after Task 7's code-quality review:** the reviewer flagged two real gaps that Task 7 itself was correctly out-of-scope for, but that should land before this plan is considered done: `.env.example` never listed `OPENAI_API_KEY` at all (pre-existing gap, made acute now that it's required by default), and nothing logs which provider is active at startup for a change with this much blast radius. Both are folded into this task since it already touches secrets documentation.

**Files:**
- Modify: `trading bot/CLAUDE.md` (the "Stack at a glance" section)
- Modify: `trading bot/.env.example` (add the missing key)
- Modify: `trading bot/run_bot.py` (one log line at startup)

- [ ] **Step 1: Update the AI line**

In `trading bot/CLAUDE.md`, find:

```
- **AI:** Anthropic Claude (`claude-sonnet-4-6`) for entry/exit scoring with prompt caching (`bot/ai_analyst.py`); OpenAI for news sentiment in `bot/researcher.py`.
```

Replace with:

```
- **AI:** OpenAI (`gpt-5.4`) is the default provider for entry/exit/technical scoring (`bot/ai_analyst.py`); switch back to Anthropic Claude (`claude-sonnet-4-6`, with prompt caching) via `Settings.llm_provider = "anthropic"` (env: `LLM_PROVIDER=anthropic`). OpenAI is also used separately for news sentiment in `bot/researcher.py` (`gpt-4o-mini`, unrelated to this switch).
```

- [ ] **Step 2: Update the secrets line**

Find (in the same file, under "Running"):

```
Secrets come from environment / `.env` (see `.env.example`): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `PROPUBLICA_API_KEY`, optional `ALERT_WEBHOOK_URL`, `DB_PATH`, `LOG_LEVEL`. `--simulated` mode runs without broker/LLM keys for the parts that don't call out.
```

Replace with:

```
Secrets come from environment / `.env` (see `.env.example`): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `PROPUBLICA_API_KEY`, optional `ALERT_WEBHOOK_URL`, `DB_PATH`, `LOG_LEVEL`, `LLM_PROVIDER` (`openai` default, or `anthropic`). `OPENAI_API_KEY` is required by default now (entry/exit/technical scoring); `ANTHROPIC_API_KEY` is only required if `LLM_PROVIDER=anthropic`. `--simulated` mode runs without broker/LLM keys for the parts that don't call out.
```

- [ ] **Step 3: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/CLAUDE.md" && git commit -m "$(cat <<'EOF'
docs: document llm_provider switch and new OpenAI default in CLAUDE.md

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 9: Final full-suite verification

**Files:** none — verification only.

- [ ] **Step 1: Run the complete trading bot test suite**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest -q`
Expected: PASS, full suite (no skips, no failures). Note the final count — it should be the pre-plan count (659) plus 6 new tests (2 in Task 1, 1 in Task 2, 2 in Task 3, 1 in Task 4 — Task 7 replaces one Task-1 test in place rather than adding a new one, net zero there), for 665 total.

- [ ] **Step 2: Confirm no stray references to the removed `_claude_call` name**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && grep -rn "_claude_call" . --include="*.py"`
Expected: no output (the name should not appear anywhere — not in `bot/ai_analyst.py`, not in any test file).

- [ ] **Step 3: Confirm `git status` is clean**

Run: `cd "/Users/thomasvromen/Documents/Claude code test" && git status`
Expected: working tree clean, all commits already pushed in prior tasks (this task makes no new commit).
