"""UniFi Network screen — connected clients list."""
from __future__ import annotations

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


class UnifiScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("R", "refresh_data", "Refresh", priority=True),
    ]

    DEFAULT_CSS = """
    UnifiScreen { layout: vertical; }
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
            yield DataTable(id="clients-table", show_cursor=False)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#clients-table", DataTable)
        t.add_columns("IP", "Name", "MAC", "Network", "Link")
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, "⏳ Fetching UniFi clients…"
        )
        try:
            from lib.unifi import fetch_clients

            unifi_cfg = self.config.unifi
            if not unifi_cfg:
                self.app.call_from_thread(
                    self.query_one("#status-bar", Static).update,
                    "❌ UniFi not configured (see config.yaml)",
                )
                return

            clients = fetch_clients(unifi_cfg, site=unifi_cfg.site)
            clients.sort(key=lambda c: (not c.is_wired, c.display_name.lower()))

            t = self.query_one("#clients-table", DataTable)
            self.app.call_from_thread(t.clear)
            wired = 0
            for c in clients:
                link = "🔌 wired" if c.is_wired else "📶 wifi"
                if c.is_wired:
                    wired += 1
                self.app.call_from_thread(
                    t.add_row, c.ip or "—", c.display_name, c.mac, c.network or "—", link
                )

            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"{len(clients)} connected clients  ({wired} wired, {len(clients) - wired} wifi)"
                f"  |  [R] Refresh  |  Esc to go back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
