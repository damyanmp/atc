"""Tests for the UniFi write guard (lib/unifi.py + cli._unifi_apply).

All HTTP goes through httpx.MockTransport — no request can reach a real
controller. Every write helper must be dry-run by default and only send when
dry_run=False (or via a confirmed CLI path).
"""
from __future__ import annotations

import json

import httpx
import pytest

import cli
import lib.unifi as unifi
from config import UniFiConfig
from lib.unifi import UniFiWritePlan

HOST = "controller.example.test"
V1 = f"https://{HOST}:443/proxy/network/integration/v1"
LEGACY = f"https://{HOST}:443/proxy/network"

ZONE = {"id": "zone-1", "name": "Internal", "networkIds": ["net-a"], "metadata": {"origin": "USER"}}
POLICY = {
    "id": "pol-1", "index": 3, "name": "Block IoT", "enabled": True,
    "action": {"type": "BLOCK"}, "metadata": {"origin": "USER_DEFINED"},
}


class Recorder:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET" and not r.url.path.endswith("/login")]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/firewall/zones/zone-1"):
                return httpx.Response(200, json=dict(ZONE))
            if path.endswith("/firewall/policies/pol-1"):
                return httpx.Response(200, json=dict(POLICY))
            return httpx.Response(404, json={})
        if request.method == "DELETE":
            return httpx.Response(200, text="")
        body = json.loads(request.content) if request.content else {}
        return httpx.Response(200, json={"ok": True, "echo": body})


@pytest.fixture
def rec(monkeypatch) -> Recorder:
    r = Recorder()
    monkeypatch.setattr(
        unifi, "_client",
        lambda cfg: httpx.Client(transport=httpx.MockTransport(r.handler), timeout=5),
    )
    return r


@pytest.fixture
def cfg() -> UniFiConfig:
    return UniFiConfig(host=HOST, api_key="test-key")


def _body(req: httpx.Request):
    return json.loads(req.content) if req.content else None


# ── Each helper: (name, call(cfg, dry_run), method, url, body) ───────────────

PF = {"_id": "pf-1", "name": "web", "dst_port": "443", "enabled": True}

CASES = [
    (
        "delete_port_forward",
        lambda c, **kw: unifi.delete_port_forward(c, "default", "pf-1", **kw),
        "DELETE", f"{LEGACY}/api/s/default/rest/portforward/pf-1", None,
    ),
    (
        "update_port_forward",
        lambda c, **kw: unifi.update_port_forward(c, "default", PF, {"enabled": False}, **kw),
        "PUT", f"{LEGACY}/api/s/default/rest/portforward/pf-1", {**PF, "enabled": False},
    ),
    (
        "update_firewall_zone_v1",
        lambda c, **kw: unifi.update_firewall_zone_v1(c, "site-1", "zone-1", {"networkIds": ["net-b"]}, **kw),
        "PUT", f"{V1}/sites/site-1/firewall/zones/zone-1", {"name": "Internal", "networkIds": ["net-b"]},
    ),
    (
        "update_firewall_policy_v1",
        lambda c, **kw: unifi.update_firewall_policy_v1(c, "site-1", "pol-1", {"enabled": False}, **kw),
        "PUT", f"{V1}/sites/site-1/firewall/policies/pol-1",
        {"name": "Block IoT", "enabled": False, "action": {"type": "BLOCK"}},
    ),
    (
        "delete_firewall_policy_v1",
        lambda c, **kw: unifi.delete_firewall_policy_v1(c, "site-1", "pol-1", **kw),
        "DELETE", f"{V1}/sites/site-1/firewall/policies/pol-1", None,
    ),
    (
        "create_firewall_policy_v1",
        lambda c, **kw: unifi.create_firewall_policy_v1(c, "site-1", {"name": "New", "enabled": True}, **kw),
        "POST", f"{V1}/sites/site-1/firewall/policies", {"name": "New", "enabled": True},
    ),
]
IDS = [c[0] for c in CASES]


@pytest.mark.parametrize("name,call,method,url,body", CASES, ids=IDS)
def test_default_is_dry_run_and_sends_nothing(rec, cfg, name, call, method, url, body):
    plan = call(cfg)  # no dry_run argument at all -> must be safe
    assert isinstance(plan, UniFiWritePlan)
    assert rec.writes == []
    assert (plan.method, plan.url, plan.body) == (method, url, body)


@pytest.mark.parametrize("name,call,method,url,body", CASES, ids=IDS)
def test_explicit_dry_run_sends_nothing(rec, cfg, name, call, method, url, body):
    plan = call(cfg, dry_run=True)
    assert isinstance(plan, UniFiWritePlan)
    assert rec.writes == []
    # Preview shows the exact method, endpoint and body.
    text = plan.describe()
    assert text.splitlines()[0] == f"{method} {url}"
    if body is not None:
        assert json.loads(text.split("\n", 1)[1]) == body


@pytest.mark.parametrize("name,call,method,url,body", CASES, ids=IDS)
def test_execute_sends_exactly_expected_request(rec, cfg, name, call, method, url, body):
    call(cfg, dry_run=False)
    assert len(rec.writes) == 1
    req = rec.writes[0]
    assert req.method == method
    assert req.url == httpx.URL(url)
    assert _body(req) == body
    assert req.headers["X-API-KEY"] == "test-key"


def test_dry_run_plan_matches_executed_request(rec, cfg):
    plan = unifi.update_firewall_policy_v1(cfg, "site-1", "pol-1", {"enabled": False})
    unifi.execute_write_plan(cfg, plan)
    (req,) = rec.writes
    assert (req.method, req.url, _body(req)) == (plan.method, httpx.URL(plan.url), plan.body)


def test_legacy_dry_run_does_not_login(rec):
    cfg = UniFiConfig(host=HOST, username="admin", password="pw")
    unifi.delete_port_forward(cfg, "default", "pf-1")
    assert rec.requests == []  # not even the login POST


def test_legacy_execute_logs_in_then_writes(rec):
    cfg = UniFiConfig(host=HOST, username="admin", password="pw")
    unifi.delete_port_forward(cfg, "default", "pf-1", dry_run=False)
    assert [r.url.path for r in rec.requests] == [
        "/api/auth/login", "/proxy/network/api/s/default/rest/portforward/pf-1",
    ]


def test_dry_run_is_keyword_only(cfg):
    with pytest.raises(TypeError):
        unifi.delete_firewall_policy_v1(cfg, "site-1", "pol-1", False)  # type: ignore[misc]


def test_execute_rejects_unknown_api(rec, cfg):
    bad = UniFiWritePlan(method="DELETE", api="other", path="/x", url=f"{V1}/x")
    with pytest.raises(ValueError):
        unifi.execute_write_plan(cfg, bad)
    assert rec.requests == []


# ── CLI guard ────────────────────────────────────────────────────────────────

def _plan(cfg):
    return unifi.delete_firewall_policy_v1(cfg, "site-1", "pol-1")


def test_cli_without_confirmation_non_interactive_sends_nothing(rec, cfg, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli._unifi_apply(cfg, _plan(cfg)) is None
    assert rec.writes == []


def test_cli_interactive_declined_sends_nothing(rec, cfg, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.Confirm, "ask", staticmethod(lambda *a, **k: False))
    assert cli._unifi_apply(cfg, _plan(cfg)) is None
    assert rec.writes == []


def test_cli_dry_run_sends_nothing_even_with_yes(rec, cfg):
    assert cli._unifi_apply(cfg, _plan(cfg), yes=True, dry_run=True) is None
    assert rec.writes == []


def test_cli_yes_sends_exactly_the_plan(rec, cfg, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    cli._unifi_apply(cfg, _plan(cfg), yes=True)
    (req,) = rec.writes
    assert (req.method, req.url) == ("DELETE", httpx.URL(f"{V1}/sites/site-1/firewall/policies/pol-1"))


def test_cli_interactive_confirmed_sends(rec, cfg, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.Confirm, "ask", staticmethod(lambda *a, **k: True))
    cli._unifi_apply(cfg, _plan(cfg))
    assert len(rec.writes) == 1
