"""Tests for lib/bundle.py — the encrypted `atc export` / `atc import` bundle.

Uses an in-memory fake keyring; never touches the real keychain or ~/.config.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lib import bundle as bn  # noqa: E402

SVC = "atc"
PW = "correct-horse-battery-staple"


class FakeKeyring:
    def __init__(self, data: dict[str, str] | None = None):
        self.data = dict(data or {})

    def get_password(self, service, key):
        assert service == SVC
        return self.data.get(key)

    def set_password(self, service, key, value):
        assert service == SVC
        self.data[key] = value


@pytest.fixture
def src_side(tmp_path):
    cfg = tmp_path / "old"
    cfg.mkdir()
    (cfg / "config.yaml").write_text("plex:\n  token: keychain:plex-token\nha:\n  token: keychain:ha-token\n")
    (cfg / "guest_users.yaml").write_text("pihole: admin\n")
    (cfg / "acars_state.json").write_text("{}")   # runtime state: must NOT export
    (cfg / "keyring.key").write_text("master")     # Linux keyring key: must NOT export
    kr = FakeKeyring({
        "__index__": "plex-token,proxmox-token",
        "__local_only__": "proxmox-token",
        "plex-token": "PLEX-SECRET-111",
        "proxmox-token": "PVE-SECRET-222",
        "atc-pin": "4321",
    })
    return kr, cfg


def test_collect_gathers_index_refs_files_pin(src_side):
    kr, cfg = src_side
    b, missing = bn.collect(kr, SVC, cfg)
    assert b.secrets == {"plex-token": "PLEX-SECRET-111", "proxmox-token": "PVE-SECRET-222"}
    assert missing == ["ha-token"]                      # referenced in config, not stored
    assert b.local_only == ["proxmox-token"]
    assert b.pin == "4321"
    assert set(b.files) == {"config.yaml", "guest_users.yaml"}
    assert "__index__" not in b.secrets


def test_sealed_bundle_has_no_plaintext(src_side):
    kr, cfg = src_side
    b, _ = bn.collect(kr, SVC, cfg)
    sealed = bn.seal(b, PW)
    assert sealed.startswith(bn.PREFIX)
    raw = base64.b64decode(sealed[len(bn.PREFIX):])
    for needle in ("PLEX-SECRET-111", "PVE-SECRET-222", "plex-token", "pihole"):
        assert needle not in sealed
        assert needle.encode() not in raw


def test_wrong_passphrase_and_tamper_rejected(src_side):
    kr, cfg = src_side
    sealed = bn.seal(bn.collect(kr, SVC, cfg)[0], PW)
    with pytest.raises(bn.BundleError, match="passphrase"):
        bn.unseal(sealed, "wrong-passphrase-123")
    blob = bytearray(base64.b64decode(sealed[len(bn.PREFIX):]))
    blob[-1] ^= 1
    with pytest.raises(bn.BundleError):
        bn.unseal(bn.PREFIX + base64.b64encode(bytes(blob)).decode(), PW)
    with pytest.raises(bn.BundleError, match="header"):
        bn.unseal("garbage", PW)


def test_roundtrip_into_empty_machine(src_side, tmp_path):
    kr, cfg = src_side
    b = bn.unseal(bn.seal(bn.collect(kr, SVC, cfg)[0], PW), PW)
    new_kr, new_dir = FakeKeyring(), tmp_path / "new" / "atc"

    p = bn.plan(b, new_kr, SVC, new_dir)
    assert p.secrets_added == ["plex-token", "proxmox-token"]
    assert not new_dir.exists() and new_kr.data == {}   # plan writes nothing

    r = bn.apply(b, new_kr, SVC, new_dir)
    assert not r.verify_failed and r.pin == "added"
    assert new_kr.data["plex-token"] == "PLEX-SECRET-111"
    assert new_kr.data["__index__"] == "plex-token,proxmox-token"
    assert new_kr.data["__local_only__"] == "proxmox-token"
    assert new_kr.data["atc-pin"] == "4321"
    assert (new_dir / "config.yaml").read_text() == (cfg / "config.yaml").read_text()
    assert not (new_dir / "acars_state.json").exists()
    assert not (new_dir / "keyring.key").exists()
    assert oct((new_dir / "config.yaml").stat().st_mode & 0o777) == "0o600"
    assert oct(new_dir.stat().st_mode & 0o777) == "0o700"


def test_existing_values_kept_unless_overwrite(src_side, tmp_path):
    kr, cfg = src_side
    b = bn.collect(kr, SVC, cfg)[0]
    new_dir = tmp_path / "n"
    new_dir.mkdir()
    (new_dir / "config.yaml").write_text("local edits\n")
    new_kr = FakeKeyring({"__index__": "mine", "mine": "keep", "plex-token": "DIFFERENT", "atc-pin": "0000"})

    r = bn.apply(b, new_kr, SVC, new_dir)
    assert r.secrets_skipped == ["plex-token"] and r.pin == "skipped"
    assert new_kr.data["plex-token"] == "DIFFERENT" and new_kr.data["atc-pin"] == "0000"
    assert set(new_kr.data["__index__"].split(",")) == {"mine", "proxmox-token"}
    # differing config file is backed up, not clobbered
    baks = list(new_dir.glob("config.yaml.bak-*"))
    assert len(baks) == 1 and baks[0].read_text() == "local edits\n"

    r2 = bn.apply(b, new_kr, SVC, new_dir, overwrite=True)
    assert r2.secrets_replaced == ["plex-token"] and r2.pin == "replaced"
    assert new_kr.data["plex-token"] == "PLEX-SECRET-111"
    assert r2.files_same == ["config.yaml", "guest_users.yaml"]


def test_rejects_path_traversal_in_files(tmp_path):
    b = bn.Bundle(files={"../evil.yaml": "x", "config.yaml": "ok\n"})
    d = tmp_path / "cfg"
    bn.apply(b, FakeKeyring(), SVC, d)
    assert not (tmp_path / "evil.yaml").exists()
    assert (d / "config.yaml").read_text() == "ok\n"


def test_readback_failure_reported(src_side, tmp_path):
    kr, cfg = src_side

    class Lossy(FakeKeyring):
        def set_password(self, service, key, value):
            if key == "plex-token":
                return  # silently drop, like a broken backend
            super().set_password(service, key, value)

    r = bn.apply(bn.collect(kr, SVC, cfg)[0], Lossy(), SVC, tmp_path / "x")
    assert r.verify_failed == ["plex-token"]
