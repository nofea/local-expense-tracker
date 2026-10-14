# Technical Specification: Privacy-First Local Expense Tracker

**Version 2.** This version replaces the original draft. It covers accounts and statements, integer money, statement-aware dedup, a defined validator with a review step, and categorization semantics. Appendix A lists the gaps between this spec and the current code.

---

## 0. Principles

1. **Local only.** No network traffic leaves the machine except calls to the configured Ollama host, which defaults to `localhost`. No CDNs, no telemetry, no update checks.
2. **Deterministic where possible.** The LLM is used only for unstructured text (PDFs and CSVs with no mapping). Everything after extraction is deterministic: validation, dedup, categorization, and analytics.
3. **Nothing enters the ledger unreviewed.** Extracted rows are staged and the user commits them explicitly.
4. **Imports are reversible.** Every transaction belongs to a statement, and deleting a statement removes its rows.

---

## 1. Architecture & Tech Stack

* **Language:** Python 3.11+ for everything.
* **Database:** one SQLite file (default `data/expenses.db`), shared by both UIs.
* **LLM:** Ollama (`/api/chat` with a JSON-schema `format`). Other backends are out of scope.
* **Web UI:** FastAPI + Jinja2 + HTMX. All assets are vendored under `web/static/`.
* **TUI:** Textual (Python). This is the canonical TUI; the earlier C++/FTXUI option is dropped.

```
            ┌──────────── CSV with known mapping ───────────┐
[Upload] ─► [Extractor] ─┤                                  ├─► [Validator] ─► [Staging + Review] ─► commit ─► [Ledger] ─► [Rules engine]
            └─ PDF / unmapped CSV ─► [Chunker] ─► [Ollama] ─┘                                                       │
                                                                                                     [Web UI]  [TUI]  ◄┘
```

`core/` holds all logic and data access. `web/` and `tui/` are thin front-ends that call `core` directly.

### 1.1 Configuration

There is a single `config.toml` at the repo root. Every key is optional, and the values below are the defaults. No other module may hardcode these values.

* `config.toml` is git-ignored. `config.example.toml` documents every key, and `EXPENSE_TRACKER_CONFIG` can point at another file.
* Unknown keys, wrong types and out-of-range values stop startup with an error that names the key.
* `EXPENSE_TRACKER_DB` overrides `storage.db_path`.
* `minor_units` must not change once data exists, because stored amounts and transaction ids depend on it.

```toml
[llm]
host = "http://localhost:11434"
model = "llama3.1"
chunk_timeout_s = 120
max_retries = 1

[money]
currency_code = "INR"
currency_symbol = "₹"
minor_units = 2            # paise per rupee = 10^2

[storage]
db_path = "data/expenses.db"

[web]
host = "127.0.0.1"         # a non-loopback value prints a warning at startup
port = 8000
```

---

## 2. Data Model

* All money is stored as **`INTEGER` minor units** (`amount_minor`, in paise). Conversion to and from decimals happens only at the I/O edges (parsing and display).
* **Sign convention:** amounts are signed from the user's point of view. Negative means money out, or a card balance increasing. Positive means money in, or a card balance decreasing. A credit-card purchase is negative. A refund on a card is positive. A payment to the card is positive **and** is a transfer.
* Dates are ISO `YYYY-MM-DD` text. **Month bucketing always uses `transaction_date`.** `value_date` is informational only.
* The schema version is tracked in `PRAGMA user_version`. Migrations are numbered Python functions in `core/migrations.py` that run on startup.
* Every connection is opened through a single helper that sets `journal_mode=WAL`, `foreign_keys=ON` and `busy_timeout=5000`.

```sql
CREATE TABLE accounts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,              -- e.g. "HDFC Savings", "Amex Gold"
    kind          TEXT NOT NULL CHECK (kind IN ('bank', 'credit_card')),
    institution   TEXT
);

CREATE TABLE statements (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    file_name       TEXT NOT NULL,                   -- sanitized original name, display only
    file_sha256     TEXT NOT NULL,                   -- hash of uploaded bytes; detects re-uploads
    period_start    TEXT,                            -- YYYY-MM-DD, nullable
    period_end      TEXT,
    opening_minor   INTEGER,                         -- nullable: not all statements show balances
    closing_minor   INTEGER,
    extractor       TEXT NOT NULL CHECK (extractor IN ('csv_mapping', 'llm')),
    model           TEXT,                            -- Ollama model used, if llm
    status          TEXT NOT NULL CHECK (status IN
                      ('queued', 'needs_password', 'extracting', 'review',
                       'committed', 'failed', 'discarded')),
    progress_done   INTEGER NOT NULL DEFAULT 0,      -- chunks processed
    progress_total  INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    reconciled      INTEGER,                         -- 1 ok, 0 mismatch, NULL not checkable
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    committed_at    TEXT,
    UNIQUE (account_id, file_sha256)
);

-- Rows awaiting review. Never read by analytics.
CREATE TABLE staged_rows (
    id               INTEGER PRIMARY KEY,
    statement_id     INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,               -- order within the statement
    transaction_date TEXT,                           -- raw LLM/CSV output; may be invalid
    value_date       TEXT,
    raw_description  TEXT,
    amount_minor     INTEGER,
    is_transfer      INTEGER NOT NULL DEFAULT 0,
    dropped          INTEGER NOT NULL DEFAULT 0,     -- user removed it in review
    issues           TEXT NOT NULL DEFAULT '[]'      -- JSON list of validator codes (§4)
);

CREATE TABLE transactions (
    id               TEXT PRIMARY KEY,               -- see §5
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    statement_id     INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    transaction_date TEXT NOT NULL,
    value_date       TEXT,
    raw_description  TEXT NOT NULL,                  -- as extracted, whitespace-normalized
    cleaned_merchant TEXT,                           -- display name; set by rule or user; never matched on
    amount_minor     INTEGER NOT NULL CHECK (amount_minor <> 0),
    is_transfer      INTEGER NOT NULL DEFAULT 0,     -- excluded from income/expense
    category         TEXT NOT NULL DEFAULT 'Uncategorized',
    category_source  TEXT NOT NULL DEFAULT 'default'
                       CHECK (category_source IN ('default', 'rule', 'manual')),
    rule_id          INTEGER REFERENCES category_rules(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_tx_date     ON transactions(transaction_date);
CREATE INDEX idx_tx_category ON transactions(category);
CREATE INDEX idx_tx_account  ON transactions(account_id, transaction_date);

CREATE TABLE category_rules (
    id               INTEGER PRIMARY KEY,
    pattern          TEXT NOT NULL UNIQUE,           -- case-insensitive substring of raw_description
    category         TEXT NOT NULL,
    cleaned_merchant TEXT,
    mark_transfer    INTEGER NOT NULL DEFAULT 0,     -- e.g. "CRED CLUB", "AUTOPAY"
    priority         INTEGER NOT NULL DEFAULT 0,     -- higher wins
    account_id       INTEGER REFERENCES accounts(id) -- NULL = all accounts
);

CREATE TABLE csv_mappings (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    header_signature  TEXT NOT NULL,                 -- sha256 of normalized header row
    skip_rows         INTEGER NOT NULL DEFAULT 0,    -- preamble lines before header
    date_col          TEXT NOT NULL,
    date_format       TEXT NOT NULL,                 -- strptime format, e.g. "%d/%m/%Y"
    value_date_col    TEXT,
    description_col   TEXT NOT NULL,
    amount_col        TEXT,                          -- single signed column, OR
    debit_col         TEXT,                          -- separate debit/credit columns
    credit_col        TEXT,
    negate            INTEGER NOT NULL DEFAULT 0,    -- flip sign (e.g. card CSVs listing charges as positive)
    UNIQUE (account_id, header_signature),
    CHECK ((amount_col IS NOT NULL) OR (debit_col IS NOT NULL AND credit_col IS NOT NULL))
);
```

Categories are free text. `Uncategorized` is reserved. Transfers are not a category; they are flagged with `is_transfer`.

---

## 3. Ingestion Pipeline

Ingestion runs as a **background job**: one job at a time, in a worker thread, so it never runs on the event loop. The `statements` row is the job record. On startup, any job left in `queued` or `extracting` is set to `failed` with the error "interrupted".

### 3.1 Upload
1. The user picks an existing account or creates one, chooses the file, and optionally enters a PDF password.
2. The server writes the upload to a `tempfile` with a generated name, in a private `tmp/` directory next to the database (mode 0700). Startup wipes that directory, so a crash never leaves a statement behind. The client-supplied filename is used only as display text, sanitized with `Path(name).name`, and stripped of control characters.
3. The server computes `file_sha256`. If a statement with the same `(account_id, file_sha256)` already exists in `committed` or `review`, or is still in progress, it rejects the upload with a link to that statement. A previous `failed` or `discarded` attempt is replaced.
4. The server inserts the `statements` row with `status='queued'` and returns a progress fragment. That fragment polls `GET /imports/{id}/status` until the job reaches `review`, `failed` or `needs_password`.

### 3.2 Extraction
* **CSV:** compute the header signature. If a `csv_mapping` exists for the account, parse deterministically, with no LLM call. If none exists, show a mapping form built from the detected header (column dropdowns, date format, negate option). The user can either save a mapping and parse, or choose "extract with LLM instead".
* **PDF:** open with `pdfplumber` using the password, if one was given.
  * If the PDF is encrypted and no password (or a wrong one) was supplied, set `needs_password` and prompt again. The password stays in job memory only and is never logged or stored.
  * If no text can be extracted, the job fails with "no text layer (scanned PDF?)". OCR is out of scope.
* **Delete the temp file** as soon as text extraction finishes, whether it succeeded or failed. Original statements are never retained.

### 3.3 LLM extraction (PDFs and unmapped CSVs)
* **Chunking:** each chunk holds one or more **whole pages** (or whole lines for CSV text), up to `CHUNK_SIZE = 6000` characters. A page longer than the limit is split on line boundaries. Chunks **do not overlap**, because overlap would create duplicate rows that per-statement dedup (§5) cannot tell apart from genuine repeats.
* **Calls:** one `ollama.chat` call per chunk, with `format = TransactionExtractionResult.model_json_schema()`, `temperature = 0`, and `chunk_timeout_s`. A chunk whose output fails schema validation is retried up to `max_retries` times. A timeout is not retried, since that would multiply the wait; the chunk fails at once. A chunk that still fails is recorded in `statements.error` but does not abort the job. The review screen shows it as a "chunk N failed" banner.
* **Statement metadata:** the first and last chunks are also asked for `period_start`, `period_end`, `opening_balance` and `closing_balance` (all nullable), through a separate small schema. The user can edit these in review.
* **Prompt:** the system prompt describes the task and the sign convention in §2, including the credit-card rules, plus one short example. It does **not** restate the JSON schema, which is enforced by `format` and defined only once, in Pydantic.
* **Progress:** `progress_done` and `progress_total` are updated after each chunk.

LLM output schema (Pydantic is the only source of truth):

```python
class ExtractedTransaction(BaseModel):
    transaction_date: str               # validated in §4, not here, so bad rows reach review
    value_date: str | None = None
    raw_description: str
    amount: Decimal                     # major units; converted to amount_minor on staging

class TransactionExtractionResult(BaseModel):
    transactions: list[ExtractedTransaction]
```

### 3.4 Staging
Rows are written to `staged_rows` in document order (`position`). Then the validator (§4) runs and the job moves to `status='review'`.

### 3.5 Review & commit
The review screen lists every staged row with its issues highlighted. The user can:
* edit the date, description or amount, toggle the transfer flag, or drop a row;
* edit the statement metadata (period and balances);
* see the reconciliation result, which is recomputed live;
* **commit** (allowed only when no non-dropped row has an `error`-level issue; a reconciliation mismatch needs an explicit "commit anyway" confirmation), or **discard**.

On commit, in a single DB transaction:
1. Compute ids (§5) and run `INSERT OR IGNORE` into `transactions`. Report the number inserted and the number already present.
2. Delete the `staged_rows`, then set `status='committed'` and `committed_at`.
3. Run the rules engine (§6) on the new rows.

---

## 4. Deterministic Validator

The validator runs on staged rows, and again after every edit in review.

| Code | Level | Rule |
|---|---|---|
| `bad_date` | error | `transaction_date` does not parse as a real ISO `YYYY-MM-DD` date. If `value_date` is present but invalid, it is set to NULL and the row gets a warning. |
| `zero_amount` | error | `amount_minor == 0` |
| `missing_desc` | error | `raw_description` is empty after whitespace normalization |
| `not_in_source` | warning | The normalized `raw_description` (case-folded, whitespace collapsed) is not a substring of the normalized extracted text. This flags likely hallucinations. |
| `duplicate_in_ledger` | info | The row's id (§5) already exists in `transactions`, so it will be skipped on commit. |
| `reconcile_mismatch` | statement | `opening + Σ(non-dropped amount_minor) ≠ closing`, checked only when both balances are known. The difference is shown. The stored value is `reconciled = 0/1/NULL`. |

Not validated (by decision): dates outside the statement period.

The source text is not stored (§9.3), so `not_in_source` is computed once, when rows are staged:
* On later revalidation, a row keeps the flag unless the user edits its description. Editing the description clears it, because the user has then vouched for the text.
* `bad_value_date` stays on the row in the same way after its value date has been cleared.
* If no source text was extracted at all, the check is skipped.

---

## 5. Identity & Deduplication

```
norm(desc) = whitespace-collapsed, trimmed raw_description (case preserved)
ordinal    = 0-based index of the row among rows in the SAME statement with
             identical (transaction_date, norm(desc), amount_minor), in document order
id         = sha256(f"{account_id}|{transaction_date}|{norm(desc)}|{amount_minor}|{ordinal}")
```

* **Same file uploaded again:** blocked up front by `file_sha256` (§3.1).
* **Overlapping statements** (e.g. a 1–31 Mar statement followed by a 15 Mar–14 Apr one): every day in the overlap appears in full in both files, so identical rows get the same ordinals and therefore the same ids, and `INSERT OR IGNORE` skips them.
* **Genuine repeats** (two ₹150 coffees on the same day): they get ordinals 0 and 1, so both are kept.
* **Known limitation:** if two statements describe the same transaction with different text, it will be imported twice. The `duplicate_in_ledger` check will not catch it. Users can remove such duplicates from the transaction list (§7). Statement-boundary days that are cut off mid-day show the same symptom.
* The id formula is part of the data contract. Changing it requires a migration that recomputes every id.

---

## 6. Categorization

* A rule matches a transaction when its `pattern` is a case-insensitive substring of `raw_description`, and either `rule.account_id` is NULL or equals the transaction's account.
* **Rules engine** (it runs after each commit and after any rule is created, edited or deleted). For every transaction with `category_source != 'manual'`:
  * Take the matching rule with the highest `priority`; ties go to the lower `id`.
  * If one exists, set `category`, `rule_id` and `category_source='rule'`. Set `cleaned_merchant` to the rule's value, which is NULL when the rule has none; on non-manual rows the merchant name only ever comes from a rule. Set `is_transfer=1` if `mark_transfer` is set.
  * If no rule matches and `category_source='rule'`, revert to `Uncategorized`, set `category_source='default'`, and set `rule_id` and `cleaned_merchant` to NULL. Deleting a rule therefore undoes its effect.
  * The engine never clears `is_transfer`, because the flag can also be set by the user in review. Removing a `mark_transfer` rule leaves the flags it set in place.
* **Manual edits** (category, merchant or transfer flag on a single transaction) set `category_source='manual'`, and rules never override them. "Reset to rules" sets the row back to `default` and re-runs the engine. Reset also clears the merchant name and the transfer flag, because the database doesn't record whether the flag came from review or from the manual edit; a `mark_transfer` rule sets it again.
* From a manual edit, the UI offers "create rule from this", which prefills `pattern` with the description.
* The rule editor shows a live match count for the pattern before saving.

---

## 7. User Interfaces

### 7.1 Web UI (`127.0.0.1:8000`)
POST, PATCH and DELETE routes return HTMX fragments from `templates/partials/`. Query parameters are validated: an out-of-range month or year returns a 400 or falls back to the current month, never a 500. Amounts are formatted using `[money]` in `config.toml`.

| Page | Features |
|---|---|
| **Upload** | Account picker/creator, file drop zone, optional PDF password, model picker (default from config), job progress, CSV mapping form, Ollama status |
| **Review** (`/imports/{id}`) | §3.5 |
| **Imports** | List of statements with account, period, row count, reconciliation and status. Actions: open review, discard, **delete committed import** (cascades its transactions, after confirmation). |
| **Dashboard** | Month navigation. Income, expense and net cards (excluding transfers). Category breakdown with % of expense (SVG + table). 6-month trend (calendar months). Top merchants. **Uncategorized count for the month, linking to the filtered list.** Filter by account. |
| **Transactions** | Filters: month, account, category, search, uncategorized only, transfers shown/hidden. Pagination. Inline edit of category, merchant and transfer flag. Delete a single row. |
| **Rules** | CRUD with priority, transfer flag, optional account scope, live match count, "re-apply all" |
| **Accounts** | CRUD. Deleting an account is allowed only when it has no statements. |

### 7.2 TUI (Textual)
Reads and writes through `core`. Upload and review stay web-only.

| Key | Action |
|---|---|
| `d` / `t` / `q` | Dashboard / Transactions / Quit |
| `←` / `→` | Previous / next month (both screens) |
| `PgUp` / `PgDn` | Previous / next page (Transactions) |
| `/` | Search (Transactions) |
| `a` | Cycle the account filter |
| `c` | Set the category on the selected row (marks it manual) |
| `x` | Toggle transfer on the selected row |
| `r` | Refresh |

The dashboard shows the same figures as the web dashboard for the selected month.

---

## 8. Analytics (definitions)

All figures are scoped to one month by `transaction_date LIKE 'YYYY-MM-%'`, optionally to one account, and **always exclude `is_transfer = 1`**.

* **Income** = Σ `amount_minor` where > 0. **Expense** = |Σ `amount_minor` where < 0|. **Net** = income − expense.
* **Category breakdown:** expense per category, and each category's % of expense. Percentages sum to 100 ± rounding.
* **Trend:** the last N calendar months, computed by month arithmetic (not by subtracting days). Months with no data show as zero.
* **Top merchants:** grouped by `COALESCE(cleaned_merchant, raw_description)`, expense only.
* **Uncategorized:** the count of non-transfer rows in the month with `category = 'Uncategorized'`.

---

## 9. Privacy & Security Requirements

1. The web server binds to `127.0.0.1` by default. A non-loopback host prints a warning at startup.
2. All JS and CSS are served locally; HTMX is vendored. No external requests from the browser.
3. Original statement files are deleted right after text extraction (§3.2). Only `file_name` and `file_sha256` are kept.
4. PDF passwords are never written to disk or logs.
5. Logs never contain transaction descriptions or amounts at INFO level or above. Neither do error messages stored in `statements.error`: an LLM output error reports only a count of validation errors, never the output.
6. Uploads are capped (default 20 MB) and only `.pdf` and `.csv` are accepted. The filename is never used as a path.
7. If `llm.host` is not loopback, the UI shows a persistent warning that statement text is leaving this machine.

DB-at-rest encryption is out of scope. Users should rely on OS disk encryption.

---

## 10. Out of Scope

OCR for scanned PDFs · multiple currencies and FX · LLM backends other than Ollama · validating dates against the statement period · DB encryption · multiple users or auth · cloud sync · budgets and alerts.

---

## 11. Roadmap & Acceptance Criteria

Each phase is done when its tests pass with `python -m unittest discover tests`. Tests use a temporary DB (`db_path` override) and a mocked Ollama, and never touch `data/expenses.db`.

**Phase 1: Schema, config, migrations, dedup**
- A fresh DB is created at the latest `user_version`. An old (v1) DB migrates correctly (Appendix B).
- Dedup: re-committing the same rows inserts 0 rows. Two identical same-day rows in one statement → 2 rows. Overlapping statements → overlap rows inserted once.
- Money: summing 0.10 × 3 gives exactly 30 minor units.

**Phase 2: Extraction, validator, staging, jobs**
- Chunks never split a line and never overlap. Every chunk is ≤ `CHUNK_SIZE`, except a single line longer than the limit.
- Each validator code in §4 has a passing and a failing test case.
- A mapped CSV is ingested with no Ollama call. A password-protected PDF moves through `needs_password` → `review`.
- The temp file is gone after extraction in both the success and failure paths.
- An interrupted job is marked `failed` on restart.

**Phase 3: Rules & analytics**
- Rule priority, manual-wins, delete-reverts and `mark_transfer` each have a test.
- Transfers are excluded from income, expense and breakdown. The trend across Jan→Mar and Dec→Jan boundaries yields consecutive, distinct months.

**Phase 4: Web UI**
- Route tests for upload → progress → review → commit → dashboard figures, delete import, inline category edit, `?month=13` → no 500.
- No `http(s)://` asset URLs in templates.

**Phase 5: TUI**
- Every binding in §7.2 has an action. A Textual `Pilot` test pages through more than 50 rows and changes a category.

---

## Appendix A: Implementation Gap Checklist (code as of `3c9b5f7`)

Ordered by priority. 🔴 = bug or security issue in the current code, 🟠 = missing spec feature, 🟡 = cleanup. ✅ = done, ◐ = partly done. Regression tests are in `tests/test_appendix_a_fixes.py` (items 1–4 and 7), `tests/test_schema_v2.py` (the schema, migration and rules items) and `tests/test_review_pipeline.py` (items 6, 9 and 10) `tests/test_config_and_llm.py` (items 20 and 22–24) `tests/test_ledger_editing.py` (items 14, 15 and 17) `tests/test_tui.py` (item 18) and `tests/test_accounts_csv_pdf.py` (items 8, 11 and 12).

1. ✅ 🔴 **Path traversal / overwrite on upload.** `web/app.py::upload_file` writes to `uploads/<client filename>` without sanitizing it. Fix: §3.1 and §9.6.
2. ✅ 🔴 **Trend skips or duplicates months.** `core/analytics.py::get_monthly_trend` subtracts `i*30` days (e.g. Mar 1 − 30d = Jan 30, so Feb is missing). Fix: calendar month arithmetic (§8).
3. ✅ 🔴 **HTMX loaded from unpkg.** `web/templates/base.html` breaks the local-only principle and fails offline. Fix: vendor it into `web/static/` (§9.2). The Google Fonts `@import` in `style.css` was removed too.
4. ✅ 🔴 **Chunks split mid-line and overlap.** `core/ingestion.py::_chunk_text` cuts rows apart, producing truncated near-duplicates. Fix: §3.3.
5. ✅ 🔴 **Genuine same-day repeats are lost.** `models.compute_transaction_id` has no ordinal and no account. Fix: §5 (requires migration).
6. ✅ 🔴 **Ingestion blocks the event loop.** `POST /upload` runs `process_file` synchronously inside `async def`. Fix: background job (§3). Done in `core/jobs.py`: one worker thread, the `statements` row as the job record, progress polling via `GET /imports/{id}/status`, and interrupted jobs marked `failed` at web startup.
7. ✅ 🔴 **Invalid query params cause a 500.** `GET /?month=13` raises `ValueError` from `datetime()`. Fix: validate (§7.1).
8. ✅ 🟠 **Schema v2.** Add `accounts`, `statements`, `staged_rows` and `csv_mappings`. On `transactions`, add `amount_minor`, `is_transfer`, `category_source`, `rule_id`, `account_id` and `statement_id`. On `category_rules`, add `priority`, `mark_transfer` and `account_id`. Add `core/migrations.py` with `user_version`. Done. Every table exists and is used, and the migration runs (Appendix B).
    * The upload form picks an existing account or creates one (§3.1). With neither, the `Default` account is used.
    * The `/accounts` page has CRUD (§7.1). Deleting an account is refused while it has statements, or rules scoped to it, and its CSV mappings are deleted with it. The page also lists the saved CSV mappings, with a delete button each.
    * The rules form sets priority, the transfer flag and an account scope, and shows a live match count (§6, §7.1).
    * The dashboard and the transactions list filter by account (§7.1). The month links and pagination keep the filter.
9. ✅ 🟠 **No validator** beyond Pydantic types. Add §4, including the `not_in_source` and reconciliation checks. Done in `core/validator.py`. Statement period and balances come from the LLM (§3.3, `ollama_client.extract_statement_metadata`) and can be edited in review.
10. ✅ 🟠 **No review step.** Rows go straight to the ledger. Add `staged_rows` and the review UI (§3.5). Done in `core/staging.py` and `/imports/{id}`. `database.import_statement` still writes straight to the ledger, and is used only by tests.
11. ✅ 🟠 **CSV always goes through the LLM.** Add `csv_mappings` and a deterministic parser (§3.2). Done in `core/csv_import.py`. On upload, a CSV is matched against the account's mappings by the signature of the header row, read `skip_rows` lines down. A match is parsed in the worker with no LLM call, so Ollama does not need to be running. With no match, the upload returns a mapping form. The file waits in `tmp/` under its generated name, which serves as the form's token. The form detects preamble lines and guesses the columns and the date format from the header names and sample rows. It shows the first rows, and the user can change the header row. The user can save the mapping and parse, choose "extract with LLM instead", or cancel, which deletes the file. A duplicate upload is rejected before the form appears. Decisions:
    * Rows that don't parse (a bad date, a blank amount) are kept and flagged by the validator in review, never dropped silently. Only blank lines are skipped.
    * The statement period comes from the first and last valid dates. Balances are left for the user to enter in review.
    * `not_in_source` is skipped, since the descriptions come straight from the file.
    * Debit/credit columns are read as absolute values: amount = credit − debit. `Dr`/`Cr` suffixes and `(1,234.50)` are understood in a single amount column.
12. ✅ 🟠 **Password-protected PDFs** are not supported (§3.2). Done. The upload form has an optional password field. If a PDF is locked and the password is missing or wrong, the job moves to `needs_password`. The worker holds the job and its file in memory, and the progress fragment (also shown on `/imports/{id}`) asks for the password, or lets the user cancel, which deletes the file. The password lives only on the in-memory job, which leaves it out of its `repr`. It is never written to `statements.error`, the logs or the disk. A restart fails a waiting job, as with any interrupted job (§3), and wipes its file. The tests build a real RC4-encrypted PDF, so this runs against pdfplumber itself rather than a mock.
13. ✅ 🟠 **Rules engine semantics.** `database.apply_rules` overwrites every match, orders rules alphabetically, and never reverts. Its WHERE clause `(category='Uncategorized' OR category != ?)` reduces to `category != ?`. Fix: rewrite per §6. Done in `database._run_rules_engine`. Deleting a rule now re-runs the engine.
14. ✅ 🟠 **No manual edit or delete of transactions** in either UI. `database.delete_transaction` exists but no route or UI calls it. Done in the web UI: each transaction row can be edited in place (category, merchant and transfer flag, saved as `manual`), reset to rules, or deleted, and the editor has a "Create rule…" link that prefills the rules form. The transactions list also gained the §7.1 "uncategorized only" and "transfers" filters. The TUI can set a category and toggle the transfer flag (item 18). Deleting and resetting stay web-only.
15. ✅ 🟠 **No imports page and no undo for an import** (§7.1). The `/imports` list and discard-in-review exist. `/imports` can now delete a committed import, cascading to its transactions, after a confirmation. It can also remove failed or discarded imports. Running jobs can't be deleted. Afterwards the same file can be uploaded again.
16. ✅ 🟠 **Transfers are counted as income and expense** in all analytics (§8). The analytics queries now filter on `is_transfer = 0`. There is no UI to flag a transfer yet, except a `mark_transfer` rule.
17. ✅ 🟠 **Uncategorized count is global**, not per month, and doesn't link anywhere (§8, §7.1). The count is now per month and links to `/transactions?year=…&month=…&uncategorized=1`. The TUI dashboard uses the per-month count too.
18. ✅ 🟠 **TUI gaps.** No page keys, so only the first 50 rows per month are reachable. Dashboard is fixed to the current month. No search, category edit, transfer toggle or account filter (§7.2). Done:
    * `←`/`→` change the month on both screens. The month and account filter live on the app, so both screens stay in sync.
    * `PgUp`/`PgDn` page through the transactions.
    * `/` searches.
    * `a` cycles the account filter, which is also now in `core.analytics` and the transaction queries (§8).
    * `c` sets the category and `x` toggles transfer. Both are manual edits.
    * `r` refreshes.

    Also fixed:
    * `←`/`→` never worked, because the focused `DataTable` swallowed them. They are now priority bindings on the screen.
    * `d`, `t` and `q` were priority bindings, which would have stolen typed text.
    * Header and Footer only rendered on the app's base screen.

    Pilot tests are in `tests/test_tui.py`.
19. ✅ 🟠 **Uploads retained forever** in `uploads/`. Fix: delete after extraction (§3.2, §9.3). The worker deletes the file as soon as its text is read, on success and failure alike.
20. ✅ 🟡 **Config scattered.** The model is hardcoded in `ollama_client.DEFAULT_MODEL` and again in `web/app.py` `Form("llama3.1")`. `₹` / `INR` are hardcoded in `tui/screens/*.py`, `web/templates/*.html` and `web/static/charts.js`. Fix: `config.toml` (§1.1). Done in `core/config.py`. Money formatting is shared by both UIs (`models.format_money`), and the charts read the currency from `<body data-currency>`. The §9.1 and §9.7 warnings for non-local hosts are done too (startup log, plus a UI banner for the LLM host).
21. ✅ 🟡 **Inconsistent connections.** `core/analytics.py::_conn` and `categorizer.preview_rule_matches` open raw sqlite connections without WAL or `busy_timeout`. Fix: use the single helper (§2).
22. ✅ 🟡 **The JSON schema is duplicated** in `SYSTEM_PROMPT` and in Pydantic. The prompt doesn't explain the credit-card sign convention. Fix: §3.3. Done. The prompt now covers the bank and card sign conventions, says balance, total and header lines are not transactions, and includes one example. In a live run with `qwen2.5-coder:14b`, it stopped emitting the "Opening Balance" row that the old prompt produced. Also fixed: parse errors used to embed the model output, and therefore descriptions, in ERROR logs and `statements.error` (§9.5).
23. ✅ 🟡 **No per-chunk timeout or retry** in `ollama_client.extract_transactions`. Done. Every call uses `ollama.Client(host, timeout=chunk_timeout_s)`. A timeout raises `ExtractionTimeout` (checked against a real Ollama). Invalid output is retried up to `max_retries` times and then raises `OutputInvalid`.
24. ✅ 🟡 **`reload=True` in `run_web.py`.** Make it a `--dev` flag. Done, and host and port come from `[web]`.
25. ✅ 🟡 **Web tests read the real `data/expenses.db`**, and the only tests cover Ollama status. Fix: §11 test strategy. Tests now set `EXPENSE_TRACKER_DB` to a temporary file before importing `core`. Every route that lists models has `list_available_models` mocked. A run of the full suite with `ollama.Client` replaced by a recorder made no calls to a real Ollama.

## Appendix B: Migration v1 → v2

✅ Implemented in `core/migrations.py` and run by `database.init_db()` on startup. Tests are in `tests/test_schema_v2.py`.

The existing DB has `user_version = 0` and contains `transactions` (REAL `amount`, id = `sha256(date|desc|amount:.2f)`) and `category_rules`.

1. Create the new tables. Insert an account named `Default` (`kind='bank'`) so the user can rename or split it later.
2. For each distinct `source_file`, insert a `statements` row with `status='committed'`, `extractor='llm'`, `file_sha256 = 'migrated:' || source_file`, and NULL periods and balances.
3. Rebuild `transactions`:
   * `amount_minor = round(amount * 100)`;
   * `account_id` = Default, and `statement_id` from step 2;
   * `category_source = 'rule'` when `category != 'Uncategorized'`, else `'default'`. v1 had no manual edits, so every non-default category came from a rule.
   * Recompute `id` per §5, with ordinals assigned in `injected_at` order within each statement.
4. Add `priority = 0`, `mark_transfer = 0` and `account_id = NULL` to the existing rules, then run the rules engine.
5. Set `user_version = 2`. Wrap everything in one transaction and back up the file to `expenses.db.v1.bak` first.

**Decisions made during implementation:**
* **Zero-amount v1 rows are dropped.** v2 has a `CHECK (amount_minor <> 0)` constraint. The dropped rows survive in the backup and are counted in the `MigrationReport`.
* **Rows that collapse to the same v2 id are merged.** Two v1 rows can differ only in whitespace, which §5 normalizes away. Statements and rows are processed in `injected_at` order, so the earliest import keeps the row.
* **Orphaned categories revert.** v1 never reverted categories when a rule was deleted. Step 4 therefore resets rows whose rule no longer exists to `Uncategorized`, following §6. Their old values remain in the backup.
* **Backups are never overwritten.** If `expenses.db.v1.bak` already exists (for example after a failed attempt), a timestamped name is used.
* **A failure leaves the database unchanged.** The whole migration rolls back and the database stays at v1.
* **Fresh installs skip the migration.** An empty file gets the v2 schema directly.
* **Unknown versions are refused.** Any `user_version` other than 0 or 2 makes startup fail.
