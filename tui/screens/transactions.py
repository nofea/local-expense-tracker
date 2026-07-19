"""Transactions screen for the TUI."""

from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Label

from core.database import get_transactions, get_transaction_count


class TransactionsScreen(Screen):
    """Scrollable, paginated transaction log."""

    BINDINGS = [
        Binding("left", "prev_month", "← Prev Month"),
        Binding("right", "next_month", "→ Next Month"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        today = datetime.today()
        self._year = today.year
        self._month = today.month
        self._page = 1
        self._per_page = 50

        yield Vertical(
            Label(self._title_text(), id="txn-title", classes="screen-title"),
            DataTable(id="txn-table"),
            Label("", id="txn-footer", classes="footer-text"),
            id="transactions-container",
        )

    def _title_text(self) -> str:
        dt = datetime(self._year, self._month, 1)
        return f" 📋 Transactions — {dt.strftime('%B %Y')}"

    def on_mount(self) -> None:
        table = self.query_one("#txn-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Date", "Description", "Category", "Amount", "Source")
        self._load_data()

    def on_screen_resume(self) -> None:
        self._load_data()

    def _load_data(self) -> None:
        table = self.query_one("#txn-table", DataTable)
        table.clear()

        txns = get_transactions(
            year=self._year, month=self._month,
            limit=self._per_page, offset=(self._page - 1) * self._per_page,
        )
        total = get_transaction_count(year=self._year, month=self._month)

        for tx in txns:
            amount_str = f"{'+'if tx.amount >= 0 else ''}₹{abs(tx.amount):,.2f}"
            desc = tx.cleaned_merchant or tx.raw_description
            if len(desc) > 40:
                desc = desc[:37] + "…"
            table.add_row(
                tx.transaction_date,
                desc,
                tx.category,
                amount_str,
                tx.source_file or "",
            )

        # Update title and footer
        try:
            self.query_one("#txn-title", Label).update(self._title_text())
            total_pages = max(1, (total + self._per_page - 1) // self._per_page)
            self.query_one("#txn-footer", Label).update(
                f"  Page {self._page}/{total_pages} · {total} total · ← → to navigate months · r to refresh"
            )
        except Exception:
            pass

    def action_prev_month(self) -> None:
        if self._month == 1:
            self._month = 12
            self._year -= 1
        else:
            self._month -= 1
        self._page = 1
        self._load_data()

    def action_next_month(self) -> None:
        if self._month == 12:
            self._month = 1
            self._year += 1
        else:
            self._month += 1
        self._page = 1
        self._load_data()

    def action_refresh(self) -> None:
        self._load_data()
