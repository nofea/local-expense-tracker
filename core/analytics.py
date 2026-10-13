"""Analytics queries for the expense tracker (app_spec.md §8).

Every function opens its own connection, runs a query, and returns
Pydantic model instances ready for serialisation.  Sums are computed on
integer ``amount_minor`` and converted to major units only at the end.
Transfers (``is_transfer = 1``) are excluded everywhere.  Every function
takes an optional *account_id*; ``None`` means all accounts.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

from core.database import connect as _conn
from core.models import CategoryBreakdown, MonthlySummary, MonthlyTrend, from_minor

_INCOME_EXPENSE = """
    SELECT
        COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income,
        COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expense
    FROM transactions
    WHERE transaction_date LIKE ? AND is_transfer = 0 AND (? IS NULL OR account_id = ?)
"""


# ---------------------------------------------------------------------------
# Monthly summary
# ---------------------------------------------------------------------------

def get_monthly_summary(
    year: int,
    month: int,
    db_path: Optional[str | Path] = None,
    *,
    account_id: Optional[int] = None,
) -> MonthlySummary:
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        row = conn.execute(_INCOME_EXPENSE, (prefix, account_id, account_id)).fetchone()
        income, expense = row["income"], row["expense"]
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
) -> list[CategoryBreakdown]:
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        rows = conn.execute(
            """
            SELECT category, -SUM(amount_minor) AS total
            FROM transactions
            WHERE transaction_date LIKE ? AND amount_minor < 0 AND is_transfer = 0
              AND (? IS NULL OR account_id = ?)
            GROUP BY category
            ORDER BY total DESC
            """,
            (prefix, account_id, account_id),
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
) -> list[MonthlyTrend]:
    """Return income/expense per month for the last *num_months* months
    ending with the month of *as_of* (default: today)."""
    conn = _conn(db_path)
    today = as_of or datetime.today()
    results: list[MonthlyTrend] = []
    try:
        for i in range(num_months - 1, -1, -1):
            # Walk back i calendar months (day arithmetic skips/repeats months)
            y, m = divmod(today.year * 12 + (today.month - 1) - i, 12)
            m += 1
            prefix = f"{y:04d}-{m:02d}-%"
            row = conn.execute(_INCOME_EXPENSE, (prefix, account_id, account_id)).fetchone()
            results.append(
                MonthlyTrend(
                    month_label=f"{y:04d}-{m:02d}",
                    income=from_minor(row["income"]),
                    expense=from_minor(row["expense"]),
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
) -> int:
    """Non-transfer rows in the month still in ``Uncategorized`` (§8)."""
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM transactions "
            "WHERE transaction_date LIKE ? AND category = 'Uncategorized' AND is_transfer = 0 "
            "AND (? IS NULL OR account_id = ?)",
            (f"{year:04d}-{month:02d}-%", account_id, account_id),
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
) -> list[dict]:
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        rows = conn.execute(
            """
            SELECT
                COALESCE(cleaned_merchant, raw_description) AS merchant,
                -SUM(amount_minor) AS total,
                COUNT(*) AS tx_count
            FROM transactions
            WHERE transaction_date LIKE ? AND amount_minor < 0 AND is_transfer = 0
              AND (? IS NULL OR account_id = ?)
            GROUP BY merchant
            ORDER BY total DESC
            LIMIT ?
            """,
            (prefix, account_id, account_id, limit),
        ).fetchall()
        return [{**dict(r), "total": from_minor(r["total"])} for r in rows]
    finally:
        conn.close()
