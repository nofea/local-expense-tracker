"""Ingestion pipeline — extract text from PDFs/CSVs and parse via LLM."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

from core.models import StatementMetadata, to_minor
from core.ollama_client import DEFAULT_MODEL, extract_statement_metadata, extract_transactions

log = logging.getLogger(__name__)

# Max characters per LLM chunk (keep well within context window).
# Chunks never overlap: overlap re-emits rows, which per-statement dedup
# cannot tell apart from genuine repeats (app_spec.md §3.3, §5).
CHUNK_SIZE = 6000


class ExtractionError(Exception):
    """The file could not be turned into text at all; the job fails."""


class PasswordRequired(Exception):
    """The PDF is encrypted and the password is missing or wrong (§3.2).
    The job waits in ``needs_password``; the message never holds the password."""


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_pages_from_pdf(filepath: str | Path, password: Optional[str] = None) -> list[str]:
    """Extract the text of each non-empty page of a PDF using pdfplumber.

    Raises :class:`PasswordRequired` if the PDF is encrypted and *password*
    is missing or wrong.  The password is never logged (§9.4).
    """
    pages: list[str] = []
    try:
        with pdfplumber.open(str(filepath), password=password or "") as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
    except Exception as exc:
        # pdfplumber wraps pdfminer's errors in PdfminerException.
        cause = exc.args[0] if isinstance(exc, PdfminerException) and exc.args else exc
        if isinstance(cause, PDFPasswordIncorrect):
            raise PasswordRequired(
                "The password is incorrect." if password else "This PDF is password-protected."
            ) from None
        if isinstance(cause, PDFEncryptionError):
            raise ExtractionError("This PDF uses an encryption method that can't be read.") from None
        raise
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

@dataclass
class Extraction:
    """What the LLM pass produced for one file, ready for staging."""

    rows: list[dict] = field(default_factory=list)  # ExtractedTransaction dumps
    source_text: str = ""  # for the not_in_source check; never stored
    metadata: StatementMetadata = field(default_factory=StatementMetadata)
    chunk_errors: list[str] = field(default_factory=list)

    @property
    def opening_minor(self) -> Optional[int]:
        b = self.metadata.opening_balance
        return None if b is None else to_minor(b)

    @property
    def closing_minor(self) -> Optional[int]:
        b = self.metadata.closing_balance
        return None if b is None else to_minor(b)


def read_pages(
    filepath: str | Path, source_name: str, password: Optional[str] = None
) -> tuple[list[str], str]:
    """Extract ``(pages, header)`` from a PDF or CSV (§3.2).

    The parser is chosen by *source_name*'s extension, so a temp file can be
    read under the user's original name.  Raises :class:`ExtractionError`,
    or :class:`PasswordRequired` for a locked PDF.
    """
    ext = Path(source_name).suffix.lower()
    if ext == ".pdf":
        pages, header = extract_pages_from_pdf(filepath, password), ""
    elif ext == ".csv":
        header, body = extract_text_from_csv(filepath)
        pages = [body] if body.strip() else []
    else:
        raise ExtractionError(f"Unsupported file type: {ext}")
    if not any(p.strip() for p in pages):
        raise ExtractionError("No text could be extracted: no text layer (scanned PDF?) or an empty file.")
    return pages, header


def extract_statement(
    pages: list[str],
    *,
    header: str = "",
    model: str = DEFAULT_MODEL,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Extraction:
    """Run the LLM over the text in non-overlapping chunks (§3.3).

    A failing chunk is recorded in ``chunk_errors`` and does not stop the
    others.  *on_progress(done, total)* is called before the first chunk and
    after each one.
    """
    chunks = _chunk_pages(pages, header=header)
    result = Extraction(source_text="\n\n".join([header, *pages]))
    log.info("Split into %d chunk(s)", len(chunks))
    if on_progress:
        on_progress(0, len(chunks))

    for i, chunk in enumerate(chunks):
        try:
            extracted = extract_transactions(chunk, model=model)
            result.rows.extend(tx.model_dump() for tx in extracted)
            log.info("Chunk %d/%d: extracted %d transactions", i + 1, len(chunks), len(extracted))
        except Exception as exc:
            msg = f"Chunk {i + 1}/{len(chunks)} failed: {exc or type(exc).__name__}"
            log.error(msg)
            result.chunk_errors.append(msg)
        if on_progress:
            on_progress(i + 1, len(chunks))

    result.metadata = _statement_metadata(chunks, model)
    return result


def _statement_metadata(chunks: list[str], model: str) -> StatementMetadata:
    """Ask the first and last chunks for period and balances.

    Values from the first chunk win, except that the closing balance and
    period end prefer the last chunk, where statement summaries usually sit.
    Failures only cost the metadata; the user can enter it in review.
    """
    def ask(chunk: str) -> StatementMetadata:
        try:
            return extract_statement_metadata(chunk, model=model)
        except Exception as exc:
            log.warning("Statement metadata extraction failed: %s", exc)
            return StatementMetadata()

    if not chunks:
        return StatementMetadata()
    first = ask(chunks[0])
    if len(chunks) == 1:
        return first
    last = ask(chunks[-1])
    return StatementMetadata(
        period_start=first.period_start or last.period_start,
        period_end=last.period_end or first.period_end,
        opening_balance=first.opening_balance if first.opening_balance is not None else last.opening_balance,
        closing_balance=last.closing_balance if last.closing_balance is not None else first.closing_balance,
    )
