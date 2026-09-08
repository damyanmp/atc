"""Update screen — dry-run first, then explicit confirm before apt upgrade."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Log, SelectionList, Static
from textual.widgets.selection_list import Selection

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config
from lib.proxmox import ClusterStatus, fetch_cluster_status, fetch_guest_ips
from lib.ssh import UpdateTarget, run_dry_run, run_update

_Phase = Literal["idle", "dry_running", "awaiting_confirm", "applying", "done"]


class UpdateScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("ctrl+a", "select_all",   "Select All"),
        ("ctrl+n", "deselect_all", "Deselect All"),
        Binding("enter", "run_updates", "Run / Confirm", priority=True),
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
        self._phase: _Phase = "idle"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-panel"):
                yield Static("Select Update Targets", classes="section-label")
                yield SelectionList(id="target-list")
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

        lxcs = [g for g in self._cluster.guests if g.type == "lxc" and g.status == "running"]
        if lxcs:
            sel.add_option(Selection("── LXC Containers ──", "lxc-header", disabled=True))
            for ct in sorted(lxcs, key=lambda g: g.name):
                sel.add_option(Selection(f"  📦 {ct.name}  [{ct.node}]", f"lxc:{ct.name}:{ct.node}"))

        vms = [g for g in self._cluster.guests if g.type == "qemu" and g.status == "running"]
        if vms:
            sel.add_option(Selection("── Virtual Machines ──", "vm-header", disabled=True))
            for vm in sorted(vms, key=lambda g: g.name):
                sel.add_option(Selection(f"  🖥  {vm.name}  [{vm.node}]", f"vm:{vm.name}:{vm.node}"))

    @work
    async def _load_cluster(self) -> None:
        self._cluster = await fetch_cluster_status(self.config.proxmox)
        self._populate_targets()

    def action_run_updates(self) -> None:
        if self._phase == "idle":
            self._start_dry_run()
        elif self._phase == "awaiting_confirm":
            self._apply_updates()
        # dry_running / applying / done: ignore keypress

    def action_select_all(self) -> None:
        self.query_one("#target-list", SelectionList).select_all()

    def action_deselect_all(self) -> None:
        self.query_one("#target-list", SelectionList).deselect_all()

    def _get_selected_targets(self, ip_map: dict[int, str] | None = None) -> list[UpdateTarget]:
        sel = self.query_one("#target-list", SelectionList)
        guests_by_name = {g.name: g for g in (self._cluster.guests if self._cluster else [])}
        targets: list[UpdateTarget] = []
        for value in sel.selected:
            parts = str(value).split(":")
            if len(parts) == 3:
                _, name, node = parts
                g = guests_by_name.get(name)
                host = (ip_map or {}).get(g.vmid) if g else None
                targets.append(UpdateTarget(
                    name=name,
                    host=host or name,
                    user=self.config.ssh.default_user,
                ))
        return targets

    async def _resolve_ips(self) -> dict[int, str]:
        """Fetch Proxmox guest IPs for all currently selected targets."""
        if not self._cluster:
            return {}
        sel = self.query_one("#target-list", SelectionList)
        selected_names = {str(v).split(":")[1] for v in sel.selected if str(v).count(":") == 2}
        guests = [g for g in self._cluster.guests if g.name in selected_names]
        return await fetch_guest_ips(self.config.proxmox, guests)

    # ── Phase 1: dry-run ──────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _start_dry_run(self) -> None:
        ip_map = await self._resolve_ips()
        targets = self._get_selected_targets(ip_map)
        if not targets:
            self.query_one("#summary", Static).update("⚠️  No targets selected.")
            return

        self._phase = "dry_running"
        log = self.query_one("#output-log", Log)
        log.clear()
        self.query_one("#summary", Static).update(
            f"🔍  Simulating upgrade on {len(targets)} target(s)… (no changes applied)"
        )

        any_upgrades = False
        for target in targets:
            log.write_line(f"\n{'─'*50}")
            log.write_line(f"🔍  Dry-run: {target.name} ({target.host})")
            log.write_line(f"{'─'*50}")

            pkg_lines: list[str] = []

            def on_line(line: str, t=target) -> None:
                # Highlight lines that show packages that would be upgraded
                if line.startswith("Inst ") or line.startswith("Remv "):
                    log.write_line(f"  [yellow]{line}[/yellow]")
                    pkg_lines.append(line)
                elif line.strip():
                    log.write_line(f"  {line}")

            result = await run_dry_run(target, self.config.ssh, on_line=on_line)

            if result.success and pkg_lines:
                any_upgrades = True
                log.write_line(f"  → {len(pkg_lines)} package(s) would be upgraded")
            elif result.success:
                log.write_line("  ✅  Already up to date")
            else:
                log.write_line("  ❌  Dry-run failed — check SSH access")

        self._phase = "awaiting_confirm"

        if any_upgrades:
            self.query_one("#summary", Static).update(
                "⚠️  Upgrades available above.  "
                "Press [bold]Enter[/bold] to apply  |  [bold]Escape[/bold] to cancel"
            )
        else:
            self.query_one("#summary", Static).update(
                "✅  All targets up to date — nothing to apply."
            )
            self._phase = "done"

    # ── Phase 2: apply ────────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _apply_updates(self) -> None:
        ip_map = await self._resolve_ips()
        targets = self._get_selected_targets(ip_map)
        if not targets:
            self._phase = "idle"
            return

        self._phase = "applying"
        log = self.query_one("#output-log", Log)
        log.clear()
        self.query_one("#summary", Static).update(
            f"⏳  Applying upgrade on {len(targets)} target(s)…"
        )

        results = []
        for target in targets:
            log.write_line(f"\n{'─'*50}")
            log.write_line(f"🔄  Upgrading {target.name} ({target.host})…")
            log.write_line(f"{'─'*50}")

            def on_line(line: str, t=target) -> None:
                log.write_line(f"  {line}")

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
        self._phase = "done"
