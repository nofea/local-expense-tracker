# Technical Specification: Privacy-First Local Expense Tracker

## 1. System Architecture & Tech Stack

The application is split into decoupled modules communicating via a shared local SQLite database.

* **Database:** SQLite (`expenses.db`).
* **Ingestion & WebUI Engine:** Python 3.11+ (leveraging native libraries, local LLM clients, and a lightweight web framework).
* **TUI / Core Aggregator Engine:** C++17/20 (or Python alternative) interacting directly with the shared SQLite DB.

```
[Unstructured PDFs/CSVs] ➔ [Python Text Extractor] ➔ [Local LLM API (Structured JSON)]
                                                                 │
                                                                 ▼
[Local WebUI (Python)]  ◄──── [Shared SQLite DB] ◄──── [Deterministic Validator]
[Terminal TUI (C++)]    ◄──── [  `expenses.db` ]

```

---

## 2. Database Schema (`expenses.db`)

The database must enforce strict normalization and prevent data duplication from overlapping statement uploads.

```sql
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,                  -- Deterministic SHA-256 Hash
    transaction_date TEXT NOT NULL,       -- ISO 8601 String (YYYY-MM-DD)
    value_date TEXT,                      -- ISO 8601 String (YYYY-MM-DD)
    raw_description TEXT NOT NULL,        -- Unmodified text from statement
    cleaned_merchant TEXT,                -- Normalized name for rules matching
    amount REAL NOT NULL,                 -- Positive float for income, negative for expense
    category TEXT DEFAULT 'Uncategorized',-- Budget category
    source_file TEXT NOT NULL,            -- Filename of the source statement
    injected_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trans_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_trans_category ON transactions(category);

```

---

## 3. Ingestion Pipeline (LLM-Dependent Parser)

### Step 1: Text Extraction Buffer

A deterministic Python script reads target documents (`.pdf` via `pdfplumber` or `.csv` via native `csv`) and extracts raw text string buffers.

### Step 2: Local LLM Extraction Loop

The raw text buffer is chunked and sent to a local LLM endpoint (e.g., Ollama, llama.cpp, or vLLM running locally) utilizing a strict **Structured Output (JSON Mode)** instruction system prompt.

#### System Prompt for the Local LLM:

```text
You are a deterministic financial data extraction engine. Analyze the provided raw text from a bank or credit card statement. Extract every transaction without omitting any.
You must output a valid JSON object containing an array of transactions matching this exact JSON schema:

{
  "type": "object",
  "properties": {
    "transactions": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "transaction_date": {"type": "string", "description": "YYYY-MM-DD format"},
          "value_date": {"type": "string", "description": "YYYY-MM-DD format if available, else null"},
          "raw_description": {"type": "string"},
          "amount": {"type": "number", "description": "Negative number for charges/debits, positive for payments/credits/income"}
        },
        "required": ["transaction_date", "raw_description", "amount"]
      }
    }
  }
}
Do not include markdown formatting, preambles, or explanations. Return ONLY the raw JSON block.

```

### Step 3: Deterministic Ingestion & Deduplication

The Python ingestion engine receives the JSON array, computes a SHA-256 hash for each transaction row (`hash(transaction_date + raw_description + amount)`), and executes an `INSERT OR IGNORE` query against the SQLite database to natively handle deduplication.

---

## 4. User Interfaces

### Module A: Lightweight WebUI (Python)

* **Framework:** FastAPI + HTMX (or Streamlit) hosted purely on `localhost:8000`.
* **Features:**
* File drop zone to upload new PDF/CSV statements directly to the ingestion pipeline.
* Monthly dashboard view displaying Total Income, Total Outflow, and Net Savings.
* Dynamic breakdown of expenses by category via interactive data tables or simple SVG charts.
* A categorization manual rules editor (e.g., text mapping: `AMZN` -> `Shopping`).



### Module B: Terminal User Interface / TUI (C++)

* **Framework:** Built using a standard terminal UI paradigm (e.g., `FTXUI` for C++ or `Textual` for Python if keeping single-language UI bindings).
* **Features:**
* Keyboard-driven navigation optimized for performance and speed.
* Real-time aggregation queries executed natively against the SQLite database.
* Command-line view of current month spending metrics and a scrollable, paginated transaction log.



---

## 5. Execution & Implementation Roadmap

Instruct your local LLM to implement this code base incrementally in the following exact phases:

1. **Database & Core Validation Setup:** Phase 1.
Implement the SQLite database initialization code and write the deterministic deduplication logic. Verify database read/write speeds.


2. **LLM Integration & Parser Bridge:** Phase 2.
Write the text extraction module. Construct the local LLM API integration layer (e.g., calling `http://localhost:11434/api/chat`), parse the incoming JSON response, and link it directly to the database writer.


3. **Reporting & Analytics Logic:** Phase 3.
Write the core analytical queries that aggregate data monthly, calculate category percentages, and flag uncategorized items.


4. **Local WebUI Implementation:** Phase 4.
Construct the lightweight web dashboard containing the file upload functionality and report visualizations.


5. **Keyboard-Driven TUI Build:** Phase 5.
Build the terminal user interface module, linking keyboard shortcuts directly to the compiled monthly data views.
