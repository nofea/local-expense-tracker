"""Schema creation and migrations, tracked with ``PRAGMA user_version``.

Version history:

* **0** — either an empty file (fresh install) or the original v1 schema
  (``transactions`` with REAL ``amount`` and ``id = sha256(date|desc|amount)``,
  plus ``category_rules``).
* **2** — the schema in app_spec.md §2.  v1 → v2 follows Appendix B.

``migrate`` is called from ``database.init_db`` on every startup and is a
no-op once the file is at ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core.models import (
    assign_ordinals,
    compute_transaction_id,
    normalize_description,
    to_minor,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

DEFAULT_ACCOUNT_NAME = "Default"

# ---------------------------------------------------------------------------
# Schema v2 (app_spec.md §2).  One statement per entry: they run inside an
# explicit transaction, which ``executescript`` would commit early.
# ---------------------------------------------------------------------------

_V2_ACCOUNTS = """
CREATE TABLE accounts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL CHECK (kind IN ('bank', 'credit_card')),
    institution   TEXT
)"""

_V2_STATEMENTS = """
CREATE TABLE statements (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    file_name       TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    period_start    TEXT,
    period_end      TEXT,
    opening_minor   INTEGER,
    closing_minor   INTEGER,
    extractor       TEXT NOT NULL CHECK (extractor IN ('csv_mapping', 'llm')),
    model           TEXT,
    status          TEXT NOT NULL CHECK (status IN
                      ('queued', 'needs_password', 'extracting', 'review',
                       'committed', 'failed', 'discarded')),
    progress_done   INTEGER NOT NULL DEFAULT 0,
    progress_total  INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    reconciled      INTEGER,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    committed_at    TEXT,
    UNIQUE (account_id, file_sha256)
)"""

_V2_STAGED_ROWS = """
CREATE TABLE staged_rows (
    id               INTEGER PRIMARY KEY,
    statement_id     INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    transaction_date TEXT,
    value_date       TEXT,
    raw_description  TEXT,
    amount_minor     INTEGER,
    is_transfer      INTEGER NOT NULL DEFAULT 0,
    dropped          INTEGER NOT NULL DEFAULT 0,
    issues           TEXT NOT NULL DEFAULT '[]'
)"""

_V2_CATEGORY_RULES = """
CREATE TABLE category_rules (
    id               INTEGER PRIMARY KEY,
    pattern          TEXT NOT NULL UNIQUE,
    category         TEXT NOT NULL,
    cleaned_merchant TEXT,
    mark_transfer    INTEGER NOT NULL DEFAULT 0,
    priority         INTEGER NOT NULL DEFAULT 0,
    account_id       INTEGER REFERENCES accounts(id)
)"""


def _v2_transactions(table: str = "transactions") -> str:
    return f"""
CREATE TABLE {table} (
    id               TEXT PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    statement_id     INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    transaction_date TEXT NOT NULL,
    value_date       TEXT,
    raw_description  TEXT NOT NULL,
    cleaned_merchant TEXT,
    amount_minor     INTEGER NOT NULL CHECK (amount_minor <> 0),
    is_transfer      INTEGER NOT NULL DEFAULT 0,
    category         TEXT NOT NULL DEFAULT 'Uncategorized',
    category_source  TEXT NOT NULL DEFAULT 'default'
                       CHECK (category_source IN ('default', 'rule', 'manual')),
    rule_id          INTEGER REFERENCES category_rules(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""


_V2_TX_INDEXES = [
    "CREATE INDEX idx_tx_date     ON transactions(transaction_date)",
    "CREATE INDEX idx_tx_category ON transactions(category)",
    "CREATE INDEX idx_tx_account  ON transactions(account_id, transaction_date)",
]

_V2_CSV_MAPPINGS = """
CREATE TABLE csv_mappings (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    header_signature  TEXT NOT NULL,
    skip_rows         INTEGER NOT NULL DEFAULT 0,
    date_col          TEXT NOT NULL,
    date_format       TEXT NOT NULL,
    value_date_col    TEXT,
    description_col   TEXT NOT NULL,
    amount_col        TEXT,
    debit_col         TEXT,
    credit_col        TEXT,
    negate            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_id, header_signature),
    CHECK ((amount_col IS NOT NULL) OR (debit_col IS NOT NULL AND credit_col IS NOT NULL))
)"""


@dataclass
class MigrationReport:
    """What a v1 → v2 migration did, for logging and tests."""

    backup_path: Optional[Path] = None
    statements_created: int = 0
    rows_migrated: int = 0
    rows_dropped_zero_amount: int = 0
    rows_merged: int = 0  # v1 rows that collapse to the same v2 id
    rules_updated: int = 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def migrate(
    conn: sqlite3.Connection,
    db_path: Optional[Path],
    run_rules: Callable[[sqlite3.Connection], int],
) -> Optional[MigrationReport]:
    """Bring the database on *conn* up to ``SCHEMA_VERSION``.

    *db_path* is the file behind *conn* (used for the pre-migration backup;
    ``None`` skips the backup).  *run_rules* is the §6 rules engine; it is
    injected to avoid a circular import with ``core.database``.

    Returns a report when a v1 → v2 migration ran, else ``None``.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return None
    if version != 0:
        raise RuntimeError(
            f"Database schema version {version} is not supported "
            f"(this build understands 0 and {SCHEMA_VERSION})."
        )

    has_v1 = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
    ).fetchone()

    if not has_v1:
        _in_transaction(conn, _create_v2)
        log.info("Created schema v%d", SCHEMA_VERSION)
        return None

    report = MigrationReport()
    if db_path is not None:
        report.backup_path = _backup(conn, Path(db_path))
    _in_transaction(conn, lambda c: _migrate_v1_to_v2(c, run_rules, report))
    log.info("Migrated schema v1 -> v%d: %s", SCHEMA_VERSION, report)
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_transaction(
    conn: sqlite3.Connection, work: Callable[[sqlite3.Connection], None]
) -> None:
    """Run *work* in one explicit transaction with foreign keys deferred.

    ``foreign_keys`` can only be toggled outside a transaction, and table
    rebuilds need it off; integrity is checked before committing instead.
    """
    old_isolation = conn.isolation_level
    old_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.isolation_level = None  # explicit BEGIN/COMMIT only
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            work(conn)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"Foreign key violations after migration: {violations[:5]}")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if old_fk else 'OFF'}")
        conn.isolation_level = old_isolation


def _backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    """Copy the database to ``<name>.v1.bak`` (never overwriting an old one)."""
    target = db_path.with_name(db_path.name + ".v1.bak")
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = db_path.with_name(f"{db_path.name}.v1.{stamp}.bak")
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    log.info("Backed up v1 database to %s", target)
    return target


def _create_v2(conn: sqlite3.Connection) -> None:
    for ddl in (
        _V2_ACCOUNTS,
        _V2_STATEMENTS,
        _V2_CATEGORY_RULES,
        _v2_transactions(),
        *_V2_TX_INDEXES,
        _V2_STAGED_ROWS,
        _V2_CSV_MAPPINGS,
    ):
        conn.execute(ddl)


def _migrate_v1_to_v2(
    conn: sqlite3.Connection,
    run_rules: Callable[[sqlite3.Connection], int],
    report: MigrationReport,
) -> None:
    # Step 1 — new tables and the Default account.
    for ddl in (_V2_ACCOUNTS, _V2_STATEMENTS, _V2_STAGED_ROWS, _V2_CSV_MAPPINGS):
        conn.execute(ddl)
    account_id = conn.execute(
        "INSERT INTO accounts (name, kind) VALUES (?, 'bank')", (DEFAULT_ACCOUNT_NAME,)
    ).lastrowid

    # Step 4 (schema part) — new rule columns.  Done before the transactions
    # rebuild because transactions.rule_id references category_rules.
    conn.execute("ALTER TABLE category_rules ADD COLUMN mark_transfer INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE category_rules ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE category_rules ADD COLUMN account_id INTEGER REFERENCES accounts(id)")

    # Step 2 — one committed statement per distinct v1 source_file, in the
    # order the files were first imported.  Rows are processed in that same
    # order, so when two v1 rows collapse to one v2 id the earliest import
    # keeps it.
    rows = conn.execute(
        """
        SELECT transaction_date, value_date, raw_description, cleaned_merchant,
               amount, category, source_file, injected_at
        FROM transactions
        ORDER BY injected_at, rowid
        """
    ).fetchall()

    statements: dict[str, int] = {}
    for source_file in dict.fromkeys(r[6] for r in rows):
        first_seen = min((r[7] for r in rows if r[6] == source_file and r[7]), default=None)
        statements[source_file] = conn.execute(
            """
            INSERT INTO statements
                (account_id, file_name, file_sha256, extractor, status,
                 created_at, committed_at)
            VALUES (?, ?, ?, 'llm', 'committed',
                    COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (account_id, source_file, f"migrated:{source_file}", first_seen, first_seen),
        ).lastrowid
    report.statements_created = len(statements)

    # Step 3 — rebuild transactions with integer money and §5 ids.
    conn.execute(_v2_transactions("transactions_v2"))

    for source_file, statement_id in statements.items():
        group = []
        for r in rows:
            if r[6] != source_file:
                continue
            amount_minor = to_minor(r[4])
            if amount_minor == 0:
                # v2 forbids zero amounts; the row survives in the backup.
                report.rows_dropped_zero_amount += 1
                continue
            group.append((r, normalize_description(r[2]), amount_minor))

        ordinals = assign_ordinals([(r[0], desc, minor) for r, desc, minor in group])
        for (r, desc, minor), ordinal in zip(group, ordinals):
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO transactions_v2
                    (id, account_id, statement_id, transaction_date, value_date,
                     raw_description, cleaned_merchant, amount_minor, category,
                     category_source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (
                    compute_transaction_id(account_id, r[0], desc, minor, ordinal),
                    account_id,
                    statement_id,
                    r[0],
                    r[1],
                    desc,
                    r[3],
                    minor,
                    r[5] or "Uncategorized",
                    # v1 had no manual edits: every real category came from a rule.
                    "default" if (r[5] or "Uncategorized") == "Uncategorized" else "rule",
                    r[7],
                ),
            )
            if cur.rowcount:
                report.rows_migrated += 1
            else:
                report.rows_merged += 1

    conn.execute("DROP TABLE transactions")
    conn.execute("ALTER TABLE transactions_v2 RENAME TO transactions")
    for ddl in _V2_TX_INDEXES:
        conn.execute(ddl)

    # Step 4 — re-run the rules engine so rule_id / category_source are exact.
    report.rules_updated = run_rules(conn)
