"""Media Pipeline screen — release calendar (left) + download queue (right)."""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config

# ── Formatting helpers ────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 16) -> str:
    n = round(min(max(pct, 0.0), 100.0) / 100.0 * width)
    return "█" * n + "░" * (width - n)


def _bytes(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024**3:.1f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024**2:.0f} MB"
    return f"{b / 1024:.0f} KB"


def _eta(raw: str) -> str:
    """Format 'HH:MM:SS' or 'D.HH:MM:SS' to a short label."""
    if not raw:
        return ""
    # strip optional "D." prefix
    if "." in raw:
        raw = raw.split(".", 1)[1]
    parts = raw.split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return raw[:8]


def _section(log: RichLog, title: str, color: str = "bold white") -> None:
    log.write(f"[{color}]{'─' * 2} {title} {'─' * max(0, 48 - len(title))}[/{color}]")


# ── Screen ────────────────────────────────────────────────────────────────────

class ArrScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("r", "reload", "Reload"),
    ]

    DEFAULT_CSS = """
    ArrScreen { layout: vertical; }
    #status-bar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    #main { height: 1fr; layout: horizontal; }

    #left-col {
        width: 2fr;
        layout: vertical;
        border-right: solid $accent;
    }
    #right-col {
        width: 3fr;
        layout: vertical;
    }
    .pane-title {
        height: 1;
        text-style: bold;
        color: $accent;
        padding: 0 1;
        background: $surface;
    }
    #calendar-log { height: 1fr; padding: 0 1; }
    #queue-log    { height: 1fr; padding: 0 1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("⏳ Loading…", id="status-bar")
        with Horizontal(id="main"):
            with Vertical(id="left-col"):
                yield Static("📅  Release Calendar  (past 5d + next 14d)", classes="pane-title")
                yield RichLog(id="calendar-log", highlight=False, markup=True)
            with Vertical(id="right-col"):
                yield Static("⬇  Download Pipeline", classes="pane-title")
                yield RichLog(id="queue-log", highlight=False, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._load)

    def action_reload(self) -> None:
        self.query_one("#calendar-log", RichLog).clear()
        self.query_one("#queue-log", RichLog).clear()
        self.query_one("#status-bar", Static).update("⏳ Reloading…")
        self._load()

    # ── Background worker ─────────────────────────────────────────────────────

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        from lib.arr import (
            get_queue,
            get_radarr_calendar,
            get_sonarr_calendar,
            get_today_imports,
        )

        today = date.today()
        start = today - timedelta(days=5)
        end   = today + timedelta(days=14)

        movies   = []
        episodes = []
        radarr_q = []
        sonarr_q = []
        radarr_imports = []
        sonarr_imports = []
        errors: list[str] = []

        if self.config.radarr:
            try:
                movies     = get_radarr_calendar(self.config.radarr, start, end)
                radarr_q   = get_queue(self.config.radarr)
                radarr_imports = get_today_imports(self.config.radarr, today)
            except Exception as e:
                errors.append(f"Radarr: {e}")
        if self.config.sonarr:
            try:
                episodes   = get_sonarr_calendar(self.config.sonarr, start, end)
                sonarr_q   = get_queue(self.config.sonarr)
                sonarr_imports = get_today_imports(self.config.sonarr, today)
            except Exception as e:
                errors.append(f"Sonarr: {e}")

        self.app.call_from_thread(
            self._render_calendar, movies, episodes, radarr_q + sonarr_q, today
        )
        self.app.call_from_thread(
            self._render_queue, radarr_q, sonarr_q, radarr_imports + sonarr_imports
        )

        status = "r reload  |  Esc back"
        if errors:
            status = "❌ " + "  ".join(errors) + "  |  " + status
        elif not self.config.radarr and not self.config.sonarr:
            status = "⚠ Radarr / Sonarr not configured in config.yaml"
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, status
        )

    # ── Calendar renderer ─────────────────────────────────────────────────────

    def _render_calendar(self, movies, episodes, queue_items, today: date) -> None:
        log = self.query_one("#calendar-log", RichLog)
        log.clear()

        # Build title → queue-item lookup for status enrichment
        q_by_title: dict[str, object] = {}
        for q in queue_items:
            q_by_title[q.title.lower()] = q

        def _queue_for(title: str):
            tl = title.lower()
            for k, q in q_by_title.items():
                if tl in k or k in tl:
                    return q
            return None

        # Merge into one list per date
        by_date: dict[date, list] = defaultdict(list)
        for m in movies:
            try:
                d = date.fromisoformat(m.release_date)
            except Exception:
                continue
            by_date[d].append(("movie", m))
        for ep in episodes:
            try:
                d = date.fromisoformat(ep.air_date)
            except Exception:
                continue
            by_date[d].append(("episode", ep))

        if not by_date:
            if not self.config.radarr and not self.config.sonarr:
                log.write("[dim]Radarr / Sonarr not configured.[/dim]")
            else:
                log.write("[dim]No releases found in the calendar window.[/dim]")
            return

        for d in sorted(by_date.keys()):
            is_today = (d == today)
            is_past  = (d < today)
            day_label = d.strftime("%a %b %-d")

            # Date header
            if is_today:
                log.write(f"\n[bold yellow]━━ TODAY · {day_label} {'━' * max(0, 38 - len(day_label))}[/bold yellow]")
            elif is_past:
                log.write(f"\n[dim]╌╌ {day_label} {'╌' * max(0, 44 - len(day_label))}[/dim]")
            else:
                days_ahead = (d - today).days
                suffix = f"  in {days_ahead}d"
                log.write(f"\n[cyan]╌╌ {day_label}{suffix} {'╌' * max(0, 44 - len(day_label) - len(suffix))}[/cyan]")

            for kind, item in by_date[d]:
                if kind == "movie":
                    icon = "🎬"
                    title_line = f"{item.title} [dim]({item.year})[/dim]"
                    q = _queue_for(item.title)
                    if item.has_file:
                        badge = "[green]✓ Downloaded[/green]"
                    elif q:
                        st = (q.status or "").lower()
                        if st == "downloading":
                            pct = q.progress_pct
                            badge = f"[blue]⬇ {_bar(pct, 10)}[/blue] [bold]{pct:.0f}%[/bold]"
                        else:
                            badge = "[yellow]⏳ Queued[/yellow]"
                    elif item.release_type == "cinema":
                        badge = "[magenta]🎭 In cinemas[/magenta]"
                    elif is_past or is_today:
                        badge = "[red]⚠ Missing[/red]"
                    else:
                        badge = "[dim]📅 Upcoming[/dim]"
                    log.write(f"  {icon}  {title_line}")
                    log.write(f"     {badge}")

                else:  # episode
                    icon = "📺"
                    ep_tag = f"S{item.season:02d}E{item.episode:02d}"
                    ep_label = f"– {item.title}" if item.title else ""
                    title_line = f"[bold]{item.series_title}[/bold]  [dim]{ep_tag} {ep_label}[/dim]"
                    q = _queue_for(item.series_title)
                    if item.has_file:
                        badge = "[green]✓ Downloaded[/green]"
                    elif q:
                        st = (q.status or "").lower()
                        if st == "downloading":
                            pct = q.progress_pct
                            badge = f"[blue]⬇ {_bar(pct, 10)}[/blue] [bold]{pct:.0f}%[/bold]"
                        else:
                            badge = "[yellow]⏳ Queued[/yellow]"
                    elif is_past or is_today:
                        if item.monitored:
                            badge = "[red]⚠ Missing[/red]"
                        else:
                            badge = "[dim]— Unmonitored[/dim]"
                    else:
                        badge = "[dim]📅 Upcoming[/dim]"
                    log.write(f"  {icon}  {title_line}")
                    log.write(f"     {badge}")

    # ── Queue renderer ────────────────────────────────────────────────────────

    def _render_queue(self, radarr_q, sonarr_q, imports) -> None:
        log = self.query_one("#queue-log", RichLog)
        log.clear()

        all_q = (
            [(q, "Radarr") for q in radarr_q] +
            [(q, "Sonarr") for q in sonarr_q]
        )

        # Bucket by status
        downloading = [(q, src) for q, src in all_q if (q.status or "").lower() == "downloading"]
        queued      = [(q, src) for q, src in all_q if (q.status or "").lower() in ("queued", "delay", "paused")]
        warnings    = [(q, src) for q, src in all_q if (q.status or "").lower() in ("warning", "error", "failed")]
        completed   = [(q, src) for q, src in all_q if (q.tracked_state or "").lower() in ("imported", "importpending", "importing")]

        # ── Downloading ───────────────────────────────────────────────────────
        if downloading:
            _section(log, f"DOWNLOADING  ({len(downloading)})", "bold blue")
            for q, src in downloading:
                pct  = q.progress_pct
                bar  = _bar(pct)
                size = _bytes(q.size_bytes) if q.size_bytes else ""
                eta  = _eta(q.time_left)
                proto = f"[{getattr(q, 'protocol', None) or src.lower()}]"
                log.write(
                    f"  [blue]⬇[/blue]  [bold]{q.title[:40]}[/bold]\n"
                    f"     [blue]{bar}[/blue]  [bold]{pct:.0f}%[/bold]"
                    f"  {size}  {f'ETA {eta}' if eta else ''}  [dim]{proto}[/dim]"
                )
        else:
            _section(log, "DOWNLOADING  (none)", "dim")

        log.write("")

        # ── Queued ────────────────────────────────────────────────────────────
        if queued:
            _section(log, f"QUEUED  ({len(queued)})", "bold yellow")
            for q, src in queued:
                size  = _bytes(q.size_bytes) if q.size_bytes else "—"
                proto = f"[{getattr(q, 'protocol', None) or src.lower()}]"
                st    = (q.status or "").lower()
                icon  = "⏸" if st == "paused" else "⏳"
                log.write(
                    f"  [yellow]{icon}[/yellow]  {q.title[:50]}"
                    f"  [dim]{size}  {proto}[/dim]"
                )
        else:
            _section(log, "QUEUED  (none)", "dim")

        log.write("")

        # ── Warnings / Errors ─────────────────────────────────────────────────
        if warnings:
            _section(log, f"WARNINGS  ({len(warnings)})", "bold red")
            for q, src in warnings:
                err = q.error_message[:60] if q.error_message else ""
                log.write(f"  [red]⚠[/red]  {q.title[:48]}  [dim]{err}[/dim]")
            log.write("")

        # ── Completed / Importing ─────────────────────────────────────────────
        if completed:
            _section(log, f"IMPORTING  ({len(completed)})", "bold green")
            for q, src in completed:
                state = (q.tracked_state or "").replace("importPending", "Import pending")
                log.write(f"  [green]↗[/green]  {q.title[:50]}  [dim]{state}[/dim]")
            log.write("")

        # ── Today's imports ───────────────────────────────────────────────────
        if imports:
            _section(log, f"IMPORTED TODAY  ({len(imports)})", "bold green")
            for rec in imports:
                if rec.media_type == "episode":
                    ep_tag = f"S{rec.season:02d}E{rec.episode:02d}"
                    label  = f"{rec.title}  [dim]{ep_tag}[/dim]"
                    if rec.episode_title:
                        label += f"  [dim]– {rec.episode_title}[/dim]"
                else:
                    label = f"{rec.title}"
                    if rec.year:
                        label += f"  [dim]({rec.year})[/dim]"
                qual = f"  [dim]{rec.quality}[/dim]" if rec.quality else ""
                size = f"  [dim]{_bytes(rec.size_bytes)}[/dim]" if rec.size_bytes else ""
                log.write(f"  [green]✓[/green]  {label}{qual}{size}")
        else:
            _section(log, "IMPORTED TODAY  (none)", "dim")

        if not all_q and not imports:
            log.write("")
            log.write("[dim]No active downloads.[/dim]")
