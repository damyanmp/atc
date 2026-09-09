"""Uptime Kuma screen — monitor status overview."""
from __future__ import annotations

import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config


def _status_icon(code: int) -> str:
    return {0: "🔴", 1: "🟢", 2: "🟡", 3: "🔵"}.get(code, "⚪")


def _status_label(code: int) -> str:
    return {0: "DOWN", 1: "UP", 2: "PENDING", 3: "MAINTENANCE"}.get(code, "UNKNOWN")


def _uptime_bar(pct: float, width: int = 10) -> str:
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)


class UptimeKumaScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    DEFAULT_CSS = """
    UptimeKumaScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #table-container { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading…", id="status-bar", markup=False)
        yield DataTable(id="monitors-table", show_cursor=False)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#monitors-table", DataTable)
        t.add_columns("", "Monitor", "Group", "Latency", "24h", "30d", "Bar", "Status")
        self.call_after_refresh(self._load)

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, "⏳ Fetching monitors…"
        )
        try:
            from lib.uptimekuma import fetch_monitors
            monitors = fetch_monitors(self.config.uptimekuma)

            t = self.query_one("#monitors-table", DataTable)
            self.app.call_from_thread(t.clear)

            for m in monitors:
                lat = f"{m.latency_ms}ms" if m.latency_ms else "—"
                u24 = f"{m.uptime_24h * 100:.1f}%"
                u30 = f"{m.uptime_720h * 100:.1f}%"
                bar = _uptime_bar(m.uptime_720h)
                self.app.call_from_thread(
                    t.add_row,
                    _status_icon(m.status),
                    m.name,
                    m.group or "—",
                    lat,
                    u24,
                    u30,
                    bar,
                    _status_label(m.status),
                )

            down = sum(1 for m in monitors if m.status == 0)
            total = len(monitors)
            summary = f"🟢 {total - down} up  🔴 {down} down  |  {total} total  |  Esc to go back"
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, summary
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
