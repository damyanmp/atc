"""Tests for CLI output format helpers.

Note: cli.py defines _fmt_bytes twice — once in the Proxmox section (short
"1.5G" form) and once in the media/arr section ("1.5 GB" form).  The second
definition wins at module level.  These tests verify the live runtime behaviour.
"""
from cli import _fmt_bytes, _fmt_pct, _fmt_uptime


class TestFmtBytes:
    def test_zero(self):
        assert _fmt_bytes(0) == "0 B"

    def test_bytes_range(self):
        assert _fmt_bytes(500) == "500.0 B"

    def test_exact_kilobyte(self):
        assert _fmt_bytes(1024) == "1.0 KB"

    def test_fractional_kilobyte(self):
        assert _fmt_bytes(1536) == "1.5 KB"

    def test_exact_megabyte(self):
        assert _fmt_bytes(1024 ** 2) == "1.0 MB"

    def test_fractional_megabyte(self):
        assert _fmt_bytes(int(2.5 * 1024 ** 2)) == "2.5 MB"

    def test_exact_gigabyte(self):
        assert _fmt_bytes(1024 ** 3) == "1.0 GB"

    def test_fractional_gigabyte(self):
        # 1.5 GB = 1.5 * 1024^3 bytes
        assert _fmt_bytes(int(1.5 * 1024 ** 3)) == "1.5 GB"

    def test_terabyte(self):
        assert _fmt_bytes(2 * 1024 ** 4) == "2.0 TB"

    def test_large_value_does_not_crash(self):
        result = _fmt_bytes(10 * 1024 ** 5)
        assert result  # just ensure no exception and non-empty output


class TestFmtPct:
    def test_low_is_green(self):
        assert "[green]" in _fmt_pct(0.50)

    def test_warning_threshold_is_yellow(self):
        assert "[yellow]" in _fmt_pct(0.80)

    def test_critical_threshold_is_red(self):
        assert "[red]" in _fmt_pct(0.95)

    def test_boundary_warn_default(self):
        # Exactly at the default warn threshold (0.75) → yellow
        assert "[yellow]" in _fmt_pct(0.75)

    def test_boundary_crit_default(self):
        # Exactly at the default crit threshold (0.90) → red
        assert "[red]" in _fmt_pct(0.90)

    def test_percentage_value_in_output(self):
        result = _fmt_pct(0.50)
        assert "50%" in result

    def test_custom_thresholds(self):
        # With very high thresholds everything below 0.99 is green
        assert "[green]" in _fmt_pct(0.80, warn=0.99, crit=0.999)

    def test_output_contains_closing_tag(self):
        assert "[/]" in _fmt_pct(0.50)


class TestFmtUptime:
    def test_zero_returns_dash(self):
        assert _fmt_uptime(0) == "—"

    def test_negative_returns_dash(self):
        assert _fmt_uptime(-1) == "—"

    def test_minutes_only(self):
        assert _fmt_uptime(30 * 60) == "30m"

    def test_hours_and_minutes(self):
        assert _fmt_uptime(3 * 3600 + 45 * 60) == "3h 45m"

    def test_days_and_hours(self):
        assert _fmt_uptime(5 * 86400 + 12 * 3600) == "5d 12h"

    def test_exactly_one_hour(self):
        assert _fmt_uptime(3600) == "1h 0m"

    def test_exactly_one_day(self):
        assert _fmt_uptime(86400) == "1d 0h"

    def test_sub_minute_rounds_to_zero_minutes(self):
        # 45 seconds → 0m
        assert _fmt_uptime(45) == "0m"
