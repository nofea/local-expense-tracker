"""Analytics queries for the expense tracker (app_spec.md §8).

Every function opens its own connection, runs a query, and returns
Pydantic model instances ready for serialisation.  Sums are computed on
integer ``amount_minor`` and converted to major units only at the end.
Transfers (``is_transfer = 1``) are excluded everywhere.  Every function
takes the scope filters of §2.4 as keyword arguments (*account_id*,
*family_id*, *member_id*, *include_joint*); none given means all accounts.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

from core.database import connect as _conn
from core.database import list_members, scope_sql
from core.models import CategoryBreakdown, MonthlySummary, MonthlyTrend, from_minor

_INCOME_EXPENSE = """
    SELECT
        COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income,
        COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expense
    FROM transactions
    WHERE transaction_date LIKE ? AND is_transfer = 0
"""


def _income_expense(conn, prefix: str, scope: dict) -> tuple[int, int]:
    where, params = scope_sql(**scope)
    row = conn.execute(_INCOME_EXPENSE + where, (prefix, *params)).fetchone()
    return row["income"], row["expense"]


# ---------------------------------------------------------------------------
# Monthly summary
# ---------------------------------------------------------------------------

def get_monthly_summary(
    year: int,
    month: int,
    db_path: Optional[str | Path] = None,
    *,
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
) -> MonthlySummary:
    scope = dict(account_id=account_id, family_id=family_id, member_id=member_id, include_joint=include_joint)
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        income, expense = _income_expense(conn, prefix, scope)
        return MonthlySummary(
            year=year,
            month=month,
            total_income=from_minor(income),
            total_expense=from_minor(expense),
            net_savings=from_minor(income - expense),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Category breakdown
# ---------------------------------------------------------------------------

def get_category_breakdown(
    year: int,
    month: int,
    db_path: Optional[str | Path] = None,
    *,
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
) -> list[CategoryBreakdown]:
    where, params = scope_sql(
        account_id=account_id, family_id=family_id, member_id=member_id, include_joint=include_joint
    )
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        rows = conn.execute(
            f"""
            SELECT category, -SUM(amount_minor) AS total
            FROM transactions
            WHERE transaction_date LIKE ? AND amount_minor < 0 AND is_transfer = 0{where}
            GROUP BY category
            ORDER BY total DESC
            """,
            (prefix, *params),
        ).fetchall()

        grand_total = sum(r["total"] for r in rows) or 1
        return [
            CategoryBreakdown(
                category=r["category"],
                total=from_minor(r["total"]),
                percentage=round(r["total"] / grand_total * 100, 1),
            )
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Monthly trend
# ---------------------------------------------------------------------------

def get_monthly_trend(
    num_months: int = 6,
    db_path: Optional[str | Path] = None,
    *,
    as_of: Optional[date] = None,
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
) -> list[MonthlyTrend]:
    """Return income/expense per month for the last *num_months* months
    ending with the month of *as_of* (default: today)."""
    scope = dict(account_id=account_id, family_id=family_id, member_id=member_id, include_joint=include_joint)
    conn = _conn(db_path)
    today = as_of or datetime.today()
    results: list[MonthlyTrend] = []
    try:
        for i in range(num_months - 1, -1, -1):
            # Walk back i calendar months (day arithmetic skips/repeats months)
            y, m = divmod(today.year * 12 + (today.month - 1) - i, 12)
            m += 1
            prefix = f"{y:04d}-{m:02d}-%"
            income, expense = _income_expense(conn, prefix, scope)
            results.append(
                MonthlyTrend(
                    month_label=f"{y:04d}-{m:02d}",
                    income=from_minor(income),
                    expense=from_minor(expense),
                )
            )
    finally:
        conn.close()
    return results


# ---------------------------------------------------------------------------
# Uncategorized count
# ---------------------------------------------------------------------------

def get_uncategorized_count(
    year: int,
    month: int,
    db_path: Optional[str | Path] = None,
    *,
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
) -> int:
    """Non-transfer rows in the month still in ``Uncategorized`` (§8)."""
    where, params = scope_sql(
        account_id=account_id, family_id=family_id, member_id=member_id, include_joint=include_joint
    )
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM transactions "
            "WHERE transaction_date LIKE ? AND category = 'Uncategorized' AND is_transfer = 0" + where,
            (f"{year:04d}-{month:02d}-%", *params),
        ).fetchone()
        return row["cnt"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Top merchants
# ---------------------------------------------------------------------------

def get_top_merchants(
    year: int,
    month: int,
    limit: int = 10,
    db_path: Optional[str | Path] = None,
    *,
    account_id: Optional[int] = None,
    family_id: Optional[int] = None,
    member_id: Optional[int] = None,
    include_joint: bool = False,
) -> list[dict]:
    where, params = scope_sql(
        account_id=account_id, family_id=family_id, member_id=member_id, include_joint=include_joint
    )
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(cleaned_merchant, raw_description) AS merchant,
                -SUM(amount_minor) AS total,
                COUNT(*) AS tx_count
            FROM transactions
            WHERE transaction_date LIKE ? AND amount_minor < 0 AND is_transfer = 0{where}
            GROUP BY merchant
            ORDER BY total DESC
            LIMIT ?
            """,
            (prefix, *params, limit),
        ).fetchall()
        return [{**dict(r), "total": from_minor(r["total"])} for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-member table (§8.1)
# ---------------------------------------------------------------------------

def get_member_summaries(
    year: int,
    month: int,
    family_id: int,
    db_path: Optional[str | Path] = None,
    *,
    account_id: Optional[int] = None,
) -> list[dict]:
    """One row per member of *family_id*, by name: income, expenses and net
    savings (major units) on the accounts where they are the primary holder,
    plus how many accounts they hold jointly.  Every linked account has one
    primary holder, so the rows add up to the family's figures."""
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    results = []
    try:
        members = sorted(list_members(family_id, db_path), key=lambda m: m["name"])
        for m in members:
            income, expense = _income_expense(
                conn, prefix, dict(account_id=account_id, member_id=m["id"])
            )
            joint = conn.execute(
                "SELECT COUNT(*) FROM account_members WHERE member_id = ? AND role = 'joint'",
                (m["id"],),
            ).fetchone()[0]
            results.append({
                "member_id": m["id"],
                "name": m["name"],
                "income": from_minor(income),
                "expense": from_minor(expense),
                "net": from_minor(income - expense),
                "joint_accounts": joint,
            })
    finally:
        conn.close()
    return results
