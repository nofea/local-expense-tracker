"""Tests for the validator (§4), staging and review (§3.4–3.5) and
background ingestion jobs (§3): Appendix A items 9, 10 and 6."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import json
import re
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import core.database as db
from core import jobs, staging, validator
from core.analytics import get_monthly_summary
from core.categorizer import add_rule
from core.models import ExtractedTransaction, StatementMetadata
from web.app import app


def _tx(date, desc, amount, value_date=None):
    return ExtractedTransaction(
        transaction_date=date, raw_description=desc, amount=amount, value_date=value_date
    )


class TestValidator(unittest.TestCase):
    """§11 Phase 2: each validator code has a passing and a failing case."""

    def issues(self, date="2025-03-05", desc="Coffee", minor=-15000, value_date=None, source=None):
        src = validator.normalize_for_match(source) if source is not None else None
        return validator.row_issues(date, value_date, desc, minor, src)

    def test_bad_date(self):
        self.assertEqual(self.issues()[0], [])
        for bad in ("2025-02-30", "05/03/2025", "20250305", "2025-3-5", "", None):
            with self.subTest(bad=bad):
                self.assertIn("bad_date", self.issues(date=bad)[0])

    def test_zero_amount(self):
        self.assertNotIn("zero_amount", self.issues(minor=-1)[0])
        self.assertIn("zero_amount", self.issues(minor=0)[0])
        self.assertIn("zero_amount", self.issues(minor=None)[0])

    def test_missing_desc(self):
        self.assertNotIn("missing_desc", self.issues(desc="x")[0])
        self.assertIn("missing_desc", self.issues(desc=" \t ")[0])
        self.assertIn("missing_desc", self.issues(desc=None)[0])

    def test_bad_value_date_is_cleared_with_warning(self):
        self.assertEqual(self.issues(value_date="2025-03-06"), ([], "2025-03-06"))
        issues, value_date = self.issues(value_date="06/03")
        self.assertEqual((issues, value_date), (["bad_value_date"], None))

    def test_not_in_source(self):
        text = "05 Mar  COFFEE   house  -150.00\n06 Mar RENT -20000"
        self.assertEqual(self.issues(desc="coffee HOUSE", source=text)[0], [])
        self.assertEqual(self.issues(desc="Imaginary Store", source=text)[0], ["not_in_source"])
        # Without source text the check is skipped.
        self.assertEqual(self.issues(desc="Imaginary Store")[0], [])

    def test_levels(self):
        self.assertTrue(validator.has_errors(["not_in_source", "bad_date"]))
        self.assertFalse(validator.has_errors(["not_in_source", "duplicate_in_ledger"]))
        self.assertEqual(set(validator.LEVELS), set(validator.MESSAGES))

    def test_reconcile(self):
        self.assertEqual(validator.reconcile(None, 100, [1]), (None, None))
        self.assertEqual(validator.reconcile(1000, 700, [-500, 200]), (1, 0))
        self.assertEqual(validator.reconcile(1000, 650, [-500, 200]), (0, -50))


class _TempDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "expenses.db"
        db.init_db(self.path)
        self.account = db.get_or_create_account(db_path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def q(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def new_import(self, sha=None):
        return staging.create_import(
            account_id=self.account,
            file_name="mar.pdf",
            file_sha256=sha or uuid.uuid4().hex,
            model="llama3.1",
            db_path=self.path,
        )

    def stage(self, rows, source_text="", **meta):
        sid = self.new_import()
        staging.stage_rows(sid, rows, source_text=source_text, db_path=self.path, **meta)
        return sid

    def row_ids(self, sid):
        return [r[0] for r in self.q("SELECT id FROM staged_rows WHERE statement_id = ? ORDER BY position", (sid,))]

    def issues(self, sid):
        return [json.loads(r[0]) for r in self.q(
            "SELECT issues FROM staged_rows WHERE statement_id = ? ORDER BY position", (sid,))]


SOURCE = "01 Mar SALARY ACME 90000.00\n02 Mar COFFEE HOUSE -150.00\n02 Mar COFFEE HOUSE -150.00"
ROWS = [
    {"transaction_date": "2025-03-01", "raw_description": "SALARY ACME", "amount": 90000.0},
    {"transaction_date": "2025-03-02", "raw_description": "COFFEE HOUSE", "amount": -150.0},
    {"transaction_date": "2025-03-02", "raw_description": "COFFEE HOUSE", "amount": -150.0},
]


class TestStagingAndReview(_TempDB):
    def test_stage_sets_review_and_validates(self):
        rows = ROWS + [
            {"transaction_date": "31/03/2025", "raw_description": "MYSTERY", "amount": -10.0},
        ]
        sid = self.stage(rows, SOURCE, opening_minor=0, closing_minor=8970000)

        self.assertEqual(staging.get_statement(sid, self.path)["status"], "review")
        self.assertEqual(self.issues(sid), [[], [], [], ["bad_date", "not_in_source"]])

        review = staging.get_review(sid, self.path)
        self.assertEqual((review.active_rows, review.error_rows, review.warning_rows), (4, 1, 0))
        self.assertFalse(review.can_commit)
        # 0 + 90000 - 150 - 150 - 10 = 89690 ≠ 89700
        self.assertEqual((review.reconciled, review.difference_minor), (0, 1000))

    def test_fixing_and_dropping_rows(self):
        sid = self.stage(ROWS + [
            {"transaction_date": "31/03/2025", "raw_description": "MYSTERY", "amount": -10.0},
        ], SOURCE)
        bad = self.row_ids(sid)[3]

        # Fix the date: the error goes, not_in_source stays (description unchanged).
        staging.update_row(sid, bad, transaction_date="2025-03-31", raw_description="MYSTERY",
                           amount="-10.00", is_transfer=False, db_path=self.path)
        self.assertEqual(self.issues(sid)[3], ["not_in_source"])

        # Editing the description means the user vouches for it.
        staging.update_row(sid, bad, transaction_date="2025-03-31", raw_description="Mystery shop",
                           amount="-1,234.50", is_transfer=True, db_path=self.path)
        self.assertEqual(self.issues(sid)[3], [])
        self.assertEqual(self.q("SELECT amount_minor, is_transfer FROM staged_rows WHERE id = ?", (bad,)),
                         [(-123450, 1)])

        staging.set_dropped(sid, bad, True, db_path=self.path)
        review = staging.get_review(sid, self.path)
        self.assertEqual(review.active_rows, 3)
        self.assertEqual(review.total_minor, 8970000)

    def test_invalid_amount_edit_is_refused(self):
        sid = self.stage(ROWS, SOURCE)
        with self.assertRaises(staging.ReviewError):
            staging.update_row(sid, self.row_ids(sid)[0], transaction_date="2025-03-01",
                               raw_description="SALARY ACME", amount="ninety", is_transfer=False,
                               db_path=self.path)

    def test_commit_blocked_by_errors_then_succeeds(self):
        sid = self.stage(ROWS + [{"transaction_date": "bad", "raw_description": "X", "amount": -1.0}], SOURCE)
        with self.assertRaisesRegex(staging.ReviewError, "errors"):
            staging.commit(sid, db_path=self.path)

        staging.set_dropped(sid, self.row_ids(sid)[3], True, db_path=self.path)
        result = staging.commit(sid, db_path=self.path)

        self.assertEqual((result.inserted, result.duplicates), (3, 0))  # both coffees kept
        statement = staging.get_statement(sid, self.path)
        self.assertEqual(statement["status"], "committed")
        self.assertIsNotNone(statement["committed_at"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM staged_rows"), [(0,)])
        with self.assertRaises(staging.ReviewError):
            staging.commit(sid, db_path=self.path)

    def test_commit_applies_rules_and_keeps_transfer_flag(self):
        add_rule("coffee", "Food", db_path=self.path)
        sid = self.stage(ROWS, SOURCE)
        salary = self.row_ids(sid)[0]
        staging.update_row(sid, salary, transaction_date="2025-03-01", raw_description="SALARY ACME",
                           amount="90000", is_transfer=True, db_path=self.path)
        staging.commit(sid, db_path=self.path)

        self.assertEqual(
            self.q("SELECT category FROM transactions WHERE raw_description = 'COFFEE HOUSE'"),
            [("Food",), ("Food",)],
        )
        summary = get_monthly_summary(2025, 3, self.path)
        self.assertEqual((summary.total_income, summary.total_expense), (0.0, 300.0))

    def test_mismatch_needs_explicit_confirmation(self):
        sid = self.stage(ROWS, SOURCE, opening_minor=0, closing_minor=1)
        with self.assertRaisesRegex(staging.ReviewError, "closing balance"):
            staging.commit(sid, db_path=self.path)
        self.assertEqual(staging.commit(sid, accept_mismatch=True, db_path=self.path).inserted, 3)

    def test_metadata_edit_reconciles(self):
        sid = self.stage(ROWS, SOURCE)
        self.assertIsNone(staging.get_review(sid, self.path).reconciled)
        staging.update_metadata(sid, period_start="2025-03-01", period_end="2025-03-31",
                                opening_balance="1,000.00", closing_balance="90700", db_path=self.path)
        review = staging.get_review(sid, self.path)
        self.assertEqual((review.reconciled, review.statement["opening_minor"]), (1, 100000))
        with self.assertRaises(staging.ReviewError):
            staging.update_metadata(sid, period_start="March", db_path=self.path)

    def test_duplicate_in_ledger_is_flagged_and_skipped(self):
        first = self.stage(ROWS, SOURCE)
        staging.commit(first, db_path=self.path)
        # An overlapping statement repeats the salary and one of the two coffees.
        second = self.stage([ROWS[0], ROWS[1],
                             {"transaction_date": "2025-04-01", "raw_description": "RENT", "amount": -20000.0}])
        self.assertEqual(self.issues(second), [["duplicate_in_ledger"], ["duplicate_in_ledger"], []])
        result = staging.commit(second, db_path=self.path)
        self.assertEqual((result.inserted, result.duplicates), (1, 2))

    def test_discard(self):
        sid = self.stage(ROWS, SOURCE)
        staging.discard(sid, db_path=self.path)
        self.assertEqual(staging.get_statement(sid, self.path)["status"], "discarded")
        self.assertEqual(self.q("SELECT COUNT(*) FROM staged_rows"), [(0,)])
        with self.assertRaises(staging.ReviewError):
            staging.update_row(sid, 1, transaction_date="2025-03-01", raw_description="x",
                               amount="1", is_transfer=False, db_path=self.path)

    def test_reupload_rules(self):
        sid = self.new_import(sha="same")
        with self.assertRaises(staging.AlreadyImported):
            self.new_import(sha="same")
        staging.set_status(sid, "failed", error="boom", db_path=self.path)
        retry = self.new_import(sha="same")  # a failed attempt is replaced
        self.assertEqual(self.q("SELECT id, status, error FROM statements"), [(retry, "queued", None)])

    def test_recover_interrupted(self):
        queued, running, review = self.new_import(), self.new_import(), self.stage(ROWS, SOURCE)
        staging.set_status(running, "extracting", db_path=self.path)
        self.assertEqual(staging.recover_interrupted(self.path), 2)
        for sid, status in ((queued, "failed"), (running, "failed"), (review, "review")):
            self.assertEqual(staging.get_statement(sid, self.path)["status"], status)
        self.assertEqual(staging.get_statement(queued, self.path)["error"], "interrupted")


@patch("core.ingestion.extract_statement_metadata")
@patch("core.ingestion.extract_transactions")
class TestJobs(_TempDB):
    def upload(self, content=b"Date,Description,Amount\n2025-03-02,COFFEE HOUSE,-150\n", name="mar.csv"):
        path = jobs.tmp_dir(self.path) / f"statement-{uuid.uuid4().hex}"
        path.write_bytes(content)
        sid = self.new_import()
        return jobs.Job(sid, path, name, "llama3.1", self.path)

    def test_success_stages_rows_and_deletes_file(self, mock_tx, mock_meta):
        mock_tx.return_value = [_tx("2025-03-02", "COFFEE HOUSE", -150.0, value_date="bad")]
        mock_meta.return_value = StatementMetadata(
            period_start="2025-03-01", period_end="2025-03-31", opening_balance=1000.0, closing_balance=850.0
        )
        job = self.upload()
        jobs.run_job(job)

        self.assertFalse(job.file_path.exists())
        s = staging.get_statement(job.statement_id, self.path)
        self.assertEqual(
            (s["status"], s["progress_done"], s["progress_total"], s["period_end"], s["closing_minor"], s["reconciled"]),
            ("review", 1, 1, "2025-03-31", 85000, 1),
        )
        self.assertEqual(self.issues(job.statement_id), [["bad_value_date"]])

    def test_partial_chunk_failure_still_reaches_review(self, mock_tx, mock_meta):
        mock_meta.return_value = StatementMetadata()
        calls = []

        def first_chunk_only(chunk, model):
            calls.append(chunk)
            if len(calls) > 1:
                raise RuntimeError("timeout")
            return [_tx("2025-03-02", "COFFEE 0", -1.0)]

        mock_tx.side_effect = first_chunk_only
        big = "Date,Description,Amount\n" + "\n".join(f"2025-03-02,COFFEE {i},-1" for i in range(600))
        job = self.upload(big.encode())
        jobs.run_job(job)

        n = len(calls)
        self.assertGreater(n, 1)
        review = staging.get_review(job.statement_id, self.path)
        self.assertEqual(review.statement["status"], "review")
        self.assertEqual(review.chunk_errors, [f"Chunk {i}/{n} failed: timeout" for i in range(2, n + 1)])
        self.assertEqual(review.statement["progress_done"], n)

    def test_all_chunks_failing_fails_the_job(self, mock_tx, mock_meta):
        mock_meta.return_value = StatementMetadata()
        mock_tx.side_effect = RuntimeError("model not found")
        job = self.upload()
        jobs.run_job(job)
        s = staging.get_statement(job.statement_id, self.path)
        self.assertEqual(s["status"], "failed")
        self.assertIn("model not found", s["error"])
        self.assertFalse(job.file_path.exists())

    def test_unreadable_file_fails_and_is_deleted(self, mock_tx, _meta):
        job = self.upload(b"", name="empty.csv")
        jobs.run_job(job)
        s = staging.get_statement(job.statement_id, self.path)
        self.assertEqual(s["status"], "failed")
        self.assertIn("No text", s["error"])
        self.assertFalse(job.file_path.exists())
        mock_tx.assert_not_called()

    def test_metadata_failure_is_not_fatal(self, mock_tx, mock_meta):
        mock_tx.return_value = [_tx("2025-03-02", "COFFEE HOUSE", -150.0)]
        mock_meta.side_effect = RuntimeError("no metadata")
        job = self.upload()
        jobs.run_job(job)
        self.assertEqual(staging.get_statement(job.statement_id, self.path)["status"], "review")

    def test_start_recovers_and_wipes_tmp(self, *_):
        leftover = jobs.tmp_dir(self.path) / "statement-crashed.pdf"
        leftover.write_bytes(b"secret")
        sid = self.new_import()
        staging.set_status(sid, "extracting", db_path=self.path)
        jobs.start(self.path)
        self.assertFalse(leftover.exists())
        self.assertEqual(staging.get_statement(sid, self.path)["status"], "failed")


@patch("web.app.list_available_models", return_value=[])
@patch("web.app.is_ollama_available", return_value=True)
@patch("core.ingestion.extract_statement_metadata",
       return_value=StatementMetadata(opening_balance=0.0, closing_balance=89850.0))
@patch("core.ingestion.extract_transactions")
class TestWebReviewFlow(unittest.TestCase):
    """Upload → progress → review → edit → commit → dashboard, through HTTP."""

    def setUp(self):
        self.client = TestClient(app)
        # Unique bytes per test: the shared test DB rejects re-uploads.
        self.csv = f"Date,Description,Amount\n# {uuid.uuid4()}\n".encode()

    def upload(self):
        res = self.client.post("/upload", files={"file": ("mar.csv", self.csv, "text/csv")})
        self.assertEqual(res.status_code, 200)
        jobs.wait_idle()
        return int(re.search(r"/imports/(\d+)/status", res.text).group(1))

    def test_full_flow(self, mock_tx, *_):
        mock_tx.return_value = [
            _tx("2026-01-01", "SALARY ACME", 90000.0),
            _tx("2026-01-02", "COFFEE HOUSE", -150.0),
            _tx("2026-01-03", "MYSTERY", -150.0),
        ]
        sid = self.upload()

        status = self.client.get(f"/imports/{sid}/status").text
        self.assertIn("ready for review", status)
        self.assertNotIn("every 1s", status)  # polling stops

        page = self.client.get(f"/imports/{sid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Mismatch", page.text)  # 90000 - 150 - 150 ≠ 89850 until MYSTERY is dropped
        self.assertIn("Description not found in the statement text", page.text)

        rows = staging.get_review(sid).rows
        res = self.client.post(f"/imports/{sid}/rows/{rows[2]['id']}/drop", data={"dropped": "true"})
        self.assertIn("Reconciled", res.text)

        res = self.client.post(f"/imports/{sid}/commit")
        self.assertIn("Committed: 2 new transaction(s)", res.text)

        dash = self.client.get("/?year=2026&month=1").text
        self.assertIn("90,000", dash)
        self.assertIn(f'href="/imports/{sid}"', self.client.get("/imports").text)

    def test_edit_errors_are_shown_not_raised(self, mock_tx, *_):
        mock_tx.return_value = [_tx("2026-02-01", "COFFEE", -1.0)]
        sid = self.upload()
        row = staging.get_review(sid).rows[0]
        res = self.client.post(
            f"/imports/{sid}/rows/{row['id']}",
            data={"transaction_date": "2026-02-01", "raw_description": "COFFEE", "amount": "abc"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("not a valid amount", res.text)

    def test_reupload_links_to_existing_import(self, mock_tx, *_):
        mock_tx.return_value = [_tx("2026-03-01", "COFFEE", -1.0)]
        sid = self.upload()
        res = self.client.post("/upload", files={"file": ("again.csv", self.csv, "text/csv")})
        self.assertIn("already uploaded", res.text)
        self.assertIn(f'href="/imports/{sid}"', res.text)

    def test_failed_job_shows_error(self, mock_tx, *_):
        mock_tx.side_effect = RuntimeError("model not found")
        sid = self.upload()
        self.assertIn("model not found", self.client.get(f"/imports/{sid}/status").text)

    def test_unknown_import_is_404(self, *_):
        self.assertEqual(self.client.get("/imports/999999").status_code, 404)
        self.assertEqual(self.client.get("/imports/999999/status").status_code, 404)


if __name__ == "__main__":
    unittest.main()
