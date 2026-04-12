"""Tests for app/ui/time.py — Europe/Tallinn timestamp formatting."""

from __future__ import annotations

from datetime import UTC, datetime

from app.ui.time import TALLINN_TZ, format_tallinn, now_tallinn, to_tallinn


class TestToTallinn:
    def test_utc_winter_converts_to_plus_two(self):
        # January: Estonia is UTC+2
        dt = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        local = to_tallinn(dt)
        assert local.hour == 14
        assert local.tzinfo == TALLINN_TZ

    def test_utc_summer_converts_to_plus_three(self):
        # July: Estonia is UTC+3 (EEST)
        dt = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        local = to_tallinn(dt)
        assert local.hour == 15

    def test_naive_treated_as_utc(self):
        """Naive datetimes must be treated as UTC (our storage convention)."""
        dt = datetime(2026, 1, 15, 12, 0)  # naive
        local = to_tallinn(dt)
        assert local.hour == 14


class TestFormatTallinn:
    def test_formats_in_tallinn_zone(self):
        dt = datetime(2026, 4, 12, 12, 0, tzinfo=UTC)
        # April 12 is within EEST (UTC+3)
        assert format_tallinn(dt) == "12.04.2026 15:00"

    def test_none_renders_em_dash(self):
        assert format_tallinn(None) == "\u2014"

    def test_non_datetime_falls_through_to_str(self):
        assert format_tallinn("not-a-datetime") == "not-a-datetime"

    def test_custom_format_string_respected(self):
        dt = datetime(2026, 4, 12, 9, 0, tzinfo=UTC)
        # EEST: 12:00
        assert format_tallinn(dt, fmt="%H:%M") == "12:00"

    def test_dst_boundary_spring_forward(self):
        """Last Sunday of March 2026 = March 29. Clocks jump 01:00 UTC → 04:00 EEST.

        At 00:59 UTC we're still +2 (EET). At 01:00 UTC we become +3 (EEST)."""
        before = datetime(2026, 3, 29, 0, 59, tzinfo=UTC)
        after = datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
        assert format_tallinn(before).endswith("02:59")
        assert format_tallinn(after).endswith("04:00")


class TestNowTallinn:
    def test_returns_tallinn_tz(self):
        n = now_tallinn()
        assert n.tzinfo == TALLINN_TZ
