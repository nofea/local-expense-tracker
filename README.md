# 💰 Privacy-First Local Expense Tracker

A fully local expense tracking application with LLM-powered bank statement parsing via **Ollama**, a premium web dashboard, and a keyboard-driven terminal UI. All data stays on your machine.

---

## Features

- **LLM-Powered Parsing** — Upload PDF/CSV bank statements and let a local LLM extract transactions automatically
- **Model Selection** — Choose which Ollama model to use for each processing job
- **Deduplication** — SHA-256 hashing prevents double-counting from overlapping statements
- **Category Rules** — Create keyword-based rules to auto-categorize transactions
- **Web Dashboard** — Beautiful dark-mode UI with charts, filters, and pagination
- **Terminal UI** — Keyboard-driven interface via Python Textual
- **100% Local** — No cloud, no APIs, no telemetry. SQLite + Ollama only.

---

## Prerequisites

- **Python 3.11+**
- **Ollama** installed and running (`ollama serve`)
- At least one model pulled (e.g. `ollama pull llama3.1`)

---

## Setup

```bash
# Clone / navigate to the project
cd expense_app

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Running

### Web UI (FastAPI + HTMX)

```bash
python run_web.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

### Terminal UI (Textual)

```bash
python -m tui.app
```

Use `d` for Dashboard, `t` for Transactions, `q` to Quit.

---

## Usage

1. **Start Ollama** — Make sure `ollama serve` is running
2. **Upload a statement** — Go to the Upload page, select your model, drop a PDF/CSV
3. **Review transactions** — Check the Transactions page for extracted data
4. **Create rules** — Use the Rules page to map keywords to categories (e.g. `AMZN` → `Shopping`)
5. **View insights** — The Dashboard shows monthly summaries, category breakdowns, and trends

---

## Project Structure

```
expense_app/
├── core/               # Shared backend logic
│   ├── database.py     # SQLite layer
│   ├── models.py       # Pydantic models
│   ├── ingestion.py    # PDF/CSV → LLM → DB pipeline
│   ├── ollama_client.py# Ollama API integration
│   ├── categorizer.py  # Category rules engine
│   └── analytics.py    # Aggregation queries
├── web/                # FastAPI web UI
│   ├── app.py          # Routes
│   ├── static/         # CSS + JS
│   └── templates/      # Jinja2 + HTMX
├── tui/                # Textual terminal UI
│   ├── app.py          # Main app
│   ├── screens/        # Dashboard + Transactions
│   └── style.tcss      # Terminal styling
├── data/               # SQLite database (auto-created)
├── uploads/            # Uploaded statements
├── run_web.py          # Web UI entry point
└── requirements.txt    # Dependencies
```

---

## Tech Stack

| Layer       | Technology                    |
|-------------|-------------------------------|
| Database    | SQLite (WAL mode)             |
| LLM         | Ollama (local, any model)     |
| Web Backend | FastAPI + Jinja2              |
| Web Frontend| HTMX + Vanilla JS + CSS      |
| Terminal UI | Python Textual                |
| PDF Parsing | pdfplumber                    |
| Validation  | Pydantic v2                   |
