"""Dockhand REST API client — Portainer replacement candidate (https://dockhand.pro).

Field names and behaviour VERIFIED against a live Dockhand v1.0.58 (OpenAPI 3.0.3 spec served
at /api/docs when FEAT_API_DOCS=true, 289 paths) on 2026-11-21, on both a local-socket
environment and a Hawser standard-mode agent. See spike/dockhand/ for the sandbox, the
contract probe and its results.

Auth: user-scoped bearer token ("dh_..."), created in the UI (Profile → API tokens) or via
POST /api/auth/tokens with a session + password.

Mirrors lib/portainer.py's public functions (same names, same return types where possible)
so cli.py can switch backends with a small diff. Differences that callers must handle:
  - stacks are addressed by NAME + env, not by a numeric id;
  - stack env vars are a separate resource (PUT /api/stacks/{name}/env), so update_stack
    never wipes them by accident the way Portainer's PUT did;
  - container events come from Dockhand's own persisted activity log, not the Docker
    daemon's in-memory buffer (survives daemon restarts; only covers time Dockhand has been
    running with collectActivity on).
"""
from __future__ import annotations

import io
import re
import tarfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from config import DockhandConfig


@dataclass
class DockhandEnvironment:
    id: int
    name: str
    connection_type: str = ""   # "socket" | "hawser-standard" | "hawser-edge" | "direct"
    host: str = ""


@dataclass
class DockhandContainer:
    id: str
    name: str
    image: str
    state: str    # "running", "exited", "restarting", "paused", etc.
    status: str   # human string e.g. "Up 2 hours", "Exited (1) 5 minutes ago"
    health: str   # "healthy", "unhealthy", "starting", "" if no healthcheck
    env_id: int
    env_name: str
    labels: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.labels is None:
            self.labels = {}

    @property
    def compose_project(self) -> str:
        """The docker-compose project name, or '' if not a Compose container."""
        return self.labels.get("com.docker.compose.project", "")

    @property
    def needs_attention(self) -> bool:
        """True for containers in a bad state (exited, dead, restarting, unhealthy)."""
        if self.state in ("exited", "dead", "restarting"):
            return True
        if self.health in ("unhealthy", "starting"):
            return True
        return False

    def started_within(self, seconds: int) -> bool:
        """True if the container started (or restarted) within the last `seconds`."""
        m = re.match(r"Up (\d+)\s+(second|minute|hour)", self.status, re.IGNORECASE)
        if not m:
            return False
        n, unit = int(m.group(1)), m.group(2).lower()
        uptime_sec = n * {"second": 1, "minute": 60, "hour": 3600}[unit]
        return uptime_sec <= seconds


@dataclass
class DockhandStack:
    name: str          # stacks are addressed by name + env, there is no numeric id
    status: str        # "running" | "partial" | "stopped" | ...
    env_id: int
    env_name: str
    source_type: str = ""   # "internal" (Dockhand-created) | "external" (adopted) | "git"
    containers: int = 0


@dataclass
class DockhandImage:
    id: str
    tags: list[str]
    size: int
    created: int
    env_id: int
    env_name: str

    @property
    def dangling(self) -> bool:
        return not self.tags

    @property
    def display_name(self) -> str:
        return self.tags[0] if self.tags else f"<none> ({self.id[:12]})"


@dataclass
class DockhandVolume:
    name: str
    driver: str
    mountpoint: str
    created: str
    env_id: int
    env_name: str


# ── plumbing ─────────────────────────────────────────────────────────────────

def _headers(cfg: DockhandConfig, sync: bool = False) -> dict:
    h = {"Authorization": f"Bearer {cfg.api_token}"}
    if sync:
        # Long-running endpoints (stack deploy/start/stop, prune) stream SSE or return a
        # jobId for the UI; Accept: application/json makes them run to completion and
        # return the result in the response body instead.
        h["Accept"] = "application/json"
    return h


def _client(cfg: DockhandConfig, timeout: float = 15) -> httpx.Client:
    return httpx.Client(base_url=cfg.base_url, verify=cfg.verify_ssl, timeout=timeout)


def _check(r: httpx.Response, what: str) -> httpx.Response:
    """Raise on HTTP errors AND on Dockhand's in-band failures.

    Synchronous operations (stack create/deploy/update, prune) answer HTTP 200 with
    {"success": false, "error": "..."} when compose itself fails (bad YAML, pull error,
    port clash). Portainer used 4xx/5xx for these, so a status-code check alone would
    report a failed deploy as success.
    """
    if not r.is_success:
        raise RuntimeError(f"Dockhand {what} HTTP {r.status_code}: {r.text[:300]}")
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            body = r.json()
        except ValueError:
            return r
        if isinstance(body, dict) and body.get("success") is False:
            raise RuntimeError(f"Dockhand {what} failed: {str(body.get('error') or body)[:400]}")
    return r


def health_check(cfg: DockhandConfig) -> bool:
    """GET /api/health — True if Dockhand is reachable and healthy."""
    with _client(cfg, timeout=5) as client:
        return client.get("/api/health", headers=_headers(cfg)).is_success


# ── environments & containers ───────────────────────────────────────────────

def fetch_environments(cfg: DockhandConfig) -> list[DockhandEnvironment]:
    """All Docker environments registered in Dockhand (GET /api/environments)."""
    with _client(cfg) as client:
        r = _check(client.get("/api/environments", headers=_headers(cfg)), "environments")
    return [
        DockhandEnvironment(
            id=e["id"],
            name=e.get("name", str(e["id"])),
            connection_type=e.get("connectionType", ""),
            host=e.get("host") or e.get("socketPath") or "",
        )
        for e in r.json()
    ]


def fetch_containers(cfg: DockhandConfig, env_id: int, env_name: str) -> list[DockhandContainer]:
    """All containers (running and stopped) in one environment (GET /api/containers?env=&all=true).

    Containers labelled dockhand.hidden=true are filtered out server-side.
    """
    with _client(cfg) as client:
        r = _check(client.get("/api/containers", headers=_headers(cfg),
                              params={"env": env_id, "all": "true"}), f"containers (env {env_id})")
    out = []
    for c in r.json():
        health = c.get("health") or ""
        out.append(DockhandContainer(
            id=c.get("id", ""),
            name=c.get("name") or c.get("id", "")[:12],
            image=c.get("image", ""),
            state=c.get("state", ""),
            status=c.get("status", ""),
            health="" if health == "none" else health,
            env_id=env_id,
            env_name=env_name,
            labels=c.get("labels") or {},
        ))
    return out


def container_action(cfg: DockhandConfig, env_id: int, container_id: str, action: str) -> None:
    """start/stop/restart/pause/unpause a container. Same signature as portainer.container_action."""
    with _client(cfg, timeout=60) as client:
        _check(client.post(f"/api/containers/{container_id}/{action}", headers=_headers(cfg),
                           params={"env": env_id}), f"container {action} ({container_id[:12]})")


def log_query_params(since_seconds: int, tail: int, now: float | None = None) -> dict:
    """Same contract as portainer.log_query_params: `since` is an absolute Unix timestamp."""
    params: dict = {"tail": max(1, int(tail))}
    if since_seconds > 0:
        ts = time.time() if now is None else now
        params["since"] = max(0, int(ts) - int(since_seconds))
    return params


def fetch_container_logs(
    cfg: DockhandConfig,
    env_id: int,
    container_id: str,
    since_seconds: int = 7200,
    tail: int = 200,
) -> str:
    """Last `tail` lines from the past `since_seconds` (GET /api/containers/{id}/logs).

    Dockhand returns {"logs": "<text>"} with Docker timestamps already prefixed and the
    stream frame headers already stripped — no demuxing needed (unlike Portainer).
    """
    params = {"env": env_id, **log_query_params(since_seconds, tail)}
    with _client(cfg, timeout=60) as client:
        r = client.get(f"/api/containers/{container_id}/logs", headers=_headers(cfg), params=params)
    if not r.is_success:
        return f"[log fetch failed: HTTP {r.status_code}]"
    body = r.json()
    return (body.get("logs", "") if isinstance(body, dict) else str(body)).rstrip("\n")


def upload_to_container(
    cfg: DockhandConfig,
    env_id: int,
    container_id: str,
    tar_bytes: bytes,
    dest_path: str,
) -> None:
    """Write a tar archive's contents into a container under dest_path (atc quick publish).

    Same signature as portainer.upload_to_container. Dockhand has no raw archive PUT, so this
    unpacks the tar locally, creates directories via /files/create and uploads files per
    directory via multipart /files/upload (Dockhand tar-wraps them server-side).
    """
    dest_path = dest_path.rstrip("/")
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        members = tar.getmembers()
        dirs = sorted({m.name for m in members if m.isdir()}
                      | {m.name.rsplit("/", 1)[0] for m in members if m.isfile() and "/" in m.name})
        by_dir: dict[str, list[tuple[str, bytes]]] = {}
        for m in members:
            if m.isfile():
                parent, _, fname = m.name.rpartition("/")
                fh = tar.extractfile(m)
                by_dir.setdefault(parent, []).append((fname, fh.read() if fh else b""))
    with _client(cfg, timeout=120) as client:
        for d in dirs:
            r = client.post(f"/api/containers/{container_id}/files/create", headers=_headers(cfg),
                            params={"env": env_id}, json={"path": f"{dest_path}/{d}", "type": "directory"})
            if not r.is_success and "exist" not in r.text.lower():
                _check(r, f"mkdir {d}")
        for d, files in by_dir.items():
            target = f"{dest_path}/{d}" if d else dest_path
            r = _check(client.post(
                f"/api/containers/{container_id}/files/upload", headers=_headers(cfg),
                params={"env": env_id, "path": target},
                files=[("files", (name, data)) for name, data in files],
            ), f"upload to {target}")
            errs = (r.json() or {}).get("errors") if r.headers.get("content-type", "").startswith("application/json") else None
            if errs:
                raise RuntimeError(f"Dockhand upload to {target}: {errs}")


# ── stacks ───────────────────────────────────────────────────────────────────

def fetch_stacks(
    cfg: DockhandConfig,
    envs: list[DockhandEnvironment] | None = None,
) -> list[DockhandStack]:
    """All compose stacks across `envs` (default: every environment). GET /api/stacks?env=X.

    Includes stacks Dockhand created ("internal"), adopted ones ("external") and Git stacks.
    """
    envs = envs if envs is not None else fetch_environments(cfg)
    out: list[DockhandStack] = []
    with _client(cfg, timeout=30) as client:
        for e in envs:
            r = _check(client.get("/api/stacks", headers=_headers(cfg), params={"env": e.id}),
                       f"stacks (env {e.id})")
            for s in r.json():
                out.append(DockhandStack(
                    name=s.get("name", ""), status=s.get("status", ""), env_id=e.id, env_name=e.name,
                    source_type=s.get("sourceType", ""), containers=len(s.get("containers") or []),
                ))
    return out


def fetch_stack_file(cfg: DockhandConfig, env_id: int, name: str) -> str:
    """Current compose file content of a stack (GET /api/stacks/{name}/compose)."""
    with _client(cfg) as client:
        r = _check(client.get(f"/api/stacks/{name}/compose", headers=_headers(cfg),
                              params={"env": env_id}), f"stack file ({name})")
    return r.json().get("content", "")


def fetch_stack_env(cfg: DockhandConfig, env_id: int, name: str) -> list[dict]:
    """Stack env vars as [{"name", "value"}] (Portainer's shape) from GET /api/stacks/{name}/env."""
    with _client(cfg) as client:
        r = _check(client.get(f"/api/stacks/{name}/env", headers=_headers(cfg),
                              params={"env": env_id}), f"stack env ({name})")
    return [{"name": v.get("key", ""), "value": v.get("value", "")}
            for v in r.json().get("variables", [])]


def _to_dh_vars(env_vars: list[dict]) -> list[dict]:
    return [{"key": v.get("name", v.get("key", "")), "value": v.get("value", "")} for v in env_vars]


def create_stack(
    cfg: DockhandConfig,
    env_id: int,
    name: str,
    compose_content: str,
    env_vars: list[dict] | None = None,
) -> str:
    """Create and start a compose stack (POST /api/stacks, synchronous). Returns the stack name.

    env_vars uses Portainer's [{"name", "value"}] shape so callers don't change.
    """
    with _client(cfg, timeout=600) as client:
        _check(client.post("/api/stacks", headers=_headers(cfg, sync=True), params={"env": env_id},
                           json={"name": name, "compose": compose_content, "start": True,
                                 "envVars": _to_dh_vars(env_vars or [])}),
               f"stack create ({name})")
    return name


def update_stack(
    cfg: DockhandConfig,
    env_id: int,
    name: str,
    compose_content: str,
    env_vars: list[dict] | None = None,
    pull_image: bool = False,
    force_clear_env: bool = False,
) -> None:
    """Save new compose content and redeploy (PUT /api/stacks/{name}/compose, restart=true).

    env_vars=None leaves the stack's env untouched (Dockhand stores env separately, so there
    is no Portainer-style wipe-on-omit). env_vars=[] still requires force_clear_env=True, to
    keep the same guard rail callers already rely on.
    """
    if env_vars == [] and not force_clear_env:
        raise ValueError("update_stack: env_vars=[] would clear all env vars; pass force_clear_env=True")
    with _client(cfg, timeout=600) as client:
        if env_vars is not None:
            # Internal/adopted stacks read non-secret vars from the stack's .env file; the
            # JSON /env endpoint only persists to the DB (meant for secrets and Git stacks)
            # and the running stack never sees it. Replace the whole file, which matches
            # Portainer's full-replace semantics.
            raw = "".join(f"{v['key']}={v['value']}\n" for v in _to_dh_vars(env_vars))
            _check(client.put(f"/api/stacks/{name}/env/raw", headers=_headers(cfg), params={"env": env_id},
                              json={"content": raw}), f"stack env update ({name})")
        _check(client.put(f"/api/stacks/{name}/compose", headers=_headers(cfg, sync=True),
                          params={"env": env_id}, json={"content": compose_content, "restart": True}),
               f"stack update ({name})")
        if pull_image:
            _check(client.post(f"/api/stacks/{name}/deploy", headers=_headers(cfg, sync=True),
                               params={"env": env_id}, json={"pull": True}), f"stack pull+deploy ({name})")


def delete_stack(cfg: DockhandConfig, env_id: int, name: str, remove_files: bool = True) -> None:
    """compose down + forget the stack (DELETE /api/stacks/{name}). Volumes are kept."""
    with _client(cfg, timeout=180) as client:
        _check(client.delete(f"/api/stacks/{name}", headers=_headers(cfg),
                             params={"env": env_id, "force": "true", "files": str(remove_files).lower()}),
               f"stack delete ({name})")


def stack_action(cfg: DockhandConfig, env_id: int, name: str, action: str) -> None:
    """start/stop/restart/down a stack, synchronously."""
    with _client(cfg, timeout=600) as client:
        _check(client.post(f"/api/stacks/{name}/{action}", headers=_headers(cfg, sync=True),
                           params={"env": env_id}), f"stack {action} ({name})")


# ── images / volumes / prune ─────────────────────────────────────────────────

def fetch_images(cfg: DockhandConfig, env_id: int, env_name: str) -> list[DockhandImage]:
    """Images in one environment (GET /api/images?env=X)."""
    with _client(cfg, timeout=30) as client:
        r = _check(client.get("/api/images", headers=_headers(cfg), params={"env": env_id}),
                   f"images (env {env_id})")
    out = []
    for img in r.json():
        tags = [t for t in (img.get("tags") or img.get("repoTags") or []) if t != "<none>:<none>"]
        out.append(DockhandImage(id=img.get("id", ""), tags=tags, size=img.get("size", 0),
                                 created=img.get("created", 0), env_id=env_id, env_name=env_name))
    return out


def fetch_volumes(cfg: DockhandConfig, env_id: int, env_name: str) -> list[DockhandVolume]:
    """Volumes in one environment (GET /api/volumes?env=X)."""
    with _client(cfg, timeout=30) as client:
        r = _check(client.get("/api/volumes", headers=_headers(cfg), params={"env": env_id}),
                   f"volumes (env {env_id})")
    return [DockhandVolume(name=v.get("name", ""), driver=v.get("driver", ""),
                           mountpoint=v.get("mountpoint", ""), created=v.get("created", ""),
                           env_id=env_id, env_name=env_name) for v in r.json()]


def _prune_result(r: httpx.Response, deleted_key: str) -> tuple[int, int]:
    res = (r.json() or {}).get("result") or {}
    return len(res.get(deleted_key) or []), int(res.get("SpaceReclaimed") or 0)


def prune_images(cfg: DockhandConfig, env_id: int, all_unused: bool = False) -> tuple[int, int]:
    """Prune dangling (or all unused) images. Returns (deleted, bytes_reclaimed)."""
    with _client(cfg, timeout=300) as client:
        r = _check(client.post("/api/prune/images", headers=_headers(cfg, sync=True),
                               params={"env": env_id, "dangling": str(not all_unused).lower()}),
                   f"prune images (env {env_id})")
    return _prune_result(r, "ImagesDeleted")


def prune_volumes(cfg: DockhandConfig, env_id: int) -> tuple[int, int]:
    """Prune unused volumes. Returns (deleted, bytes_reclaimed).

    UNVERIFIED response shape: assumed to mirror prune_images (Docker's VolumesDeleted);
    the spike didn't fire it because it hits every unused volume on the daemon.
    """
    with _client(cfg, timeout=300) as client:
        r = _check(client.post("/api/prune/volumes", headers=_headers(cfg, sync=True),
                               params={"env": env_id}), f"prune volumes (env {env_id})")
    return _prune_result(r, "VolumesDeleted")


# ── events (blackbox outage detection) ──────────────────────────────────────

_EVENT_ACTIONS = "start,stop,die,kill,oom"


def fetch_container_events(cfg: DockhandConfig, env_id: int, since_seconds: int = 7200) -> list[dict]:
    """Container start/stop/die/kill/oom events for the window, in Docker's event shape.

    Source is Dockhand's activity log (GET /api/activity), rewritten to
    {"Action", "time", "Actor": {"Attributes": {...}}} so portainer.outages_from_events
    works unchanged.
    """
    frm = datetime.fromtimestamp(time.time() - since_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    events: list[dict] = []
    offset, limit = 0, 500
    with _client(cfg, timeout=30) as client:
        while True:
            r = _check(client.get("/api/activity", headers=_headers(cfg), params={
                "environmentId": env_id, "actions": _EVENT_ACTIONS, "fromDate": frm,
                "limit": limit, "offset": offset}), f"activity (env {env_id})")
            body = r.json()
            rows = body.get("events", []) if isinstance(body, dict) else body
            for a in rows:
                ts = a.get("timestamp") or a.get("createdAt") or ""
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    t = 0
                attrs = dict(a.get("actorAttributes") or {})
                attrs.setdefault("name", a.get("containerName", ""))
                events.append({"Action": a.get("action", ""), "time": int(t), "id": a.get("containerId", ""),
                               "Actor": {"Attributes": attrs}})
            total = body.get("total", len(rows)) if isinstance(body, dict) else len(rows)
            offset += limit
            if not rows or offset >= total:
                break
    return events


@dataclass
class InferredStack:
    """A Compose project inferred from container labels (not registered as a
    Dockhand-managed stack). Same shape/semantics as lib.portainer.InferredStack."""
    project: str
    env_id: int
    env_name: str
    containers: list[DockhandContainer]

    @property
    def name(self) -> str:
        return self.project

    @property
    def running(self) -> int:
        return sum(1 for c in self.containers if c.state == "running")

    @property
    def total(self) -> int:
        return len(self.containers)


def infer_compose_stacks(
    containers: list[DockhandContainer],
    known_stack_names: set[str] | None = None,
) -> list[InferredStack]:
    """Group containers by their docker-compose project label.

    Containers without a compose project label are ignored. Pass
    known_stack_names to skip projects already registered as a
    Dockhand-managed stack (avoids duplicates in the listing).
    """
    known = {n.lower() for n in (known_stack_names or set())}
    by_project: dict[tuple[int, str, str], list[DockhandContainer]] = {}
    for c in containers:
        project = c.compose_project
        if not project:
            continue
        if project.lower() in known:
            continue
        key = (c.env_id, c.env_name, project)
        by_project.setdefault(key, []).append(c)

    return [
        InferredStack(project=project, env_id=env_id, env_name=env_name, containers=ctrs)
        for (env_id, env_name, project), ctrs in sorted(by_project.items())
    ]
