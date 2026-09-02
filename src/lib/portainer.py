"""Portainer REST API client.

Supports Portainer CE and EE via Bearer token authentication.
Generate a token: Portainer UI → Account → Access tokens → Add access token.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from config import PortainerConfig


@dataclass
class PortainerEnvironment:
    id: int
    name: str
    url: str
    status: int   # 1=active, 2=inactive


@dataclass
class PortainerContainer:
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


def _headers(cfg: PortainerConfig) -> dict:
    return {"X-API-Key": cfg.api_key}


def _client(cfg: PortainerConfig, timeout: float = 15) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=timeout)


def fetch_environments(cfg: PortainerConfig) -> list[PortainerEnvironment]:
    """Return all Docker environments registered in Portainer."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/endpoints", headers=_headers(cfg))
        if not r.is_success:
            raise RuntimeError(f"Portainer endpoints HTTP {r.status_code}: {r.text[:200]}")
        return [
            PortainerEnvironment(
                id=e["Id"],
                name=e["Name"],
                url=e.get("URL", ""),
                status=e.get("Status", 1),
            )
            for e in r.json()
        ]


def fetch_containers(
    cfg: PortainerConfig,
    env_id: int,
    env_name: str,
    all_containers: bool = True,
) -> list[PortainerContainer]:
    """Return containers for a given Portainer environment.

    Args:
        all_containers: If True, include stopped containers. Default True so
            blackbox always sees exited containers.
    """
    params = {"all": "true"} if all_containers else {}
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/endpoints/{env_id}/docker/containers/json",
            headers=_headers(cfg),
            params=params,
        )
        if not r.is_success:
            raise RuntimeError(
                f"Portainer containers HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )

    containers = []
    for c in r.json():
        names = c.get("Names", [])
        name = names[0].lstrip("/") if names else c["Id"][:12]
        health_status = c.get("Status", "")
        # Extract health from State if present
        state_obj = c.get("State", "")
        health = ""
        if isinstance(state_obj, dict):
            health_obj = state_obj.get("Health", {})
            health = health_obj.get("Status", "") if health_obj else ""
        # Fall back to parsing the status string
        if not health and "(healthy)" in health_status:
            health = "healthy"
        elif not health and "(unhealthy)" in health_status:
            health = "unhealthy"

        containers.append(PortainerContainer(
            id=c["Id"],
            name=name,
            image=c.get("Image", ""),
            state=c.get("State", ""),
            status=health_status,
            health=health,
            env_id=env_id,
            env_name=env_name,
            labels=c.get("Labels") or {},
        ))

    return containers


def fetch_container_logs(
    cfg: PortainerConfig,
    env_id: int,
    container_id: str,
    since_seconds: int = 7200,
    tail: int = 200,
) -> str:
    """Fetch the last `tail` lines of container logs from the past `since_seconds`."""
    params = {
        "stdout": "true",
        "stderr": "true",
        "timestamps": "true",
        "since": since_seconds,
        "tail": tail,
    }
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/endpoints/{env_id}/docker/containers/{container_id}/logs",
            headers=_headers(cfg),
            params=params,
        )
        if not r.is_success:
            return f"[log fetch failed: HTTP {r.status_code}]"

    # Docker log stream has an 8-byte header per frame; strip it for readability
    raw = r.content
    lines = []
    i = 0
    while i < len(raw):
        if i + 8 > len(raw):
            break
        # header: [stream_type(1), 0, 0, 0, size(4 big-endian)]
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        payload = raw[i + 8:i + 8 + size]
        try:
            lines.append(payload.decode("utf-8", errors="replace").rstrip("\n"))
        except Exception:
            pass
        i += 8 + size

    return "\n".join(lines) if lines else r.text


# ── Images ────────────────────────────────────────────────────────────────────

@dataclass
class PortainerImage:
    id: str           # full SHA256
    tags: list[str]   # repo:tag pairs; empty = dangling
    size: int         # bytes (compressed on-disk size)
    created: int      # unix timestamp
    env_id: int
    env_name: str

    @property
    def dangling(self) -> bool:
        return not self.tags

    @property
    def display_name(self) -> str:
        return self.tags[0] if self.tags else f"<none> ({self.id[:12]})"


def fetch_images(
    cfg: PortainerConfig,
    env_id: int,
    env_name: str,
) -> list[PortainerImage]:
    """Return all images for a given Portainer environment."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/endpoints/{env_id}/docker/images/json",
            headers=_headers(cfg),
            params={"all": "false"},  # exclude intermediate layers
        )
        if not r.is_success:
            raise RuntimeError(
                f"Portainer images HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )

    images = []
    for img in r.json():
        tags = [t for t in (img.get("RepoTags") or []) if t != "<none>:<none>"]
        images.append(PortainerImage(
            id=img.get("Id", ""),
            tags=tags,
            size=img.get("Size", 0),
            created=img.get("Created", 0),
            env_id=env_id,
            env_name=env_name,
        ))
    return images


def prune_images(
    cfg: PortainerConfig,
    env_id: int,
    all_unused: bool = False,
) -> tuple[int, int]:
    """Prune images. Returns (images_deleted, bytes_reclaimed).

    all_unused=False: only dangling (untagged) images.
    all_unused=True: all images not referenced by any container.
    """
    filters = '{"dangling":["false"]}' if all_unused else '{"dangling":["true"]}'
    with _client(cfg) as client:
        r = client.post(
            f"{cfg.base_url}/api/endpoints/{env_id}/docker/images/prune",
            headers=_headers(cfg),
            params={"filters": filters},
        )
        if not r.is_success:
            raise RuntimeError(
                f"Image prune HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )

    data = r.json()
    deleted = len(data.get("ImagesDeleted") or [])
    reclaimed = data.get("SpaceReclaimed", 0)
    return deleted, reclaimed


# ── Volumes ───────────────────────────────────────────────────────────────────

@dataclass
class PortainerVolume:
    name: str
    driver: str
    mountpoint: str
    created: str      # ISO 8601 string (may be empty)
    env_id: int
    env_name: str


def fetch_volumes(
    cfg: PortainerConfig,
    env_id: int,
    env_name: str,
) -> list[PortainerVolume]:
    """Return all volumes for a given Portainer environment."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/endpoints/{env_id}/docker/volumes",
            headers=_headers(cfg),
        )
        if not r.is_success:
            raise RuntimeError(
                f"Portainer volumes HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )

    volumes = []
    for v in r.json().get("Volumes") or []:
        volumes.append(PortainerVolume(
            name=v.get("Name", ""),
            driver=v.get("Driver", ""),
            mountpoint=v.get("Mountpoint", ""),
            created=v.get("CreatedAt", ""),
            env_id=env_id,
            env_name=env_name,
        ))
    return volumes


def prune_volumes(
    cfg: PortainerConfig,
    env_id: int,
) -> tuple[int, int]:
    """Prune unused (orphaned) volumes. Returns (volumes_deleted, bytes_reclaimed)."""
    with _client(cfg) as client:
        r = client.post(
            f"{cfg.base_url}/api/endpoints/{env_id}/docker/volumes/prune",
            headers=_headers(cfg),
        )
        if not r.is_success:
            raise RuntimeError(
                f"Volume prune HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )

    data = r.json()
    deleted = len(data.get("VolumesDeleted") or [])
    reclaimed = data.get("SpaceReclaimed", 0)
    return deleted, reclaimed


# ── Container outage detection ────────────────────────────────────────────────

@dataclass
class ContainerOutage:
    env_name: str
    container_name: str
    started_at: str    # ISO 8601 timestamp of die/stop event
    ended_at: str      # ISO 8601 timestamp of recovery start, or "ongoing"
    duration_min: int  # 0 if ongoing
    exit_code: str     # exit code from die event, "" if stop


def container_action(
    cfg: PortainerConfig,
    env_id: int,
    container_id: str,
    action: str,
) -> None:
    """Perform start/stop/restart on a container. Raises on failure."""
    with _client(cfg) as client:
        r = client.post(
            f"{cfg.base_url}/api/endpoints/{env_id}/docker/containers/{container_id}/{action}",
            headers=_headers(cfg),
        )
        # 304 = already in desired state (e.g. start on a running container)
        if not r.is_success and r.status_code != 304:
            raise RuntimeError(
                f"Container {action} HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )


@dataclass
class PortainerStack:
    id: int
    name: str
    status: int   # 1=active, 2=inactive
    env_id: int
    env_name: str
    type: int     # 1=swarm, 2=compose


def fetch_stacks(
    cfg: PortainerConfig,
    envs: "list[PortainerEnvironment] | None" = None,
) -> list[PortainerStack]:
    """Return all stacks registered in Portainer."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/stacks", headers=_headers(cfg))
        if not r.is_success:
            raise RuntimeError(f"Portainer stacks HTTP {r.status_code}: {r.text[:200]}")

    env_map = {e.id: e.name for e in (envs or [])}
    return [
        PortainerStack(
            id=s["Id"],
            name=s["Name"],
            status=s.get("Status", 1),
            env_id=s.get("EndpointId", 0),
            env_name=env_map.get(s.get("EndpointId", 0), str(s.get("EndpointId", 0))),
            type=s.get("Type", 2),
        )
        for s in r.json()
    ]


def fetch_stack_file(cfg: PortainerConfig, stack_id: int) -> str:
    """Return the current Compose file content for an existing stack."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/stacks/{stack_id}/file", headers=_headers(cfg))
        if not r.is_success:
            raise RuntimeError(f"Stack file HTTP {r.status_code} (stack {stack_id}): {r.text[:200]}")
    return r.json().get("StackFileContent", "")


def create_stack(
    cfg: PortainerConfig,
    env_id: int,
    name: str,
    compose_content: str,
    env_vars: "list[dict] | None" = None,
) -> int:
    """Create a new standalone Compose stack on the given environment. Returns the new stack id.

    Uses a long timeout: Portainer pulls every image referenced in the
    compose file synchronously before returning, which can take minutes for
    a multi-service stack with large/uncached images. A short timeout here
    doesn't cancel the deploy — Portainer keeps running it server-side — it
    just makes the CLI report a false failure while a real stack silently
    comes up, risking an accidental duplicate on retry.
    """
    with _client(cfg, timeout=300) as client:
        r = client.post(
            f"{cfg.base_url}/api/stacks/create/standalone/string",
            headers=_headers(cfg),
            params={"endpointId": env_id},
            json={"name": name, "stackFileContent": compose_content, "env": env_vars or []},
        )
        if not r.is_success:
            raise RuntimeError(f"Stack create HTTP {r.status_code} (env {env_id}): {r.text[:300]}")
    return r.json().get("Id", 0)


def fetch_stack_env(cfg: PortainerConfig, stack_id: int) -> list[dict]:
    """Return the current Env array (list of {"name": ..., "value": ...}) for a stack."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/stacks/{stack_id}", headers=_headers(cfg))
        if not r.is_success:
            raise RuntimeError(f"Stack fetch HTTP {r.status_code} (stack {stack_id}): {r.text[:200]}")
    return r.json().get("Env", [])


def update_stack(
    cfg: PortainerConfig,
    stack_id: int,
    env_id: int,
    compose_content: str,
    env_vars: "list[dict] | None" = None,
    prune: bool = True,
    pull_image: bool = False,
    force_clear_env: bool = False,
) -> None:
    """Redeploy an existing Compose stack with new file content.

    SAFETY: Portainer's PUT /api/stacks/{id} REPLACES the stack's Env array
    wholesale — it does not merge. A caller that omits env_vars (e.g. only
    wants to bump pullImage/prune) would otherwise silently wipe every
    secret (SECRET_KEY, POSTGRES_PASSWORD, ...) attached to the stack,
    crash-looping it. This happened for real on 2026-09-02 (staging-peppermint,
    stack 93) and required a full DB wipe + fresh secrets to recover, because
    no backup of the original values existed.

    To prevent a repeat: if env_vars is None, this function now AUTO-FETCHES
    and reuses the stack's current Env array instead of defaulting to [].
    To genuinely clear all env vars, you must pass env_vars=[] AND
    force_clear_env=True — an accidental empty list alone is rejected.

    pull_image forces Portainer to re-pull images before redeploying — needed
    for `:latest`-style tags to actually refresh, since Docker otherwise only
    pulls images it doesn't already have locally.

    Uses a long timeout for the same reason as create_stack: an image pull
    (forced or first-time) runs synchronously inside this request.
    """
    if env_vars is None:
        env_vars = fetch_stack_env(cfg, stack_id)
    elif env_vars == [] and not force_clear_env:
        raise ValueError(
            "update_stack: env_vars=[] would wipe all env vars on this stack. "
            "Pass force_clear_env=True if you genuinely intend to clear them, "
            "or omit env_vars entirely to preserve the stack's current values."
        )

    with _client(cfg, timeout=300) as client:
        r = client.put(
            f"{cfg.base_url}/api/stacks/{stack_id}",
            headers=_headers(cfg),
            params={"endpointId": env_id},
            json={
                "stackFileContent": compose_content,
                "env": env_vars,
                "prune": prune,
                "pullImage": pull_image,
            },
        )
        if not r.is_success:
            raise RuntimeError(f"Stack update HTTP {r.status_code} (stack {stack_id}): {r.text[:300]}")


def delete_stack(cfg: PortainerConfig, stack_id: int, env_id: int) -> None:
    """Remove a Compose stack and its containers."""
    with _client(cfg, timeout=60) as client:
        r = client.delete(
            f"{cfg.base_url}/api/stacks/{stack_id}",
            headers=_headers(cfg),
            params={"endpointId": env_id},
        )
        if not r.is_success:
            raise RuntimeError(f"Stack delete HTTP {r.status_code} (stack {stack_id}): {r.text[:300]}")


def stack_action(
    cfg: PortainerConfig,
    stack_id: int,
    action: str,
    endpoint_id: int = 0,
) -> None:
    """Perform start/stop on a stack. Raises on failure.

    start can trigger an image pull the same way create/update do, so this
    also gets a long timeout rather than the 15s default.
    """
    with _client(cfg, timeout=300) as client:
        r = client.post(
            f"{cfg.base_url}/api/stacks/{stack_id}/{action}",
            headers=_headers(cfg),
            params={"endpointId": endpoint_id},
        )
        if not r.is_success:
            raise RuntimeError(
                f"Stack {action} HTTP {r.status_code} (stack {stack_id}): {r.text[:200]}"
            )


@dataclass
class InferredStack:
    """A Compose project inferred from container labels (not registered in Portainer)."""
    project: str          # com.docker.compose.project value
    env_id: int
    env_name: str
    containers: "list[PortainerContainer]"

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
    containers: "list[PortainerContainer]",
    portainer_stack_names: "set[str] | None" = None,
) -> "list[InferredStack]":
    """Group containers by their docker-compose project label.

    Containers without a compose project label are ignored.
    Pass portainer_stack_names to skip projects already registered as
    a Portainer-managed stack (avoids duplicates in the listing).
    """
    known = {n.lower() for n in (portainer_stack_names or set())}
    by_project: dict[tuple[int, str, str], list[PortainerContainer]] = {}
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


def fetch_container_events(
    cfg,
    env_id: int,
    since_seconds: int = 7200,
) -> list[dict]:
    """Return Docker container events (start/stop/die/kill/oom) for the window."""
    since_ts = int(time.time()) - since_seconds
    params = {
        "since": since_ts,
        "filters": '{"type":["container"],"event":["start","stop","die","kill","oom"]}',
    }
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/endpoints/{env_id}/docker/events",
            headers=_headers(cfg),
            params=params,
        )
        if not r.is_success:
            raise RuntimeError(
                f"Portainer events HTTP {r.status_code} (env {env_id}): {r.text[:200]}"
            )

    # Docker returns newline-delimited JSON objects
    events = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            import json
            events.append(json.loads(line))
        except Exception:
            pass
    return events


def outages_from_events(
    env_name: str,
    events: list[dict],
) -> list[ContainerOutage]:
    """Reconstruct container outage events from a Docker event stream.

    Pairs each die/stop/kill/oom with the next start for the same container.
    Containers that never recovered within the window are marked "ongoing".
    """
    # Group events by container name, sorted chronologically
    by_container: dict[str, list[dict]] = {}
    for e in sorted(events, key=lambda x: x.get("time", 0)):
        name = e.get("Actor", {}).get("Attributes", {}).get("name", "")
        if not name:
            continue
        by_container.setdefault(name, []).append(e)

    _DOWN_ACTIONS = {"die", "stop", "kill", "oom"}
    outages: list[ContainerOutage] = []

    for container_name, evts in by_container.items():
        down_event: dict | None = None

        for e in evts:
            action = e.get("Action", "")
            ts_unix = e.get("time", 0)
            ts_iso = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()

            if action in _DOWN_ACTIONS and down_event is None:
                down_event = e
                down_event["_iso"] = ts_iso

            elif action == "start" and down_event is not None:
                started = down_event["_iso"]
                dur = int((ts_unix - down_event.get("time", ts_unix)) / 60)
                exit_code = down_event.get("Actor", {}).get("Attributes", {}).get("exitCode", "")
                outages.append(ContainerOutage(
                    env_name=env_name,
                    container_name=container_name,
                    started_at=started,
                    ended_at=ts_iso,
                    duration_min=dur,
                    exit_code=exit_code,
                ))
                down_event = None

        # Still down at end of event window → ongoing
        if down_event is not None:
            started = down_event["_iso"]
            now = datetime.now(timezone.utc)
            dur = int((now.timestamp() - down_event.get("time", now.timestamp())) / 60)
            exit_code = down_event.get("Actor", {}).get("Attributes", {}).get("exitCode", "")
            outages.append(ContainerOutage(
                env_name=env_name,
                container_name=container_name,
                started_at=started,
                ended_at="ongoing",
                duration_min=dur,
                exit_code=exit_code,
            ))

    return outages
