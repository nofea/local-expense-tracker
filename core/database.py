"""SQLite database layer for the expense tracker.

All functions accept an optional *db_path* so callers can override the
default location (``data/expenses.db`` relative to the project root).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from core.models import (
    CategoryRule,
    TransactionRecord,
    compute_transaction_id,
)

# ---------------------------------------------------------------------------
# Default DB path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "expenses.db"


def _get_conn(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db(db_path: Optional[str | Path] = None) -> None:
    """Create tables and indices if they don't exist."""
    conn = _get_conn(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                transaction_date TEXT NOT NULL,
                value_date TEXT,
                raw_description TEXT NOT NULL,
                cleaned_merchant TEXT,
                amount REAL NOT NULL,
                category TEXT DEFAULT 'Uncategorized',
                source_file TEXT NOT NULL,
                injected_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_trans_date
                ON transactions(transaction_date);
            CREATE INDEX IF NOT EXISTS idx_trans_category
                ON transactions(category);

            CREATE TABLE IF NOT EXISTS category_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                cleaned_merchant TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transaction CRUD
# ---------------------------------------------------------------------------

def insert_transactions(
    rows: list[dict],
    source_file: str,
    db_path: Optional[str | Path] = None,
) -> tuple[int, int]:
    """Insert transactions using INSERT OR IGNORE for deduplication.

    Returns ``(inserted_count, skipped_count)``.
    """
    conn = _get_conn(db_path)
    inserted = 0
    try:
        for row in rows:
            tx_id = compute_transaction_id(
                row["transaction_date"], row["raw_description"], row["amount"]
            )
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO transactions
                    (id, transaction_date, value_date, raw_description,
                     cleaned_merchant, amount, category, source_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_id,
                    row["transaction_date"],
                    row.get("value_date"),
                    row["raw_description"],
                    row.get("cleaned_merchant"),
                    row["amount"],
                    row.get("category", "Uncategorized"),
                    source_file,
                ),
            )
            inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted, len(rows) - inserted


def get_transactions(
    db_path: Optional[str | Path] = None,
    *,
    year: Optional[int] = None,
    month: Optional[int] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TransactionRecord]:
    """Fetch transactions with optional filters, newest first."""
    conn = _get_conn(db_path)
    clauses: list[str] = []
    params: list[object] = []

    if year and month:
        clauses.append(
            "transaction_date LIKE ?"
        )
        params.append(f"{year:04d}-{month:02d}-%")
    elif year:
        clauses.append("transaction_date LIKE ?")
        params.append(f"{year:04d}-%")

    if category:
        clauses.append("category = ?")
        params.append(category)

    if search:
        clauses.append("(raw_description LIKE ? OR cleaned_merchant LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT * FROM transactions{where} "
        f"ORDER BY transaction_date DESC, injected_at DESC "
        f"LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    try:
        rows = conn.execute(sql, params).fetchall()
        return [TransactionRecord(**dict(r)) for r in rows]
    finally:
        conn.close()


def get_transaction_count(
    db_path: Optional[str | Path] = None,
    *,
    year: Optional[int] = None,
    month: Optional[int] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
) -> int:
    """Count transactions with optional filters."""
    conn = _get_conn(db_path)
    clauses: list[str] = []
    params: list[object] = []

    if year and month:
        clauses.append("transaction_date LIKE ?")
        params.append(f"{year:04d}-{month:02d}-%")
    elif year:
        clauses.append("transaction_date LIKE ?")
        params.append(f"{year:04d}-%")

    if category:
        clauses.append("category = ?")
        params.append(category)

    if search:
        clauses.append("(raw_description LIKE ? OR cleaned_merchant LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT COUNT(*) FROM transactions{where}"

    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def delete_transaction(
    tx_id: str, db_path: Optional[str | Path] = None
) -> bool:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_categories(db_path: Optional[str | Path] = None) -> list[str]:
    """Return sorted list of distinct categories in the database."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT category FROM transactions ORDER BY category"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Category rules CRUD
# ---------------------------------------------------------------------------

def upsert_rule(rule: CategoryRule, db_path: Optional[str | Path] = None) -> int:
    """Insert or update a categorization rule. Returns the rule id."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO category_rules (pattern, category, cleaned_merchant)
            VALUES (?, ?, ?)
            ON CONFLICT(pattern) DO UPDATE SET
                category = excluded.category,
                cleaned_merchant = excluded.cleaned_merchant
            """,
            (rule.pattern, rule.category, rule.cleaned_merchant),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def get_rules(db_path: Optional[str | Path] = None) -> list[CategoryRule]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, pattern, category, cleaned_merchant FROM category_rules ORDER BY pattern"
        ).fetchall()
        return [CategoryRule(**dict(r)) for r in rows]
    finally:
        conn.close()


def delete_rule(rule_id: int, db_path: Optional[str | Path] = None) -> bool:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM category_rules WHERE id = ?", (rule_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def apply_rules(db_path: Optional[str | Path] = None) -> int:
    """Apply all category rules to transactions. Returns number of rows updated."""
    conn = _get_conn(db_path)
    rules = get_rules(db_path)
    total_updated = 0
    try:
        for rule in rules:
            pattern = f"%{rule.pattern}%"
            cur = conn.execute(
                """
                UPDATE transactions
                SET category = ?,
                    cleaned_merchant = COALESCE(?, cleaned_merchant)
                WHERE raw_description LIKE ? COLLATE NOCASE
                  AND (category = 'Uncategorized' OR category != ?)
                """,
                (rule.category, rule.cleaned_merchant, pattern, rule.category),
            )
            total_updated += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return total_updated
