"""Background ingestion jobs (app_spec.md §3).

One worker thread processes uploads one at a time, so LLM extraction never
runs on the web server's event loop.  The ``statements`` row is the job
record: the web UI polls its ``status`` and ``progress_*`` columns.

Uploaded files wait in a private ``tmp/`` directory next to the database
and are deleted as soon as their text has been extracted (§3.2, §9.3).
:func:`start` wipes that directory and marks jobs that were running when
the server stopped as failed.
"""

from __future__ import annotations

import logging
import queue
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core import database, staging
from core.ingestion import ExtractionError, extract_statement, read_pages

log = logging.getLogger(__name__)


@dataclass
class Job:
    statement_id: int
    file_path: Path
    source_name: str
    model: str
    db_path: Optional[Path] = None


_queue: "queue.Queue[Job]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


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
    """Extract, stage and validate one upload, ending in ``review`` or
    ``failed``.  The uploaded file is always deleted."""
    db_path = job.db_path
    staging.set_status(job.statement_id, "extracting", db_path=db_path)
    try:
        try:
            pages, header = read_pages(job.file_path, job.source_name)
        finally:
            # Original statements are never retained (§3.2).
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
        job.file_path.unlink(missing_ok=True)
