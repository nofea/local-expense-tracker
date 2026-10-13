"""Dashboard screen for the TUI."""

from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

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
    """Monthly summary dashboard."""

    def compose(self) -> ComposeResult:
        today = datetime.today()
        self._year = today.year
        self._month = today.month

        yield Vertical(
            Label(f" 📊 Dashboard — {today.strftime('%B %Y')}", id="dash-title", classes="screen-title"),
            Horizontal(
                StatCard("INCOME", format_money(0, decimals=0), "income-card"),
                StatCard("EXPENSES", format_money(0, decimals=0), "expense-card"),
                StatCard("SAVINGS", format_money(0, decimals=0), "savings-card"),
                StatCard("UNCATEGORIZED", "0", "warning-card"),
                id="stats-row",
            ),
            Label(" Spending by Category", classes="section-title"),
            Vertical(id="category-bars"),
            id="dashboard-container",
        )

    def on_mount(self) -> None:
        self._refresh_data()

    def on_screen_resume(self) -> None:
        self._refresh_data()

    def _refresh_data(self) -> None:
        summary = get_monthly_summary(self._year, self._month)
        breakdown = get_category_breakdown(self._year, self._month)
        uncategorized = get_uncategorized_count()

        # Update stat cards
        cards = self.query(StatCard)
        card_list = list(cards)
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
