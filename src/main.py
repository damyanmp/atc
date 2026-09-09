"""atc — entry point. Routes to CLI (typer) or TUI (Textual)."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ and src/lib are importable regardless of working directory
_SRC = Path(__file__).parent
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "lib"))

from textual.app import App
from textual.binding import Binding

from config import Config
from screens.arr import ArrScreen
from screens.backup import BackupScreen
from screens.dashboard import DashboardScreen
from screens.docker import DockerScreen
from screens.metrics import MetricsScreen
from screens.pihole import PiholeScreen
from screens.plex import PlexScreen
from screens.quick import QuickScreen
from screens.scripts import ScriptsScreen
from screens.seerr import SeerrScreen
from screens.speedtest import SpeedtestScreen
from screens.unifi import UnifiScreen
from screens.update import UpdateScreen
from screens.uptimekuma import UptimeKumaScreen


class HomelabApp(App):
    """atc — Air Traffic Control TUI."""

    TITLE = "atc"
    SUB_TITLE = "Air Traffic Control"
    CSS = """
    Screen { background: $background; }
    Header { background: $primary; color: $text; }
    Footer { background: $primary-darken-1; }
    """

    # Priority bindings live on the App so they're always active regardless
    # of which widget has focus or which screen is on top.
    BINDINGS = [
        Binding("u", "nav_update",    "Update",    priority=True, show=True),
        Binding("r", "nav_scripts",   "Scripts",   priority=True, show=True),
        Binding("k", "nav_kuma",      "Kuma",      priority=True, show=True),
        Binding("s", "nav_seerr",     "Seerr",     priority=True, show=True),
        Binding("x", "nav_plex",      "Plex",      priority=True, show=True),
        Binding("p", "nav_pihole",    "Pi-hole",   priority=True, show=True),
        Binding("t", "nav_speedtest", "Speedtest", priority=True, show=True),
        Binding("a", "nav_arr",       "Pipeline",  priority=True, show=True),
        Binding("d", "nav_docker",    "Docker",    priority=True, show=True),
        Binding("n", "nav_unifi",     "UniFi",     priority=True, show=True),
        Binding("m", "nav_metrics",   "Metrics",   priority=True, show=True),
        Binding("b", "nav_backup",    "Backup",    priority=True, show=True),
        Binding("g", "nav_quick",     "Quick",     priority=True, show=True),
        Binding("q", "quit",          "Quit",      priority=True, show=True),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def on_mount(self) -> None:
        self.install_screen(DashboardScreen(self.config), name="dashboard")
        self.install_screen(UpdateScreen(self.config), name="update")
        self.install_screen(ScriptsScreen(self.config), name="scripts")
        self.install_screen(UptimeKumaScreen(self.config), name="uptimekuma")
        self.install_screen(SeerrScreen(self.config), name="seerr")
        self.install_screen(PlexScreen(self.config), name="plex")
        self.install_screen(PiholeScreen(self.config), name="pihole")
        self.install_screen(SpeedtestScreen(self.config), name="speedtest")
        self.install_screen(ArrScreen(self.config), name="arr")
        self.install_screen(DockerScreen(self.config), name="docker")
        self.install_screen(UnifiScreen(self.config), name="unifi")
        self.install_screen(MetricsScreen(self.config), name="metrics")
        self.install_screen(BackupScreen(self.config), name="backup")
        self.install_screen(QuickScreen(self.config), name="quick")
        self.push_screen("dashboard")

    # ── Navigation actions ────────────────────────────────────────────────────
    def action_nav_update(self)    -> None: self.push_screen("update")
    def action_nav_scripts(self)   -> None: self.push_screen("scripts")
    def action_nav_kuma(self)      -> None: self.push_screen("uptimekuma")
    def action_nav_seerr(self)     -> None: self.push_screen("seerr")
    def action_nav_plex(self)      -> None: self.push_screen("plex")
    def action_nav_pihole(self)    -> None: self.push_screen("pihole")
    def action_nav_speedtest(self) -> None: self.push_screen("speedtest")
    def action_nav_arr(self)      -> None: self.push_screen("arr")
    def action_nav_docker(self)   -> None: self.push_screen("docker")
    def action_nav_unifi(self)    -> None: self.push_screen("unifi")
    def action_nav_metrics(self)  -> None: self.push_screen("metrics")
    def action_nav_backup(self)   -> None: self.push_screen("backup")
    def action_nav_quick(self)    -> None: self.push_screen("quick")

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action.startswith("nav_") or action == "quit":
            # Priority bindings are checked App-down and would otherwise fire
            # even while a modal (e.g. Docker's per-row ActionMenu) is on
            # top and expects to capture every keystroke itself — including
            # single letters like "s" that collide with a nav shortcut here.
            # Suppress all nav/quit shortcuts whenever a modal screen is active.
            from textual.screen import ModalScreen
            if isinstance(self.screen, ModalScreen):
                return False
            return True
        return super().check_action(action, parameters)


def main() -> None:
    import click
    import typer.main as _tm

    from cli import app as cli_app
    from cli import run_backup_run, run_get_detail, run_update_hosts

    # Build the Click app once so we can patch subgroups before invocation.
    click_app = _tm.get_command(cli_app)

    # Patch the 'get' subgroup so that unknown subcommand names (e.g. a VMID or
    # hostname) are routed to the detail view instead of raising "No such command".
    get_group = click_app.commands.get("get")  # type: ignore[union-attr]
    if get_group is not None:
        _orig_get_cmd = get_group.get_command

        def _patched_get_cmd(ctx: click.Context, cmd_name: str):
            rv = _orig_get_cmd(ctx, cmd_name)
            if rv is not None:
                return rv

            selector = cmd_name

            @click.command(cmd_name, add_help_option=True,
                           short_help=f"Show detail for '{cmd_name}'.")
            @click.option("-t", "--tag", default=None,
                          help="Additional tag filter")
            @click.option("-o", "--output", "output_fmt",
                          type=click.Choice(["table", "json"]), default="table",
                          help="Output format: table | json")
            def _detail(tag, output_fmt):
                run_get_detail(selector, tag, output_fmt)

            return _detail

        get_group.get_command = _patched_get_cmd  # type: ignore[method-assign]

    # Patch the 'update' subgroup so that unknown names are routed to
    # 'update hosts' (apt upgrade on matching guests).
    update_group = click_app.commands.get("update")  # type: ignore[union-attr]
    if update_group is not None:
        _orig_update_cmd = update_group.get_command

        def _patched_update_cmd(ctx: click.Context, cmd_name: str):
            rv = _orig_update_cmd(ctx, cmd_name)
            if rv is not None:
                return rv

            selector = cmd_name

            @click.command(cmd_name, add_help_option=True,
                           short_help=f"Run apt upgrade on guests matching '{cmd_name}'.")
            @click.option("-l", "--label", default=None,
                          help="Additional tag filter")
            @click.option("--yes", "-y", is_flag=True, default=False,
                          help="Skip confirmation")
            @click.option("--verbose", "-v", is_flag=True, default=False,
                          help="Show full apt output")
            def _update(label, yes, verbose):
                run_update_hosts(selector, label, yes, verbose)

            return _update

        update_group.get_command = _patched_update_cmd  # type: ignore[method-assign]

    # Patch the 'backup' subgroup so that unknown names are routed to
    # 'backup run' (immediate vzdump backup of matching guests).
    backup_group = click_app.commands.get("backup")  # type: ignore[union-attr]
    if backup_group is not None:
        _orig_backup_cmd = backup_group.get_command

        def _patched_backup_cmd(ctx: click.Context, cmd_name: str):
            rv = _orig_backup_cmd(ctx, cmd_name)
            if rv is not None:
                return rv

            selector = cmd_name

            @click.command(cmd_name, add_help_option=True,
                           short_help=f"Back up guests matching '{cmd_name}'.")
            @click.option("-l", "--label", default=None,
                          help="Additional tag filter")
            @click.option("--storage", "-s", default=None,
                          help="Backup storage (auto-detected if omitted)")
            @click.option("--yes", "-y", is_flag=True, default=False,
                          help="Skip confirmation")
            def _backup(label, storage, yes):
                run_backup_run(selector, label, storage, yes)

            return _backup

        backup_group.get_command = _patched_backup_cmd  # type: ignore[method-assign]

    # Register "up" as a hidden alias for "uptimekuma"
    if "uptimekuma" in (click_app.commands or {}):
        click_app.commands["up"] = click_app.commands["uptimekuma"]
        _orig_list_commands = click_app.list_commands
        click_app.list_commands = lambda ctx: [  # type: ignore[method-assign]
            c for c in _orig_list_commands(ctx) if c != "up"
        ]

    # Auth gate — skip for `atc auth` subcommands so the user can set/clear PIN
    # without being asked for the PIN they're trying to configure.
    raw_args = sys.argv[1:]
    _auth_exempt = raw_args and raw_args[0] == "auth"
    if not _auth_exempt:
        from auth import require_auth
        require_auth()

    click_app(standalone_mode=True)


if __name__ == "__main__":
    main()
