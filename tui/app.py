"""Expense Tracker Terminal UI — main application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from tui.screens.dashboard import DashboardScreen
from tui.screens.transactions import TransactionsScreen


class ExpenseTrackerTUI(App):
    """Keyboard-driven expense tracker terminal interface."""

    CSS_PATH = "style.tcss"
    TITLE = "Expense Tracker"
    SUB_TITLE = "Privacy-First · Local-Only"

    BINDINGS = [
        Binding("d", "switch_screen('dashboard')", "Dashboard", priority=True),
        Binding("t", "switch_screen('transactions')", "Transactions", priority=True),
        Binding("q", "quit", "Quit", priority=True),
    ]

    SCREENS = {
        "dashboard": DashboardScreen,
        "transactions": TransactionsScreen,
    }

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()

    def on_mount(self) -> None:
        self.push_screen("dashboard")

    def action_switch_screen(self, screen_name: str) -> None:
        """Switch to the named screen, popping back first if needed."""
        # Pop all pushed screens back to base
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(screen_name)


def main():
    """Entry point for the TUI."""
    from core.database import init_db
    init_db()
    app = ExpenseTrackerTUI()
    app.run()


if __name__ == "__main__":
    main()
