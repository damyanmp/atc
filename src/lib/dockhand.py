"""Dockhand REST API client.

Eventual Portainer replacement — https://dockhand.pro. Bearer-token auth
("dh_..." tokens, created in the Dockhand UI under Profile → API tokens).

FIELD NAMES ARE UNVERIFIED. The endpoint table and auth model below come
straight from https://dockhand.pro/manual/#api-reference, which documents
routes and bodies but not exact response field casing (Dockhand only serves
a live OpenAPI schema from a running instance with FEAT_API_DOCS=true — the
public docs site isn't one). Field lookups here assume Dockhand's own
camelCase convention (id/name/image/state/health/labels/environmentId), the
norm for a modern JS/TS API, as the closest reasonable guess — NOT confirmed
against a real response. Before relying on this against a live Dockhand
instance: point one at FEAT_API_DOCS=true, hit each endpoint below, and fix
any field name that doesn't match. Until then, treat every fetch_* function
as unverified.

Mirrors lib/portainer.py's public shape (dataclass fields, function names,
needs_attention/compose_project/started_within semantics) so a future
backend switch in cli.py has as small a diff as possible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from config import DockhandConfig


@dataclass
class DockhandEnvironment:
    id: int
    name: str
    url: str = ""
    status: str = ""  # Dockhand doesn't use Portainer's 1/2 status ints


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
        """True if the container started (or restarted) within the last `seconds`.

        Parses the human status string e.g. "Up 3 minutes", "Up 47 seconds".
        Only matches containers that are currently "Up" — avoids flagging
        long-running healthy containers.
        """
        m = re.match(r"Up (\d+)\s+(second|minute|hour)", self.status, re.IGNORECASE)
        if not m:
            return False
        n, unit = int(m.group(1)), m.group(2).lower()
        uptime_sec = n * {"second": 1, "minute": 60, "hour": 3600}[unit]
        return uptime_sec <= seconds


@dataclass
class DockhandStack:
    name: str          # stacks are addressed by name, not id — no id in the API
    status: str
    env_id: int
    env_name: str


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


def _headers(cfg: DockhandConfig) -> dict:
    return {"Authorization": f"Bearer {cfg.api_token}"}


def _client(cfg: DockhandConfig, timeout: float = 15) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=timeout)


def health_check(cfg: DockhandConfig) -> bool:
    """GET /api/health — True if Dockhand is reachable and healthy."""
    with _client(cfg, timeout=5) as client:
        r = client.get(f"{cfg.base_url}/api/health", headers=_headers(cfg))
        return r.is_success


def fetch_environments(cfg: DockhandConfig) -> list[DockhandEnvironment]:
    """Return all Docker environments registered in Dockhand (GET /api/environments)."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/environments", headers=_headers(cfg))
        if not r.is_success:
            raise RuntimeError(f"Dockhand environments HTTP {r.status_code}: {r.text[:200]}")
        return [
            DockhandEnvironment(
                id=e["id"],
                name=e.get("name", str(e["id"])),
                url=e.get("url", ""),
                status=e.get("status", ""),
            )
            for e in r.json()
        ]


def fetch_containers(
    cfg: DockhandConfig,
    env_id: int,
    env_name: str,
) -> list[DockhandContainer]:
    """Return containers for a given Dockhand environment (GET /api/containers?env=X)."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/containers",
            headers=_headers(cfg),
            params={"env": env_id},
        )
        if not r.is_success:
            raise RuntimeError(
                f"Dockhand containers HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )

    containers = []
    for c in r.json():
        name = c.get("name") or c.get("id", "")[:12]
        containers.append(DockhandContainer(
            id=c.get("id", ""),
            name=name,
            image=c.get("image", ""),
            state=c.get("state", ""),
            status=c.get("status", ""),
            health=c.get("health", ""),
            env_id=env_id,
            env_name=env_name,
            labels=c.get("labels") or {},
        ))
    return containers


def container_action(cfg: DockhandConfig, container_id: str, action: str) -> None:
    """Perform start/stop/restart/pause/unpause on a container. Raises on failure."""
    with _client(cfg) as client:
        r = client.post(
            f"{cfg.base_url}/api/containers/{container_id}/{action}",
            headers=_headers(cfg),
        )
        if not r.is_success:
            raise RuntimeError(
                f"Container {action} HTTP {r.status_code} ({container_id}): {r.text[:200]}"
            )


def fetch_container_logs(
    cfg: DockhandConfig,
    container_id: str,
    tail: int = 200,
) -> str:
    """Fetch the last `tail` lines of container logs (GET /api/containers/[id]/logs)."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/containers/{container_id}/logs",
            headers=_headers(cfg),
            params={"tail": tail},
        )
        if not r.is_success:
            return f"[log fetch failed: HTTP {r.status_code}]"
    return r.text


def fetch_stacks(
    cfg: DockhandConfig,
    env_id: int | None = None,
    env_name: str = "",
) -> list[DockhandStack]:
    """Return compose stacks (GET /api/stacks[?env=X])."""
    params = {"env": env_id} if env_id is not None else {}
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/stacks", headers=_headers(cfg), params=params)
        if not r.is_success:
            raise RuntimeError(f"Dockhand stacks HTTP {r.status_code}: {r.text[:200]}")
    return [
        DockhandStack(
            name=s.get("name", ""),
            status=s.get("status", ""),
            env_id=env_id or s.get("environmentId", 0),
            env_name=env_name,
        )
        for s in r.json()
    ]


def stack_action(
    cfg: DockhandConfig,
    name: str,
    action: str,
    env_id: int | None = None,
    sync: bool = True,
) -> dict:
    """Perform start/stop/restart/deploy/down on a stack (by NAME — stacks have no id).

    sync=True sends `Accept: application/json` so the server runs the full
    operation and returns the result directly instead of a {"jobId": ...}
    for async polling (the latter is meant for the Dockhand UI, not scripts).
    """
    params = {"env": env_id} if env_id is not None else {}
    headers = _headers(cfg)
    if sync:
        headers["Accept"] = "application/json"
    with _client(cfg, timeout=300) as client:
        r = client.post(
            f"{cfg.base_url}/api/stacks/{name}/{action}",
            headers=headers,
            params=params,
        )
        if not r.is_success:
            raise RuntimeError(
                f"Stack {action} HTTP {r.status_code} ({name}): {r.text[:200]}"
            )
    return r.json() if r.content else {}


def delete_stack(cfg: DockhandConfig, name: str, env_id: int | None = None) -> None:
    """Remove a compose stack (DELETE /api/stacks/[name])."""
    params = {"env": env_id} if env_id is not None else {}
    with _client(cfg, timeout=60) as client:
        r = client.delete(
            f"{cfg.base_url}/api/stacks/{name}", headers=_headers(cfg), params=params,
        )
        if not r.is_success:
            raise RuntimeError(f"Stack delete HTTP {r.status_code} ({name}): {r.text[:200]}")


def fetch_images(cfg: DockhandConfig, env_id: int, env_name: str) -> list[DockhandImage]:
    """Return images for a given environment (GET /api/images?env=X)."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/images", headers=_headers(cfg), params={"env": env_id},
        )
        if not r.is_success:
            raise RuntimeError(
                f"Dockhand images HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )
    images = []
    for img in r.json():
        tags = [t for t in (img.get("tags") or []) if t != "<none>:<none>"]
        images.append(DockhandImage(
            id=img.get("id", ""),
            tags=tags,
            size=img.get("size", 0),
            created=img.get("created", 0),
            env_id=env_id,
            env_name=env_name,
        ))
    return images


def fetch_volumes(cfg: DockhandConfig, env_id: int, env_name: str) -> list[DockhandVolume]:
    """Return volumes for a given environment (GET /api/volumes?env=X)."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/volumes", headers=_headers(cfg), params={"env": env_id},
        )
        if not r.is_success:
            raise RuntimeError(
                f"Dockhand volumes HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )
    volumes = []
    for v in r.json():
        volumes.append(DockhandVolume(
            name=v.get("name", ""),
            driver=v.get("driver", ""),
            mountpoint=v.get("mountpoint", ""),
            created=v.get("created", ""),
            env_id=env_id,
            env_name=env_name,
        ))
    return volumes


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
