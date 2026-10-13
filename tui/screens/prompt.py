"""A small modal text prompt, used for search and category entry."""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label


class PromptScreen(ModalScreen[Optional[str]]):
    """Ask for one line of text. Dismisses with the text on Enter, or with
    ``None`` on Escape."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, value: str = "", placeholder: str = "") -> None:
        super().__init__()
        self._title = title
        self._value = value
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt"):
            yield Label(self._title, id="prompt-title")
            yield Input(value=self._value, placeholder=self._placeholder, id="prompt-input")
            yield Label("Enter to confirm · Esc to cancel", classes="muted-text")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)
