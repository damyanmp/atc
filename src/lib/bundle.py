"""Encrypted export/import bundle for moving an atc install to a new machine.

`atc export` packs every keychain secret atc knows about (the `__index__`
list, the local_only flags, the auth PIN) plus the config files that live in
the config dir (config.yaml and its sidecars) into ONE passphrase-encrypted
file. `atc import` reverses it on the new machine, writing secrets into
whatever keyring backend is active there (macOS Keychain, or the encrypted
cryptfile backend on Linux).

Wire format (single ASCII line, safe to AirDrop/scp):

    "ATCBUNDLE1:" + base64( salt(16) + nonce(12) + ciphertext_and_tag )

Key derivation: scrypt(passphrase, salt, n=2**17, r=8, p=1, length=32).
Cipher: AES-256-GCM with the "ATCBUNDLE1" prefix bound as associated data.
Same KDF/cipher pair as mobile_qr.py, with a higher scrypt cost because
this file holds every secret, not a subset, and it is only derived once.

The plaintext is JSON and never touches disk; there is no unencrypted mode.
Runtime state (acars_state.json, search_state.json) and the Linux keyring
master key (keyring.key) are deliberately NOT exported: state rebuilds
itself, and the new machine gets its own keyring backend.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

PREFIX = "ATCBUNDLE1:"
_AAD = b"ATCBUNDLE1"
_SCRYPT_N = 2 ** 17
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_LEN = 16
_NONCE_LEN = 12
FORMAT_VERSION = 1
MIN_PASSPHRASE_LEN = 12

# Files in the config dir that make up an install. Order = display order.
CONFIG_FILES = ("config.yaml", "guest_users.yaml", "guest_update_cmds.yaml")

# Reserved keyring entries that aren't in the user-facing index.
INDEX_KEY = "__index__"
LOCAL_ONLY_KEY = "__local_only__"
PIN_KEY = "atc-pin"

_KEYCHAIN_REF = re.compile(r"keychain:([A-Za-z0-9_.\-]+)")


class BundleError(Exception):
    """Wrong passphrase, corrupt file, or unsupported version."""


# ── crypto ────────────────────────────────────────────────────────────────────

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt(plaintext: bytes, passphrase: str) -> str:
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_derive_key(passphrase, salt)).encrypt(nonce, plaintext, _AAD)
    return PREFIX + base64.b64encode(salt + nonce + ct).decode("ascii")


def decrypt(encoded: str, passphrase: str) -> bytes:
    encoded = encoded.strip()
    if not encoded.startswith(PREFIX):
        raise BundleError("Not an atc export bundle (missing ATCBUNDLE1 header).")
    try:
        blob = base64.b64decode(encoded[len(PREFIX):], validate=True)
    except ValueError as e:
        raise BundleError("Bundle is corrupt (bad base64).") from e
    if len(blob) < _SALT_LEN + _NONCE_LEN + 16:
        raise BundleError("Bundle is truncated.")
    salt, nonce, ct = blob[:_SALT_LEN], blob[_SALT_LEN:_SALT_LEN + _NONCE_LEN], blob[_SALT_LEN + _NONCE_LEN:]
    try:
        return AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, ct, _AAD)
    except Exception as e:  # InvalidTag
        raise BundleError("Wrong passphrase, or the file was modified.") from e


# ── payload ───────────────────────────────────────────────────────────────────

@dataclass
class Bundle:
    secrets: dict[str, str] = field(default_factory=dict)
    local_only: list[str] = field(default_factory=list)
    pin: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    source_host: str = ""
    created: str = ""
    version: int = FORMAT_VERSION

    def to_json(self) -> bytes:
        return json.dumps({
            "version": self.version,
            "created": self.created,
            "source_host": self.source_host,
            "secrets": self.secrets,
            "local_only": sorted(self.local_only),
            "pin": self.pin,
            "files": self.files,
        }).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> Bundle:
        try:
            d = json.loads(raw)
        except ValueError as e:
            raise BundleError("Decrypted payload is not valid JSON.") from e
        if d.get("version") != FORMAT_VERSION:
            raise BundleError(f"Unsupported bundle version {d.get('version')!r} (this atc reads {FORMAT_VERSION}).")
        return cls(
            secrets=dict(d.get("secrets") or {}),
            local_only=list(d.get("local_only") or []),
            pin=d.get("pin"),
            files=dict(d.get("files") or {}),
            source_host=d.get("source_host", ""),
            created=d.get("created", ""),
        )

    def referenced_keys(self) -> set[str]:
        """keychain:<key> references found in the bundled config.yaml."""
        return set(_KEYCHAIN_REF.findall(self.files.get("config.yaml", "")))


def seal(bundle: Bundle, passphrase: str) -> str:
    return encrypt(bundle.to_json(), passphrase)


def unseal(encoded: str, passphrase: str) -> Bundle:
    return Bundle.from_json(decrypt(encoded, passphrase))


# ── collect (export side) ─────────────────────────────────────────────────────

def _split(raw: str | None) -> list[str]:
    return sorted(k for k in (raw or "").split(",") if k)


def collect(kr, service: str, config_dir: Path) -> tuple[Bundle, list[str]]:
    """Read everything from keyring `kr` + `config_dir` into a Bundle.

    Returns (bundle, missing) where `missing` are keys that config.yaml
    references (or the index lists) but have no value in the keyring.
    """
    b = Bundle(
        source_host=socket.gethostname(),
        created=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    for name in CONFIG_FILES:
        p = config_dir / name
        if p.is_file():
            b.files[name] = p.read_text()

    wanted = set(_split(kr.get_password(service, INDEX_KEY))) | b.referenced_keys()
    wanted -= {INDEX_KEY, LOCAL_ONLY_KEY, PIN_KEY}
    missing: list[str] = []
    for key in sorted(wanted):
        val = kr.get_password(service, key)
        if val is None:
            missing.append(key)
        else:
            b.secrets[key] = val
    b.local_only = [k for k in _split(kr.get_password(service, LOCAL_ONLY_KEY)) if k in b.secrets]
    b.pin = kr.get_password(service, PIN_KEY)
    return b, missing


# ── apply (import side) ───────────────────────────────────────────────────────

@dataclass
class ApplyResult:
    secrets_added: list[str] = field(default_factory=list)
    secrets_same: list[str] = field(default_factory=list)
    secrets_skipped: list[str] = field(default_factory=list)   # differ, not overwritten
    secrets_replaced: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    files_same: list[str] = field(default_factory=list)
    files_backed_up: list[str] = field(default_factory=list)   # backup paths
    pin: str = "none"          # none | added | same | skipped | replaced
    verify_failed: list[str] = field(default_factory=list)


def plan(bundle: Bundle, kr, service: str, config_dir: Path) -> ApplyResult:
    """Dry run: classify what apply() would do. Touches nothing."""
    return _apply(bundle, kr, service, config_dir, overwrite=False, write=False)


def apply(bundle: Bundle, kr, service: str, config_dir: Path, overwrite: bool = False) -> ApplyResult:
    return _apply(bundle, kr, service, config_dir, overwrite=overwrite, write=True)


def _apply(bundle: Bundle, kr, service: str, config_dir: Path, *, overwrite: bool, write: bool) -> ApplyResult:
    r = ApplyResult()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Config files first: a failed secret write later still leaves a usable config.
    if write:
        config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config_dir, 0o700)
    for name, content in bundle.files.items():
        if "/" in name or name not in CONFIG_FILES:
            continue  # never write outside the config dir
        dest = config_dir / name
        if dest.exists() and dest.read_text() == content:
            r.files_same.append(name)
            continue
        if dest.exists():
            bak = dest.with_name(f"{name}.bak-{stamp}")
            r.files_backed_up.append(str(bak))
            if write:
                dest.replace(bak)
        r.files_written.append(name)
        if write:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.chmod(dest, 0o600)

    index = set(_split(kr.get_password(service, INDEX_KEY)))
    local_only = set(_split(kr.get_password(service, LOCAL_ONLY_KEY)))
    touched: list[str] = []
    for key, val in sorted(bundle.secrets.items()):
        cur = kr.get_password(service, key)
        if cur == val:
            r.secrets_same.append(key)
        elif cur is None:
            r.secrets_added.append(key)
            touched.append(key)
        elif overwrite:
            r.secrets_replaced.append(key)
            touched.append(key)
        else:
            r.secrets_skipped.append(key)
            continue
        index.add(key)
        if key in bundle.local_only:
            local_only.add(key)

    if bundle.pin is not None:
        cur = kr.get_password(service, PIN_KEY)
        if cur == bundle.pin:
            r.pin = "same"
        elif cur is None:
            r.pin = "added"
        else:
            r.pin = "replaced" if overwrite else "skipped"

    if not write:
        return r

    for key in touched:
        kr.set_password(service, key, bundle.secrets[key])
    if r.pin in ("added", "replaced"):
        kr.set_password(service, PIN_KEY, bundle.pin)
    kr.set_password(service, INDEX_KEY, ",".join(sorted(index)))
    kr.set_password(service, LOCAL_ONLY_KEY, ",".join(sorted(local_only)))

    # Read every value back; a backend that silently dropped a write shows here.
    for key in touched:
        if kr.get_password(service, key) != bundle.secrets[key]:
            r.verify_failed.append(key)
    if r.pin in ("added", "replaced") and kr.get_password(service, PIN_KEY) != bundle.pin:
        r.verify_failed.append(PIN_KEY)
    return r
