"""Pydantic models for the expense tracker."""

from __future__ import annotations

import hashlib
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from pydantic import BaseModel, Field, computed_field


# ---------------------------------------------------------------------------
# Transaction models
# ---------------------------------------------------------------------------

class TransactionRecord(BaseModel):
    """A single transaction as stored in the database (schema v2)."""

    id: str
    account_id: int
    statement_id: int
    transaction_date: str
    value_date: Optional[str] = None
    raw_description: str
    cleaned_merchant: Optional[str] = None
    amount_minor: int
    is_transfer: bool = False
    category: str = "Uncategorized"
    category_source: str = "default"
    rule_id: Optional[int] = None
    created_at: Optional[str] = None
    source_file: str = ""  # statements.file_name, joined in by queries

    @property
    def amount(self) -> float:
        """Amount in major units, for display only — never sum this."""
        return from_minor(self.amount_minor)


class ExtractedTransaction(BaseModel):
    """Schema returned by the LLM for a single extracted transaction.

    This is used both as the Pydantic validation model *and* passed to
    Ollama's ``format`` parameter (via ``model_json_schema()``) so the
    LLM is constrained to return this exact shape.
    """

    transaction_date: str = Field(
        ..., description="Transaction date in YYYY-MM-DD format"
    )
    value_date: Optional[str] = Field(
        None, description="Value/posting date in YYYY-MM-DD format if available, else null"
    )
    raw_description: str = Field(
        ..., description="Original unmodified description text from the statement"
    )
    amount: float = Field(
        ...,
        description=(
            "Negative number for charges/debits/expenses, "
            "positive for payments/credits/income"
        ),
    )


class TransactionExtractionResult(BaseModel):
    """Top-level structured output schema sent to Ollama."""

    transactions: list[ExtractedTransaction]


# ---------------------------------------------------------------------------
# Categorization rule
# ---------------------------------------------------------------------------

class CategoryRule(BaseModel):
    """A pattern-matching rule that maps a merchant keyword to a category."""

    id: Optional[int] = None
    pattern: str = Field(
        ..., description="Case-insensitive substring to match in raw_description"
    )
    category: str = Field(
        ..., description="Budget category to assign (e.g. Shopping, Food, Rent)"
    )
    cleaned_merchant: Optional[str] = Field(
        None, description="Optional normalized merchant name"
    )
    mark_transfer: bool = Field(
        False, description="Matching rows are flagged as transfers"
    )
    priority: int = Field(0, description="Higher wins when several rules match")
    account_id: Optional[int] = Field(
        None, description="Restrict the rule to one account (None = all)"
    )


# ---------------------------------------------------------------------------
# Analytics response models
# ---------------------------------------------------------------------------

class MonthlySummary(BaseModel):
    year: int
    month: int
    total_income: float = 0.0
    total_expense: float = 0.0
    net_savings: float = 0.0


class CategoryBreakdown(BaseModel):
    category: str
    total: float
    percentage: float


class MonthlyTrend(BaseModel):
    month_label: str  # e.g. "2025-06"
    income: float
    expense: float


class IngestionSummary(BaseModel):
    """Result of processing a single uploaded file."""

    source_file: str
    total_extracted: int = 0
    new_inserted: int = 0
    duplicates_skipped: int = 0
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minor units per major unit (paise per rupee).  Moves to config.toml with
# Appendix A item 20.
MINOR_UNITS = 2


def to_minor(amount: float | str | Decimal) -> int:
    """Convert a major-unit amount to integer minor units, rounding half up.

    Floats go through ``str`` first so that e.g. ``0.1`` is treated as the
    decimal 0.1 rather than its binary approximation.
    """
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return int((value * 10**MINOR_UNITS).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_minor(amount_minor: int) -> float:
    """Convert integer minor units to a major-unit float for display."""
    return amount_minor / 10**MINOR_UNITS


def normalize_description(raw_description: str) -> str:
    """Collapse runs of whitespace and trim; case is preserved (§5)."""
    return " ".join(raw_description.split())


def compute_transaction_id(
    account_id: int,
    transaction_date: str,
    raw_description: str,
    amount_minor: int,
    ordinal: int,
) -> str:
    """Deterministic SHA-256 transaction id (app_spec.md §5).

    *ordinal* is the row's 0-based index among rows in the same statement
    with identical (date, normalized description, amount).  This formula is
    part of the data contract: changing it needs a migration that
    recomputes every id.
    """
    payload = (
        f"{account_id}|{transaction_date}|{normalize_description(raw_description)}"
        f"|{amount_minor}|{ordinal}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assign_ordinals(keys: list[tuple]) -> list[int]:
    """Return the §5 ordinal for each key, in order.

    The ordinal counts earlier occurrences of the same key, so ``[a, b, a]``
    gives ``[0, 0, 1]``.
    """
    seen: dict[tuple, int] = {}
    ordinals: list[int] = []
    for key in keys:
        ordinals.append(seen.get(key, 0))
        seen[key] = ordinals[-1] + 1
    return ordinals
