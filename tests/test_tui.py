"""Textual Pilot tests for the TUI (Appendix A item 18, spec §7.2 and the
§11 Phase 5 acceptance criteria)."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import unittest
import uuid
from pathlib import Path

from textual.widgets import DataTable, Input, Label

import core.database as db
from core.analytics import get_monthly_summary
from core.categorizer import add_rule
from tui.app import ExpenseTrackerTUI
from tui.screens.dashboard import DashboardScreen, StatCard
from tui.screens.prompt import PromptScreen
from tui.screens.transactions import TransactionsScreen


def _rows(year, month, n, desc="SHOP", amount=-1.0):
    return [
        {"transaction_date": f"{year:04d}-{month:02d}-{(i % 28) + 1:02d}",
         "raw_description": f"{desc} {i}", "amount": amount - i}
        for i in range(n)
    ]


class TestTUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "expenses.db"
        db.init_db(self.path)
        self.account = db.get_or_create_account(db_path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def seed(self, rows, account=None):
        db.import_statement(rows, account_id=account or self.account, file_name="s.csv",
                            file_sha256=uuid.uuid4().hex, db_path=self.path)

    def app(self):
        return ExpenseTrackerTUI(year=2051, month=1, db_path=self.path)

    @staticmethod
    def text(screen, selector):
        return str(screen.query_one(selector, Label).render())

    @staticmethod
    def cards(screen):
        return [str(c.query_one(".stat-value", Label).render()) for c in screen.query(StatCard)]

    def test_every_spec_binding_has_an_action(self):
        spec = {
            ExpenseTrackerTUI: {"d", "t", "q"},
            DashboardScreen: {"left", "right", "a", "r"},
            TransactionsScreen: {"left", "right", "pageup", "pagedown", "slash", "a", "c", "x", "r"},
        }
        for cls, keys in spec.items():
            bound = {b.key: b.action for b in cls.BINDINGS}
            self.assertLessEqual(keys, set(bound), cls.__name__)
            for key in keys:
                action = bound[key].split("(")[0]
                self.assertTrue(hasattr(cls, f"action_{action}"), f"{cls.__name__}: {key} -> {action}")

    async def test_pages_through_more_than_50_rows_and_changes_a_category(self):
        self.seed(_rows(2051, 1, 60))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("t")
            screen = app.screen
            table = screen.query_one(DataTable)
            self.assertIsInstance(screen, TransactionsScreen)
            self.assertEqual(table.row_count, 50)
            self.assertIn("Page 1/2 · 60 total", self.text(screen, "#txn-footer"))

            await pilot.press("pagedown")
            self.assertEqual((table.row_count, screen.page), (10, 2))
            await pilot.press("pagedown")  # already on the last page
            self.assertEqual(screen.page, 2)

            # Change the category of the selected row on page 2.
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            await pilot.press("c")
            self.assertIsInstance(app.screen, PromptScreen)
            prompt_input = app.screen.query_one(Input)
            self.assertEqual(prompt_input.value, "Uncategorized")
            prompt_input.value = "Groceries"
            await pilot.press("enter")
            await pilot.pause()

            tx = db.get_transaction(row_key, self.path)
            self.assertEqual((tx.category, tx.category_source), ("Groceries", "manual"))
            self.assertIn("Groceries", [str(c) for c in table.get_row(row_key)])

            await pilot.press("pageup")
            self.assertEqual((table.row_count, screen.page), (50, 1))

        # Manual means rules leave it alone.
        add_rule("shop", "Shopping", db_path=self.path)
        self.assertEqual(db.get_transaction(row_key, self.path).category, "Groceries")

    async def test_prompt_escape_cancels(self):
        self.seed(_rows(2051, 1, 1))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("t", "c")
            app.screen.query_one(Input).value = "Nope"
            await pilot.press("escape")
            self.assertIsInstance(app.screen, TransactionsScreen)
        [tx] = db.get_transactions(self.path)
        self.assertEqual((tx.category, tx.category_source), ("Uncategorized", "default"))

    async def test_month_navigation_is_shared_between_screens(self):
        self.seed(_rows(2051, 1, 3) + _rows(2051, 2, 7, desc="FEB"))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("t")
            table = app.screen.query_one(DataTable)
            self.assertEqual(table.row_count, 3)
            await pilot.press("right")  # the table has focus; the screen must still win
            self.assertEqual((app.year, app.month, table.row_count), (2051, 2, 7))
            self.assertIn("February 2051", self.text(app.screen, "#txn-title"))

            await pilot.press("d")
            self.assertIsInstance(app.screen, DashboardScreen)
            self.assertIn("February 2051", self.text(app.screen, "#dash-title"))
            self.assertEqual(self.cards(app.screen)[3], "7")  # uncategorized

            await pilot.press("left", "left")
            self.assertEqual((app.year, app.month), (2050, 12))
            self.assertIn("December 2050", self.text(app.screen, "#dash-title"))

    async def test_search_and_typing_letters_in_the_prompt(self):
        self.seed(_rows(2051, 1, 3, desc="SWIGGY") + _rows(2051, 1, 4, desc="RENT"))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("t", "slash")
            self.assertIsInstance(app.screen, PromptScreen)
            # "d", "t" and "q" are app bindings but must reach the input.
            await pilot.press("d", "t", "q")
            self.assertIsInstance(app.screen, PromptScreen)
            self.assertEqual(app.screen.query_one(Input).value, "dtq")

            app.screen.query_one(Input).value = "swiggy"
            await pilot.press("enter")
            screen = app.screen
            self.assertEqual(screen.query_one(DataTable).row_count, 3)
            self.assertIn('search "swiggy"', self.text(screen, "#txn-title"))

            await pilot.press("escape")
            self.assertEqual(screen.query_one(DataTable).row_count, 7)

    async def test_toggle_transfer_excludes_from_dashboard(self):
        self.seed(_rows(2051, 1, 1, desc="CARD PAYMENT", amount=-500.0) + _rows(2051, 1, 1, desc="FOOD", amount=-20.0))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("t")
            table = app.screen.query_one(DataTable)
            card_row = next(i for i in range(table.row_count) if "CARD" in str(table.get_row_at(i)[1]))
            table.move_cursor(row=card_row)
            await pilot.press("x")
            self.assertIn("transfer", str(table.get_row_at(card_row)[3]))

            await pilot.press("d")
            self.assertEqual(self.cards(app.screen)[1], "₹20")

            await pilot.press("t", "x")  # toggle back
        self.assertEqual(get_monthly_summary(2051, 1, self.path).total_expense, 520.0)

    async def test_account_filter_cycles(self):
        card = db.get_or_create_account("Amex", "credit_card", db_path=self.path)
        self.seed(_rows(2051, 1, 2))
        self.seed(_rows(2051, 1, 5, desc="AMEX"), account=card)
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("t")
            table = app.screen.query_one(DataTable)
            seen = []
            for _ in range(3):
                seen.append((app.account_label(), table.row_count))
                await pilot.press("a")
            # all → Amex → Default (by name) → back to all
            self.assertEqual(seen, [("All accounts", 7), ("Amex", 5), ("Default", 2)])
            self.assertEqual(app.account_label(), "All accounts")

            await pilot.press("a", "d")  # filter carries over to the dashboard
            self.assertIn("Amex", self.text(app.screen, "#dash-title"))
            self.assertEqual(self.cards(app.screen)[3], "5")

    async def test_refresh_picks_up_external_changes(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("t")
            table = app.screen.query_one(DataTable)
            self.assertEqual(table.row_count, 0)
            await pilot.press("c", "x")  # nothing selected: a notice, not a crash
            self.assertIsInstance(app.screen, TransactionsScreen)

            self.seed(_rows(2051, 1, 4))  # e.g. committed from the web UI
            await pilot.press("r")
            self.assertEqual(table.row_count, 4)
            await pilot.press("d")
            self.assertEqual(self.cards(app.screen)[3], "4")


if __name__ == "__main__":
    unittest.main()
