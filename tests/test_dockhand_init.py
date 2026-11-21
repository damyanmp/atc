"""Tests for the `atc init dockhand` pieces: section spec, environment builder, agent command."""
from __future__ import annotations

import pytest

from lib import dockhand as dh
from lib.config_sections import (
    DOCKHAND_ENV_KINDS,
    build_dockhand_environment,
    build_scalar_section,
    section_by_key,
    validate_env_name,
)


def test_dockhand_section_matches_config_example():
    spec = section_by_key("dockhand")
    assert spec is not None
    names = [f.name for f in spec.fields]
    assert names == ["host", "port", "https", "api_token", "verify_ssl"]
    tok = next(f for f in spec.fields if f.name == "api_token")
    assert tok.secret and tok.secret_key == "dockhand-api-token"  # same key as config.yaml.example
    out = build_scalar_section(spec, {"host": "h", "port": 3000, "https": False,
                                      "api_token": "keychain:dockhand-api-token", "verify_ssl": False})
    assert out["api_token"] == "keychain:dockhand-api-token"


def test_env_kinds_are_dockhand_connection_types():
    assert [k.key for k in DOCKHAND_ENV_KINDS] == ["hawser-standard", "hawser-edge", "socket", "direct"]


@pytest.mark.parametrize("kind,answers,expected", [
    ("socket", {"name": "docker-1"}, {"name": "docker-1", "connectionType": "socket",
                                      "socketPath": "/var/run/docker.sock"}),
    ("hawser-standard", {"name": "nas", "host": "10.0.0.5", "hawser_token": "t"},
     {"name": "nas", "connectionType": "hawser-standard", "host": "10.0.0.5", "port": 2376,
      "protocol": "http", "hawserToken": "t"}),
    ("hawser-edge", {"name": "vps"}, {"name": "vps", "connectionType": "hawser-edge"}),
    ("direct", {"name": "old", "host": "h", "tls": True, "tls_skip_verify": True},
     {"name": "old", "connectionType": "direct", "host": "h", "port": 2375, "protocol": "https",
      "tlsSkipVerify": True}),
])
def test_build_environment(kind, answers, expected):
    assert build_dockhand_environment(kind, answers) == expected


@pytest.mark.parametrize("kind,answers", [
    ("bogus", {"name": "x"}),
    ("socket", {"name": ""}),
    ("direct", {"name": "x"}),
    ("hawser-standard", {"name": "x", "host": "h"}),  # no token
])
def test_build_environment_rejects(kind, answers):
    with pytest.raises(ValueError):
        build_dockhand_environment(kind, answers)


def test_validate_env_name():
    assert validate_env_name("docker-2", ["docker-1"]) == ""
    assert "already exists" in validate_env_name("Docker-1", ["docker-1"])
    assert validate_env_name("-bad", [])
    assert validate_env_name("", [])


def test_hawser_commands():
    std = dh.hawser_run_command("hawser-standard", "tok")
    edge = dh.hawser_run_command("hawser-edge", "tok", "wss://dh:3000/api/hawser/connect")
    assert "-p 2376:2376" in std and "DOCKHAND_SERVER_URL" not in std
    assert "DOCKHAND_SERVER_URL=wss://dh:3000/api/hawser/connect" in edge and "-p " not in edge
    for cmd in (std, edge):
        assert "--label dockhand.update=false" in cmd and "-e TOKEN=tok" in cmd


def test_edge_url_scheme():
    from config import DockhandConfig
    assert dh.edge_connect_url(DockhandConfig(host="h", port=3000, api_token="x")) == "ws://h:3000/api/hawser/connect"
    assert dh.edge_connect_url(DockhandConfig(host="h", port=443, https=True, api_token="x")).startswith("wss://")
