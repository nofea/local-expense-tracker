# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Privacy-first, fully local expense tracker. Bank statements (PDF/CSV) are parsed into transactions by a local LLM served by **Ollama**, stored in SQLite, and viewed through either a FastAPI + HTMX web UI or a Textual terminal UI. `app_spec.md` (v2) is the target spec, and the code does not implement most of it yet. It specifies accounts/statements tables, integer `amount_minor` money, a validator plus review-before-commit step, background ingestion jobs, a new dedup id, and rule priority with manual overrides. Its Appendix A is the prioritized gap checklist, and Appendix B is the v1→v2 DB migration. This file describes the code **as it currently is**; when the two disagree, the spec is the intended direction.

## Commands

All commands run from the repo root — modules use absolute imports (`from core...`), so there is no package install step.

```bash
pip install -r requirements.txt     # Python 3.11+

python run_web.py                   # Web UI at http://127.0.0.1:8000 (uvicorn, reload=True)
python -m tui.app                   # Terminal UI (keys: d dashboard, t transactions, q quit)

# Tests (stdlib unittest; pytest is not in requirements)
python -m unittest discover tests     # tests set EXPENSE_TRACKER_DB to a temp file; never the real DB
python -m unittest tests.test_ollama_status.TestWebOllamaStatusRendering.test_api_status_endpoint_online
```

Real ingestion needs `ollama serve` running on `localhost:11434` with a model pulled (default `llama3.1`). Importing `web.app` calls `db.init_db()`, so every test module must set `EXPENSE_TRACKER_DB` **before** importing `core` or `web`. Copy the guard block at the top of the existing test files, or a test run will migrate your real DB. A few Ollama-status tests still call a real local Ollama.

No linter or formatter is configured.

## Architecture

Three layers: `core/` holds all logic and data access; `web/` and `tui/` are thin front-ends that call `core` directly. There is no service layer or API between them, and both UIs share the same SQLite file.

**Ingestion pipeline** (`core/ingestion.py::process_file`), triggered by `POST /upload`:
1. Extract text: `pdfplumber` page by page for PDFs. CSVs are re-rendered as a `|`-joined header plus body (still sent to the LLM, not parsed directly). The parser is chosen by the extension of `source_name` (the user's sanitized filename), not by the temp file path.
2. `_chunk_pages` packs whole pages into chunks of up to `CHUNK_SIZE=6000` chars. A page that is too large is split on line boundaries, and the CSV header is repeated in every chunk. Chunks **must not overlap**, because overlap re-emits rows that the planned per-statement dedup (spec §5) can't tell apart from genuine repeats.
3. `core/ollama_client.extract_transactions` calls `ollama.chat` with `format=TransactionExtractionResult.model_json_schema()` and `temperature=0`. If strict validation fails, it falls back to a lenient JSON parse. A failing chunk is recorded in `IngestionSummary.errors` and does not abort the other chunks.
4. `database.import_statement` runs in one transaction. It creates a `committed` statement under the `Default` account (the review step doesn't exist yet), inserts the rows with `INSERT OR IGNORE`, and runs the rules engine. The id is `models.compute_transaction_id`: `sha256(account_id|date|normalized desc|amount_minor|ordinal)`, where the ordinal counts identical rows within one statement (spec §5). This keeps genuine same-day repeats while still deduping overlapping statements. Pass the **whole statement** in one call, because ordinals restart on every call. Re-uploading the same bytes is rejected up front by `file_sha256`. Changing the id formula breaks dedup against existing data, so only change it through a migration that recomputes every id.
5. Zero-amount rows are counted as `invalid` and skipped, because v2 has `CHECK (amount_minor <> 0)`.

The route saves the upload to a `tempfile` under a generated name and runs `process_file` via `run_in_threadpool`, so the event loop isn't blocked. The request still waits for the whole LLM run, though; spec §3 replaces this with a background job. The temp file is deleted in `finally`. The client filename is never used as a path (`_sanitize_filename`). Only `.pdf` and `.csv` are accepted, up to 20 MB.

**Keep in sync:**
- The LLM output schema is defined twice: as Pydantic models in `core/models.py` (`ExtractedTransaction` / `TransactionExtractionResult`) and as hand-written JSON in `SYSTEM_PROMPT` in `core/ollama_client.py`.
- The default model is `DEFAULT_MODEL` in `ollama_client.py`, and it is hardcoded again as the `Form("llama3.1")` default in `web/app.py`.

**Database** (`core/database.py`, `core/migrations.py`): there is no ORM. The schema is spec §2 (v2), versioned with `PRAGMA user_version`. `init_db()` calls `migrations.migrate()` on every startup. It creates v2 on an empty file, or migrates a v0/v1 file per spec Appendix B (backup to `expenses.db.v1.bak`, one transaction that rolls back on failure). DDL runs statement by statement inside an explicit `BEGIN` (never `executescript`, which commits). All connections, including analytics, go through `database.connect()` (WAL, foreign keys, `busy_timeout=5000`), and every function accepts a `db_path` override. The default is `data/expenses.db`, or the `EXPENSE_TRACKER_DB` env var when set. Money is integer `amount_minor` (paise), and `models.to_minor` / `from_minor` are the only conversions. `TransactionRecord.amount` is a display-only property, so never sum it. Queries join `statements` to expose `source_file`. Dates are `YYYY-MM-DD` text, and month filtering uses `LIKE 'YYYY-MM-%'`. Amounts are negative for money out, and analytics excludes `is_transfer = 1` rows.

**Category rules** (spec §6, `database._run_rules_engine`): rules match on a Python `casefold()` substring of `raw_description`, never SQL `LIKE`, so `%` and `_` are literal. A rule can be scoped by `account_id`. For each non-`manual` row, the highest `priority` wins, with ties going to the lowest id. The engine sets `category`, `cleaned_merchant`, `rule_id` and `category_source='rule'`, and sets `is_transfer=1` for `mark_transfer` rules but never clears it. When a row's rule no longer matches, the row reverts to `Uncategorized`/`default`. `categorizer.add_rule` and `remove_rule` re-run the engine. `count_rule_matches` uses the same matching for previews.

**Web UI** (`web/app.py`): server-rendered Jinja2 templates with HTMX. POST/DELETE routes return fragments from `templates/partials/` rather than full pages, so new interactive features should follow that pattern. A Jinja `context_processors` hook injects `ollama_status` into every template (it drives the status badge in `base.html`). `is_ollama_available()` caches its result for 3 seconds, and `/api/ollama-status` bypasses the cache with `force=True`. Charts are hand-rolled SVG in `web/static/charts.js` with no JS dependencies. Static assets are local only (HTMX is vendored at `web/static/htmx.min.js`, with no remote fonts or CDNs); `tests/test_appendix_a_fixes.py` enforces this. Dashboard and transactions query params are clamped, and an invalid month falls back to the current one.

**TUI** (`tui/`): a Textual app with `DashboardScreen` and `TransactionsScreen`, which call `core.analytics` / `core.database` directly. Styling lives in `tui/style.tcss`.

`data/` is gitignored and created automatically. `uploads/` is no longer written to (statements aren't retained); any files left there from earlier versions can be deleted.
