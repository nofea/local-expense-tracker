"""SQLite database layer for the expense tracker (schema v2, app_spec.md §2).

All functions accept an optional *db_path* so callers can override the
default location (``storage.db_path`` in config.toml, default
``data/expenses.db``, or the ``EXPENSE_TRACKER_DB`` environment variable).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from core import migrations
from core.config import get_config
from core.models import (
    CategoryRule,
    TransactionRecord,
    assign_ordinals,
    compute_transaction_id,
    normalize_description,
    to_minor,
)

# ---------------------------------------------------------------------------
# Default DB path
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = get_config().db_path


def connect(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Open a connection with the pragmas every caller must use (§2)."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db(db_path: Optional[str | Path] = None) -> Optional[migrations.MigrationReport]:
    """Create or migrate the schema to the current version.

    Returns the migration report when a v1 database was upgraded.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = connect(path)
    try:
        return migrations.migrate(conn, path, _run_rules_engine)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def get_or_create_account(
    name: str = migrations.DEFAULT_ACCOUNT_NAME,
    kind: str = "bank",
    db_path: Optional[str | Path] = None,
) -> int:
    """Return the id of the account called *name*, creating it if needed."""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (name, kind) VALUES (?, ?)", (name, kind)
        )
        conn.commit()
        return conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transaction CRUD
# ---------------------------------------------------------------------------

def import_statement(
    rows: list[dict],
    *,
    account_id: int,
    file_name: str,
    file_sha256: str,
    extractor: str = "llm",
    model: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> tuple[int, int, int, int]:
    """Record a committed statement and insert its rows, in one transaction.

    This writes straight to the ledger and skips review, so the upload flow
    must not use it (it goes through ``core.staging``).  It exists for tests
    and fixtures that need ledger data.

    *rows* must be the complete statement in document order (ordinals in the
    §5 id are counted within it).  Each row has ``transaction_date``,
    ``raw_description`` and either ``amount`` (major units) or
    ``amount_minor``, plus optional ``value_date``.  The rules engine runs
    before committing.

    Returns ``(statement_id, inserted, duplicates, invalid)``, where
    *invalid* counts zero-amount rows, which v2 rejects.
    """
    conn = connect(db_path)
    try:
        statement_id = conn.execute(
            """
            INSERT INTO statements
                (account_id, file_name, file_sha256, extractor, model,
                 status, committed_at)
            VALUES (?, ?, ?, ?, ?, 'committed', CURRENT_TIMESTAMP)
            """,
            (account_id, file_name, file_sha256, extractor, model),
        ).lastrowid
        inserted, duplicates, invalid = _insert_rows(conn, rows, account_id, statement_id)
        _run_rules_engine(conn)
        conn.commit()
        return statement_id, inserted, duplicates, invalid
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_rows(
    conn: sqlite3.Connection, rows: list[dict], account_id: int, statement_id: int
) -> tuple[int, int, int]:
    prepared = []
    invalid = 0
    for row in rows:
        minor = row["amount_minor"] if "amount_minor" in row else to_minor(row["amount"])
        if minor == 0:
            invalid += 1
            continue
        prepared.append((row, normalize_description(row["raw_description"]), minor))

    ordinals = assign_ordinals([(r["transaction_date"], d, m) for r, d, m in prepared])
    inserted = 0
    for (row, desc, minor), ordinal in zip(prepared, ordinals):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO transactions
                (id, account_id, statement_id, transaction_date, value_date,
                 raw_description, amount_minor, is_transfer)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                compute_transaction_id(account_id, row["transaction_date"], desc, minor, ordinal),
                account_id,
                statement_id,
                row["transaction_date"],
                row.get("value_date"),
                desc,
                minor,
                int(bool(row.get("is_transfer", False))),
            ),
        )
        inserted += cur.rowcount
    return inserted, len(prepared) - inserted, invalid


def _filters(
    year: Optional[int],
    month: Optional[int],
    category: Optional[str],
    search: Optional[str],
    uncategorized: bool = False,
    transfers: str = "show",
) -> tuple[str, list[object]]:
    """WHERE clause for the transaction list.

    *uncategorized* matches the dashboard's definition (§8): non-transfer
    rows still in ``Uncategorized``.  *transfers* is ``show``, ``hide`` or
    ``only``.
    """
    clauses: list[str] = []
    params: list[object] = []

    if year and month:
        clauses.append("t.transaction_date LIKE ?")
        params.append(f"{year:04d}-{month:02d}-%")
    elif year:
        clauses.append("t.transaction_date LIKE ?")
        params.append(f"{year:04d}-%")

    if category:
        clauses.append("t.category = ?")
        params.append(category)

    if search:
        clauses.append("(t.raw_description LIKE ? OR t.cleaned_merchant LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    if uncategorized:
        clauses.append("t.category = 'Uncategorized' AND t.is_transfer = 0")

    if transfers == "hide":
        clauses.append("t.is_transfer = 0")
    elif transfers == "only":
        clauses.append("t.is_transfer = 1")

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def get_transactions(
    db_path: Optional[str | Path] = None,
    *,
    year: Optional[int] = None,
    month: Optional[int] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    uncategorized: bool = False,
    transfers: str = "show",
    limit: int = 50,
    offset: int = 0,
) -> list[TransactionRecord]:
    """Fetch transactions with optional filters, newest first."""
    conn = connect(db_path)
    where, params = _filters(year, month, category, search, uncategorized, transfers)
    sql = (
        "SELECT t.*, s.file_name AS source_file "
        "FROM transactions t JOIN statements s ON s.id = t.statement_id"
        f"{where} "
        "ORDER BY t.transaction_date DESC, t.created_at DESC, t.id "
        "LIMIT ? OFFSET ?"
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
    uncategorized: bool = False,
    transfers: str = "show",
) -> int:
    """Count transactions with optional filters."""
    conn = connect(db_path)
    where, params = _filters(year, month, category, search, uncategorized, transfers)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM transactions t{where}", params).fetchone()[0]
    finally:
        conn.close()


def get_transaction(
    tx_id: str, db_path: Optional[str | Path] = None
) -> Optional[TransactionRecord]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT t.*, s.file_name AS source_file "
            "FROM transactions t JOIN statements s ON s.id = t.statement_id WHERE t.id = ?",
            (tx_id,),
        ).fetchone()
        return TransactionRecord(**dict(row)) if row else None
    finally:
        conn.close()


def set_manual_category(
    tx_id: str,
    *,
    category: str,
    cleaned_merchant: Optional[str],
    is_transfer: bool,
    db_path: Optional[str | Path] = None,
) -> bool:
    """Apply a user edit (§6): the row becomes ``manual`` and rules never
    touch it again until :func:`reset_to_rules`.  Returns False if the
    transaction does not exist."""
    category = " ".join(category.split()) or "Uncategorized"
    merchant = " ".join((cleaned_merchant or "").split()) or None
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE transactions
            SET category = ?, cleaned_merchant = ?, is_transfer = ?,
                category_source = 'manual', rule_id = NULL
            WHERE id = ?
            """,
            (category, merchant, int(is_transfer), tx_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reset_to_rules(tx_id: str, db_path: Optional[str | Path] = None) -> bool:
    """Undo manual edits on one row and let the rules engine decide again.

    The transfer flag is cleared too (a ``mark_transfer`` rule sets it
    again), since there is no record of whether it came from review or from
    the manual edit.
    """
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE transactions
            SET category = 'Uncategorized', cleaned_merchant = NULL, is_transfer = 0,
                category_source = 'default', rule_id = NULL
            WHERE id = ?
            """,
            (tx_id,),
        )
        _run_rules_engine(conn)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_transaction(
    tx_id: str, db_path: Optional[str | Path] = None
) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_categories(db_path: Optional[str | Path] = None) -> list[str]:
    """Return sorted list of distinct categories in the database."""
    conn = connect(db_path)
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
    """Insert or update a categorization rule (keyed on pattern). Returns its id."""
    conn = connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO category_rules
                (pattern, category, cleaned_merchant, mark_transfer, priority, account_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pattern) DO UPDATE SET
                category = excluded.category,
                cleaned_merchant = excluded.cleaned_merchant,
                mark_transfer = excluded.mark_transfer,
                priority = excluded.priority,
                account_id = excluded.account_id
            """,
            (
                rule.pattern,
                rule.category,
                rule.cleaned_merchant,
                int(rule.mark_transfer),
                rule.priority,
                rule.account_id,
            ),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM category_rules WHERE pattern = ?", (rule.pattern,)
        ).fetchone()[0]
    finally:
        conn.close()


def get_rules(db_path: Optional[str | Path] = None) -> list[CategoryRule]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, pattern, category, cleaned_merchant, mark_transfer, priority, account_id
            FROM category_rules ORDER BY pattern
            """
        ).fetchall()
        return [CategoryRule(**dict(r)) for r in rows]
    finally:
        conn.close()


def delete_rule(rule_id: int, db_path: Optional[str | Path] = None) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute("DELETE FROM category_rules WHERE id = ?", (rule_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def apply_rules(db_path: Optional[str | Path] = None) -> int:
    """Run the rules engine over all transactions. Returns rows updated."""
    conn = connect(db_path)
    try:
        updated = _run_rules_engine(conn)
        conn.commit()
        return updated
    finally:
        conn.close()


def _run_rules_engine(conn: sqlite3.Connection) -> int:
    """The §6 rules engine, on an open connection (caller commits).

    For every non-manual transaction, the matching rule with the highest
    priority (ties: lowest id) sets category, cleaned_merchant and rule_id,
    and flags the row as a transfer if the rule says so.  A row whose rule
    no longer matches (e.g. it was deleted) reverts to Uncategorized.
    Matching is a case-insensitive substring test on raw_description.
    """
    rules = [
        (r, r["pattern"].casefold())
        for r in conn.execute(
            """
            SELECT id, pattern, category, cleaned_merchant, mark_transfer, account_id
            FROM category_rules ORDER BY priority DESC, id ASC
            """
        )
        if r["pattern"].strip()
    ]
    rows = conn.execute(
        """
        SELECT id, account_id, raw_description, category, cleaned_merchant,
               is_transfer, category_source, rule_id
        FROM transactions WHERE category_source != 'manual'
        """
    ).fetchall()

    updates = []
    for t in rows:
        desc = t["raw_description"].casefold()
        rule = next(
            (
                r
                for r, pattern in rules
                if pattern in desc and r["account_id"] in (None, t["account_id"])
            ),
            None,
        )
        if rule is not None:
            new = (
                rule["category"],
                rule["cleaned_merchant"],
                1 if rule["mark_transfer"] else t["is_transfer"],
                "rule",
                rule["id"],
            )
        elif t["category_source"] == "rule":
            new = ("Uncategorized", None, t["is_transfer"], "default", None)
        else:
            continue
        old = (t["category"], t["cleaned_merchant"], t["is_transfer"], t["category_source"], t["rule_id"])
        if new != old:
            updates.append((*new, t["id"]))

    conn.executemany(
        """
        UPDATE transactions
        SET category = ?, cleaned_merchant = ?, is_transfer = ?,
            category_source = ?, rule_id = ?
        WHERE id = ?
        """,
        updates,
    )
    return len(updates)


def count_rule_matches(
    pattern: str,
    account_id: Optional[int] = None,
    db_path: Optional[str | Path] = None,
) -> int:
    """How many transactions *pattern* matches, using the engine's semantics."""
    needle = pattern.casefold()
    if not needle.strip():
        return 0
    conn = connect(db_path)
    try:
        sql = "SELECT raw_description FROM transactions"
        params: tuple = ()
        if account_id is not None:
            sql += " WHERE account_id = ?"
            params = (account_id,)
        return sum(1 for (desc,) in conn.execute(sql, params) if needle in desc.casefold())
    finally:
        conn.close()
