"""Deterministic CSV parsing with a saved column mapping (app_spec.md §3.2).

A CSV statement whose header matches a ``csv_mappings`` row of its account
is parsed here, with no LLM call.  Matching is by header signature: the
SHA-256 of the normalized header row found ``skip_rows`` lines into the
file, so a bank's preamble lines (account number, period, …) are skipped.

Parsing never drops a data row: a date that doesn't match the format or an
amount that doesn't parse is passed through for the validator (§4) to flag
in review.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

# strptime formats offered in the mapping form, with an example of each.
DATE_FORMATS: dict[str, str] = {
    "%Y-%m-%d": "2025-03-31",
    "%d/%m/%Y": "31/03/2025",
    "%d-%m-%Y": "31-03-2025",
    "%d.%m.%Y": "31.03.2025",
    "%d/%m/%y": "31/03/25",
    "%d-%m-%y": "31-03-25",
    "%d %b %Y": "31 Mar 2025",
    "%d-%b-%Y": "31-Mar-2025",
    "%d-%b-%y": "31-Mar-25",
    "%d %b %y": "31 Mar 25",
    "%d %B %Y": "31 March 2025",
    "%m/%d/%Y": "03/31/2025",
    "%Y/%m/%d": "2025/03/31",
}


class CsvMappingError(ValueError):
    """A mapping doesn't fit the file; the message is shown to the user."""


@dataclass
class CsvMapping:
    header_signature: str
    skip_rows: int
    date_col: str
    date_format: str
    description_col: str
    value_date_col: Optional[str] = None
    amount_col: Optional[str] = None  # a single signed column, or
    debit_col: Optional[str] = None  # separate money-out and
    credit_col: Optional[str] = None  # money-in columns
    negate: bool = False
    id: Optional[int] = None
    account_id: Optional[int] = None

    @classmethod
    def from_row(cls, row) -> "CsvMapping":
        data = dict(row)
        data["negate"] = bool(data["negate"])
        return cls(**data)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_rows(path: str | Path) -> list[list[str]]:
    """All rows of a CSV file, cells stripped.  The delimiter is sniffed."""
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(raw), dialect)]


def _width(row: list[str]) -> int:
    """Number of cells up to the last non-empty one."""
    n = len(row)
    while n and not row[n - 1]:
        n -= 1
    return n


def detect_header(rows: list[list[str]]) -> int:
    """Guess how many preamble lines precede the header row.

    Data rows share one width; preamble lines (``Account No,1234``) are
    narrower.  The header is the first row at least as wide as the most
    common width.
    """
    widths = [_width(r) for r in rows]
    common = Counter(w for w in widths if w >= 2).most_common(1)
    if not common:
        return 0
    modal = common[0][0]
    return next(i for i, w in enumerate(widths) if w >= modal)


def header_signature(header: list[str]) -> str:
    """SHA-256 of the header row, case- and whitespace-insensitive."""
    cells = [" ".join(c.split()).casefold() for c in header[: _width(header)]]
    return hashlib.sha256("|".join(cells).encode("utf-8")).hexdigest()


def find_mapping(rows: list[list[str]], mappings: list[CsvMapping]) -> Optional[CsvMapping]:
    """The first saved mapping whose header signature matches this file."""
    for m in mappings:
        if m.skip_rows < len(rows) and header_signature(rows[m.skip_rows]) == m.header_signature:
            return m
    return None


def columns(header: list[str]) -> list[str]:
    """Distinct non-empty column names, in file order (the form's choices)."""
    return list(dict.fromkeys(c for c in header if c))


# ---------------------------------------------------------------------------
# Guessing a mapping for the form
# ---------------------------------------------------------------------------

_DESC_WORDS = ("description", "narration", "particular", "details", "remark", "memo", "payee", "merchant")
_DEBIT_WORDS = ("debit", "withdrawal", "paid out", "money out")
_CREDIT_WORDS = ("credit", "deposit", "paid in", "money in")


def _find(cols: list[str], words: tuple[str, ...], exclude: tuple[str, ...] = (), taken=()) -> Optional[str]:
    for col in cols:
        name = col.casefold()
        tokens = set(re.split(r"\W+", name))
        if col in taken or any(x in name for x in exclude):
            continue
        if any((w in tokens) if len(w) <= 3 else (w in name) for w in words):
            return col
    return None


def guess_mapping(header: list[str], samples: list[list[str]]) -> dict:
    """Form defaults from the column names and a few data rows."""
    cols = columns(header)
    date_col = _find(cols, ("date",), exclude=("value",)) or _find(cols, ("txn", "posted"))
    value_date_col = _find(cols, ("value",), taken=(date_col,))
    taken = (date_col, value_date_col)
    description_col = _find(cols, _DESC_WORDS, taken=taken) or _find(cols, ("transaction",), taken=taken)
    taken += (description_col,)
    debit_col = _find(cols, _DEBIT_WORDS + ("dr",), taken=taken)
    credit_col = _find(cols, _CREDIT_WORDS + ("cr",), taken=taken + (debit_col,))
    amount_col = None if (debit_col and credit_col) else _find(cols, ("amount", "amt"), taken=taken)

    date_format = "%Y-%m-%d"
    if date_col:
        i = header.index(date_col)
        values = [r[i] for r in samples if i < len(r) and r[i]]
        date_format = next(
            (f for f in DATE_FORMATS if values and all(parse_date(v, f) for v in values)), date_format
        )
    return {
        "date_col": date_col,
        "date_format": date_format,
        "value_date_col": value_date_col,
        "description_col": description_col,
        "amount_mode": "split" if (debit_col and credit_col) else "single",
        "amount_col": amount_col,
        "debit_col": debit_col,
        "credit_col": credit_col,
        "negate": False,
    }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_date(text: str, fmt: str) -> Optional[str]:
    """``text`` as ISO ``YYYY-MM-DD``, or None.  A trailing time
    (``31/03/2025 10:15``) is ignored."""
    text = " ".join(text.split())
    for candidate in (text, " ".join(text.split(" ")[: fmt.count(" ") + 1])):
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(text: str) -> Optional[Decimal]:
    """Parse ``-1,234.50``, ``(1,234.50)``, ``₹ 99``, ``500.00 Dr`` or
    ``500.00 Cr``; None when blank or unparseable."""
    s = text.strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    suffix = s.rstrip(". ").upper()
    if suffix.endswith("DR"):
        negative, s = not negative, s.rstrip(". ")[:-2]
    elif suffix.endswith("CR"):
        s = s.rstrip(". ")[:-2]
    s = re.sub(r"[^\d.+-]", "", s)
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if negative else value


def validate(rows: list[list[str]], mapping: CsvMapping) -> None:
    """Raise :class:`CsvMappingError` unless *mapping* fits *rows*."""
    if not 0 <= mapping.skip_rows < len(rows):
        raise CsvMappingError("The header row is past the end of the file.")
    cols = columns(rows[mapping.skip_rows])
    if not mapping.date_col or not mapping.description_col:
        raise CsvMappingError("Choose the date and description columns.")
    if mapping.amount_col is None and not (mapping.debit_col and mapping.credit_col):
        raise CsvMappingError("Choose an amount column, or both a debit and a credit column.")
    for col in (mapping.date_col, mapping.value_date_col, mapping.description_col,
                mapping.amount_col, mapping.debit_col, mapping.credit_col):
        if col is not None and col not in cols:
            raise CsvMappingError(f"There is no column named '{col}' in this file.")
    if mapping.date_format not in DATE_FORMATS:
        raise CsvMappingError("Choose a date format from the list.")
    data = [r for r in rows[mapping.skip_rows + 1 :] if any(r)]
    if not data:
        raise CsvMappingError("No data rows were found below the header row.")
    i = rows[mapping.skip_rows].index(mapping.date_col)
    if not any(i < len(r) and parse_date(r[i], mapping.date_format) for r in data):
        example = DATE_FORMATS[mapping.date_format]
        raise CsvMappingError(
            f"None of the values in '{mapping.date_col}' look like {example}. Choose another date format."
        )


def parse(rows: list[list[str]], mapping: CsvMapping) -> list[dict]:
    """Turn the data rows below the header into staging rows.

    Each row has ``transaction_date``, ``value_date``, ``raw_description``
    and ``amount`` (a Decimal in major units, or None), like an LLM row.
    Blank lines are skipped; nothing else is.
    """
    header = rows[mapping.skip_rows]
    index: dict[str, int] = {}
    for i, name in enumerate(header):
        index.setdefault(name, i)

    def cell(row: list[str], col: Optional[str]) -> str:
        i = index.get(col) if col else None
        return row[i] if i is not None and i < len(row) else ""

    out: list[dict] = []
    for row in rows[mapping.skip_rows + 1 :]:
        if not any(row):
            continue
        if mapping.amount_col is not None:
            amount = parse_amount(cell(row, mapping.amount_col))
        else:
            debit = parse_amount(cell(row, mapping.debit_col))
            credit = parse_amount(cell(row, mapping.credit_col))
            amount = None if debit is None and credit is None else abs(credit or 0) - abs(debit or 0)
        if amount is not None and mapping.negate:
            amount = -amount
        raw_date = cell(row, mapping.date_col)
        raw_value_date = cell(row, mapping.value_date_col)
        out.append({
            # Unparseable dates are kept as-is so review flags them (§4).
            "transaction_date": parse_date(raw_date, mapping.date_format) or raw_date,
            "value_date": (parse_date(raw_value_date, mapping.date_format) or raw_value_date) or None,
            "raw_description": cell(row, mapping.description_col),
            "amount": amount,
        })
    return out


def parse_file(path: str | Path, mapping: CsvMapping) -> list[dict]:
    return parse(read_rows(path), mapping)
