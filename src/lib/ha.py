"""Home Assistant REST API client."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from config import HAConfig, HAStatConfig


@dataclass
class HAStatus:
    online: bool
    version: str = ""
    state: str = ""    # "RUNNING", "STARTING", etc.
    location: str = ""
    error: str = ""


@dataclass
class HAEntityState:
    entity_id: str
    state: str
    attributes: dict = field(default_factory=dict)
    last_changed: str = ""
    last_updated: str = ""


@dataclass
class HAStatResult:
    cfg: HAStatConfig
    state: str
    unit: str = ""
    error: str = ""

    @property
    def display(self) -> str:
        if self.error:
            return f"[red]error[/]"
        unit = self.unit or self.cfg.unit
        return f"{self.state}{(' ' + unit) if unit else ''}"


def _headers(cfg: HAConfig) -> dict:
    return {
        "Authorization": f"Bearer {cfg.token}",
        "Content-Type": "application/json",
    }


def _client(cfg: HAConfig) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=10)


def get_status(cfg: HAConfig) -> HAStatus:
    try:
        with _client(cfg) as client:
            h = _headers(cfg)
            r = client.get(f"{cfg.base_url}/api/config", headers=h)
            r.raise_for_status()
            d = r.json()
            return HAStatus(
                online=True,
                version=d.get("version", ""),
                state=d.get("state", ""),
                location=d.get("location_name", ""),
            )
    except Exception as e:
        return HAStatus(online=False, error=str(e))


def get_entity_state(cfg: HAConfig, entity_id: str) -> HAEntityState:
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/states/{entity_id}", headers=_headers(cfg))
        r.raise_for_status()
        d = r.json()
        return HAEntityState(
            entity_id=d["entity_id"],
            state=d["state"],
            attributes=d.get("attributes", {}),
            last_changed=d.get("last_changed", ""),
            last_updated=d.get("last_updated", ""),
        )


def call_service(
    cfg: HAConfig,
    domain: str,
    service: str,
    data: dict | None = None,
) -> list[dict]:
    """Call a HA service. Returns list of affected entity states."""
    with _client(cfg) as client:
        r = client.post(
            f"{cfg.base_url}/api/services/{domain}/{service}",
            json=data or {},
            headers=_headers(cfg),
        )
        r.raise_for_status()
        return r.json()


_UPDATE_FEATURE_INSTALL = 1  # homeassistant.components.update.UpdateEntityFeature.INSTALL


@dataclass
class HAUpdateEntity:
    entity_id: str
    title: str          # friendly name / display title
    installed_version: str
    latest_version: str
    release_url: str = ""
    category: str = ""  # "core", "supervisor", "os", "hacs", "integration", "addon", etc.
    supported_features: int = 0

    @property
    def is_pending(self) -> bool:
        return self.installed_version != self.latest_version

    @property
    def supports_install(self) -> bool:
        """Whether HA's update.install service will actually work for this entity.

        Some integrations (e.g. Pi-hole) only report update availability and never
        implement install — calling update.install on them always 500s. HA exposes
        this via the INSTALL bit of the entity's supported_features bitmask.
        """
        return bool(self.supported_features & _UPDATE_FEATURE_INSTALL)

    @property
    def short_category(self) -> str:
        eid = self.entity_id.lower()
        if "home_assistant_core" in eid:
            return "core"
        if "home_assistant_supervisor" in eid:
            return "supervisor"
        if "home_assistant_operating_system" in eid:
            return "os"
        if "hacs" in eid:
            return "hacs"
        # add-on updates typically contain "addon" in the entity id
        if "addon" in eid:
            return "addon"
        return self.category or "integration"


def list_updates(cfg: HAConfig, pending_only: bool = True) -> list[HAUpdateEntity]:
    """Return all update.* entities, optionally filtered to those with updates available."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/states", headers=_headers(cfg))
        r.raise_for_status()
        entities = []
        for d in r.json():
            if not d.get("entity_id", "").startswith("update."):
                continue
            attrs = d.get("attributes", {})
            installed = attrs.get("installed_version") or ""
            latest = attrs.get("latest_version") or ""
            ue = HAUpdateEntity(
                entity_id=d["entity_id"],
                title=attrs.get("title") or attrs.get("friendly_name") or d["entity_id"],
                installed_version=installed,
                latest_version=latest,
                release_url=attrs.get("release_url") or "",
                category=attrs.get("entity_category") or "",
                supported_features=attrs.get("supported_features") or 0,
            )
            if pending_only and not ue.is_pending:
                continue
            entities.append(ue)
        return entities


def get_diagnostics(cfg: HAConfig, since_iso: str = "", tail_lines: int = 150) -> str:
    """Return HA diagnostic text for the blackbox dump.

    Combines two sources (both gracefully skipped on error):
      1. Recent logbook entries since `since_iso` (ISO 8601 string).
      2. Active persistent notifications (user-visible warnings/errors).

    The old /api/error_log endpoint was removed in HA 2023.6+.
    """
    parts: list[str] = []

    with _client(cfg) as client:
        h = _headers(cfg)

        # ── Logbook ───────────────────────────────────────────────────────────
        try:
            url = f"{cfg.base_url}/api/logbook"
            if since_iso:
                url += f"/{since_iso}"
            r = client.get(url, headers=h)
            if r.is_success:
                entries = r.json()
                if entries:
                    parts.append(f"Logbook ({len(entries)} entries):")
                    for e in entries[-tail_lines:]:
                        when = e.get("when", "")[:19]
                        name = e.get("name", "")
                        msg  = e.get("message", "")
                        domain = e.get("domain", "")
                        parts.append(f"  [{when}] {domain}/{name}: {msg}")
                else:
                    parts.append("Logbook: no entries in window.")
            else:
                parts.append(f"Logbook: HTTP {r.status_code}")
        except Exception as e:
            parts.append(f"Logbook: error — {e}")

        # ── Persistent notifications ──────────────────────────────────────────
        try:
            r = client.get(f"{cfg.base_url}/api/states", headers=h)
            if r.is_success:
                notifs = [
                    s for s in r.json()
                    if s.get("entity_id", "").startswith("persistent_notification.")
                    and s.get("state") != "dismissed"
                ]
                if notifs:
                    parts.append(f"\nPersistent notifications ({len(notifs)}):")
                    for n in notifs:
                        attrs = n.get("attributes", {})
                        title = attrs.get("title", n["entity_id"])
                        msg   = attrs.get("message", "")
                        parts.append(f"  ⚠  {title}: {msg}")
                else:
                    parts.append("\nPersistent notifications: none.")
        except Exception as e:
            parts.append(f"\nPersistent notifications: error — {e}")

    return "\n".join(parts)


def get_stats(cfg: HAConfig) -> list[HAStatResult]:
    results: list[HAStatResult] = []
    with _client(cfg) as client:
        h = _headers(cfg)
        for stat in cfg.stats:
            try:
                r = client.get(f"{cfg.base_url}/api/states/{stat.entity}", headers=h)
                r.raise_for_status()
                d = r.json()
                unit = d.get("attributes", {}).get("unit_of_measurement", stat.unit)
                results.append(HAStatResult(cfg=stat, state=d["state"], unit=unit))
            except Exception as e:
                results.append(HAStatResult(cfg=stat, state="", error=str(e)))
    return results
