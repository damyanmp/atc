"""TLS cert deployment to Pi-hole instances via asyncssh.

Mirrors infrastructure/pihole/certs/deploy-pihole-tls.sh.
Cert source: nginx-proxy-manager on Docker-01 (192.168.1.78).
Targets: Globemaster (Synology Docker), Viggen and Draken (Proxmox LXC).
"""
from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass

import asyncssh

_CERT_SOURCE_USER = "dmp"
_CERT_SOURCE_HOST = "192.168.1.78"
_CERT_SOURCE_PATH = "/home/dmp/workspace/nginx-proxy-manager/letsencrypt/live/npm-12"

_GLOBEMASTER_USER = "damyanmp"
_GLOBEMASTER_HOST = "192.168.1.151"
_GLOBEMASTER_PIHOLE_DIR = "/volume1/docker/pihole/pihole"
_GLOBEMASTER_DOCKER = "sudo /var/packages/ContainerManager/target/usr/bin/docker"

OnLine = Callable[[str], None]


@dataclass
class DeployTarget:
    name: str
    host: str
    user: str
    kind: str   # "globemaster" | "lxc"


ALL_TARGETS: list[DeployTarget] = [
    DeployTarget("Globemaster", _GLOBEMASTER_HOST, _GLOBEMASTER_USER, "globemaster"),
    DeployTarget("Viggen",      "192.168.1.232",   "root",            "lxc"),
    DeployTarget("Draken",      "192.168.1.191",   "root",            "lxc"),
]


def _conn(host: str, user: str) -> dict:
    return dict(host=host, username=user, known_hosts=None)


async def fetch_combined_pem(on_line: OnLine) -> bytes:
    """SSH into Docker-01 and return fullchain + privkey as a single combined PEM."""
    on_line(f"  Connecting to Docker-01 ({_CERT_SOURCE_HOST})…")
    async with asyncssh.connect(**_conn(_CERT_SOURCE_HOST, _CERT_SOURCE_USER)) as conn:
        full = await conn.run(f"sudo cat {_CERT_SOURCE_PATH}/fullchain.pem", check=True)
        key  = await conn.run(f"sudo cat {_CERT_SOURCE_PATH}/privkey.pem",   check=True)

    fullchain = (full.stdout or "").encode()
    privkey   = (key.stdout  or "").encode()

    if not fullchain:
        raise RuntimeError("fullchain.pem is empty — check sudo access on Docker-01")
    if not privkey:
        raise RuntimeError("privkey.pem is empty — check sudo access on Docker-01")

    on_line("  Combined PEM ready")
    return fullchain + privkey


async def _deploy_globemaster(pem: bytes, on_line: OnLine) -> bool:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    d = _GLOBEMASTER_PIHOLE_DIR
    docker = _GLOBEMASTER_DOCKER

    on_line(f"  Connecting to Globemaster ({_GLOBEMASTER_HOST})…")
    async with asyncssh.connect(**_conn(_GLOBEMASTER_HOST, _GLOBEMASTER_USER)) as conn:
        await conn.run(
            f'sudo test -f "{d}/tls.pem" '
            f'&& sudo cp "{d}/tls.pem" "{d}/tls.pem.{ts}.bak" '
            f'|| true',
            check=False,
        )
        on_line("  Backed up existing cert")

        # Write to /tmp first (no sudo), then sudo-move into the pihole dir
        tmp = f"/tmp/pihole-tls-{ts}.pem"
        await conn.run(f"cat > {tmp}", input=pem, check=True)
        await conn.run(
            f"sudo mv {tmp} {d}/tls.pem "
            f"&& sudo chmod 644 {d}/tls.pem "
            f"&& sudo chown root:root {d}/tls.pem",
            check=True,
        )
        on_line("  Installed tls.pem")

        r1 = await conn.run(
            f"{docker} exec pihole pihole-FTL --config webserver.tls.cert /etc/pihole/tls.pem",
            check=False,
        )
        on_line(f"  FTL cert: {(r1.stdout or r1.stderr or 'ok').strip()}")

        r2 = await conn.run(
            f"{docker} exec pihole pihole-FTL --config webserver.port '8080,8443s'",
            check=False,
        )
        on_line(f"  FTL port: {(r2.stdout or r2.stderr or 'ok').strip()}")

        await conn.run(f"{docker} restart pihole", check=True)
        on_line("  Restarted pihole container")

    return True


async def _deploy_lxc(target: DeployTarget, pem: bytes, on_line: OnLine) -> bool:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    on_line(f"  Connecting to {target.name} ({target.host})…")
    async with asyncssh.connect(**_conn(target.host, target.user)) as conn:
        await conn.run(
            f"[ -f /etc/pihole/tls.pem ] "
            f"&& cp /etc/pihole/tls.pem /etc/pihole/tls.pem.{ts}.bak "
            f"|| true",
            check=False,
        )
        on_line("  Backed up existing cert")

        # Running as root, so write directly
        await conn.run("cat > /etc/pihole/tls.pem", input=pem, check=True)
        await conn.run(
            "chown root:pihole /etc/pihole/tls.pem && chmod 640 /etc/pihole/tls.pem",
            check=True,
        )
        on_line("  Installed tls.pem")

        r1 = await conn.run(
            "pihole-FTL --config webserver.tls.cert /etc/pihole/tls.pem",
            check=False,
        )
        on_line(f"  FTL cert: {(r1.stdout or r1.stderr or 'ok').strip()}")

        r2 = await conn.run(
            "pihole-FTL --config webserver.port '8080,8443s'",
            check=False,
        )
        on_line(f"  FTL port: {(r2.stdout or r2.stderr or 'ok').strip()}")

        await conn.run("systemctl restart pihole-FTL", check=True)
        on_line("  Restarted pihole-FTL")

    return True


async def deploy_target(target: DeployTarget, pem: bytes, on_line: OnLine) -> bool:
    try:
        if target.kind == "globemaster":
            return await _deploy_globemaster(pem, on_line)
        return await _deploy_lxc(target, pem, on_line)
    except Exception as e:
        on_line(f"  [ERROR] {e}")
        return False
