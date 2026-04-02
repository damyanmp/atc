"""atc — entry point. Routes to CLI (typer) or TUI (Textual)."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ and src/lib are importable regardless of working directory
_SRC = Path(__file__).parent
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "lib"))

from textual.app import App
from config import load_config, Config
from screens.dashboard import DashboardScreen
from screens.update import UpdateScreen
from screens.scripts import ScriptsScreen


class HomelabApp(App):
    """atc — Air Traffic Control TUI."""

    TITLE = "atc"
    SUB_TITLE = "Air Traffic Control"
    CSS = """
    Screen { background: $background; }
    Header { background: $primary; color: $text; }
    Footer { background: $primary-darken-1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def on_mount(self) -> None:
        self.install_screen(DashboardScreen(self.config), name="dashboard")
        self.install_screen(UpdateScreen(self.config), name="update")
        self.install_screen(ScriptsScreen(self.config), name="scripts")
        self.push_screen("dashboard")


def main() -> None:
    import click
    import typer.main as _tm
    from cli import app as cli_app, run_get_detail, run_update_hosts, run_backup_run

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
