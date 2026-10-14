"""Tests for schema v2: migrations (Appendix B), dedup (§5), money (§2),
rules engine (§6) and transfer exclusion (§8)."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import hashlib
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import core.database as db
from core import migrations
from core.analytics import get_category_breakdown, get_monthly_summary
from core.categorizer import add_rule, remove_rule
from core.models import compute_transaction_id, to_minor

_REAL_DB = Path(__file__).resolve().parent.parent / "data" / "expenses.db"

# The original schema, verbatim from core/database.py before v2.
_V1_DDL = """
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    transaction_date TEXT NOT NULL,
    value_date TEXT,
    raw_description TEXT NOT NULL,
    cleaned_merchant TEXT,
    amount REAL NOT NULL,
    category TEXT DEFAULT 'Uncategorized',
    source_file TEXT NOT NULL,
    injected_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trans_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_trans_category ON transactions(category);
CREATE TABLE IF NOT EXISTS category_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    cleaned_merchant TEXT
);
"""


def _v1_id(date, desc, amount):
    return hashlib.sha256(f"{date}|{desc}|{amount:.2f}".encode()).hexdigest()


class _TempDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "expenses.db"

    def tearDown(self):
        self._tmp.cleanup()

    def q(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class TestTestIsolation(unittest.TestCase):
    def test_tests_never_use_the_real_database(self):
        self.assertNotEqual(db.DEFAULT_DB_PATH.resolve(), _REAL_DB.resolve())


class TestFreshSchema(_TempDB):
    def test_fresh_db_is_created_at_latest_version(self):
        self.assertIsNone(db.init_db(self.path))
        self.assertEqual(self.q("PRAGMA user_version")[0][0], migrations.SCHEMA_VERSION)
        tables = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(
            {"accounts", "statements", "staged_rows", "transactions", "category_rules", "csv_mappings"}
            <= tables
        )

    def test_init_is_idempotent(self):
        db.init_db(self.path)
        db.init_db(self.path)
        self.assertEqual(self.q("PRAGMA user_version")[0][0], 2)

    def test_unknown_version_is_refused(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version = 7")
        conn.close()
        with self.assertRaises(RuntimeError):
            db.init_db(self.path)

    def test_connection_pragmas(self):
        db.init_db(self.path)
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()


class TestMigrationV1ToV2(_TempDB):
    def _make_v1(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(_V1_DDL)
        rows = [
            # date, desc, amount, category, merchant, file, injected_at
            ("2025-01-05", "AMZN Mktp IN", -499.99, "Shopping", "Amazon", "jan.pdf", "2025-02-01 10:00:00"),
            ("2025-01-06", "SALARY ACME", 50000.0, "Uncategorized", None, "jan.pdf", "2025-02-01 10:00:01"),
            ("2025-01-07", "Old  Rule  Shop", -10.0, "Groceries", None, "jan.pdf", "2025-02-01 10:00:02"),
            ("2025-01-08", "Zero row", 0.0, "Uncategorized", None, "jan.pdf", "2025-02-01 10:00:03"),
            # Same day/amount as jan.pdf's AMZN row but extra whitespace: v1 kept
            # both (different hash); v2 normalizes and treats it as the same row.
            ("2025-01-05", "AMZN  Mktp IN", -499.99, "Shopping", None, "feb.pdf", "2025-03-01 09:00:00"),
            ("2025-02-02", "Coffee", -150.0, "Uncategorized", None, "feb.pdf", "2025-03-01 09:00:01"),
        ]
        for d, desc, amt, cat, merch, f, at in rows:
            conn.execute(
                "INSERT INTO transactions (id, transaction_date, raw_description, cleaned_merchant,"
                " amount, category, source_file, injected_at) VALUES (?,?,?,?,?,?,?,?)",
                (_v1_id(d, desc, amt), d, desc, merch, amt, cat, f, at),
            )
        conn.execute(
            "INSERT INTO category_rules (pattern, category, cleaned_merchant) VALUES ('amzn', 'Shopping', 'Amazon')"
        )
        conn.commit()
        conn.close()

    def test_migrates_v1_database(self):
        self._make_v1()
        report = db.init_db(self.path)

        self.assertEqual(self.q("PRAGMA user_version")[0][0], 2)
        self.assertEqual(report.statements_created, 2)
        self.assertEqual(report.rows_dropped_zero_amount, 1)
        self.assertEqual(report.rows_merged, 1)
        self.assertEqual(report.rows_migrated, 4)

        # Backup is the untouched v1 file.
        self.assertEqual(report.backup_path.name, "expenses.db.v1.bak")
        bak = sqlite3.connect(report.backup_path)
        self.assertEqual(bak.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 6)
        self.assertEqual(bak.execute("PRAGMA user_version").fetchone()[0], 0)
        bak.close()

        (account_id,) = self.q("SELECT id FROM accounts WHERE name = 'Default'")[0]
        statements = dict(self.q("SELECT file_name, file_sha256 FROM statements"))
        self.assertEqual(statements, {"jan.pdf": "migrated:jan.pdf", "feb.pdf": "migrated:feb.pdf"})

        cols = {r[1] for r in self.q("PRAGMA table_info(transactions)")}
        self.assertNotIn("amount", cols)
        self.assertNotIn("source_file", cols)

        amzn = self.q(
            "SELECT id, amount_minor, category, category_source, rule_id, cleaned_merchant"
            " FROM transactions WHERE transaction_date = '2025-01-05'"
        )
        self.assertEqual(len(amzn), 1)
        tx_id, minor, cat, source, rule_id, merchant = amzn[0]
        self.assertEqual(minor, -49999)
        self.assertEqual(tx_id, compute_transaction_id(account_id, "2025-01-05", "AMZN Mktp IN", -49999, 0))
        self.assertEqual((cat, source, merchant), ("Shopping", "rule", "Amazon"))
        self.assertIsNotNone(rule_id)

        # Category from a rule that no longer exists reverts (§6), and the
        # stored description is whitespace-normalized.
        self.assertEqual(
            self.q("SELECT raw_description, category, category_source FROM transactions"
                   " WHERE transaction_date = '2025-01-07'"),
            [("Old Rule Shop", "Uncategorized", "default")],
        )
        rule_cols = {r[1] for r in self.q("PRAGMA table_info(category_rules)")}
        self.assertTrue({"priority", "mark_transfer", "account_id"} <= rule_cols)
        self.assertEqual(self.q("PRAGMA foreign_key_check"), [])

        # The migrated DB works through the normal API.
        txns = db.get_transactions(self.path, year=2025, month=1)
        self.assertEqual({t.source_file for t in txns}, {"jan.pdf"})
        self.assertEqual(sorted(t.amount for t in txns), [-499.99, -10.0, 50000.0])

    def test_failed_migration_rolls_back(self):
        self._make_v1()
        with patch.object(db, "_run_rules_engine", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                db.init_db(self.path)

        self.assertEqual(self.q("PRAGMA user_version")[0][0], 0)
        cols = {r[1] for r in self.q("PRAGMA table_info(transactions)")}
        self.assertIn("amount", cols)
        tables = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("accounts", tables)
        self.assertEqual(self.q("SELECT COUNT(*) FROM transactions")[0][0], 6)

        # A later retry succeeds and does not overwrite the first backup.
        report = db.init_db(self.path)
        self.assertNotEqual(report.backup_path.name, "expenses.db.v1.bak")
        self.assertTrue((self.path.parent / "expenses.db.v1.bak").exists())


class TestDedupAndMoney(_TempDB):
    def setUp(self):
        super().setUp()
        db.init_db(self.path)
        self.account = db.get_or_create_account(db_path=self.path)

    def _import(self, rows, sha):
        return db.import_statement(
            rows, account_id=self.account, file_name=f"{sha}.csv", file_sha256=sha, db_path=self.path
        )

    def test_recommitting_same_rows_inserts_nothing(self):
        rows = [{"transaction_date": "2025-03-01", "raw_description": "Shop", "amount": -5}]
        self.assertEqual(self._import(rows, "a")[1:], (1, 0, 0))
        self.assertEqual(self._import(rows, "b")[1:], (0, 1, 0))

    def test_identical_same_day_rows_are_both_kept(self):
        coffee = {"transaction_date": "2025-03-01", "raw_description": "Coffee", "amount": -150}
        _, inserted, dupes, _ = self._import([coffee, dict(coffee)], "a")
        self.assertEqual((inserted, dupes), (2, 0))

    def test_overlapping_statements_insert_overlap_once(self):
        coffee = {"transaction_date": "2025-03-15", "raw_description": "Coffee", "amount": -150}
        march = [
            {"transaction_date": "2025-03-01", "raw_description": "Rent", "amount": -20000},
            coffee, dict(coffee),
        ]
        april = [
            coffee, dict(coffee),
            {"transaction_date": "2025-04-02", "raw_description": "Rent", "amount": -20000},
        ]
        self._import(march, "mar")
        _, inserted, dupes, _ = self._import(april, "apr")
        self.assertEqual((inserted, dupes), (1, 2))
        self.assertEqual(db.get_transaction_count(self.path), 4)

    def test_zero_amount_rows_are_rejected(self):
        rows = [{"transaction_date": "2025-03-01", "raw_description": "Nothing", "amount": 0.001}]
        self.assertEqual(self._import(rows, "a")[1:], (0, 0, 1))

    def test_money_is_exact(self):
        self.assertEqual(sum(to_minor(0.10) for _ in range(3)), 30)
        self.assertEqual(to_minor("0.005"), 1)
        self.assertEqual(to_minor(-0.005), -1)
        self.assertEqual(to_minor(1234.56), 123456)
        rows = [{"transaction_date": "2025-03-01", "raw_description": f"x{i}", "amount": -0.1} for i in range(3)]
        self._import(rows, "a")
        self.assertEqual(get_monthly_summary(2025, 3, self.path).total_expense, 0.3)


class TestRulesEngine(_TempDB):
    def setUp(self):
        super().setUp()
        db.init_db(self.path)
        self.account = db.get_or_create_account(db_path=self.path)
        self.other = db.get_or_create_account("Card", "credit_card", db_path=self.path)
        db.import_statement(
            [
                {"transaction_date": "2025-05-01", "raw_description": "AMAZON PRIME VIDEO", "amount": -199},
                {"transaction_date": "2025-05-02", "raw_description": "CRED CLUB PAYMENT", "amount": -5000},
                {"transaction_date": "2025-05-03", "raw_description": "Café Ümlaut", "amount": -80},
            ],
            account_id=self.account, file_name="may.csv", file_sha256="may", db_path=self.path,
        )

    def _row(self, desc_like):
        return self.q(
            "SELECT category, category_source, cleaned_merchant, is_transfer FROM transactions"
            " WHERE raw_description LIKE ?", (f"%{desc_like}%",)
        )[0]

    def test_priority_then_lowest_id_wins(self):
        add_rule("amazon", "Shopping", db_path=self.path)
        add_rule("prime video", "Entertainment", db_path=self.path)  # same priority, higher id
        self.assertEqual(self._row("PRIME")[0], "Shopping")
        add_rule("prime video", "Entertainment", db_path=self.path, priority=5)
        self.assertEqual(self._row("PRIME")[0], "Entertainment")

    def test_manual_edits_win(self):
        conn = db.connect(self.path)
        conn.execute(
            "UPDATE transactions SET category = 'Gifts', category_source = 'manual'"
            " WHERE raw_description LIKE '%AMAZON%'"
        )
        conn.commit()
        conn.close()
        add_rule("amazon", "Shopping", db_path=self.path)
        self.assertEqual(self._row("AMAZON")[:2], ("Gifts", "manual"))

    def test_deleting_a_rule_reverts_its_rows(self):
        rule_id = add_rule("amazon", "Shopping", "Amazon", db_path=self.path)
        self.assertEqual(self._row("AMAZON")[:3], ("Shopping", "rule", "Amazon"))
        remove_rule(rule_id, db_path=self.path)
        self.assertEqual(self._row("AMAZON")[:3], ("Uncategorized", "default", None))

    def test_mark_transfer_and_exclusion_from_analytics(self):
        add_rule("cred club", "Card payment", db_path=self.path, mark_transfer=True)
        self.assertEqual(self._row("CRED")[3], 1)
        summary = get_monthly_summary(2025, 5, self.path)
        self.assertEqual(summary.total_expense, 279.0)
        self.assertNotIn("Card payment", {b.category for b in get_category_breakdown(2025, 5, self.path)})

    def test_account_scoped_rule(self):
        add_rule("amazon", "Shopping", db_path=self.path, account_id=self.other)
        self.assertEqual(self._row("AMAZON")[0], "Uncategorized")

    def test_case_insensitive_unicode_and_literal_wildcards(self):
        add_rule("CAFÉ ümlaut", "Food", db_path=self.path)
        self.assertEqual(self._row("Caf")[0], "Food")
        add_rule("%", "Everything", db_path=self.path)  # literal %, not a LIKE wildcard
        self.assertEqual(self._row("AMAZON")[0], "Uncategorized")


if __name__ == "__main__":
    unittest.main()
