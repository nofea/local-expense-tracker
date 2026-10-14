"""Tests for manual transaction edits and deletes (Appendix A item 14),
deleting an import (item 15) and the per-month uncategorized count
(item 17)."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import html
import re
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

import core.database as db
from core import staging
from core.analytics import get_monthly_summary, get_uncategorized_count
from core.categorizer import add_rule
from web.app import app


def _rows(year, month, n=1, desc="SHOP", amount=-10.0, day=1):
    return [
        {"transaction_date": f"{year:04d}-{month:02d}-{day:02d}", "raw_description": f"{desc} {i}", "amount": amount}
        for i in range(n)
    ]


class _TempDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "expenses.db"
        db.init_db(self.path)
        self.account = db.get_or_create_account(db_path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def seed(self, rows, sha=None):
        return db.import_statement(
            rows, account_id=self.account, file_name="s.csv",
            file_sha256=sha or uuid.uuid4().hex, db_path=self.path,
        )[0]

    def one(self, search):
        [tx] = db.get_transactions(self.path, search=search)
        return tx


class TestManualEdits(_TempDB):
    def test_manual_edit_survives_rules(self):
        self.seed(_rows(2025, 5, desc="SWIGGY ORDER"))
        tx = self.one("SWIGGY")
        db.set_manual_category(tx.id, category="  Dining   out ", cleaned_merchant=" Swiggy ",
                               is_transfer=False, db_path=self.path)
        add_rule("swiggy", "Food", db_path=self.path)

        tx = self.one("SWIGGY")
        self.assertEqual((tx.category, tx.cleaned_merchant, tx.category_source, tx.rule_id),
                         ("Dining out", "Swiggy", "manual", None))

    def test_blank_category_means_uncategorized(self):
        self.seed(_rows(2025, 5))
        tx = self.one("SHOP")
        db.set_manual_category(tx.id, category="  ", cleaned_merchant="", is_transfer=False, db_path=self.path)
        tx = self.one("SHOP")
        self.assertEqual((tx.category, tx.cleaned_merchant), ("Uncategorized", None))

    def test_reset_hands_the_row_back_to_rules(self):
        self.seed(_rows(2025, 5, desc="CRED CLUB"))
        add_rule("cred", "Card payment", db_path=self.path)
        tx = self.one("CRED")
        db.set_manual_category(tx.id, category="Misc", cleaned_merchant=None, is_transfer=True, db_path=self.path)

        self.assertTrue(db.reset_to_rules(tx.id, self.path))
        tx = self.one("CRED")
        self.assertEqual((tx.category, tx.category_source, tx.is_transfer), ("Card payment", "rule", False))

        add_rule("cred", "Card payment", db_path=self.path, mark_transfer=True)
        db.set_manual_category(tx.id, category="Misc", cleaned_merchant=None, is_transfer=False, db_path=self.path)
        db.reset_to_rules(tx.id, self.path)
        self.assertTrue(self.one("CRED").is_transfer)

    def test_manual_transfer_is_excluded_from_totals(self):
        self.seed(_rows(2025, 6, desc="RENT", amount=-100.0) + _rows(2025, 6, desc="OWN ACCOUNT", amount=-50.0))
        db.set_manual_category(self.one("OWN").id, category="Transfer", cleaned_merchant=None,
                               is_transfer=True, db_path=self.path)
        self.assertEqual(get_monthly_summary(2025, 6, self.path).total_expense, 100.0)

    def test_missing_transaction(self):
        self.assertFalse(db.set_manual_category("nope", category="x", cleaned_merchant=None,
                                                is_transfer=False, db_path=self.path))
        self.assertFalse(db.reset_to_rules("nope", self.path))
        self.assertIsNone(db.get_transaction("nope", self.path))

    def test_delete_transaction(self):
        self.seed(_rows(2025, 5, n=2))
        tx = self.one("SHOP 0")
        self.assertTrue(db.delete_transaction(tx.id, self.path))
        self.assertEqual(db.get_transaction_count(self.path), 1)


class TestFiltersAndUncategorized(_TempDB):
    def setUp(self):
        super().setUp()
        self.seed(_rows(2025, 7, n=3, desc="MISC") + _rows(2025, 7, desc="NETFLIX") +
                  _rows(2025, 7, desc="CARD PAYMENT") + _rows(2025, 8, n=2, desc="LATER"))
        add_rule("netflix", "Entertainment", db_path=self.path)
        add_rule("card payment", "Card", db_path=self.path, mark_transfer=True)

    def test_uncategorized_is_per_month_and_ignores_transfers(self):
        self.assertEqual(get_uncategorized_count(2025, 7, self.path), 3)
        self.assertEqual(get_uncategorized_count(2025, 8, self.path), 2)
        self.assertEqual(get_uncategorized_count(2025, 9, self.path), 0)

    def test_uncategorized_filter_matches_the_count(self):
        txns = db.get_transactions(self.path, year=2025, month=7, uncategorized=True)
        self.assertEqual(len(txns), get_uncategorized_count(2025, 7, self.path))
        self.assertEqual(db.get_transaction_count(self.path, year=2025, month=7, uncategorized=True), 3)

    def test_transfer_filter(self):
        count = lambda t: db.get_transaction_count(self.path, year=2025, month=7, transfers=t)
        self.assertEqual((count("show"), count("hide"), count("only")), (5, 4, 1))


class TestDeleteImport(_TempDB):
    def test_delete_committed_import_cascades_and_allows_reupload(self):
        keep = self.seed(_rows(2025, 1, desc="KEEP"))
        gone = self.seed(_rows(2025, 2, n=3, desc="GONE"), sha="file-sha")
        self.assertEqual(staging.delete_import(gone, self.path), 3)

        self.assertIsNone(staging.get_statement(gone, self.path))
        self.assertEqual([t.statement_id for t in db.get_transactions(self.path)], [keep])
        # The same file can now be uploaded again.
        staging.create_import(account_id=self.account, file_name="again.csv",
                              file_sha256="file-sha", model=None, db_path=self.path)

    def test_delete_import_in_review_removes_staged_rows(self):
        sid = staging.create_import(account_id=self.account, file_name="x.csv",
                                    file_sha256="x", model=None, db_path=self.path)
        staging.stage_rows(sid, _rows(2025, 3), source_text="SHOP 0", db_path=self.path)
        staging.delete_import(sid, self.path)
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM staged_rows").fetchone()[0], 0)
        conn.close()

    def test_running_import_cannot_be_deleted(self):
        sid = staging.create_import(account_id=self.account, file_name="x.csv",
                                    file_sha256="y", model=None, db_path=self.path)
        with self.assertRaisesRegex(staging.ReviewError, "still running"):
            staging.delete_import(sid, self.path)
        with self.assertRaisesRegex(staging.ReviewError, "not found"):
            staging.delete_import(99999, self.path)


@patch("web.app.list_available_models", return_value=[])
@patch("web.app.is_ollama_available", return_value=False)
class TestWebLedger(unittest.TestCase):
    """Through HTTP, against the shared test DB.  Each test uses its own
    year so the data sets don't overlap."""

    def setUp(self):
        self.client = TestClient(app)
        self.account = db.get_or_create_account()

    def seed(self, rows):
        return db.import_statement(rows, account_id=self.account, file_name="web.csv",
                                   file_sha256=uuid.uuid4().hex)[0]

    def test_edit_save_reset_delete_row(self, *_):
        self.seed(_rows(2041, 1, desc="AMAZON MKTP"))
        [tx] = db.get_transactions(year=2041, month=1)

        form = self.client.get(f"/transactions/{tx.id}/edit").text
        self.assertIn('name="category"', form)
        self.assertIn("/rules?pattern=AMAZON+MKTP+0", html.unescape(form))

        row = self.client.post(f"/transactions/{tx.id}", data={
            "category": "Shopping", "cleaned_merchant": "Amazon", "is_transfer": "true"}).text
        self.assertIn("Shopping", row)
        self.assertIn(">manual<", row)
        self.assertIn(">transfer<", row)
        self.assertIn("Reset to rules", row)

        row = self.client.post(f"/transactions/{tx.id}/reset").text
        self.assertNotIn(">manual<", row)
        self.assertIn("Uncategorized", row)

        res = self.client.delete(f"/transactions/{tx.id}")
        self.assertEqual((res.status_code, res.text), (200, ""))
        self.assertEqual(self.client.delete(f"/transactions/{tx.id}").status_code, 404)
        self.assertEqual(self.client.get(f"/transactions/{tx.id}/row").status_code, 404)

    def test_dashboard_links_to_this_months_uncategorized(self, *_):
        self.seed(_rows(2042, 3, n=4) + _rows(2042, 4, n=1))
        page = self.client.get("/?year=2042&month=3").text
        card = re.search(r'<a class="card stat-card uncategorized.*?</a>', page, re.S).group(0)
        self.assertIn('href="/transactions?year=2042&month=3&uncategorized=1"', card)
        self.assertIn('<div class="stat-value">4</div>', card)

        listing = self.client.get("/transactions?year=2042&month=3&uncategorized=1").text
        self.assertIn("4 transactions found", listing)

    def test_pagination_keeps_filters(self, *_):
        self.seed(_rows(2043, 2, n=35))
        page = self.client.get("/transactions?year=2043&month=2&uncategorized=1&transfers=hide").text
        next_link = html.unescape(re.search(r'href="(/transactions\?page=2[^"]*)"', page).group(1))
        qs = parse_qs(urlparse(next_link).query)
        self.assertEqual(
            {k: v[0] for k, v in qs.items()},
            {"page": "2", "year": "2043", "month": "2", "uncategorized": "1", "transfers": "hide"},
        )
        self.assertIn("35 transactions found", page)
        self.assertEqual(self.client.get(next_link).text.count('id="tx-'), 5)

    def test_bad_transfer_filter_falls_back(self, *_):
        self.assertEqual(self.client.get("/transactions?transfers=bogus").status_code, 200)

    def test_rules_prefill(self, *_):
        page = self.client.get("/rules?pattern=UPI-SWIGGY-8812&category=Food").text
        self.assertIn('value="UPI-SWIGGY-8812"', page)
        self.assertIn('value="Food"', page)
        self.assertIn("Shorten it", page)
        self.assertNotIn("Shorten it", self.client.get("/rules").text)

    def test_delete_import_from_imports_page(self, *_):
        sid = self.seed(_rows(2044, 5, n=2))
        page = self.client.get("/imports").text
        self.assertIn(f'hx-delete="/imports/{sid}"', page)

        res = self.client.delete(f"/imports/{sid}").text
        self.assertIn(f"Import #{sid} deleted, with its 2 transaction(s).", res)
        self.assertNotIn(f'hx-delete="/imports/{sid}"', res)
        self.assertEqual(db.get_transaction_count(year=2044), 0)
        self.assertIn("Import not found", self.client.delete(f"/imports/{sid}").text)


if __name__ == "__main__":
    unittest.main()
