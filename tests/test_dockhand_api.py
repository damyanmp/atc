"""lib/dockhand.py HTTP-layer tests against response shapes captured from a live Dockhand v1.0.58.

The JSON fixtures below are trimmed copies of real responses recorded during the spike
(spike/dockhand/), so a field rename in Dockhand shows up here as a failing test.
"""
from __future__ import annotations

import io
import json
import tarfile

import httpx
import pytest

from config import DockhandConfig
from lib import dockhand as dh
from lib.portainer import outages_from_events

CFG = DockhandConfig(host="dh.test", port=3000, api_token="dh_test")

CONTAINER_ROW = {
    "id": "3216396115feb4357a33", "name": "atc-spike-shapes-w-1", "image": "busybox:1.36",
    "imageId": "sha256:73aa", "state": "running", "status": "Up 12 seconds", "created": 1795274307,
    "restartCount": 0, "health": "none",
    "labels": {"com.docker.compose.project": "atc-spike-shapes", "com.docker.compose.service": "w"},
}
STACK_ROW = {"name": "atc-spike-shapes", "containers": ["3216396115feb4357a33"], "status": "running",
             "health": "none", "sourceType": "internal", "updatesAvailable": False}
ACTIVITY = {"events": [
    {"id": 2, "environmentId": 2, "containerId": "c1", "containerName": "w-1", "action": "die",
     "timestamp": "2026-11-21T15:18:40.000Z", "actorAttributes": {"exitCode": "137"}},
    {"id": 3, "environmentId": 2, "containerId": "c1", "containerName": "w-1", "action": "start",
     "timestamp": "2026-11-21T15:20:40.000Z", "actorAttributes": {}},
], "total": 2, "limit": 500, "offset": 0}


@pytest.fixture
def server(monkeypatch):
    """Route lib.dockhand's httpx clients to a handler; returns the list of seen requests."""
    seen: list[httpx.Request] = []
    routes: dict[tuple[str, str], object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        resp = routes.get((req.method, req.url.path))
        if callable(resp):
            resp = resp(req)
        if resp is None:
            return httpx.Response(404, json={"error": "no route"})
        return resp if isinstance(resp, httpx.Response) else httpx.Response(200, json=resp)

    def fake_client(cfg, timeout=15):
        return httpx.Client(base_url=cfg.base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(dh, "_client", fake_client)
    return seen, routes


def test_containers_parse_and_normalise_health(server):
    seen, routes = server
    routes[("GET", "/api/containers")] = [CONTAINER_ROW]
    [c] = dh.fetch_containers(CFG, 2, "docker-2")
    assert (c.name, c.state, c.status, c.health, c.compose_project) == (
        "atc-spike-shapes-w-1", "running", "Up 12 seconds", "", "atc-spike-shapes")
    assert seen[0].url.params["env"] == "2" and seen[0].url.params["all"] == "true"
    assert seen[0].headers["authorization"] == "Bearer dh_test"


def test_logs_unwraps_json_and_sends_absolute_since(server):
    seen, routes = server
    routes[("GET", "/api/containers/abc/logs")] = {"logs": "2026-11-21T15:18:39Z line 12\n"}
    out = dh.fetch_container_logs(CFG, 2, "abc", since_seconds=60, tail=5)
    assert out == "2026-11-21T15:18:39Z line 12"
    p = seen[0].url.params
    assert p["tail"] == "5" and int(p["since"]) > 1_700_000_000 and p["env"] == "2"


def test_stacks_across_envs(server):
    _, routes = server
    routes[("GET", "/api/stacks")] = [STACK_ROW]
    envs = [dh.DockhandEnvironment(1, "docker-1"), dh.DockhandEnvironment(2, "docker-2")]
    ss = dh.fetch_stacks(CFG, envs)
    assert [(s.env_name, s.name, s.status, s.source_type, s.containers) for s in ss] == [
        ("docker-1", "atc-spike-shapes", "running", "internal", 1),
        ("docker-2", "atc-spike-shapes", "running", "internal", 1)]


def test_stack_env_mapped_to_portainer_shape(server):
    _, routes = server
    routes[("GET", "/api/stacks/app/env")] = {"variables": [{"key": "A", "value": "1", "isSecret": False}],
                                              "injectedSecretKeys": [], "secretProvider": None}
    assert dh.fetch_stack_env(CFG, 1, "app") == [{"name": "A", "value": "1"}]


def test_in_band_failure_raises(server):
    _, routes = server
    routes[("POST", "/api/stacks")] = {"success": False, "error": "failed to parse compose.yaml"}
    with pytest.raises(RuntimeError, match="failed to parse"):
        dh.create_stack(CFG, 1, "bad", "x")


def test_create_stack_sends_sync_header_and_dh_env_shape(server):
    seen, routes = server
    routes[("POST", "/api/stacks")] = {"success": True}
    dh.create_stack(CFG, 1, "app", "services: {}", [{"name": "A", "value": "1"}])
    body = json.loads(seen[0].content)
    assert seen[0].headers["accept"] == "application/json"
    assert body["envVars"] == [{"key": "A", "value": "1"}] and body["start"] is True


def test_update_stack_without_env_does_not_touch_env(server):
    seen, routes = server
    routes[("PUT", "/api/stacks/app/compose")] = {"success": True}
    dh.update_stack(CFG, 1, "app", "services: {}")
    assert [r.url.path for r in seen] == ["/api/stacks/app/compose"]


def test_update_stack_env_goes_to_env_file_before_redeploy(server):
    # The JSON /env endpoint only writes the DB; internal stacks read their .env file.
    seen, routes = server
    routes[("PUT", "/api/stacks/app/env/raw")] = {"success": True}
    routes[("PUT", "/api/stacks/app/compose")] = {"success": True}
    dh.update_stack(CFG, 1, "app", "services: {}", [{"name": "A", "value": "1"}, {"name": "B", "value": "x=y"}])
    assert [r.url.path for r in seen] == ["/api/stacks/app/env/raw", "/api/stacks/app/compose"]
    assert json.loads(seen[0].content) == {"content": "A=1\nB=x=y\n"}
    assert json.loads(seen[1].content) == {"content": "services: {}", "restart": True}


def test_update_stack_empty_env_needs_force():
    with pytest.raises(ValueError):
        dh.update_stack(CFG, 1, "app", "x", [])


def test_prune_images_result(server):
    _, routes = server
    routes[("POST", "/api/prune/images")] = {"success": True, "result": {
        "ImagesDeleted": [{"Deleted": "sha256:a"}, {"Untagged": "b"}], "SpaceReclaimed": 1234}}
    assert dh.prune_images(CFG, 1) == (2, 1234)


def test_events_feed_outage_reconstruction(server):
    seen, routes = server
    routes[("GET", "/api/activity")] = ACTIVITY
    ev = dh.fetch_container_events(CFG, 2, since_seconds=3600)
    assert seen[0].url.params["actions"] == "start,stop,die,kill,oom"
    [o] = outages_from_events("docker-2", ev)
    assert (o.container_name, o.duration_min, o.exit_code) == ("w-1", 2, "137")


def test_upload_unpacks_tar_into_dirs(server):
    seen, routes = server
    routes[("POST", "/api/containers/web/files/create")] = {"success": True}
    routes[("POST", "/api/containers/web/files/upload")] = {"uploaded": ["x"], "errors": []}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in (("slug/index.html", b"<h1/>"), ("slug/img/a.svg", b"<svg/>")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    dh.upload_to_container(CFG, 1, "web", buf.getvalue(), "/srv/")
    mk = [json.loads(r.content)["path"] for r in seen if r.url.path.endswith("/create")]
    up = [r.url.params["path"] for r in seen if r.url.path.endswith("/upload")]
    assert mk == ["/srv/slug", "/srv/slug/img"]
    assert sorted(up) == ["/srv/slug", "/srv/slug/img"]
