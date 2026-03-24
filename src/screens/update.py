"""Update screen — select targets and stream apt-get upgrade output."""
from __future__ import annotations

import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Label,
    Log, Static, SelectionList
)
from textual.widgets.selection_list import Selection

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config
from lib.proxmox import ClusterStatus, fetch_cluster_status
from lib.ssh import UpdateTarget, run_update


class UpdateScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("ctrl+a", "select_all", "Select All"),
        ("ctrl+n", "deselect_all", "Deselect All"),
    ]

    DEFAULT_CSS = """
    UpdateScreen {
        layout: horizontal;
    }
    #left-panel {
        width: 40;
        layout: vertical;
        border-right: solid $accent;
        padding: 1;
    }
    #right-panel {
        width: 1fr;
        layout: vertical;
        padding: 1;
    }
    #target-list {
        height: 1fr;
        border: round $accent;
    }
    #controls {
        height: auto;
        layout: horizontal;
        margin-top: 1;
    }
    #controls Button {
        margin-right: 1;
    }
    #output-log {
        height: 1fr;
        border: round $accent;
    }
    #summary {
        height: 3;
        padding: 1;
    }
    .section-label {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    """

    def __init__(self, config: Config, cluster: ClusterStatus | None = None) -> None:
        super().__init__()
        self.config = config
        self._cluster = cluster
        self._running = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-panel"):
                yield Static("Select Update Targets", classes="section-label")
                yield SelectionList(id="target-list")
                with Horizontal(id="controls"):
                    yield Button("▶  Run Updates", id="run-btn", variant="success")
                    yield Button("✕ Back", id="back-btn", variant="default")
            with Vertical(id="right-panel"):
                yield Static("Output", classes="section-label")
                yield Log(id="output-log", highlight=True)
                yield Static("", id="summary")
        yield Footer()

    def on_mount(self) -> None:
        self._populate_targets()

    def _populate_targets(self) -> None:
        sel = self.query_one("#target-list", SelectionList)
        sel.clear_options()

        if not self._cluster:
            sel.add_option(Selection("⏳ Loading cluster data…", "loading", disabled=True))
            self._load_cluster()
            return

        # Add LXC section
        lxcs = [g for g in self._cluster.guests if g.type == "lxc" and g.status == "running"]
        if lxcs:
            sel.add_option(Selection("── LXC Containers ──", "lxc-header", disabled=True))
            for ct in sorted(lxcs, key=lambda g: g.name):
                sel.add_option(Selection(f"  📦 {ct.name}  [{ct.node}]", f"lxc:{ct.name}:{ct.node}"))

        # Add VM section
        vms = [g for g in self._cluster.guests if g.type == "qemu" and g.status == "running"]
        if vms:
            sel.add_option(Selection("── Virtual Machines ──", "vm-header", disabled=True))
            for vm in sorted(vms, key=lambda g: g.name):
                sel.add_option(Selection(f"  🖥  {vm.name}  [{vm.node}]", f"vm:{vm.name}:{vm.node}"))

    @work
    async def _load_cluster(self) -> None:
        self._cluster = await fetch_cluster_status(self.config.proxmox)
        self._populate_targets()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-btn":
            self._start_updates()
        elif event.button.id == "back-btn":
            self.app.pop_screen()

    def action_select_all(self) -> None:
        sel = self.query_one("#target-list", SelectionList)
        sel.select_all()

    def action_deselect_all(self) -> None:
        sel = self.query_one("#target-list", SelectionList)
        sel.deselect_all()

    def _get_selected_targets(self) -> list[UpdateTarget]:
        sel = self.query_one("#target-list", SelectionList)
        targets: list[UpdateTarget] = []
        for value in sel.selected:
            parts = str(value).split(":")
            if len(parts) == 3:
                _, name, node = parts
                # Look up IP from cluster — guests only have name/node from Proxmox API
                # The user must configure SSH access; we use name as hostname fallback
                host = self._resolve_host(name)
                targets.append(UpdateTarget(
                    name=name,
                    host=host,
                    user=self.config.ssh.default_user,
                ))
        return targets

    def _resolve_host(self, name: str) -> str:
        """Try to resolve a guest name to an IP. Falls back to name (relies on DNS/hosts)."""
        # In most homelabs, the hostname is resolvable via Pi-hole/DNS
        # Future: could look up IP from Proxmox guest agent
        return name

    @work(exclusive=True)
    async def _start_updates(self) -> None:
        if self._running:
            return
        targets = self._get_selected_targets()
        if not targets:
            self.query_one("#summary", Static).update("⚠️  No targets selected.")
            return

        self._running = True
        log = self.query_one("#output-log", Log)
        log.clear()
        self.query_one("#summary", Static).update(f"⏳ Updating {len(targets)} target(s)…")
        self.query_one("#run-btn", Button).disabled = True

        results = []
        for target in targets:
            log.write_line(f"\n{'─'*50}")
            log.write_line(f"🔄  Updating {target.name} ({target.host})…")
            log.write_line(f"{'─'*50}")

            def on_line(line: str, t=target) -> None:
                # Strip ANSI for cleaner display
                self.app.call_from_thread(log.write_line, f"  {line}")

            result = await run_update(target, self.config.ssh, on_line=on_line)
            results.append(result)
            icon = "✅" if result.success else "❌"
            log.write_line(f"{icon}  {target.name} done")

        ok = sum(1 for r in results if r.success)
        fail = len(results) - ok
        summary = f"✅ {ok} succeeded"
        if fail:
            summary += f"  ❌ {fail} failed"
        self.query_one("#summary", Static).update(summary)
        self.query_one("#run-btn", Button).disabled = False
        self._running = False
