"""Tests for families, members and account holders (app_spec.md §2.4,
§7.1, §7.2, §8.1 and Appendix C)."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from textual.widgets import DataTable, Label

import core.database as db
from core import migrations
from core.analytics import (
    get_category_breakdown,
    get_member_summaries,
    get_monthly_summary,
    get_uncategorized_count,
)
from tui.app import ExpenseTrackerTUI
from tui.screens.dashboard import DashboardScreen, StatCard
from tui.screens.transactions import TransactionsScreen
from web.app import app


def _seed(account_id, rows, db_path=None):
    db.import_statement(
        [{"transaction_date": d, "raw_description": desc, "amount": amt} for d, desc, amt in rows],
        account_id=account_id, file_name="s.csv", file_sha256=uuid.uuid4().hex, db_path=db_path,
    )


class _TempDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "expenses.db"
        db.init_db(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def q(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def household(self):
        """The Sharmas: Asha (primary) + Ravi (joint) on a joint savings
        account, Asha's own card, and Ravi's own account."""
        p = self.path
        self.family = db.create_family("The Sharmas", p)
        self.asha = db.create_member(self.family, "Asha", p)
        self.ravi = db.create_member(self.family, "Ravi", p)
        self.joint = db.create_account("Joint Savings", "bank", db_path=p)
        self.card = db.create_account("Asha Card", "credit_card", db_path=p)
        self.ravi_acct = db.create_account("Ravi Savings", "bank", db_path=p)
        self.other = db.create_account("Unlinked", "bank", db_path=p)
        db.set_account_holders(self.joint, self.asha, [self.ravi], p)
        db.set_account_holders(self.card, self.asha, [], p)
        db.set_account_holders(self.ravi_acct, self.ravi, [], p)
        _seed(self.joint, [("2052-03-01", "SALARY ASHA", 1000), ("2052-03-02", "RENT", -400)], p)
        _seed(self.card, [("2052-03-03", "SHOPPING", -50)], p)
        _seed(self.ravi_acct, [("2052-03-04", "SALARY RAVI", 500), ("2052-03-05", "FUEL", -30)], p)
        _seed(self.other, [("2052-03-06", "ELSEWHERE", -7)], p)


class TestMigrationV3(_TempDB):
    def test_fresh_db_is_v3(self):
        self.assertEqual(self.q("PRAGMA user_version")[0][0], 3)
        tables = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"families", "members", "account_members"} <= tables)

    def test_v2_database_migrates_with_data_unchanged(self):
        path = Path(self._tmp.name) / "v2.db"
        conn = sqlite3.connect(path)
        conn.isolation_level = None
        conn.execute("BEGIN")
        for ddl in (
            migrations._V2_ACCOUNTS, migrations._V2_STATEMENTS, migrations._V2_CATEGORY_RULES,
            migrations._v2_transactions(), *migrations._V2_TX_INDEXES,
            migrations._V2_STAGED_ROWS, migrations._V2_CSV_MAPPINGS,
        ):
            conn.execute(ddl)
        conn.execute("INSERT INTO accounts (name, kind) VALUES ('Default', 'bank')")
        conn.execute("PRAGMA user_version = 2")
        conn.execute("COMMIT")
        conn.close()

        self.assertIsNone(db.init_db(path))
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT name FROM accounts").fetchall(), [("Default",)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM account_members").fetchone()[0], 0)
        finally:
            conn.close()
        # No backup for an additive migration.
        self.assertFalse(list(Path(self._tmp.name).glob("*.bak")))

    def test_unknown_version_is_refused(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version = 4")
        conn.close()
        with self.assertRaises(RuntimeError):
            db.init_db(self.path)


class TestFamilyCrud(_TempDB):
    def test_names(self):
        p = self.path
        fam = db.create_family("  The   Sharmas ", p)
        self.assertEqual(db.list_families(p)["families"][0]["name"], "The Sharmas")
        for bad in ("", "   "):
            with self.assertRaises(db.FamilyError):
                db.create_family(bad, p)
        with self.assertRaises(db.FamilyError):
            db.create_family("The Sharmas", p)
        db.create_member(fam, "Asha", p)
        with self.assertRaises(db.FamilyError):
            db.create_member(fam, "Asha", p)
        other = db.create_family("The Iyers", p)
        db.create_member(other, "Asha", p)  # same name, different family
        with self.assertRaises(db.FamilyError):
            db.rename_family(other, "The Sharmas", p)
        with self.assertRaises(db.FamilyError):
            db.create_member(999, "Ghost", p)

    def test_delete_family_cascades_but_keeps_ledger(self):
        self.household()
        before = self.q("SELECT COUNT(*) FROM transactions")[0][0]
        db.delete_family(self.family, self.path)
        self.assertEqual(self.q("SELECT COUNT(*) FROM members")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM account_members")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM transactions")[0][0], before)
        self.assertEqual(len(db.list_families(self.path)["unlinked"]), 4)

    def test_deleting_an_account_removes_its_links(self):
        p = self.path
        fam = db.create_family("F", p)
        m = db.create_member(fam, "M", p)
        acct = db.create_account("Empty", db_path=p)
        db.set_account_holders(acct, m, [], p)
        db.delete_account(acct, p)
        self.assertEqual(self.q("SELECT COUNT(*) FROM account_members")[0][0], 0)


class TestHolderRules(_TempDB):
    def setUp(self):
        super().setUp()
        self.household()

    def test_second_primary_rejected_by_index(self):
        with self.assertRaises(sqlite3.IntegrityError):
            conn = db.connect(self.path)
            try:
                conn.execute(
                    "INSERT INTO account_members (account_id, member_id, role) VALUES (?, ?, 'primary')",
                    (self.joint, self.ravi),
                )
            finally:
                conn.close()

    def test_joint_without_primary_refused(self):
        with self.assertRaises(db.FamilyError):
            db.set_account_holders(self.card, None, [self.ravi], self.path)

    def test_cross_family_holder_refused(self):
        other = db.create_family("The Iyers", self.path)
        stranger = db.create_member(other, "Kiran", self.path)
        with self.assertRaises(db.FamilyError):
            db.set_account_holders(self.joint, self.asha, [stranger], self.path)
        # Unchanged after the refusal.
        self.assertEqual(
            sorted(self.q("SELECT member_id, role FROM account_members WHERE account_id = ?", (self.joint,))),
            sorted([(self.asha, "primary"), (self.ravi, "joint")]),
        )

    def test_deleting_primary_of_joint_account_refused(self):
        with self.assertRaises(db.FamilyError):
            db.delete_member(self.asha, self.path)
        db.delete_member(self.ravi, self.path)  # joint holder: allowed
        self.assertEqual(
            self.q("SELECT member_id, role FROM account_members WHERE account_id = ?", (self.joint,)),
            [(self.asha, "primary")],
        )

    def test_moving_a_member_who_shares_an_account_refused(self):
        other = db.create_family("The Iyers", self.path)
        with self.assertRaises(db.FamilyError):
            db.update_member(self.ravi, name="Ravi", family_id=other, db_path=self.path)
        db.set_account_holders(self.joint, self.asha, [], self.path)
        db.update_member(self.ravi, name="Ravi K", family_id=other, db_path=self.path)
        fams = {f["name"]: f for f in db.list_families(self.path)["families"]}
        self.assertEqual([a["name"] for a in fams["The Iyers"]["accounts"]], ["Ravi Savings"])

    def test_promote_joint_holder_and_unlink(self):
        db.set_account_holders(self.joint, self.ravi, [self.asha], self.path)
        self.assertEqual(
            dict(self.q("SELECT member_id, role FROM account_members WHERE account_id = ?", (self.joint,))),
            {self.ravi: "primary", self.asha: "joint"},
        )
        db.set_account_holders(self.joint, None, [], self.path)
        self.assertIn("Joint Savings", [a["name"] for a in db.list_families(self.path)["unlinked"]])


class TestScopeAnalytics(_TempDB):
    def setUp(self):
        super().setUp()
        self.household()

    def summary(self, **scope):
        s = get_monthly_summary(2052, 3, self.path, **scope)
        return s.total_income, s.total_expense

    def test_family_counts_each_transaction_once(self):
        self.assertEqual(self.summary(), (1500, 487))
        self.assertEqual(self.summary(family_id=self.family), (1500, 480))

    def test_member_scope_primary_and_joint(self):
        self.assertEqual(self.summary(member_id=self.asha), (1000, 450))
        self.assertEqual(self.summary(member_id=self.ravi), (500, 30))
        self.assertEqual(self.summary(member_id=self.ravi, include_joint=True), (1500, 430))

    def test_per_member_table_adds_up_to_family(self):
        rows = get_member_summaries(2052, 3, self.family, self.path)
        self.assertEqual([r["name"] for r in rows], ["Asha", "Ravi"])
        self.assertEqual(sum(r["income"] for r in rows), 1500)
        self.assertEqual(sum(r["expense"] for r in rows), 480)
        self.assertEqual({r["name"]: r["joint_accounts"] for r in rows}, {"Asha": 0, "Ravi": 1})

    def test_intersections_and_empty_scope(self):
        other = db.create_family("The Iyers", self.path)
        self.assertEqual(self.summary(family_id=other), (0, 0))
        self.assertEqual(self.summary(family_id=other, member_id=self.asha), (0, 0))
        self.assertEqual(self.summary(member_id=self.asha, account_id=self.ravi_acct), (0, 0))
        self.assertEqual(self.summary(member_id=self.asha, account_id=self.card), (0, 50))
        self.assertEqual(get_category_breakdown(2052, 3, self.path, family_id=other), [])

    def test_uncategorized_count_matches_list(self):
        scope = dict(family_id=self.family, member_id=self.ravi, include_joint=True)
        n = get_uncategorized_count(2052, 3, self.path, **scope)
        listed = db.get_transaction_count(self.path, year=2052, month=3, uncategorized=True, **scope)
        self.assertEqual((n, listed), (4, 4))

    def test_payment_between_members_counts_both_sides(self):
        _seed(self.card, [("2052-03-10", "TO RAVI", -100)], self.path)
        _seed(self.ravi_acct, [("2052-03-10", "FROM ASHA", 100)], self.path)
        self.assertEqual(self.summary(member_id=self.asha), (1000, 550))
        self.assertEqual(self.summary(member_id=self.ravi), (600, 30))
        income, expense = self.summary(family_id=self.family)
        self.assertEqual(income - expense, 1500 - 480)  # net savings unchanged


@patch("web.app.is_ollama_available", return_value=False)
@patch("web.app.list_available_models", return_value=[])
class TestFamiliesWeb(unittest.TestCase):
    """Uses the module's shared temp DB (EXPENSE_TRACKER_DB)."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        tag = uuid.uuid4().hex[:6]
        self.family = db.create_family(f"Fam {tag}")
        self.asha = db.create_member(self.family, f"Asha {tag}")
        self.ravi = db.create_member(self.family, f"Ravi {tag}")
        self.joint = db.create_account(f"Joint {tag}")
        db.set_account_holders(self.joint, self.asha, [self.ravi])
        _seed(self.joint, [("2053-04-01", f"JOINTSPEND {tag}", -321)])
        self.tag = tag

    def tearDown(self):
        db.delete_family(self.family)

    def test_dashboard_family_and_member_filters(self, *_):
        page = self.client.get(f"/?year=2053&month=4&family_id={self.family}").text
        self.assertIn("By Member", page)
        self.assertIn(f"Asha {self.tag}", page)
        self.assertIn("₹321", page)
        self.assertIn(f"&family_id={self.family}", page)  # month links keep the scope

        ravi = self.client.get(f"/?year=2053&month=4&member_id={self.ravi}").text
        self.assertNotIn("₹321", ravi)
        ravi_joint = self.client.get(f"/?year=2053&month=4&member_id={self.ravi}&include_joint=1").text
        self.assertIn("₹321", ravi_joint)
        self.assertIn("include_joint=1", ravi_joint)
        self.assertNotIn("By Member", ravi_joint)

    def test_bad_params_never_500(self, *_):
        for qs in ("family_id=abc", "member_id=999999", "include_joint=x", "family_id=-1&member_id=zz"):
            self.assertEqual(self.client.get(f"/?{qs}").status_code, 200, qs)
            self.assertEqual(self.client.get(f"/transactions?{qs}").status_code, 200, qs)

    def test_transactions_member_filter(self, *_):
        page = self.client.get(f"/transactions?year=2053&member_id={self.ravi}&include_joint=1").text
        self.assertIn(f"JOINTSPEND {self.tag}", page)
        self.assertNotIn(
            f"JOINTSPEND {self.tag}", self.client.get(f"/transactions?year=2053&member_id={self.ravi}").text
        )

    def test_families_routes(self, *_):
        page = self.client.get("/families").text
        self.assertIn(f"Fam {self.tag}", page)
        res = self.client.post(f"/families/{self.family}/members", data={"name": f"Kiran {self.tag}"})
        self.assertIn("added", res.text)
        res = self.client.post(f"/families/{self.family}/members", data={"name": f"Kiran {self.tag}"})
        self.assertIn("already has a member", res.text)
        res = self.client.delete(f"/members/{self.asha}")
        self.assertIn("Make another holder primary first", res.text)
        res = self.client.post(
            f"/accounts/{self.joint}/holders",
            data={"primary_member_id": str(self.ravi), "joint_member_ids": [str(self.asha)]},
        )
        self.assertIn("Account holders saved", res.text)
        accounts = self.client.get("/accounts").text
        self.assertIn(f"Ravi {self.tag} (primary), Asha {self.tag} (joint)", accounts)
        res = self.client.post(f"/families/{self.family}", data={"name": ""})
        self.assertIn("needs a name", res.text)


class TestFamiliesTUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "expenses.db"
        db.init_db(self.path)
        _TempDB.household(self)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bindings_have_actions(self):
        for cls in (DashboardScreen, TransactionsScreen):
            bound = {b.key: b.action for b in cls.BINDINGS}
            for key in ("f", "m", "j"):
                self.assertTrue(hasattr(cls, f"action_{bound[key]}"), f"{cls.__name__}: {key}")
                self.assertFalse(next(b for b in cls.BINDINGS if b.key == key).priority)

    async def test_family_member_joint_keys(self):
        app = ExpenseTrackerTUI(year=2052, month=3, db_path=self.path)

        def cards():
            return [str(c.query_one(".stat-value", Label).render()) for c in app.screen.query(StatCard)]

        async with app.run_test(size=(160, 40)) as pilot:
            self.assertEqual(cards()[:2], ["₹1,500", "₹487"])
            await pilot.press("f")  # The Sharmas
            self.assertEqual(cards()[:2], ["₹1,500", "₹480"])
            await pilot.press("m")  # Asha
            self.assertEqual(cards()[:2], ["₹1,000", "₹450"])
            await pilot.press("m")  # Ravi
            self.assertEqual(cards()[:2], ["₹500", "₹30"])
            await pilot.press("j")
            self.assertIn("Ravi (+ joint)", str(app.screen.query_one("#dash-title", Label).render()))
            self.assertEqual(cards()[:2], ["₹1,500", "₹430"])

            await pilot.press("t")  # carries over to the transaction list
            self.assertEqual(app.screen.query_one(DataTable).row_count, 4)

            await pilot.press("f")  # back to all families: member and joint reset
            self.assertEqual((app.member_id, app.include_joint), (None, False))
            self.assertEqual(app.screen.query_one(DataTable).row_count, 6)


if __name__ == "__main__":
    unittest.main()
