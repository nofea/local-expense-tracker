"""Dashboard screen for the TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, Static

from core.models import format_money
from core.analytics import (
    get_category_breakdown,
    get_monthly_summary,
    get_uncategorized_count,
)


class StatCard(Static):
    """A styled statistics card."""

    def __init__(self, title: str, value: str, style_class: str = "") -> None:
        super().__init__()
        self._title = title
        self._value = value
        if style_class:
            self.add_class(style_class)

    def compose(self) -> ComposeResult:
        yield Label(self._title, classes="stat-title")
        yield Label(self._value, classes="stat-value")

    def update_value(self, value: str) -> None:
        self._value = value
        try:
            self.query_one(".stat-value", Label).update(value)
        except Exception:
            pass


class CategoryBar(Static):
    """A text-based bar for a category."""

    def __init__(self, category: str, total: float, percentage: float, max_width: int = 30) -> None:
        super().__init__()
        self._category = category
        self._total = total
        self._percentage = percentage
        self._max_width = max_width

    def compose(self) -> ComposeResult:
        bar_len = max(1, int(self._percentage / 100 * self._max_width))
        bar = "█" * bar_len + "░" * (self._max_width - bar_len)
        yield Label(
            f"  {self._category:<20s} {bar} {format_money(self._total, decimals=0):>11}  ({self._percentage:.1f}%)"
        )


class DashboardScreen(Screen):
    """Monthly summary for the app's selected month and scope (§7.2)."""

    BINDINGS = [
        Binding("left", "prev_month", "Prev month"),
        Binding("right", "next_month", "Next month"),
        Binding("a", "cycle_account", "Account"),
        Binding("f", "cycle_family", "Family"),
        Binding("m", "cycle_member", "Member"),
        Binding("j", "toggle_joint", "Joint"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Label("", id="dash-title", classes="screen-title"),
            Horizontal(
                StatCard("INCOME", format_money(0, decimals=0), "income-card"),
                StatCard("EXPENSES", format_money(0, decimals=0), "expense-card"),
                StatCard("SAVINGS", format_money(0, decimals=0), "savings-card"),
                StatCard("UNCATEGORIZED", "0", "warning-card"),
                id="stats-row",
            ),
            Label(" Spending by Category", classes="section-title"),
            Vertical(id="category-bars"),
            Label(" Transfers are excluded from all figures.", classes="muted-text"),
            id="dashboard-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_data()

    def on_screen_resume(self) -> None:
        self._refresh_data()

    def _refresh_data(self) -> None:
        app = self.app
        filters = dict(db_path=app.db_path, **app.scope())
        summary = get_monthly_summary(app.year, app.month, **filters)
        breakdown = get_category_breakdown(app.year, app.month, **filters)
        uncategorized = get_uncategorized_count(app.year, app.month, **filters)

        self.query_one("#dash-title", Label).update(
            f" 📊 Dashboard — {app.period_label} · {app.scope_label()}"
        )

        # Update stat cards
        card_list = list(self.query(StatCard))
        if len(card_list) >= 4:
            card_list[0].update_value(format_money(summary.total_income, decimals=0))
            card_list[1].update_value(format_money(summary.total_expense, decimals=0))
            card_list[2].update_value(format_money(summary.net_savings, decimals=0))
            card_list[3].update_value(str(uncategorized))

        # Update category bars
        container = self.query_one("#category-bars", Vertical)
        container.remove_children()
        if breakdown:
            for cat in breakdown:
                container.mount(
                    CategoryBar(cat.category, cat.total, cat.percentage)
                )
        else:
            container.mount(
                Label("  No expense data for this month", classes="muted-text")
            )

    def action_prev_month(self) -> None:
        self.app.shift_month(-1)
        self._refresh_data()

    def action_next_month(self) -> None:
        self.app.shift_month(1)
        self._refresh_data()

    def action_cycle_account(self) -> None:
        self.notify(f"Showing {self.app.cycle_account()}")
        self._refresh_data()

    def action_cycle_family(self) -> None:
        self.notify(f"Showing {self.app.cycle_family()}")
        self._refresh_data()

    def action_cycle_member(self) -> None:
        self.notify(f"Showing {self.app.cycle_member()}")
        self._refresh_data()

    def action_toggle_joint(self) -> None:
        self.notify(self.app.toggle_joint())
        self._refresh_data()

    def action_refresh(self) -> None:
        self._refresh_data()
