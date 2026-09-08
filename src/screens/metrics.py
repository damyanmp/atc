"""Prometheus metrics screen — ad-hoc PromQL instant queries."""
from __future__ import annotations

import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config


class MetricsScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("/", "focus_query", "Query", priority=True),
    ]

    DEFAULT_CSS = """
    MetricsScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #query-input { margin: 1 1 0 1; }
    #table-container { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Enter a PromQL expression below and press Enter", id="status-bar")
        yield Input(placeholder="e.g. up, node_load1, rate(node_cpu_seconds_total[5m])", id="query-input")
        with Vertical(id="table-container"):
            yield DataTable(id="results-table", show_cursor=False)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#results-table", DataTable)
        t.add_columns("Labels", "Value")
        if not self.config.prometheus:
            self.query_one("#status-bar", Static).update(
                "❌ Prometheus not configured (see config.yaml)"
            )
        self.set_timer(0.05, lambda: self.query_one("#query-input", Input).focus())

    def action_focus_query(self) -> None:
        self.query_one("#query-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "query-input":
            return
        promql = event.value.strip()
        if promql and self.config.prometheus:
            self._run_query(promql)

    @work(exclusive=True, thread=True)
    def _run_query(self, promql: str) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, f"⏳ Querying: {promql}"
        )
        try:
            from lib.prometheus import query

            results = query(self.config.prometheus, promql)

            t = self.query_one("#results-table", DataTable)
            self.app.call_from_thread(t.clear)

            if not results:
                self.app.call_from_thread(
                    self.query_one("#status-bar", Static).update,
                    f"(no results)  |  {promql}  |  Esc to go back",
                )
                return

            for r in results:
                labels = ", ".join(f"{k}={v}" for k, v in r.metric.items() if k != "__name__")
                name = r.metric.get("__name__", "")
                label_str = f"{name}{{{labels}}}" if labels else (name or "(scalar)")
                self.app.call_from_thread(t.add_row, label_str, f"{r.value:g}")

            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"{len(results)} result(s)  |  {promql}  |  [/] Query  |  Esc to go back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
