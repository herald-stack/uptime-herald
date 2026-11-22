# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
maintenance.py - Decides whether a monitor is in a maintenance window.

Evaluates whether a monitor is currently in a maintenance window
based on the `maintenance` section of the assembled config dict,
sourced from maintenance.yaml.
"""

import logging
from datetime import datetime, timedelta, timezone

from src.config import MONTHLY_PATTERN_RE as _MONTHLY_RE
from src.config import FOLLOWING_PATTERN_RE as _FOLLOWING_RE
from src.config import (
    WEEKDAYS,
    entry_applies_to_monitor,
    following_days_ahead,
    get_display_tz,
)

logger = logging.getLogger(__name__)

# Map ordinal strings to zero-based week index
_ORDINAL_MAP = {"1st": 0, "2nd": 1, "3rd": 2, "4th": 3}


# --------------------------------------------------------------------------------------
def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


# --------------------------------------------------------------------------------------
def _is_weekday_name(value: str) -> bool:
    """Check if the value is a weekday name (case-insensitive)."""
    return value.lower() in WEEKDAYS


# --------------------------------------------------------------------------------------
def _is_monthly_pattern(value: str) -> bool:
    """Check if the value is an ordinal + weekday (e.g. '2nd Tuesday')."""
    return bool(_MONTHLY_RE.match(value))


# --------------------------------------------------------------------------------------
def _parse_monthly_pattern(value: str) -> tuple[str, int]:
    """Parse '2nd Tuesday' into ('2nd', 1) — ordinal and weekday number.

    Raises ValueError if `value` isn't an ordinal-weekday pattern or the
    weekday name is unrecognized. The pattern check isn't reachable from the
    only caller (_window_active_now gates on _is_monthly_pattern() first); it
    is explicit so a direct call gets the documented ValueError, not an
    AttributeError on None.
    """
    match = _MONTHLY_RE.match(value)
    if match is None:
        raise ValueError(
            f"Not an ordinal-weekday pattern: {value!r} "
            f"(expected e.g. '2nd Tuesday' or 'last Friday')"
        )
    ordinal = match.group(1).lower()
    weekday_name = match.group(2).lower()
    if weekday_name not in WEEKDAYS:
        raise ValueError(f"Unknown weekday: {weekday_name}")
    return ordinal, WEEKDAYS[weekday_name]


# --------------------------------------------------------------------------------------
def _nth_weekday_date(
    year: int, month: int, ordinal: str, weekday: int
) -> datetime:
    """Get the date of the Nth (or last) weekday in a given month.

    Args:
        year: e.g. 2026
        month: 1-12
        ordinal: "1st", "2nd", "3rd", "4th", or "last"
        weekday: 0=Monday .. 6=Sunday

    Returns:
        datetime at midnight (00:00) on the target date.

    _ORDINAL_MAP only ever supplies "1st"-"4th" (never "5th"+), and every
    month has at least 4 of every weekday, so the Nth weekday always exists
    and there is no bounds check. If a "5th"-style ordinal is ever added to
    _ORDINAL_MAP, this needs one.
    """
    first = datetime(year, month, 1)
    first_weekday = first.weekday()
    offset = (weekday - first_weekday) % 7
    first_occurrence = first + timedelta(days=offset)

    if ordinal == "last":
        target = first_occurrence
        while True:
            next_occ = target + timedelta(weeks=1)
            if next_occ.month != month:
                break
            target = next_occ
        return target
    else:
        return first_occurrence + timedelta(weeks=_ORDINAL_MAP[ordinal])


# --------------------------------------------------------------------------------------
def _following_weekly_active(
    start_wd: int, end_wd: int, start_hm: tuple, end_hm: tuple, now_local: datetime,
) -> bool:
    """A weekly window that ends on "following <weekday>"."""
    week = 7 * 24 * 60
    now_week = now_local.weekday() * 1440 + now_local.hour * 60 + now_local.minute
    start_week = start_wd * 1440 + start_hm[0] * 60 + start_hm[1]
    lasts = (
        following_days_ahead(start_wd, end_wd) * 1440
        + (end_hm[0] * 60 + end_hm[1]) - (start_hm[0] * 60 + start_hm[1])
    )
    return (now_week - start_week) % week < lasts


# --------------------------------------------------------------------------------------
def _following_span_active(
    start_date: datetime, end_wd: int, start_hm: tuple, end_hm: tuple,
    now_local: datetime,
) -> bool:
    """One window that starts on `start_date` and ends on the first `end_wd`
    after it."""
    end_date = start_date + timedelta(
        days=following_days_ahead(start_date.weekday(), end_wd)
    )
    window_start = start_date.replace(
        hour=start_hm[0], minute=start_hm[1], tzinfo=now_local.tzinfo
    )
    window_end = end_date.replace(
        hour=end_hm[0], minute=end_hm[1], tzinfo=now_local.tzinfo
    )
    return window_start <= now_local < window_end


# --------------------------------------------------------------------------------------
def _following_window_active(
    start_day: str, end_wd: int, start_hm: tuple, end_hm: tuple,
    now_local: datetime,
) -> bool:
    """A window whose end_day is "following <weekday>": it ends on the first
    such weekday after the day it starts on, whatever kind of day that is."""
    if _is_weekday_name(start_day):
        return _following_weekly_active(
            WEEKDAYS[start_day.lower()], end_wd, start_hm, end_hm, now_local
        )
    if not _is_monthly_pattern(start_day):
        start_date = datetime.strptime(start_day, "%Y-%m-%d")
        return _following_span_active(start_date, end_wd, start_hm, end_hm, now_local)
    # A monthly window that starts late in one month can still be running in
    # the next, so the previous month's window is checked as well.
    start_ord, start_wd = _parse_monthly_pattern(start_day)
    year, month = now_local.year, now_local.month
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    return any(
        _following_span_active(
            _nth_weekday_date(anchor_year, anchor_month, start_ord, start_wd),
            end_wd, start_hm, end_hm, now_local,
        )
        for anchor_year, anchor_month in ((prev_year, prev_month), (year, month))
    )


# --------------------------------------------------------------------------------------
def _window_active_now(
    window: dict,
    now_local: datetime,
) -> bool:
    """Check if a single maintenance window is currently active.

    Args:
        window: maintenance window config dict
        now_local: current time in the configured display timezone

    Returns:
        True if the window is currently active.
    """
    start_day = window["start_day"]
    end_day = window["end_day"]
    start_hour, start_min = _parse_time(window["start_time"])
    end_hour, end_min = _parse_time(window["end_time"])

    following = _FOLLOWING_RE.match(end_day)
    if following:
        return _following_window_active(
            start_day, WEEKDAYS[following.group(1).lower()],
            (start_hour, start_min), (end_hour, end_min), now_local,
        )

    if _is_weekday_name(start_day) and _is_weekday_name(end_day):
        # Recurring weekly window
        start_wd = WEEKDAYS[start_day.lower()]
        end_wd = WEEKDAYS[end_day.lower()]

        now_wd = now_local.weekday()
        now_minutes = now_local.hour * 60 + now_local.minute
        start_minutes = start_hour * 60 + start_min
        end_minutes = end_hour * 60 + end_min

        # Build a weekly timeline in minutes from Monday 00:00
        now_week = now_wd * 24 * 60 + now_minutes
        start_week = start_wd * 24 * 60 + start_minutes
        end_week = end_wd * 24 * 60 + end_minutes

        if start_week <= end_week:
            # Same-day window (e.g. Sunday 02:00 - Sunday 04:00)
            return start_week <= now_week < end_week
        else:
            # Window wraps past midnight (e.g. Sunday 23:00 - Monday 02:00)
            # But for weekly, wrap means past Sunday midnight into Monday
            # Active if now >= start OR now < end
            return now_week >= start_week or now_week < end_week

    elif _is_monthly_pattern(start_day) and _is_monthly_pattern(end_day):
        # Monthly recurring window (e.g. "2nd Tuesday" for Patch Tuesday,
        # or "last Friday" -> "1st Monday" for an end-of-month window that
        # crosses into the next calendar month).
        start_ord, start_wd = _parse_monthly_pattern(start_day)
        end_ord, end_wd = _parse_monthly_pattern(end_day)

        # A window whose start anchor falls in month M can still be active
        # when `now` has rolled into month M+1 (e.g. "last Friday" of July
        # through "1st Monday" of August, checked on Aug 1). Anchoring both
        # start and end to now's month misses that entirely. Instead, build
        # the two candidate windows that could plausibly contain `now` —
        # one anchored to the previous month, one to the current month —
        # and check both.
        year, month = now_local.year, now_local.month
        prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)

        for anchor_year, anchor_month in (
            (prev_year, prev_month),
            (year, month),
        ):
            start_date = _nth_weekday_date(
                anchor_year, anchor_month, start_ord, start_wd
            )

            # Find the end anchor in the same month as start, or the next
            # month if that's earlier in the calendar than start (the
            # month-crossing case).
            end_month_year, end_month = anchor_year, anchor_month
            end_date = _nth_weekday_date(
                end_month_year, end_month, end_ord, end_wd
            )
            if end_date < start_date:
                end_month_year, end_month = (
                    (end_month_year + 1, 1) if end_month == 12
                    else (end_month_year, end_month + 1)
                )
                end_date = _nth_weekday_date(
                    end_month_year, end_month, end_ord, end_wd
                )

            window_start = start_date.replace(
                hour=start_hour, minute=start_min, tzinfo=now_local.tzinfo,
            )
            window_end = end_date.replace(
                hour=end_hour, minute=end_min, tzinfo=now_local.tzinfo,
            )

            if window_start <= now_local < window_end:
                return True

        return False

    else:
        # One-off window with ISO dates
        start_date = datetime.strptime(start_day, "%Y-%m-%d")
        end_date = datetime.strptime(end_day, "%Y-%m-%d")

        window_start = start_date.replace(
            hour=start_hour, minute=start_min, tzinfo=now_local.tzinfo
        )
        window_end = end_date.replace(
            hour=end_hour, minute=end_min, tzinfo=now_local.tzinfo
        )

        return window_start <= now_local < window_end


# --------------------------------------------------------------------------------------
def _find_monitor_tags(config: dict, monitor_name: str) -> set:
    """Look up a monitor's tags from config, for tag-based maintenance
    window targeting."""
    for m in config.get("monitors", []):
        if m["name"] == monitor_name:
            return set(m.get("tags", []))
    return set()


# --------------------------------------------------------------------------------------
def _check_window_active(window: dict, now_local: datetime) -> str | None:
    """Evaluate a single maintenance window.

    Returns the window's display name if it's currently active, else None.
    Logs a warning (rather than raising) on malformed window data.

    The caught set covers every way a hand-edited maintenance.yaml can
    be wrong, because this runs inside the dashboard render path
    (_build_monitor_list) as well as the monitoring loop -- an escaping
    exception here is a 500 on /, /kiosk and /api/status, not just a
    skipped scheduling pass. IndexError: a start_time/end_time with no
    colon ("0200") reaching _parse_time's parts[1]. AttributeError: a
    field that isn't a string at all, e.g. an unquoted `start_day:
    2026-07-15` (parsed as a date) or a key left empty (None).
    """
    try:
        if _window_active_now(window, now_local):
            return window["name"]
    except (KeyError, ValueError, TypeError, IndexError, AttributeError) as e:
        logger.warning(
            "Invalid maintenance window '%s': %s",
            window.get("name", "?"), e,
        )
    return None


# --------------------------------------------------------------------------------------
def get_active_maintenance(
    config: dict,
    monitor_name: str,
) -> list[str]:
    """Return list of active maintenance window names for a monitor.

    Args:
        config: full config dict
        monitor_name: name of the monitor to check

    Returns:
        List of window names that are currently active for this monitor.
        Empty list if no windows are active.
    """
    windows = config.get("maintenance", [])
    if not windows:
        return []

    tz = get_display_tz(config)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    monitor_tags = _find_monitor_tags(config, monitor_name)

    active = []
    for window in windows:
        if not entry_applies_to_monitor(window, monitor_name, monitor_tags):
            continue
        name = _check_window_active(window, now_local)
        if name is not None:
            active.append(name)

    return active
