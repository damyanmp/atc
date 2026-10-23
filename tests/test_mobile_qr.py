"""Regression tests for mobile_qr.py's passphrase-encrypted QR payload.

Does not touch the keychain or config loading — only the standalone
encrypt/decrypt helpers, which is where the actual security property lives.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobile_qr import ATCQR1_PREFIX, decrypt_payload, encrypt_payload  # noqa: E402


def test_roundtrip():
    plaintext = "proxmox:\n  host: example\n  token_value: super-secret-token\n"
    encoded = encrypt_payload(plaintext, "correct-horse-battery-staple")
    assert encoded.startswith(ATCQR1_PREFIX)
    assert decrypt_payload(encoded, "correct-horse-battery-staple") == plaintext


def test_payload_never_contains_plaintext_secret():
    secret = "super-secret-token-xyz123"
    plaintext = f"proxmox:\n  token_value: {secret}\n"
    encoded = encrypt_payload(plaintext, "a-strong-passphrase")
    assert secret not in encoded


def test_wrong_passphrase_rejected():
    plaintext = "plex:\n  token: abc\n"
    encoded = encrypt_payload(plaintext, "right-passphrase")
    with pytest.raises(InvalidTag):
        decrypt_payload(encoded, "wrong-passphrase")


def test_fresh_salt_and_nonce_each_call():
    plaintext = "plex:\n  token: abc\n"
    a = encrypt_payload(plaintext, "same-passphrase")
    b = encrypt_payload(plaintext, "same-passphrase")
    # Same plaintext + same passphrase must never produce identical ciphertext.
    assert a != b


def test_rejects_non_atcqr1_payload():
    with pytest.raises(ValueError):
        decrypt_payload("not-a-real-payload", "whatever")
