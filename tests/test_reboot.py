"""Tests for lib/reboot.py: reboot-needed detection after apt updates."""
from __future__ import annotations

import subprocess

from lib.reboot import REBOOT_CHECK_CMD, parse_reboot_check


def test_proxmox_new_kernel_installed_no_flag():
    out = "RUN:7.0.0-3-pve\nNEW:7.0.14-29-pve\n"
    assert parse_reboot_check(out) == "kernel 7.0.0-3-pve running, 7.0.14-29-pve installed"


def test_natural_version_order_not_lexical():
    # lexically "7.0.14-9" > "7.0.14-29"; naturally it is older
    assert parse_reboot_check("RUN:7.0.14-29-pve\nNEW:7.0.14-9-pve\n") == ""


def test_running_newest_is_clean():
    assert parse_reboot_check("RUN:6.8.0-149-generic\nNEW:6.8.0-149-generic\n") == ""


def test_ubuntu_flag_with_pkgs():
    out = "FLAG:linux-image-6.8.0-150-generic linux-base \nRUN:6.8.0-149-generic\nNEW:6.8.0-150-generic\n"
    r = parse_reboot_check(out)
    assert "6.8.0-150-generic installed" in r
    assert "reboot-required flag (linux-image-6.8.0-150-generic, linux-base)" in r


def test_flag_only_libc():
    assert parse_reboot_check("FLAG:libc6 \nRUN:a\nNEW:a\n") == "reboot-required flag (libc6)"


def test_empty_or_ssh_failure_is_falsy():
    assert parse_reboot_check("") == ""
    assert parse_reboot_check("RUN:6.8.0-149-generic\nNEW:\n") == ""


def test_command_runs_locally_and_parses():
    out = subprocess.run(["sh", "-c", REBOOT_CHECK_CMD], capture_output=True, text=True).stdout
    assert "RUN:" in out and "NEW:" in out
    assert isinstance(parse_reboot_check(out), str)
