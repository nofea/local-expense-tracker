"""Analytics queries for the expense tracker.

Every function opens its own connection, runs a query, and returns
Pydantic model instances ready for serialisation.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.database import DEFAULT_DB_PATH
from core.models import CategoryBreakdown, MonthlySummary, MonthlyTrend


def _conn(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    path = str(db_path or DEFAULT_DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Monthly summary
# ---------------------------------------------------------------------------

def get_monthly_summary(
    year: int, month: int, db_path: Optional[str | Path] = None
) -> MonthlySummary:
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END), 0) AS expense
            FROM transactions
            WHERE transaction_date LIKE ?
            """,
            (prefix,),
        ).fetchone()
        income = row["income"]
        expense = abs(row["expense"])
        return MonthlySummary(
            year=year,
            month=month,
            total_income=round(income, 2),
            total_expense=round(expense, 2),
            net_savings=round(income - expense, 2),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Category breakdown
# ---------------------------------------------------------------------------

def get_category_breakdown(
    year: int, month: int, db_path: Optional[str | Path] = None
) -> list[CategoryBreakdown]:
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        rows = conn.execute(
            """
            SELECT category, SUM(ABS(amount)) AS total
            FROM transactions
            WHERE transaction_date LIKE ? AND amount < 0
            GROUP BY category
            ORDER BY total DESC
            """,
            (prefix,),
        ).fetchall()

        grand_total = sum(r["total"] for r in rows) or 1
        return [
            CategoryBreakdown(
                category=r["category"],
                total=round(r["total"], 2),
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
    num_months: int = 6, db_path: Optional[str | Path] = None
) -> list[MonthlyTrend]:
    """Return income/expense per month for the last *num_months* months."""
    conn = _conn(db_path)
    today = datetime.today()
    results: list[MonthlyTrend] = []
    try:
        for i in range(num_months - 1, -1, -1):
            # Walk back i months
            dt = today.replace(day=1) - timedelta(days=i * 30)
            y, m = dt.year, dt.month
            prefix = f"{y:04d}-{m:02d}-%"
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS income,
                    COALESCE(SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS expense
                FROM transactions
                WHERE transaction_date LIKE ?
                """,
                (prefix,),
            ).fetchone()
            results.append(
                MonthlyTrend(
                    month_label=f"{y:04d}-{m:02d}",
                    income=round(row["income"], 2),
                    expense=round(row["expense"], 2),
                )
            )
    finally:
        conn.close()
    return results


# ---------------------------------------------------------------------------
# Uncategorized count
# ---------------------------------------------------------------------------

def get_uncategorized_count(db_path: Optional[str | Path] = None) -> int:
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM transactions WHERE category = 'Uncategorized'"
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
) -> list[dict]:
    conn = _conn(db_path)
    prefix = f"{year:04d}-{month:02d}-%"
    try:
        rows = conn.execute(
            """
            SELECT
                COALESCE(cleaned_merchant, raw_description) AS merchant,
                SUM(ABS(amount)) AS total,
                COUNT(*) AS tx_count
            FROM transactions
            WHERE transaction_date LIKE ? AND amount < 0
            GROUP BY merchant
            ORDER BY total DESC
            LIMIT ?
            """,
            (prefix, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
