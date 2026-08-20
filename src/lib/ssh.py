"""SSH helpers for running commands on VMs and LXCs via asyncssh."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncssh

from config import SSHConfig


@dataclass
class CommandResult:
    host: str
    name: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0


async def run_command(
    host: str,
    command: str,
    ssh_cfg: SSHConfig,
    user: str | None = None,
) -> CommandResult:
    """Run a single command on a remote host and return the full output."""
    effective_user = user or ssh_cfg.default_user
    known_hosts = None if not ssh_cfg.verify_host_keys else asyncssh.read_known_hosts("~/.ssh/known_hosts")
    try:
        async with asyncssh.connect(
            host,
            username=effective_user,
            known_hosts=known_hosts,
            connect_timeout=ssh_cfg.connect_timeout,
        ) as conn:
            result = await conn.run(command, check=False)
            return CommandResult(
                host=host,
                name=host,
                exit_code=result.exit_status or 0,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
    except Exception as e:
        return CommandResult(
            host=host,
            name=host,
            exit_code=1,
            stdout="",
            stderr=f"SSH connection failed: {e}",
        )


async def stream_command(
    host: str,
    command: str,
    ssh_cfg: SSHConfig,
    user: str | None = None,
) -> AsyncIterator[str]:
    """Stream command output line by line from a remote host."""
    effective_user = user or ssh_cfg.default_user
    known_hosts = None if not ssh_cfg.verify_host_keys else asyncssh.read_known_hosts("~/.ssh/known_hosts")
    try:
        async with asyncssh.connect(
            host,
            username=effective_user,
            known_hosts=known_hosts,
            connect_timeout=ssh_cfg.connect_timeout,
        ) as conn:
            async with conn.create_process(command) as process:
                async for line in process.stdout:
                    yield line.rstrip("\n")
                await process.wait()
    except Exception as e:
        yield f"[ERROR] SSH to {host} failed: {e}"


UPDATE_COMMAND = (
    "DEBIAN_FRONTEND=noninteractive apt-get update -q && "
    "DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -q && "
    "apt-get autoremove -y -q"
)

# Simulate only — no packages are installed. Used for the dry-run confirmation gate.
DRY_RUN_COMMAND = (
    "DEBIAN_FRONTEND=noninteractive apt-get update -q 2>&1 && "
    "apt-get full-upgrade --dry-run -q 2>&1"
)


@dataclass
class UpdateTarget:
    name: str       # display name (e.g. "viggen")
    host: str       # IP or hostname to SSH into
    user: str = "root"
    update_cmd: str | None = None       # custom post-apt command
    update_cmd_headless: bool = False   # if True, stream output; if False, hand off PTY


async def run_dry_run(
    target: UpdateTarget,
    ssh_cfg: SSHConfig,
    on_line: callable | None = None,
) -> CommandResult:
    """Simulate apt upgrade — show what would change without installing anything."""
    lines: list[str] = []
    async for line in stream_command(target.host, DRY_RUN_COMMAND, ssh_cfg, user=target.user):
        lines.append(line)
        if on_line:
            on_line(line)
    full_output = "\n".join(lines)
    failed = any(kw in full_output.lower() for kw in ["error:", "e: "])
    return CommandResult(
        host=target.host,
        name=target.name,
        exit_code=0 if not failed else 1,
        stdout=full_output,
        stderr="",
    )


async def run_update(
    target: UpdateTarget,
    ssh_cfg: SSHConfig,
    on_line: callable | None = None,
) -> CommandResult:
    """Run apt-get update+upgrade on a target, optionally calling on_line(str) per output line."""
    lines: list[str] = []
    async for line in stream_command(target.host, UPDATE_COMMAND, ssh_cfg, user=target.user):
        lines.append(line)
        if on_line:
            on_line(line)

    # Determine success from last few lines (apt returns 0 on success)
    full_output = "\n".join(lines)
    failed = any(kw in full_output.lower() for kw in ["error:", "failed", "e: "])
    return CommandResult(
        host=target.host,
        name=target.name,
        exit_code=0 if not failed else 1,
        stdout=full_output,
        stderr="",
    )


async def run_updates_parallel(
    targets: list[UpdateTarget],
    ssh_cfg: SSHConfig,
    on_line: callable | None = None,
) -> list[CommandResult]:
    """Run apt updates on multiple targets concurrently."""
    tasks = [run_update(t, ssh_cfg, on_line=on_line) for t in targets]
    return list(await asyncio.gather(*tasks))


async def check_ssh_reachable(host: str, ssh_cfg: SSHConfig, user: str | None = None) -> bool:
    """Quick SSH connectivity check."""
    result = await run_command(host, "echo ok", ssh_cfg, user=user)
    return result.success and "ok" in result.stdout
