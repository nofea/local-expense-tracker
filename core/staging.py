"""Staging, review and commit of imported statements (app_spec.md §3.4–3.5).

A ``statements`` row is both the import job record and, once extraction
finishes, the container for its ``staged_rows``.  Nothing reaches
``transactions`` until :func:`commit` is called for a statement in
``review``.

Lifecycle: ``queued`` → ``extracting`` → ``review`` → ``committed`` or
``discarded``; any step before review can end in ``failed``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from core import validator
from core.database import _insert_rows, _run_rules_engine, connect
from core.models import assign_ordinals, compute_transaction_id, normalize_description, to_minor

# Statuses that make a re-upload of the same file pointless.
_ACTIVE = ("queued", "needs_password", "extracting", "review", "committed")
_INTERRUPTIBLE = ("queued", "needs_password", "extracting")


class AlreadyImported(Exception):
    """The same file (by SHA-256) is already imported or in progress."""

    def __init__(self, statement: sqlite3.Row):
        self.statement = statement
        super().__init__(
            f"This file was already uploaded as '{statement['file_name']}' "
            f"(import #{statement['id']}, {statement['status']})."
        )


class ReviewError(Exception):
    """A review action was refused; the message is shown to the user."""


@contextmanager
def _write_tx(db_path: Optional[str | Path]) -> Iterator[sqlite3.Connection]:
    """A connection inside ``BEGIN IMMEDIATE``, so status checks and the
    writes that depend on them cannot interleave with another request."""
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Job / statement records
# ---------------------------------------------------------------------------

def create_import(
    *,
    account_id: int,
    file_name: str,
    file_sha256: str,
    model: Optional[str],
    db_path: Optional[str | Path] = None,
) -> int:
    """Create a ``queued`` statement for a new upload (§3.1 steps 3–4).

    Raises :class:`AlreadyImported` if the account already has this file in
    progress, in review or committed.  A previous failed or discarded
    attempt is replaced.
    """
    with _write_tx(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM statements WHERE account_id = ? AND file_sha256 = ?",
            (account_id, file_sha256),
        ).fetchone()
        if existing is not None:
            if existing["status"] in _ACTIVE:
                raise AlreadyImported(existing)
            conn.execute("DELETE FROM statements WHERE id = ?", (existing["id"],))
        return conn.execute(
            """
            INSERT INTO statements
                (account_id, file_name, file_sha256, extractor, model, status)
            VALUES (?, ?, ?, 'llm', ?, 'queued')
            """,
            (account_id, file_name, file_sha256, model),
        ).lastrowid


def get_statement(statement_id: int, db_path: Optional[str | Path] = None) -> Optional[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT * FROM statements WHERE id = ?", (statement_id,)).fetchone()
    finally:
        conn.close()


def list_imports(db_path: Optional[str | Path] = None) -> list[dict]:
    """All statements, newest first, with staged and committed row counts."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.*, a.name AS account_name,
                   (SELECT COUNT(*) FROM staged_rows r
                     WHERE r.statement_id = s.id AND r.dropped = 0) AS staged_count,
                   (SELECT COUNT(*) FROM transactions t
                     WHERE t.statement_id = s.id) AS committed_count
            FROM statements s JOIN accounts a ON a.id = s.account_id
            ORDER BY s.created_at DESC, s.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_status(
    statement_id: int,
    status: str,
    *,
    error: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE statements SET status = ?, error = COALESCE(?, error) WHERE id = ?",
            (status, error, statement_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_progress(
    statement_id: int, done: int, total: int, db_path: Optional[str | Path] = None
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE statements SET progress_done = ?, progress_total = ? WHERE id = ?",
            (done, total, statement_id),
        )
        conn.commit()
    finally:
        conn.close()


def recover_interrupted(db_path: Optional[str | Path] = None) -> int:
    """Mark jobs that were running when the server stopped as failed (§3).

    Call once at web-server startup, before the worker starts.  Returns the
    number of jobs marked.
    """
    conn = connect(db_path)
    try:
        cur = conn.execute(
            f"""
            UPDATE statements SET status = 'failed', error = 'interrupted'
            WHERE status IN ({",".join("?" * len(_INTERRUPTIBLE))})
            """,
            _INTERRUPTIBLE,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Staging (§3.4)
# ---------------------------------------------------------------------------

def stage_rows(
    statement_id: int,
    rows: list[dict],
    *,
    source_text: str,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    opening_minor: Optional[int] = None,
    closing_minor: Optional[int] = None,
    error: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> None:
    """Store extracted rows in document order, validate, and move the
    statement to ``review``.

    *rows* carry ``transaction_date``, ``value_date``, ``raw_description``
    and ``amount`` (major units).  *source_text* is used once for the
    ``not_in_source`` check and is not stored.
    """
    with _write_tx(db_path) as conn:
        conn.execute("DELETE FROM staged_rows WHERE statement_id = ?", (statement_id,))
        conn.executemany(
            """
            INSERT INTO staged_rows
                (statement_id, position, transaction_date, value_date,
                 raw_description, amount_minor)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    statement_id,
                    position,
                    row.get("transaction_date"),
                    row.get("value_date"),
                    row.get("raw_description"),
                    _amount_minor(row.get("amount")),
                )
                for position, row in enumerate(rows)
            ],
        )
        conn.execute(
            """
            UPDATE statements
            SET period_start = ?, period_end = ?, opening_minor = ?,
                closing_minor = ?, error = ?, status = 'review'
            WHERE id = ?
            """,
            (
                period_start if validator.is_iso_date(period_start) else None,
                period_end if validator.is_iso_date(period_end) else None,
                opening_minor,
                closing_minor,
                error,
                statement_id,
            ),
        )
        # With no source text there is nothing to check descriptions against.
        _revalidate(conn, statement_id, validator.normalize_for_match(source_text) or None)


def _amount_minor(amount) -> Optional[int]:
    try:
        return None if amount is None else to_minor(amount)
    except (ArithmeticError, ValueError):
        return None


def _revalidate(
    conn: sqlite3.Connection, statement_id: int, source_norm: Optional[str] = None
) -> None:
    """Recompute every row's issues and the statement's reconciliation.

    Without *source_norm* (i.e. after staging, when the source text is gone)
    a row keeps ``not_in_source`` from its previous issues; editing the
    description clears it, since the user has then vouched for the text.
    ``bad_value_date`` is likewise kept once the value date was cleared.
    """
    statement = conn.execute(
        "SELECT account_id, opening_minor, closing_minor FROM statements WHERE id = ?",
        (statement_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT * FROM staged_rows WHERE statement_id = ? ORDER BY position",
        (statement_id,),
    ).fetchall()

    issues_by_row: dict[int, list[str]] = {}
    value_dates: dict[int, Optional[str]] = {}
    for row in rows:
        issues, value_date = validator.row_issues(
            row["transaction_date"],
            row["value_date"],
            row["raw_description"],
            row["amount_minor"],
            source_norm,
        )
        if source_norm is None:
            previous = json.loads(row["issues"])
            for sticky in ("not_in_source", "bad_value_date"):
                if sticky in previous and sticky not in issues:
                    issues.append(sticky)
        issues_by_row[row["id"]] = issues
        value_dates[row["id"]] = value_date

    # duplicate_in_ledger: ids exactly as commit would compute them, i.e.
    # over the non-dropped rows (all error-free by then) in position order.
    candidates = [
        r for r in rows if not r["dropped"] and not validator.has_errors(issues_by_row[r["id"]])
    ]
    keys = [
        (r["transaction_date"], normalize_description(r["raw_description"]), r["amount_minor"])
        for r in candidates
    ]
    ids = {
        r["id"]: compute_transaction_id(statement["account_id"], *key, ordinal)
        for r, key, ordinal in zip(candidates, keys, assign_ordinals(keys))
    }
    existing: set[str] = set()
    id_list = list(ids.values())
    for i in range(0, len(id_list), 500):
        batch = id_list[i : i + 500]
        existing.update(
            r[0]
            for r in conn.execute(
                f"SELECT id FROM transactions WHERE id IN ({','.join('?' * len(batch))})", batch
            )
        )
    for row_id, tx_id in ids.items():
        if tx_id in existing:
            issues_by_row[row_id].append("duplicate_in_ledger")

    order = list(validator.LEVELS)
    conn.executemany(
        "UPDATE staged_rows SET issues = ?, value_date = ? WHERE id = ?",
        [
            (json.dumps(sorted(set(issues_by_row[r["id"]]), key=order.index)), value_dates[r["id"]], r["id"])
            for r in rows
        ],
    )

    reconciled, _ = validator.reconcile(
        statement["opening_minor"],
        statement["closing_minor"],
        (r["amount_minor"] or 0 for r in rows if not r["dropped"]),
    )
    conn.execute("UPDATE statements SET reconciled = ? WHERE id = ?", (reconciled, statement_id))


# ---------------------------------------------------------------------------
# Review (§3.5)
# ---------------------------------------------------------------------------

@dataclass
class Review:
    statement: dict
    rows: list[dict]
    error_rows: int = 0
    warning_rows: int = 0
    duplicate_rows: int = 0
    active_rows: int = 0
    total_minor: int = 0
    reconciled: Optional[int] = None
    difference_minor: Optional[int] = None
    chunk_errors: list[str] = field(default_factory=list)

    @property
    def can_commit(self) -> bool:
        return self.statement["status"] == "review" and self.error_rows == 0 and self.active_rows > 0


def get_review(statement_id: int, db_path: Optional[str | Path] = None) -> Optional[Review]:
    conn = connect(db_path)
    try:
        statement = conn.execute(
            """
            SELECT s.*, a.name AS account_name
            FROM statements s JOIN accounts a ON a.id = s.account_id
            WHERE s.id = ?
            """,
            (statement_id,),
        ).fetchone()
        if statement is None:
            return None
        rows = conn.execute(
            "SELECT * FROM staged_rows WHERE statement_id = ? ORDER BY position",
            (statement_id,),
        ).fetchall()
    finally:
        conn.close()

    review = Review(statement=dict(statement), rows=[])
    for r in rows:
        row = dict(r)
        row["issues"] = json.loads(row["issues"])
        row["levels"] = {validator.LEVELS[c] for c in row["issues"]}
        review.rows.append(row)
        if row["dropped"]:
            continue
        review.active_rows += 1
        review.total_minor += row["amount_minor"] or 0
        if validator.ERROR in row["levels"]:
            review.error_rows += 1
        elif validator.WARNING in row["levels"]:
            review.warning_rows += 1
        if "duplicate_in_ledger" in row["issues"]:
            review.duplicate_rows += 1

    review.reconciled, review.difference_minor = validator.reconcile(
        statement["opening_minor"], statement["closing_minor"], [review.total_minor]
    )
    if statement["error"]:
        review.chunk_errors = statement["error"].splitlines()
    return review


def _require_review(conn: sqlite3.Connection, statement_id: int) -> sqlite3.Row:
    statement = conn.execute("SELECT * FROM statements WHERE id = ?", (statement_id,)).fetchone()
    if statement is None:
        raise ReviewError("Import not found.")
    if statement["status"] != "review":
        raise ReviewError(f"This import is {statement['status']}, so it can no longer be edited.")
    return statement


def update_row(
    statement_id: int,
    row_id: int,
    *,
    transaction_date: str,
    raw_description: str,
    amount: str,
    is_transfer: bool,
    db_path: Optional[str | Path] = None,
) -> None:
    """Apply an edit from the review screen, then revalidate."""
    try:
        amount_minor = to_minor(amount.replace(",", "").strip())
    except (ArithmeticError, ValueError):
        raise ReviewError(f"'{amount}' is not a valid amount.") from None

    with _write_tx(db_path) as conn:
        _require_review(conn, statement_id)
        row = conn.execute(
            "SELECT * FROM staged_rows WHERE id = ? AND statement_id = ?", (row_id, statement_id)
        ).fetchone()
        if row is None:
            raise ReviewError("Row not found.")
        issues = json.loads(row["issues"])
        if normalize_description(raw_description) != normalize_description(row["raw_description"] or ""):
            issues = [c for c in issues if c != "not_in_source"]
        conn.execute(
            """
            UPDATE staged_rows
            SET transaction_date = ?, raw_description = ?, amount_minor = ?,
                is_transfer = ?, issues = ?
            WHERE id = ?
            """,
            (
                transaction_date.strip(),
                raw_description,
                amount_minor,
                int(is_transfer),
                json.dumps(issues),
                row_id,
            ),
        )
        _revalidate(conn, statement_id)


def set_dropped(
    statement_id: int, row_id: int, dropped: bool, db_path: Optional[str | Path] = None
) -> None:
    with _write_tx(db_path) as conn:
        _require_review(conn, statement_id)
        cur = conn.execute(
            "UPDATE staged_rows SET dropped = ? WHERE id = ? AND statement_id = ?",
            (int(dropped), row_id, statement_id),
        )
        if cur.rowcount == 0:
            raise ReviewError("Row not found.")
        _revalidate(conn, statement_id)


def update_metadata(
    statement_id: int,
    *,
    period_start: str = "",
    period_end: str = "",
    opening_balance: str = "",
    closing_balance: str = "",
    db_path: Optional[str | Path] = None,
) -> None:
    """Edit the statement period and balances (blank = unknown)."""
    for label, value in (("Period start", period_start), ("Period end", period_end)):
        if value.strip() and not validator.is_iso_date(value.strip()):
            raise ReviewError(f"{label} must be a YYYY-MM-DD date.")
    balances = []
    for label, value in (("Opening balance", opening_balance), ("Closing balance", closing_balance)):
        value = value.replace(",", "").strip()
        try:
            balances.append(to_minor(value) if value else None)
        except (ArithmeticError, ValueError):
            raise ReviewError(f"{label} '{value}' is not a valid amount.") from None

    with _write_tx(db_path) as conn:
        _require_review(conn, statement_id)
        conn.execute(
            """
            UPDATE statements
            SET period_start = ?, period_end = ?, opening_minor = ?, closing_minor = ?
            WHERE id = ?
            """,
            (period_start.strip() or None, period_end.strip() or None, *balances, statement_id),
        )
        _revalidate(conn, statement_id)


@dataclass
class CommitResult:
    inserted: int
    duplicates: int


def commit(
    statement_id: int,
    *,
    accept_mismatch: bool = False,
    db_path: Optional[str | Path] = None,
) -> CommitResult:
    """Move the reviewed rows into the ledger, in one transaction (§3.5).

    Refused while any non-dropped row has an error-level issue, and on a
    reconciliation mismatch unless *accept_mismatch* is set.
    """
    with _write_tx(db_path) as conn:
        statement = _require_review(conn, statement_id)
        _revalidate(conn, statement_id)
        rows = conn.execute(
            """
            SELECT * FROM staged_rows
            WHERE statement_id = ? AND dropped = 0 ORDER BY position
            """,
            (statement_id,),
        ).fetchall()
        if not rows:
            raise ReviewError("There are no rows to commit. Discard the import instead.")
        if any(validator.has_errors(json.loads(r["issues"])) for r in rows):
            raise ReviewError("Fix or drop the rows marked as errors before committing.")
        reconciled = conn.execute(
            "SELECT reconciled FROM statements WHERE id = ?", (statement_id,)
        ).fetchone()[0]
        if reconciled == 0 and not accept_mismatch:
            raise ReviewError(
                "The rows don't add up to the closing balance. "
                "Check for missing rows, or tick “Commit anyway”."
            )

        inserted, duplicates, _ = _insert_rows(
            conn,
            [
                {
                    "transaction_date": r["transaction_date"],
                    "value_date": r["value_date"],
                    "raw_description": r["raw_description"],
                    "amount_minor": r["amount_minor"],
                    "is_transfer": r["is_transfer"],
                }
                for r in rows
            ],
            statement["account_id"],
            statement_id,
        )
        conn.execute("DELETE FROM staged_rows WHERE statement_id = ?", (statement_id,))
        conn.execute(
            "UPDATE statements SET status = 'committed', committed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (statement_id,),
        )
        _run_rules_engine(conn)
    return CommitResult(inserted=inserted, duplicates=duplicates)


def discard(statement_id: int, db_path: Optional[str | Path] = None) -> None:
    """Drop a statement in review; its staged rows are deleted."""
    with _write_tx(db_path) as conn:
        _require_review(conn, statement_id)
        conn.execute("DELETE FROM staged_rows WHERE statement_id = ?", (statement_id,))
        conn.execute("UPDATE statements SET status = 'discarded' WHERE id = ?", (statement_id,))
