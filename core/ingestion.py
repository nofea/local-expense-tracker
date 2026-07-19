"""Ingestion pipeline — extract text from PDFs/CSVs and parse via LLM."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Optional

import pdfplumber

from core.database import apply_rules, insert_transactions
from core.models import IngestionSummary
from core.ollama_client import DEFAULT_MODEL, extract_transactions

log = logging.getLogger(__name__)

# Max characters per LLM chunk (keep well within context window)
CHUNK_SIZE = 3000
CHUNK_OVERLAP = 300


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text_from_pdf(filepath: str | Path) -> str:
    """Extract all text from a PDF using pdfplumber."""
    pages: list[str] = []
    with pdfplumber.open(str(filepath)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)


def extract_text_from_csv(filepath: str | Path) -> str:
    """Read a CSV file and format it as a text block for the LLM."""
    path = Path(filepath)
    raw = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.reader(io.StringIO(raw))

    lines: list[str] = []
    header: list[str] | None = None
    for i, row in enumerate(reader):
        if i == 0:
            header = row
            lines.append(" | ".join(header))
            lines.append("-" * 60)
        else:
            lines.append(" | ".join(row))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks of ~CHUNK_SIZE characters."""
    if len(text) <= CHUNK_SIZE:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        chunks.append(chunk)
        start = end - CHUNK_OVERLAP
    return chunks


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_file(
    filepath: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    db_path: Optional[str | Path] = None,
) -> IngestionSummary:
    """Full ingestion pipeline for a single file.

    1. Extract raw text (PDF or CSV)
    2. Chunk the text
    3. Send each chunk to the LLM for structured extraction
    4. Validate, deduplicate, and insert into the database
    5. Apply categorization rules
    """
    filepath = Path(filepath)
    summary = IngestionSummary(source_file=filepath.name)

    # Step 1 — text extraction
    ext = filepath.suffix.lower()
    if ext == ".pdf":
        raw_text = extract_text_from_pdf(filepath)
    elif ext == ".csv":
        raw_text = extract_text_from_csv(filepath)
    else:
        summary.errors.append(f"Unsupported file type: {ext}")
        return summary

    if not raw_text.strip():
        summary.errors.append("No text could be extracted from the file.")
        return summary

    log.info("Extracted %d chars from %s", len(raw_text), filepath.name)

    # Step 2 — chunk
    chunks = _chunk_text(raw_text)
    log.info("Split into %d chunk(s)", len(chunks))

    # Step 3 — LLM extraction per chunk
    all_transactions: list[dict] = []
    for i, chunk in enumerate(chunks):
        try:
            extracted = extract_transactions(chunk, model=model)
            for tx in extracted:
                all_transactions.append(tx.model_dump())
            log.info("Chunk %d/%d: extracted %d transactions", i + 1, len(chunks), len(extracted))
        except Exception as exc:
            msg = f"Chunk {i + 1}/{len(chunks)} failed: {exc}"
            log.error(msg)
            summary.errors.append(msg)

    summary.total_extracted = len(all_transactions)

    if not all_transactions:
        if not summary.errors:
            summary.errors.append("LLM returned no transactions.")
        return summary

    # Step 4 — insert into DB
    inserted, skipped = insert_transactions(
        all_transactions, source_file=filepath.name, db_path=db_path
    )
    summary.new_inserted = inserted
    summary.duplicates_skipped = skipped

    # Step 5 — apply categorization rules
    try:
        apply_rules(db_path)
    except Exception as exc:
        log.warning("Rule application failed: %s", exc)

    log.info(
        "Ingestion complete for %s: %d extracted, %d new, %d dupes",
        filepath.name,
        summary.total_extracted,
        summary.new_inserted,
        summary.duplicates_skipped,
    )
    return summary
