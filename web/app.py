"""FastAPI web application for the expense tracker."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import database as db
from core import jobs, staging, validator
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
):
    y, m = _valid_month(year, month)
    summary = get_monthly_summary(y, m)
    breakdown = get_category_breakdown(y, m)
    trend = get_monthly_trend(6)
    uncategorized = get_uncategorized_count()
    top_merchants = get_top_merchants(y, m)

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
            "ollama_available": ollama_ok,
            "ollama_status": ollama_ok,
        },
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(""),
):
    # The client filename is display text only — never a path (§3.1, §9.6).
    source_name = _sanitize_filename(file.filename)
    ext = Path(source_name).suffix.lower()

    def failed(message: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="partials/upload_result.html",
            context={"request": request, "error": message},
        )

    if ext not in _ALLOWED_EXTENSIONS:
        return failed(f"Unsupported file type '{ext or '(none)'}'. Upload a .pdf or .csv statement.")
    if not is_ollama_available():
        return failed(
            f"Ollama is not running or unreachable at {get_config().llm.host}. "
            "Please ensure Ollama is installed and running ('ollama serve') before processing."
        )
    model = model.strip() or get_config().llm.model

    # Save under a generated name in the private job directory; the worker
    # deletes it as soon as the text is extracted (§3.2, §9.3).
    fd, tmp_name = tempfile.mkstemp(prefix="statement-", suffix=ext, dir=jobs.tmp_dir())
    tmp_path = Path(tmp_name)
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

        statement_id = staging.create_import(
            account_id=db.get_or_create_account(),
            file_name=source_name,
            file_sha256=sha.hexdigest(),
            model=model,
        )
    except _UploadTooLarge:
        tmp_path.unlink(missing_ok=True)
        return failed(f"File is larger than the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    except staging.AlreadyImported as exc:
        tmp_path.unlink(missing_ok=True)
        return templates.TemplateResponse(
            request=request,
            name="partials/upload_result.html",
            context={"request": request, "error": str(exc), "existing_id": exc.statement["id"]},
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    jobs.submit(jobs.Job(statement_id, tmp_path, source_name, model))
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


@app.get("/imports/{statement_id}/status", response_class=HTMLResponse)
async def import_status(request: Request, statement_id: int):
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
    page: int = 1,
):
    per_page = 30
    page = max(1, page)
    if month is not None and not 1 <= month <= 12:
        month = None
    offset = (page - 1) * per_page

    txns = db.get_transactions(
        year=year, month=month, category=category,
        search=search, limit=per_page, offset=offset,
    )
    total = db.get_transaction_count(
        year=year, month=month, category=category, search=search,
    )
    categories = db.get_categories()
    total_pages = max(1, (total + per_page - 1) // per_page)

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
        },
    )


# ---------------------------------------------------------------------------
# Routes — Rules
# ---------------------------------------------------------------------------

@app.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    rules = db.get_rules()
    return templates.TemplateResponse(
        request=request, name="rules.html", context={"request": request, "rules": rules}
    )


@app.post("/rules", response_class=HTMLResponse)
async def create_rule(
    request: Request,
    pattern: str = Form(...),
    category: str = Form(...),
    cleaned_merchant: Optional[str] = Form(None),
):
    add_rule(pattern, category, cleaned_merchant or None)
    rules = db.get_rules()
    return templates.TemplateResponse(
        request=request,
        name="partials/rule_row.html",
        context={"request": request, "rules": rules},
    )


@app.delete("/rules/{rule_id}", response_class=HTMLResponse)
async def delete_rule_route(request: Request, rule_id: int):
    remove_rule(rule_id)
    rules = db.get_rules()
    return templates.TemplateResponse(
        request=request,
        name="partials/rule_row.html",
        context={"request": request, "rules": rules},
    )


@app.post("/rules/apply", response_class=HTMLResponse)
async def apply_rules_route(request: Request):
    updated = apply_all_rules()
    rules = db.get_rules()
    return templates.TemplateResponse(
        request=request,
        name="partials/rule_row.html",
        context={"request": request, "rules": rules, "updated_count": updated},
    )
