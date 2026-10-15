"""Quick sites screen — read-only view of what's published to quick.local.

Publishing/bootstrapping a new site needs a local filesystem path and stays
CLI-only (`atc quick publish <dir>`) — this screen just shows current state.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config


class QuickScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("R", "refresh_data", "Refresh", priority=True),
    ]

    DEFAULT_CSS = """
    QuickScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #table-container { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading…", id="status-bar", markup=False)
        with Vertical(id="table-container"):
            yield DataTable(id="sites-table", show_cursor=False)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#sites-table", DataTable)
        t.add_columns("Slug", "URL")
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        domain = self.config.quick.domain
        if not domain:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                "❌ 'quick' is not configured — set quick.domain in config.yaml",
            )
            return
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, f"⏳ Fetching https://{domain}/directory…"
        )
        try:
            import httpx

            with httpx.Client(verify=False, timeout=10) as client:
                r = client.get(f"https://{domain}/directory")
            r.raise_for_status()

            names = re.findall(r'value="([^"]+)"', r.text)

            t = self.query_one("#sites-table", DataTable)
            self.app.call_from_thread(t.clear)
            for name in names:
                self.app.call_from_thread(t.add_row, name, f"https://{domain}/{name}/")

            summary = (
                "(nothing published)" if not names
                else f"{len(names)} site(s) published"
            )
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"{summary}  |  publish via 'atc quick publish <dir>'  |  [R] Refresh  |  Esc to go back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
