"""Tests for `atc acars -o json` serialization (_acars_to_json)."""
import json

from cli import _acars_to_json


def test_empty_is_all_clear():
    out = _acars_to_json([], [], ["proxmox", "unifi"])
    assert out == {
        "summary": {"total": 0, "crit": 0, "warn": 0, "sources_checked": ["proxmox", "unifi"]},
        "alerts": [],
        "source_errors": [],
    }


def test_counts_and_crit_first_ordering():
    alerts = [
        ("WARN", "TLS Certs", "example.test — 20d remaining"),
        ("CRIT", "Uptime Kuma", "Service A — DOWN (24h: 90%)"),
        ("WARN", "Beszel", "host-1 — Disk 88%"),
        ("CRIT", "Proxmox", "Node node-1 — offline"),
    ]
    out = _acars_to_json(alerts, [], ["beszel", "certs", "proxmox", "uk"])
    assert out["summary"]["total"] == 4
    assert out["summary"]["crit"] == 2
    assert out["summary"]["warn"] == 2
    sev_src = [(a["severity"], a["source"]) for a in out["alerts"]]
    assert sev_src == [
        ("CRIT", "Proxmox"),
        ("CRIT", "Uptime Kuma"),
        ("WARN", "Beszel"),
        ("WARN", "TLS Certs"),
    ]


def test_source_errors_passthrough_and_json_serializable():
    out = _acars_to_json(
        [("CRIT", "UniFi", "switch-1 Port 3 — rx_errors=1")],
        ["Loki: timed out"],
        ["loki", "unifi"],
    )
    assert out["source_errors"] == ["Loki: timed out"]
    # Round-trips cleanly as JSON (what the CLI actually prints)
    assert json.loads(json.dumps(out)) == out
