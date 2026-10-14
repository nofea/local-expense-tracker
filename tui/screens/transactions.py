"""Transactions screen for the TUI (app_spec.md §7.2)."""

from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label

from core.database import (
    get_transaction,
    get_transaction_count,
    get_transactions,
    set_manual_category,
)
from core.models import TransactionRecord, format_money
from tui.screens.prompt import PromptScreen

PER_PAGE = 50


class TransactionsScreen(Screen):
    """Scrollable, paginated transaction log with category and transfer edits.

    Edits made here are manual (§6): rules will not change those rows again.
    """

    # ←/→ and PgUp/PgDn are also DataTable bindings; priority makes the
    # screen's month and page navigation win while the table has focus.
    # They are listed in the screen's own help line (#txn-footer) rather
    # than the Footer, which would otherwise overflow and hide d/t/q.
    BINDINGS = [
        Binding("left", "prev_month", "Prev month", priority=True, show=False),
        Binding("right", "next_month", "Next month", priority=True, show=False),
        Binding("pageup", "prev_page", "Prev page", priority=True, show=False),
        Binding("pagedown", "next_page", "Next page", priority=True, show=False),
        Binding("slash", "search", "Search", key_display="/"),
        Binding("escape", "clear_search", "Clear search", show=False),
        Binding("a", "cycle_account", "Account"),
        Binding("c", "set_category", "Category"),
        Binding("x", "toggle_transfer", "Transfer"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.page = 1
        self.total_pages = 1
        self.search: str = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Label("", id="txn-title", classes="screen-title"),
            DataTable(id="txn-table"),
            Label("", id="txn-footer", classes="footer-text"),
            id="transactions-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#txn-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Date", "Description", "Category", "Flags", "Amount", "Source")
        self._load_data()

    def on_screen_resume(self) -> None:
        self._load_data()

    # --- data ---------------------------------------------------------------

    def _filters(self) -> dict:
        app = self.app
        return dict(
            year=app.year,
            month=app.month,
            search=self.search or None,
            account_id=app.account_id,
        )

    def _load_data(self) -> None:
        app = self.app
        table = self.query_one("#txn-table", DataTable)
        cursor_row = table.cursor_row

        total = get_transaction_count(app.db_path, **self._filters())
        self.total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        self.page = min(self.page, self.total_pages)
        txns = get_transactions(
            app.db_path, **self._filters(), limit=PER_PAGE, offset=(self.page - 1) * PER_PAGE
        )

        table.clear()
        for tx in txns:
            desc = tx.cleaned_merchant or tx.raw_description
            if len(desc) > 40:
                desc = desc[:37] + "…"
            flags = " ".join(
                f for f, on in (("manual", tx.category_source == "manual"), ("transfer", tx.is_transfer)) if on
            )
            table.add_row(
                tx.transaction_date,
                desc,
                tx.category,
                Text(flags, style="dim"),
                Text(
                    format_money(tx.amount_minor, minor=True, signed=True),
                    style="green" if tx.amount_minor >= 0 else "red",
                    justify="right",
                ),
                tx.source_file or "",
                key=tx.id,
            )
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1))

        title = f" 📋 Transactions — {app.period_label} · {app.account_label()}"
        if self.search:
            title += f' · search "{self.search}"'
        self.query_one("#txn-title", Label).update(title)
        self.query_one("#txn-footer", Label).update(
            f"  Page {self.page}/{self.total_pages} · {total} total · "
            "c category · x transfer · / search · PgUp/PgDn pages · ←/→ months"
        )

    def _selected(self) -> Optional[TransactionRecord]:
        table = self.query_one("#txn-table", DataTable)
        if table.row_count == 0:
            self.notify("No transaction selected", severity="warning")
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return get_transaction(row_key.value, self.app.db_path)

    # --- navigation ---------------------------------------------------------

    def _go_to_month(self, delta: int) -> None:
        self.app.shift_month(delta)
        self.page = 1
        self.query_one("#txn-table", DataTable).move_cursor(row=0)
        self._load_data()

    def action_prev_month(self) -> None:
        self._go_to_month(-1)

    def action_next_month(self) -> None:
        self._go_to_month(1)

    def action_prev_page(self) -> None:
        if self.page > 1:
            self.page -= 1
            self._load_data()

    def action_next_page(self) -> None:
        if self.page < self.total_pages:
            self.page += 1
            self._load_data()

    def action_cycle_account(self) -> None:
        self.notify(f"Showing {self.app.cycle_account()}")
        self.page = 1
        self._load_data()

    def action_refresh(self) -> None:
        self._load_data()

    # --- search -------------------------------------------------------------

    def action_search(self) -> None:
        def apply(value: Optional[str]) -> None:
            if value is None:
                return
            self.search = value.strip()
            self.page = 1
            self._load_data()

        self.app.push_screen(
            PromptScreen("Search descriptions (empty to clear)", self.search, "e.g. swiggy"), apply
        )

    def action_clear_search(self) -> None:
        if self.search:
            self.search = ""
            self.page = 1
            self._load_data()

    # --- edits (manual, §6) ---------------------------------------------------

    def action_set_category(self) -> None:
        tx = self._selected()
        if tx is None:
            return

        def apply(value: Optional[str]) -> None:
            if value is None:
                return
            set_manual_category(
                tx.id,
                category=value,
                cleaned_merchant=tx.cleaned_merchant,
                is_transfer=tx.is_transfer,
                db_path=self.app.db_path,
            )
            self.notify(f"Category set to {value.strip() or 'Uncategorized'} (rules won't change it)")
            self._load_data()

        self.app.push_screen(
            PromptScreen(f"Category for {tx.cleaned_merchant or tx.raw_description}", tx.category), apply
        )

    def action_toggle_transfer(self) -> None:
        tx = self._selected()
        if tx is None:
            return
        set_manual_category(
            tx.id,
            category=tx.category,
            cleaned_merchant=tx.cleaned_merchant,
            is_transfer=not tx.is_transfer,
            db_path=self.app.db_path,
        )
        self.notify(
            "Marked as transfer (excluded from totals)" if not tx.is_transfer else "No longer a transfer"
        )
        self._load_data()
