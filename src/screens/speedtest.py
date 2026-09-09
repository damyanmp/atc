"""Speedtest screen — live animated ping sparkline + speed gauges."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from statistics import median as _median

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config

_BAR_W   = 36
_MAX_MBS = 1000.0
_SPIN    = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPARK   = "▁▂▃▄▅▆▇█"
_PING_N  = 10



def _quality(mbps: float) -> tuple[str, str]:
    if mbps >= 900: return "Gigabit",   "bright_green"
    if mbps >= 500: return "Excellent", "green"
    if mbps >= 200: return "Fast",      "cyan"
    if mbps >= 100: return "Good",      "yellow"
    if mbps >= 25:  return "Basic",     "dark_orange"
    return "Slow", "red"


def _ping_color(ms: float) -> str:
    if ms < 10:  return "bright_green"
    if ms < 30:  return "green"
    if ms < 80:  return "yellow"
    return "red"


def _bar(mbps: float) -> str:
    filled = round(min(mbps / _MAX_MBS, 1.0) * _BAR_W)
    return "█" * filled + "░" * (_BAR_W - filled)


def _fmt_bytes(b: int) -> str:
    if b >= 1024 ** 3: return f"{b / 1024**3:.2f} GB"
    if b >= 1024 ** 2: return f"{b / 1024**2:.0f} MB"
    return f"{b / 1024:.0f} KB"


# ── Panel renderers ───────────────────────────────────────────────────────────

def _ping_panel(samples: list[float], active: bool) -> str:
    """Render the ping panel — dot progress + sparkline + summary."""
    lines: list[str] = ["[bold cyan]📡  PING[/bold cyan]", ""]

    # Dot row — one dot per sample, color by RTT
    dots: list[str] = []
    for i in range(_PING_N):
        if i < len(samples):
            c = _ping_color(samples[i])
            dots.append(f"[{c}]●[/{c}]")
        elif i == len(samples) and active:
            dots.append("[dim blink]◌[/dim blink]")
        else:
            dots.append("[dim]○[/dim]")
    lines.append("  " + "  ".join(dots))

    if not samples:
        lines.append("\n  [dim]waiting for samples…[/dim]")
        return "\n".join(lines)

    # RTT values row
    rtt_row = "   ".join(f"{s:5.1f}" for s in samples)
    lines.append(f"  [dim]{rtt_row} ms[/dim]")

    # Sparkline
    mn, mx = min(samples), max(samples)
    span = mx - mn or 0.001
    spark = "".join(
        _SPARK[min(int((s - mn) / span * len(_SPARK)), len(_SPARK) - 1)]
        for s in samples
    )
    lines.append(f"  [cyan]{spark}[/cyan]")

    # Summary (only when done)
    if not active:
        med = _median(samples)
        deltas = [abs(samples[i] - samples[i - 1]) for i in range(1, len(samples))]
        jitter = sum(deltas) / len(deltas) if deltas else 0.0
        c = _ping_color(med)
        lines.append("")
        lines.append(
            f"  [{c}]Median  {med:.1f} ms[/{c}]"
            f"   [dim]min {mn:.1f}   max {mx:.1f}   jitter {jitter:.1f} ms[/dim]"
        )

    return "\n".join(lines)


def _speed_panel(icon: str, label: str, mbps: float, total_bytes: int, phase: str) -> str:
    """Render a download or upload panel.

    phase: "idle" | "active" | "done"
    """
    title = f"[bold]{icon}  {label}[/bold]"

    if phase == "idle":
        empty_bar = "[dim]" + "░" * _BAR_W + "[/dim]"
        return f"{title}\n\n  [dim]    —   Mbps[/dim]\n\n  {empty_bar}\n  [dim]0% of 1 Gbps[/dim]\n\n  [dim]waiting[/dim]\n"

    qlabel, color = _quality(mbps)
    filled_bar = f"[{color}]{_bar(mbps)}[/{color}]"
    pct = min(mbps / _MAX_MBS * 100, 100)
    ellipsis = "  [dim blink]…[/dim blink]" if phase == "active" else ""
    xfer = f"\n  [dim]{_fmt_bytes(total_bytes)} transferred[/dim]" if total_bytes else ""

    return (
        f"{title}\n"
        f"\n"
        f"  [{color}]{mbps:>8.1f} Mbps[/{color}]{ellipsis}\n"
        f"\n"
        f"  {filled_bar}\n"
        f"  [dim]{pct:.0f}% of 1 Gbps[/dim]\n"
        f"\n"
        f"  [{color}]{qlabel}[/{color}]{xfer}\n"
    )


# ── Screen ────────────────────────────────────────────────────────────────────

class SpeedtestScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("ctrl+r", "rerun",          "Re-run"),
    ]

    DEFAULT_CSS = """
    SpeedtestScreen { layout: vertical; }

    #status-bar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    #main {
        height: 1fr;
        layout: vertical;
        margin: 0;
    }
    #ping-panel {
        border: round $accent;
        padding: 1 2;
        margin: 1 1 0 1;
        height: auto;
        min-height: 8;
    }
    #speed-row {
        height: 1fr;
        layout: horizontal;
        margin: 1 1 0 1;
    }
    #dl-panel {
        border: round $accent;
        padding: 1 2;
        width: 1fr;
        height: 1fr;
        margin: 0 1 0 0;
    }
    #ul-panel {
        border: round $accent;
        padding: 1 2;
        width: 1fr;
        height: 1fr;
    }
    #fact-bar {
        height: 3;
        border: round $accent;
        background: $surface;
        color: $text;
        padding: 0 2;
        margin: 1 1 0 1;
        content-align: left middle;
    }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Starting…", id="status-bar", markup=False)
        with Vertical(id="main"):
            yield Static(_ping_panel([], False), id="ping-panel", markup=True)
            with Horizontal(id="speed-row"):
                yield Static(_speed_panel("⬇", "DOWNLOAD", 0, 0, "idle"), id="dl-panel", markup=True)
                yield Static(_speed_panel("⬆", "UPLOAD",   0, 0, "idle"), id="ul-panel", markup=True)
        yield Static("", id="fact-bar", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self._run_test()

    def action_rerun(self) -> None:
        self._run_test()

    @work(exclusive=True)
    async def _run_test(self) -> None:
        if self.config.openspeedtest is None:
            self.query_one("#status-bar", Static).update(
                "❌ No openspeedtest section in config.yaml"
            )
            return

        cfg    = self.config.openspeedtest
        status = self.query_one("#status-bar", Static)
        pp     = self.query_one("#ping-panel", Static)
        dlp    = self.query_one("#dl-panel",   Static)
        ulp    = self.query_one("#ul-panel",   Static)

        self.query_one("#fact-bar", Static).update("")
        pp.update(_ping_panel([], True))
        dlp.update(_speed_panel("⬇", "DOWNLOAD", 0, 0, "idle"))
        ulp.update(_speed_panel("⬆", "UPLOAD",   0, 0, "idle"))

        try:
            from lib.speedtest import download_test, ping_test, upload_test

            # ── Ping ──────────────────────────────────────────────────────────
            samples: list[float] = []
            spin_i  = 0
            stop_spin = asyncio.Event()

            def on_sample(i: int, rtt: float) -> None:
                samples.append(rtt)
                pp.update(_ping_panel(samples, i < _PING_N - 1))

            async def _spin_status() -> None:
                nonlocal spin_i
                while not stop_spin.is_set():
                    status.update(
                        f"{_SPIN[spin_i % len(_SPIN)]}  Pinging…  "
                        f"({len(samples)}/{_PING_N} samples)"
                    )
                    spin_i += 1
                    await asyncio.sleep(0.1)

            spin_task = asyncio.create_task(_spin_status())
            try:
                ping = await ping_test(cfg, samples=_PING_N, on_sample=on_sample)
            finally:
                stop_spin.set()
                await spin_task

            pp.update(_ping_panel(ping.samples, False))

            # ── Download ──────────────────────────────────────────────────────
            dl_counter: list[int] = [0]
            stop_dl = asyncio.Event()
            t0_dl   = time.perf_counter()

            async def _live_dl() -> None:
                while not stop_dl.is_set():
                    elapsed = time.perf_counter() - t0_dl
                    mbps = (dl_counter[0] * 8) / (max(elapsed, 0.001) * 1_000_000)
                    dlp.update(_speed_panel("⬇", "DOWNLOAD", mbps, dl_counter[0], "active"))
                    await asyncio.sleep(0.2)

            status.update("⬇  Download test  (10 s, 4 streams)…")
            live_dl = asyncio.create_task(_live_dl())
            try:
                dl_mbps, dl_bytes = await download_test(
                    cfg, duration_s=10, parallel=4, counter=dl_counter
                )
            finally:
                stop_dl.set()
                await live_dl

            dlp.update(_speed_panel("⬇", "DOWNLOAD", dl_mbps, dl_bytes, "done"))

            # ── Upload ────────────────────────────────────────────────────────
            ul_counter: list[int] = [0]
            stop_ul = asyncio.Event()
            t0_ul   = time.perf_counter()

            async def _live_ul() -> None:
                while not stop_ul.is_set():
                    elapsed = time.perf_counter() - t0_ul
                    mbps = (ul_counter[0] * 8) / (max(elapsed, 0.001) * 1_000_000)
                    ulp.update(_speed_panel("⬆", "UPLOAD", mbps, ul_counter[0], "active"))
                    await asyncio.sleep(0.2)

            status.update("⬆  Upload test  (10 s, 2 streams)…")
            live_ul = asyncio.create_task(_live_ul())
            try:
                ul_mbps, ul_bytes = await upload_test(
                    cfg, duration_s=10, parallel=2, counter=ul_counter
                )
            finally:
                stop_ul.set()
                await live_ul

            ulp.update(_speed_panel("⬆", "UPLOAD", ul_mbps, ul_bytes, "done"))

            # ── Summary status bar + fact ─────────────────────────────────────
            dl_q, _ = _quality(dl_mbps)
            ul_q, _ = _quality(ul_mbps)
            status.update(
                f"📡 {ping.median_ms:.1f} ms  "
                f"⬇ {dl_mbps:.1f} Mbps ({dl_q})  "
                f"⬆ {ul_mbps:.1f} Mbps ({ul_q})"
                f"  •  Esc back  •  ^R re-run"
            )
            from lib.speedtest import pick_fact
            fact = pick_fact(ping.median_ms, dl_mbps, ul_mbps)
            self.query_one("#fact-bar", Static).update(f"[bold yellow]💡[/bold yellow]  [italic]{fact}[/italic]")

        except Exception as e:
            status.update(f"❌ {e}")
