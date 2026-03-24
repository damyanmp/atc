"""TLS certificate expiry checks — stdlib ssl + cryptography, no extra deps."""
from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class CertStatus:
    name: str
    host: str
    port: int
    ok: bool
    subject: str = ""
    expiry: datetime | None = None
    days_left: int = 0
    error: str = ""


def _check_cert_sync(host: str, port: int, sni: str, verify: bool) -> tuple[str, datetime]:
    """Blocking TLS connect — returns (subject_cn, expiry_utc)."""
    from cryptography import x509

    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=sni) as ssock:
            der = ssock.getpeercert(binary_form=True)

    cert = x509.load_der_x509_certificate(der)

    try:
        expiry = cert.not_valid_after_utc  # cryptography >= 42
    except AttributeError:
        expiry = cert.not_valid_after.replace(tzinfo=timezone.utc)

    try:
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    except (IndexError, Exception):
        cn = ""

    return cn, expiry


async def check_cert(
    name: str,
    host: str,
    port: int,
    sni: str | None = None,
    verify: bool = True,
) -> CertStatus:
    effective_sni = sni or host
    try:
        loop = asyncio.get_event_loop()
        subject, expiry = await loop.run_in_executor(
            None, _check_cert_sync, host, port, effective_sni, verify
        )
        days_left = (expiry - datetime.now(timezone.utc)).days
        return CertStatus(
            name=name, host=host, port=port, ok=True,
            subject=subject, expiry=expiry, days_left=days_left,
        )
    except Exception as e:
        return CertStatus(name=name, host=host, port=port, ok=False, error=str(e))


async def check_all_certs(checks: list) -> list[CertStatus]:
    return list(await asyncio.gather(*[
        check_cert(c.name, c.host, c.port, c.sni, c.verify_ssl)
        for c in checks
    ]))
