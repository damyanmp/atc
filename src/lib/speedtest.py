"""OpenSpeedTest client — ping, download, and upload measurements.

OpenSpeedTest (openspeedtest/latest) exposes these test endpoints:
  HEAD /?n=<rand>              →  ~8 KB HTML, used for RTT/latency
  GET  /downloading?n=<rand>  →  30 MB binary blob, repeatable
  POST /upload                →  accepts body, returns 200 empty
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from statistics import median
from typing import Callable, Optional

import httpx

from config import OpenSpeedTestConfig


@dataclass
class PingResult:
    samples: list[float] = field(default_factory=list)  # RTTs in ms

    @property
    def median_ms(self) -> float:
        return median(self.samples) if self.samples else 0.0

    @property
    def min_ms(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def jitter_ms(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        deltas = [abs(self.samples[i] - self.samples[i - 1]) for i in range(1, len(self.samples))]
        return sum(deltas) / len(deltas)


@dataclass
class SpeedResult:
    ok: bool = True
    error: str = ""
    ping: PingResult = field(default_factory=PingResult)
    download_mbps: float = 0.0
    download_bytes: int = 0
    upload_mbps: float = 0.0
    upload_bytes: int = 0
    duration_s: float = 10.0
    parallel_dl: int = 4
    parallel_ul: int = 2


# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_url(cfg: OpenSpeedTestConfig) -> str:
    scheme = "https" if cfg.https else "http"
    return f"{scheme}://{cfg.host}:{cfg.port}"


def _nc() -> str:
    """Cache-busting query string."""
    return f"?n={random.random()}"


# ── Ping ──────────────────────────────────────────────────────────────────────

async def ping_test(
    cfg: OpenSpeedTestConfig,
    samples: int = 10,
    on_sample: Optional[Callable[[int, float], None]] = None,
) -> PingResult:
    """Measure round-trip latency with HEAD requests.

    on_sample(index, rtt_ms) is called synchronously after each completed sample.
    """
    url = f"{_base_url(cfg)}/{_nc()}"
    result = PingResult()
    async with httpx.AsyncClient(verify=cfg.verify_ssl, timeout=10) as client:
        for i in range(samples):
            t0 = time.perf_counter()
            try:
                await client.head(url)
            except Exception:
                continue
            rtt_ms = (time.perf_counter() - t0) * 1000
            result.samples.append(rtt_ms)
            if on_sample:
                on_sample(i, rtt_ms)
    return result


# ── Download ──────────────────────────────────────────────────────────────────

async def _download_worker(
    client: httpx.AsyncClient,
    url: str,
    counter: list[int],
) -> None:
    """Stream GET /downloading repeatedly, counting received bytes."""
    while True:
        try:
            async with client.stream("GET", url + _nc()) as resp:
                async for chunk in resp.aiter_bytes(65536):
                    counter[0] += len(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(0.05)


async def download_test(
    cfg: OpenSpeedTestConfig,
    duration_s: float = 10.0,
    parallel: int = 4,
    counter: Optional[list[int]] = None,
) -> tuple[float, int]:
    """Return (mbps, total_bytes) for a timed download test."""
    if counter is None:
        counter = [0]
    url = f"{_base_url(cfg)}/downloading"
    async with httpx.AsyncClient(verify=cfg.verify_ssl, timeout=duration_s + 10) as client:
        tasks = [
            asyncio.create_task(_download_worker(client, url, counter))
            for _ in range(parallel)
        ]
        t0 = time.perf_counter()
        await asyncio.sleep(duration_s)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.perf_counter() - t0
    mbps = (counter[0] * 8) / (elapsed * 1_000_000)
    return mbps, counter[0]


# ── Upload ────────────────────────────────────────────────────────────────────

async def _upload_worker(
    client: httpx.AsyncClient,
    url: str,
    counter: list[int],
    payload_size: int = 8 * 1024 * 1024,
) -> None:
    """POST fixed-size payloads to /upload, counting sent bytes."""
    payload = os.urandom(payload_size)
    while True:
        try:
            counter[0] += len(payload)
            await client.post(url, content=payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(0.05)


async def upload_test(
    cfg: OpenSpeedTestConfig,
    duration_s: float = 10.0,
    parallel: int = 2,
    counter: Optional[list[int]] = None,
) -> tuple[float, int]:
    """Return (mbps, total_bytes) for a timed upload test."""
    if counter is None:
        counter = [0]
    url = f"{_base_url(cfg)}/upload"
    async with httpx.AsyncClient(verify=cfg.verify_ssl, timeout=duration_s + 10) as client:
        tasks = [
            asyncio.create_task(_upload_worker(client, url, counter))
            for _ in range(parallel)
        ]
        t0 = time.perf_counter()
        await asyncio.sleep(duration_s)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.perf_counter() - t0
    mbps = (counter[0] * 8) / (elapsed * 1_000_000)
    return mbps, counter[0]
