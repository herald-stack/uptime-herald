# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
base.py - The check result type and helpers shared by all checks.
"""

import time
from dataclasses import dataclass, field


# --------------------------------------------------------------------------------------
@dataclass
class CheckResult:
    status: str  # HTTP: actual status code (e.g. "200", "503",
                 # "timeout", "failed"). Ping/TCP: "up"/"down".
    response_ms: float
    error: str | None = None
    details: dict = field(default_factory=dict)


# `status` carries two unrelated kinds of value, and which one you're holding
# depends on where it came from:
#
#   1. A check RESULT -- what this dataclass produces. An HTTP status
#      code as a string ("200", "503"), a transport failure name
#      ("timeout", "failed"), or "up"/"down" for ping/tcp.
#   2. A ROW KIND -- written to checks.status by main.py for rows that
#      aren't a check at all: "maintenance" and "inactive" log rows.
#      Plus "unknown", MonitorState's sentinel for "never checked yet".
#
# Everything downstream has to keep the two straight: is_up() below returns
# False for the row kinds; is_up=NULL in the database distinguishes them from
# real checks; every stats query filters
# `status != 'maintenance' AND status != 'inactive'`;
# dashboard._is_confirmed_down() exists so "unknown" isn't counted as a
# failure; and _compute_display_status() needs a 3-way up/down/nodata answer
# rather than a boolean.
#
# A separate `row_kind` column (check | maintenance | inactive), with `status`
# reserved for real results, would remove the overloading. That is a schema
# migration plus a rewrite of every stats query in both backends, the
# dashboard's classification helpers, and the templates' and JS's status
# handling. The filter sites are findable via `status != 'maintenance'`
# across db_mariadb.py and db_postgres.py.


# --------------------------------------------------------------------------------------
def elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since `start` (a time.monotonic() reading).

    Deliberately unrounded -- every monitor type uses this for
    CheckResult.response_ms, which flows straight into storage. Rounding
    happens only at display/aggregation time (db_mariadb.py/db_postgres.py,
    the dashboard templates and JS), not at measurement time, so the
    configurable `latency_decimals` setting (config.py) sees real
    sub-millisecond precision.
    """
    return (time.monotonic() - start) * 1000


# --------------------------------------------------------------------------------------
def is_up(status: str | None, expected_status: int | None = None) -> bool:
    """Determine if a check status represents a healthy monitor.

    HTTP monitors store the actual HTTP status code or error string.
    Ping/TCP monitors store "up" or "down".
    Returns False for None or empty status (unknown/new monitor).

    expected_status is an http-monitor-only per-monitor override (the
    `expected_status` config field): when set, that exact status code
    -- and only that code -- counts as up, replacing the default 2xx
    range check entirely. Needed for endpoints where a non-2xx response
    is the correct "alive" signal, e.g. an unauthenticated Docker
    Registry v2 API root returning 401. Left unset (None), 200-299 is up.
    """
    if status is None:
        return False
    if status == "up":
        return True
    if status == "down":
        return False
    # HTTP status codes
    if status.isdigit():
        code = int(status)
        if expected_status is not None:
            return code == expected_status
        return 200 <= code < 300
    # Non-numeric error strings (timeout, failed, etc.)
    return False
