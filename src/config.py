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

    keyring.set_password(KEYCHAIN_SERVICE, key_name, value)
    print(f"    ✅  Stored in keychain as {key_name!r}\n")
    return value


@dataclass
class ProxmoxClusterConfig:
    host: str
    user: str
    token_name: str
    token_value: str
    port: int = 8006
    verify_ssl: bool = False


@dataclass
class SSHConfig:
    default_user: str = "root"
    key_file: str = "~/.ssh/id_rsa"
    connect_timeout: int = 10
    node_host_pattern: str = "{name}"  # e.g. "pve-{name}.local.example.com"

    @property
    def expanded_key_file(self) -> str:
        return os.path.expanduser(self.key_file)

    def node_host(self, node_name: str) -> str:
        return self.node_host_pattern.format(name=node_name)


@dataclass
class DSMConfig:
    name: str
    host: str
    user: str
    password: str
    port: int = 5000
    https: bool = False
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
class TLSCheckConfig:
    name: str
    host: str
    port: int = 443
    sni: str | None = None  # SNI override — useful for wildcard certs on internal IPs
    verify_ssl: bool = True


@dataclass
class Config:
    proxmox: ProxmoxClusterConfig = field(default_factory=lambda: ProxmoxClusterConfig("", "", "", ""))
    ssh: SSHConfig = field(default_factory=SSHConfig)
    dsm_instances: list[DSMConfig] = field(default_factory=list)
    pihole_instances: list[PiholeInstanceConfig] = field(default_factory=list)
    ha: HAConfig | None = None
    seerr: SeerrConfig | None = None
    plex: PlexConfig | None = None
    radarr: ArrConfig | None = None
    sonarr: ArrConfig | None = None
    tls_checks: list[TLSCheckConfig] = field(default_factory=list)
    scripts_dir: str = "../../"
    refresh_interval: int = 30
    config_path: Path = field(default_factory=Path)

    @property
    def resolved_scripts_dir(self) -> Path:
        base = self.config_path.parent if self.config_path else Path.cwd()
        return (base / self.scripts_dir).resolve()


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

    # Proxmox cluster (single connection, cluster API returns all nodes)
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
    ssh = SSHConfig(
        default_user=ssh_raw.get("default_user", "root"),
        key_file=ssh_raw.get("key_file", "~/.ssh/id_rsa"),
        connect_timeout=int(ssh_raw.get("connect_timeout", 10)),
        node_host_pattern=ssh_raw.get("node_host_pattern", "{name}"),
    )

    # DSM
    dsm_instances = []
    for d in raw.get("dsm", []):
        dsm_name = d["name"]
        dsm_instances.append(DSMConfig(
            name=dsm_name,
            host=d["host"],
            port=int(d.get("port", 5000)),
            https=bool(d.get("https", False)),
            user=d["user"],
            password=_resolve_secret(d["password"], f"dsm/{dsm_name}/password"),
            verify_ssl=bool(d.get("verify_ssl", False)),
        ))

    # Pi-hole instances
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
        ha_stats = [
            HAStatConfig(
                name=s["name"],
                entity=s["entity"],
                unit=s.get("unit", ""),
            )
            for s in ha_raw.get("stats", [])
        ]
        ha = HAConfig(
            host=ha_raw["host"],
            port=int(ha_raw.get("port", 8123)),
            https=bool(ha_raw.get("https", False)),
            token=_resolve_secret(ha_raw["token"], "homeassistant/token"),
            verify_ssl=bool(ha_raw.get("verify_ssl", False)),
            stats=ha_stats,
        )

    # Seerr
    seerr: SeerrConfig | None = None
    if "seerr" in raw:
        sr = raw["seerr"]
        seerr = SeerrConfig(
            host=sr["host"],
            port=int(sr.get("port", 443)),
            https=bool(sr.get("https", True)),
            api_key=_resolve_secret(sr["api_key"], "seerr/api_key"),
            verify_ssl=bool(sr.get("verify_ssl", False)),
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

    # Radarr / Sonarr
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

    sonarr: ArrConfig | None = None
    if "sonarr" in raw:
        sr2 = raw["sonarr"]
        sonarr = ArrConfig(
            name="sonarr",
            host=sr2["host"],
            port=int(sr2.get("port", 8989)),
            base_path=sr2.get("base_path", ""),
            https=bool(sr2.get("https", False)),
            api_key=_resolve_secret(sr2["api_key"], "sonarr/api_key"),
            verify_ssl=bool(sr2.get("verify_ssl", False)),
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

    return Config(
        proxmox=proxmox,
        ssh=ssh,
        dsm_instances=dsm_instances,
        pihole_instances=pihole_instances,
        ha=ha,
        seerr=seerr,
        plex=plex,
        radarr=radarr,
        sonarr=sonarr,
        tls_checks=tls_checks,
        scripts_dir=raw.get("scripts_dir", "../../"),
        refresh_interval=int(raw.get("refresh_interval", 30)),
        config_path=config_path.resolve(),
    )


# ── Keychain management helpers (used by `atc secrets`) ──────────────────────

def secrets_list() -> list[tuple[str, str]]:
    """Return all (key, masked_value) pairs stored under the atc service."""
    # keyring doesn't have a list-all API on all backends,
    # so we read from a small index we maintain ourselves.
    index_key = "__index__"
    raw = keyring.get_password(KEYCHAIN_SERVICE, index_key) or ""
    keys = [k for k in raw.split(",") if k]
    result = []
    for k in sorted(keys):
        val = keyring.get_password(KEYCHAIN_SERVICE, k) or ""
        masked = val[:2] + "***" + val[-2:] if len(val) > 4 else "***"
        result.append((k, masked))
    return result


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


def secret_get(key: str) -> str | None:
    return keyring.get_password(KEYCHAIN_SERVICE, key)
