"""acars alerts on recent change: UniFi port counter deltas over a rolling
window (lib.acars_state) and Pi-hole FTL message age."""
import json

import pytest

import cli
from lib import acars_state
from lib.unifi import UniFiDevice

T0 = 1_800_000_000.0
HOUR = 3600
WINDOW_S = cli._ACARS_PORT_WINDOW_HOURS * HOUR
MAC = "00:00:5e:00:53:01"


def _switch(link_down=0, rx=0, tx=0, uptime=10 * 86400, up=True, name="switch-1", port_idx=14):
    return UniFiDevice(
        name=name, model="USW", type="usw", state=1, version="1.0",
        previous_firmware_version="", uptime=uptime, startup_timestamp=0,
        upgrade_duration=0, uplink_device_name="",
        raw={"mac": MAC, "port_table": [{
            "port_idx": port_idx, "enable": True, "up": up,
            "link_down_count": link_down, "rx_errors": rx, "tx_errors": tx,
        }]},
    )


class _Cfg:
    site = "default"


@pytest.fixture
def run(tmp_path, monkeypatch):
    """run(device, now) -> alerts, with fetch_devices mocked and state in tmp_path."""
    state_file = tmp_path / "acars_state.json"

    def _run(device, now):
        monkeypatch.setattr("lib.unifi.fetch_devices", lambda cfg, site="default": [device])
        return cli._acars_fetch_unifi(_Cfg(), state_file, now=now)

    _run.state_file = state_file
    return _run


# ── UniFi port deltas ─────────────────────────────────────────────────────────

def test_first_run_is_baseline_only(run):
    # Big lifetime totals, no history: no CRIT on the very first run.
    assert run(_switch(link_down=40, rx=500, tx=7), T0) == []
    assert run.state_file.exists()


def test_unchanged_counters_no_alert(run):
    run(_switch(link_down=40, rx=500), T0)
    assert run(_switch(link_down=40, rx=500, uptime=10 * 86400 + HOUR), T0 + HOUR) == []


def test_linkdown_delta_under_warn(run):
    run(_switch(link_down=10), T0)
    up = 10 * 86400 + HOUR
    assert run(_switch(link_down=10 + cli._ACARS_PORT_LINKDOWN_WARN - 1, uptime=up), T0 + HOUR) == []


def test_linkdown_delta_warn(run):
    run(_switch(link_down=10), T0)
    up = 10 * 86400 + HOUR
    alerts = run(_switch(link_down=10 + cli._ACARS_PORT_LINKDOWN_WARN, uptime=up), T0 + HOUR)
    assert alerts == [{
        "text": f"switch-1 Port 14 — {cli._ACARS_PORT_LINKDOWN_WARN} link-down events in last 24h",
        "severity": "WARN",
    }]


def test_linkdown_delta_crit(run):
    run(_switch(link_down=10), T0)
    up = 10 * 86400 + HOUR
    alerts = run(_switch(link_down=10 + cli._ACARS_PORT_LINKDOWN_CRIT, uptime=up), T0 + HOUR)
    assert [a["severity"] for a in alerts] == ["CRIT"]
    assert f"{cli._ACARS_PORT_LINKDOWN_CRIT} link-down events in last 24h" in alerts[0]["text"]


def test_error_delta_crit(run):
    run(_switch(rx=100, tx=5), T0)
    alerts = run(_switch(rx=101, tx=5, uptime=10 * 86400 + HOUR), T0 + HOUR)
    assert alerts == [{"text": "switch-1 Port 14 — rx_errors +1 tx_errors +0 in last 24h", "severity": "CRIT"}]


def test_down_port_recorded_but_not_alerted(run):
    run(_switch(rx=0, up=False), T0)
    assert run(_switch(rx=9, up=False, uptime=10 * 86400 + HOUR), T0 + HOUR) == []
    # Comes back up: its baseline already exists, so the new errors since then count.
    alerts = run(_switch(rx=12, uptime=10 * 86400 + 2 * HOUR), T0 + 2 * HOUR)
    assert alerts and "rx_errors +12" in alerts[0]["text"]


def test_counter_decrease_rebaselines(run):
    run(_switch(link_down=50), T0)
    # Counter went down (uptime unchanged-ish): treated as reset, no alert.
    assert run(_switch(link_down=2, uptime=10 * 86400 + HOUR), T0 + HOUR) == []
    # Next delta is measured from the new baseline (2), not the old one.
    alerts = run(_switch(link_down=2 + cli._ACARS_PORT_LINKDOWN_WARN, uptime=10 * 86400 + 2 * HOUR), T0 + 2 * HOUR)
    assert [a["severity"] for a in alerts] == ["WARN"]
    samples = json.loads(run.state_file.read_text())["switches"][MAC]["samples"]
    assert [s["ports"]["14"][0] for s in samples] == [2, 2 + cli._ACARS_PORT_LINKDOWN_WARN]


def test_uptime_drop_rebaselines(run):
    run(_switch(link_down=0), T0)
    # Rebooted 2h ago (outside the 15-min device-reboot WARN) and already flapped
    # past CRIT — but it's a fresh boot, so history is dropped: baseline only.
    assert run(_switch(link_down=cli._ACARS_PORT_LINKDOWN_CRIT, uptime=2 * HOUR), T0 + 3 * HOUR) == []
    samples = json.loads(run.state_file.read_text())["switches"][MAC]["samples"]
    assert len(samples) == 1 and samples[0]["uptime"] == 2 * HOUR


def test_reboot_with_long_gap_rebaselines(run):
    # Uptime is higher than before, but boot time moved: still a reboot.
    run(_switch(link_down=0, uptime=HOUR), T0)
    assert run(_switch(link_down=cli._ACARS_PORT_LINKDOWN_CRIT, uptime=2 * HOUR), T0 + 6 * HOUR) == []


def test_samples_older_than_window_pruned_and_not_counted(run):
    run(_switch(link_down=0), T0)
    up0 = 10 * 86400
    # Within window: 5 flaps → WARN.
    assert run(_switch(link_down=5, uptime=up0 + 12 * HOUR), T0 + 12 * HOUR)
    # Past the window from T0: T0 sample is pruned, baseline is the 12h one (5).
    later = T0 + WINDOW_S + HOUR
    assert run(_switch(link_down=6, uptime=up0 + int(later - T0)), later) == []
    samples = json.loads(run.state_file.read_text())["switches"][MAC]["samples"]
    assert all(s["ts"] >= later - WINDOW_S for s in samples)
    assert T0 not in [s["ts"] for s in samples]


def test_switch_gone_is_pruned(tmp_path):
    state = {"switches": {MAC: {"samples": [{"ts": T0, "uptime": 1, "ports": {}}]}}}
    acars_state.prune(state, T0 + WINDOW_S + 1, WINDOW_S)
    assert state["switches"] == {}


def test_frequent_runs_keep_baseline_and_bound_samples():
    state: dict = {"switches": {}}
    for i in range(200):  # one run per minute for 200 minutes
        acars_state.record_and_diff(state, MAC, T0 + i * 60, 10_000 + i * 60, {"1": (i // 50, 0, 0)}, WINDOW_S)
    samples = state["switches"][MAC]["samples"]
    assert samples[0]["ts"] == T0                         # baseline kept
    assert len(samples) < 60                              # thinned, not one per run
    d = acars_state.record_and_diff(state, MAC, T0 + 200 * 60, 10_000 + 200 * 60, {"1": (4, 0, 0)}, WINDOW_S)
    assert d == {"1": (4, 0, 0)}


@pytest.mark.parametrize("content", ["{not json", '{"switches": []}', '["x"]', ""])
def test_corrupt_state_file_doesnt_crash(run, capsys, content):
    run.state_file.write_text(content)
    assert run(_switch(link_down=40, rx=3), T0) == []
    assert "acars_state.json" in capsys.readouterr().err
    # And it was rewritten as valid state.
    assert MAC in json.loads(run.state_file.read_text())["switches"]


def test_unwritable_state_dir_doesnt_crash(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("lib.unifi.fetch_devices", lambda cfg, site="default": [_switch(rx=3)])
    missing = tmp_path / "no-such-dir" / "acars_state.json"
    assert cli._acars_fetch_unifi(_Cfg(), missing, now=T0) == []
    assert "can't write" in capsys.readouterr().err


def test_no_state_file_means_no_port_alerts(monkeypatch):
    monkeypatch.setattr("lib.unifi.fetch_devices", lambda cfg, site="default": [_switch(link_down=99, rx=99)])
    assert cli._acars_fetch_unifi(_Cfg(), None, now=T0) == []


def test_offline_device_check_unchanged(run):
    d = _switch()
    d.state = 0
    assert run(d, T0) == [{"text": "switch-1 (USW) — offline (state=0)", "severity": "CRIT"}]


def test_state_path_follows_config_dir(tmp_path):
    assert acars_state.state_path(tmp_path / "config.yaml") == tmp_path / "acars_state.json"


# ── Pi-hole message age ───────────────────────────────────────────────────────

def _pihole(monkeypatch, msgs, now=T0):
    async def _fake(cfgs):
        return [("pihole-1", msgs)]
    monkeypatch.setattr("lib.pihole.fetch_messages_all", _fake)
    return cli._acars_fetch_pihole_messages([object()], now=now)


@pytest.mark.parametrize("age_s, label", [
    (5, "<1m ago"), (5 * 60, "5m ago"), (2 * HOUR + 59 * 60, "2h ago"),
    (3 * 86400, "3d ago"), (-30, "<1m ago"),
])
def test_age_label(age_s, label):
    assert cli._acars_age_label(age_s) == label


def test_message_text_has_age(monkeypatch):
    out = _pihole(monkeypatch, [{"type": "RATE_LIMIT", "plain": "Client rate-limited", "timestamp": T0 - 2 * HOUR}])
    assert out == [{"text": "pihole-1: [RATE_LIMIT] Client rate-limited (2h ago)", "severity": "WARN"}]


def test_old_message_dropped(monkeypatch):
    old = T0 - cli._ACARS_PIHOLE_MSG_MAX_AGE_HOURS * HOUR - 1
    assert _pihole(monkeypatch, [{"type": "RATE_LIMIT", "plain": "x", "timestamp": old}]) == []


@pytest.mark.parametrize("mtype", ["DISK_MESSAGE", "DISK_EXTENDED"])
def test_old_disk_message_kept(monkeypatch, mtype):
    out = _pihole(monkeypatch, [{"type": mtype, "plain": "disk 95% full", "timestamp": T0 - 3 * 86400}])
    assert len(out) == 1 and out[0]["text"].endswith("(3d ago)")


@pytest.mark.parametrize("ts", [None, "garbage", 0, True])
def test_missing_or_invalid_timestamp_kept(monkeypatch, ts):
    msg = {"type": "LOAD", "plain": "load high"}
    if ts is not None:
        msg["timestamp"] = ts
    out = _pihole(monkeypatch, [msg])
    assert out == [{"text": "pihole-1: [LOAD] load high (age unknown)", "severity": "WARN"}]
