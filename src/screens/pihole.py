"""Pi-hole screen — blocking status across all instances + DNS records."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config


class PiholeScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
    ]

    DEFAULT_CSS = """
    PiholeScreen { layout: vertical; }
    #status-bar   { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #top-section  { height: auto; max-height: 12; margin: 1 1 0 1; border: round $accent; padding: 0 1; }
    #instances-table { height: auto; }
    #dns-section  { height: 1fr; margin: 1; border: round $accent; padding: 0 1; }
    .section-label { text-style: bold; color: $accent; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading…", id="status-bar", markup=False)
        with Vertical(id="top-section"):
            yield Static("Instances", classes="section-label")
            yield DataTable(id="instances-table", show_cursor=False)
        with Vertical(id="dns-section"):
            yield Static("Custom DNS Records (primary)", classes="section-label")
            yield DataTable(id="dns-table", show_cursor=True)
        yield Footer()

    def on_mount(self) -> None:
        inst_t = self.query_one("#instances-table", DataTable)
        inst_t.add_columns("Instance", "Online", "Blocking", "Queries Today", "Blocked", "% Blocked")

        dns_t = self.query_one("#dns-table", DataTable)
        dns_t.add_columns("Domain", "IP")

        self.call_after_refresh(self._load)

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, "⏳ Fetching Pi-hole data…"
        )
        try:
            from lib.pihole import fetch_all_status, list_custom_dns

            instances = self.config.pihole_instances
            statuses = asyncio.run(fetch_all_status(instances))

            inst_t = self.query_one("#instances-table", DataTable)
            self.app.call_from_thread(inst_t.clear)

            for s in statuses:
                online = "🟢 Online" if s.online else "🔴 Offline"
                blocking = ("✅ Active" if s.blocking else "⏸ Paused") if s.online else "—"
                queries = f"{s.queries_today:,}" if s.online else "—"
                blocked = f"{s.blocked_today:,}" if s.online else "—"
                pct = f"{s.blocked_pct:.1f}%" if s.online else "—"
                self.app.call_from_thread(
                    inst_t.add_row, s.name, online, blocking, queries, blocked, pct
                )

            primary = next((i for i in instances if i.primary), instances[0] if instances else None)
            if primary:
                try:
                    records = asyncio.run(list_custom_dns(primary))
                    dns_t = self.query_one("#dns-table", DataTable)
                    self.app.call_from_thread(dns_t.clear)
                    for rec in records:
                        self.app.call_from_thread(
                            dns_t.add_row, rec.get("domain", ""), rec.get("ip", "")
                        )
                    dns_count = len(records)
                except Exception as e:
                    dns_count = 0
                    dns_t = self.query_one("#dns-table", DataTable)
                    self.app.call_from_thread(dns_t.clear)
                    self.app.call_from_thread(dns_t.add_row, f"Error: {e}", "")
            else:
                dns_count = 0

            online_count = sum(1 for s in statuses if s.online)
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"🟢 {online_count}/{len(statuses)} instances online  |  {dns_count} DNS records  |  Esc to go back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
