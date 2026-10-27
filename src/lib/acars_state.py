"""Persistent per-port counter history for `atc acars`.

UniFi switch port counters (link_down_count, rx_errors, tx_errors) are
cumulative since the switch's own last boot, and the controller has no reset
endpoint. To alert on *recent change* rather than lifetime totals, acars keeps
a short rolling history of timestamped samples per switch and compares the
current counters against the oldest sample still inside the window.

State is persisted to <config_dir>/acars_state.json:

  {"version": 1,
   "switches": {
     "<switch mac>": {"samples": [
       {"ts": <epoch s>, "uptime": <s>, "ports": {"<port_idx>": [link_down, rx_errors, tx_errors]}},
       ...]}}}

Load/save never raise: a missing, unreadable or corrupt file means "no
history" (one line on stderr), and a failed write is reported the same way.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_STATE_VERSION = 1

# Don't store a new sample if the last one is younger than this — bounds the
# file size when acars runs every minute from a monitoring wrapper. Deltas are
# measured against the oldest sample in the window, so thinning loses nothing.
_MIN_SAMPLE_INTERVAL_S = 300

# Boot time (ts - uptime) moving later by more than this between samples means
# the switch rebooted, even if the run gap was long enough that the new uptime
# is already higher than the previous one.
_BOOT_SHIFT_SLACK_S = 300

PortCounters = tuple[int, int, int]   # (link_down, rx_errors, tx_errors)


def state_path(config_path: Path) -> Path:
    """acars state file, adjacent to config.yaml (same rule as search_state)."""
    return config_path.parent / "acars_state.json"


def _warn(msg: str) -> None:
    print(f"atc acars: {msg}", file=sys.stderr)


def load_state(path: Path) -> dict:
    """Return the state dict, or an empty one if there is no usable file."""
    empty: dict = {"version": _STATE_VERSION, "switches": {}}
    try:
        text = path.read_text()
    except FileNotFoundError:
        return empty
    except OSError as e:
        _warn(f"can't read {path.name} ({e}); port deltas start from scratch")
        return empty
    try:
        raw = json.loads(text)
        switches = raw["switches"]
        if not isinstance(switches, dict):
            raise ValueError("'switches' is not an object")
        for entry in switches.values():
            if not isinstance(entry.get("samples"), list):
                raise ValueError("switch entry without a 'samples' list")
    except Exception as e:
        _warn(f"ignoring corrupt {path.name} ({e}); port deltas start from scratch")
        return empty
    return {"version": _STATE_VERSION, "switches": switches}


def save_state(path: Path, state: dict) -> None:
    """Atomic write (temp file in the same dir + rename). Never raises."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, separators=(",", ":"))
        os.replace(tmp, path)
        tmp = None
    except Exception as e:
        _warn(f"can't write {path.name} ({e}); port history not saved")
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _as_counters(v) -> PortCounters | None:
    try:
        a, b, c = v
        return int(a), int(b), int(c)
    except Exception:
        return None


def _valid_sample(s) -> bool:
    return (
        isinstance(s, dict)
        and isinstance(s.get("ts"), (int, float))
        and isinstance(s.get("ports"), dict)
    )


def prune(state: dict, now: float, window_s: int) -> None:
    """Drop samples older than the window, and switches left with none."""
    cutoff = now - window_s
    switches = state.setdefault("switches", {})
    for key in list(switches):
        samples = [s for s in switches[key].get("samples", []) if _valid_sample(s) and s["ts"] >= cutoff]
        if samples:
            switches[key]["samples"] = samples
        else:
            del switches[key]


def _is_reset(last: dict, now: float, uptime: int, ports: dict[str, PortCounters]) -> bool:
    last_uptime = int(last.get("uptime", 0) or 0)
    if uptime and last_uptime:
        if uptime < last_uptime:
            return True
        if (now - uptime) - (last["ts"] - last_uptime) > _BOOT_SHIFT_SLACK_S:
            return True
    for idx, cur in ports.items():
        prev = _as_counters(last["ports"].get(idx))
        if prev is not None and any(c < p for c, p in zip(cur, prev, strict=True)):
            return True
    return False


def record_and_diff(
    state: dict,
    switch_key: str,
    now: float,
    uptime: int,
    ports: dict[str, PortCounters],
    window_s: int,
) -> dict[str, PortCounters]:
    """Record this run's counters for one switch and return, per port_idx,
    the increase since the oldest in-window sample that has that port.

    Ports with no prior in-window sample are absent from the result (first
    run = baseline only). A counter going down or the switch's uptime going
    backwards is a reboot: the switch's history is dropped and this sample
    becomes the new baseline (empty result). Call prune() first.
    """
    entry = state.setdefault("switches", {}).setdefault(switch_key, {"samples": []})
    samples = [s for s in entry.get("samples", []) if _valid_sample(s)]

    if samples and _is_reset(samples[-1], now, uptime, ports):
        samples = []

    deltas: dict[str, PortCounters] = {}
    for idx, cur in ports.items():
        for s in samples:
            base = _as_counters(s["ports"].get(idx))
            if base is not None:
                deltas[idx] = (cur[0] - base[0], cur[1] - base[1], cur[2] - base[2])
                break

    sample = {"ts": int(now), "uptime": int(uptime or 0), "ports": {k: list(v) for k, v in ports.items()}}
    if len(samples) >= 2 and now - samples[-2]["ts"] < _MIN_SAMPLE_INTERVAL_S:
        # Too soon after the last kept sample: overwrite the newest one (so
        # reset detection still compares against the latest counters) instead
        # of growing the list. Samples stay roughly _MIN_SAMPLE_INTERVAL_S apart.
        samples[-1] = sample
    else:
        samples.append(sample)
    entry["samples"] = samples
    return deltas
