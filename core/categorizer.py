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
    *,
    priority: int = 0,
    mark_transfer: bool = False,
    account_id: Optional[int] = None,
) -> int:
    """Create or update a rule and immediately apply it."""
    rule = CategoryRule(
        pattern=pattern,
        category=category,
        cleaned_merchant=cleaned_merchant,
        priority=priority,
        mark_transfer=mark_transfer,
        account_id=account_id,
    )
    rule_id = db.upsert_rule(rule, db_path)
    db.apply_rules(db_path)
    return rule_id


def remove_rule(rule_id: int, db_path: Optional[str | Path] = None) -> bool:
    """Delete a rule by id and re-run the engine, reverting rows it had set."""
    removed = db.delete_rule(rule_id, db_path)
    if removed:
        db.apply_rules(db_path)
    return removed


def preview_rule_matches(
    pattern: str, db_path: Optional[str | Path] = None
) -> int:
    """Count how many existing transactions would match a given pattern."""
    return db.count_rule_matches(pattern, db_path=db_path)
