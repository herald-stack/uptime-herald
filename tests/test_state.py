# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_state.py - Tests for the helpers in src/state.py.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.state import (
    counts_toward_stats,
    hour_floor,
    hour_window_start,
    period_window_start,
)


@pytest.mark.parametrize("status, details, expected", [
    ("200", None, True),
    ("200", {}, True),
    ("up", {"host": "h", "port": 80}, True),
    ("503", {"status_code": 503}, True),
    ("timeout", None, True),
    # log rows that are not real checks never count
    ("maintenance", None, False),
    ("inactive", None, False),
    # a retry made while a state change is being verified counts only when
    # it is the one that confirmed the change
    ("503", {"verifying": True}, False),
    ("503", {"verifying": True, "passed": False}, False),
    ("503", {"verifying": True, "passed": True}, True),
    ("200", {"verifying": False}, True),
    ("200", {"verifying": None}, True),
])
def test_counts_toward_stats(status, details, expected):
    assert counts_toward_stats(status, details) is expected


def test_hour_floor_drops_minutes_seconds_and_microseconds():
    moment = datetime(2026, 10, 28, 5, 59, 59, 999999, tzinfo=timezone.utc)
    assert hour_floor(moment) == datetime(2026, 10, 28, 5, 0, tzinfo=timezone.utc)


def test_hour_window_start_counts_the_current_hour_as_one_slot():
    now = datetime(2026, 10, 28, 5, 30, tzinfo=timezone.utc)
    assert hour_window_start(now, 1) == datetime(2026, 10, 28, 5, 0, tzinfo=timezone.utc)
    assert hour_window_start(now, 24) == datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)


def test_period_window_start_goes_back_whole_days_from_the_top_of_the_hour():
    now = datetime(2026, 10, 28, 5, 30, tzinfo=timezone.utc)
    assert period_window_start(now, 1) == datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
    assert period_window_start(now, 30) == datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)


def test_period_window_start_covers_at_least_the_period_and_under_an_hour_more():
    for minute in (0, 1, 30, 59):
        now = datetime(2026, 10, 28, 5, minute, 59, tzinfo=timezone.utc)
        covered = now - period_window_start(now, 7)
        assert timedelta(days=7) <= covered < timedelta(days=7, hours=1)
