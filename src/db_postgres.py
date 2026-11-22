# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
db_postgres.py - The PostgreSQL database backend.

The other of two backend modules with an identical function surface --
db_mariadb.py is the other. src/db.py picks between them at import time
based on DB_BACKEND and re-exports whichever one is selected. See
src/db.py's docstring for the dispatch mechanism.

Deliberately a separate, largely-duplicated module rather than a shared
abstraction layer (an ORM, a query builder) over both dialects: the project
is deliberately minimal-dependency and plain-SQL throughout, and pulling in
something like SQLAlchemy to unify the two dialects would cut against that.
The cost is real duplication between this file and db_mariadb.py; the benefit
is that neither file needs anything beyond its own driver, and each stays
plain and readable.

Key dialect differences from the MariaDB backend, worth knowing before
touching this file:
    - asyncpg has no DB-API cursor object -- connections expose
      fetch()/fetchrow()/fetchval()/execute() directly.
    - Placeholders are numbered ($1, $2, ...), not %s.
    - JSONB columns need an explicit ::jsonb cast on the placeholder
      when asyncpg is handed a plain Python str (it doesn't guess).
    - MariaDB's REGEXP -> Postgres's ~ (and NOT REGEXP -> !~).
    - MariaDB's JSON_VALUE(col, '$.key') -> Postgres's col->>'key'.
    - MariaDB's DATE_FORMAT(...) -> Postgres's TO_CHAR(...).
    - MariaDB's INSERT ... ON DUPLICATE KEY UPDATE ->
      Postgres's INSERT ... ON CONFLICT (...) DO UPDATE SET ... EXCLUDED.
    - Postgres supports ALTER TABLE ... ADD COLUMN IF NOT EXISTS
      natively, so the migration step doesn't need the MariaDB
      backend's "catch duplicate-column-name error" dance at all.

Environment variables (same names as the MariaDB backend, so switching
DB_BACKEND doesn't also require renaming every other DB_* variable):
    DB_HOST     - database hostname (required)
    DB_PORT     - database port (required)
    DB_NAME     - database name (required)
    DB_USER     - database user (required)
    DB_PASSWORD - database password (required)
                  None of the five has a built-in fallback: a
                  missing one stops startup (see
                  state.db_connection_settings()).
    DB_POOL_MIN_SIZE - connections held open (default: 1)
    DB_POOL_MAX_SIZE - connection ceiling (default: 5). Raise it for
                  deployments with many monitors: db.transaction()
                  holds a connection for its whole duration, so this
                  is what bounds concurrent check writes.
"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg

from src.state import (
    MonitorState,
    bar_status,
    counts_toward_stats,
    db_connection_settings,
    hour_floor,
    hour_window_start,
    period_window_start,
    hourly_bar_scaffold,
    is_state_change,
    pool_sizes,
)

logger = logging.getLogger(__name__)

# See db_mariadb.py's matching comment for the full reasoning -- these are
# fixed literals with no path back to caller-supplied input, don't
# parameterize them. Same is_up-column-based design as that file too:
# classification happens once in Python at insert time (src/main.py, using
# monitors/base.py's is_up(), which knows about a monitor's expected_status
# override), not re-derived here from the raw status code -- is_up is NULL
# (not TRUE/FALSE) for maintenance/inactive log rows, so `is_up = TRUE`/
# `is_up = FALSE` exclude them for free via three-valued logic.
_IS_UP_SQL = "is_up = TRUE"
_PREV_IS_UP_SQL = "prev_is_up = TRUE"
_IS_DOWN_SQL = "is_up = FALSE"
# Excludes in-flight retry-verification check rows (details.verifying=true)
# from stats, EXCEPT the one that confirms the new state (details.passed=true).
# See db_mariadb.py's matching comment for the full reasoning.
_EXCLUDE_VERIFYING_SQL = (
    "(details IS NULL OR details->>'verifying' IS NULL "
    "OR details->>'verifying' = 'false' "
    "OR details->>'passed' = 'true')"
)

# --- Config ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS monitors (
    name             VARCHAR(255) NOT NULL PRIMARY KEY,
    type             VARCHAR(50)  NOT NULL,
    status           VARCHAR(20)  NOT NULL DEFAULT 'unknown',
    last_check       TIMESTAMPTZ  NULL,
    last_change      TIMESTAMPTZ  NULL,
    first_check      TIMESTAMPTZ  NULL,
    total_checks     INT          NOT NULL DEFAULT 0,
    total_up         INT          NOT NULL DEFAULT 0,
    total_down       INT          NOT NULL DEFAULT 0,
    uptime_pct       DECIMAL(5,2) NOT NULL DEFAULT 0.00,
    last_alert_sent  TIMESTAMPTZ  NULL,
    state_changed    BOOLEAN      NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS checks (
    id            BIGSERIAL    PRIMARY KEY,
    monitor_name  VARCHAR(255) NOT NULL,
    check_time    TIMESTAMPTZ  NOT NULL,
    -- VARCHAR(20), not (10): the longest real value is the
    -- 'maintenance' log-row marker at 11 characters, and Postgres
    -- rejects an over-length value outright (22001), so at (10) every
    -- maintenance log row failed to insert. See db_mariadb.py's twin.
    status        VARCHAR(20)  NOT NULL,
    is_up         BOOLEAN      NULL,
    response_ms   REAL         NULL,
    error         TEXT         NULL,
    details       JSONB        NULL,
    CONSTRAINT fk_monitor
        FOREIGN KEY (monitor_name) REFERENCES monitors(name)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_monitor_time ON checks (monitor_name, check_time);

-- For "all checks, newest first" (the check log when view_all_checks is on).
-- Without it that is a sort of the whole table on every request.
CREATE INDEX IF NOT EXISTS idx_check_time ON checks (check_time);

-- The checks that belong in the "state changes" check log (the default view),
-- recorded by insert_check() as each check arrives -- see db_mariadb.py's
-- twin. Rows go when their check is pruned or its monitor deleted
-- (ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS check_changes (
    check_id      BIGINT       NOT NULL PRIMARY KEY,
    monitor_name  VARCHAR(255) NOT NULL,
    check_time    TIMESTAMPTZ  NOT NULL,
    CONSTRAINT fk_changes_check
        FOREIGN KEY (check_id) REFERENCES checks(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_changes_time ON check_changes (check_time);
CREATE INDEX IF NOT EXISTS idx_changes_monitor_time
    ON check_changes (monitor_name, check_time);

-- Which one-time system notices (e.g. database end-of-support reminders)
-- have already been sent, so a restart doesn't send them again.
CREATE TABLE IF NOT EXISTS system_notices (
    notice_key  VARCHAR(255) NOT NULL PRIMARY KEY,
    sent_at     TIMESTAMPTZ  NOT NULL
);

-- One row per monitor per hour (UTC), kept up to date by insert_check().
-- See db_mariadb.py's twin for what each column counts and why it exists.
CREATE TABLE IF NOT EXISTS check_hourly (
    monitor_name  VARCHAR(255)     NOT NULL,
    hour_start    TIMESTAMPTZ      NOT NULL,
    up_count      INT              NOT NULL DEFAULT 0,
    down_count    INT              NOT NULL DEFAULT 0,
    stat_total    INT              NOT NULL DEFAULT 0,
    stat_up       INT              NOT NULL DEFAULT 0,
    stat_down     INT              NOT NULL DEFAULT 0,
    resp_sum      DOUBLE PRECISION NOT NULL DEFAULT 0,
    resp_count    INT              NOT NULL DEFAULT 0,
    PRIMARY KEY (monitor_name, hour_start),
    CONSTRAINT fk_hourly_monitor
        FOREIGN KEY (monitor_name) REFERENCES monitors(name)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_hourly_time ON check_hourly (hour_start);
"""


# --- Pool management ------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


# --------------------------------------------------------------------------------------
async def init_pool() -> None:
    """Create the connection pool. Call once at startup."""
    global _pool
    if _pool is not None:
        return
    db_host, db_port, db_name, db_user, db_password = (
        db_connection_settings()
    )
    min_size, max_size = pool_sizes()
    _pool = await asyncpg.create_pool(
        host=db_host,
        port=db_port,
        database=db_name,
        user=db_user,
        password=db_password,
        min_size=min_size,
        max_size=max_size,
    )
    logger.info(
        "DB pool created: %s@%s:%d/%s (pool %d-%d)",
        db_user, db_host, db_port, db_name, min_size, max_size,
    )


# --------------------------------------------------------------------------------------
async def close_pool() -> None:
    """Close the connection pool. Call on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("DB pool closed")


# --------------------------------------------------------------------------------------
async def init_schema() -> None:
    """Create tables if they don't exist."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised -- call init_pool() first")
    async with _pool.acquire() as conn:
        for statement in SCHEMA_SQL.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                await conn.execute(stmt)
        # Add state_changed column if missing (migration for existing DBs).
        # Unlike the MariaDB backend, Postgres supports IF NOT EXISTS on
        # ADD COLUMN directly -- no need to catch a duplicate-column error.
        await conn.execute(
            "ALTER TABLE monitors ADD COLUMN IF NOT EXISTS state_changed "
            "BOOLEAN NOT NULL DEFAULT FALSE"
        )
        # Add is_up column if missing (migration for existing DBs). New
        # rows always get an explicit is_up from insert_check() going
        # forward; the backfill below only ever has real work to do once,
        # for check rows that predate this column, and is safe to run on
        # every startup after that since it will simply match zero rows.
        await conn.execute(
            "ALTER TABLE checks ADD COLUMN IF NOT EXISTS is_up BOOLEAN NULL"
        )
        # Widen checks.status if this DB predates the VARCHAR(20) schema
        # above. 'maintenance' is 11 characters, so on a VARCHAR(10) column
        # Postgres rejects every maintenance log row with 22001.
        #
        # Guarded on the current length rather than run unconditionally,
        # for the same reason as db_mariadb.py's twin. Unconditionally,
        # `ALTER COLUMN status TYPE VARCHAR(20)` is not the no-op it looks
        # like: on a column that is already WIDER than 20 it is a
        # narrowing conversion, which fails outright (22001) once any row
        # exceeds 20 characters and takes the app down on every start. It
        # also takes an ACCESS EXCLUSIVE lock on `checks` every boot.
        status_len = await conn.fetchval(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'checks' "
            "AND column_name = 'status'"
        )
        if status_len is not None and int(status_len) < 20:
            await conn.execute(
                "ALTER TABLE checks ALTER COLUMN status TYPE VARCHAR(20)"
            )
            logger.info(
                "Widened checks.status to VARCHAR(20) so 'maintenance' "
                "log rows fit"
            )
        # Repair rows truncated by a VARCHAR(10) column before the backfill
        # runs -- see db_mariadb.py's twin for why the order matters. Postgres
        # rejects over-length values outright rather than truncating, so this
        # only finds rows on a database that was once served by a non-strict
        # MariaDB and migrated across; it is a cheap no-op otherwise.
        repaired = await conn.execute(
            "UPDATE checks SET status = 'maintenance' "
            "WHERE status = 'maintenanc' AND is_up IS NULL"
        )
        if repaired and not repaired.endswith(" 0"):
            logger.info(
                "Repaired maintenance log rows truncated by the old "
                "VARCHAR(10) status column: %s", repaired,
            )
        backfilled = await conn.execute(
            "UPDATE checks SET is_up = "
            "(status = 'up' OR status ~ '^2[0-9]{2}$') "
            "WHERE is_up IS NULL "
            "AND status != 'maintenance' AND status != 'inactive'"
        )
        # asyncpg's execute() returns a status string like "UPDATE 42" --
        # log it as-is rather than parsing out the count, it's only for
        # an operator glancing at startup logs, not consumed programmatically.
        if backfilled != "UPDATE 0":
            logger.info("Backfilled is_up for pre-existing check rows: %s", backfilled)
        await _backfill_check_hourly(conn)
        await _backfill_check_changes(conn)
    logger.info("DB schema verified")


# --------------------------------------------------------------------------------------
async def _backfill_check_changes(conn) -> None:
    """Fill check_changes from the existing checks, once.

    Only runs when the table is empty and checks exist, i.e. on a database
    that predates it. Applies the window-function rule over the whole history
    in one pass.
    """
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM check_changes)"):
        return
    if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM checks)"):
        return
    logger.info(
        "Building the list of state-change checks (one time; this can take "
        "a while on a large history)"
    )
    result = await conn.execute(
        """
        INSERT INTO check_changes (check_id, monitor_name, check_time)
        SELECT id, monitor_name, check_time FROM (
            SELECT id, monitor_name, check_time, is_up, details,
                   LAG(status) OVER (
                       PARTITION BY monitor_name ORDER BY check_time
                   ) AS prev_status,
                   LAG(is_up) OVER (
                       PARTITION BY monitor_name ORDER BY check_time
                   ) AS prev_is_up
            FROM checks
        ) AS t
        WHERE prev_status IS NULL
            OR is_up IS DISTINCT FROM prev_is_up
            OR (details->>'verifying')::boolean IS TRUE
        """
    )
    logger.info("State-change list built: %s", result)


# --------------------------------------------------------------------------------------
async def _backfill_check_hourly(conn) -> None:
    """Fill check_hourly from the existing checks, once.

    Only runs when the summary table is empty and checks exist, i.e. on a
    database that predates it. The counts match the raw check rows.
    """
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM check_hourly)"):
        return
    if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM checks)"):
        return
    logger.info(
        "Building the hourly summary of existing checks (one time; this can "
        "take a while on a large history)"
    )
    counted = (
        "status != 'maintenance' AND status != 'inactive' "
        f"AND {_EXCLUDE_VERIFYING_SQL}"
    )
    result = await conn.execute(
        f"""
        INSERT INTO check_hourly
            (monitor_name, hour_start, up_count, down_count, stat_total,
             stat_up, stat_down, resp_sum, resp_count)
        SELECT monitor_name,
               date_trunc('hour', check_time AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
               COUNT(*) FILTER (WHERE {_IS_UP_SQL}),
               COUNT(*) FILTER (WHERE {_IS_DOWN_SQL}),
               COUNT(*) FILTER (WHERE {counted}),
               COUNT(*) FILTER (WHERE {counted} AND {_IS_UP_SQL}),
               COUNT(*) FILTER (WHERE {counted} AND {_IS_DOWN_SQL}),
               COALESCE(SUM(response_ms) FILTER (
                   WHERE {counted} AND {_IS_UP_SQL} AND response_ms IS NOT NULL
               ), 0),
               COUNT(*) FILTER (
                   WHERE {counted} AND {_IS_UP_SQL} AND response_ms IS NOT NULL
               )
        FROM checks
        GROUP BY monitor_name,
                 date_trunc('hour', check_time AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        """
    )
    logger.info("Hourly summary built: %s", result)


# --- State operations -----------------------------------------------------------------

# --------------------------------------------------------------------------------------
def _state_from_row(row) -> MonitorState:
    return MonitorState(
        name=row["name"],
        type=row["type"],
        status=row["status"],
        last_check=_dt_to_iso(row["last_check"]),
        last_change=_dt_to_iso(row["last_change"]),
        first_check=_dt_to_iso(row["first_check"]),
        total_checks=row["total_checks"],
        total_up=row["total_up"],
        total_down=row["total_down"],
        uptime_pct=float(row["uptime_pct"]),
        last_alert_sent=_dt_to_iso(row["last_alert_sent"]),
        state_changed=bool(row["state_changed"]),
    )


# --------------------------------------------------------------------------------------
async def load_state(name: str, monitor_type: str) -> MonitorState:
    """Load a monitor's aggregate state, or return a fresh state if not found."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM monitors WHERE name = $1", name,
        )
    if row is None:
        return MonitorState(name=name, type=monitor_type)
    return _state_from_row(row)


# --------------------------------------------------------------------------------------
async def load_states(monitors: list[tuple[str, str]]) -> dict[str, MonitorState]:
    """load_state() for many monitors at once: {name: state}, in one query.

    `monitors` is a list of (name, type); a monitor with no row yet gets a
    fresh state, same as load_state().
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        rows = {row["name"]: row for row in await conn.fetch("SELECT * FROM monitors")}
    return {
        name: (
            _state_from_row(rows[name]) if name in rows
            else MonitorState(name=name, type=monitor_type)
        )
        for name, monitor_type in monitors
    }


# --------------------------------------------------------------------------------------
@asynccontextmanager
async def transaction():
    """Run several writes as one atomic unit.

    Yields a backend connection to pass as the `conn=` argument of the
    write functions below, so they all run on the same connection inside
    one transaction:

        async with db.transaction() as conn:
            await db.save_state(state, conn=conn)
            await db.insert_check(..., conn=conn)

    Commits when the block exits normally; rolls back and re-raises if
    anything inside it raises. See db_mariadb.transaction()'s docstring
    for the desynchronisation this exists to prevent.

    asyncpg has no autocommit setting to work around -- every statement
    is its own implicit transaction unless wrapped -- so this delegates
    to asyncpg's own Connection.transaction(), which handles the
    commit/rollback semantics natively.
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        async with conn.transaction():
            yield conn


# --------------------------------------------------------------------------------------
@asynccontextmanager
async def _connection(conn):
    """Yield `conn` when given, else a pooled connection.

    The asyncpg counterpart to db_mariadb's _cursor() -- lets every write
    function below take an optional `conn=` and join a caller's
    transaction without duplicating its SQL for the two cases.
    """
    if conn is not None:
        yield conn
        return
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as own_conn:
        yield own_conn


# --------------------------------------------------------------------------------------
async def save_state(state: MonitorState, conn=None) -> None:
    """Upsert a monitor's aggregate state.

    Pass `conn` from db.transaction() to make this atomic with other
    writes; omit it and this commits on its own.
    """
    if _pool is None and conn is None:
        raise RuntimeError("DB pool not initialised")
    if state.total_checks > 0:
        state.uptime_pct = round(state.total_up / state.total_checks * 100, 2)
    async with _connection(conn) as c:
        await c.execute(
            """
            INSERT INTO monitors
                (name, type, status, last_check, last_change,
                 first_check, total_checks, total_up, total_down,
                 uptime_pct, last_alert_sent, state_changed)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (name) DO UPDATE SET
                type = EXCLUDED.type,
                status = EXCLUDED.status,
                last_check = EXCLUDED.last_check,
                last_change = EXCLUDED.last_change,
                first_check = EXCLUDED.first_check,
                total_checks = EXCLUDED.total_checks,
                total_up = EXCLUDED.total_up,
                total_down = EXCLUDED.total_down,
                uptime_pct = EXCLUDED.uptime_pct,
                last_alert_sent = EXCLUDED.last_alert_sent,
                state_changed = EXCLUDED.state_changed
            """,
            state.name,
            state.type,
            state.status,
            _iso_to_dt(state.last_check),
            _iso_to_dt(state.last_change),
            _iso_to_dt(state.first_check),
            state.total_checks,
            state.total_up,
            state.total_down,
            state.uptime_pct,
            _iso_to_dt(state.last_alert_sent),
            state.state_changed,
        )


# --- Check operations -----------------------------------------------------------------

# --------------------------------------------------------------------------------------
async def insert_check(
    monitor_name: str,
    check_time: str,
    status: str,
    is_up: bool | None = None,
    response_ms: float | None = None,
    error: str | None = None,
    details: dict | None = None,
    conn=None,
) -> None:
    """Insert a single check record.

    is_up is the already-classified health of this check (from
    monitors.base.is_up(), which knows about the monitor's expected_status
    override if it has one) -- not re-derived from `status` here. Leave it
    None for maintenance/inactive log rows, which aren't a real check and
    have never counted as up or down.

    Pass `conn` from db.transaction() to make this atomic with the
    save_state() that accompanies it; omit it and this commits on its
    own.
    """
    if _pool is None and conn is None:
        raise RuntimeError("DB pool not initialised")
    dt = _iso_to_dt(check_time)
    details_json = json.dumps(details) if details else None
    async with _connection(conn) as c:
        # The monitor's previous check, for the state-change log (below).
        previous = await c.fetchrow(
            "SELECT is_up FROM checks WHERE monitor_name = $1 "
            "ORDER BY check_time DESC LIMIT 1",
            monitor_name,
        )
        check_id = await c.fetchval(
            """
            INSERT INTO checks
                (monitor_name, check_time, status, is_up, response_ms,
                 error, details)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id
            """,
            monitor_name, dt, status, is_up, response_ms, error, details_json,
        )
        if is_state_change(
            previous is not None,
            None if previous is None else previous["is_up"],
            is_up, details,
        ):
            await c.execute(
                "INSERT INTO check_changes (check_id, monitor_name, check_time) "
                "VALUES ($1, $2, $3)",
                check_id, monitor_name, dt,
            )
        if dt is not None:
            await _add_to_check_hourly(
                c, monitor_name, dt, status, is_up, response_ms, details
            )


# --------------------------------------------------------------------------------------
async def _add_to_check_hourly(
    c, monitor_name, dt, status, is_up, response_ms, details
) -> None:
    """Add one check to its hour in check_hourly (see the table's comment)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    up = is_up is not None and bool(is_up)
    down = is_up is not None and not is_up
    counted = counts_toward_stats(status, details)
    counted_response = counted and up and response_ms is not None
    await c.execute(
        """
        INSERT INTO check_hourly
            (monitor_name, hour_start, up_count, down_count, stat_total,
             stat_up, stat_down, resp_sum, resp_count)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (monitor_name, hour_start) DO UPDATE SET
            up_count = check_hourly.up_count + EXCLUDED.up_count,
            down_count = check_hourly.down_count + EXCLUDED.down_count,
            stat_total = check_hourly.stat_total + EXCLUDED.stat_total,
            stat_up = check_hourly.stat_up + EXCLUDED.stat_up,
            stat_down = check_hourly.stat_down + EXCLUDED.stat_down,
            resp_sum = check_hourly.resp_sum + EXCLUDED.resp_sum,
            resp_count = check_hourly.resp_count + EXCLUDED.resp_count
        """,
        monitor_name, hour_floor(dt.astimezone(timezone.utc)),
        int(up), int(down), int(counted), int(counted and up),
        int(counted and down),
        float(response_ms) if counted_response else 0.0,
        int(counted_response),
    )


# --------------------------------------------------------------------------------------
async def get_checks_paginated(
    monitor_name: str, page: int = 1, per_page: int = 25,
    state_changes_only: bool = False,
) -> dict:
    """Return a page of checks for a monitor, newest first.

    When state_changes_only=True, only returns checks where the
    monitor's health state (up/down) transitioned from the previous
    check, as recorded in check_changes when each check was inserted.
    Verification/retry checks are included.

    Returns:
        {"checks": [...], "total": int, "page": int,
         "per_page": int, "total_pages": int}
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    page = max(1, page)
    offset = (page - 1) * per_page
    async with _pool.acquire() as conn:
        if state_changes_only:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM check_changes WHERE monitor_name = $1",
                monitor_name,
            )

            rows = await conn.fetch(
                """
                SELECT c.check_time, c.status, c.is_up, c.response_ms,
                       c.error, c.details
                FROM check_changes s
                JOIN checks c ON c.id = s.check_id
                WHERE s.monitor_name = $1
                ORDER BY s.check_time DESC
                LIMIT $2 OFFSET $3
                """,
                monitor_name, per_page, offset,
            )
        else:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM checks WHERE monitor_name = $1",
                monitor_name,
            )
            rows = await conn.fetch(
                """
                SELECT check_time, status, is_up, response_ms, error, details
                FROM checks
                WHERE monitor_name = $1
                ORDER BY check_time DESC
                LIMIT $2 OFFSET $3
                """,
                monitor_name, per_page, offset,
            )

    checks = []
    for row in rows:
        entry = {
            "time": _dt_to_iso(row["check_time"]),
            "status": row["status"],
            # None for maintenance/inactive log rows (never up or down) -- the
            # frontend doesn't re-derive up/down from the raw status code (a
            # fixed "2xx = up" rule can't know about a monitor's expected_status
            # override), it uses this stored classification directly.
            # asyncpg maps BOOLEAN natively to Python bool already, but
            # coerce explicitly anyway -- keeps both backends' JSON output
            # identical and doesn't depend on that driver behavior holding.
            "is_up": None if row["is_up"] is None else bool(row["is_up"]),
            # `is not None`, not a truthiness test -- see db_mariadb.py's
            # twin for why: a NULL response_ms means the check never
            # completed, and 0.0 would report that as a real measurement.
            "response_ms": (
                float(row["response_ms"])
                if row["response_ms"] is not None else None
            ),
        }
        if row["error"]:
            entry["error"] = row["error"]
        if row["details"]:
            entry["details"] = json.loads(row["details"])
        checks.append(entry)

    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "checks": checks,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


# --------------------------------------------------------------------------------------
async def get_all_checks_paginated(
    page: int = 1, per_page: int = 25,
    state_changes_only: bool = False,
) -> dict:
    """Return a page of checks across ALL monitors, newest first.

    When state_changes_only=True, only returns checks where the
    monitor's health state (up/down) transitioned from its previous
    check for that same monitor, as recorded in check_changes when each
    check was inserted. Verification/retry checks are included.

    Returns:
        {"checks": [...], "total": int, "page": int,
         "per_page": int, "total_pages": int}
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    page = max(1, page)  # see get_checks_paginated for why
    offset = (page - 1) * per_page
    async with _pool.acquire() as conn:
        if state_changes_only:
            total = await conn.fetchval("SELECT COUNT(*) FROM check_changes")

            rows = await conn.fetch(
                """
                SELECT c.monitor_name, c.check_time, c.status, c.is_up,
                       c.response_ms, c.error, c.details
                FROM check_changes s
                JOIN checks c ON c.id = s.check_id
                ORDER BY s.check_time DESC
                LIMIT $1 OFFSET $2
                """,
                per_page, offset,
            )
        else:
            total = await conn.fetchval("SELECT COUNT(*) FROM checks")
            rows = await conn.fetch(
                """
                SELECT monitor_name, check_time, status, is_up,
                       response_ms, error, details
                FROM checks
                ORDER BY check_time DESC
                LIMIT $1 OFFSET $2
                """,
                per_page, offset,
            )

    checks = []
    for row in rows:
        entry = {
            "monitor": row["monitor_name"],
            "time": _dt_to_iso(row["check_time"]),
            "status": row["status"],
            # asyncpg maps BOOLEAN natively to Python bool already, but
            # coerce explicitly anyway -- keeps both backends' JSON output
            # identical and doesn't depend on that driver behavior holding.
            "is_up": None if row["is_up"] is None else bool(row["is_up"]),
            # `is not None`, not a truthiness test -- see db_mariadb.py's
            # twin for why: a NULL response_ms means the check never
            # completed, and 0.0 would report that as a real measurement.
            "response_ms": (
                float(row["response_ms"])
                if row["response_ms"] is not None else None
            ),
        }
        if row["error"]:
            entry["error"] = row["error"]
        if row["details"]:
            entry["details"] = json.loads(row["details"])
        checks.append(entry)

    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "checks": checks,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


# --------------------------------------------------------------------------------------
async def get_recent_checks(
    monitor_name: str, hours: int = 168
) -> list[dict]:
    """Return all checks within the last N hours for a monitor."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT check_time, status, response_ms, error, details
            FROM checks
            WHERE monitor_name = $1 AND check_time >= $2
            ORDER BY check_time ASC
            """,
            monitor_name, cutoff,
        )

    checks = []
    for row in rows:
        entry = {
            "time": _dt_to_iso(row["check_time"]),
            "status": row["status"],
            # `is not None`, not a truthiness test -- see db_mariadb.py's
            # twin for why: a NULL response_ms means the check never
            # completed, and 0.0 would report that as a real measurement.
            "response_ms": (
                float(row["response_ms"])
                if row["response_ms"] is not None else None
            ),
        }
        if row["error"]:
            entry["error"] = row["error"]
        if row["details"]:
            entry["details"] = json.loads(row["details"])
        checks.append(entry)
    return checks


# --------------------------------------------------------------------------------------
async def get_last_response_ms_many(
    monitor_names: list[str],
) -> dict[str, float | None]:
    """The response time of each monitor's most recent check, in one query."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.name,
                   (SELECT c.response_ms FROM checks c
                    WHERE c.monitor_name = m.name
                    ORDER BY c.check_time DESC LIMIT 1) AS response_ms
            FROM monitors m
            """
        )
    by_name = {row["name"]: row["response_ms"] for row in rows}
    return {
        name: float(by_name[name]) if by_name.get(name) is not None else None
        for name in monitor_names
    }


# --------------------------------------------------------------------------------------
def _uptime_pct(total, up_count, decimals: int) -> float:
    total = int(total or 0)
    return round(int(up_count or 0) / total * 100, decimals) if total > 0 else 0.0


# --------------------------------------------------------------------------------------
async def get_uptime_pct_many(
    monitor_names: list[str], days: int, decimals: int = 2
) -> dict[str, float]:
    """Uptime % over the last N days for many monitors, in one query.

    Counted from the hourly summary, in whole hours: a window of N days
    starts at the top of the current hour, N days back, and runs to now (see
    state.period_window_start).
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    start = period_window_start(datetime.now(timezone.utc), days)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT monitor_name, SUM(stat_total) AS total, SUM(stat_up) AS up_count
            FROM check_hourly
            WHERE hour_start >= $1
            GROUP BY monitor_name
            """,
            start,
        )
    by_name = {row["monitor_name"]: (row["total"], row["up_count"]) for row in rows}
    return {
        name: _uptime_pct(*by_name.get(name, (0, 0)), decimals)
        for name in monitor_names
    }


# --------------------------------------------------------------------------------------
async def prune_old_checks(monitor_name: str, retention_days: int) -> None:
    """Delete checks older than retention_days for a monitor."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    async with _pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM checks WHERE monitor_name = $1 AND check_time < $2",
            monitor_name, cutoff,
        )
        # The hour the cutoff falls in stays: part of it is still in
        # `checks`, and the summary should keep the whole hour's counts.
        await conn.execute(
            "DELETE FROM check_hourly "
            "WHERE monitor_name = $1 AND hour_start < $2",
            monitor_name, hour_floor(cutoff),
        )
    logger.debug(
        "Pruned checks older than %s for '%s'", cutoff, monitor_name
    )




# --------------------------------------------------------------------------------------
async def get_all_monitor_names() -> list[str]:
    """Return all monitor names that exist in the monitors table."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT name FROM monitors")
    return [row["name"] for row in rows]


# --------------------------------------------------------------------------------------
async def delete_monitor(name: str) -> None:
    """Delete a monitor and all its checks (CASCADE handles check rows).

    The checks table has ON DELETE CASCADE on the foreign key to monitors,
    so deleting the monitor row automatically removes all its check history.
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM monitors WHERE name = $1", name)
    logger.info(
        "Deleted monitor '%s' and associated checks (config removal cleanup)",
        name,
    )


# --------------------------------------------------------------------------------------
async def get_monitor_stats(
    monitor_name: str,
    days: int = 30,
    uptime_decimals: int = 2,
    latency_decimals: int = 1,
) -> dict:
    """Return windowed stats for a monitor over the last N days.

    Returns:
        {"uptime_pct": float, "total_checks": int, "total_up": int,
         "total_down": int, "total_timeout": int, "outages": int}
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    # Whole hours, like the other statistics (see get_uptime_pct_many).
    cutoff = period_window_start(datetime.now(timezone.utc), days)
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                SUM(stat_total) AS total,
                SUM(stat_up) AS up_count,
                SUM(stat_down) AS down_count,
                SUM(resp_sum) / NULLIF(SUM(resp_count), 0) AS avg_response
            FROM check_hourly
            WHERE monitor_name = $1 AND hour_start >= $2
            """,
            monitor_name, cutoff,
        )

        # Deliberately its own query, not folded into the one above: a
        # timeout is just a raw event, not a claim about being down, so
        # unlike total/up_count/down_count it does NOT go through
        # _EXCLUDE_VERIFYING_SQL -- DOWN requires the retry mechanism to
        # confirm it first (avoiding false-positive fatigue), a timeout
        # doesn't need that same confirmation to be worth counting.
        # status='timeout' can never also be 'maintenance'/'inactive',
        # so those exclusions don't apply here either.
        timeout_row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS timeout_count
            FROM checks
            WHERE monitor_name = $1 AND check_time >= $2
              AND status = 'timeout'
            """,
            monitor_name, cutoff,
        )

        outage_row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS outages
            FROM (
                SELECT is_up,
                       LAG(is_up) OVER (ORDER BY check_time) AS prev_is_up
                FROM checks
                WHERE monitor_name = $1 AND check_time >= $2
                  AND status != 'maintenance' AND status != 'inactive'
                  AND {_EXCLUDE_VERIFYING_SQL}
                ORDER BY check_time
            ) AS t
            WHERE {_IS_DOWN_SQL} AND {_PREV_IS_UP_SQL}
            """,
            monitor_name, cutoff,
        )

    total = row["total"] or 0
    up_count = row["up_count"] or 0
    down_count = row["down_count"] or 0
    timeout_count = timeout_row["timeout_count"] or 0
    outages = outage_row["outages"] or 0
    avg_response = (
        round(float(row["avg_response"]), latency_decimals)
        if row["avg_response"] is not None else None
    )
    uptime_pct = round(up_count / total * 100, uptime_decimals) if total > 0 else 0.0

    return {
        "uptime_pct": uptime_pct,
        "total_checks": total,
        "total_up": up_count,
        "total_down": down_count,
        "total_timeout": timeout_count,
        "outages": outages,
        "avg_response": avg_response,
    }


# --------------------------------------------------------------------------------------
async def get_aggregate_stats(
    monitor_names: list[str],
    periods: list[tuple[str, int]],
    uptime_decimals: int = 2,
    latency_decimals: int = 1,
) -> dict:
    """Return aggregate uptime % and avg latency across all given monitors
    for multiple time periods.

    Only includes monitors in the monitor_names list (active monitors).
    Inactive monitors should be excluded by the caller.

    Counted from the hourly summary in one pass over the longest period,
    in whole hours: a period of N days starts at the top of the current hour,
    N days back (see get_uptime_pct_many).

    Args:
        monitor_names: list of active monitor names
        periods: list of (key, days) tuples, e.g. [("24h", 1), ("7d", 7)]

    Returns:
        {
            "uptime_pct": {key: float, ...},
            "avg_latency": {key: float, ...},
        }
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")

    uptime_pct = {key: 0.0 for key, _ in periods}
    avg_latency = {key: 0.0 for key, _ in periods}

    if not monitor_names:
        return {"uptime_pct": uptime_pct, "avg_latency": avg_latency}

    now = datetime.now(timezone.utc)
    starts = [period_window_start(now, days) for _, days in periods]
    # One column group per period; each period's start is its own numbered
    # parameter ($1, $2, ...), used four times. The `hour_start >= $n`
    # conditions pick the hours that belong to each period out of the single
    # scan. = ANY($n) takes the plain Python list of names directly -- the
    # Postgres-idiomatic way to handle a variable-length IN (...) clause.
    columns = ", ".join(
        f"COALESCE(SUM(stat_total) FILTER (WHERE hour_start >= ${n}), 0), "
        f"COALESCE(SUM(stat_up) FILTER (WHERE hour_start >= ${n}), 0), "
        f"COALESCE(SUM(resp_sum) FILTER (WHERE hour_start >= ${n}), 0), "
        f"COALESCE(SUM(resp_count) FILTER (WHERE hour_start >= ${n}), 0)"
        for n in range(1, len(periods) + 1)
    )
    earliest = len(periods) + 1
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {columns}
            FROM check_hourly
            WHERE hour_start >= ${earliest} AND monitor_name = ANY(${earliest + 1})
            """,
            *starts, min(starts), monitor_names,
        )

    values = tuple(row)
    for index, (key, _) in enumerate(periods):
        total, up_count, resp_sum, resp_count = values[index * 4:index * 4 + 4]
        uptime_pct[key] = _uptime_pct(total, up_count, uptime_decimals)
        avg_latency[key] = (
            round(float(resp_sum) / int(resp_count), latency_decimals)
            if int(resp_count) > 0 else 0.0
        )

    return {"uptime_pct": uptime_pct, "avg_latency": avg_latency}


# --------------------------------------------------------------------------------------
async def get_response_time_series(
    monitor_name: str, hours: int = 168
) -> list[dict]:
    """Return response time data points for the line graph.

    Returns:
        [{"time": iso_string, "response_ms": float}, ...]
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT check_time, response_ms
            FROM checks
            WHERE monitor_name = $1 AND check_time >= $2
              AND {_IS_UP_SQL}
              AND response_ms IS NOT NULL
            ORDER BY check_time ASC
            """,
            monitor_name, cutoff,
        )
    return [
        {
            "time": _dt_to_iso(row["check_time"]),
            "response_ms": float(row["response_ms"]),
        }
        for row in rows
    ]


# --- Hourly bar aggregation (SQL-side) ------------------------------------------------

# --------------------------------------------------------------------------------------
async def get_hourly_bars(
    monitor_name: str,
    hours: int = 360,
    display_tz=None,
    now: datetime | None = None,
) -> list[dict]:
    """Bucket checks into hourly bars for the last N hours.

    Reads the hourly summary. Returns list of dicts, newest hour first:
        {"hour": "2026-06-18T14:00", "status": "up"|"down"|"mixed"|"nodata",
         "hour_display": ..., "hour_display_end": ...}

    Bucketing is always done in UTC. If display_tz is provided, hour
    labels are converted to that timezone. `now` lets a caller that builds
    bars for many monitors in one request use a single clock reading, so
    every monitor's bars line up with the same hour labels even when the
    request straddles the top of an hour.

    Unlike the stats functions (get_monitor_stats/get_aggregate_stats/
    get_uptime_pct_many), verifying checks are deliberately counted here
    -- see db_mariadb.py's matching comment for why.
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")

    now = now or datetime.now(timezone.utc)
    slot_zero = hour_floor(now)
    cutoff = hour_window_start(now, hours)

    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT hour_start, up_count, down_count FROM check_hourly
            WHERE monitor_name = $1 AND hour_start >= $2
            """,
            monitor_name, cutoff,
        )
    # hour_start is TIMESTAMPTZ, so convert to UTC before building the key
    # (the scaffold's bucket keys are UTC).
    db_buckets = {
        row["hour_start"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:00"): (
            row["up_count"], row["down_count"],
        )
        for row in rows
    }

    # Build full bucket list (including nodata hours). Labels come from
    # the shared, cached scaffold in state.py -- see db_mariadb.py's twin
    # and hourly_bar_scaffold()'s own comment for why.
    result = []
    for slot in hourly_bar_scaffold(slot_zero, hours, display_tz):
        up_count, down_count = db_buckets.get(slot["bucket_key"], (0, 0))
        result.append({
            "hour": slot["hour"],
            "hour_display": slot["hour_display"],
            "hour_display_end": slot["hour_display_end"],
            "status": bar_status(up_count, down_count),
        })

    return result


# --------------------------------------------------------------------------------------
def _unpack_bar_statuses(packed: str | None, hours: int, width: int) -> list[str]:
    """Turn get_hourly_bar_statuses()'s packed string into hours bar
    statuses, newest first (hours with no entry are "nodata")."""
    statuses = ["nodata"] * hours
    text = packed or ""
    step = width + 2
    for start in range(0, len(text), step):
        chunk = text[start:start + step]
        statuses[int(chunk[:width])] = bar_status(int(chunk[-2]), int(chunk[-1]))
    return statuses


# --------------------------------------------------------------------------------------
async def get_hourly_bar_statuses(
    monitor_names: list[str],
    hours: int,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """The bar statuses of many monitors at once, newest hour first.

    Same statuses as get_hourly_bars(), for the monitor lists: no labels
    (they are the same for every monitor, see state.hourly_bar_scaffold) and
    one query for all the monitors instead of one each.
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")

    now = now or datetime.now(timezone.utc)
    slot_zero = hour_floor(now)
    # The database packs each monitor's hours into one string -- see
    # db_mariadb.get_hourly_bar_statuses for why: per hour, its age in hours
    # (0 = the current hour) zero-padded to a fixed width, then a 1/0 flag
    # for "has up checks" and one for "has down checks".
    width = len(str(hours))
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT monitor_name,
                   string_agg(
                       lpad(((EXTRACT(EPOCH FROM ($1::timestamptz - hour_start))
                              / 3600)::int)::text, {width}, '0')
                       || (up_count > 0)::int::text
                       || (down_count > 0)::int::text,
                       ''
                   ) AS packed
            FROM check_hourly
            WHERE hour_start >= $2 AND hour_start <= $1::timestamptz
            GROUP BY monitor_name
            """,
            slot_zero, hour_window_start(now, hours),
        )
    packed = {row["monitor_name"]: row["packed"] for row in rows}

    return {
        name: _unpack_bar_statuses(packed.get(name), hours, width)
        for name in monitor_names
    }


# --------------------------------------------------------------------------------------
async def get_server_version() -> str:
    """Return the database server's version as PostgreSQL's numeric
    server_version_num (e.g. "180006" for 18.6)."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        value = await conn.fetchval("SHOW server_version_num")
    return str(value)


# --------------------------------------------------------------------------------------
async def has_system_notice(key: str) -> bool:
    """Whether the system notice with this key has already been sent."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM system_notices WHERE notice_key = $1", key
        )
    return row is not None


# --------------------------------------------------------------------------------------
async def record_system_notice(key: str) -> None:
    """Remember that the system notice with this key has been sent."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO system_notices (notice_key, sent_at) "
            "VALUES ($1, now()) ON CONFLICT DO NOTHING",
            key,
        )


# --- Helpers --------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
def _dt_to_iso(dt) -> str | None:
    """Convert a datetime object to an ISO string, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# --------------------------------------------------------------------------------------
def _iso_to_dt(iso: str | None) -> datetime | None:
    """Convert an ISO string to a datetime object, or None."""
    if iso is None:
        return None
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
