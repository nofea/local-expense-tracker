"""Ollama API integration layer.

Provides functions to list available models and extract structured
transaction data from raw text using a local LLM served by Ollama.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import ollama as _ollama

from core.models import ExtractedTransaction, TransactionExtractionResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "llama3.1"

SYSTEM_PROMPT = """\
You are a deterministic financial data extraction engine. Analyze the \
provided raw text from a bank or credit card statement. Extract every \
transaction without omitting any.

You must output a valid JSON object containing an array of transactions \
matching this exact JSON schema:

{
  "type": "object",
  "properties": {
    "transactions": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "transaction_date": {"type": "string", "description": "YYYY-MM-DD format"},
          "value_date": {"type": "string", "description": "YYYY-MM-DD format if available, else null"},
          "raw_description": {"type": "string"},
          "amount": {"type": "number", "description": "Negative number for charges/debits, positive for payments/credits/income"}
        },
        "required": ["transaction_date", "raw_description", "amount"]
      }
    }
  }
}

Do not include markdown formatting, preambles, or explanations. \
Return ONLY the raw JSON block.\
"""


# ---------------------------------------------------------------------------
# Model listing (for the UI model selector)
# ---------------------------------------------------------------------------

def list_available_models() -> list[dict]:
    """Return a list of models currently available in the local Ollama instance.

    Each entry is a dict with at least ``name`` and ``size`` keys.
    Returns an empty list if Ollama is unreachable.
    """
    try:
        response = _ollama.list()
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


def is_ollama_available() -> bool:
    """Quick health check — can we reach the Ollama daemon?"""
    try:
        _ollama.list()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

def extract_transactions(
    raw_text: str,
    *,
    model: str = DEFAULT_MODEL,
) -> list[ExtractedTransaction]:
    """Send *raw_text* to the Ollama model and parse structured output.

    Uses the ``format`` parameter with the Pydantic JSON schema so
    Ollama constrains the LLM to valid JSON matching our schema.

    Returns a list of validated ``ExtractedTransaction`` objects.
    """
    schema = TransactionExtractionResult.model_json_schema()

    log.info("Sending %d chars to Ollama model '%s'", len(raw_text), model)

    response = _ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
        format=schema,
        options={"temperature": 0},
    )

    content = response["message"]["content"]
    log.debug("Raw LLM response: %s", content[:500])

    # Parse and validate
    try:
        result = TransactionExtractionResult.model_validate_json(content)
        return result.transactions
    except Exception as exc:
        log.error("Failed to parse LLM response: %s", exc)
        # Attempt a more lenient parse
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "transactions" in data:
                return [
                    ExtractedTransaction.model_validate(t)
                    for t in data["transactions"]
                ]
            elif isinstance(data, list):
                return [
                    ExtractedTransaction.model_validate(t)
                    for t in data
                ]
        except Exception:
            pass
        raise ValueError(f"Could not parse LLM output: {exc}") from exc
