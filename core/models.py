"""Pydantic models for the expense tracker."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, computed_field


# ---------------------------------------------------------------------------
# Transaction models
# ---------------------------------------------------------------------------

class TransactionRecord(BaseModel):
    """A single transaction as stored in the database."""

    id: str
    transaction_date: str
    value_date: Optional[str] = None
    raw_description: str
    cleaned_merchant: Optional[str] = None
    amount: float
    category: str = "Uncategorized"
    source_file: str = ""
    injected_at: Optional[str] = None


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

def compute_transaction_id(
    transaction_date: str, raw_description: str, amount: float
) -> str:
    """Deterministic SHA-256 hash for deduplication."""
    payload = f"{transaction_date}|{raw_description}|{amount:.2f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
