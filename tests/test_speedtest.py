"""Tests for lib/speedtest.py — dataclasses and pure-logic behaviour."""
from __future__ import annotations

from lib.speedtest import PingResult, SpeedResult

# ── PingResult ────────────────────────────────────────────────────────────────

class TestPingResult:
    def test_empty(self):
        p = PingResult()
        assert p.median_ms == 0.0
        assert p.min_ms == 0.0
        assert p.max_ms == 0.0
        assert p.jitter_ms == 0.0

    def test_single_sample(self):
        p = PingResult(samples=[5.0])
        assert p.median_ms == 5.0
        assert p.min_ms == 5.0
        assert p.max_ms == 5.0
        assert p.jitter_ms == 0.0

    def test_median_odd(self):
        p = PingResult(samples=[3.0, 1.0, 5.0])
        assert p.median_ms == 3.0

    def test_median_even(self):
        p = PingResult(samples=[2.0, 4.0])
        assert p.median_ms == 3.0

    def test_min_max(self):
        p = PingResult(samples=[10.0, 2.0, 8.0, 1.0])
        assert p.min_ms == 1.0
        assert p.max_ms == 10.0

    def test_jitter_two_samples(self):
        p = PingResult(samples=[1.0, 3.0])
        assert p.jitter_ms == 2.0

    def test_jitter_uniform(self):
        # All samples identical → zero jitter
        p = PingResult(samples=[5.0, 5.0, 5.0, 5.0])
        assert p.jitter_ms == 0.0

    def test_jitter_average_of_deltas(self):
        # deltas: |2-1|=1, |5-2|=3, |4-5|=1 → avg = 5/3
        p = PingResult(samples=[1.0, 2.0, 5.0, 4.0])
        assert round(p.jitter_ms, 4) == round(5 / 3, 4)

    def test_samples_are_stored(self):
        p = PingResult(samples=[1.1, 2.2, 3.3])
        assert len(p.samples) == 3


# ── SpeedResult ───────────────────────────────────────────────────────────────

class TestSpeedResult:
    def test_defaults(self):
        r = SpeedResult()
        assert r.ok is True
        assert r.error == ""
        assert r.download_mbps == 0.0
        assert r.upload_mbps == 0.0
        assert r.download_bytes == 0
        assert r.upload_bytes == 0
        assert r.duration_s == 10.0
        assert r.parallel_dl == 4
        assert r.parallel_ul == 2

    def test_failure(self):
        r = SpeedResult(ok=False, error="Connection refused")
        assert not r.ok
        assert r.error == "Connection refused"

    def test_full_result(self):
        r = SpeedResult(
            ok=True,
            ping=PingResult(samples=[1.0, 2.0, 1.5]),
            download_mbps=842.3,
            download_bytes=1_050_000_000,
            upload_mbps=437.1,
            upload_bytes=546_000_000,
            duration_s=10.0,
            parallel_dl=4,
            parallel_ul=2,
        )
        assert r.ping.median_ms == 1.5
        assert r.download_mbps == 842.3
        assert r.upload_mbps == 437.1
        assert r.parallel_dl == 4

    def test_ping_embedded(self):
        p = PingResult(samples=[3.0, 4.0, 5.0])
        r = SpeedResult(ping=p)
        assert r.ping.min_ms == 3.0
        assert r.ping.max_ms == 5.0
        assert r.ping.jitter_ms == 1.0
