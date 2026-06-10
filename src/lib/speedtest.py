"""OpenSpeedTest client — ping, download, and upload measurements.

OpenSpeedTest (openspeedtest/latest) exposes these test endpoints:
  HEAD /?n=<rand>              →  ~8 KB HTML, used for RTT/latency
  GET  /downloading?n=<rand>  →  30 MB binary blob, repeatable
  POST /upload                →  accepts body, returns 200 empty
"""
from __future__ import annotations

import asyncio
import hashlib
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


# ── Upload ───────────────────────────────────────────────────────────────────

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


# ── Contextual facts ──────────────────────────────────────────────────────────
# Each entry: (condition(ping_ms, dl_mbps, ul_mbps) -> bool, fact_string)

_FACTS: list[tuple] = [
    # Gigabit download >= 900 Mbps
    (lambda p, d, u: d >= 900,
     "You could download the entire English Wikipedia (22 GB) in under 3 minutes."),
    (lambda p, d, u: d >= 900,
     "A 100 GB PS5 game installs in under 15 minutes. Coffee's barely brewed."),
    (lambda p, d, u: d >= 900,
     "You're moving data faster than a USB 3.0 drive can write (approx 625 MB/s)."),
    (lambda p, d, u: d >= 900,
     "The Hubble Space Telescope transmits at approx 1 Mbps. You're 1,000x faster."),
    (lambda p, d, u: d >= 900,
     "Netflix's full 4K catalog (~15 TB) would take roughly 33 hours to pull down."),
    # Excellent download 500-900 Mbps
    (lambda p, d, u: 500 <= d < 900,
     "You could replicate a 1 TB NAS drive offsite in just over 4 hours."),
    (lambda p, d, u: 500 <= d < 900,
     "A 50 GB game downloads in about 12 minutes -- one episode of TV."),
    (lambda p, d, u: 500 <= d < 900,
     "Your connection could stream 8K video (approx 100 Mbps) with 4x headroom to spare."),
    (lambda p, d, u: 500 <= d < 900,
     "Fast enough to run a 50-person office in simultaneous 4K video calls."),
    (lambda p, d, u: 500 <= d < 900,
     "Only ~10% of global internet users ever see speeds in this range."),
    # Fast download 200-500 Mbps
    (lambda p, d, u: 200 <= d < 500,
     "Enough to stream 15 simultaneous 4K Netflix streams (each approx 25 Mbps)."),
    (lambda p, d, u: 200 <= d < 500,
     "A full season of 4K TV (approx 40 GB) downloads in about 20 minutes."),
    (lambda p, d, u: 200 <= d < 500,
     "You're 3-4x faster than the global average broadband speed of approx 68 Mbps."),
    (lambda p, d, u: 200 <= d < 500,
     "A Docker image pull that takes 30 s on basic broadband finishes in under 8 s."),
    (lambda p, d, u: 200 <= d < 500,
     "You could mirror a 100 GB git repo in the time it takes to read this sentence."),
    # Good download 100-200 Mbps
    (lambda p, d, u: 100 <= d < 200,
     "Fast enough for every person in a 4-bedroom household to 4K stream simultaneously."),
    (lambda p, d, u: 100 <= d < 200,
     "A 15 GB game update downloads in roughly 15 minutes."),
    (lambda p, d, u: 100 <= d < 200,
     "You're at 4-8x the FCC's broadband definition of 25 Mbps."),
    (lambda p, d, u: 100 <= d < 200,
     "Enough bandwidth for 8 simultaneous Zoom calls in 1080p HD."),
    (lambda p, d, u: 100 <= d < 200,
     "Netflix 4K streams at 25 Mbps -- you have 4-8x that headroom."),
    # Basic download 25-100 Mbps
    (lambda p, d, u: 25 <= d < 100,
     "Enough for 4K streaming, though a big game download will be felt by others."),
    (lambda p, d, u: 25 <= d < 100,
     "You're above the FCC's updated broadband minimum of 25 Mbps."),
    (lambda p, d, u: 25 <= d < 100,
     "A 4 GB game patch takes roughly 5-8 minutes to pull down."),
    (lambda p, d, u: 25 <= d < 100,
     "Spotify's highest-quality stream is 320 kbps -- you're using approx 200x that right now."),
    (lambda p, d, u: 25 <= d < 100,
     "Comfortable for 4 simultaneous HD video calls -- a full household."),
    # Slow download < 25 Mbps
    (lambda p, d, u: d < 25,
     "The original iPhone (2007) used EDGE at 0.2 Mbps. You're still 100x that."),
    (lambda p, d, u: d < 25,
     "4K Netflix needs 25 Mbps -- dropping to 1080p would help here."),
    (lambda p, d, u: d < 25,
     "ADSL in 2003 topped out at 8 Mbps. You have the future, sort of."),
    (lambda p, d, u: d < 25,
     "A 1 GB OS update will take 10+ minutes. Time for a proper break."),
    (lambda p, d, u: d < 25,
     "Dial-up in 1999 ran at 0.056 Mbps. Progress is progress, just maybe not enough."),
    # Excellent ping < 10 ms
    (lambda p, d, u: p < 10,
     "Your ping is faster than the fastest human blink (approx 100 ms)."),
    (lambda p, d, u: p < 10,
     "Pro gamers consider 1-5 ms ping 'essentially zero.' You're in that bracket."),
    (lambda p, d, u: p < 10,
     "Light travels approx 3,000 km in 10 ms. Your packets are nearly photon-speed on this hop."),
    (lambda p, d, u: p < 10,
     "Surgeons operating remote robots require under 50 ms -- you're 5x inside that threshold."),
    (lambda p, d, u: p < 10,
     "At this latency, your game server processes your inputs more than 100 times per second."),
    # Good ping 10-30 ms
    (lambda p, d, u: 10 <= p < 30,
     "Your ping is 10x faster than the average human reaction time (250 ms)."),
    (lambda p, d, u: 10 <= p < 30,
     "Competitive gaming at this latency is essentially indistinguishable from local play."),
    (lambda p, d, u: 10 <= p < 30,
     "The average transatlantic fiber cable adds approx 70 ms. You're well ahead of that."),
    (lambda p, d, u: 10 <= p < 30,
     "A 20 ms ping means your game server sees your moves 50 times per second."),
    (lambda p, d, u: 10 <= p < 30,
     "Voice calls sound natural under 150 ms -- you have 5-7x the headroom."),
    # OK ping 30-80 ms
    (lambda p, d, u: 30 <= p < 80,
     "Still great for streaming -- Zoom only needs under 150 ms for clear conversation."),
    (lambda p, d, u: 30 <= p < 80,
     "A 50 ms ping is imperceptible in casual gaming; only twitch shooters notice."),
    (lambda p, d, u: 30 <= p < 80,
     "Your ping is roughly the time it takes sound to travel 17 metres through air."),
    (lambda p, d, u: 30 <= p < 80,
     "Live TV broadcasts add approx 2,000 ms of latency -- your stream is 40x more responsive."),
    (lambda p, d, u: 30 <= p < 80,
     "WebRTC voice calls tolerate up to 400 ms -- you're well inside comfortable territory."),
    # High ping >= 80 ms
    (lambda p, d, u: p >= 80,
     "Speech becomes awkward over 150 ms -- you're approaching that threshold."),
    (lambda p, d, u: p >= 80,
     "Geostationary satellite internet averages 550-600 ms. You're still below that ceiling."),
    (lambda p, d, u: p >= 80,
     "The ISS orbits at 400 km altitude and still gets under 600 ms to ground. You're in good company."),
    (lambda p, d, u: p >= 80,
     "High-frequency traders consider 1 ms critical -- your ping is their entire trading day."),
    (lambda p, d, u: p >= 80,
     "VoIP calls start degrading noticeably above 100 ms -- worth checking your routing."),
    # High upload >= 200 Mbps
    (lambda p, d, u: u >= 200,
     "With this upload speed, you could push a full VM snapshot (100 GB) offsite in about an hour."),
]


def pick_fact(ping_ms: float, dl_mbps: float, ul_mbps: float) -> str:
    """Return a contextual fact based on the measured result tiers."""
    matching = [fact for cond, fact in _FACTS if cond(ping_ms, dl_mbps, ul_mbps)]
    pool = matching if matching else [f for _, f in _FACTS]
    key = f"{ping_ms:.0f}|{dl_mbps:.0f}|{ul_mbps:.0f}"
    idx = int(hashlib.md5(key.encode()).hexdigest(), 16) % len(pool)
    return pool[idx]
