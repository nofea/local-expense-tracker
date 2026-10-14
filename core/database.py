"""SQLite database layer for the expense tracker (schema v3, app_spec.md §2).

All functions accept an optional *db_path* so callers can override the
default location (``storage.db_path`` in config.toml, default
``data/expenses.db``, or the ``EXPENSE_TRACKER_DB`` environment variable).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from core import migrations
from core.config import get_config
from core.csv_import import CsvMapping
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


def list_accounts(db_path: Optional[str | Path] = None) -> list[sqlite3.Row]:
    """All accounts (id, name, kind, institution), by name."""
    conn = connect(db_path)
    try:
        return conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
    finally:
        conn.close()


ACCOUNT_KINDS = ("bank", "credit_card")


class AccountError(ValueError):
    """An account change was refused; the message is shown to the user."""


def _account_fields(name: str, kind: str, institution: Optional[str]) -> tuple[str, str, Optional[str]]:
    name = " ".join(name.split())
    if not name:
        raise AccountError("The account needs a name.")
    if kind not in ACCOUNT_KINDS:
        raise AccountError("The account type must be bank or credit card.")
    return name, kind, " ".join((institution or "").split()) or None


def create_account(
    name: str,
    kind: str = "bank",
    institution: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> int:
    """Create an account; raises :class:`AccountError` on a bad or taken name."""
    fields = _account_fields(name, kind, institution)
    conn = connect(db_path)
    try:
        account_id = conn.execute(
            "INSERT INTO accounts (name, kind, institution) VALUES (?, ?, ?)", fields
        ).lastrowid
        conn.commit()
        return account_id
    except sqlite3.IntegrityError:
        raise AccountError(f"An account named '{fields[0]}' already exists.") from None
    finally:
        conn.close()


def update_account(
    account_id: int,
    *,
    name: str,
    kind: str,
    institution: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> None:
    fields = _account_fields(name, kind, institution)
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE accounts SET name = ?, kind = ?, institution = ? WHERE id = ?",
            (*fields, account_id),
        )
        if cur.rowcount == 0:
            raise AccountError("Account not found.")
        conn.commit()
    except sqlite3.IntegrityError:
        raise AccountError(f"An account named '{fields[0]}' already exists.") from None
    finally:
        conn.close()


def delete_account(account_id: int, db_path: Optional[str | Path] = None) -> None:
    """Delete an account that has no statements (§7.1).

    Its CSV mappings and holder links (§2.4) go with it.  Rules scoped to it must be deleted first,
    since turning them into all-account rules would change other rows.
    """
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            statements = conn.execute(
                "SELECT COUNT(*) FROM statements WHERE account_id = ?", (account_id,)
            ).fetchone()[0]
            if statements:
                raise AccountError(
                    f"This account has {statements} import(s). Delete them on the Imports page first."
                )
            rules = conn.execute(
                "SELECT COUNT(*) FROM category_rules WHERE account_id = ?", (account_id,)
            ).fetchone()[0]
            if rules:
                raise AccountError(f"{rules} rule(s) apply only to this account. Delete them first.")
            conn.execute("DELETE FROM csv_mappings WHERE account_id = ?", (account_id,))
            if conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,)).rowcount == 0:
                raise AccountError("Account not found.")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def list_accounts_summary(db_path: Optional[str | Path] = None) -> list[dict]:
    """Accounts with their import, transaction and scoped-rule counts."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT a.*,
                   (SELECT COUNT(*) FROM statements s WHERE s.account_id = a.id) AS statement_count,
                   (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS transaction_count,
                   (SELECT COUNT(*) FROM category_rules r WHERE r.account_id = a.id) AS rule_count
            FROM accounts a ORDER BY a.name
            """
        ).fetchall()
        holders = _account_holders(conn)
        return [{**dict(r), "holders": holders.get(r["id"], [])} for r in rows]
    finally:
        conn.close()


def _account_holders(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """account id -> its holders (member_id, name, role), primary first."""
    holders: dict[int, list[dict]] = {}
    for r in conn.execute(
        """
        SELECT am.account_id, am.member_id, m.name, am.role
        FROM account_members am JOIN members m ON m.id = am.member_id
        ORDER BY am.role = 'joint', m.name
        """
    ):
        holders.setdefault(r["account_id"], []).append(
            {"member_id": r["member_id"], "name": r["name"], "role": r["role"]}
        )
    return holders


# ---------------------------------------------------------------------------
# Families, members & account holders (§2.4)
# ---------------------------------------------------------------------------

class FamilyError(ValueError):
    """A family, member or holder change was refused; the message is shown to the user."""


@contextmanager
def _write_tx(db_path: Optional[str | Path]) -> Iterator[sqlite3.Connection]:
    """A connection inside ``BEGIN IMMEDIATE``, so checks and writes are atomic."""
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


def _clean_name(name: str, what: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise FamilyError(f"The {what} needs a name.")
    return name


def _require(conn: sqlite3.Connection, table: str, row_id: int, what: str) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise FamilyError(f"{what} not found.")
    return row


def create_family(name: str, db_path: Optional[str | Path] = None) -> int:
    name = _clean_name(name, "family")
    try:
        with _write_tx(db_path) as conn:
            return conn.execute("INSERT INTO families (name) VALUES (?)", (name,)).lastrowid
    except sqlite3.IntegrityError:
        raise FamilyError(f"A family named '{name}' already exists.") from None


def rename_family(family_id: int, name: str, db_path: Optional[str | Path] = None) -> None:
    name = _clean_name(name, "family")
    try:
        with _write_tx(db_path) as conn:
            _require(conn, "families", family_id, "Family")
            conn.execute("UPDATE families SET name = ? WHERE id = ?", (name, family_id))
    except sqlite3.IntegrityError:
        raise FamilyError(f"A family named '{name}' already exists.") from None


def delete_family(family_id: int, db_path: Optional[str | Path] = None) -> None:
    """Delete a family with its members and their holder links.  All holders
    of an account are in one family, so no account is left half-linked; the
    ledger is untouched."""
    with _write_tx(db_path) as conn:
        _require(conn, "families", family_id, "Family")
        conn.execute("DELETE FROM families WHERE id = ?", (family_id,))


def create_member(family_id: int, name: str, db_path: Optional[str | Path] = None) -> int:
    name = _clean_name(name, "member")
    try:
        with _write_tx(db_path) as conn:
            _require(conn, "families", family_id, "Family")
            return conn.execute(
                "INSERT INTO members (family_id, name) VALUES (?, ?)", (family_id, name)
            ).lastrowid
    except sqlite3.IntegrityError:
        raise FamilyError(f"This family already has a member named '{name}'.") from None


def _shared_accounts(conn: sqlite3.Connection, member_id: int, role: Optional[str] = None) -> int:
    """How many of the member's accounts (optionally in *role*) have other holders."""
    return conn.execute(
        f"""
        SELECT COUNT(*) FROM account_members am
        WHERE am.member_id = ? {"AND am.role = ?" if role else ""}
          AND EXISTS (SELECT 1 FROM account_members o
                      WHERE o.account_id = am.account_id AND o.member_id != am.member_id)
        """,
        (member_id, role) if role else (member_id,),
    ).fetchone()[0]


def update_member(
    member_id: int,
    *,
    name: str,
    family_id: int,
    db_path: Optional[str | Path] = None,
) -> None:
    """Rename a member and/or move them to another family.  A move is refused
    while they share an account, which would then span two families."""
    name = _clean_name(name, "member")
    try:
        with _write_tx(db_path) as conn:
            member = _require(conn, "members", member_id, "Member")
            _require(conn, "families", family_id, "Family")
            if family_id != member["family_id"] and _shared_accounts(conn, member_id):
                raise FamilyError(
                    f"{member['name']} shares accounts with other members. "
                    "Change those accounts' holders before moving them to another family."
                )
            conn.execute(
                "UPDATE members SET name = ?, family_id = ? WHERE id = ?", (name, family_id, member_id)
            )
    except sqlite3.IntegrityError:
        raise FamilyError(f"That family already has a member named '{name}'.") from None


def delete_member(member_id: int, db_path: Optional[str | Path] = None) -> None:
    """Delete a member and their holder links.  Refused while they are the
    primary holder of an account that still has joint holders."""
    with _write_tx(db_path) as conn:
        member = _require(conn, "members", member_id, "Member")
        if _shared_accounts(conn, member_id, "primary"):
            raise FamilyError(
                f"{member['name']} is the primary holder of a joint account. "
                "Make another holder primary first."
            )
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))


def set_account_holders(
    account_id: int,
    primary_member_id: Optional[int],
    joint_member_ids: Optional[list[int]] = None,
    db_path: Optional[str | Path] = None,
) -> None:
    """Replace an account's holders (§2.4).  ``None`` and no joint holders
    unlinks it.  There must be a primary if there are joint holders, and all
    holders must be in one family."""
    joint = list(dict.fromkeys(m for m in (joint_member_ids or []) if m != primary_member_id))
    if joint and primary_member_id is None:
        raise FamilyError("Choose a primary holder for this account.")
    with _write_tx(db_path) as conn:
        _require(conn, "accounts", account_id, "Account")
        families = set()
        for member_id in ([primary_member_id] if primary_member_id is not None else []) + joint:
            families.add(_require(conn, "members", member_id, "Member")["family_id"])
        if len(families) > 1:
            raise FamilyError("All holders of an account must be in the same family.")
        conn.execute("DELETE FROM account_members WHERE account_id = ?", (account_id,))
        if primary_member_id is not None:
            conn.execute(
                "INSERT INTO account_members (account_id, member_id, role) VALUES (?, ?, 'primary')",
                (account_id, primary_member_id),
            )
        conn.executemany(
            "INSERT INTO account_members (account_id, member_id, role) VALUES (?, ?, 'joint')",
            [(account_id, m) for m in joint],
        )


def list_families(db_path: Optional[str | Path] = None) -> dict:
    """Everything the Families page and the scope pickers need.

    Returns ``{"families": [...], "unlinked": [...]}``.  Each family is
    ``{id, name, members, accounts}``; each member ``{id, name, family_id,
    accounts: [{id, name, kind, role}]}``; each family account ``{id, name,
    kind, primary_id, joint_ids}``.  *unlinked* lists accounts with no
    holders.  Everything is ordered by name.
    """
    conn = connect(db_path)
    try:
        families = [
            {"id": f["id"], "name": f["name"], "members": [], "accounts": []}
            for f in conn.execute("SELECT id, name FROM families ORDER BY name")
        ]
        by_id = {f["id"]: f for f in families}
        members = {}
        for m in conn.execute("SELECT id, name, family_id FROM members ORDER BY name"):
            member = {"id": m["id"], "name": m["name"], "family_id": m["family_id"], "accounts": []}
            members[m["id"]] = member
            by_id[m["family_id"]]["members"].append(member)

        holders = _account_holders(conn)
        unlinked = []
        for a in conn.execute("SELECT id, name, kind FROM accounts ORDER BY name"):
            account_holders = holders.get(a["id"])
            if not account_holders:
                unlinked.append(dict(a))
                continue
            for h in account_holders:
                members[h["member_id"]]["accounts"].append({**dict(a), "role": h["role"]})
            family = by_id[members[account_holders[0]["member_id"]]["family_id"]]
            family["accounts"].append({
                **dict(a),
                "primary_id": next((h["member_id"] for h in account_holders if h["role"] == "primary"), None),
                "joint_ids": [h["member_id"] for h in account_holders if h["role"] == "joint"],
            })
        return {"families": families, "unlinked": unlinked}
    finally:
        conn.close()


def list_members(
    family_id: Optional[int] = None, db_path: Optional[str | Path] = None
) -> list[sqlite3.Row]:
    """Members (id, name, family_id, family_name), by family then name;
    only *family_id*'s when given."""
    conn = connect(db_path)
    try:
        return conn.execute(
            """
            SELECT m.id, m.name, m.family_id, f.name AS family_name
            FROM members m JOIN families f ON f.id = m.family_id
            WHERE (? IS NULL OR m.family_id = ?)
            ORDER BY f.name, m.name
            """,
            (family_id, family_id),
        ).fetchall()
    finally:
        conn.close()


def scope_sql(
    column: str = "account_id",
    *,
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
) -> tuple[str, list[object]]:
    """``" AND ..."`` restricting *column* to a scope's accounts (§2.4).

    The filters intersect.  Each one is an ``IN (subquery)``, never a join,
    so a transaction is counted once however many holders its account has.
    Returns ``("", [])`` for "all accounts".
    """
    clauses: list[str] = []
    params: list[object] = []
    if account_id is not None:
        clauses.append(f"{column} = ?")
        params.append(account_id)
    if family_id is not None:
        clauses.append(
            f"{column} IN (SELECT am.account_id FROM account_members am"
            " JOIN members m ON m.id = am.member_id WHERE m.family_id = ?)"
        )
        params.append(family_id)
    if member_id is not None:
        role = "" if include_joint else " AND role = 'primary'"
        clauses.append(f"{column} IN (SELECT account_id FROM account_members WHERE member_id = ?{role})")
        params.append(member_id)
    return "".join(f" AND {c}" for c in clauses), params


# ---------------------------------------------------------------------------
# CSV mappings (§3.2)
# ---------------------------------------------------------------------------

def get_csv_mappings(
    account_id: Optional[int] = None, db_path: Optional[str | Path] = None
) -> list[CsvMapping]:
    """Saved mappings, for one account or all of them."""
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM csv_mappings"
        params: tuple = ()
        if account_id is not None:
            sql += " WHERE account_id = ?"
            params = (account_id,)
        return [CsvMapping.from_row(r) for r in conn.execute(sql + " ORDER BY id", params)]
    finally:
        conn.close()


def save_csv_mapping(
    account_id: int, mapping: CsvMapping, db_path: Optional[str | Path] = None
) -> int:
    """Insert or replace the account's mapping for this header. Returns its id."""
    conn = connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO csv_mappings
                (account_id, header_signature, skip_rows, date_col, date_format,
                 value_date_col, description_col, amount_col, debit_col, credit_col, negate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, header_signature) DO UPDATE SET
                skip_rows = excluded.skip_rows,
                date_col = excluded.date_col,
                date_format = excluded.date_format,
                value_date_col = excluded.value_date_col,
                description_col = excluded.description_col,
                amount_col = excluded.amount_col,
                debit_col = excluded.debit_col,
                credit_col = excluded.credit_col,
                negate = excluded.negate
            """,
            (
                account_id, mapping.header_signature, mapping.skip_rows, mapping.date_col,
                mapping.date_format, mapping.value_date_col, mapping.description_col,
                mapping.amount_col, mapping.debit_col, mapping.credit_col, int(mapping.negate),
            ),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM csv_mappings WHERE account_id = ? AND header_signature = ?",
            (account_id, mapping.header_signature),
        ).fetchone()[0]
    finally:
        conn.close()


def delete_csv_mapping(mapping_id: int, db_path: Optional[str | Path] = None) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute("DELETE FROM csv_mappings WHERE id = ?", (mapping_id,))
        conn.commit()
        return cur.rowcount > 0
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
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
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

    scope, scope_params = scope_sql(
        "t.account_id", account_id=account_id, family_id=family_id,
        member_id=member_id, include_joint=include_joint,
    )
    if scope:
        clauses.append(scope.removeprefix(" AND "))
        params.extend(scope_params)

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
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[TransactionRecord]:
    """Fetch transactions with optional filters, newest first."""
    conn = connect(db_path)
    where, params = _filters(
        year, month, category, search, uncategorized, transfers,
        account_id, family_id, member_id, include_joint,
    )
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
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
) -> int:
    """Count transactions with optional filters."""
    conn = connect(db_path)
    where, params = _filters(
        year, month, category, search, uncategorized, transfers,
        account_id, family_id, member_id, include_joint,
    )
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
