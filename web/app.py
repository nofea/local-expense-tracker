"""FastAPI web application for the expense tracker."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import database as db
from core.analytics import (
    get_category_breakdown,
    get_monthly_summary,
    get_monthly_trend,
    get_uncategorized_count,
    get_top_merchants,
)
from core.categorizer import add_rule, apply_all_rules, remove_rule
from core.ingestion import process_file
from core.models import CategoryRule
from core.ollama_client import is_ollama_available, list_available_models

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WEB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _WEB_DIR.parent
_UPLOADS_DIR = _PROJECT_ROOT / "uploads"
_UPLOADS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

def _global_template_context(request: Request) -> dict:
    """Inject Ollama connectivity state into all template renders."""
    status = is_ollama_available()
    return {
        "ollama_status": status,
        "ollama_available": status,
    }


app = FastAPI(title="Expense Tracker", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
templates = Jinja2Templates(
    directory=str(_WEB_DIR / "templates"),
    context_processors=[_global_template_context],
)

# Ensure DB is initialised on startup
db.init_db()


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _now() -> tuple[int, int]:
    """Return (year, month) for the current date."""
    today = datetime.today()
    return today.year, today.month


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
    y, m = year or _now()[0], month or _now()[1]
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
    model: str = Form("llama3.1"),
):
    # Save uploaded file
    dest = _UPLOADS_DIR / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Guard: check Ollama availability before starting pipeline
    if not is_ollama_available():
        result = None
        error = (
            "Ollama is not running or unreachable at localhost:11434. "
            "Please ensure Ollama is installed and running ('ollama serve') before processing."
        )
    else:
        # Run ingestion pipeline
        try:
            result = process_file(dest, model=model)
        except Exception as exc:
            log.exception("Ingestion failed")
            result = None
            error = str(exc)
        else:
            error = None

    # Get updated model list for re-rendering
    models = list_available_models()

    return templates.TemplateResponse(
        request=request,
        name="partials/upload_result.html",
        context={
            "request": request,
            "result": result,
            "error": error,
            "models": models,
        },
    )


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
