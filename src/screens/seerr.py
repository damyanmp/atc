"""Seerr screen — media request queue."""
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

_REQ_STATUS_ICON = {"pending": "🟡", "approved": "🟢", "declined": "🔴", "failed": "💥"}
_MEDIA_STATUS_ICON = {
    "unknown": "⚪", "pending": "🟡", "processing": "🔵",
    "partially available": "🟤", "available": "🟢",
}


class SeerrScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    DEFAULT_CSS = """
    SeerrScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #requests-table { height: 1fr; margin: 1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading…", id="status-bar", markup=False)
        yield DataTable(id="requests-table", show_cursor=True)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#requests-table", DataTable)
        t.add_columns("", "Title", "Type", "Request", "Media", "Requested By", "Date")
        self.call_after_refresh(self._load)

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, "⏳ Fetching requests…"
        )
        try:
            from lib.seerr import list_requests
            requests = list_requests(self.config.seerr)

            t = self.query_one("#requests-table", DataTable)
            self.app.call_from_thread(t.clear)

            for r in requests:
                media_icon = _MEDIA_STATUS_ICON.get(r.media_status, "⚪")
                req_icon = _REQ_STATUS_ICON.get(r.request_status, "⚪")
                self.app.call_from_thread(
                    t.add_row,
                    media_icon,
                    r.title,
                    "🎬" if r.media_type == "movie" else "📺",
                    f"{req_icon} {r.request_status}",
                    r.media_status,
                    r.requested_by or "—",
                    r.created_at,
                )

            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"{len(requests)} request(s)  |  Esc to go back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
