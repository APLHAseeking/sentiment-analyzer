# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
pip install -r requirements.txt
python app.py
# Opens at http://localhost:8080
```

The app initializes the SQLite database (`signals.db`) on first run.

## Architecture

Single-file Flask backend (`app.py`) with a vanilla JS frontend — no build step, no framework.

**Data flow:**
1. User uploads a CSV/Excel file → `POST /api/upload` saves it to disk and returns column names
2. User maps columns (headline, ticker, date, source) → `POST /api/settings` stores the mapping as JSON in the `settings` table
3. `GET /api/data` reads the file via pandas, joins signals from SQLite, and returns paginated rows
4. `POST /api/process` sends batches of row IDs to the Anthropic API and persists the buy/neutral/sell classification back to SQLite
5. `GET /api/export` streams the merged result as a CSV download

**Persistence model:**
- The uploaded file stays on disk at its original path; only the path is stored in the `settings` table
- `signals.db` holds two tables: `signals` (AI classifications keyed by `row_id`) and `settings` (key/value store for API key, model, file path, column mapping)
- `row_id` is a SHA-256 hash of `ticker|date|headline` — used to deduplicate and join across the file and DB
- The in-memory `_df_cache` dict caches the loaded DataFrame; it is invalidated when file path or column mapping changes

**Frontend (`static/app.js`):**
- No framework — plain ES2020 with Bootstrap 5 for UI components
- All state lives in the `state` object (page, filters, processing flag)
- Processing happens in chunks of 50 row IDs; the stop button sets `state.stopProcessing = true` which is checked between chunks

**AI classification (`/api/process`):**
- Uses `client.messages.create` with a structured system prompt that enforces JSON output: `{"signal": "buy"|"neutral"|"sell", "reason": "..."}`
- Retries up to 3 times on `RateLimitError` with exponential back-off (5s, 10s, 20s)
- 0.4s sleep between each row to stay within rate limits

## Key constraints

- `signals.db` is excluded from git (runtime data). The uploaded data file is also not committed.
- The Anthropic API key is stored in the `settings` table; it is never exposed to the frontend (the settings GET endpoint returns only `api_key_set: bool`).
- Date filtering is string-based (lexicographic comparison), so date columns should be in ISO format (YYYY-MM-DD) to work correctly.
