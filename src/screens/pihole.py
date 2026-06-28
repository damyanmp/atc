"""Pi-hole screen — blocking status across all instances + DNS records.

Press 'd' to open the TLS cert deploy screen.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Log, Static, SelectionList
from textual.widgets.selection_list import Selection

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config


class PiholeScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("d", "deploy_tls", "Deploy TLS", priority=True),
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
        yield Static("Loading…", id="status-bar")
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
                f"🟢 {online_count}/{len(statuses)} instances online  |  {dns_count} DNS records  |  [d] Deploy TLS  |  Esc to go back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )

    def action_deploy_tls(self) -> None:
        self.app.push_screen(TLSDeployScreen(self.config))


class TLSDeployScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("enter", "deploy", "Deploy", priority=True),
        ("ctrl+a", "select_all", "Select All"),
        ("ctrl+n", "deselect_all", "Deselect All"),
    ]

    DEFAULT_CSS = """
    TLSDeployScreen { layout: horizontal; }
    #left-panel {
        width: 30;
        layout: vertical;
        border-right: solid $accent;
        padding: 1;
    }
    #right-panel {
        width: 1fr;
        layout: vertical;
        padding: 1;
    }
    #target-list { height: 1fr; border: round $accent; }
    #output-log  { height: 1fr; border: round $accent; }
    #summary     { height: 3; padding: 1; }
    .section-label { text-style: bold; color: $accent; margin-bottom: 1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._phase = "idle"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-panel"):
                yield Static("Deploy Targets", classes="section-label")
                yield SelectionList(
                    Selection("Globemaster", "Globemaster"),
                    Selection("Viggen",      "Viggen"),
                    Selection("Draken",      "Draken"),
                    id="target-list",
                )
            with Vertical(id="right-panel"):
                yield Static("Output", classes="section-label")
                yield Log(id="output-log", highlight=True)
                yield Static(
                    "Select targets and press [bold]Enter[/bold] to deploy TLS cert  |  Esc to go back",
                    id="summary",
                    markup=True,
                )
        yield Footer()

    def action_select_all(self) -> None:
        self.query_one("#target-list", SelectionList).select_all()

    def action_deselect_all(self) -> None:
        self.query_one("#target-list", SelectionList).deselect_all()

    def action_deploy(self) -> None:
        if self._phase == "idle":
            self._run_deploy()

    @work(exclusive=True)
    async def _run_deploy(self) -> None:
        from lib.tls_deploy import ALL_TARGETS, fetch_combined_pem, deploy_target

        sel = self.query_one("#target-list", SelectionList)
        selected_names = {str(v) for v in sel.selected}
        targets = [t for t in ALL_TARGETS if t.name in selected_names]

        if not targets:
            self.query_one("#summary", Static).update("⚠️  No targets selected.")
            return

        self._phase = "deploying"
        log = self.query_one("#output-log", Log)
        log.clear()
        self.query_one("#summary", Static).update("⏳  Fetching cert from Docker-01…")

        def on_line(line: str) -> None:
            log.write_line(line)

        try:
            pem = await fetch_combined_pem(on_line)
        except Exception as e:
            log.write_line(f"❌  Failed to fetch cert: {e}")
            self.query_one("#summary", Static).update("❌  Cert fetch failed — see log above")
            self._phase = "idle"
            return

        results: list[tuple[str, bool]] = []
        for target in targets:
            log.write_line(f"\n{'─' * 50}")
            log.write_line(f"🔄  Deploying to {target.name}…")
            log.write_line(f"{'─' * 50}")

            ok = await deploy_target(target, pem, on_line)
            results.append((target.name, ok))
            log.write_line(f"{'✅' if ok else '❌'}  {target.name} done")

        success = sum(1 for _, ok in results if ok)
        fail = len(results) - success
        summary = f"✅ {success}/{len(results)} succeeded"
        if fail:
            summary += f"  ❌ {fail} failed — see log above"
        self.query_one("#summary", Static).update(summary)
        self._phase = "done"
