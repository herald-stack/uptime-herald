# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_maintenance.py - Tests for maintenance window evaluation.
"""

import contextlib
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.maintenance import _nth_weekday_date, _window_active_now, get_active_maintenance

# Ordinals for monthly recurrence tests
ORDINALS = ["1st", "2nd", "3rd", "4th", "last"]

# Fixed reference instant used everywhere a test needs "now" — Wednesday,
# July 15 2026, noon Central. Deliberately not tied to the real wall clock
# so these tests are reproducible regardless of when/where the suite runs.
# July 15 2026 also happens to be the 3rd Wednesday of the month, which
# test_monthly_window_monitor_targeting relies on.
FIXED_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
FIXED_NOW_WEEKDAY = "Wednesday"


def _make_config(windows, tz="America/Chicago", monitors=None):
    """Build a config dict with maintenance windows and optional monitors."""
    config = {
        "global": {"timezone": tz},
        "maintenance": windows,
    }
    if monitors is not None:
        config["monitors"] = monitors
    return config


@contextlib.contextmanager
def _frozen_now(fixed_dt):
    """Patch src.maintenance's `datetime.now()` to return a fixed instant.

    get_active_maintenance() calls datetime.now(timezone.utc) internally
    rather than accepting an injectable time (only _window_active_now()
    takes now_local as a parameter), so tests that exercise it need this
    to stay deterministic regardless of when the suite actually runs.
    """
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_dt.astimezone(tz) if tz else fixed_dt

    with patch("src.maintenance.datetime", _FrozenDatetime):
        yield


# --- Recurring weekly windows ---------------------------------------------------------

def test_recurring_window_active():
    """Window that should be active right now."""
    # Window: today 00:00 to today 23:59
    window = {
        "name": "All day",
        "start_day": FIXED_NOW_WEEKDAY,
        "start_time": "00:00",
        "end_day": FIXED_NOW_WEEKDAY,
        "end_time": "23:59",
        "monitors": "all",
    }
    assert _window_active_now(window, FIXED_NOW)


def test_recurring_window_inactive():
    """Window that already ended today."""
    # Window: today 00:00 to today 00:01 (already passed — FIXED_NOW is noon)
    window = {
        "name": "Early morning",
        "start_day": FIXED_NOW_WEEKDAY,
        "start_time": "00:00",
        "end_day": FIXED_NOW_WEEKDAY,
        "end_time": "00:01",
        "monitors": "all",
    }
    assert not _window_active_now(window, FIXED_NOW)


def test_recurring_window_crosses_midnight():
    """Window that wraps past midnight (e.g. Sunday 23:00 to Monday 02:00)."""
    tz = ZoneInfo("America/Chicago")
    # Monday 01:00 — should be in Sunday 23:00 - Monday 02:00 window
    test_now = datetime(2026, 7, 6, 1, 0, tzinfo=tz)  # Monday
    window = {
        "name": "Overnight",
        "start_day": "Sunday",
        "start_time": "23:00",
        "end_day": "Monday",
        "end_time": "02:00",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)

    # Monday 03:00 — should be outside the window
    test_now_late = datetime(2026, 7, 6, 3, 0, tzinfo=tz)
    assert not _window_active_now(window, test_now_late)


# --- One-off windows ------------------------------------------------------------------

def test_one_off_window_active():
    """One-off window that should be active at a specific time."""
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 15, 23, 30, tzinfo=tz)
    window = {
        "name": "Firmware upgrade",
        "start_day": "2026-07-15",
        "start_time": "23:00",
        "end_day": "2026-07-16",
        "end_time": "01:00",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)


def test_one_off_window_before_start():
    """One-off window that hasn't started yet."""
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 15, 22, 0, tzinfo=tz)
    window = {
        "name": "Firmware upgrade",
        "start_day": "2026-07-15",
        "start_time": "23:00",
        "end_day": "2026-07-16",
        "end_time": "01:00",
        "monitors": "all",
    }
    assert not _window_active_now(window, test_now)


def test_one_off_window_after_end():
    """One-off window that has already ended."""
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 16, 2, 30, tzinfo=tz)
    window = {
        "name": "Firmware upgrade",
        "start_day": "2026-07-15",
        "start_time": "23:00",
        "end_day": "2026-07-16",
        "end_time": "01:00",
        "monitors": "all",
    }
    assert not _window_active_now(window, test_now)


# --- Monitor targeting ----------------------------------------------------------------

def test_monitors_all():
    """monitors: 'all' applies to any monitor."""
    config = _make_config([{
        "name": "Global",
        "start_day": FIXED_NOW_WEEKDAY,
        "start_time": "00:00",
        "end_day": FIXED_NOW_WEEKDAY,
        "end_time": "23:59",
        "monitors": "all",
    }])
    with _frozen_now(FIXED_NOW):
        result = get_active_maintenance(config, "any-monitor")
    assert "Global" in result


def test_monitors_list():
    """monitors: list only applies to named monitors."""
    config = _make_config([{
        "name": "Specific",
        "start_day": FIXED_NOW_WEEKDAY,
        "start_time": "00:00",
        "end_day": FIXED_NOW_WEEKDAY,
        "end_time": "23:59",
        "monitors": ["drd01", "drd02"],
    }])
    with _frozen_now(FIXED_NOW):
        assert "Specific" in get_active_maintenance(config, "drd01")
        assert "Specific" in get_active_maintenance(config, "drd02")
        assert get_active_maintenance(config, "drd03") == []


def test_no_maintenance_section():
    """No maintenance section returns empty list."""
    config = {"global": {"timezone": "UTC"}, "monitors": []}
    assert get_active_maintenance(config, "any") == []


def test_overlapping_windows():
    """Multiple active windows for the same monitor."""
    config = _make_config([
        {
            "name": "Window A",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "monitors": "all",
        },
        {
            "name": "Window B",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "monitors": ["drd01"],
        },
    ])
    with _frozen_now(FIXED_NOW):
        result = get_active_maintenance(config, "drd01")
    assert "Window A" in result
    assert "Window B" in result
    assert len(result) == 2


def test_invalid_window_skipped():
    """Invalid window config is skipped, not crashed."""
    config = _make_config([
        {
            "name": "Good window",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "monitors": "all",
        },
        {
            "name": "Bad window",
            "start_day": "NotADay",
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "monitors": "all",
        },
    ])
    with _frozen_now(FIXED_NOW):
        result = get_active_maintenance(config, "any")
    assert "Good window" in result
    assert "Bad window" not in result


def test_window_without_monitors_or_tags_matches_nothing():
    """A window with neither monitors nor tags matches no monitor (the config
    check rejects such a window, so this is only the matching rule)."""
    config = _make_config([
        {
            "name": "Disabled window",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
        },
        {
            "name": "Active window",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "monitors": "all",
        },
    ])
    with _frozen_now(FIXED_NOW):
        result = get_active_maintenance(config, "any")
    assert "Active window" in result
    assert "Disabled window" not in result


# --- Tag targeting --------------------------------------------------------------------


def test_maintenance_tags_match():
    """Maintenance window with tags should apply to monitors with matching tags."""
    config = _make_config(
        [{
            "name": "Tagged maintenance",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "tags": ["external"],
        }],
        monitors=[
            {"name": "web1", "type": "http", "url": "http://x.com", "tags": ["external"]},
            {"name": "db1", "type": "ping", "host": "db1", "tags": ["internal"]},
        ],
    )
    with _frozen_now(FIXED_NOW):
        assert "Tagged maintenance" in get_active_maintenance(config, "web1")
        assert get_active_maintenance(config, "db1") == []


def test_maintenance_tags_and_monitors_both_match():
    """Window with both monitors list and tags — monitor matches if it hits either."""
    config = _make_config(
        [{
            "name": "Combined",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "monitors": ["db1"],
            "tags": ["external"],
        }],
        monitors=[
            {"name": "web1", "type": "http", "url": "http://x.com", "tags": ["external"]},
            {"name": "db1", "type": "ping", "host": "db1", "tags": ["internal"]},
        ],
    )
    with _frozen_now(FIXED_NOW):
        assert "Combined" in get_active_maintenance(config, "web1")  # matches via tag
        assert "Combined" in get_active_maintenance(config, "db1")   # matches via name


def test_maintenance_tags_all_matches_monitors_that_have_a_tag():
    """tags: "all" is every monitor that has at least one tag, not every monitor."""
    config = _make_config(
        [{
            "name": "Any tagged",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "tags": "all",
        }],
        monitors=[
            {"name": "web1", "type": "http", "url": "http://x.com", "tags": ["external"]},
            {"name": "db1", "type": "ping", "host": "db1", "tags": ["internal"]},
            {"name": "bare", "type": "ping", "host": "bare"},
        ],
    )
    with _frozen_now(FIXED_NOW):
        assert "Any tagged" in get_active_maintenance(config, "web1")
        assert "Any tagged" in get_active_maintenance(config, "db1")
        assert get_active_maintenance(config, "bare") == []


def test_maintenance_monitors_all_matches_untagged_monitors_too():
    config = _make_config(
        [{
            "name": "Everything",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "monitors": "all",
        }],
        monitors=[{"name": "bare", "type": "ping", "host": "bare"}],
    )
    with _frozen_now(FIXED_NOW):
        assert "Everything" in get_active_maintenance(config, "bare")


def test_maintenance_no_monitors_section():
    """No monitors section in config — tag targeting finds no tags, only monitors list works."""
    config = _make_config(
        [{
            "name": "Tagged but no monitors",
            "start_day": FIXED_NOW_WEEKDAY,
            "start_time": "00:00",
            "end_day": FIXED_NOW_WEEKDAY,
            "end_time": "23:59",
            "tags": ["external"],
        }],
    )
    # No monitors in config, so no monitor has the "external" tag
    with _frozen_now(FIXED_NOW):
        assert get_active_maintenance(config, "any") == []


# --- Monthly recurrence windows (ordinal + weekday) -----------------------------------

def test_monthly_window_active():
    """Monthly window active on the correct Nth weekday."""
    # July 2026: the 2nd Tuesday is July 14
    # Window: 2nd Tuesday 22:00 to 2nd Wednesday 02:00
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 14, 23, 30, tzinfo=tz)  # 2nd Tuesday night
    window = {
        "name": "Patch Tuesday",
        "start_day": "2nd Tuesday",
        "start_time": "22:00",
        "end_day": "2nd Wednesday",
        "end_time": "02:00",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)


def test_monthly_window_inactive_wrong_week():
    """Monthly window not active on the 1st Tuesday (only 2nd)."""
    # July 2026: 1st Tuesday is July 7
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 7, 23, 30, tzinfo=tz)  # 1st Tuesday night
    window = {
        "name": "Patch Tuesday",
        "start_day": "2nd Tuesday",
        "start_time": "22:00",
        "end_day": "2nd Wednesday",
        "end_time": "02:00",
        "monitors": "all",
    }
    assert not _window_active_now(window, test_now)


def test_monthly_window_inactive_wrong_weekday():
    """Monthly window not active on a different weekday."""
    tz = ZoneInfo("America/Chicago")
    # July 14 2026 is a Tuesday; 2nd Monday would be July 13
    test_now = datetime(2026, 7, 13, 23, 30, tzinfo=tz)  # 2nd Monday
    window = {
        "name": "Patch Tuesday",
        "start_day": "2nd Tuesday",
        "start_time": "22:00",
        "end_day": "2nd Wednesday",
        "end_time": "02:00",
        "monitors": "all",
    }
    assert not _window_active_now(window, test_now)


def test_monthly_window_same_day():
    """Monthly window within a single day (no midnight cross)."""
    # July 2026: 2nd Tuesday is July 14
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 14, 3, 0, tzinfo=tz)  # 2nd Tuesday 3am
    window = {
        "name": "2nd Tuesday maintenance",
        "start_day": "2nd Tuesday",
        "start_time": "02:00",
        "end_day": "2nd Tuesday",
        "end_time": "04:00",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)

    # Before window
    test_before = datetime(2026, 7, 14, 1, 0, tzinfo=tz)
    assert not _window_active_now(window, test_before)

    # After window
    test_after = datetime(2026, 7, 14, 5, 0, tzinfo=tz)
    assert not _window_active_now(window, test_after)


def test_monthly_window_last_weekday_active():
    """'last' ordinal — active on the last Friday of the month."""
    # July 2026: last Friday is July 31
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 31, 23, 30, tzinfo=tz)  # last Friday night
    window = {
        "name": "Last Friday cleanup",
        "start_day": "last Friday",
        "start_time": "23:00",
        "end_day": "last Saturday",
        "end_time": "01:00",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)


def test_monthly_window_last_weekday_inactive_not_last():
    """'last' ordinal — not active on a Friday that isn't the last one."""
    # July 2026: July 3 is the 1st Friday, not the last
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 3, 23, 30, tzinfo=tz)  # 1st Friday
    window = {
        "name": "Last Friday cleanup",
        "start_day": "last Friday",
        "start_time": "23:00",
        "end_day": "last Saturday",
        "end_time": "01:00",
        "monitors": "all",
    }
    assert not _window_active_now(window, test_now)


def test_monthly_window_1st_weekday():
    """1st ordinal works correctly."""
    # July 2026: 1st Wednesday is July 1
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 1, 12, 0, tzinfo=tz)  # 1st Wednesday noon
    window = {
        "name": "First Wed",
        "start_day": "1st Wednesday",
        "start_time": "00:00",
        "end_day": "1st Wednesday",
        "end_time": "23:59",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)

    # July 8 is the 2nd Wednesday — not 1st
    test_wrong = datetime(2026, 7, 8, 12, 0, tzinfo=tz)
    assert not _window_active_now(window, test_wrong)


def test_monthly_window_3rd_weekday():
    """3rd ordinal works correctly."""
    # July 2026: 3rd Thursday is July 16
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 16, 12, 0, tzinfo=tz)  # 3rd Thursday
    window = {
        "name": "Third Thu",
        "start_day": "3rd Thursday",
        "start_time": "00:00",
        "end_day": "3rd Thursday",
        "end_time": "23:59",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)


def test_monthly_window_4th_weekday():
    """4th ordinal works correctly."""
    # July 2026: 4th Friday is July 24
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 24, 12, 0, tzinfo=tz)  # 4th Friday
    window = {
        "name": "Fourth Fri",
        "start_day": "4th Friday",
        "start_time": "00:00",
        "end_day": "4th Friday",
        "end_time": "23:59",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)


def test_monthly_window_invalid_ordinal_skipped():
    """Invalid ordinal+weekday is skipped gracefully."""
    config = _make_config([
        {
            "name": "Bad ordinal",
            "start_day": "5th Tuesday",
            "start_time": "00:00",
            "end_day": "5th Tuesday",
            "end_time": "23:59",
            "monitors": "all",
        },
    ])
    # Should not crash, just return empty
    with _frozen_now(FIXED_NOW):
        result = get_active_maintenance(config, "any")
    assert result == []


def test_monthly_window_unrecognized_weekday_name_skipped():
    """'2nd Blursday' matches the ordinal+word regex (_MONTHLY_RE doesn't
    know real weekday names, just that it's a word) but isn't a real
    weekday -- distinct from "5th Tuesday" above, which fails at the
    ordinal level instead and never reaches the weekday-name check at
    all. Both must fail gracefully, not crash."""
    config = _make_config([
        {
            "name": "Typo'd weekday",
            "start_day": "2nd Blursday",
            "start_time": "00:00",
            "end_day": "2nd Blursday",
            "end_time": "23:59",
            "monitors": "all",
        },
    ])
    with _frozen_now(FIXED_NOW):
        result = get_active_maintenance(config, "any")
    assert result == []


def test_monthly_window_monitor_targeting():
    """Monthly recurrence respects monitor targeting."""
    # FIXED_NOW (July 15 2026) is the 3rd Wednesday of the month.
    start_day = f"3rd {FIXED_NOW_WEEKDAY}"
    config = _make_config([{
        "name": "Patch Tuesday",
        "start_day": start_day,
        "start_time": "00:00",
        "end_day": start_day,
        "end_time": "23:59",
        "monitors": ["drd01", "drd02"],
    }])
    with _frozen_now(FIXED_NOW):
        assert "Patch Tuesday" in get_active_maintenance(config, "drd01")
        assert "Patch Tuesday" in get_active_maintenance(config, "drd02")
        assert get_active_maintenance(config, "drd03") == []


def test_monthly_window_case_insensitive():
    """Monthly recurrence is case-insensitive."""
    # July 2026: 2nd Tuesday is July 14
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 14, 12, 0, tzinfo=tz)
    window = {
        "name": "Patch Tuesday",
        "start_day": "2ND tuesday",
        "start_time": "00:00",
        "end_day": "2ND TUESDAY",
        "end_time": "23:59",
        "monitors": "all",
    }
    assert _window_active_now(window, test_now)


# --- Monthly windows that cross a calendar month boundary -----------------------------
#
# "last Friday" of one month through "1st Monday" of the next.
# _window_active_now() handles this by checking two candidate anchors
# (previous month and current month) rather than assuming the window's
# start and end both fall in `now`'s own month -- these tests exercise
# that prev-month-anchor path specifically, checked from a date that has
# already rolled into the following month.
#
# July 2026: last Friday = July 31. August 2026: 1st Monday = August 3.

_CROSS_MONTH_WINDOW = {
    "name": "End of month deploy freeze",
    "start_day": "last Friday",
    "start_time": "18:00",
    "end_day": "1st Monday",
    "end_time": "06:00",
    "monitors": "all",
}


def test_monthly_window_crosses_month_boundary_active_after_rollover():
    """Checked on Aug 1 -- `now` has already rolled into August, but the
    window's start anchor (last Friday) is in July. Only the prev-month
    anchor branch can find this; anchoring solely to now's own month
    (August) would search for "last Friday" of August, which hasn't
    happened yet, and wrongly report the window as inactive."""
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 8, 1, 12, 0, tzinfo=tz)
    assert _window_active_now(_CROSS_MONTH_WINDOW, test_now)


def test_monthly_window_crosses_month_boundary_active_right_at_start():
    """Checked in July itself, right as the window begins -- the
    current-month anchor (not the prev-month one) should catch this."""
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 31, 19, 0, tzinfo=tz)
    assert _window_active_now(_CROSS_MONTH_WINDOW, test_now)


def test_monthly_window_crosses_month_boundary_inactive_before_start():
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 7, 30, 12, 0, tzinfo=tz)
    assert not _window_active_now(_CROSS_MONTH_WINDOW, test_now)


def test_monthly_window_crosses_month_boundary_inactive_after_end():
    """Checked well after the window closed (1st Monday of August, 06:00)
    -- confirms the prev-month anchor doesn't stay permanently "active"
    for the rest of the following month."""
    tz = ZoneInfo("America/Chicago")
    test_now = datetime(2026, 8, 4, 12, 0, tzinfo=tz)
    assert not _window_active_now(_CROSS_MONTH_WINDOW, test_now)


def test_monthly_window_crosses_year_boundary():
    """The same prev-month-anchor logic, at the harder edge: December
    into January, where the "previous month" calculation also has to
    roll the year back."""
    window = {
        "name": "Year-end freeze",
        "start_day": "last Friday",
        "start_time": "18:00",
        "end_day": "1st Monday",
        "end_time": "06:00",
        "monitors": "all",
    }
    tz = ZoneInfo("America/Chicago")
    # December 2026: last Friday is Dec 25. January 2027: 1st Monday is Jan 4.
    test_now = datetime(2027, 1, 1, 12, 0, tzinfo=tz)
    assert _window_active_now(window, test_now)


# --- end_day: "following <weekday>" ---------------------------------------------------
# The end is the first such weekday after the start day, so a monthly window can
# cross midnight (or a month end) without its end ever falling before its start.

_CT = ZoneInfo("America/Chicago")


def _following(start_day, start_time, end_wd, end_time):
    return {
        "name": "F", "start_day": start_day, "start_time": start_time,
        "end_day": f"following {end_wd}", "end_time": end_time, "monitors": "all",
    }


@pytest.mark.parametrize("year,month", [(y, m) for y in (2026, 2027) for m in range(1, 13)])
def test_following_ends_the_day_after_a_monthly_start_in_every_month(year, month):
    """"2nd Tuesday" 23:00 to "following Wednesday" 01:00 is Tuesday night into
    Wednesday morning in every month, including the ones where the 2nd
    Wednesday is before the 2nd Tuesday (April and July 2026)."""
    window = _following("2nd Tuesday", "23:00", "Wednesday", "01:00")
    tuesday = _nth_weekday_date(year, month, "2nd", 1)
    day = datetime(year, month, tuesday.day, tzinfo=_CT)
    assert not _window_active_now(window, day.replace(hour=22, minute=30))
    assert _window_active_now(window, day.replace(hour=23, minute=30))
    assert _window_active_now(window, (day + timedelta(days=1)).replace(minute=30))
    assert not _window_active_now(window, (day + timedelta(days=1)).replace(hour=1, minute=30))
    assert not _window_active_now(window, day + timedelta(days=9, hours=12))


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 7, 12, 23, 45, tzinfo=_CT), True),    # Sunday, after the start
    (datetime(2026, 7, 13, 0, 15, tzinfo=_CT), True),     # Monday, before the end
    (datetime(2026, 7, 13, 1, 0, tzinfo=_CT), False),     # Monday, after the end
    (datetime(2026, 7, 12, 22, 0, tzinfo=_CT), False),    # Sunday, before the start
    (datetime(2026, 7, 15, 12, 0, tzinfo=_CT), False),    # Wednesday
])
def test_following_with_a_weekly_start(when, expected):
    window = _following("Sunday", "23:30", "Monday", "00:30")
    assert _window_active_now(window, when) is expected


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 7, 31, 22, 30, tzinfo=_CT), True),    # the last Friday, July 31
    (datetime(2026, 8, 1, 12, 0, tzinfo=_CT), True),      # into the next month
    (datetime(2026, 8, 3, 5, 0, tzinfo=_CT), True),       # Monday, before the end
    (datetime(2026, 8, 3, 7, 0, tzinfo=_CT), False),      # Monday, after the end
    (datetime(2026, 7, 24, 23, 0, tzinfo=_CT), False),    # a Friday that is not the last
])
def test_following_crosses_a_month_end(when, expected):
    window = _following("last Friday", "22:00", "Monday", "06:00")
    assert _window_active_now(window, when) is expected


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 7, 15, 23, 30, tzinfo=_CT), True),    # Wednesday, after the start
    (datetime(2026, 7, 16, 0, 30, tzinfo=_CT), True),     # Thursday, before the end
    (datetime(2026, 7, 16, 2, 0, tzinfo=_CT), False),     # Thursday, after the end
    (datetime(2026, 7, 22, 23, 30, tzinfo=_CT), False),   # a week later: a one-off
])
def test_following_with_a_date_start(when, expected):
    window = _following("2026-07-15", "23:00", "Thursday", "01:00")
    assert _window_active_now(window, when) is expected


def test_following_the_same_weekday_means_a_week_later():
    """2026-07-15 is a Wednesday, so "following Wednesday" is July 22, not July 15."""
    window = _following("2026-07-15", "23:00", "Wednesday", "01:00")
    assert _window_active_now(window, datetime(2026, 7, 16, 12, 0, tzinfo=_CT))
    assert _window_active_now(window, datetime(2026, 7, 21, 12, 0, tzinfo=_CT))
    assert not _window_active_now(window, datetime(2026, 7, 22, 2, 0, tzinfo=_CT))
