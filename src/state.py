# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
state.py - Monitor state shared by both database backends.

MonitorState — the aggregate-state data structure shared by both
database backends (db_mariadb.py / db_postgres.py).

Lives here rather than in either backend module specifically: it's a
plain Python dataclass with no SQL-dialect dependency at all, so there's
nothing to duplicate between the two backends -- both import it from
here instead of each defining their own copy.

hourly_bar_scaffold() is here for the same reason: it's the pure-Python
half of get_hourly_bars(), identical in both backends and dependent on
no dialect at all.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


# --------------------------------------------------------------------------------------
@dataclass
class MonitorState:
    """Aggregate state for a single monitor.

    Checks are NOT stored here -- they live in the checks table.
    """
    name: str
    type: str
    status: str = "unknown"
    last_check: str | None = None
    last_change: str | None = None
    first_check: str | None = None
    total_checks: int = 0
    total_up: int = 0
    total_down: int = 0
    uptime_pct: float = 0.0
    last_alert_sent: str | None = None
    state_changed: bool = False


# --- Hourly bar scaffold --------------------------------------------------------------
#
# get_hourly_bars() returns one entry per hour for the last N hours, and
# every entry needs three preformatted label strings. Those labels depend
# only on (slot_zero, hours, display_tz) -- never on which monitor is
# being rendered.
#
# At the dashboard's default hours=360, building them takes 360 iterations x
# ~5 strftime() calls x 2 timezone conversions. Doing that per monitor, on
# every dashboard render and every /api/status poll, would be CPU-bound label
# formatting whose cost tracks the bucket count rather than the number of check
# rows, which matters on the small-SBC hardware this project targets.
#
# Since the work is identical across monitors, it's computed once and
# cached. The cache key includes slot_zero, so it self-invalidates every
# hour rather than needing explicit expiry.
_SCAFFOLD_CACHE: dict[tuple, list[dict]] = {}
_SCAFFOLD_CACHE_MAX = 8


# --------------------------------------------------------------------------------------
def hourly_bar_scaffold(
    slot_zero: datetime,
    hours: int,
    display_tz=None,
) -> list[dict]:
    """Per-hour bucket keys and display labels, newest slot first.

    Returns `hours` dicts of {bucket_key, hour, hour_display,
    hour_display_end}. `bucket_key` matches the 'YYYY-MM-DD HH:00' string
    both backends GROUP BY on, so a caller can look its own counts up
    without recomputing any of this.

    Cached on (slot_zero, hours, display_tz) -- callers pass an
    hour-aligned slot_zero, so entries age out naturally as the hour
    rolls over.
    """
    key = (slot_zero, hours, str(display_tz))
    cached = _SCAFFOLD_CACHE.get(key)
    if cached is not None:
        return cached

    scaffold = []
    for i in range(hours):
        slot_start = slot_zero - timedelta(hours=i)
        hour_utc = slot_start.strftime("%Y-%m-%dT%H:%M")
        hour_display = hour_utc
        hour_display_end = ""
        if display_tz is not None:
            utc_dt = slot_start.replace(tzinfo=timezone.utc)
            hour_display = utc_dt.astimezone(display_tz).strftime(
                "%Y-%m-%d %H:%M"
            )
            end_dt = utc_dt + timedelta(hours=1)
            hour_display_end = end_dt.astimezone(display_tz).strftime(
                "%H:%M %Z"
            )
        scaffold.append({
            "bucket_key": slot_start.strftime("%Y-%m-%d %H:00"),
            "hour": hour_utc,
            "hour_display": hour_display,
            "hour_display_end": hour_display_end,
        })

    if len(_SCAFFOLD_CACHE) >= _SCAFFOLD_CACHE_MAX:
        # Plain FIFO eviction -- keys are (hour, hours, tz) triples, so
        # the realistic working set is tiny (one or two per process).
        _SCAFFOLD_CACHE.pop(next(iter(_SCAFFOLD_CACHE)))
    _SCAFFOLD_CACHE[key] = scaffold
    return scaffold


# --------------------------------------------------------------------------------------
def counts_toward_stats(status: str, details: dict | None) -> bool:
    """Whether a check row is counted by the uptime/latency statistics.

    The same rule the stats queries apply in SQL (see _EXCLUDE_VERIFYING_SQL
    in the backends): maintenance and inactive log rows never count, and a
    check made while a state change is still being verified counts only if it
    is the one that confirmed the change (`passed`). Used at insert time to
    keep the hourly summary table (check_hourly) in step with what the raw-row
    queries would have counted.
    """
    if status in ("maintenance", "inactive"):
        return False
    if not details:
        return True
    verifying = details.get("verifying")
    if verifying is None or verifying is False:
        return True
    return details.get("passed") is True


# --------------------------------------------------------------------------------------
def is_state_change(
    has_previous: bool,
    previous_is_up: bool | None,
    is_up: bool | None,
    details: dict | None,
) -> bool:
    """Whether a check belongs in the "state changes" check log.

    The rule for the log, applied once when the check is inserted rather
    than with a window function over the whole history on every request: the
    first check of a monitor, a check whose health differs from the monitor's
    previous check (a missing is_up, as on a maintenance log row, counts as
    its own value), or a check made while a state change is being verified.
    """
    if not has_previous:
        return True
    if (is_up is None) != (previous_is_up is None):
        return True
    if is_up is not None and bool(is_up) != bool(previous_is_up):
        return True
    return details is not None and details.get("verifying") is True


# --------------------------------------------------------------------------------------
def hour_window_start(now: datetime, hours: int) -> datetime:
    """The first hourly slot of a window of `hours` slots ending in the
    current (partial) hour -- the same convention the hourly bars use."""
    return hour_floor(now) - timedelta(hours=hours - 1)


# --------------------------------------------------------------------------------------
def period_window_start(now: datetime, days: int) -> datetime:
    """The first hourly slot of a statistics period of `days` days: the top of
    the current hour, `days` days back. The period then covers at least the
    full `days` days and at most 59 minutes more (the hourly summary cannot
    start a window in the middle of an hour)."""
    return hour_floor(now) - timedelta(days=days)


# --------------------------------------------------------------------------------------
def hour_floor(moment: datetime) -> datetime:
    """The start of the hour `moment` falls in (same tzinfo, no minutes)."""
    return moment.replace(minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------------------
def bar_status(up_count: int, down_count: int) -> str:
    """Classify one hourly bucket from its up/down tallies.

    Shared by both backends for the same reason as the scaffold: it is
    pure classification logic with nothing dialect-specific about it.
    """
    if up_count + down_count == 0:
        return "nodata"
    if down_count == 0:
        return "up"
    if up_count == 0:
        return "down"
    return "mixed"


# --- Connection pool sizing -----------------------------------------------------------
#
# Shared by both backends for the same reason as the helpers above: it is
# pure, dialect-free parsing with nothing MariaDB- or Postgres-specific
# about it. Deliberately env-var driven rather than read from
# config.yaml: the DB_* settings are infrastructure for
# docker-compose.yaml, fixed at process start, and the backends never
# import src/config.py (see src/db.py's docstring on why DB_BACKEND
# works the same way).

POOL_MIN_SIZE_ENV = "DB_POOL_MIN_SIZE"
POOL_MAX_SIZE_ENV = "DB_POOL_MAX_SIZE"
DEFAULT_POOL_MIN_SIZE = 1
DEFAULT_POOL_MAX_SIZE = 5


# --------------------------------------------------------------------------------------
def _pool_int(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(
            f"{env_name} must be an integer, got {raw!r}"
        ) from None
    if value < 1:
        raise ValueError(
            f"{env_name} must be at least 1, got {value}. A pool that can "
            f"hand out no connections would deadlock the monitoring loop "
            f"on its first check."
        )
    return value


# --------------------------------------------------------------------------------------
def pool_sizes() -> tuple[int, int]:
    """Return (min_size, max_size) for the connection pool.

    Configurable because the right size depends entirely on how many
    monitors are configured, which nothing here can predict -- and
    because db.transaction() holds a connection for its whole duration,
    so the pool ceiling is what bounds concurrent check writes. The
    defaults are a minimum of 1 and a maximum of 5.

    Validated here rather than left to the driver: aiomysql and asyncpg
    disagree about what they accept (aiomysql treats maxsize=0 as
    unlimited, asyncpg rejects max_size < 1), and a bad value should fail
    loudly at startup with a clear message rather than produce a pool
    that deadlocks or silently ignores the ceiling.
    """
    min_size = _pool_int(POOL_MIN_SIZE_ENV, DEFAULT_POOL_MIN_SIZE)
    max_size = _pool_int(POOL_MAX_SIZE_ENV, DEFAULT_POOL_MAX_SIZE)
    if min_size > max_size:
        raise ValueError(
            f"{POOL_MIN_SIZE_ENV} ({min_size}) cannot exceed "
            f"{POOL_MAX_SIZE_ENV} ({max_size})"
        )
    return min_size, max_size


# --- Connection settings --------------------------------------------------------------
#
# Shared by both backends, like pool_sizes() above. The software assumes
# nothing about where the database is or how to log in: every setting
# below must be declared in the environment (docker-compose supplies
# them), and a missing one stops startup with a message naming it.

DB_CONNECTION_ENV = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


# --------------------------------------------------------------------------------------
def db_connection_settings() -> tuple[str, int, str, str, str]:
    """Return (host, port, name, user, password) from the environment.

    Raises ValueError, naming every missing setting at once, when any of
    DB_HOST, DB_PORT, DB_NAME, DB_USER or DB_PASSWORD is unset or empty.
    There are deliberately no built-in fallbacks, including for the
    password: a fallback credential is one a user can run with by
    accident.
    """
    values = {name: os.environ.get(name, "") for name in DB_CONNECTION_ENV}
    missing = [name for name, value in values.items() if value.strip() == ""]
    if missing:
        raise ValueError(
            f"Required database setting(s) not set: {', '.join(missing)}. "
            f"Declare them in .env (docker compose passes them to the app)."
        )
    try:
        port = int(values["DB_PORT"].strip())
    except ValueError:
        raise ValueError(
            f"DB_PORT must be an integer, got {values['DB_PORT']!r}"
        ) from None
    return (
        values["DB_HOST"].strip(),
        port,
        values["DB_NAME"].strip(),
        values["DB_USER"].strip(),
        values["DB_PASSWORD"],
    )
