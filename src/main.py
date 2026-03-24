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
    # No args → show help (typer handles it)
    # "tui" arg → Textual app (also handled via typer command in cli.py)
    # Any other arg → delegate to typer CLI
    from cli import app as cli_app
    cli_app()


if __name__ == "__main__":
    main()
