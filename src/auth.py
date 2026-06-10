"""PIN-based authentication scoped to the current terminal session.

Flow
----
- If no PIN is configured: all invocations pass through silently.
- If a valid session file for this terminal session exists: pass through.
- Otherwise: prompt for PIN, verify against keychain, create a session marker.

Session identity
----------------
The session ID (os.getsid) is the PID of the session leader (typically the
shell).  It is identical for every process spawned in the same terminal and
changes when a new terminal / SSH session is opened — so auth is prompted
exactly once per terminal session, with no arbitrary time limit.

The session marker lives at /tmp/atc-session-<user>-<sid> (chmod 600).
It contains only the literal string "ok".  No secrets are written to /tmp.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac as _hmac
import os
import sys
from pathlib import Path

_KEYCHAIN_SERVICE = "atc"
_PIN_KEY = "atc-pin"


def _session_file() -> Path:
    user = os.getenv("USER", "default")
    sid = os.getsid(os.getpid())
    return Path(f"/tmp/atc-session-{user}-{sid}")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _kr():
    import keyring
    return keyring


def _verify_pin(candidate: str) -> bool:
    stored = _kr().get_password(_KEYCHAIN_SERVICE, _PIN_KEY)
    if stored is None:
        return False
    return _hmac.compare_digest(
        hashlib.sha256(stored.encode()).digest(),
        hashlib.sha256(candidate.encode()).digest(),
    )


def _session_valid() -> bool:
    try:
        return _session_file().read_text().strip() == "ok"
    except Exception:
        return False


def _create_session() -> None:
    sf = _session_file()
    sf.write_text("ok")
    sf.chmod(0o600)


# ── Public API ────────────────────────────────────────────────────────────────

def has_pin() -> bool:
    """Return True if a PIN has been configured."""
    return _kr().get_password(_KEYCHAIN_SERVICE, _PIN_KEY) is not None


def require_auth() -> None:
    """Gate every atc invocation behind a PIN.

    No-op if no PIN is configured.  Exits with code 1 on wrong PIN.
    """
    if not has_pin():
        return
    if _session_valid():
        return
    try:
        pin = getpass.getpass("🔒  atc PIN: ")
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)
    if not _verify_pin(pin):
        print("Incorrect PIN.")
        sys.exit(1)
    _create_session()


def cmd_set_pin() -> None:
    """Interactively set or change the PIN (called from the CLI layer)."""
    try:
        pin = getpass.getpass("New PIN: ")
        if not pin:
            print("PIN cannot be empty.")
            sys.exit(1)
        confirm = getpass.getpass("Confirm PIN: ")
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)
    if pin != confirm:
        print("PINs do not match.")
        sys.exit(1)
    _kr().set_password(_KEYCHAIN_SERVICE, _PIN_KEY, pin)
    from config import _index_add
    _index_add(_PIN_KEY)
    # Invalidate any existing session so the new PIN takes effect immediately.
    _session_file().unlink(missing_ok=True)
    print("✅  PIN set. You'll be prompted once per terminal session.")


def cmd_clear_pin() -> None:
    """Remove the PIN, disabling auth (called from the CLI layer)."""
    if not has_pin():
        print("No PIN is configured.")
        return
    try:
        _kr().delete_password(_KEYCHAIN_SERVICE, _PIN_KEY)
    except Exception:
        pass
    from config import _index_remove
    _index_remove(_PIN_KEY)
    _session_file().unlink(missing_ok=True)
    print("✅  PIN removed. Auth disabled.")
