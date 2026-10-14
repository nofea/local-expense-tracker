"""Expense Tracker Terminal UI — main application (app_spec.md §7.2).

The app owns the shared view state, meaning the selected month and the
account filter, so the dashboard and the transaction log always show the
same period.  Screens read it on every refresh.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from textual.app import App
from textual.binding import Binding

from core.database import list_accounts
from tui.screens.dashboard import DashboardScreen
from tui.screens.transactions import TransactionsScreen


class ExpenseTrackerTUI(App):
    """Keyboard-driven expense tracker terminal interface."""

    CSS_PATH = "style.tcss"
    TITLE = "Expense Tracker"
    SUB_TITLE = "Privacy-First · Local-Only"

    # Not priority bindings: a focused text input (search, category prompt)
    # must receive these letters as text.
    BINDINGS = [
        Binding("d", "show('dashboard')", "Dashboard"),
        Binding("t", "show('transactions')", "Transactions"),
        Binding("q", "quit", "Quit"),
    ]

    SCREENS = {
        "dashboard": DashboardScreen,
        "transactions": TransactionsScreen,
    }

    def __init__(
        self,
        *,
        year: Optional[int] = None,
        month: Optional[int] = None,
        db_path: Optional[str | Path] = None,
    ) -> None:
        super().__init__()
        today = datetime.today()
        self.year = year or today.year
        self.month = month or today.month
        self.account_id: Optional[int] = None  # None = all accounts
        self.db_path = db_path

    def on_mount(self) -> None:
        self.push_screen("dashboard")

    def action_show(self, screen_name: str) -> None:
        """Switch to the named screen (a no-op if it is already showing)."""
        if self.screen is not self.get_screen(screen_name):
            self.switch_screen(screen_name)

    # --- shared view state -------------------------------------------------

    @property
    def period_label(self) -> str:
        return datetime(self.year, self.month, 1).strftime("%B %Y")

    def shift_month(self, delta: int) -> None:
        y, m = divmod(self.year * 12 + (self.month - 1) + delta, 12)
        self.year, self.month = y, m + 1

    def account_label(self) -> str:
        if self.account_id is None:
            return "All accounts"
        for account in list_accounts(self.db_path):
            if account["id"] == self.account_id:
                return account["name"]
        return "All accounts"

    def cycle_account(self) -> str:
        """Advance the account filter: all → each account by name → all."""
        ids: list[Optional[int]] = [None] + [a["id"] for a in list_accounts(self.db_path)]
        position = ids.index(self.account_id) if self.account_id in ids else 0
        self.account_id = ids[(position + 1) % len(ids)]
        return self.account_label()


def main():
    """Entry point for the TUI."""
    from core.database import init_db
    init_db()
    app = ExpenseTrackerTUI()
    app.run()


if __name__ == "__main__":
    main()
