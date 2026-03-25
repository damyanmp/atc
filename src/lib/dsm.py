"""Synology DSM HTTP API wrapper — basic health checks only."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from config import DSMConfig, KEYCHAIN_SERVICE

# DSM session identifier — groups all requests from this tool in DSM logs
_SESSION = KEYCHAIN_SERVICE

# DSM error code 403 = 2FA required
_ERR_2FA = 403


@dataclass
class DSMStatus:
    name: str
    host: str
    online: bool
    model: str = ""
    dsmVersion: str = ""
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    volumes: list[dict] = field(default_factory=list)   # [{name, used_gb, total_gb, percent}]
    error: str = ""


async def _login(client: httpx.AsyncClient, cfg: DSMConfig, otp_code: str = "") -> str | None:
    """Authenticate and return a session ID (sid).

    If the account has 2FA enabled, DSM returns error 403 with a challenge.
    Pass otp_code to complete the second factor via SYNO.API.Auth v6.
    """
    params: dict = {
        "api": "SYNO.API.Auth",
        "method": "login",
        "account": cfg.user,
        "passwd": cfg.password,
        "session": _SESSION,
        "format": "sid",
    }
    if otp_code:
        params["version"] = "6"
        params["otp_code"] = otp_code
    else:
        params["version"] = "3"

    resp = await client.get(
        f"{cfg.base_url}/webapi/auth.cgi",
        params=params,
        timeout=10,
    )
    data = resp.json()
    if data.get("success"):
        return data["data"]["sid"]

    # 2FA challenge — caller must retry with OTP
    if data.get("error", {}).get("code") == _ERR_2FA:
        return None

    return None


def _prompt_otp(dsm_name: str) -> str:
    """Prompt the user for a TOTP code on stdout/stdin."""
    import sys
    print(f"\n🔐  {dsm_name} requires 2FA — enter your authenticator code:", file=sys.stderr)
    try:
        code = input("    OTP: ").strip()
    except (KeyboardInterrupt, EOFError):
        return ""
    return code


async def _logout(client: httpx.AsyncClient, cfg: DSMConfig, sid: str) -> None:
    try:
        await client.get(
            f"{cfg.base_url}/webapi/auth.cgi",
            params={
                "api": "SYNO.API.Auth",
                "version": "1",
                "method": "logout",
                "session": _SESSION,
                "_sid": sid,
            },
            timeout=5,
        )
    except Exception:
        pass


async def fetch_dsm_status(cfg: DSMConfig, no_prompt: bool = False) -> DSMStatus:
    """Fetch basic DSM health: model, version, CPU, memory, volumes."""
    try:
        async with httpx.AsyncClient(verify=cfg.verify_ssl) as client:
            sid = await _login(client, cfg)
            if not sid:
                # 2FA required — in TUI mode we can't block on stdin
                if no_prompt:
                    return DSMStatus(name=cfg.name, host=cfg.host, online=False, error="2FA required — run: atc dsm status")
                # CLI mode: prompt interactively (runs in thread to allow blocking input)
                import asyncio
                otp = await asyncio.get_event_loop().run_in_executor(
                    None, _prompt_otp, cfg.name
                )
                if not otp:
                    return DSMStatus(name=cfg.name, host=cfg.host, online=False, error="2FA aborted")
                sid = await _login(client, cfg, otp_code=otp)
            if not sid:
                return DSMStatus(name=cfg.name, host=cfg.host, online=False, error="Auth failed")

            try:
                # System info
                info_resp = await client.get(
                    f"{cfg.base_url}/webapi/entry.cgi",
                    params={
                        "api": "SYNO.Core.System",
                        "version": "1",
                        "method": "info",
                        "_sid": sid,
                    },
                    timeout=10,
                )
                info = info_resp.json().get("data", {})

                # Storage / volumes
                storage_resp = await client.get(
                    f"{cfg.base_url}/webapi/entry.cgi",
                    params={
                        "api": "SYNO.Storage.CGI.Storage",
                        "version": "1",
                        "method": "load_info",
                        "_sid": sid,
                    },
                    timeout=10,
                )
                storage_data = storage_resp.json().get("data", {})

                volumes = []
                for vol in storage_data.get("volumes", []):
                    total = int(vol.get("size", {}).get("total", 0))
                    used = int(vol.get("size", {}).get("used", 0))
                    if total > 0:
                        volumes.append({
                            "name": vol.get("id", "unknown"),
                            "used_gb": round(used / (1024 ** 3), 1),
                            "total_gb": round(total / (1024 ** 3), 1),
                            "percent": round(used / total * 100, 1),
                        })

                # Utilization
                util_resp = await client.get(
                    f"{cfg.base_url}/webapi/entry.cgi",
                    params={
                        "api": "SYNO.Core.System.Utilization",
                        "version": "1",
                        "method": "get",
                        "_sid": sid,
                    },
                    timeout=10,
                )
                util = util_resp.json().get("data", {})
                cpu_percent = float(util.get("cpu", {}).get("user_load", 0)) + \
                              float(util.get("cpu", {}).get("system_load", 0))
                mem_total = int(util.get("memory", {}).get("real_total", 1))
                mem_avail = int(util.get("memory", {}).get("avail_real", mem_total))
                mem_percent = round((1 - mem_avail / mem_total) * 100, 1) if mem_total > 0 else 0.0

                return DSMStatus(
                    name=cfg.name,
                    host=cfg.host,
                    online=True,
                    model=info.get("model", ""),
                    dsmVersion=info.get("version_string", ""),
                    cpu_percent=round(cpu_percent, 1),
                    mem_percent=mem_percent,
                    volumes=volumes,
                )
            finally:
                await _logout(client, cfg, sid)

    except Exception as e:
        return DSMStatus(name=cfg.name, host=cfg.host, online=False, error=str(e))


async def fetch_all_dsm(cfgs: list[DSMConfig], no_prompt: bool = False) -> list[DSMStatus]:
    return list(await asyncio.gather(*[fetch_dsm_status(cfg, no_prompt=no_prompt) for cfg in cfgs]))
