"""Detect whether a Debian-family host needs a reboot after updates.

Ubuntu writes /var/run/reboot-required after a kernel/libc update. Plain
Debian and Proxmox VE do not, so that flag alone misses every Proxmox node.
We also compare the running kernel with the newest kernel image in /boot.
"""
from __future__ import annotations

import re

# One SSH round-trip, no root needed. Prints tagged lines for parse_reboot_check().
REBOOT_CHECK_CMD = (
    "test -f /var/run/reboot-required && "
    "echo \"FLAG:$(tr '\\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)\"; "
    "echo \"RUN:$(uname -r)\"; "
    "echo \"NEW:$(ls /boot/vmlinuz-* 2>/dev/null | sed 's,^/boot/vmlinuz-,,' | sort -V | tail -1)\""
)


def _vkey(v: str) -> list:
    """Natural sort key matching `sort -V` closely enough for kernel names."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", v)]


def parse_reboot_check(stdout: str) -> str:
    """Return a human reason if a reboot is needed, else "".

    The result is truthy/falsy so callers can treat it as a bool.
    """
    flag = None
    running = newest = ""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("FLAG:"):
            flag = line[5:].strip()
        elif line.startswith("RUN:"):
            running = line[4:].strip()
        elif line.startswith("NEW:"):
            newest = line[4:].strip()

    reasons: list[str] = []
    if running and newest and running != newest and _vkey(newest) > _vkey(running):
        reasons.append(f"kernel {running} running, {newest} installed")
    if flag is not None:
        pkgs = flag.split()
        reasons.append(
            "reboot-required flag" + (f" ({', '.join(pkgs[:4])}{'…' if len(pkgs) > 4 else ''})" if pkgs else "")
        )
    return "; ".join(reasons)
