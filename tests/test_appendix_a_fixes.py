"""Regression tests for the app_spec.md Appendix A fixes (items 1, 2, 3, 4, 7)."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import re
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import core.ingestion as ingestion
from core import jobs
from core.analytics import get_monthly_trend
from core.database import get_or_create_account, import_statement, init_db
from web.app import _sanitize_filename, app

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestChunking(unittest.TestCase):
    """Item 4: chunks are page/line aligned and never overlap."""

    def test_small_pages_are_packed_together(self):
        chunks = ingestion._chunk_pages(["page one", "page two"])
        self.assertEqual(chunks, ["page one\n\npage two"])

    def test_pages_are_never_split_when_they_fit(self):
        page = "x" * (ingestion.CHUNK_SIZE - 10)
        chunks = ingestion._chunk_pages([page, page])
        self.assertEqual(chunks, [page, page])

    def test_oversized_page_splits_on_line_boundaries_without_overlap(self):
        lines = [f"2024-01-{i % 28 + 1:02d} MERCHANT {i:05d} -{i}.00" for i in range(2000)]
        chunks = ingestion._chunk_pages(["\n".join(lines)])

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), ingestion.CHUNK_SIZE)
        # Re-joining the chunks reproduces every line exactly once, in order.
        rejoined = [line for c in chunks for line in c.split("\n")]
        self.assertEqual(rejoined, lines)

    def test_single_overlong_line_becomes_its_own_chunk(self):
        long_line = "y" * (ingestion.CHUNK_SIZE + 100)
        chunks = ingestion._chunk_pages([f"short\n{long_line}\nshort2"])
        self.assertIn(long_line, chunks)

    def test_header_is_repeated_and_counted_in_budget(self):
        header = "Date | Description | Amount\n" + "-" * 60
        body = "\n".join(f"2024-01-01 | Shop {i} | -1.00" for i in range(1000))
        chunks = ingestion._chunk_pages([body], header=header)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith(header + "\n"))
            self.assertLessEqual(len(chunk), ingestion.CHUNK_SIZE)
        rows = [l for c in chunks for l in c.split("\n")[2:]]
        self.assertEqual(rows, body.split("\n"))

    @patch("core.ingestion.extract_statement_metadata")
    @patch("core.ingestion.extract_transactions", return_value=[])
    def test_csv_parser_chosen_by_source_name_not_temp_name(self, mock_extract, _meta):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement-abc123.tmp"
            path.write_text("Date,Description,Amount\n2024-01-01,Store,-10.00\n")
            pages, header = ingestion.read_pages(path, "hdfc_jan.csv")
        ingestion.extract_statement(pages, header=header)

        sent = mock_extract.call_args[0][0]
        self.assertTrue(sent.startswith("Date | Description | Amount"))
        self.assertIn("2024-01-01 | Store | -10.00", sent)


class TestMonthlyTrend(unittest.TestCase):
    """Item 2: trend walks calendar months, never skipping or repeating."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        init_db(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def _labels(self, as_of, n=6):
        return [t.month_label for t in get_monthly_trend(n, self.db, as_of=as_of)]

    def test_march_includes_february(self):
        # Old code: Mar 1 - 30 days = Jan 30, so February was missing.
        self.assertEqual(
            self._labels(date(2025, 3, 31), 3), ["2025-01", "2025-02", "2025-03"]
        )

    def test_year_boundary(self):
        self.assertEqual(
            self._labels(date(2025, 1, 15), 3), ["2024-11", "2024-12", "2025-01"]
        )

    def test_twelve_months_distinct_and_consecutive(self):
        labels = self._labels(date(2025, 7, 1), 12)
        self.assertEqual(len(set(labels)), 12)
        self.assertEqual(labels[0], "2024-08")
        self.assertEqual(labels[-1], "2025-07")

    def test_sums_per_month(self):
        import_statement(
            [
                {"transaction_date": "2025-02-10", "raw_description": "Salary", "amount": 1000.0},
                {"transaction_date": "2025-02-11", "raw_description": "Rent", "amount": -400.0},
            ],
            account_id=get_or_create_account(db_path=self.db),
            file_name="feb.csv",
            file_sha256="feb",
            db_path=self.db,
        )
        trend = get_monthly_trend(2, self.db, as_of=date(2025, 3, 5))
        feb = trend[0]
        self.assertEqual((feb.month_label, feb.income, feb.expense), ("2025-02", 1000.0, 400.0))
        self.assertEqual((trend[1].income, trend[1].expense), (0.0, 0.0))


class TestUploadHandling(unittest.TestCase):
    """Items 1 and 19: uploads never use the client filename as a path and
    are not retained.  (Deletion after extraction is tested on the worker in
    test_review_pipeline.)"""

    def setUp(self):
        self.client = TestClient(app)

    def test_sanitize_filename(self):
        self.assertEqual(_sanitize_filename("../../etc/passwd.csv"), "passwd.csv")
        self.assertEqual(_sanitize_filename("..\\..\\win\\stmt.pdf"), "stmt.pdf")
        self.assertEqual(_sanitize_filename("/abs/path/a.csv"), "a.csv")
        self.assertEqual(_sanitize_filename("bad\x00\nname.csv"), "badname.csv")
        self.assertEqual(_sanitize_filename(".."), "upload")
        self.assertEqual(_sanitize_filename(None), "upload")

    @patch("web.app.is_ollama_available", return_value=True)
    def test_traversal_name_is_queued_from_private_temp_file(self, _avail):
        with patch("web.app.jobs.submit") as submit:
            res = self.client.post(
                "/upload",
                files={"file": ("../../evil.csv", b"Date,Description,Amount\nevil\n", "text/csv")},
                data={"model": "llama3.1"},
            )

        self.assertEqual(res.status_code, 200)
        job = submit.call_args[0][0]
        self.assertEqual(job.source_name, "evil.csv")
        self.assertNotIn("evil", job.file_path.name)
        self.assertEqual(job.file_path.parent, jobs.tmp_dir())
        self.assertTrue(job.file_path.exists())
        job.file_path.unlink()
        self.assertFalse((_REPO_ROOT.parent / "evil.csv").exists())

    @patch("web.app.is_ollama_available", return_value=True)
    def test_rejects_unsupported_extension(self, _avail):
        with patch("web.app.jobs.submit") as submit:
            res = self.client.post("/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
        submit.assert_not_called()
        self.assertIn("Unsupported file type", res.text)

    @patch("web.app._MAX_UPLOAD_BYTES", 10)
    @patch("web.app.is_ollama_available", return_value=True)
    def test_rejects_oversized_upload_and_removes_partial_file(self, _avail):
        before = set(jobs.tmp_dir().iterdir())
        with patch("web.app.jobs.submit") as submit:
            res = self.client.post("/upload", files={"file": ("big.csv", b"x" * 100, "text/csv")})
        submit.assert_not_called()
        self.assertIn("larger than", res.text)
        self.assertEqual(set(jobs.tmp_dir().iterdir()), before)


class TestQueryParamValidation(unittest.TestCase):
    """Item 7: out-of-range month/year never causes a 500."""

    def setUp(self):
        self.client = TestClient(app)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_dashboard_bad_params_fall_back(self, _avail):
        for qs in ("?year=2025&month=13", "?year=2025&month=0", "?year=0&month=5", "?year=99999&month=1"):
            with self.subTest(qs=qs):
                self.assertEqual(self.client.get("/" + qs).status_code, 200)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_transactions_bad_params(self, _avail):
        for qs in ("?year=2025&month=13", "?page=0", "?page=-5"):
            with self.subTest(qs=qs):
                self.assertEqual(self.client.get("/transactions" + qs).status_code, 200)


class TestNoRemoteAssets(unittest.TestCase):
    """Item 3: templates and static CSS/JS load nothing from the network."""

    def test_no_external_urls(self):
        web = _REPO_ROOT / "web"
        files = list((web / "templates").rglob("*.html")) + [
            p for p in (web / "static").iterdir() if p.suffix in {".css", ".js"} and p.name != "htmx.min.js"
        ]
        pattern = re.compile(r"(src|href)\s*=\s*[\"']https?://|@import\s+url\(\s*[\"']?https?://")
        offenders = [str(f.relative_to(_REPO_ROOT)) for f in files if pattern.search(f.read_text())]
        self.assertEqual(offenders, [])

    def test_htmx_is_vendored(self):
        self.assertTrue((_REPO_ROOT / "web" / "static" / "htmx.min.js").is_file())


if __name__ == "__main__":
    unittest.main()
