"""Ollama API integration layer.

Provides functions to list available models and extract structured
transaction data from raw text using a local LLM served by Ollama.
Host, default model, per-chunk timeout and retries come from
``[llm]`` in config.toml (app_spec.md §1.4, §3.3).

The output schemas live only in ``core.models`` (Pydantic); they are
enforced through Ollama's ``format`` parameter and never restated in the
prompts.  Error messages and INFO+ logs never include model output, which
contains transaction descriptions (§9.5).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

try:
    import ollama as _ollama
except ImportError:
    _ollama = None

try:
    import httpx
except ImportError:  # httpx ships with ollama; this only guards odd installs
    httpx = None

from pydantic import ValidationError

from core.config import get_config
from core.models import ExtractedTransaction, StatementMetadata, TransactionExtractionResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults & Cache
# ---------------------------------------------------------------------------

DEFAULT_MODEL = get_config().llm.model

_ollama_status_cache: tuple[float, bool] = (0.0, False)
_CACHE_TTL = 3.0  # seconds cache to prevent repeated blocking probes

SYSTEM_PROMPT = """\
You extract transactions from the text of part of a bank account or credit \
card statement. Return every transaction row exactly once, in the order it \
appears.

For each transaction:
- transaction_date: the transaction date as YYYY-MM-DD. Statements often print \
dates day-first: read 03/04/2026 as 3 April 2026 unless the statement clearly \
uses month/day order.
- value_date: the value date as YYYY-MM-DD if the statement has a separate \
value-date column, otherwise null.
- raw_description: the narration exactly as printed, on one line. Do not \
shorten, translate or tidy it.
- amount: a signed number from the account holder's point of view, without \
currency symbols or thousands separators.
  - Bank account: withdrawals and debits are negative; deposits and credits \
are positive.
  - Credit card: purchases, fees, interest and charges are negative; payments \
to the card, refunds and cashback are positive.
  - With separate debit and credit columns, use whichever column holds the \
value. Never use the running balance column as the amount.

Do not output lines that are not transactions: opening or closing balance, \
balance brought or carried forward, totals and subtotals, page headers and \
footers, reward-point summaries. If there are no transactions, return an \
empty list.

Example input:
  Date        Narration             Withdrawal    Deposit      Balance
  01/03/2026  Opening Balance                                  10,000.00
  02/03/2026  UPI-SWIGGY-8812       450.50                      9,549.50
  05/03/2026  NEFT CR-ACME SALARY                 90,000.00    99,549.50
Example output:
  {"transactions": [
    {"transaction_date": "2026-03-02", "value_date": null, \
"raw_description": "UPI-SWIGGY-8812", "amount": -450.50},
    {"transaction_date": "2026-03-05", "value_date": null, \
"raw_description": "NEFT CR-ACME SALARY", "amount": 90000.00}
  ]}\
"""

METADATA_PROMPT = """\
You read the header and summary of a bank or credit card statement. Report \
the statement period and the opening and closing balances exactly as printed. \
Dates must be YYYY-MM-DD. Use null for anything the text does not show; never \
guess or compute a value.\
"""


class ExtractionTimeout(TimeoutError):
    """The model did not answer within ``llm.chunk_timeout_s``."""


class OutputInvalid(ValueError):
    """The model's output did not match the schema, even after retries."""


def _client(timeout: float):
    return _ollama.Client(host=get_config().llm.host, timeout=timeout)


def _unavailable_message() -> str:
    return (
        f"Ollama is not running or unreachable at {get_config().llm.host}. "
        "Please ensure Ollama is running ('ollama serve') before processing statements."
    )


# ---------------------------------------------------------------------------
# Health & Model listing (for the UI model selector)
# ---------------------------------------------------------------------------

def is_ollama_available(force: bool = False) -> bool:
    """Quick health check — can we reach the Ollama daemon?"""
    if _ollama is None:
        return False

    global _ollama_status_cache
    now = time.time()
    last_time, cached_val = _ollama_status_cache
    if not force and (now - last_time < _CACHE_TTL):
        return cached_val

    try:
        _client(timeout=1.5).list()
        status = True
    except Exception:
        status = False

    _ollama_status_cache = (now, status)
    return status


def list_available_models() -> list[dict]:
    """Return a list of models currently available in the local Ollama instance.

    Each entry is a dict with at least ``name`` and ``size`` keys.
    Returns an empty list if Ollama is unreachable.
    """
    if not is_ollama_available():
        return []

    try:
        response = _client(timeout=2.0).list()
        models = []
        for m in response.get("models", []):
            models.append(
                {
                    "name": m.get("name", m.get("model", "unknown")),
                    "size": m.get("size", 0),
                    "modified_at": m.get("modified_at", ""),
                }
            )
        return models
    except Exception as exc:
        log.warning("Could not list Ollama models: %s", exc)
        return []


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

def _chat(system: str, text: str, schema: dict, model: str) -> str:
    """One schema-constrained, deterministic chat call; returns the content.

    Raises :class:`ExtractionTimeout` when the call exceeds the configured
    per-chunk timeout.
    """
    timeout = get_config().llm.chunk_timeout_s
    try:
        response = _client(timeout=timeout).chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            format=schema,
            options={"temperature": 0},
        )
    except Exception as exc:
        if (httpx is not None and isinstance(exc, httpx.TimeoutException)) or isinstance(
            exc, TimeoutError
        ):
            raise ExtractionTimeout(f"no response from the model within {timeout:g}s") from None
        raise
    return response["message"]["content"]


def _parse_transactions(content: str) -> list[ExtractedTransaction]:
    """Strict schema parse, falling back to a bare list or a dict whose
    ``transactions`` key validates.  Raises :class:`OutputInvalid` with a
    message that does not echo the output."""
    try:
        return TransactionExtractionResult.model_validate_json(content).transactions
    except ValidationError as strict_error:
        try:
            data = json.loads(content)
            items = data["transactions"] if isinstance(data, dict) else data
            if isinstance(items, list):
                return [ExtractedTransaction.model_validate(t) for t in items]
        except (ValueError, KeyError, TypeError, ValidationError):
            pass
        raise OutputInvalid(
            f"model output did not match the transaction schema "
            f"({strict_error.error_count()} validation error(s))"
        ) from None


def extract_transactions(
    raw_text: str,
    *,
    model: Optional[str] = None,
) -> list[ExtractedTransaction]:
    """Send *raw_text* to the Ollama model and parse structured output.

    Output that fails schema validation is retried up to ``llm.max_retries``
    times.  Timeouts and connection errors are not retried: the chunk fails
    and the job moves on.
    """
    if _ollama is None:
        raise RuntimeError("Ollama Python package is not installed.")
    if not is_ollama_available():
        raise RuntimeError(_unavailable_message())

    model = model or get_config().llm.model
    schema = TransactionExtractionResult.model_json_schema()
    attempts = 1 + get_config().llm.max_retries

    for attempt in range(1, attempts + 1):
        log.info("Sending %d chars to Ollama model '%s' (attempt %d/%d)",
                 len(raw_text), model, attempt, attempts)
        content = _chat(SYSTEM_PROMPT, raw_text, schema, model)
        log.debug("Raw LLM response: %s", content[:500])
        try:
            return _parse_transactions(content)
        except OutputInvalid as exc:
            log.warning("Attempt %d/%d: %s", attempt, attempts, exc)
            if attempt == attempts:
                raise


def extract_statement_metadata(
    raw_text: str,
    *,
    model: Optional[str] = None,
) -> StatementMetadata:
    """Ask the model for the statement period and balances in *raw_text*."""
    if _ollama is None:
        raise RuntimeError("Ollama Python package is not installed.")
    content = _chat(
        METADATA_PROMPT,
        raw_text,
        StatementMetadata.model_json_schema(),
        model or get_config().llm.model,
    )
    try:
        return StatementMetadata.model_validate_json(content)
    except ValidationError as exc:
        raise OutputInvalid(
            f"model output did not match the metadata schema ({exc.error_count()} error(s))"
        ) from None
