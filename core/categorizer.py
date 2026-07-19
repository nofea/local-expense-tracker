"""Categorization rules engine.

Provides a thin convenience wrapper around ``database.apply_rules``
and helpers for matching / previewing rules against transactions.
"""

from __future__ import annotations

import logging
from typing import Optional
from pathlib import Path

from core import database as db
from core.models import CategoryRule

log = logging.getLogger(__name__)


def apply_all_rules(db_path: Optional[str | Path] = None) -> int:
    """Apply every stored rule to all transactions. Returns rows updated."""
    updated = db.apply_rules(db_path)
    log.info("Applied categorization rules — %d rows updated", updated)
    return updated


def add_rule(
    pattern: str,
    category: str,
    cleaned_merchant: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> int:
    """Create or update a rule and immediately apply it."""
    rule = CategoryRule(
        pattern=pattern, category=category, cleaned_merchant=cleaned_merchant
    )
    rule_id = db.upsert_rule(rule, db_path)
    db.apply_rules(db_path)
    return rule_id


def remove_rule(rule_id: int, db_path: Optional[str | Path] = None) -> bool:
    """Delete a rule by id."""
    return db.delete_rule(rule_id, db_path)


def preview_rule_matches(
    pattern: str, db_path: Optional[str | Path] = None
) -> int:
    """Count how many existing transactions would match a given pattern."""
    import sqlite3

    path = db_path or db.DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE raw_description LIKE ? COLLATE NOCASE",
            (f"%{pattern}%",),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()
