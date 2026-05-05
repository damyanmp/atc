"""Config loader — reads config.yaml into typed dataclasses.

Secrets can be stored in the system keychain rather than in plain text.
Use the sentinel  keychain:<key-name>  as a value in config.yaml, e.g.:

    token_value: keychain:pve-01-token

On first use atc will prompt for the value and store it in the keychain.
Subsequent runs fetch it silently.  Run  atc secrets  to manage stored keys.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import keyring
import yaml

KEYCHAIN_SERVICE = "atc"
KEYCHAIN_PREFIX = "keychain:"


def _resolve_secret(raw_value: str, key_hint: str) -> str:
    """If raw_value is a keychain sentinel, look up (or prompt+store) the real value."""
    if not isinstance(raw_value, str) or not raw_value.startswith(KEYCHAIN_PREFIX):
        return raw_value

    key_name = raw_value[len(KEYCHAIN_PREFIX):]
    stored = keyring.get_password(KEYCHAIN_SERVICE, key_name)
    if stored:
        return stored

    # Not stored yet — prompt once and save
    print(f"\n🔑  Secret needed: {key_hint!r} (key: {key_name!r})")
    print(f"    This will be stored securely in your system keychain.")
    try:
        import getpass
        value = getpass.getpass(f"    Enter value: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)

    if not value:
        print("    Empty value — aborting.")
        sys.exit(1)

    secret_set(key_name, value)
    print(f"    ✅  Stored in keychain as {key_name!r}\n")
    return value


# ── Infrastructure ────────────────────────────────────────────────────────────

@dataclass
class ProxmoxClusterConfig:
    host: str
    user: str
    token_name: str
    token_value: str
    port: int = 8006
    verify_ssl: bool = False


@dataclass
class GuestUpdateCmd:
    cmd: str
    headless: bool = False


@dataclass
class SSHConfig:
    default_user: str = "root"
    key_file: str = "~/.ssh/id_rsa"
    connect_timeout: int = 10
    node_host_pattern: str = "{name}"  # e.g. "pve-{name}.local.example.com"
    verify_host_keys: bool = False     # set True to enforce ~/.ssh/known_hosts checking
    guest_users: dict = field(default_factory=dict)                      # guest name → ssh user
    guest_update_cmds: dict = field(default_factory=dict)                # guest name → GuestUpdateCmd

    @property
    def expanded_key_file(self) -> str:
        return os.path.expanduser(self.key_file)

    def node_host(self, node_name: str) -> str:
        return self.node_host_pattern.format(name=node_name)

    def guest_user(self, guest_name: str) -> str | None:
        return self.guest_users.get(guest_name)

    def guest_update_cmd(self, guest_name: str) -> "GuestUpdateCmd | None":
        return self.guest_update_cmds.get(guest_name)


# ── Monitoring ────────────────────────────────────────────────────────────────

@dataclass
class BeszelConfig:
    host: str
    api_key: str
    port: int = 443
    https: bool = True
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


# ── Network ───────────────────────────────────────────────────────────────────

@dataclass
class UniFiConfig:
    host: str
    api_key: str = ""        # preferred: UniFi Network 8.x+ API key
    username: str = ""       # fallback: username/password cookie auth
    password: str = ""
    port: int = 443
    https: bool = True
    udm: bool = True         # True for UDM/UDR (adds /proxy/network/ prefix)
    site: str = "default"
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass
class PiholeInstanceConfig:
    name: str
    host: str
    password: str
    port: int = 80
    https: bool = False
    verify_ssl: bool = False
    primary: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


# ── Home Automation ───────────────────────────────────────────────────────────

@dataclass
class HAStatConfig:
    name: str
    entity: str
    unit: str = ""


@dataclass
class HAConfig:
    host: str
    token: str
    port: int = 8123
    https: bool = False
    verify_ssl: bool = False
    stats: list[HAStatConfig] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


# ── Media ─────────────────────────────────────────────────────────────────────

@dataclass
class PlexConfig:
    host: str
    token: str
    port: int = 32400
    https: bool = False
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass
class ArrConfig:
    """Shared config for Radarr and Sonarr (both use the Servarr API)."""
    name: str           # "radarr" or "sonarr" — used in keychain key names
    host: str
    api_key: str
    port: int = 80
    base_path: str = ""  # e.g. "/radarr" or "/sonarr"
    https: bool = False
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}{self.base_path}"


@dataclass
class SeerrConfig:
    host: str
    api_key: str
    port: int = 443
    https: bool = True
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass
class TautulliConfig:
    host: str
    api_key: str
    port: int = 8181
    https: bool = False
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


# ── Monitoring ────────────────────────────────────────────────────────────────

@dataclass
class OpenSpeedTestConfig:
    host: str
    port: int = 3000
    https: bool = False
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass
class TLSCheckConfig:
    name: str
    host: str
    port: int = 443
    sni: str | None = None  # SNI override — useful for wildcard certs on internal IPs
    verify_ssl: bool = True


# ── Docker Hub ───────────────────────────────────────────────────────────────

@dataclass
class DockerHubConfig:
    """Optional Docker Hub credentials for authenticated tag-list API calls.

    Without this, atc queries Docker Hub anonymously and will hit rate limits
    quickly when checking many images. Create a Personal Access Token (PAT) at
    hub.docker.com → Account Settings → Personal access tokens.

    config.yaml:
        docker_hub:
          username: damyanmp
          token: keychain:dockerhub-token
    """
    username: str
    token: str    # Docker Hub PAT (read-only scope is enough)


# ── Portainer ─────────────────────────────────────────────────────────────────

@dataclass
class PortainerConfig:
    host: str
    api_key: str          # Portainer API token
    port: int = 9000
    https: bool = False
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


# ── Monitoring ────────────────────────────────────────────────────────────────

@dataclass
class UptimeKumaConfig:
    host: str
    api_key: str = ""
    slug: str = "default"
    port: int = 3001
    https: bool = False
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass
class PrometheusConfig:
    host: str
    port: int = 9090
    https: bool = False
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"


# ── Blackbox ──────────────────────────────────────────────────────────────────

@dataclass
class BlackboxSshHost:
    name: str
    host: str
    user: str = ""  # empty = use ssh.default_user


@dataclass
class BlackboxConfig:
    ssh_hosts: list[BlackboxSshHost] = field(default_factory=list)
    # Proxmox nodes are always added automatically


# ── Top-level config ──────────────────────────────────────────────────────────

@dataclass
class Config:
    proxmox: ProxmoxClusterConfig = field(default_factory=lambda: ProxmoxClusterConfig("", "", "", ""))
    ssh: SSHConfig = field(default_factory=SSHConfig)
    beszel: BeszelConfig | None = None
    pihole_instances: list[PiholeInstanceConfig] = field(default_factory=list)
    ha: HAConfig | None = None
    plex: PlexConfig | None = None
    radarr: ArrConfig | None = None
    sonarr: ArrConfig | None = None
    tautulli: TautulliConfig | None = None
    seerr: SeerrConfig | None = None
    openspeedtest: OpenSpeedTestConfig | None = None
    tls_checks: list[TLSCheckConfig] = field(default_factory=list)
    uptimekuma: UptimeKumaConfig | None = None
    portainer: PortainerConfig | None = None
    prometheus: PrometheusConfig | None = None
    unifi: UniFiConfig | None = None
    blackbox: BlackboxConfig = field(default_factory=BlackboxConfig)
    docker_hub: DockerHubConfig | None = None
    scripts_dir: str = "../../"
    refresh_interval: int = 30
    config_path: Path = field(default_factory=Path)

    @property
    def resolved_scripts_dir(self) -> Path:
        base = self.config_path.parent if self.config_path else Path.cwd()
        return (base / self.scripts_dir).resolve()


# ── Loader ────────────────────────────────────────────────────────────────────

def load_config(config_path: Path | None = None) -> Config:
    """Load config.yaml, resolving any keychain:* secret sentinels."""
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}\n"
            f"Copy config.yaml.example → config.yaml and fill in your values."
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    # Proxmox
    px_raw = raw.get("proxmox", {})
    proxmox = ProxmoxClusterConfig(
        host=px_raw["host"],
        port=int(px_raw.get("port", 8006)),
        user=px_raw["user"],
        token_name=px_raw["token_name"],
        token_value=_resolve_secret(px_raw["token_value"], "proxmox/cluster/token_value"),
        verify_ssl=bool(px_raw.get("verify_ssl", False)),
    )

    # SSH
    ssh_raw = raw.get("ssh", {})
    # guest_users: merge config.yaml's ssh.guest_users with the optional sidecar file
    guest_users: dict[str, str] = dict(ssh_raw.get("guest_users", {}))
    sidecar = config_path.parent / "guest_users.yaml"
    if sidecar.exists():
        with open(sidecar) as _f:
            guest_users.update(yaml.safe_load(_f) or {})
    # guest_update_cmds: optional sidecar for per-guest custom update commands
    # Supports both plain string ("update") and dict ({cmd: ..., headless: true})
    def _parse_update_cmds(raw_dict: dict) -> dict:
        out = {}
        for name, val in raw_dict.items():
            if isinstance(val, str):
                out[name] = GuestUpdateCmd(cmd=val)
            elif isinstance(val, dict):
                out[name] = GuestUpdateCmd(cmd=val["cmd"], headless=bool(val.get("headless", False)))
        return out

    guest_update_cmds: dict[str, GuestUpdateCmd] = _parse_update_cmds(ssh_raw.get("guest_update_cmds", {}))
    cmds_sidecar = config_path.parent / "guest_update_cmds.yaml"
    if cmds_sidecar.exists():
        with open(cmds_sidecar) as _f:
            guest_update_cmds.update(_parse_update_cmds(yaml.safe_load(_f) or {}))
    ssh = SSHConfig(
        default_user=ssh_raw.get("default_user", "root"),
        key_file=ssh_raw.get("key_file", "~/.ssh/id_rsa"),
        connect_timeout=int(ssh_raw.get("connect_timeout", 10)),
        node_host_pattern=ssh_raw.get("node_host_pattern", "{name}"),
        verify_host_keys=bool(ssh_raw.get("verify_host_keys", False)),
        guest_users=guest_users,
        guest_update_cmds=guest_update_cmds,
    )

    # Beszel
    beszel: BeszelConfig | None = None
    if "beszel" in raw:
        bz = raw["beszel"]
        beszel = BeszelConfig(
            host=bz["host"],
            port=int(bz.get("port", 443)),
            https=bool(bz.get("https", True)),
            api_key=_resolve_secret(bz["api_key"], "beszel/api_key"),
            verify_ssl=bool(bz.get("verify_ssl", False)),
        )

    # Pi-hole
    pihole_instances: list[PiholeInstanceConfig] = []
    for p in raw.get("pihole", []):
        ph_name = p["name"]
        pihole_instances.append(PiholeInstanceConfig(
            name=ph_name,
            host=p["host"],
            port=int(p.get("port", 80)),
            https=bool(p.get("https", False)),
            password=_resolve_secret(p["password"], f"pihole/{ph_name}/password"),
            verify_ssl=bool(p.get("verify_ssl", False)),
            primary=bool(p.get("primary", False)),
        ))

    # Home Assistant
    ha: HAConfig | None = None
    if "homeassistant" in raw:
        ha_raw = raw["homeassistant"]
        ha = HAConfig(
            host=ha_raw["host"],
            port=int(ha_raw.get("port", 8123)),
            https=bool(ha_raw.get("https", False)),
            token=_resolve_secret(ha_raw["token"], "homeassistant/token"),
            verify_ssl=bool(ha_raw.get("verify_ssl", False)),
            stats=[
                HAStatConfig(name=s["name"], entity=s["entity"], unit=s.get("unit", ""))
                for s in ha_raw.get("stats", [])
            ],
        )

    # Plex
    plex: PlexConfig | None = None
    if "plex" in raw:
        px2 = raw["plex"]
        plex = PlexConfig(
            host=px2["host"],
            port=int(px2.get("port", 32400)),
            https=bool(px2.get("https", False)),
            token=_resolve_secret(px2["token"], "plex/token"),
            verify_ssl=bool(px2.get("verify_ssl", False)),
        )

    # Radarr
    radarr: ArrConfig | None = None
    if "radarr" in raw:
        rr = raw["radarr"]
        radarr = ArrConfig(
            name="radarr",
            host=rr["host"],
            port=int(rr.get("port", 7878)),
            base_path=rr.get("base_path", ""),
            https=bool(rr.get("https", False)),
            api_key=_resolve_secret(rr["api_key"], "radarr/api_key"),
            verify_ssl=bool(rr.get("verify_ssl", False)),
        )

    # Sonarr
    sonarr: ArrConfig | None = None
    if "sonarr" in raw:
        sr = raw["sonarr"]
        sonarr = ArrConfig(
            name="sonarr",
            host=sr["host"],
            port=int(sr.get("port", 8989)),
            base_path=sr.get("base_path", ""),
            https=bool(sr.get("https", False)),
            api_key=_resolve_secret(sr["api_key"], "sonarr/api_key"),
            verify_ssl=bool(sr.get("verify_ssl", False)),
        )

    # Tautulli
    tautulli: TautulliConfig | None = None
    if "tautulli" in raw:
        tt = raw["tautulli"]
        tautulli = TautulliConfig(
            host=tt["host"],
            port=int(tt.get("port", 8181)),
            https=bool(tt.get("https", False)),
            api_key=_resolve_secret(tt["api_key"], "tautulli/api_key"),
            verify_ssl=bool(tt.get("verify_ssl", False)),
        )

    # Seerr (Overseerr / Jellyseerr)
    seerr: SeerrConfig | None = None
    if "seerr" in raw:
        sr2 = raw["seerr"]
        seerr = SeerrConfig(
            host=sr2["host"],
            port=int(sr2.get("port", 443)),
            https=bool(sr2.get("https", True)),
            api_key=_resolve_secret(sr2["api_key"], "seerr/api_key"),
            verify_ssl=bool(sr2.get("verify_ssl", False)),
        )

    # OpenSpeedTest
    openspeedtest: OpenSpeedTestConfig | None = None
    if "openspeedtest" in raw:
        ost = raw["openspeedtest"]
        openspeedtest = OpenSpeedTestConfig(
            host=ost["host"],
            port=int(ost.get("port", 3000)),
            https=bool(ost.get("https", False)),
            verify_ssl=bool(ost.get("verify_ssl", False)),
        )

    # TLS cert checks
    tls_checks = []
    for t in raw.get("tls", []):
        tls_checks.append(TLSCheckConfig(
            name=t["name"],
            host=t["host"],
            port=int(t.get("port", 443)),
            sni=t.get("sni"),
            verify_ssl=bool(t.get("verify_ssl", True)),
        ))

    # Portainer
    portainer: PortainerConfig | None = None
    if "portainer" in raw:
        pt = raw["portainer"]
        portainer = PortainerConfig(
            host=pt["host"],
            port=int(pt.get("port", 9000)),
            https=bool(pt.get("https", False)),
            api_key=_resolve_secret(pt["api_key"], "portainer/api_key"),
            verify_ssl=bool(pt.get("verify_ssl", False)),
        )

    # Uptime Kuma
    uptimekuma: UptimeKumaConfig | None = None
    if "uptimekuma" in raw:
        uk = raw["uptimekuma"]
        uptimekuma = UptimeKumaConfig(
            host=uk["host"],
            port=int(uk.get("port", 3001)),
            slug=uk.get("slug", "default"),
            https=bool(uk.get("https", False)),
            verify_ssl=bool(uk.get("verify_ssl", False)),
            api_key=_resolve_secret(uk.get("api_key", ""), "uptimekuma/api_key") if uk.get("api_key") else "",
        )

    # Prometheus
    prometheus: PrometheusConfig | None = None
    if "prometheus" in raw:
        pm = raw["prometheus"]
        prometheus = PrometheusConfig(
            host=pm["host"],
            port=int(pm.get("port", 9090)),
            https=bool(pm.get("https", False)),
            verify_ssl=bool(pm.get("verify_ssl", False)),
        )

    # UniFi
    unifi: UniFiConfig | None = None
    if "unifi" in raw:
        uf = raw["unifi"]
        unifi = UniFiConfig(
            host=uf["host"],
            port=int(uf.get("port", 443)),
            https=bool(uf.get("https", True)),
            udm=bool(uf.get("udm", True)),
            site=uf.get("site", "default"),
            verify_ssl=bool(uf.get("verify_ssl", False)),
            api_key=_resolve_secret(uf["api_key"], "unifi/api_key") if uf.get("api_key") else "",
            username=uf.get("username", ""),
            password=_resolve_secret(uf["password"], "unifi/password") if uf.get("password") else "",
        )

    # Blackbox
    bb_raw = raw.get("blackbox", {})
    blackbox = BlackboxConfig(
        ssh_hosts=[
            BlackboxSshHost(
                name=h["name"],
                host=h["host"],
                user=h.get("user", ""),
            )
            for h in bb_raw.get("ssh_hosts", [])
        ],
    )

    # Docker Hub (optional — needed for authenticated tag-list API calls)
    docker_hub: DockerHubConfig | None = None
    if "docker_hub" in raw:
        dh = raw["docker_hub"]
        docker_hub = DockerHubConfig(
            username=dh["username"],
            token=_resolve_secret(dh["token"], "docker_hub/token"),
        )

    return Config(
        proxmox=proxmox,
        ssh=ssh,
        beszel=beszel,
        pihole_instances=pihole_instances,
        ha=ha,
        plex=plex,
        radarr=radarr,
        sonarr=sonarr,
        tautulli=tautulli,
        seerr=seerr,
        openspeedtest=openspeedtest,
        tls_checks=tls_checks,
        uptimekuma=uptimekuma,
        portainer=portainer,
        prometheus=prometheus,
        unifi=unifi,
        blackbox=blackbox,
        docker_hub=docker_hub,
        scripts_dir=raw.get("scripts_dir", "../../"),
        refresh_interval=int(raw.get("refresh_interval", 30)),
        config_path=config_path.resolve(),
    )


# ── Guest user persistence ────────────────────────────────────────────────────

def save_guest_user(config_path: Path, guest_name: str, user: str) -> None:
    """Persist a guest→user mapping to the guest_users.yaml sidecar file."""
    sidecar = config_path.parent / "guest_users.yaml"
    data: dict[str, str] = {}
    if sidecar.exists():
        with open(sidecar) as f:
            data = yaml.safe_load(f) or {}
    data[guest_name] = user
    with open(sidecar, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


def save_guest_update_cmd(config_path: Path, guest_name: str, cmd: str, headless: bool = False) -> None:
    """Persist a guest→update-command mapping to the guest_update_cmds.yaml sidecar file."""
    sidecar = config_path.parent / "guest_update_cmds.yaml"
    data: dict = {}
    if sidecar.exists():
        with open(sidecar) as f:
            data = yaml.safe_load(f) or {}
    data[guest_name] = {"cmd": cmd, "headless": headless}
    with open(sidecar, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


def remove_guest_update_cmd(config_path: Path, guest_name: str) -> bool:
    """Remove a guest's custom update command from the sidecar file.

    Returns True if the entry existed and was removed, False if it wasn't present.
    """
    sidecar = config_path.parent / "guest_update_cmds.yaml"
    if not sidecar.exists():
        return False
    with open(sidecar) as f:
        data = yaml.safe_load(f) or {}
    if guest_name not in data:
        return False
    del data[guest_name]
    with open(sidecar, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    return True


# ── Keychain management helpers (used by `atc secrets`) ──────────────────────

def secrets_list() -> list[str]:
    """Return all keys stored under the atc service."""
    # keyring doesn't have a list-all API on all backends,
    # so we read from a small index we maintain ourselves.
    index_key = "__index__"
    raw = keyring.get_password(KEYCHAIN_SERVICE, index_key) or ""
    return sorted(k for k in raw.split(",") if k)


def _index_add(key: str) -> None:
    index_key = "__index__"
    raw = keyring.get_password(KEYCHAIN_SERVICE, index_key) or ""
    keys = set(k for k in raw.split(",") if k)
    keys.add(key)
    keyring.set_password(KEYCHAIN_SERVICE, index_key, ",".join(sorted(keys)))


def _index_remove(key: str) -> None:
    index_key = "__index__"
    raw = keyring.get_password(KEYCHAIN_SERVICE, index_key) or ""
    keys = set(k for k in raw.split(",") if k)
    keys.discard(key)
    keyring.set_password(KEYCHAIN_SERVICE, index_key, ",".join(sorted(keys)))


def secret_set(key: str, value: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, key, value)
    _index_add(key)


def secret_delete(key: str) -> bool:
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, key)
        _index_remove(key)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


def secret_verify(key: str, candidate: str) -> bool:
    """Return True iff candidate matches the stored secret, compared via SHA-256 hashes.

    Never exposes or returns the stored plaintext value.
    Uses hmac.compare_digest to prevent timing-based side-channel attacks.
    """
    import hashlib
    import hmac as _hmac
    stored = keyring.get_password(KEYCHAIN_SERVICE, key)
    if stored is None:
        return False
    stored_hash = hashlib.sha256(stored.encode()).digest()
    candidate_hash = hashlib.sha256(candidate.encode()).digest()
    return _hmac.compare_digest(stored_hash, candidate_hash)


def _find_keychain_refs(obj: object) -> set[str]:
    """Recursively find all keychain:<key> sentinel values in a parsed YAML object."""
    if isinstance(obj, str) and obj.startswith(KEYCHAIN_PREFIX):
        return {obj[len(KEYCHAIN_PREFIX):]}
    if isinstance(obj, dict):
        result: set[str] = set()
        for v in obj.values():
            result |= _find_keychain_refs(v)
        return result
    if isinstance(obj, list):
        result = set()
        for item in obj:
            result |= _find_keychain_refs(item)
        return result
    return set()


def secrets_scan_config(config_path: Path | None = None) -> tuple[list[str], list[str]]:
    """Scan config.yaml for keychain: sentinels and repair the index for any found in the keychain.

    Returns (repaired, missing):
      repaired — keys that were in the keychain but missing from the index (now added)
      missing  — keys referenced in config.yaml but not yet stored in the keychain
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config.yaml"

    with open(config_path) as f:
        data = yaml.safe_load(f)

    found_keys = sorted(_find_keychain_refs(data))

    index_key = "__index__"
    raw = keyring.get_password(KEYCHAIN_SERVICE, index_key) or ""
    indexed = {k for k in raw.split(",") if k}

    repaired: list[str] = []
    missing: list[str] = []
    for key in found_keys:
        if keyring.get_password(KEYCHAIN_SERVICE, key) is not None:
            if key not in indexed:
                _index_add(key)
                repaired.append(key)
        else:
            missing.append(key)

    return repaired, missing


def secret_repair(key: str, candidate: str) -> bool:
    """Verify candidate matches the stored secret and, if so, add key to the index.

    Returns True if the hash matched (and the index was repaired), False otherwise.
    Never exposes or returns the stored plaintext value.
    """
    if not secret_verify(key, candidate):
        return False
    _index_add(key)
    return True
