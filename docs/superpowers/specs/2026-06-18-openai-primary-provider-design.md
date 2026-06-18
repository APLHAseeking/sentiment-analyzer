# OpenAI as Primary LLM Provider — Design Spec

**Date:** 2026-06-18
**Status:** Approved

## Goal

Make OpenAI (`gpt-5.4`) the default LLM provider for entry/exit/technical scoring in `bot/ai_analyst.py`, while keeping Anthropic Claude available via a config switch. This is a provider swap, not a quality or prompt rewrite — same schemas, same call sites, same retry/parsing logic. Only the underlying API call branches by provider.

## Design Principle

Single seam. Every existing scoring function (`score_entry`, `score_entry_with_debate`'s bull/bear/rescore, `review_exit`, `score_technical`) already funnels through one low-level call function (`_claude_call`, `bot/ai_analyst.py:371`). Only that function and its client-getter/retry plumbing change. Prompts, schemas (`EntryScore`, `ExitDecision`, `TechnicalScore`), and decision logic (e.g. `parse_technical_response`'s reward:risk sanity check) are untouched.

---

## Architecture

```
score_entry / score_entry_with_debate / review_exit / score_technical (unchanged)
  -> _call_with_retry(fn)  [catches anthropic.RateLimitError OR openai.RateLimitError]
    -> _llm_call(system_text, user_text, max_tokens)  [renamed from _claude_call]
      -> settings.credentials.llm_provider == "openai" (default): OpenAI path
      -> settings.credentials.llm_provider == "anthropic": today's exact code path
```

## Component 1: Config (`system/config.py`)

`Credentials` gains `llm_provider: str = "openai"` (co-located with `anthropic_api_key`/`openai_api_key`, since it governs which of the two is active — no new dataclass, matching the existing single-flag-on-existing-dataclass precedent set by `SizingConfig.enable_technical_gate`). `Settings.validate()` rejects any value outside `{"anthropic", "openai"}`.

## Component 2: OpenAI client getter (`bot/ai_analyst.py`)

New `_get_openai_client()` — same lazy-singleton shape as the existing `_get_client()` and as `bot/researcher.py`'s `_get_sentiment_client()`. Reads `settings.credentials.openai_api_key`; raises `RuntimeError("Missing required env var: OPENAI_API_KEY")` if empty. No new secret — `OPENAI_API_KEY` is already required for `bot/researcher.py`'s sentiment classifier, and `openai>=1.30.0` is already in `requirements.txt`.

## Component 3: Provider-dispatching call (`bot/ai_analyst.py`)

`_claude_call` renamed to `_llm_call(system_text: str, user_text: str, max_tokens: int = 512) -> str`. Branches on `settings.credentials.llm_provider`:

- **`"openai"` (default):** `client.chat.completions.create(model="gpt-5.4", max_tokens=max_tokens, temperature=0, seed=0, messages=[{"role": "system", "content": system_text}, {"role": "user", "content": user_text}])`, return `resp.choices[0].message.content`. Mirrors `bot/researcher.py`'s `_score_sentiment` exactly (same `temperature=0` + `seed=0` determinism convention). No `cache_control` block — OpenAI's prompt caching is automatic on prefixes ≥1024 tokens, no code-level opt-in.
- **`"anthropic"`:** today's exact code — `cache_control: {"type": "ephemeral"}` on the system block, `model="claude-sonnet-4-6"`, `temperature=0`.

All five callers of `_claude_call` are updated to call `_llm_call` — no other change to their bodies.

## Component 4: Retry wrapper (`bot/ai_analyst.py`)

`_call_with_retry` currently catches only `_anthropic.RateLimitError`. Add `openai.RateLimitError` to the same except clause (tuple of exception types) so retry-with-backoff works for both providers. The `ValueError` (JSON parse failure) branch is provider-agnostic already and needs no change.

## Component 5: Tests

Changing the default to `"openai"` would silently break every existing test in `tests/test_ai_analyst.py` that patches `bot.ai_analyst._get_client` (the Anthropic getter) without setting a provider — there are 30+. Fix:

- A fixture (file-scoped, `autouse` within `test_ai_analyst.py`) that forces `settings.credentials.llm_provider == "anthropic"` for that file, preserving every existing test's original intent unchanged.
- New tests covering the OpenAI path: `_get_openai_client` raises on missing key (mirrors the existing `_get_client` missing-key test); a happy-path call returns the expected text; `openai.RateLimitError` triggers the same backoff-and-retry behavior as the Anthropic case.
- One test in `tests/test_config.py` asserting the real (unfixtured) default is `Credentials().llm_provider == "openai"`, and one asserting `Settings.validate()` rejects an invalid provider string.

## Component 6: Docs (`trading bot/CLAUDE.md`)

The "Stack at a glance" line currently reads: *"AI: Anthropic Claude (`claude-sonnet-4-6`) for entry/exit scoring with prompt caching (`bot/ai_analyst.py`); OpenAI for news sentiment in `bot/researcher.py`."* Updated to state OpenAI (`gpt-5.4`) is the default for entry/exit/technical scoring, `llm_provider` is the switch back to Claude, and OpenAI's sentiment-classifier usage in `researcher.py` is unchanged and separate.

---

## What Is NOT Changed / Out of Scope

| Item | Status |
|---|---|
| `bot/researcher.py` sentiment classifier | Unchanged — already OpenAI (`gpt-4o-mini`), separate call site, not part of this swap |
| `EntryScore`, `ExitDecision`, `TechnicalScore` dataclasses | Unchanged |
| `parse_entry_response`, `parse_exit_response`, `parse_technical_response` | Unchanged — both providers return the same JSON-text shape these already parse |
| All system prompts (`_ENTRY_SCHEMA`, `_BULL_SYSTEM`, `_BEAR_SYSTEM`, `_EXIT_SYSTEM`, `_TECHNICAL_SCHEMA`, etc.) | Unchanged |
| Bull/bear debate logic, conviction thresholds, reward:risk sanity checks | Unchanged |
| `requirements.txt` | Unchanged — `openai>=1.30.0` already present |
| New secrets | None — `OPENAI_API_KEY` already required today |

## Modified Files

| File | Change |
|---|---|
| `trading bot/system/config.py` | `Credentials.llm_provider: str = "openai"`; validation in `Settings.validate()` |
| `trading bot/bot/ai_analyst.py` | `_claude_call` → `_llm_call` with provider branch; new `_get_openai_client`; `_call_with_retry` catches `openai.RateLimitError` too; five call sites updated |
| `trading bot/tests/test_config.py` | Default-provider test, invalid-provider validation test |
| `trading bot/tests/test_ai_analyst.py` | Anthropic-forcing fixture for existing tests; new OpenAI-path tests |
| `trading bot/CLAUDE.md` | Stack line updated to reflect OpenAI as default scoring provider |
