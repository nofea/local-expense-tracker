"""Background ingestion jobs (app_spec.md §3).

One worker thread processes uploads one at a time, so LLM extraction never
runs on the web server's event loop.  The ``statements`` row is the job
record: the web UI polls its ``status`` and ``progress_*`` columns.

Uploaded files wait in a private ``tmp/`` directory next to the database
and are deleted as soon as their text has been extracted (§3.2, §9.3).
:func:`start` wipes that directory and marks jobs that were running when
the server stopped as failed.

A CSV job with a saved mapping is parsed deterministically, with no LLM
call.  A PDF that needs a password parks its job in ``needs_password``: the
job (and its file) is held in memory until :func:`provide_password` or
:func:`cancel`.  Passwords live only in the job object, never on disk.
"""

from __future__ import annotations

import logging
import queue
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core import csv_import, database, staging, validator
from core.csv_import import CsvMapping
from core.ingestion import ExtractionError, PasswordRequired, extract_statement, read_pages

log = logging.getLogger(__name__)


@dataclass
class Job:
    statement_id: int
    file_path: Path
    source_name: str
    model: str
    db_path: Optional[Path] = None
    mapping: Optional[CsvMapping] = None  # set: parse the CSV without the LLM
    password: Optional[str] = field(default=None, repr=False)


_queue: "queue.Queue[Job]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()

# Jobs waiting in needs_password, by statement id.
_held: dict[int, Job] = {}
_held_lock = threading.Lock()


def tmp_dir(db_path: Optional[str | Path] = None) -> Path:
    """Private directory for uploads awaiting extraction."""
    path = (Path(db_path) if db_path else database.DEFAULT_DB_PATH).parent / "tmp"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def start(db_path: Optional[str | Path] = None) -> None:
    """Recover from a previous run and start the worker.

    Call once per server process, after ``database.init_db``.  Only the web
    server may call this: it fails any job another process is running.
    """
    interrupted = staging.recover_interrupted(db_path)
    if interrupted:
        log.warning("Marked %d interrupted import(s) as failed", interrupted)
    shutil.rmtree(tmp_dir(db_path), ignore_errors=True)
    tmp_dir(db_path)
    _ensure_worker()


def submit(job: Job) -> None:
    _ensure_worker()
    _queue.put(job)


def provide_password(statement_id: int, password: str) -> bool:
    """Retry a ``needs_password`` job with *password*.  Returns False if the
    job is not waiting (e.g. the server restarted, which wipes its file)."""
    with _held_lock:
        job = _held.pop(statement_id, None)
    if job is None:
        return False
    if not staging.requeue(statement_id, job.db_path):
        job.file_path.unlink(missing_ok=True)
        return False
    job.password = password
    submit(job)
    return True


def cancel(statement_id: int, db_path: Optional[str | Path] = None) -> bool:
    """Give up on a ``needs_password`` job: delete its file and mark it
    ``discarded``."""
    with _held_lock:
        job = _held.pop(statement_id, None)
    if job is None:
        return False
    job.file_path.unlink(missing_ok=True)
    staging.set_status(statement_id, "discarded", db_path=job.db_path or db_path)
    return True


def wait_idle() -> None:
    """Block until every submitted job has finished (used by tests)."""
    _queue.join()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="ingestion-worker", daemon=True)
            _worker.start()


def _work() -> None:
    while True:
        job = _queue.get()
        try:
            run_job(job)
        except Exception:  # run_job records failures; this is a last resort
            log.exception("Ingestion worker crashed on import #%d", job.statement_id)
        finally:
            _queue.task_done()


def run_job(job: Job) -> None:
    """Extract, stage and validate one upload, ending in ``review``,
    ``failed`` or ``needs_password``.  The uploaded file is deleted unless
    the job is held for a password."""
    db_path = job.db_path
    staging.set_status(job.statement_id, "extracting", db_path=db_path)
    held = False
    try:
        if job.mapping is not None:
            try:
                rows = csv_import.parse_file(job.file_path, job.mapping)
            finally:
                job.file_path.unlink(missing_ok=True)  # §3.2
            _stage_mapped_csv(job, rows)
            return

        try:
            pages, header = read_pages(job.file_path, job.source_name, job.password)
        except PasswordRequired as exc:
            job.password = None
            with _held_lock:
                _held[job.statement_id] = job
            held = True
            staging.set_status(job.statement_id, "needs_password", error=str(exc), db_path=db_path)
            return
        finally:
            # Original statements are never retained (§3.2).
            if not held:
                job.file_path.unlink(missing_ok=True)

        extraction = extract_statement(
            pages,
            header=header,
            model=job.model,
            on_progress=lambda done, total: staging.set_progress(
                job.statement_id, done, total, db_path
            ),
        )
        if not extraction.rows:
            reason = "\n".join(extraction.chunk_errors) or "The model found no transactions."
            staging.set_status(job.statement_id, "failed", error=reason, db_path=db_path)
            return

        staging.stage_rows(
            job.statement_id,
            extraction.rows,
            source_text=extraction.source_text,
            period_start=extraction.metadata.period_start,
            period_end=extraction.metadata.period_end,
            opening_minor=extraction.opening_minor,
            closing_minor=extraction.closing_minor,
            error="\n".join(extraction.chunk_errors) or None,
            db_path=db_path,
        )
        log.info("Import #%d ready for review (%d rows)", job.statement_id, len(extraction.rows))
    except ExtractionError as exc:
        staging.set_status(job.statement_id, "failed", error=str(exc), db_path=db_path)
    except Exception as exc:
        log.exception("Import #%d failed", job.statement_id)
        staging.set_status(job.statement_id, "failed", error=str(exc) or type(exc).__name__, db_path=db_path)
    finally:
        if not held:
            job.file_path.unlink(missing_ok=True)


def _stage_mapped_csv(job: Job, rows: list[dict]) -> None:
    """Stage rows parsed with a saved mapping.  Their text comes straight
    from the file, so there is no ``not_in_source`` check; the period is
    taken from the dates, and the balances are left for the user."""
    staging.set_progress(job.statement_id, 1, 1, job.db_path)
    if not rows:
        staging.set_status(
            job.statement_id, "failed", error="No rows were found below the header.", db_path=job.db_path
        )
        return
    dates = sorted(d for r in rows if validator.is_iso_date(d := r["transaction_date"]))
    staging.stage_rows(
        job.statement_id,
        rows,
        source_text="",
        period_start=dates[0] if dates else None,
        period_end=dates[-1] if dates else None,
        db_path=job.db_path,
    )
    log.info("Import #%d ready for review (%d rows, CSV mapping)", job.statement_id, len(rows))
