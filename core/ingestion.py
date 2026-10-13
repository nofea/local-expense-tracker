"""Ingestion pipeline — extract text from PDFs/CSVs and parse via LLM."""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from pathlib import Path
from typing import Optional

import pdfplumber

from core.database import find_statement, get_or_create_account, import_statement
from core.models import IngestionSummary
from core.ollama_client import DEFAULT_MODEL, extract_transactions

log = logging.getLogger(__name__)

# Max characters per LLM chunk (keep well within context window).
# Chunks never overlap: overlap re-emits rows, which per-statement dedup
# cannot tell apart from genuine repeats (app_spec.md §3.3, §5).
CHUNK_SIZE = 6000


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_pages_from_pdf(filepath: str | Path) -> list[str]:
    """Extract the text of each non-empty page of a PDF using pdfplumber."""
    pages: list[str] = []
    with pdfplumber.open(str(filepath)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return pages


def extract_text_from_csv(filepath: str | Path) -> tuple[str, str]:
    """Read a CSV file and format it as text for the LLM.

    Returns ``(header, body)``: the header row (plus a separator line) and
    the data rows, one per line, ``|``-joined.
    """
    path = Path(filepath)
    raw = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.reader(io.StringIO(raw))

    header = ""
    lines: list[str] = []
    for i, row in enumerate(reader):
        if i == 0:
            header = " | ".join(row) + "\n" + "-" * 60
        else:
            lines.append(" | ".join(row))
    return header, "\n".join(lines)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_pages(pages: list[str], header: str = "") -> list[str]:
    """Pack whole pages into chunks of at most ~CHUNK_SIZE characters.

    A page that is too large on its own is split on line boundaries, so a
    row is never cut in half.  A single line longer than CHUNK_SIZE becomes
    its own chunk.  *header* (e.g. the CSV header row) is prepended to every
    chunk so the LLM always sees the column names; it is not a transaction,
    so repeating it cannot create duplicates.
    """
    prefix = f"{header}\n" if header else ""
    budget = max(1, CHUNK_SIZE - len(prefix))

    # Break oversized pages into line-aligned pieces first.
    units: list[str] = []
    for page in pages:
        if len(page) <= budget:
            units.append(page)
            continue
        piece: list[str] = []
        size = 0
        for line in page.splitlines():
            if piece and size + len(line) + 1 > budget:
                units.append("\n".join(piece))
                piece, size = [], 0
            piece.append(line)
            size += len(line) + 1
        if piece:
            units.append("\n".join(piece))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and size + len(unit) + 2 > budget:
            chunks.append(prefix + "\n\n".join(current))
            current, size = [], 0
        current.append(unit)
        size += len(unit) + 2
    if current:
        chunks.append(prefix + "\n\n".join(current))
    return chunks


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_file(
    filepath: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    db_path: Optional[str | Path] = None,
    source_name: Optional[str] = None,
) -> IngestionSummary:
    """Full ingestion pipeline for a single file.

    *source_name* is the display name recorded as ``source_file`` (and used
    to pick the parser by extension); it defaults to the file's own name,
    so callers can process a temp file under the user's original name.

    0. Reject a file already imported into the account (same SHA-256)
    1. Extract raw text (PDF or CSV)
    2. Chunk the text
    3. Send each chunk to the LLM for structured extraction
    4. Record a statement and insert its rows (§5 dedup), then apply
       categorization rules — all in one transaction

    Until the review step (app_spec.md §3.5) exists, everything goes into
    the ``Default`` account and is committed straight away.
    """
    filepath = Path(filepath)
    source_name = source_name or filepath.name
    summary = IngestionSummary(source_file=source_name)

    # Step 0 — re-upload check (§3.1)
    file_sha256 = hashlib.sha256(filepath.read_bytes()).hexdigest()
    account_id = get_or_create_account(db_path=db_path)
    existing = find_statement(account_id, file_sha256, db_path)
    if existing is not None:
        summary.errors.append(
            f"This file was already imported as '{existing['file_name']}' "
            f"on {existing['created_at']}."
        )
        return summary

    # Step 1 — text extraction
    ext = Path(source_name).suffix.lower()
    if ext == ".pdf":
        pages = extract_pages_from_pdf(filepath)
        header = ""
    elif ext == ".csv":
        header, body = extract_text_from_csv(filepath)
        pages = [body] if body.strip() else []
    else:
        summary.errors.append(f"Unsupported file type: {ext}")
        return summary

    if not any(p.strip() for p in pages):
        summary.errors.append("No text could be extracted from the file.")
        return summary

    log.info("Extracted %d chars from %s", sum(map(len, pages)), source_name)

    # Step 2 — chunk
    chunks = _chunk_pages(pages, header=header)
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

    # Step 4 — record the statement, insert rows, apply rules
    _, inserted, skipped, invalid = import_statement(
        all_transactions,
        account_id=account_id,
        file_name=source_name,
        file_sha256=file_sha256,
        extractor="llm",
        model=model,
        db_path=db_path,
    )
    summary.new_inserted = inserted
    summary.duplicates_skipped = skipped
    if invalid:
        summary.errors.append(f"{invalid} row(s) with a zero amount were skipped.")

    log.info(
        "Ingestion complete for %s: %d extracted, %d new, %d dupes",
        source_name,
        summary.total_extracted,
        summary.new_inserted,
        summary.duplicates_skipped,
    )
    return summary
