"""Blackbox — diagnostic snapshot collector.

Gathers data from every layer of the stack in parallel and returns a
BlackboxDump that can be rendered to a plain-text file for analysis.

Sources collected (each optional/skipped if not configured):
  - Uptime Kuma: current DOWN monitors + last 10 outage events
  - Proxmox: offline nodes + failed cluster tasks
  - SSH hosts: journalctl error log from Proxmox nodes + configured extras
  - Portainer: container health + logs for unhealthy/exited containers
  - Home Assistant: error log tail
  - Prometheus: active/firing alert rules
  - UniFi Network: device inventory, recent reboots, firmware updates, offline devices
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from config import Config


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class BBUptimeKuma:
    down_monitors: list[str] = field(default_factory=list)   # "name (uptime 24h: X%)"
    outages: list[str] = field(default_factory=list)          # formatted outage lines
    outage_note: str = ""   # informational note when outage history unavailable
    error: str = ""


@dataclass
class BBProxmox:
    offline_nodes: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class BBHostLog:
    name: str
    host: str
    journal_errors: str = ""
    error: str = ""


@dataclass
class BBDockerEnv:
    name: str
    containers: list[str] = field(default_factory=list)       # one-line summaries
    outages: list[str] = field(default_factory=list)          # formatted outage lines
    logs: dict[str, str] = field(default_factory=dict)        # container_name → log text
    error: str = ""


@dataclass
class BBHomeAssistant:
    error_log: str = ""
    error: str = ""


@dataclass
class BBPrometheus:
    firing_alerts: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class BBLoki:
    syslog_events: list[str] = field(default_factory=list)   # filtered, formatted "[ts] host: line"
    pihole_events: list[str] = field(default_factory=list)
    syslog_lines_scanned: int = 0
    pihole_lines_scanned: int = 0
    error: str = ""


@dataclass
class BBUniFi:
    devices: list[str] = field(default_factory=list)           # full device table, one line each
    recent_reboots: list[str] = field(default_factory=list)    # boot time inside window
    firmware_updates: list[str] = field(default_factory=list)  # version changed, inside window
    offline_devices: list[str] = field(default_factory=list)   # state != 1 (not connected)
    error: str = ""


@dataclass
class BlackboxDump:
    collected_at: str
    window_hours: int
    uptime_kuma: BBUptimeKuma | None = None
    proxmox: BBProxmox | None = None
    ssh_hosts: list[BBHostLog] = field(default_factory=list)
    docker_envs: list[BBDockerEnv] = field(default_factory=list)
    homeassistant: BBHomeAssistant | None = None
    prometheus: BBPrometheus | None = None
    loki: BBLoki | None = None
    unifi: BBUniFi | None = None


# ── Collectors ────────────────────────────────────────────────────────────────

def _collect_uptimekuma(cfg) -> BBUptimeKuma:
    from lib.uptimekuma import fetch_monitors, fetch_outages, status_label

    result = BBUptimeKuma()
    try:
        monitors = fetch_monitors(cfg.uptimekuma)
        for m in monitors:
            label, _ = status_label(m.status)
            if m.status != 1:  # not UP
                result.down_monitors.append(
                    f"{m.name} — {label} (uptime 24h: {m.uptime_24h*100:.0f}%,"
                    f" 30d: {m.uptime_720h*100:.0f}%)"
                )
    except Exception as e:
        result.error = f"monitor fetch failed: {e}"

    try:
        outages = fetch_outages(cfg.uptimekuma, limit=10)
        for o in outages:
            dur = f"{o.duration_min}m" if o.ended_at != "ongoing" else "ONGOING"
            msg = f" — {o.error_msg}" if o.error_msg else ""
            result.outages.append(
                f"[{o.started_at[:19]}] {o.monitor_name}: DOWN for {dur}{msg}"
            )
        if not outages:
            result.outage_note = "no status page configured — outage history unavailable"
    except Exception as e:
        if not result.error:
            result.error = f"outage history failed: {e}"
        else:
            result.error += f"; outage history failed: {e}"

    return result


def _collect_proxmox(cfg, window_hours: int) -> BBProxmox:
    from proxmoxer import ProxmoxAPI
    from config import ProxmoxClusterConfig

    result = BBProxmox()
    try:
        px = ProxmoxAPI(
            cfg.proxmox.host,
            port=cfg.proxmox.port,
            user=cfg.proxmox.user,
            token_name=cfg.proxmox.token_name,
            token_value=cfg.proxmox.token_value,
            verify_ssl=cfg.proxmox.verify_ssl,
            timeout=10,
        )

        # Offline nodes
        for node in px.nodes.get():
            if node.get("status") != "online":
                result.offline_nodes.append(
                    f"{node['node']} — status: {node.get('status', 'unknown')}"
                )

        # Recent failed cluster tasks
        import time
        since = int(time.time()) - (window_hours * 3600)
        try:
            tasks = px.cluster.tasks.get()
            for t in tasks:
                starttime = int(t.get("starttime", 0))
                if starttime < since:
                    continue
                status = t.get("status", "")
                if status not in ("OK", ""):
                    ts = datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat()
                    result.failed_tasks.append(
                        f"[{ts[:19]}] {t.get('node','?')} {t.get('type','?')}"
                        f" {t.get('id','?')} — {status}"
                    )
        except Exception as e:
            result.failed_tasks.append(f"[task fetch error: {e}]")

    except Exception as e:
        result.error = str(e)

    return result


async def _collect_host_journal(name: str, host: str, user: str | None, ssh_cfg, window_hours: int) -> BBHostLog:
    from lib.ssh import run_command

    cmd = (
        f"journalctl -p err --since '{window_hours} hours ago' "
        f"--no-pager -q -o short-iso 2>/dev/null | tail -n 300"
    )
    result = await run_command(host, cmd, ssh_cfg, user=user or None)
    r = BBHostLog(name=name, host=host)
    if result.success or result.stdout:
        r.journal_errors = result.stdout or "(no errors in window)"
    else:
        r.error = result.stderr or "command failed"
    return r


async def _collect_all_ssh(cfg, window_hours: int) -> list[BBHostLog]:
    from lib.proxmox import get_status as _px_status
    from proxmoxer import ProxmoxAPI

    targets: list[tuple[str, str, str | None]] = []  # (name, host, user)

    # Discover Proxmox nodes
    try:
        px = ProxmoxAPI(
            cfg.proxmox.host,
            port=cfg.proxmox.port,
            user=cfg.proxmox.user,
            token_name=cfg.proxmox.token_name,
            token_value=cfg.proxmox.token_value,
            verify_ssl=cfg.proxmox.verify_ssl,
            timeout=10,
        )
        for node in px.nodes.get():
            node_name = node["node"]
            host = cfg.ssh.node_host(node_name)
            targets.append((node_name, host, None))
    except Exception:
        pass

    # Extra configured SSH hosts
    for h in cfg.blackbox.ssh_hosts:
        targets.append((h.name, h.host, h.user or None))

    tasks = [
        _collect_host_journal(name, host, user, cfg.ssh, window_hours)
        for name, host, user in targets
    ]
    return list(await asyncio.gather(*tasks))


_ERROR_PATTERN = re.compile(
    r"\b(error|err|critical|crit|fatal|panic|exception|traceback|stderr"
    r"|unhandled|segfault|oom|killed|abort)\b",
    re.IGNORECASE,
)


def _log_has_errors(text: str) -> bool:
    """Return True if any line in `text` looks like an application error."""
    for line in text.splitlines():
        if _ERROR_PATTERN.search(line):
            return True
    return False


async def _collect_portainer(cfg, window_hours: int, all_containers: bool) -> list[BBDockerEnv]:
    from lib.portainer import (
        fetch_environments, fetch_containers, fetch_container_logs,
        fetch_container_events, outages_from_events,
    )

    since_seconds = window_hours * 3600
    envs = fetch_environments(cfg.portainer)

    async def _process_env(env) -> BBDockerEnv:
        bb_env = BBDockerEnv(name=env.name)
        if env.status != 1:
            bb_env.error = "environment inactive"
            return bb_env
        try:
            containers = fetch_containers(cfg.portainer, env.id, env.name, all_containers=True)
        except Exception as e:
            bb_env.error = str(e)
            return bb_env

        # Container outage detection via Docker events
        try:
            events = fetch_container_events(cfg.portainer, env.id, since_seconds=since_seconds)
            for o in outages_from_events(env.name, events):
                dur = f"{o.duration_min}m" if o.ended_at != "ongoing" else "ONGOING"
                code = f" exit={o.exit_code}" if o.exit_code and o.exit_code != "0" else ""
                bb_env.outages.append(
                    f"[{o.started_at[:19]}] {o.container_name}: DOWN for {dur}{code}"
                )
        except Exception as e:
            bb_env.outages.append(f"[event fetch failed: {e}]")

        for c in containers:
            health_tag   = f" [{c.health}]" if c.health else ""
            restart_tag  = " [recent-restart]" if (c.state == "running" and c.started_within(since_seconds)) else ""
            bb_env.containers.append(
                f"  {c.name:<40} {c.state:<12}{health_tag}{restart_tag}  {c.status}"
            )

            # Always fetch logs within the window for every container.
            # Include in the dump if: bad state, recent restart, errors in output, or --all-containers.
            try:
                log_text = fetch_container_logs(
                    cfg.portainer, env.id, c.id, since_seconds=since_seconds
                )
            except Exception as e:
                bb_env.logs[c.name] = f"[log fetch failed: {e}]"
                continue

            if not log_text.strip():
                continue

            if all_containers or c.needs_attention or c.started_within(since_seconds) or _log_has_errors(log_text):
                bb_env.logs[c.name] = log_text

        return bb_env

    tasks = [_process_env(env) for env in envs]
    return list(await asyncio.gather(*tasks))


def _collect_homeassistant(cfg, window_hours: int) -> BBHomeAssistant:
    from lib.ha import get_diagnostics
    from datetime import datetime, timezone, timedelta

    result = BBHomeAssistant()
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        result.error_log = get_diagnostics(cfg.ha, since_iso=since)
    except Exception as e:
        result.error = str(e)
    return result


_SKIP_ALERTS = {"Watchdog", "NoAlertsFiring", "DeadMansSwitch"}


def _collect_prometheus(
    prometheus_url: str, verify_ssl: bool = False, window_hours: int = 2
) -> BBPrometheus:
    """Currently-active alerts (with annotations) plus anything that fired and
    already resolved within the lookback window — `/api/v1/alerts` alone is a
    point-in-time snapshot and misses alerts that cleared before this ran."""
    result = BBPrometheus()
    active_by_key: dict[tuple[str, str], dict] = {}

    try:
        with httpx.Client(verify=verify_ssl, timeout=10) as client:
            r = client.get(f"{prometheus_url}/api/v1/alerts")
            if not r.is_success:
                result.error = f"HTTP {r.status_code}"
                return result
            data = r.json().get("data", {}).get("alerts", [])
            for alert in data:
                if alert.get("state") not in ("firing", "pending"):
                    continue
                labels = alert.get("labels", {})
                name = labels.get("alertname", "?")
                if name in _SKIP_ALERTS:
                    continue
                annotations = alert.get("annotations", {})
                severity = labels.get("severity", "")
                summary = annotations.get("summary", annotations.get("description", ""))
                state = alert.get("state", "")
                active_at = alert.get("activeAt", "")[:19]
                instance = labels.get("instance", "")
                active_by_key[(name, instance)] = {
                    "active_at": active_at,
                    "severity": severity,
                    "summary": summary,
                    "state": state,
                }
                result.firing_alerts.append(
                    f"[{active_at}] {name} ({severity}) [{state}]"
                    + (f" — {summary}" if summary else "")
                )

            # Historical pass: anything that fired at any point in the window,
            # via a range query over the ALERTS metric (survives resolution).
            import time as _time

            now = _time.time()
            start = now - (window_hours * 3600)
            rr = client.get(
                f"{prometheus_url}/api/v1/query_range",
                params={
                    "query": 'ALERTS{alertstate="firing"}',
                    "start": str(start),
                    "end": str(now),
                    "step": "60",
                },
            )
            if rr.is_success:
                rdata = rr.json().get("data", {}).get("result", [])
                for series in rdata:
                    labels = series.get("metric", {})
                    name = labels.get("alertname", "?")
                    if name in _SKIP_ALERTS:
                        continue
                    instance = labels.get("instance", "")
                    key = (name, instance)
                    if key in active_by_key:
                        continue  # already reported as currently active above
                    values = series.get("values", [])
                    if not values:
                        continue
                    first_ts = float(values[0][0])
                    last_ts = float(values[-1][0])
                    from datetime import datetime as _dt, timezone as _tz

                    first_iso = _dt.fromtimestamp(first_ts, tz=_tz.utc).isoformat()[:19]
                    last_iso = _dt.fromtimestamp(last_ts, tz=_tz.utc).isoformat()[:19]
                    severity = labels.get("severity", "")
                    result.firing_alerts.append(
                        f"[{first_iso} → {last_iso}] {name} ({severity}) [resolved — "
                        f"fired within last {window_hours}h, cleared before this scan]"
                    )
    except Exception as e:
        result.error = str(e)
    return result


def _collect_loki(cfg, window_hours: int) -> BBLoki:
    from lib.loki import fetch_network_events, fetch_pihole_events, count_over_window

    result = BBLoki()
    window_minutes = window_hours * 60

    try:
        result.syslog_lines_scanned = count_over_window(cfg.loki, '{job="syslog"}', window_minutes)
        for ts, host, line in fetch_network_events(cfg.loki, window_minutes):
            prefix = f"{host}: " if host else ""
            result.syslog_events.append(f"[{ts}] {prefix}{line.strip()}")
        result.syslog_events = result.syslog_events[:300]
    except Exception as e:
        result.error = f"syslog query failed: {e}"

    try:
        result.pihole_lines_scanned = count_over_window(cfg.loki, '{job="pihole"}', window_minutes)
        for ts, host, line in fetch_pihole_events(cfg.loki, window_minutes):
            prefix = f"{host}: " if host else ""
            result.pihole_events.append(f"[{ts}] {prefix}{line.strip()}")
        result.pihole_events = result.pihole_events[:300]
    except Exception as e:
        if result.error:
            result.error += f"; pihole query failed: {e}"
        else:
            result.error = f"pihole query failed: {e}"

    return result


def _fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"


def _collect_unifi(cfg, window_hours: int) -> BBUniFi:
    from lib.unifi import fetch_devices
    import time

    result = BBUniFi()
    try:
        devices = fetch_devices(cfg.unifi, site=cfg.unifi.site)
    except Exception as e:
        result.error = str(e)
        return result

    now = int(time.time())
    since = now - (window_hours * 3600)

    for d in devices:
        state_tag = "up" if d.state == 1 else f"DOWN(state={d.state})"
        uplink = f" ← {d.uplink_device_name}" if d.uplink_device_name else ""
        result.devices.append(
            f"  {d.name:<20} {d.type:<5} {d.model:<10} v{d.version:<14} {state_tag:<14}"
            f" up {_fmt_duration(d.uptime)}{uplink}"
        )

        if d.state != 1:
            result.offline_devices.append(f"{d.name} ({d.model}) — state={d.state}")

        boot_time = now - d.uptime if d.uptime else d.startup_timestamp
        if boot_time and boot_time >= since:
            boot_iso = datetime.fromtimestamp(boot_time, tz=timezone.utc).isoformat()[:19]
            result.recent_reboots.append(
                f"[{boot_iso}] {d.name} ({d.model}) rebooted — up {_fmt_duration(d.uptime)}{uplink}"
            )

        if d.previous_firmware_version and d.previous_firmware_version != d.version and boot_time >= since:
            boot_iso = datetime.fromtimestamp(boot_time, tz=timezone.utc).isoformat()[:19]
            dur = f" ({d.upgrade_duration}s)" if d.upgrade_duration else ""
            result.firmware_updates.append(
                f"[{boot_iso}] {d.name} ({d.model}): {d.previous_firmware_version} → {d.version}{dur}"
            )

    return result


# ── Orchestration ─────────────────────────────────────────────────────────────

async def collect_all(
    cfg: "Config",
    window_hours: int = 2,
    all_containers: bool = False,
    prometheus_url: str = "",
    prometheus_verify_ssl: bool = False,
) -> BlackboxDump:
    """Collect diagnostics from all configured sources in parallel."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    dump = BlackboxDump(collected_at=now, window_hours=window_hours)

    async def _wrap_sync(fn, *args):
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

    tasks = {}

    if cfg.uptimekuma:
        tasks["uk"] = asyncio.create_task(_wrap_sync(_collect_uptimekuma, cfg))

    tasks["proxmox"] = asyncio.create_task(_wrap_sync(_collect_proxmox, cfg, window_hours))
    tasks["ssh"] = asyncio.create_task(_collect_all_ssh(cfg, window_hours))

    if cfg.portainer:
        tasks["portainer"] = asyncio.create_task(
            _collect_portainer(cfg, window_hours, all_containers)
        )

    if cfg.ha:
        tasks["ha"] = asyncio.create_task(_wrap_sync(_collect_homeassistant, cfg, window_hours))

    if prometheus_url:
        tasks["prometheus"] = asyncio.create_task(
            _wrap_sync(_collect_prometheus, prometheus_url, prometheus_verify_ssl, window_hours)
        )

    if cfg.loki:
        tasks["loki"] = asyncio.create_task(_wrap_sync(_collect_loki, cfg, window_hours))

    if cfg.unifi:
        tasks["unifi"] = asyncio.create_task(_wrap_sync(_collect_unifi, cfg, window_hours))

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    result_map = dict(zip(tasks.keys(), results))

    if "uk" in result_map:
        v = result_map["uk"]
        dump.uptime_kuma = v if isinstance(v, BBUptimeKuma) else BBUptimeKuma(error=str(v))

    v = result_map.get("proxmox")
    dump.proxmox = v if isinstance(v, BBProxmox) else BBProxmox(error=str(v))

    v = result_map.get("ssh")
    dump.ssh_hosts = v if isinstance(v, list) else []

    if "portainer" in result_map:
        v = result_map["portainer"]
        dump.docker_envs = v if isinstance(v, list) else []

    if "ha" in result_map:
        v = result_map["ha"]
        dump.homeassistant = v if isinstance(v, BBHomeAssistant) else BBHomeAssistant(error=str(v))

    if "prometheus" in result_map:
        v = result_map["prometheus"]
        dump.prometheus = v if isinstance(v, BBPrometheus) else BBPrometheus(error=str(v))

    if "loki" in result_map:
        v = result_map["loki"]
        dump.loki = v if isinstance(v, BBLoki) else BBLoki(error=str(v))

    if "unifi" in result_map:
        v = result_map["unifi"]
        dump.unifi = v if isinstance(v, BBUniFi) else BBUniFi(error=str(v))

    return dump


# ── Renderer ──────────────────────────────────────────────────────────────────

_SEP = "━" * 72


def _section(title: str) -> str:
    return f"\n{_SEP}\n{title}\n{_SEP}\n"


def render_dump(dump: BlackboxDump) -> str:
    lines: list[str] = []

    # Header
    lines.append(f"# Blackbox Dump — {dump.collected_at}")
    lines.append(f"# Window: {dump.window_hours}h")
    lines.append("")

    # Summary
    lines.append(_section("SUMMARY"))
    if dump.uptime_kuma:
        if dump.uptime_kuma.error:
            lines.append(f"  Uptime Kuma : ERROR — {dump.uptime_kuma.error}")
        else:
            down = len(dump.uptime_kuma.down_monitors)
            outages = len(dump.uptime_kuma.outages)
            lines.append(f"  Uptime Kuma : {down} monitor(s) DOWN, {outages} recent outage(s)")
    if dump.proxmox:
        if dump.proxmox.error:
            lines.append(f"  Proxmox     : ERROR — {dump.proxmox.error}")
        else:
            lines.append(
                f"  Proxmox     : {len(dump.proxmox.offline_nodes)} offline node(s),"
                f" {len(dump.proxmox.failed_tasks)} failed task(s)"
            )
    if dump.ssh_hosts:
        lines.append(f"  SSH hosts   : {len(dump.ssh_hosts)} host(s) polled")
    if dump.docker_envs:
        total_c = sum(len(e.containers) for e in dump.docker_envs)
        total_o = sum(len(e.outages) for e in dump.docker_envs)
        total_l = sum(len(e.logs) for e in dump.docker_envs)
        lines.append(
            f"  Docker      : {len(dump.docker_envs)} env(s),"
            f" {total_c} container(s), {total_o} outage(s), {total_l} log(s) captured"
        )
    if dump.homeassistant:
        if dump.homeassistant.error:
            lines.append(f"  HA          : ERROR — {dump.homeassistant.error}")
        else:
            lines.append("  HA          : error log captured")
    if dump.prometheus:
        if dump.prometheus.error:
            lines.append(f"  Prometheus  : ERROR — {dump.prometheus.error}")
        else:
            lines.append(f"  Prometheus  : {len(dump.prometheus.firing_alerts)} firing alert(s)")
    if dump.loki:
        if dump.loki.error:
            lines.append(f"  Loki        : ERROR — {dump.loki.error}")
        else:
            lines.append(
                f"  Loki        : {len(dump.loki.syslog_events)} syslog event(s)"
                f" (of {dump.loki.syslog_lines_scanned} scanned),"
                f" {len(dump.loki.pihole_events)} pihole event(s)"
                f" (of {dump.loki.pihole_lines_scanned} scanned)"
            )
    if dump.unifi:
        if dump.unifi.error:
            lines.append(f"  UniFi       : ERROR — {dump.unifi.error}")
        else:
            lines.append(
                f"  UniFi       : {len(dump.unifi.devices)} device(s),"
                f" {len(dump.unifi.recent_reboots)} recent reboot(s),"
                f" {len(dump.unifi.firmware_updates)} firmware update(s),"
                f" {len(dump.unifi.offline_devices)} offline"
            )

    # Uptime Kuma
    if dump.uptime_kuma:
        lines.append(_section("UPTIME KUMA"))
        if dump.uptime_kuma.error:
            lines.append(f"ERROR: {dump.uptime_kuma.error}\n")
        if dump.uptime_kuma.down_monitors:
            lines.append("Current DOWN / degraded monitors:")
            for m in dump.uptime_kuma.down_monitors:
                lines.append(f"  ✗ {m}")
        else:
            lines.append("  All monitors UP.")
        lines.append("")
        lines.append(f"Recent outages (last 10):")
        if dump.uptime_kuma.outages:
            for o in dump.uptime_kuma.outages:
                lines.append(f"  {o}")
        elif dump.uptime_kuma.outage_note:
            lines.append(f"  ({dump.uptime_kuma.outage_note})")
        else:
            lines.append("  No outages recorded.")

    # Proxmox
    if dump.proxmox:
        lines.append(_section("PROXMOX CLUSTER"))
        if dump.proxmox.error:
            lines.append(f"ERROR: {dump.proxmox.error}\n")
        else:
            if dump.proxmox.offline_nodes:
                lines.append("Offline nodes:")
                for n in dump.proxmox.offline_nodes:
                    lines.append(f"  ✗ {n}")
            else:
                lines.append("  All nodes online.")
            lines.append("")
            if dump.proxmox.failed_tasks:
                lines.append(f"Failed tasks in last {dump.window_hours}h:")
                for t in dump.proxmox.failed_tasks:
                    lines.append(f"  {t}")
            else:
                lines.append(f"  No failed tasks in last {dump.window_hours}h.")

    # SSH / journalctl
    for host in dump.ssh_hosts:
        lines.append(_section(f"HOST: {host.name} ({host.host})"))
        if host.error:
            lines.append(f"ERROR: {host.error}")
        elif host.journal_errors:
            lines.append(host.journal_errors)
        else:
            lines.append("  (no output)")

    # Docker via Portainer
    for env in dump.docker_envs:
        lines.append(_section(f"DOCKER ENV: {env.name} (via Portainer)"))
        if env.error:
            lines.append(f"ERROR: {env.error}")
            continue

        if env.outages:
            lines.append(f"Outages in last {dump.window_hours}h:")
            for o in env.outages:
                lines.append(f"  ✗ {o}")
        else:
            lines.append(f"  No container outages in last {dump.window_hours}h.")
        lines.append("")

        if env.containers:
            lines.append("Containers:")
            lines.append(f"  {'NAME':<40} {'STATE':<12} STATUS")
            lines.append(f"  {'-'*40} {'-'*12} {'-'*30}")
            for c in env.containers:
                lines.append(c)
        else:
            lines.append("  No containers found.")

        if env.logs:
            for cname, log_text in env.logs.items():
                lines.append(f"\n--- Logs: {cname} ---")
                lines.append(log_text)

    # Home Assistant
    if dump.homeassistant:
        lines.append(_section("HOME ASSISTANT ERROR LOG"))
        if dump.homeassistant.error:
            lines.append(f"ERROR: {dump.homeassistant.error}")
        else:
            lines.append(dump.homeassistant.error_log or "(empty)")

    # Prometheus
    if dump.prometheus:
        lines.append(_section("PROMETHEUS ALERTS"))
        if dump.prometheus.error:
            lines.append(f"ERROR: {dump.prometheus.error}")
        elif dump.prometheus.firing_alerts:
            for a in dump.prometheus.firing_alerts:
                lines.append(f"  {a}")
        else:
            lines.append("  No firing alerts.")

    # Loki — syslog / pihole
    if dump.loki:
        lines.append(_section("LOKI — SYSLOG / PIHOLE"))
        if dump.loki.error:
            lines.append(f"ERROR: {dump.loki.error}")
        else:
            lines.append(
                f"Syslog events in last {dump.window_hours}h"
                f" ({dump.loki.syslog_lines_scanned} lines scanned):"
            )
            if dump.loki.syslog_events:
                for s in dump.loki.syslog_events:
                    lines.append(f"  {s}")
            else:
                lines.append("  No flagged syslog events.")
            lines.append("")

            lines.append(
                f"Pi-hole events in last {dump.window_hours}h"
                f" ({dump.loki.pihole_lines_scanned} lines scanned):"
            )
            if dump.loki.pihole_events:
                for p in dump.loki.pihole_events:
                    lines.append(f"  {p}")
            else:
                lines.append("  No flagged pihole events.")

    # UniFi Network
    if dump.unifi:
        lines.append(_section("UNIFI NETWORK"))
        if dump.unifi.error:
            lines.append(f"ERROR: {dump.unifi.error}")
        else:
            if dump.unifi.firmware_updates:
                lines.append(f"Firmware updates in last {dump.window_hours}h:")
                for u in dump.unifi.firmware_updates:
                    lines.append(f"  ⬆ {u}")
            else:
                lines.append(f"  No firmware updates in last {dump.window_hours}h.")
            lines.append("")

            if dump.unifi.recent_reboots:
                lines.append(f"Reboots in last {dump.window_hours}h:")
                for r in dump.unifi.recent_reboots:
                    lines.append(f"  ↻ {r}")
            else:
                lines.append(f"  No device reboots in last {dump.window_hours}h.")
            lines.append("")

            if dump.unifi.offline_devices:
                lines.append("Currently offline / not connected:")
                for o in dump.unifi.offline_devices:
                    lines.append(f"  ✗ {o}")
            else:
                lines.append("  All devices online.")
            lines.append("")

            if dump.unifi.devices:
                lines.append("Devices:")
                for d in dump.unifi.devices:
                    lines.append(d)
            else:
                lines.append("  No devices found.")

    lines.append(f"\n{_SEP}")
    lines.append(f"# End of blackbox dump — {dump.collected_at}")
    return "\n".join(lines)
