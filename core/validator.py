"""Deterministic validator for staged rows (app_spec.md §4).

Pure functions only: no database access.  ``core.staging`` loads rows,
calls these, and stores the resulting issue codes.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

ERROR = "error"
WARNING = "warning"
INFO = "info"

# Issue code -> level.  Rows with any error-level issue block commit.
LEVELS: dict[str, str] = {
    "bad_date": ERROR,
    "zero_amount": ERROR,
    "missing_desc": ERROR,
    "bad_value_date": WARNING,  # value_date was unparseable and has been cleared
    "not_in_source": WARNING,
    "duplicate_in_ledger": INFO,
}

MESSAGES: dict[str, str] = {
    "bad_date": "Date is not a valid YYYY-MM-DD date",
    "zero_amount": "Amount is zero or missing",
    "missing_desc": "Description is empty",
    "bad_value_date": "Value date was not a valid date and was cleared",
    "not_in_source": "Description not found in the statement text (possible hallucination)",
    "duplicate_in_ledger": "Already in the ledger; will be skipped on commit",
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_iso_date(value: Optional[str]) -> bool:
    """True for a real calendar date written exactly as ``YYYY-MM-DD``."""
    if not value or not _ISO_DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def normalize_for_match(text: str) -> str:
    """Case-fold and collapse whitespace, for the ``not_in_source`` check."""
    return " ".join(text.casefold().split())


def row_issues(
    transaction_date: Optional[str],
    value_date: Optional[str],
    raw_description: Optional[str],
    amount_minor: Optional[int],
    source_norm: Optional[str] = None,
) -> tuple[list[str], Optional[str]]:
    """Validate one row's own fields.

    Returns ``(issues, value_date)``: the issue codes in ``LEVELS`` order,
    and the value date to store (``None`` if it was invalid).
    ``not_in_source`` is only checked when *source_norm* (the statement
    text, already passed through ``normalize_for_match``) is given;
    ``duplicate_in_ledger`` needs the database and is added by the caller.
    """
    issues: list[str] = []
    if not is_iso_date(transaction_date):
        issues.append("bad_date")
    if not amount_minor:
        issues.append("zero_amount")
    desc = normalize_for_match(raw_description or "")
    if not desc:
        issues.append("missing_desc")
    if value_date is not None and not is_iso_date(value_date):
        issues.append("bad_value_date")
        value_date = None
    if source_norm is not None and desc and desc not in source_norm:
        issues.append("not_in_source")
    return issues, value_date


def has_errors(issues: Iterable[str]) -> bool:
    return any(LEVELS.get(code) == ERROR for code in issues)


def reconcile(
    opening_minor: Optional[int],
    closing_minor: Optional[int],
    amounts_minor: Iterable[int],
) -> tuple[Optional[int], Optional[int]]:
    """Check ``opening + Σ amounts == closing`` (§4 ``reconcile_mismatch``).

    Returns ``(reconciled, difference)``: ``(None, None)`` when either
    balance is unknown, else ``(1, 0)`` or ``(0, closing - expected)``.
    """
    if opening_minor is None or closing_minor is None:
        return None, None
    difference = closing_minor - (opening_minor + sum(amounts_minor))
    return (1 if difference == 0 else 0), difference
