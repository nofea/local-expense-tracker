# 💰 Privacy-First Local Expense Tracker

A fully local expense tracker. It reads your bank and credit-card statements (PDF or CSV) with an LLM running on your own machine through **Ollama**, lets you review every extracted row before saving it, and shows your spending in a web dashboard and a keyboard-driven terminal UI. Your data stays on your machine.

The full specification — how every part works and why — is in [`app_spec.md`](app_spec.md).

---

## Features

- **Local LLM parsing** — Upload a PDF or CSV statement and a local Ollama model extracts the transactions
- **Review before saving** — Extracted rows are checked (bad dates, zero amounts, descriptions not found in the statement, rows already imported, balances that don't add up) and nothing reaches your ledger until you commit it
- **CSV mappings** — Describe a bank's CSV columns once; later statements with the same header are read without the LLM, so Ollama doesn't even need to be running
- **Password-protected PDFs** — Enter the password at upload or when asked; it is never saved
- **Accounts** — File statements under bank accounts and cards, and filter the dashboard and transactions by account
- **Families** — Group accounts into families and members, with a primary holder and any joint holders per account (add-on cards included). See the whole family's income and spending, a per-member table, or one member's figures, optionally including their joint accounts
- **Deduplication** — Overlapping statements don't double-count, re-uploading a file is refused, and genuine repeats (two identical coffees on one day) are kept
- **Undo an import** — Delete an imported statement and all of its transactions in one step
- **Category rules** — Keyword rules with priorities, per-account scope and a live match count; your manual edits always win over rules
- **Transfers** — Flag card payments and moves between your own accounts so they don't count as income or spending
- **Web dashboard** — Monthly income, expenses and savings, spending by category, a 6-month trend and top merchants
- **Terminal UI** — Browse months, search, set categories and flag transfers from the keyboard
- **100% local** — No cloud, no CDNs, no telemetry. SQLite + Ollama only. Uploaded statement files are deleted as soon as their text is read

---

## Prerequisites

- **Python 3.11+**
- **Ollama** installed and running (`ollama serve`), with at least one model pulled (e.g. `ollama pull llama3.1`). Needed for PDFs and for CSVs you haven't mapped; a CSV with a saved mapping is read without it.

---

## Setup

```bash
# From the repository root
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: change settings (Ollama host and default model, currency, database path, web host/port)
cp config.example.toml config.toml
```

Every setting is optional; the defaults are listed in `config.example.toml`. The defaults use Indian rupees (`₹`, 2 decimal places). If you use another currency, set `[money]` **before** importing anything: `minor_units` must not change once you have data.

---

## Running

All commands run from the repository root.

### Web UI (FastAPI + HTMX)

```bash
python run_web.py          # http://127.0.0.1:8000 by default
python run_web.py --dev    # auto-reload when the code changes
```

The web server is the only part of the app that imports statements.

### Terminal UI (Textual)

```bash
python -m tui.app
```

| Key | Action |
|---|---|
| `d` / `t` / `q` | Dashboard / Transactions / Quit |
| `←` / `→` | Previous / next month |
| `PgUp` / `PgDn` | Previous / next page |
| `/` | Search (`Esc` clears it) |
| `a` | Cycle the account filter |
| `f` | Cycle the family filter (resets the member filter) |
| `m` | Cycle the member filter |
| `j` | Include / exclude the selected member's joint accounts |
| `c` | Set the category of the selected row |
| `x` | Toggle the transfer flag of the selected row |
| `r` | Refresh |

The terminal UI and the web UI can run at the same time; they share the same database.

---

## Usage

1. **Start Ollama** — make sure `ollama serve` is running (not needed for CSVs you have already mapped).
2. **Upload a statement** — on the Upload page, pick or create an account, choose the model, and drop in a PDF or CSV. The first CSV of each layout asks you to map its columns (or to use the LLM instead).
3. **Review and commit** — when extraction finishes, open the review screen. Fix or drop any flagged rows, enter the opening and closing balances if they are missing, and commit.
4. **Create rules** — on the Category Rules page, map keywords to categories (e.g. `AMZN` → `Shopping`). Mark rules for card payments or self-transfers as transfers.
5. **Fix individual rows** — on the Transactions page, edit a row's category, merchant name or transfer flag, or delete it.
6. **View insights** — the Dashboard shows monthly summaries, category breakdowns, trends and top merchants.
7. **Set up your family (optional)** — on the Families page, create a family, add its members, and pick each account's primary holder and joint holders. Each account's figures count under its primary holder, so the dashboard's per-member table adds up to the family total. Money sent between two members counts as one member's expense and the other's income; don't mark it as a transfer.

Made a mistake? The Imports page can delete a whole import, and you can then upload the file again.

---

## Tests

```bash
python -m unittest discover tests
```

The tests use a temporary database and a mocked Ollama; they never touch `data/expenses.db` or a real model.

---

## Project Structure

```
.
├── core/                # All logic and data access
│   ├── config.py        # Settings from config.toml
│   ├── database.py      # SQLite connections, ledger queries, accounts, rules engine
│   ├── migrations.py    # Schema creation and upgrades
│   ├── models.py        # Pydantic models, money conversion, transaction ids
│   ├── jobs.py          # Background import worker
│   ├── ingestion.py     # PDF/CSV text extraction and chunking for the LLM
│   ├── ollama_client.py # Ollama calls, prompts, retries and timeouts
│   ├── csv_import.py    # CSV column mappings (no LLM)
│   ├── staging.py       # Review, commit, discard and delete of imports
│   ├── validator.py     # Checks on extracted rows
│   ├── categorizer.py   # Rule helpers
│   └── analytics.py     # Dashboard figures
├── web/                 # FastAPI web UI
│   ├── app.py           # Routes
│   ├── static/          # CSS, chart JS, vendored HTMX
│   └── templates/       # Jinja2 pages and HTMX fragments
├── tui/                 # Textual terminal UI
│   ├── app.py           # Main app
│   ├── screens/         # Dashboard, Transactions, prompt
│   └── style.tcss       # Terminal styling
├── tests/               # unittest suites
├── data/                # SQLite database (auto-created, git-ignored)
├── app_spec.md          # Full specification
├── config.example.toml  # Every setting, documented
├── run_web.py           # Web UI entry point
└── requirements.txt     # Dependencies
```

If you used an earlier version, any files left in `uploads/` can be deleted; statements are no longer kept. An older database is upgraded automatically on first start, and a backup of it is saved next to it as `expenses.db.v1.bak`.

---

## Tech Stack

| Layer        | Technology                       |
|--------------|----------------------------------|
| Database     | SQLite (WAL mode)                |
| LLM          | Ollama (local, any model)        |
| Web Backend  | FastAPI + Jinja2                 |
| Web Frontend | HTMX + vanilla JS (SVG charts) + CSS |
| Terminal UI  | Python Textual                   |
| PDF Parsing  | pdfplumber                       |
| Validation   | Pydantic v2                      |
