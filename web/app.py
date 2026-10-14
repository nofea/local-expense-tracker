"""FastAPI web application for the expense tracker."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import database as db
from core import csv_import, jobs, staging, validator
from core.analytics import (
    get_category_breakdown,
    get_monthly_summary,
    get_monthly_trend,
    get_uncategorized_count,
    get_top_merchants,
)
from core.categorizer import add_rule, apply_all_rules, remove_rule
from core.config import get_config
from core.models import CategoryRule, format_money, minor_to_input
from core.ollama_client import is_ollama_available, list_available_models

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WEB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _WEB_DIR.parent

# Upload limits (app_spec.md §9.6)
_ALLOWED_EXTENSIONS = {".pdf", ".csv"}
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

def _global_template_context(request: Request) -> dict:
    """Inject Ollama connectivity state and config into all template renders."""
    status = is_ollama_available()
    return {
        "ollama_status": status,
        "ollama_available": status,
        "config": get_config(),
    }


app = FastAPI(title="Expense Tracker", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
templates = Jinja2Templates(
    directory=str(_WEB_DIR / "templates"),
    context_processors=[_global_template_context],
)

# money: integer minor units -> "+₹1,234.50"; currency: major-unit floats
# (analytics results) -> "₹1,235" with *decimals* places; major: minor
# units -> editable "1234.50".  Symbol and decimals come from config.toml.
templates.env.filters["money"] = lambda v, signed=True: format_money(v, minor=True, signed=signed)
templates.env.filters["currency"] = lambda v, decimals=None, signed=False: format_money(
    v, decimals=decimals, signed=signed
)
templates.env.filters["major"] = minor_to_input
templates.env.globals["issue_levels"] = validator.LEVELS
templates.env.globals["issue_messages"] = validator.MESSAGES

# Ensure DB is initialised on startup, then recover interrupted imports and
# start the ingestion worker (only the web server runs jobs).
db.init_db()
jobs.start()


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

class _UploadTooLarge(Exception):
    pass


def _sanitize_filename(name: Optional[str]) -> str:
    """Reduce a client-supplied filename to a safe display name."""
    # Treat backslashes as separators too, so Windows-style paths collapse.
    base = Path((name or "").replace("\\", "/")).name
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C").strip()
    if base in {"", ".", ".."}:
        return "upload"
    return base[:255]


def _now() -> tuple[int, int]:
    """Return (year, month) for the current date."""
    today = datetime.today()
    return today.year, today.month


def _valid_month(year: Optional[int], month: Optional[int]) -> tuple[int, int]:
    """Return (year, month), falling back to the current month when either
    is missing or out of range, so bad query params never cause a 500."""
    now_y, now_m = _now()
    y, m = year or now_y, month or now_m
    if not (1 <= y <= 9999 and 1 <= m <= 12):
        return now_y, now_m
    return y, m


# ---------------------------------------------------------------------------
# Routes — System & Status
# ---------------------------------------------------------------------------

@app.get("/api/ollama-status", response_class=HTMLResponse)
async def ollama_status_badge(request: Request):
    """Return partial HTML for Ollama status indicator badge (used by HTMX)."""
    return templates.TemplateResponse(
        request=request,
        name="partials/ollama_status.html",
        context={
            "request": request,
            "ollama_status": is_ollama_available(force=True),
        },
    )


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    year: Optional[int] = None,
    month: Optional[int] = None,
    account_id: Optional[str] = None,
):
    y, m = _valid_month(year, month)
    account = _account_param(account_id)
    summary = get_monthly_summary(y, m, account_id=account)
    breakdown = get_category_breakdown(y, m, account_id=account)
    trend = get_monthly_trend(6, account_id=account)
    uncategorized = get_uncategorized_count(y, m, account_id=account)
    top_merchants = get_top_merchants(y, m, account_id=account)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "summary": summary,
            "breakdown": [b.model_dump() for b in breakdown],
            "trend": [t.model_dump() for t in trend],
            "uncategorized": uncategorized,
            "top_merchants": top_merchants,
            "year": y,
            "month": m,
            "month_name": datetime(y, m, 1).strftime("%B %Y"),
            "accounts": db.list_accounts(),
            "account_id": account,
        },
    )


# ---------------------------------------------------------------------------
# Routes — Upload
# ---------------------------------------------------------------------------

@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    models = list_available_models()
    ollama_ok = is_ollama_available()
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={
            "request": request,
            "models": models,
            "accounts": db.list_accounts(),
            "account_kinds": db.ACCOUNT_KINDS,
            "ollama_available": ollama_ok,
            "ollama_status": ollama_ok,
        },
    )


def _upload_failed(request: Request, message: str, existing_id: Optional[int] = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="partials/upload_result.html",
        context={"request": request, "error": message, "existing_id": existing_id},
    )


def _ollama_unreachable() -> str:
    return (
        f"Ollama is not running or unreachable at {get_config().llm.host}. "
        "Please ensure Ollama is installed and running ('ollama serve') before processing."
    )


def _resolve_account(account_id: str, new_account_name: str, new_account_kind: str) -> int:
    """The account picked on the upload form (§3.1 step 1): a new name wins,
    then the selected id; with neither, the ``Default`` account."""
    if new_account_name.strip():
        name = " ".join(new_account_name.split())
        existing = next((a for a in db.list_accounts() if a["name"] == name), None)
        if existing is not None:
            return existing["id"]
        return db.create_account(name, new_account_kind)
    if account_id.strip():
        try:
            wanted = int(account_id)
        except ValueError:
            wanted = None
        if not any(a["id"] == wanted for a in db.list_accounts()):
            raise db.AccountError("The selected account no longer exists.")
        return wanted
    return db.get_or_create_account()


def _file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            sha.update(chunk)
    return sha.hexdigest()


@app.post("/upload", response_class=HTMLResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(""),
    password: str = Form(""),
    account_id: str = Form(""),
    new_account_name: str = Form(""),
    new_account_kind: str = Form("bank"),
):
    # The client filename is display text only — never a path (§3.1, §9.6).
    source_name = _sanitize_filename(file.filename)
    ext = Path(source_name).suffix.lower()

    if ext not in _ALLOWED_EXTENSIONS:
        return _upload_failed(request, f"Unsupported file type '{ext or '(none)'}'. Upload a .pdf or .csv statement.")
    # A CSV may have a saved mapping and need no LLM; that is checked below.
    if ext == ".pdf" and not is_ollama_available():
        return _upload_failed(request, _ollama_unreachable())
    model = model.strip() or get_config().llm.model

    # Save under a generated name in the private job directory; the worker
    # deletes it as soon as the text is extracted (§3.2, §9.3).
    fd, tmp_name = tempfile.mkstemp(prefix="statement-", suffix=ext, dir=jobs.tmp_dir())
    tmp_path = Path(tmp_name)
    mapping = None
    try:
        sha = hashlib.sha256()
        with os.fdopen(fd, "wb") as f:
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise _UploadTooLarge()
                sha.update(chunk)
                f.write(chunk)

        account = _resolve_account(account_id, new_account_name, new_account_kind)
        if ext == ".csv":
            rows = csv_import.read_rows(tmp_path)
            mapping = csv_import.find_mapping(rows, db.get_csv_mappings(account))
            if mapping is None:
                # Ask how to read it; the file waits in tmp/ until then (§3.2).
                staging.check_not_imported(account, sha.hexdigest())
                return _mapping_form(
                    request, token=tmp_path.name, file_name=source_name, account_id=account, model=model
                )
        if mapping is None and not is_ollama_available():
            tmp_path.unlink(missing_ok=True)
            return _upload_failed(request, _ollama_unreachable())

        statement_id = staging.create_import(
            account_id=account,
            file_name=source_name,
            file_sha256=sha.hexdigest(),
            model=model,
            extractor="llm" if mapping is None else "csv_mapping",
        )
    except _UploadTooLarge:
        tmp_path.unlink(missing_ok=True)
        return _upload_failed(request, f"File is larger than the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    except staging.AlreadyImported as exc:
        tmp_path.unlink(missing_ok=True)
        return _upload_failed(request, str(exc), exc.statement["id"])
    except db.AccountError as exc:
        tmp_path.unlink(missing_ok=True)
        return _upload_failed(request, str(exc))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    jobs.submit(jobs.Job(statement_id, tmp_path, source_name, model, mapping=mapping, password=password or None))
    return _import_status(request, statement_id)


# --- CSV mapping (§3.2) -----------------------------------------------------

_HELD_CSV = re.compile(r"statement-[A-Za-z0-9_]+\.csv")


def _held_csv(token: str) -> Optional[Path]:
    """The temp file of a CSV awaiting a mapping decision.  *token* is the
    generated file name and is checked so it can never name another path."""
    if not _HELD_CSV.fullmatch(token):
        return None
    path = jobs.tmp_dir() / token
    return path if path.is_file() else None


def _mapping_form(
    request: Request,
    *,
    token: str,
    file_name: str,
    account_id: int,
    model: str,
    skip_rows: Optional[int] = None,
    values: Optional[dict] = None,
    error: Optional[str] = None,
) -> HTMLResponse:
    rows = csv_import.read_rows(jobs.tmp_dir() / token)
    if not rows:
        (jobs.tmp_dir() / token).unlink(missing_ok=True)
        return _upload_failed(request, "The file is empty.")
    skip = csv_import.detect_header(rows) if skip_rows is None else min(max(skip_rows, 0), len(rows) - 1)
    header = rows[skip]
    samples = [r for r in rows[skip + 1 :] if any(r)][:5]
    account = next((a for a in db.list_accounts() if a["id"] == account_id), None)
    return templates.TemplateResponse(
        request=request,
        name="partials/csv_mapping.html",
        context={
            "request": request,
            "token": token,
            "file_name": file_name,
            "account_id": account_id,
            "account_name": account["name"] if account else "",
            "model": model,
            "skip_rows": skip,
            "header": header,
            "columns": csv_import.columns(header),
            "samples": samples,
            "date_formats": csv_import.DATE_FORMATS,
            "values": values or csv_import.guess_mapping(header, samples),
            "error": error,
        },
    )


@app.post("/upload/csv-mapping", response_class=HTMLResponse)
async def csv_mapping_submit(
    request: Request,
    token: str = Form(...),
    file_name: str = Form(""),
    account_id: int = Form(...),
    model: str = Form(""),
    action: str = Form("save"),
    skip_rows: int = Form(0),
    date_col: str = Form(""),
    date_format: str = Form(""),
    value_date_col: str = Form(""),
    description_col: str = Form(""),
    amount_mode: str = Form("single"),
    amount_col: str = Form(""),
    debit_col: str = Form(""),
    credit_col: str = Form(""),
    negate: bool = Form(False),
):
    """The mapping form's actions: ``save`` a mapping and parse with it,
    extract with the ``llm`` instead, ``refresh`` the form for another
    header row, or ``cancel`` (the file is deleted)."""
    path = _held_csv(token)
    if path is None:
        return _upload_failed(request, "This upload has expired. Please upload the file again.")
    file_name = _sanitize_filename(file_name)
    model = model.strip() or get_config().llm.model
    form = dict(token=token, file_name=file_name, account_id=account_id, model=model)

    if action == "cancel":
        path.unlink(missing_ok=True)
        return templates.TemplateResponse(
            request=request,
            name="partials/upload_result.html",
            context={"request": request, "notice": f"Upload of {file_name} cancelled. The file was deleted."},
        )
    if action == "refresh":
        return _mapping_form(request, **form, skip_rows=skip_rows)

    values = {
        "date_col": date_col or None,
        "date_format": date_format,
        "value_date_col": value_date_col or None,
        "description_col": description_col or None,
        "amount_mode": amount_mode,
        "amount_col": (amount_col or None) if amount_mode == "single" else None,
        "debit_col": (debit_col or None) if amount_mode == "split" else None,
        "credit_col": (credit_col or None) if amount_mode == "split" else None,
        "negate": negate,
    }
    mapping = None
    if action == "llm":
        if not is_ollama_available():
            return _mapping_form(request, **form, skip_rows=skip_rows, values=values, error=_ollama_unreachable())
    else:
        rows = csv_import.read_rows(path)
        header = rows[skip_rows] if 0 <= skip_rows < len(rows) else []
        mapping = csv_import.CsvMapping(
            header_signature=csv_import.header_signature(header),
            skip_rows=skip_rows,
            date_col=values["date_col"] or "",
            date_format=date_format,
            description_col=values["description_col"] or "",
            value_date_col=values["value_date_col"],
            amount_col=values["amount_col"],
            debit_col=values["debit_col"],
            credit_col=values["credit_col"],
            negate=negate,
        )
        try:
            csv_import.validate(rows, mapping)
        except csv_import.CsvMappingError as exc:
            return _mapping_form(request, **form, skip_rows=skip_rows, values=values, error=str(exc))
        mapping.account_id = account_id
        mapping.id = db.save_csv_mapping(account_id, mapping)

    try:
        statement_id = staging.create_import(
            account_id=account_id,
            file_name=file_name,
            file_sha256=_file_sha256(path),
            model=model,
            extractor="llm" if mapping is None else "csv_mapping",
        )
    except staging.AlreadyImported as exc:
        path.unlink(missing_ok=True)
        return _upload_failed(request, str(exc), exc.statement["id"])
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    jobs.submit(jobs.Job(statement_id, path, file_name, model, mapping=mapping))
    return _import_status(request, statement_id)


def _import_status(request: Request, statement_id: int) -> HTMLResponse:
    statement = staging.get_statement(statement_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/import_status.html",
        context={"request": request, "statement": statement},
        status_code=200 if statement else 404,
    )


# ---------------------------------------------------------------------------
# Routes — Imports & review (app_spec.md §3.5)
# ---------------------------------------------------------------------------

@app.get("/imports", response_class=HTMLResponse)
async def imports_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="imports.html",
        context={"request": request, "imports": staging.list_imports()},
    )


@app.delete("/imports/{statement_id}", response_class=HTMLResponse)
async def delete_import_route(request: Request, statement_id: int):
    try:
        removed = staging.delete_import(statement_id)
        message, error = f"Import #{statement_id} deleted, with its {removed} transaction(s).", None
    except staging.ReviewError as exc:
        message, error = None, str(exc)
    return templates.TemplateResponse(
        request=request,
        name="partials/imports_table.html",
        context={"request": request, "imports": staging.list_imports(), "message": message, "error": error},
    )


@app.get("/imports/{statement_id}/status", response_class=HTMLResponse)
async def import_status(request: Request, statement_id: int):
    return _import_status(request, statement_id)


@app.post("/imports/{statement_id}/password", response_class=HTMLResponse)
async def import_password(request: Request, statement_id: int, password: str = Form("")):
    """Retry a ``needs_password`` PDF (§3.2).  The password stays in the
    job's memory only."""
    if password:
        jobs.provide_password(statement_id, password)
    return _import_status(request, statement_id)


@app.post("/imports/{statement_id}/cancel", response_class=HTMLResponse)
async def import_cancel(request: Request, statement_id: int):
    jobs.cancel(statement_id)
    return _import_status(request, statement_id)


@app.get("/imports/{statement_id}", response_class=HTMLResponse)
async def review_page(request: Request, statement_id: int):
    review = staging.get_review(statement_id)
    if review is None:
        return HTMLResponse("Import not found", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={"request": request, "review": review},
    )


def _review_body(
    request: Request,
    statement_id: int,
    *,
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> HTMLResponse:
    review = staging.get_review(statement_id)
    if review is None:
        return HTMLResponse("Import not found", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="partials/review_body.html",
        context={"request": request, "review": review, "message": message, "error": error},
    )


@app.post("/imports/{statement_id}/rows/{row_id}", response_class=HTMLResponse)
async def edit_staged_row(
    request: Request,
    statement_id: int,
    row_id: int,
    transaction_date: str = Form(""),
    raw_description: str = Form(""),
    amount: str = Form(""),
    is_transfer: bool = Form(False),
):
    try:
        staging.update_row(
            statement_id,
            row_id,
            transaction_date=transaction_date,
            raw_description=raw_description,
            amount=amount,
            is_transfer=is_transfer,
        )
    except staging.ReviewError as exc:
        return _review_body(request, statement_id, error=str(exc))
    return _review_body(request, statement_id)


@app.post("/imports/{statement_id}/rows/{row_id}/drop", response_class=HTMLResponse)
async def drop_staged_row(
    request: Request, statement_id: int, row_id: int, dropped: bool = Form(True)
):
    try:
        staging.set_dropped(statement_id, row_id, dropped)
    except staging.ReviewError as exc:
        return _review_body(request, statement_id, error=str(exc))
    return _review_body(request, statement_id)


@app.post("/imports/{statement_id}/meta", response_class=HTMLResponse)
async def edit_statement_meta(
    request: Request,
    statement_id: int,
    period_start: str = Form(""),
    period_end: str = Form(""),
    opening_balance: str = Form(""),
    closing_balance: str = Form(""),
):
    try:
        staging.update_metadata(
            statement_id,
            period_start=period_start,
            period_end=period_end,
            opening_balance=opening_balance,
            closing_balance=closing_balance,
        )
    except staging.ReviewError as exc:
        return _review_body(request, statement_id, error=str(exc))
    return _review_body(request, statement_id)


@app.post("/imports/{statement_id}/commit", response_class=HTMLResponse)
async def commit_import(
    request: Request, statement_id: int, accept_mismatch: bool = Form(False)
):
    try:
        result = staging.commit(statement_id, accept_mismatch=accept_mismatch)
    except staging.ReviewError as exc:
        return _review_body(request, statement_id, error=str(exc))
    return _review_body(
        request,
        statement_id,
        message=f"Committed: {result.inserted} new transaction(s), "
        f"{result.duplicates} already in the ledger.",
    )


@app.post("/imports/{statement_id}/discard", response_class=HTMLResponse)
async def discard_import(request: Request, statement_id: int):
    try:
        staging.discard(statement_id)
    except staging.ReviewError as exc:
        return _review_body(request, statement_id, error=str(exc))
    return _review_body(request, statement_id, message="Import discarded. Nothing was saved.")


# ---------------------------------------------------------------------------
# Routes — Transactions
# ---------------------------------------------------------------------------

@app.get("/transactions", response_class=HTMLResponse)
async def transactions_page(
    request: Request,
    year: Optional[int] = None,
    month: Optional[int] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    uncategorized: bool = False,
    transfers: str = "show",
    account_id: Optional[str] = None,
    page: int = 1,
):
    per_page = 30
    page = max(1, page)
    if month is not None and not 1 <= month <= 12:
        month = None
    if transfers not in ("show", "hide", "only"):
        transfers = "show"
    offset = (page - 1) * per_page

    filters = dict(
        year=year, month=month, category=category, search=search,
        uncategorized=uncategorized, transfers=transfers, account_id=_account_param(account_id),
    )
    txns = db.get_transactions(**filters, limit=per_page, offset=offset)
    total = db.get_transaction_count(**filters)
    categories = db.get_categories()
    total_pages = max(1, (total + per_page - 1) // per_page)
    # Query string of the active filters, for pagination links.
    filter_qs = urlencode({
        k: (1 if v is True else v)
        for k, v in filters.items()
        if v not in (None, "", False) and not (k == "transfers" and v == "show")
    })

    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context={
            "request": request,
            "transactions": txns,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "per_page": per_page,
            "categories": categories,
            "filter_year": year,
            "filter_month": month,
            "filter_category": category,
            "filter_search": search or "",
            "filter_uncategorized": uncategorized,
            "filter_transfers": transfers,
            "filter_account": filters["account_id"],
            "accounts": db.list_accounts(),
            "filter_qs": filter_qs,
        },
    )


def _tx_row(request: Request, tx_id: str, template: str = "partials/tx_row.html") -> HTMLResponse:
    tx = db.get_transaction(tx_id)
    if tx is None:
        return HTMLResponse("Transaction not found", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"request": request, "tx": tx, "categories": db.get_categories()},
    )


@app.get("/transactions/{tx_id}/row", response_class=HTMLResponse)
async def transaction_row(request: Request, tx_id: str):
    return _tx_row(request, tx_id)


@app.get("/transactions/{tx_id}/edit", response_class=HTMLResponse)
async def transaction_edit_row(request: Request, tx_id: str):
    return _tx_row(request, tx_id, "partials/tx_row_edit.html")


@app.post("/transactions/{tx_id}", response_class=HTMLResponse)
async def update_transaction(
    request: Request,
    tx_id: str,
    category: str = Form(""),
    cleaned_merchant: str = Form(""),
    is_transfer: bool = Form(False),
):
    db.set_manual_category(
        tx_id, category=category, cleaned_merchant=cleaned_merchant, is_transfer=is_transfer
    )
    return _tx_row(request, tx_id)


@app.post("/transactions/{tx_id}/reset", response_class=HTMLResponse)
async def reset_transaction(request: Request, tx_id: str):
    db.reset_to_rules(tx_id)
    return _tx_row(request, tx_id)


@app.delete("/transactions/{tx_id}")
async def delete_transaction_route(tx_id: str):
    if not db.delete_transaction(tx_id):
        return HTMLResponse("Transaction not found", status_code=404)
    return Response(status_code=200)  # empty body: HTMX removes the row


# ---------------------------------------------------------------------------
# Routes — Accounts (app_spec.md §7.1)
# ---------------------------------------------------------------------------

def _accounts_context(message: Optional[str] = None, error: Optional[str] = None) -> dict:
    accounts = db.list_accounts_summary()
    names = {a["id"]: a["name"] for a in accounts}
    return {
        "accounts": accounts,
        "account_kinds": db.ACCOUNT_KINDS,
        "mappings": [(m, names.get(m.account_id, "?")) for m in db.get_csv_mappings()],
        "date_formats": csv_import.DATE_FORMATS,
        "message": message,
        "error": error,
    }


def _accounts_table(
    request: Request, *, message: Optional[str] = None, error: Optional[str] = None
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="partials/accounts_table.html",
        context={"request": request, **_accounts_context(message, error)},
    )


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context={"request": request, **_accounts_context()},
    )


@app.post("/accounts", response_class=HTMLResponse)
async def create_account_route(
    request: Request,
    name: str = Form(""),
    kind: str = Form("bank"),
    institution: str = Form(""),
):
    try:
        db.create_account(name, kind, institution)
    except db.AccountError as exc:
        return _accounts_table(request, error=str(exc))
    return _accounts_table(request, message=f"Account '{' '.join(name.split())}' created.")


@app.post("/accounts/{account_id}", response_class=HTMLResponse)
async def update_account_route(
    request: Request,
    account_id: int,
    name: str = Form(""),
    kind: str = Form("bank"),
    institution: str = Form(""),
):
    try:
        db.update_account(account_id, name=name, kind=kind, institution=institution)
    except db.AccountError as exc:
        return _accounts_table(request, error=str(exc))
    return _accounts_table(request, message="Account saved.")


@app.delete("/accounts/{account_id}", response_class=HTMLResponse)
async def delete_account_route(request: Request, account_id: int):
    try:
        db.delete_account(account_id)
    except db.AccountError as exc:
        return _accounts_table(request, error=str(exc))
    return _accounts_table(request, message="Account deleted.")


@app.delete("/csv-mappings/{mapping_id}", response_class=HTMLResponse)
async def delete_csv_mapping_route(request: Request, mapping_id: int):
    if not db.delete_csv_mapping(mapping_id):
        return _accounts_table(request, error="Mapping not found.")
    return _accounts_table(
        request, message="Mapping deleted. The next file with that header will ask for a mapping again."
    )


# ---------------------------------------------------------------------------
# Routes — Rules
# ---------------------------------------------------------------------------

def _rules_fragment(request: Request, **extra) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="partials/rule_row.html",
        context={
            "request": request,
            "rules": db.get_rules(),
            "account_names": {a["id"]: a["name"] for a in db.list_accounts()},
            **extra,
        },
    )


def _account_param(account_id: Optional[str]) -> Optional[int]:
    """An account id from a form or query string; blank or unknown = all."""
    try:
        wanted = int(account_id) if account_id else None
    except ValueError:
        return None
    return wanted if any(a["id"] == wanted for a in db.list_accounts()) else None


@app.get("/rules", response_class=HTMLResponse)
async def rules_page(
    request: Request,
    pattern: str = "",
    category: str = "",
    cleaned_merchant: str = "",
):
    """Rules page; the query params prefill the form ("create rule from
    this" on a transaction, §6)."""
    accounts = db.list_accounts()
    return templates.TemplateResponse(
        request=request,
        name="rules.html",
        context={
            "request": request,
            "rules": db.get_rules(),
            "accounts": accounts,
            "account_names": {a["id"]: a["name"] for a in accounts},
            "prefill": {"pattern": pattern, "category": category, "cleaned_merchant": cleaned_merchant},
            "match_count": db.count_rule_matches(pattern) if pattern else None,
        },
    )


@app.get("/rules/match-count", response_class=HTMLResponse)
async def rule_match_count(pattern: str = "", account_id: Optional[str] = None):
    """Live match count for the rule form (§6), with the engine's matching."""
    if not pattern.strip():
        return HTMLResponse("")
    n = db.count_rule_matches(pattern, _account_param(account_id))
    return HTMLResponse(f"Matches {n} transaction{'s' if n != 1 else ''}")


@app.post("/rules", response_class=HTMLResponse)
async def create_rule(
    request: Request,
    pattern: str = Form(...),
    category: str = Form(...),
    cleaned_merchant: Optional[str] = Form(None),
    priority: int = Form(0),
    mark_transfer: bool = Form(False),
    account_id: Optional[str] = Form(None),
):
    add_rule(
        pattern,
        category,
        cleaned_merchant or None,
        priority=priority,
        mark_transfer=mark_transfer,
        account_id=_account_param(account_id),
    )
    return _rules_fragment(request)


@app.delete("/rules/{rule_id}", response_class=HTMLResponse)
async def delete_rule_route(request: Request, rule_id: int):
    remove_rule(rule_id)
    return _rules_fragment(request)


@app.post("/rules/apply", response_class=HTMLResponse)
async def apply_rules_route(request: Request):
    return _rules_fragment(request, updated_count=apply_all_rules())
