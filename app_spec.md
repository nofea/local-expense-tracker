# Technical Specification: Privacy-First Local Expense Tracker

**Version 2.** This document describes what the application does, how its parts fit together and why they are built the way they are. It is written for someone who has never seen the code or the app. Read it top to bottom once; after that, each numbered section stands on its own and other sections refer to it as "§N".

**Status.** The code implements everything in this version (checked against commit `ffcef6b`). Appendix A records how the current code got here, and lists the known limitations that remain. Appendix B describes the one-time upgrade of databases created by the first version of the app.

---

## Contents

0. [What the app is](#0-what-the-app-is)
1. [Architecture & tech stack](#1-architecture--tech-stack)
2. [Data model](#2-data-model)
3. [Ingestion pipeline](#3-ingestion-pipeline)
4. [Deterministic validator](#4-deterministic-validator)
5. [Identity & deduplication](#5-identity--deduplication)
6. [Categorization](#6-categorization)
7. [User interfaces](#7-user-interfaces)
8. [Analytics](#8-analytics)
9. [Privacy & security requirements](#9-privacy--security-requirements)
10. [Out of scope](#10-out-of-scope)
11. [Testing & acceptance criteria](#11-testing--acceptance-criteria)
- [Appendix A: Implementation history & known limitations](#appendix-a-implementation-history--known-limitations)
- [Appendix B: Migration v1 → v2](#appendix-b-migration-v1--v2)

---

## 0. What the app is

The app turns bank and credit-card statements into a searchable, categorized ledger of transactions, and shows monthly summaries of income and spending. It is built for a single person who does not want their financial data to leave their computer.

A typical session looks like this:

1. The user downloads a statement (a PDF, or a CSV export) from their bank's website.
2. They upload it in the app's web UI and say which account it belongs to (for example "HDFC Savings" or "Amex Gold").
3. The app reads the transactions out of the file. A CSV whose columns the user has described once is read directly. A PDF, or a CSV with an unfamiliar layout, is read by a large language model (LLM) that runs **on the user's own machine** through [Ollama](https://ollama.com).
4. The app shows the extracted rows on a **review** screen, with anything suspicious highlighted. The user fixes or drops rows, then **commits** them.
5. Committed rows enter the **ledger**. User-defined **rules** assign them categories ("SWIGGY" → Food).
6. The dashboard (in the browser, or in a terminal UI) shows income, expenses, net savings and a spending breakdown for any month.

### 0.1 Principles

These four rules drive most of the design decisions in this document.

1. **Local only.** Nothing leaves the machine except the calls the app makes to the configured Ollama server, which by default is on the same machine (`localhost`). There are no CDNs, no web fonts, no telemetry and no update checks. If the user points the app at an Ollama server on another machine, the UI warns them on every page (§9.7).
2. **Deterministic where possible.** An LLM can misread or invent data, so it is used only for the one step that needs it: turning unstructured text (a PDF, or a CSV the app has no mapping for) into rows. Everything after that — validation, duplicate detection, categorization and analytics — is ordinary code that gives the same answer every time.
3. **Nothing enters the ledger unreviewed.** Extracted rows are held in a staging area. They only become real transactions when the user explicitly commits them (§3.5).
4. **Imports are reversible.** Every transaction remembers which statement it came from. Deleting a statement deletes its transactions, so a bad import can be undone in one step (§3.6).

### 0.2 Glossary

| Term | Meaning |
|---|---|
| **Account** | A bank account or credit card the user owns, e.g. "HDFC Savings". Every statement belongs to one account. |
| **Statement** | One uploaded file. In the database a statement row also serves as the record of its import job (§3) and keeps the period and balances printed on the statement. |
| **Import / job** | The process of turning one uploaded file into staged rows. "Import" and "statement" refer to the same database row. |
| **Staged row** | A row extracted from a statement that is waiting for review. Staged rows are never counted in any figure. |
| **Ledger** | The `transactions` table: rows the user has committed. All analytics read from it. |
| **Commit** | The user's action that moves a statement's reviewed rows into the ledger. |
| **Minor units** | The smallest unit of the currency. For Indian rupees (the default), 1 rupee = 100 paise, so ₹12.50 is stored as `1250`. |
| **Transfer** | Money moving between the user's own accounts, e.g. paying a credit-card bill from a savings account. Transfers are neither income nor expense, so analytics ignore them. |
| **Rule** | A user-defined "if the description contains X, the category is Y" instruction (§6). |
| **CSV mapping** | A saved description of a bank's CSV layout (which column holds the date, which the amount, and so on), so later files with the same layout can be read without the LLM (§3.2). |
| **Chunk** | A slice of a statement's text small enough to send to the LLM in one request (§3.3). |
| **Ollama** | A program that runs open-weight LLMs locally and serves them over HTTP. The app talks to it at `http://localhost:11434` by default. |

---

## 1. Architecture & Tech Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11 or newer, for everything. |
| Storage | One SQLite database file, by default `data/expenses.db`. Both user interfaces read and write the same file. |
| LLM | Ollama, called through its chat API with a JSON schema that constrains the output (the `format` parameter). Other LLM backends are out of scope. |
| Web UI | FastAPI (server) + Jinja2 (HTML templates) + [HTMX](https://htmx.org) (small in-page updates without a JavaScript framework). HTMX and all other assets are stored in the repository under `web/static/`. Charts are hand-written SVG. |
| Terminal UI | [Textual](https://textual.textualize.io), a Python library for terminal apps. |
| PDF text | `pdfplumber` (which uses `pdfminer.six`). |
| Validation of LLM output | Pydantic v2 models. |

### 1.1 Components

The code is split into three layers. `core/` contains all of the logic and all database access. `web/` and `tui/` are thin front-ends that call functions in `core` directly; there is no internal API or service layer between them.

```
core/
  config.py         Settings from config.toml (§1.2)
  database.py       Connections, accounts, ledger queries, rules engine (§2, §6)
  migrations.py     Schema creation and the v1 → v2 upgrade (§2, Appendix B)
  models.py         Pydantic models, money conversion, transaction ids (§2, §5)
  jobs.py           Background worker that runs imports (§3)
  ingestion.py      Text extraction from PDF/CSV, chunking, LLM orchestration (§3.2, §3.3)
  ollama_client.py  All calls to Ollama, prompts, retries and timeouts (§3.3)
  csv_import.py     Deterministic CSV parsing with a saved mapping (§3.2)
  staging.py        Staged rows, review edits, commit, discard, delete (§3.4–§3.6)
  validator.py      Pure validation checks for staged rows (§4)
  categorizer.py    Thin wrappers for adding/removing rules and re-running them (§6)
  analytics.py      Monthly figures for the dashboards (§8)
web/
  app.py            Every HTTP route
  templates/        Jinja2 pages; templates/partials/ holds the HTMX fragments
  static/           style.css, charts.js, vendored htmx.min.js
tui/
  app.py            Textual application and shared view state
  screens/          Dashboard, Transactions and a text-prompt modal
  style.tcss        Terminal styling
tests/              unittest suites (§11)
run_web.py          Starts the web server
config.example.toml Documented example of every setting
```

### 1.2 Data flow

```
                      ┌──── CSV whose header matches a saved mapping ────┐
                      │       (core/csv_import.py, no LLM)               │
[Upload] ─► [tmp file]┤                                                  ├─► [Staging + validator] ─► review ─► commit ─► [Ledger] ─► [Rules engine]
                      │                                                  │      (staging.py,                               │
                      └── PDF / unmapped CSV ─► [Chunker] ─► [Ollama] ───┘       validator.py)                    [Web UI] [TUI] ◄┘
                              (ingestion.py)     (ollama_client.py)
```

### 1.3 Processes and threads

* **The web server** (`python run_web.py`) is the only process that runs imports. When `web/app.py` is imported it initializes the database (§2.1) and calls `jobs.start()`, which recovers from any previous crash (§3) and starts **one** background worker thread. Uploads are queued to that thread and processed one at a time, so a slow LLM call never blocks the web server from answering other requests.
* **The terminal UI** (`python -m tui.app`) can view the ledger and make small edits (§7.2). It never starts or recovers import jobs, because doing so would mark the web server's running jobs as failed.
* Both may run at the same time against the same database file. SQLite's WAL mode and a 5-second busy timeout (§2.1) let them share it safely.

### 1.4 Configuration

All settings live in one optional file, `config.toml`, at the repository root. It is git-ignored, because it is personal to each installation. `config.example.toml` lists every key with a comment; copy it to `config.toml` and edit. The environment variable `EXPENSE_TRACKER_CONFIG` can point at a different file.

Every key is optional. A missing file or a missing key means the default shown below. No other module may hardcode any of these values; they must always be read from the configuration.

```toml
[llm]
host = "http://localhost:11434"   # where Ollama listens
model = "llama3.1"                # model pre-selected in the upload form
chunk_timeout_s = 120             # seconds to wait for one chunk before giving up on it
max_retries = 1                   # extra attempts when the model's output doesn't match the schema

[money]
currency_code = "INR"             # ISO 4217 code, used by the charts
currency_symbol = "₹"             # shown in front of every amount
minor_units = 2                   # decimal places of the currency (paise per rupee = 10^2)

[storage]
db_path = "data/expenses.db"      # relative paths are relative to the repo root

[web]
host = "127.0.0.1"                # address the web server listens on
port = 8000
```

Rules:

* **Strict validation.** An unknown section, an unknown key, a value of the wrong type, or an out-of-range value stops startup with an error that names the file, section and key. The checks are: `llm.host` must be an `http://` or `https://` URL; `chunk_timeout_s` > 0; `max_retries` ≥ 0; `minor_units` between 0 and 4; `port` between 1 and 65535. (An integer is accepted where a float is expected.)
* **`EXPENSE_TRACKER_DB`**, when set, overrides `storage.db_path`. The tests use it to point at a temporary file.
* **`minor_units` must never change once data exists.** Stored amounts and transaction ids (§5) are expressed in those units, so changing it would silently rescale every amount.
* **Loaded once.** The configuration is read the first time it is needed and cached for the life of the process.
* **Non-local hosts are flagged.** If `web.host` is not a loopback address, `run_web.py` logs a warning that anyone on the network can read the data. If `llm.host` is not loopback, `run_web.py` logs a warning and every web page shows a banner that statement text is leaving the machine (§9.7). "Loopback" means `localhost`, `127.0.0.0/8` or `::1`.

---

## 2. Data Model

### 2.1 Conventions

* **Money is stored as integers in minor units**, in columns named `*_minor` (e.g. `amount_minor = -45050` for −₹450.50). Floating-point numbers cannot represent most decimal amounts exactly, and summing them drifts. Conversion to and from decimal numbers happens only at the edges: when parsing input and when displaying output. `models.to_minor` (which rounds half up, after going through the decimal string form so that `0.1` really means 0.1) and `models.from_minor` are the only two conversion functions.
* **Sign convention.** Amounts are signed from the account holder's point of view:
  * **Negative** = money out of a bank account, or a credit card's balance owed going up.
  * **Positive** = money into a bank account, or a credit card's balance owed going down.
  * So a credit-card purchase is negative, a refund to the card is positive, and a payment to the card is positive **and** is a transfer (the money came from the user's own bank account).
* **Dates** are ISO `YYYY-MM-DD` text. A statement may print two dates per row: the *transaction date* (when it happened) and the *value date* (when the bank settled it). **Every month-based figure uses `transaction_date`**; `value_date` is stored for information only.
* **Schema versioning.** The schema version is stored in SQLite's `PRAGMA user_version`. On every startup, `database.init_db()` calls `migrations.migrate()`, which creates the current schema in an empty file, upgrades a version-0 (v1) file per Appendix B, does nothing for a current file, and refuses to start for any other version. Schema statements run one by one inside an explicit transaction (never with `executescript`, which would commit part-way through).
* **Connections.** Every connection, including the read-only analytics queries, is opened by `database.connect()`. It sets `journal_mode=WAL` (readers don't block the writer), `foreign_keys=ON` (SQLite enforces references only when asked) and `busy_timeout=5000` (wait up to 5 s for a lock instead of failing at once). Every database function accepts an optional `db_path` argument, so tests can use a separate file.
* **Write ordering.** Any write that depends on a status check (for example "commit only if the statement is still in review") runs inside `BEGIN IMMEDIATE`, which takes the write lock before the check. This stops two browser tabs from committing the same statement twice.

### 2.2 Tables

```sql
-- A bank account or credit card the user owns.
CREATE TABLE accounts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,              -- e.g. "HDFC Savings", "Amex Gold"
    kind          TEXT NOT NULL CHECK (kind IN ('bank', 'credit_card')),
    institution   TEXT                               -- optional, e.g. "HDFC Bank"
);

-- One uploaded file. Also the record of its import job (§3).
CREATE TABLE statements (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    file_name       TEXT NOT NULL,                   -- sanitized original name, display only
    file_sha256     TEXT NOT NULL,                   -- hash of the uploaded bytes; detects re-uploads
    period_start    TEXT,                            -- YYYY-MM-DD, NULL if unknown
    period_end      TEXT,
    opening_minor   INTEGER,                         -- NULL: not all statements print balances
    closing_minor   INTEGER,
    extractor       TEXT NOT NULL CHECK (extractor IN ('csv_mapping', 'llm')),
    model           TEXT,                            -- Ollama model used; NULL for csv_mapping
    status          TEXT NOT NULL CHECK (status IN
                      ('queued', 'needs_password', 'extracting', 'review',
                       'committed', 'failed', 'discarded')),
    progress_done   INTEGER NOT NULL DEFAULT 0,      -- chunks processed so far
    progress_total  INTEGER NOT NULL DEFAULT 0,      -- chunks in total
    error           TEXT,                            -- failure reason, or one line per failed chunk
    reconciled      INTEGER,                         -- 1 balances add up, 0 mismatch, NULL can't check (§4)
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    committed_at    TEXT,
    UNIQUE (account_id, file_sha256)
);

-- Rows extracted from a statement and awaiting review. Never read by analytics.
CREATE TABLE staged_rows (
    id               INTEGER PRIMARY KEY,
    statement_id     INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,               -- 0-based order within the statement
    transaction_date TEXT,                           -- raw LLM/CSV output; may be invalid
    value_date       TEXT,                           -- set to NULL by the validator if invalid
    raw_description  TEXT,
    amount_minor     INTEGER,                        -- NULL if the amount couldn't be parsed
    is_transfer      INTEGER NOT NULL DEFAULT 0,     -- the user can tick this in review
    dropped          INTEGER NOT NULL DEFAULT 0,     -- the user removed it in review
    issues           TEXT NOT NULL DEFAULT '[]'      -- JSON list of validator codes (§4)
);

-- The ledger.
CREATE TABLE transactions (
    id               TEXT PRIMARY KEY,               -- deterministic hash, see §5
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    statement_id     INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    transaction_date TEXT NOT NULL,
    value_date       TEXT,
    raw_description  TEXT NOT NULL,                  -- as extracted, whitespace-normalized
    cleaned_merchant TEXT,                           -- display name; set by a rule or the user; never matched on
    amount_minor     INTEGER NOT NULL CHECK (amount_minor <> 0),
    is_transfer      INTEGER NOT NULL DEFAULT 0,     -- 1 = excluded from income and expense
    category         TEXT NOT NULL DEFAULT 'Uncategorized',
    category_source  TEXT NOT NULL DEFAULT 'default'
                       CHECK (category_source IN ('default', 'rule', 'manual')),
    rule_id          INTEGER REFERENCES category_rules(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_tx_date     ON transactions(transaction_date);
CREATE INDEX idx_tx_category ON transactions(category);
CREATE INDEX idx_tx_account  ON transactions(account_id, transaction_date);

-- User-defined categorization rules (§6).
CREATE TABLE category_rules (
    id               INTEGER PRIMARY KEY,
    pattern          TEXT NOT NULL UNIQUE,           -- case-insensitive substring of raw_description
    category         TEXT NOT NULL,
    cleaned_merchant TEXT,                           -- optional display name to set
    mark_transfer    INTEGER NOT NULL DEFAULT 0,     -- e.g. for "CRED CLUB", "AUTOPAY"
    priority         INTEGER NOT NULL DEFAULT 0,     -- higher wins
    account_id       INTEGER REFERENCES accounts(id) -- NULL = applies to all accounts
);

-- Saved CSV layouts, per account (§3.2).
CREATE TABLE csv_mappings (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    header_signature  TEXT NOT NULL,                 -- sha256 of the normalized header row
    skip_rows         INTEGER NOT NULL DEFAULT 0,    -- preamble lines above the header row
    date_col          TEXT NOT NULL,                 -- column names as printed in the header
    date_format       TEXT NOT NULL,                 -- strptime format, e.g. "%d/%m/%Y"
    value_date_col    TEXT,
    description_col   TEXT NOT NULL,
    amount_col        TEXT,                          -- a single signed column, OR
    debit_col         TEXT,                          -- separate money-out and
    credit_col        TEXT,                          -- money-in columns
    negate            INTEGER NOT NULL DEFAULT 0,    -- flip every sign (e.g. card CSVs that list charges as positive)
    UNIQUE (account_id, header_signature),
    CHECK ((amount_col IS NOT NULL) OR (debit_col IS NOT NULL AND credit_col IS NOT NULL))
);
```

Notes on the tables:

* **Categories are free text.** There is no categories table; the list of categories shown in the UI is the distinct values in `transactions.category`. `Uncategorized` is reserved for "no rule or user has assigned one".
* **Transfers are a flag, not a category.** A transfer still has a category (it can be "Credit card payment", say), but `is_transfer = 1` excludes it from every figure (§8).
* **`category_source`** records who set the category: `default` (nobody), `rule` (the rules engine, with `rule_id` saying which rule) or `manual` (the user). §6 explains how it controls the rules engine.
* **`raw_description` is the matching key; `cleaned_merchant` is for display.** Rules always match on the description as printed on the statement. A friendlier merchant name (e.g. "Swiggy" for `UPI-SWIGGY-8812`) is shown when set but is never matched against.
* **Cascades.** Deleting a statement deletes its staged rows and its transactions (`ON DELETE CASCADE`). Deleting a rule leaves the transactions it categorized in place, with `rule_id` set to NULL, until the rules engine runs again (§6).
* Queries for the transaction list join `statements` so each row can show the name of the file it came from.

### 2.3 Statement lifecycle

A statement's `status` moves through these states:

```
 upload ──► queued ──► extracting ──► review ──► committed
               ▲            │            │
               │            │            └──► discarded        (user clicked "Discard")
   password    │            ├──► failed                         (no text, no rows, or an unexpected error)
   given       │            │
               └──── needs_password ──► discarded              (user cancelled)

 web-server startup: queued / needs_password / extracting ──► failed ("interrupted")
```

* `queued`: waiting for the worker thread.
* `extracting`: the worker is reading the file and calling the LLM. `progress_done`/`progress_total` count chunks.
* `needs_password`: the PDF is encrypted and the password was missing or wrong (§3.2).
* `review`: rows are staged and waiting for the user (§3.5).
* `committed`: rows are in the ledger.
* `failed`: extraction did not produce anything to review. `error` says why.
* `discarded`: the user threw the import away. Its staged rows are deleted.

A `failed` or `discarded` statement does not block uploading the same file again: the new upload replaces it (§3.1).

---

## 3. Ingestion Pipeline

Ingestion is everything from "the user picks a file" to "rows are waiting on the review screen". It runs as a **background job**: the upload request stores the file, creates a `statements` row in `queued` and returns at once. A single worker thread (`core/jobs.py`) then processes queued jobs one at a time. The `statements` row is the job record: the browser polls it to show progress.

**Crash recovery.** When the web server starts, `jobs.start()` marks every statement still in `queued`, `needs_password` or `extracting` as `failed` with the error "interrupted", because the thread that was handling it no longer exists. It then empties the temporary upload directory, so no statement file survives a crash.

### 3.1 Upload (`POST /upload`)

1. **The form.** The user picks an existing account *or* types the name (and type) of a new one, chooses a `.pdf` or `.csv` file, chooses an Ollama model (the list comes from Ollama; the configured `llm.model` is pre-selected), and may enter a PDF password.
2. **File checks.** The client-supplied filename is used only as display text. It is reduced to its last path component (backslashes count as separators), stripped of control characters, truncated to 255 characters, and replaced by `upload` if nothing is left. Its extension must be `.pdf` or `.csv`. For a PDF, Ollama must be reachable, or the upload is refused right away. (A CSV may not need the LLM, so that check happens later.)
3. **Storage.** The file is streamed to disk under a generated name (`statement-XXXX.pdf`) in a private `tmp/` directory next to the database (created with mode `0700`). Its SHA-256 hash is computed while it is written. If it grows beyond **20 MB**, the upload is stopped and the partial file deleted.
4. **Account.** A new account name wins (an existing account with exactly that name is reused). Otherwise the selected account id is used, and it must still exist. With neither, the account named `Default` is used, and created if needed.
5. **CSV mapping lookup.** For a CSV, the app looks for a saved mapping for this account whose header signature matches the file (§3.2). With no match, the upload returns the **CSV mapping form** instead of starting a job, and the file waits in `tmp/` until the user decides.
6. **Duplicate check.** If the same account already has a statement with the same `file_sha256` that is `queued`, `needs_password`, `extracting`, `review` or `committed`, the upload is refused with a message linking to that statement, and the file is deleted. A previous `failed` or `discarded` statement for the same bytes is deleted and replaced. (For an unmapped CSV this check happens before the mapping form is shown, so the user doesn't map a file they can't import.)
7. **Ollama check.** If the LLM will be used (a PDF, or a CSV without a mapping), Ollama must be reachable.
8. **Job.** The server inserts the `statements` row (`status='queued'`, `extractor` = `csv_mapping` or `llm`, `model` only for `llm`), queues the job and returns a progress fragment. The fragment polls `GET /imports/{id}/status` every second, showing "chunk N of M" and a progress bar, until the job leaves `queued`/`extracting`. It then shows a link to the review screen, the failure reason, or a password prompt.

### 3.2 Reading the file

#### CSV with a saved mapping (no LLM)

Banks export CSVs in many layouts, often with a few lines of account details ("preamble") above the real header row. A **CSV mapping** describes one layout for one account. It is identified by the **header signature**: the SHA-256 of the header row, after trimming each cell, collapsing internal whitespace, case-folding, dropping trailing empty cells and joining with `|`. The header row is looked for `skip_rows` lines into the file. The first saved mapping (lowest id) whose signature matches is used.

Parsing (`core/csv_import.py`):

* The file is read as UTF-8 (a byte-order mark is ignored; undecodable bytes are replaced). The delimiter is detected among `,` `;` tab and `|`, falling back to comma. Every cell is trimmed.
* Every non-blank line below the header row becomes one staged row. **No data row is ever silently dropped**: a date that doesn't match the format is passed through as-is, and an amount that doesn't parse becomes empty, so the validator (§4) flags them in review.
* **Dates** are parsed with the mapping's `strptime` format. A trailing time (`31/03/2025 10:15`) is ignored. The value-date column, if mapped, uses the same format.
* **Amounts** understand thousands separators, currency symbols, a leading minus, accounting-style parentheses (`(1,234.50)` = −1234.50), and `Dr`/`Cr` suffixes (`500.00 Dr` = −500, `500.00 Cr` = +500).
* **Single amount column:** the parsed value is used as-is. **Debit/credit columns:** both are read as absolute values and amount = credit − debit (a blank cell counts as 0; if both are blank the amount is empty). **`negate`** then flips the sign, for card exports that list charges as positive numbers.
* The statement period is taken from the earliest and latest valid transaction dates. Balances are left empty for the user to enter in review.
* The `not_in_source` check (§4) is skipped, because the descriptions come straight from the file.

#### CSV without a mapping: the mapping form

The form (`partials/csv_mapping.html`) helps the user describe the layout once:

* **Header row.** The app guesses how many preamble lines there are: data rows share one width (number of cells up to the last non-empty one), preamble lines are narrower, so the header is the first row at least as wide as the most common width. The user can change the number; the form then reloads with that row as the header.
* **Column guesses.** From the header names: a column containing "date" (but not "value") for the date, "value" for the value date, words like "description", "narration", "particulars", "details", "remark", "memo", "payee" or "merchant" for the description, "debit"/"withdrawal"/"Dr" and "credit"/"deposit"/"Cr" for split amount columns, otherwise "amount"/"amt" for a single column.
* **Date format.** The first of 13 offered formats (e.g. `%Y-%m-%d`, `%d/%m/%Y`, `%d-%b-%Y`, `%m/%d/%Y`) that parses every sample value of the date column.
* The first five data rows are shown as a preview.

The user then chooses one of:

* **Save mapping & read file.** The mapping is checked: the date and description columns must be chosen; either an amount column or both a debit and a credit column; every chosen column must exist in the header; the date format must be one of the offered ones; there must be at least one data row; and at least one value in the date column must parse with the format. If a check fails, the form is shown again with the error. Otherwise the mapping is saved for the account (replacing any earlier mapping with the same signature) and the file is parsed as above.
* **Extract with LLM instead.** The file is treated like a PDF (below). No mapping is saved.
* **Cancel.** The file is deleted.

The waiting file is identified in the form by its generated name, which serves as a token. The server only accepts tokens of the exact form `statement-<letters/digits/_>.csv` that name an existing file in `tmp/`, so a forged token can never point at another path. If the server restarted in the meantime, `tmp/` has been emptied and the user is asked to upload again.

#### PDF, or CSV sent to the LLM

* **PDF:** each page's text is extracted with `pdfplumber`, using the password if one was given. Pages with no text are skipped.
  * If the PDF is encrypted and the password is missing or wrong, the job moves to `needs_password` with the message "This PDF is password-protected." or "The password is incorrect." The worker keeps the job, including its file, in memory. The progress fragment (also shown on `/imports/{id}`) asks for the password (`POST /imports/{id}/password`), which re-queues the job, or offers **Cancel import** (`POST /imports/{id}/cancel`), which deletes the file and marks the statement `discarded`. The password exists only in the in-memory job object, which leaves it out of its printed representation; it is never written to the database, the logs or the disk. A server restart fails a waiting job like any other interrupted job.
  * A PDF encrypted with a method pdfplumber cannot read fails with "This PDF uses an encryption method that can't be read."
* **CSV for the LLM:** the first row is treated as the header. Each row is joined with ` | ` into one line of text. The header (plus a separator line) is repeated at the top of every chunk (§3.3).
* If no text at all comes out of the file, the job fails with "No text could be extracted: no text layer (scanned PDF?) or an empty file." Reading scanned images (OCR) is out of scope.

#### The uploaded file is always deleted

The worker deletes the temporary file **as soon as its text has been read**, whether reading succeeded or failed. The only exception is a job waiting for a password, which keeps its file until the password arrives or the user cancels. Original statements are never retained (§9.3); the database keeps only `file_name` and `file_sha256`.

### 3.3 LLM extraction (PDFs and unmapped CSVs)

**Chunking.** A statement can be longer than a model handles well in one request, so its text is split into chunks of at most `CHUNK_SIZE = 6000` characters:

* Whole pages are packed into a chunk until the next page would not fit.
* A page longer than the limit is split on line boundaries. A row is never cut in half. A single line longer than the limit becomes a chunk of its own.
* For CSV text, the header is prepended to every chunk so the model always sees the column names. The header is not a transaction, so repeating it cannot create rows.
* **Chunks never overlap.** Overlap is a common trick to avoid losing rows at chunk edges, but here it would make the model emit the rows in the overlap twice, and the dedup scheme (§5) would keep both copies as genuine repeats.

**Calls.** Each chunk gets one chat request to Ollama, with:

* `format` = the JSON schema of `TransactionExtractionResult` (below), which forces the model's output into that shape;
* `temperature = 0`, so the same input gives (nearly) the same output;
* a client timeout of `llm.chunk_timeout_s`.

**Failures are per chunk.**

* Output that does not match the schema is retried up to `llm.max_retries` more times, then fails with "model output did not match the transaction schema (N validation error(s))". (A response that is a bare list of transactions, or a dict whose `transactions` list validates item by item, is still accepted.)
* A timeout fails at once with "no response from the model within Ns"; it is not retried, because a retry would multiply the wait. Connection errors are not retried either.
* A failed chunk is recorded as one line, `Chunk N/M failed: <reason>`, and the remaining chunks still run. The review screen shows each line as a warning banner, so the user knows rows may be missing.
* If the chunks together produce **no rows at all**, the job fails, with the chunk errors as the reason, or "The model found no transactions." if there were none.

**Statement metadata.** After the transaction pass, the first chunk and (if different) the last chunk are each asked, with a separate prompt and the `StatementMetadata` schema, for the statement period and opening/closing balances. The first chunk's answers win, except that the period end and closing balance prefer the last chunk, where statement summaries usually appear. A failure here is logged and only costs the metadata. Values that are not valid ISO dates are discarded. The user can enter or correct all four values in review.

**Prompts.** The system prompt (`ollama_client.SYSTEM_PROMPT`) explains the task, the date format (with a hint that statements usually print dates day-first), that descriptions must be copied exactly, and the sign convention of §2.1 for both bank accounts and credit cards. It says that balance, total, header and footer lines are not transactions, and that the running-balance column is never the amount. It ends with one short worked example. It does **not** describe the JSON structure: that is enforced through `format` and defined only once, in the Pydantic models.

**Progress.** `progress_total` is set to the number of chunks before the first call, and `progress_done` is updated after each chunk.

The output schemas (in `core/models.py`, the single source of truth):

```python
class ExtractedTransaction(BaseModel):
    transaction_date: str          # deliberately not a date type: bad dates must reach review (§4)
    value_date: str | None = None
    raw_description: str
    amount: float                  # major units; converted with to_minor() when staged

class TransactionExtractionResult(BaseModel):
    transactions: list[ExtractedTransaction]

class StatementMetadata(BaseModel):
    period_start: str | None = None
    period_end: str | None = None
    opening_balance: float | None = None
    closing_balance: float | None = None
```

Each field also carries a short `description` that ends up in the JSON schema sent to the model.

### 3.4 Staging

Extracted rows (from the CSV parser or the LLM) are written to `staged_rows` in document order, with `position` = 0, 1, 2, … Amounts are converted to minor units; an amount that cannot be converted is stored as NULL. Then the validator (§4) runs over the whole statement, the period and balances are stored, the chunk errors (if any) are saved in `statements.error`, and the statement moves to `review`. All of this happens in one database transaction.

### 3.5 Review & commit (`/imports/{id}`)

While a job is queued, extracting or waiting for a password, the review page shows the same live progress fragment as the upload page. Once the statement is in `review`, the page lists every staged row with its issues highlighted (errors in red, warnings in amber, duplicates noted), plus a summary: the number of rows, the number with errors, warnings and duplicates, and the total amount.

The user can:

* **edit a row's** transaction date, description or amount, or tick its transfer box, and save it (`POST /imports/{id}/rows/{row_id}`);
* **drop** a row, or **restore** a dropped one (`POST /imports/{id}/rows/{row_id}/drop`);
* **edit the statement details**: period start/end and opening/closing balance, where blank means unknown (`POST /imports/{id}/meta`);
* see the **reconciliation** result (do the balances add up? §4) and the difference if not;
* **commit** (`POST /imports/{id}/commit`), or
* **discard** the whole import (`POST /imports/{id}/discard`): its staged rows are deleted, the statement becomes `discarded`, and nothing is saved.

After every action the server revalidates the whole statement and re-renders the review body, because one edit can change other rows' duplicate flags and the reconciliation. Edits are refused once the statement has left `review`.

**Commit** is allowed only when there is at least one non-dropped row and no non-dropped row has an error-level issue. If the balances don't reconcile, the user must also tick "Commit anyway". The server revalidates first and enforces both conditions itself, whatever the browser sends. Then, in a single database transaction:

1. Compute the §5 id of each non-dropped row (ordinals counted over those rows in `position` order) and `INSERT OR IGNORE` it into `transactions`, copying the date, value date, whitespace-normalized description, amount and transfer flag. Rows whose id already exists are skipped. The page then reports "Committed: N new transaction(s), M already in the ledger."
2. Delete the statement's staged rows, and set `status='committed'` and `committed_at`.
3. Run the rules engine (§6) over the ledger, which categorizes the new rows.

### 3.6 Managing imports (`/imports`)

The Imports page lists every statement, newest first, with its account, period, row count (staged rows before commit, ledger rows after), reconciliation result, status and upload time. Actions depend on the status:

* `review` → **Review**; `needs_password` → **Enter password** (both open `/imports/{id}`).
* `committed` → **Delete import**, after a confirmation that names the number of transactions. This deletes the statement and, by cascade, all of its transactions, **including any manual edits** to them. The same file can then be uploaded again.
* `failed` or `discarded` → **Remove** (deletes the record).
* Running jobs (`queued`, `extracting`, `needs_password`) can't be deleted.

---

## 4. Deterministic Validator

The validator (`core/validator.py`, pure functions with no database access) checks staged rows when they are first staged, and again after every review edit. Each problem is recorded as a code in the row's `issues` list. The level decides what happens:

* **error** — the row cannot be committed until it is fixed or dropped;
* **warning** — the row is highlighted, but can be committed;
* **info** — for information only.

| Code | Level | Rule |
|---|---|---|
| `bad_date` | error | `transaction_date` is not a real calendar date written exactly as `YYYY-MM-DD`. |
| `zero_amount` | error | The amount is zero, or missing (it could not be parsed). The ledger forbids zero amounts. |
| `missing_desc` | error | `raw_description` is empty after trimming whitespace. |
| `bad_value_date` | warning | `value_date` was present but not a valid `YYYY-MM-DD` date. It is set to NULL. |
| `not_in_source` | warning | The description, case-folded with whitespace collapsed, does not appear anywhere in the statement's extracted text (normalized the same way). This flags descriptions the model may have invented or altered. |
| `duplicate_in_ledger` | info | The row's §5 id already exists in `transactions`, so commit will skip it. This is computed only for non-dropped rows without errors, with ordinals counted exactly as commit will count them. Typical cause: an overlapping statement was imported earlier. |

**Reconciliation** is a statement-level check. When both the opening and closing balances are known, the validator checks that `opening + Σ(amount_minor of non-dropped rows) = closing` (missing amounts count as 0). The result is stored in `statements.reconciled`: `1` if it adds up, `0` if not, `NULL` if either balance is unknown. On a mismatch, the review screen shows the difference (`closing − expected`), and commit needs an explicit "Commit anyway". A mismatch usually means the model missed or invented a row, or a sign is wrong.

Not validated, by decision: whether a row's date falls inside the statement period. Banks often list transactions posted after the period end.

**Sticky warnings.** The source text is not stored (§9.3), so `not_in_source` can only be computed once, at staging time:

* On later revalidations, a row keeps `not_in_source` unless the user changes its description. Editing the description clears it, because the user has now vouched for the text.
* `bad_value_date` stays on the row in the same way, after its value date was cleared.
* If no source text was extracted at all (for example a mapped CSV), the check is skipped.

---

## 5. Identity & Deduplication

Bank statements often overlap (a statement for 1–31 March, then one for 15 March–14 April), and users may upload the same file twice. The app must import each real transaction exactly once, without losing genuine repeats such as two identical coffees on the same day. It does this with a deterministic transaction id:

```
norm(desc) = raw_description with runs of whitespace collapsed to one space and trimmed (case preserved)
ordinal    = 0-based index of the row among the rows of the SAME statement that have identical
             (transaction_date, norm(desc), amount_minor), counted in document order
id         = sha256(f"{account_id}|{transaction_date}|{norm(desc)}|{amount_minor}|{ordinal}")
```

How the cases work out:

* **The same file uploaded again** is stopped before extraction, by `file_sha256` (§3.1).
* **Overlapping statements.** Every day inside the overlap appears in full in both files, so identical rows get the same ordinals in both, therefore the same ids, and `INSERT OR IGNORE` skips them on the second commit. Review shows them as `duplicate_in_ledger` beforehand.
* **Genuine repeats** — two ₹150 coffees at the same shop on the same day — get ordinals 0 and 1 within the statement, so both are kept.
* **The account is part of the id**, so the same description and amount on two different accounts are two transactions.

Known limitations:

* If two statements describe the same transaction with different text (say, the LLM read it slightly differently each time), it is imported twice and `duplicate_in_ledger` will not catch it. The user can delete the extra row from the transaction list (§7.1).
* The same happens when a statement starts or ends part-way through a day, so a boundary day is only partly listed in one of the files.

Rules for code that inserts transactions:

* The ordinal restarts on every call, so a **whole statement** must be inserted in one call.
* The id formula is part of the data contract. Changing it requires a migration that recomputes every stored id.
* `database.import_statement` writes a statement straight to the ledger, bypassing review. It exists only for tests and must not be used by the upload flow.

---

## 6. Categorization

**Matching.** A rule matches a transaction when:

* its `pattern`, case-folded (Python `str.casefold()`), is a substring of the transaction's case-folded `raw_description` — this is plain text matching, so characters like `%` and `_` have no special meaning; and
* the rule's `account_id` is NULL (all accounts) or equals the transaction's account.

A rule with a blank pattern never matches.

**The rules engine** (`database._run_rules_engine`) runs after every commit, after a rule is created, updated or deleted, after "Reset to rules" on a transaction, and when the user clicks "Re-apply all rules". It looks at every transaction in the ledger whose `category_source` is not `manual`:

* It picks the matching rule with the **highest `priority`**; ties go to the rule with the **lowest id** (the oldest).
* If a rule matches, it sets `category`, `rule_id` and `category_source='rule'`, and sets `cleaned_merchant` to the rule's value (NULL if the rule has none — on non-manual rows the merchant name only ever comes from a rule). If the rule has `mark_transfer`, it sets `is_transfer = 1`.
* If no rule matches and the row was categorized by a rule (`category_source='rule'`), it reverts the row to `Uncategorized`, `category_source='default'`, and clears `rule_id` and `cleaned_merchant`. So **deleting a rule undoes its effect**.
* The engine **never clears `is_transfer`**, because the flag may also have been set by the user in review. As a result, deleting a `mark_transfer` rule leaves the transfer flags it set in place.

**Manual edits.** When the user changes a single transaction's category, merchant name or transfer flag (web or TUI), the row becomes `category_source='manual'` with `rule_id` cleared, and the rules engine never touches it again. A blank category becomes `Uncategorized`.

**Reset to rules** (web only) returns a manual row to the engine: it sets the category to `Uncategorized`, `category_source='default'`, clears the merchant name **and the transfer flag**, then runs the engine. The transfer flag is cleared because the database doesn't record whether it came from review or from the manual edit; a `mark_transfer` rule will set it again.

**Example.** Rules: `SWIGGY` → Food (priority 0) and `SWIGGY INSTAMART` → Groceries (priority 10). The description `UPI-SWIGGY INSTAMART-1234` matches both, and Groceries wins on priority. `UPI-SWIGGY-8812` matches only the first, so it becomes Food. If the user then manually sets the second row to "Eating out", no later rule change affects it until they click "Reset to rules".

**Rule editor (Rules page).**

* The form takes a pattern, a category, an optional merchant name, a priority (default 0), a "mark as transfer" box and an optional account scope.
* Patterns are unique. Saving a rule with an existing pattern **updates** that rule instead of adding a second one; this is how rules are edited.
* While the user types a pattern, the form shows a **live match count** ("Matches 12 transactions"), computed with exactly the engine's matching and the chosen account scope.
* From a transaction's edit row, **"Create rule…"** opens the Rules page with the pattern prefilled from the transaction's description (and the category and merchant name from the row).

---

## 7. User Interfaces

### 7.1 Web UI (default `http://127.0.0.1:8000`)

Pages are rendered on the server with Jinja2. Interactive actions use HTMX: a button sends a request and the server answers with an HTML **fragment** (from `web/templates/partials/`) that replaces part of the page. POST and DELETE routes therefore return fragments, not whole pages.

General behaviour:

* **Query parameters never cause a server error.** On the dashboard, a missing or out-of-range `year`/`month` falls back to the current month. On the transaction list, an out-of-range month is ignored (all months of the year are shown). An unknown or blank `account_id` means "all accounts".
* **Money** is formatted with `[money]` from the configuration, e.g. `−₹450.50`; positive amounts in the ledger are shown with a `+`.
* **Ollama status badge.** The sidebar shows whether Ollama is reachable. It re-checks every 10 seconds (and on click) through `GET /api/ollama-status`. Ordinary page renders reuse a result cached for 3 seconds so they never wait on Ollama.
* A **warning banner** appears on every page when `llm.host` is not local (§9.7).
* No external resources: every script, stylesheet and font is served from `/static/`.

| Page | Features |
|---|---|
| **Dashboard** (`/`) | Previous/next month navigation, and an account filter (kept when changing month). Cards for income, expenses and net savings, all excluding transfers. The **uncategorized count** for the month, linking to the transaction list filtered to those rows. Spending by category with each category's % of expenses (SVG chart and table). Income vs. expenses for the 6 calendar months ending with the **current** month (SVG chart). The top 10 merchants by spending. Definitions in §8. |
| **Upload** (`/upload`) | Account picker or new-account fields, file chooser (`.pdf`, `.csv`), optional PDF password, model picker, then the live job progress or the CSV mapping form (§3.1, §3.2). The model list and Ollama status come from Ollama. |
| **Imports** (`/imports`) | §3.6. |
| **Review** (`/imports/{id}`) | §3.5. |
| **Transactions** (`/transactions`) | Filters: year, month, account, category, free-text search (a substring of the description or merchant name), "uncategorized only", and transfers shown / hidden / only. 30 rows per page, newest first; the page links keep the filters. Each row shows a "manual" or "transfer" marker where relevant and can be: **edited in place** (category, with existing categories suggested; merchant name; transfer flag), which makes it manual (§6); **reset to rules**; or **deleted** after a confirmation. The edit row also has the "Create rule…" link. |
| **Accounts** (`/accounts`) | Create accounts (name, bank or credit card, optional institution); rename or change them in place; delete them. Deletion is refused while the account has any imports (delete those first), or rules scoped to it (deleting a scoped rule silently widening it to all accounts would recategorize other rows). Deleting an account also deletes its CSV mappings. The page also lists every saved CSV mapping with its columns, and each can be deleted; the next file with that header will then ask for a mapping again. |
| **Rules** (`/rules`) | The rule list (pattern, category, merchant, priority, account scope, transfer flag), the rule form with its live match count (§6), delete (which re-runs the engine), and "Re-apply all rules", which reports how many rows changed. |

### 7.2 Terminal UI (Textual)

Started with `python -m tui.app`. It reads and edits the same database through `core`. Uploading and reviewing statements are web-only, as are deleting transactions and "Reset to rules".

It has two screens. The selected month and account filter belong to the app, so both screens always show the same period and account.

* **Dashboard:** income, expenses and net savings for the selected month, the uncategorized count, and spending by category as text bars with percentages. These are the same figures the web dashboard shows for that month and account (§8). The trend chart and top merchants are web-only.
* **Transactions:** a table of the month's transactions (date, merchant name or description, category, manual/transfer flags, amount, source file), 50 per page.

| Key | Action |
|---|---|
| `d` / `t` / `q` | Dashboard / Transactions / Quit |
| `←` / `→` | Previous / next month (both screens) |
| `PgUp` / `PgDn` | Previous / next page (Transactions) |
| `/` | Search descriptions; an empty search clears it (Transactions) |
| `Esc` | Clear the search (Transactions) |
| `a` | Cycle the account filter: all accounts → each account by name → all (both screens) |
| `c` | Set the category of the selected row; it becomes manual (Transactions) |
| `x` | Toggle the transfer flag of the selected row; it becomes manual (Transactions) |
| `r` | Refresh (both screens) |

Two implementation constraints, both caused by how Textual routes keys:

* `d`, `t` and `q` are ordinary app bindings, **not** priority bindings. Otherwise they would be captured while the user types a search term or a category into the text prompt.
* `←`, `→`, `PgUp` and `PgDn` **are** priority bindings on the Transactions screen. Otherwise the focused table would consume them to move its cursor.

---

## 8. Analytics

All figures are for one calendar month, selected by `transaction_date LIKE 'YYYY-MM-%'`, optionally for one account. They **always exclude transfers** (`is_transfer = 1`). Sums are computed on integer minor units and converted for display only at the end.

* **Income** = the sum of positive amounts. **Expenses** = the absolute value of the sum of negative amounts. **Net savings** = income − expenses.
* **Category breakdown:** expenses per category, largest first, and each category's percentage of total expenses, rounded to one decimal place. Percentages add up to 100, give or take rounding. Income is not broken down.
* **Trend:** income and expenses for each of the last N calendar months (N = 6 on the dashboard), oldest first, computed with month arithmetic (not by subtracting 30-day blocks, which skips or repeats months). Months with no data show as zero.
* **Top merchants:** expenses grouped by the merchant name if set, otherwise by the description, largest first, top 10.
* **Uncategorized:** the number of non-transfer rows in the month whose category is `Uncategorized`. The transaction list's "uncategorized only" filter uses exactly the same definition, so the dashboard count and the filtered list always agree.

---

## 9. Privacy & Security Requirements

1. **The web server listens on `127.0.0.1` by default**, so only this machine can reach it. Another host can be configured; `run_web.py` then logs a warning. There is no login (§10), so exposing the server exposes the data.
2. **The browser makes no external requests.** All JavaScript and CSS are served locally; HTMX is vendored. A test fails if any template or stylesheet references an `http(s)://` asset.
3. **Original statement files are deleted** right after their text is read (§3.2). Only `file_name` and `file_sha256` are kept. The temporary directory is private (`0700`) and emptied on every server start.
4. **PDF passwords are never written** to the database, the logs or the disk (§3.2).
5. **Logs at INFO level or above never contain transaction descriptions, amounts or model output, and neither do the error messages stored in `statements.error`** (which are shown in the UI and kept in the database). For example, an invalid-output error reports only the number of validation errors. Raw model output is logged only at DEBUG level.
6. **Uploads are limited** to 20 MB and to `.pdf` and `.csv` files. The client's filename is never used as a path (§3.1).
7. **If `llm.host` is not local**, every page shows a persistent warning that statement text is leaving this machine, and `run_web.py` logs one at startup.

Encrypting the database file is out of scope; users should rely on their operating system's disk encryption.

---

## 10. Out of Scope

* OCR for scanned (image-only) PDFs.
* Multiple currencies and exchange rates. One database has one currency.
* LLM backends other than Ollama.
* Checking row dates against the statement period.
* Database encryption.
* Multiple users, logins or access control.
* Cloud sync or backup.
* Budgets and alerts.

---

## 11. Testing & Acceptance Criteria

Tests use Python's built-in `unittest` and run with:

```bash
python -m unittest discover tests
```

Rules for every test:

* **Never touch the real database.** Each test module sets `EXPENSE_TRACKER_DB` to a temporary file **before** importing anything from `core` or `web` (importing `web.app` initializes the database and starts the worker). Copy the guard block at the top of an existing test file.
* **Never call a real Ollama.** Tests that render `/upload` patch `web.app.list_available_models` and `web.app.is_ollama_available`; tests that run jobs patch `core.ingestion.extract_transactions` and `extract_statement_metadata`.
* Tests that need different settings patch `get_config` in the module under test, because the configuration is cached on first use.

| Suite | Covers |
|---|---|
| `test_schema_v2.py` | Schema creation, the v1 → v2 migration, ids and dedup, money conversion, the rules engine |
| `test_review_pipeline.py` | Jobs, chunking, the validator, staging, review edits, commit, discard |
| `test_accounts_csv_pdf.py` | Accounts, CSV mappings and parsing, password-protected PDFs (with a real encrypted PDF) |
| `test_ledger_editing.py` | Manual edits, reset to rules, deleting transactions and imports, list filters |
| `test_config_and_llm.py` | Configuration loading and validation, prompts, retries, timeouts, no model output in errors |
| `test_appendix_a_fixes.py` | Regression tests for the original bugs (path traversal, trend months, local assets, chunking, bad query params) |
| `test_ollama_status.py` | The Ollama status badge and its cache |
| `test_tui.py` | The terminal UI, driven with Textual's `Pilot` |

The acceptance criteria each area was built against:

**Schema, config, migrations, dedup**
- A fresh database is created at the latest `user_version`. A v1 database migrates correctly (Appendix B).
- Re-committing the same rows inserts 0 rows. Two identical same-day rows in one statement give 2 rows. Overlapping statements insert the overlapping rows once.
- Summing 0.10 three times gives exactly 30 minor units.

**Extraction, validator, staging, jobs**
- Chunks never split a line and never overlap. Every chunk is at most `CHUNK_SIZE`, except a chunk holding a single line longer than that.
- Each validator code in §4 has a passing and a failing test case.
- A mapped CSV is imported with no call to Ollama. A password-protected PDF moves through `needs_password` → `review`.
- The temporary file is gone after extraction, on both the success and failure paths.
- An interrupted job is marked `failed` on restart.

**Rules & analytics**
- Rule priority, manual edits winning, delete-reverts and `mark_transfer` each have a test.
- Transfers are excluded from income, expenses and the breakdown. The trend across the Jan→Mar and Dec→Jan boundaries gives consecutive, distinct months.

**Web UI**
- Route tests for upload → progress → review → commit → dashboard figures, deleting an import, inline category editing, and `?month=13` not causing a server error.
- No `http(s)://` asset URLs in templates.

**TUI**
- Every binding in §7.2 has an action. A `Pilot` test pages through more than 50 rows and changes a category.

---

## Appendix A: Implementation History & Known Limitations

### A.1 History

Version 2 of this spec was written as a review of the first version of the code. That review found the gaps below, in priority order (🔴 bug or security issue, 🟠 missing feature, 🟡 cleanup). All of them are now fixed (✅); the list is kept so the reasons behind the current design stay on record.

1. ✅ 🔴 **Path traversal on upload.** Uploads were written to `uploads/<client filename>` without sanitizing it. Fixed per §3.1 and §9.6.
2. ✅ 🔴 **The trend skipped or repeated months.** It subtracted `i*30` days (Mar 1 − 30 days = Jan 30, so February went missing). Now uses calendar-month arithmetic (§8).
3. ✅ 🔴 **HTMX was loaded from a CDN**, breaking the local-only principle and failing offline. It is vendored now, and a Google Fonts import was removed (§9.2).
4. ✅ 🔴 **Chunks split rows and overlapped**, producing truncated near-duplicates. Fixed per §3.3.
5. ✅ 🔴 **Genuine same-day repeats were lost**, because the id had no ordinal and no account. Fixed per §5, with a migration (Appendix B).
6. ✅ 🔴 **Ingestion blocked the web server**, running synchronously inside the request. Now a background job (§3).
7. ✅ 🔴 **Invalid query parameters caused a server error** (`/?month=13`). Now validated (§7.1).
8. ✅ 🟠 **Schema v2**: accounts, statements, staged rows, CSV mappings, integer money, transfer flags, category sources and rule priority/scope (§2), with the account picker, the Accounts page, rule options and account filters that use them.
9. ✅ 🟠 **No validator** beyond the schema. Added §4, including `not_in_source` and reconciliation, with LLM-extracted statement metadata (§3.3).
10. ✅ 🟠 **No review step**: rows went straight into the ledger. Added staging and the review screen (§3.4, §3.5).
11. ✅ 🟠 **Every CSV went through the LLM.** Added CSV mappings and a deterministic parser (§3.2).
12. ✅ 🟠 **Password-protected PDFs were not supported.** Added the `needs_password` flow (§3.2).
13. ✅ 🟠 **Rules engine semantics.** Rules were applied in alphabetical order, overwrote everything and never reverted. Rewritten per §6.
14. ✅ 🟠 **No manual editing or deletion of transactions.** Added to the web UI, with "Create rule…", plus the uncategorized and transfer filters (§7.1). The TUI can set categories and transfer flags (§7.2).
15. ✅ 🟠 **No Imports page and no way to undo an import.** Added (§3.6).
16. ✅ 🟠 **Transfers counted as income and expense.** Analytics now exclude them (§8).
17. ✅ 🟠 **The uncategorized count was global** and didn't link anywhere. It is now per month and links to the filtered list (§7.1, §8).
18. ✅ 🟠 **TUI gaps**: only the first 50 rows were reachable, the dashboard was fixed to the current month, there was no search, editing or account filter, and the arrow keys never worked (the focused table swallowed them). All fixed (§7.2).
19. ✅ 🟠 **Uploaded statements were kept forever** in `uploads/`. They are now deleted after extraction (§3.2). Any files left in `uploads/` by the old version can be deleted by hand.
20. ✅ 🟡 **Settings were scattered** (model name, currency symbol hardcoded in several places). Moved to `config.toml` (§1.4).
21. ✅ 🟡 **Inconsistent database connections** (some without WAL or a busy timeout). All go through `database.connect()` now (§2.1).
22. ✅ 🟡 **The JSON schema was duplicated in the prompt**, and the prompt didn't explain credit-card signs. Fixed (§3.3). Parse errors also used to embed model output in logs and `statements.error`, which is now forbidden (§9.5).
23. ✅ 🟡 **No per-chunk timeout or retry.** Added (§3.3).
24. ✅ 🟡 **Auto-reload was always on** in `run_web.py`. It is now the `--dev` flag, and host and port come from the configuration.
25. ✅ 🟡 **Tests read the real database** and only covered the Ollama badge. Fixed per §11.

### A.2 Known limitations

These are current, deliberate or accepted behaviours, not bugs to fix without a spec change:

* **Differently-worded duplicates** are not detected (§5).
* **The dashboard trend chart always ends with the current month**, not the month selected on the dashboard. The cards, breakdown, uncategorized count and top merchants follow the selected month.
* **Rules cannot be renamed.** A rule is edited by saving its pattern again with new settings; changing the pattern means adding a new rule and deleting the old one.
* **Review cannot edit a row's value date**, only the transaction date, description, amount and transfer flag. An invalid value date is cleared automatically (§4).
* **The TUI cannot delete transactions or reset them to rules**; use the web UI.
* **Transaction-list search uses SQL `LIKE`**, so `%` and `_` typed into the search box act as wildcards (unlike rule patterns, §6).

---

## Appendix B: Migration v1 → v2

✅ Implemented in `core/migrations.py` and run automatically by `database.init_db()` on startup. Tests are in `tests/test_schema_v2.py`.

The first version of the app stored everything in two tables, with `PRAGMA user_version = 0`:

* `transactions`, with a floating-point `amount`, a `source_file` name, an `injected_at` timestamp, and `id = sha256(date|desc|amount:.2f)`;
* `category_rules`.

Steps:

1. Back up the file to `expenses.db.v1.bak` (next to the database) before changing anything.
2. Create the new tables. Insert an account named `Default` (`kind='bank'`), which the user can rename later.
3. Add `priority = 0`, `mark_transfer = 0` and `account_id = NULL` to the existing rules. (This happens before the transactions are rebuilt, because the new `transactions.rule_id` references `category_rules`.)
4. For each distinct `source_file`, in the order the files were first imported, insert a `statements` row with `status='committed'`, `extractor='llm'`, `file_sha256 = 'migrated:' || source_file`, empty periods and balances, and `created_at`/`committed_at` set to the file's first `injected_at`.
5. Rebuild `transactions`:
   * `amount_minor = to_minor(amount)` (rounding half up);
   * `account_id` = the Default account, `statement_id` from step 4;
   * the description whitespace-normalized;
   * `category_source = 'rule'` when the category is not `Uncategorized`, else `'default'` (v1 had no manual edits, so every real category came from a rule);
   * `id` recomputed per §5, with ordinals assigned in `injected_at` order within each statement.
6. Run the rules engine (§6), so `rule_id` and `category_source` are exact.
7. Check foreign keys, then set `user_version = 2`.

Steps 2–7 run in one transaction.

**Decisions made during implementation:**

* **Zero-amount v1 rows are dropped.** v2 forbids zero amounts (`CHECK (amount_minor <> 0)`). The dropped rows survive in the backup and are counted in the `MigrationReport`.
* **Rows that collapse to the same v2 id are merged.** Two v1 rows can differ only in whitespace, which §5 normalizes away. Statements and rows are processed in `injected_at` order, so the earliest import keeps the row.
* **Orphaned categories revert.** v1 never reverted categories when a rule was deleted. Step 6 therefore resets rows whose rule no longer exists to `Uncategorized`, following §6. Their old values remain in the backup.
* **Backups are never overwritten.** If `expenses.db.v1.bak` already exists (for example after a failed attempt), a timestamped name is used instead.
* **A failure leaves the database unchanged.** The whole migration rolls back and the file stays at v1.
* **Fresh installs skip the migration.** An empty file gets the v2 schema directly.
* **Unknown versions are refused.** Any `user_version` other than 0 or 2 stops startup with an error.
