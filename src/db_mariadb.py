# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
db_mariadb.py - The MariaDB database backend.

One of two backend modules with an identical function surface --
db_postgres.py is the other. src/db.py picks between them at import
time based on DB_BACKEND and re-exports whichever one is selected, so
every other module in the app just does `from src import db` without
caring which database is actually in use. See src/db.py's docstring for
the dispatch mechanism.

Connection is configured via environment variables so the app works
with either a bundled compose container or an external database server.

Environment variables:
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

import aiomysql

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

# The single source of truth for "is this check healthy" at the SQL level.
# Reads the `checks.is_up` column directly rather than re-deriving health from
# the raw status code. Classification happens once, in Python, at insert time
# (src/main.py, using monitors/base.py's is_up()), where the monitor's
# per-monitor `expected_status` override is available. A fixed regex against
# the stored status code can't be made monitor-aware without either a live
# join back to config or duplicating is_up()'s logic in SQL per monitor.
# Storing the classified result avoids both, and makes a check's
# classification a frozen historical fact that doesn't change if the monitor's
# expected_status is edited later.
#
# is_up is NULL (not TRUE/FALSE) for maintenance/inactive log rows: they
# aren't a real check and are never counted as up or down. `is_up = TRUE` and
# `is_up = FALSE` exclude NULL via SQL's three-valued logic, so every query
# below gets that exclusion without its own status filter.
#
# SECURITY: every query below that f-string-interpolates one of these
# constants is safe *only* because they are, and stay, fixed literals with no
# path back to caller-supplied input (see ruff.toml's per-file-ignore for the
# S608 reasoning). If one of these names is ever built from a function
# argument, config value, or anything else outside this module, every one of
# those interpolation sites becomes a live SQL injection vector at once, and
# the S608 suppression in ruff.toml will hide it from the linter. Don't
# parameterize these.
_IS_UP_SQL = "is_up = TRUE"
_PREV_IS_UP_SQL = "prev_is_up = TRUE"
_IS_DOWN_SQL = "is_up = FALSE"

# Excludes in-flight retry-verification check rows (details.verifying=true)
# from stats -- EXCEPT the one that confirms the new state (details.passed=true,
# set only on the final retry of a sequence that didn't abort; see
# _build_verification_check_details in main.py). A plain "verifying" row is a
# real check but not yet a confirmed state change, and if the retry sequence
# turns out to be a false alarm it should never have counted toward downtime.
# The confirming row is the actual transition point and counts toward the
# state it verified, like an ordinary check.
#
# JSON_UNQUOTE(JSON_EXTRACT(...)), not JSON_VALUE(...): MariaDB's JSON_VALUE()
# renders a JSON boolean as '1'/'0', so a clause spelled with it could never
# match a `verifying: false` row. That would change the filter from "exclude
# in-flight retries" to "exclude anything mentioning verifying at all",
# dropping those rows from every stats query on MariaDB while Postgres'
# `details->>'verifying' = 'false'` twin keeps them.
_EXCLUDE_VERIFYING_SQL = (
    "(details IS NULL "
    "OR JSON_UNQUOTE(JSON_EXTRACT(details, '$.verifying')) IS NULL "
    "OR JSON_UNQUOTE(JSON_EXTRACT(details, '$.verifying')) = 'false' "
    "OR JSON_UNQUOTE(JSON_EXTRACT(details, '$.passed')) = 'true')"
)

# --- Config ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS monitors (
    name             VARCHAR(255) NOT NULL PRIMARY KEY,
    type             VARCHAR(50)  NOT NULL,
    status           VARCHAR(20)  NOT NULL DEFAULT 'unknown',
    last_check       DATETIME(3)  NULL,
    last_change      DATETIME(3)  NULL,
    first_check      DATETIME(3)  NULL,
    total_checks     INT          NOT NULL DEFAULT 0,
    total_up         INT          NOT NULL DEFAULT 0,
    total_down       INT          NOT NULL DEFAULT 0,
    uptime_pct       DECIMAL(5,2) NOT NULL DEFAULT 0.00,
    last_alert_sent  DATETIME(3)  NULL,
    state_changed    BOOLEAN      NOT NULL DEFAULT FALSE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS checks (
    id            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    monitor_name  VARCHAR(255) NOT NULL,
    check_time    DATETIME(3)  NOT NULL,
    -- VARCHAR(20), not (10): the longest real value is the
    -- 'maintenance' log-row marker at 11 characters. At (10) every
    -- maintenance insert failed outright under strict mode (and
    -- truncated to 'maintenanc' without it, which then slipped past
    -- the `status != 'maintenance'` filters the stats queries rely on).
    status        VARCHAR(20)  NOT NULL,
    is_up         BOOLEAN      NULL,
    response_ms   FLOAT        NULL,
    error         TEXT         NULL,
    details       JSON         NULL,
    INDEX idx_monitor_time (monitor_name, check_time),
    CONSTRAINT fk_monitor
        FOREIGN KEY (monitor_name) REFERENCES monitors(name)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- For "all checks, newest first" (the check log when view_all_checks is on).
-- Without it that is a sort of the whole table on every request.
CREATE INDEX IF NOT EXISTS idx_check_time ON checks (check_time);

-- The checks that belong in the "state changes" check log (the default view),
-- recorded by insert_check() as each check arrives: the first check of a
-- monitor, one whose health differs from the previous check's, or one made
-- while a state change is being verified (state.is_state_change). Finding
-- them with a window function over every check on every request would not
-- be workable with a long history. Rows go when
-- their check is pruned or its monitor deleted (ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS check_changes (
    check_id      BIGINT       NOT NULL PRIMARY KEY,
    monitor_name  VARCHAR(255) NOT NULL,
    check_time    DATETIME(3)  NOT NULL,
    INDEX idx_changes_time (check_time),
    INDEX idx_changes_monitor_time (monitor_name, check_time),
    CONSTRAINT fk_changes_check
        FOREIGN KEY (check_id) REFERENCES checks(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Which one-time system notices (e.g. database end-of-support reminders)
-- have already been sent, so a restart doesn't send them again.
CREATE TABLE IF NOT EXISTS system_notices (
    notice_key  VARCHAR(255) NOT NULL PRIMARY KEY,
    sent_at     DATETIME(3)  NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One row per monitor per hour (UTC), kept up to date by insert_check().
-- The dashboard's uptime percentages, latency averages and hourly bars used
-- to be counted from the raw `checks` rows on every page load and every
-- auto-refresh, which stops being workable with a long history (millions of
-- rows per request). They read these pre-added-up rows instead.
--   up_count / down_count   every check with is_up TRUE / FALSE (the bars)
--   stat_total / stat_up /  the checks the statistics count: not maintenance
--   stat_down               or inactive log rows, and not unconfirmed
--                           retry-verification rows (state.counts_toward_stats)
--   resp_sum / resp_count   response time of the counted, up checks that have one
CREATE TABLE IF NOT EXISTS check_hourly (
    monitor_name  VARCHAR(255) NOT NULL,
    hour_start    DATETIME     NOT NULL,
    up_count      INT          NOT NULL DEFAULT 0,
    down_count    INT          NOT NULL DEFAULT 0,
    stat_total    INT          NOT NULL DEFAULT 0,
    stat_up       INT          NOT NULL DEFAULT 0,
    stat_down     INT          NOT NULL DEFAULT 0,
    resp_sum      DOUBLE       NOT NULL DEFAULT 0,
    resp_count    INT          NOT NULL DEFAULT 0,
    PRIMARY KEY (monitor_name, hour_start),
    INDEX idx_hourly_time (hour_start),
    CONSTRAINT fk_hourly_monitor
        FOREIGN KEY (monitor_name) REFERENCES monitors(name)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


# --- Pool management ------------------------------------------------------------------

_pool: aiomysql.pool.Pool | None = None


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
    _pool = await aiomysql.create_pool(
        host=db_host,
        port=db_port,
        db=db_name,
        user=db_user,
        password=db_password,
        autocommit=True,
        minsize=min_size,
        maxsize=max_size,
        charset="utf8mb4",
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
        _pool.close()
        await _pool.wait_closed()
        _pool = None
        logger.info("DB pool closed")


# --------------------------------------------------------------------------------------
async def _migrate_status_column(cur) -> None:
    """Widen checks.status to VARCHAR(20), then repair truncated rows.

    'maintenance' is 11 characters, so a database created with VARCHAR(10)
    either fails to insert every maintenance log row (error 1406 under strict
    mode) or silently truncates it to 'maintenanc'.

    Guarded on the current length rather than run unconditionally, so a
    large checks table isn't handed an ALTER on every single startup.

    The truncation repair has to happen BEFORE init_schema()'s is_up
    backfill, not after: 'maintenanc' matches neither status name the
    backfill excludes, so the backfill would evaluate the up/2xx test on
    it, write is_up = FALSE, and count every past maintenance window as a
    confirmed outage. Since the backfill only touches `is_up IS NULL`,
    that is not repairable by re-running it afterwards.
    """
    await cur.execute(
        "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'checks' "
        "AND COLUMN_NAME = 'status'"
    )
    status_col = await cur.fetchone()
    if (
        status_col is not None
        and status_col[0] is not None
        and int(status_col[0]) < 20
    ):
        await cur.execute(
            "ALTER TABLE checks MODIFY COLUMN status VARCHAR(20) NOT NULL"
        )
        logger.info(
            "Widened checks.status to VARCHAR(20) so 'maintenance' "
            "log rows fit"
        )

    await cur.execute(
        "UPDATE checks SET status = 'maintenance' "
        "WHERE status = 'maintenanc' AND is_up IS NULL"
    )
    if cur.rowcount:
        logger.info(
            "Repaired %d maintenance log row(s) truncated by the old "
            "VARCHAR(10) status column",
            cur.rowcount,
        )


# --------------------------------------------------------------------------------------
async def init_schema() -> None:
    """Create tables if they don't exist."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised -- call init_pool() first")
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            for statement in SCHEMA_SQL.strip().split(";"):
                stmt = statement.strip()
                if stmt:
                    await cur.execute(stmt)
            # Add state_changed column if missing (migration for existing DBs)
            try:
                await cur.execute(
                    "ALTER TABLE monitors ADD COLUMN state_changed "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                )
                logger.info("Added state_changed column to monitors table")
            except aiomysql.Error as e:
                # MariaDB error 1060 = "Duplicate column name" — the
                # expected case on every startup after the first, since this
                # migration always runs. Anything else (permissions, a
                # dropped connection, version incompatibility) should fail
                # loudly at startup rather than leave the app silently
                # missing a column that save_state()/load_state() assume
                # exists on every read and write.
                if e.args and e.args[0] == 1060:
                    pass
                else:
                    logger.critical(
                        "state_changed column migration failed with an "
                        "unexpected error — refusing to start with a "
                        "possibly-inconsistent schema: %s", e,
                    )
                    raise
            # Add is_up column if missing (migration for existing DBs) --
            # same duplicate-column-is-expected pattern as state_changed
            # above. New rows always get an explicit is_up from
            # insert_check() going forward; the backfill below only ever
            # has real work to do once, for check rows that predate this
            # column, and is safe to run on every startup after that since
            # it will simply match zero rows.
            try:
                await cur.execute(
                    "ALTER TABLE checks ADD COLUMN is_up BOOLEAN NULL"
                )
                logger.info("Added is_up column to checks table")
            except aiomysql.Error as e:
                if e.args and e.args[0] == 1060:
                    pass
                else:
                    logger.critical(
                        "is_up column migration failed with an unexpected "
                        "error — refusing to start with a possibly-"
                        "inconsistent schema: %s", e,
                    )
                    raise
            await _migrate_status_column(cur)
            await cur.execute(
                "UPDATE checks SET is_up = "
                "(status = 'up' OR status REGEXP '^2[0-9]{2}$') "
                "WHERE is_up IS NULL "
                "AND status != 'maintenance' AND status != 'inactive'"
            )
            if cur.rowcount:
                logger.info(
                    "Backfilled is_up for %d pre-existing check row(s)",
                    cur.rowcount,
                )
            await _backfill_check_hourly(cur)
            await _backfill_check_changes(cur)
    logger.info("DB schema verified")


# --------------------------------------------------------------------------------------
async def _backfill_check_changes(cur) -> None:
    """Fill check_changes from the existing checks, once.

    Only runs when the table is empty and checks exist, i.e. on a database
    that predates it. Applies the window-function rule over the whole history
    in one pass; one statement per monitor would be about six times slower on
    a large history, because each monitor's rows are read through the
    monitor_name index one row at a time.
    """
    await cur.execute("SELECT EXISTS (SELECT 1 FROM check_changes)")
    if (await cur.fetchone())[0]:
        return
    await cur.execute("SELECT EXISTS (SELECT 1 FROM checks)")
    if not (await cur.fetchone())[0]:
        return
    logger.info(
        "Building the list of state-change checks (one time; this can take "
        "a while on a large history)"
    )
    await cur.execute(
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
            OR NOT (is_up <=> prev_is_up)
            OR JSON_EXTRACT(details, '$.verifying') = true
        """
    )
    logger.info("State-change list built: %d row(s)", cur.rowcount)


# --------------------------------------------------------------------------------------
async def _backfill_check_hourly(cur) -> None:
    """Fill check_hourly from the existing checks, once.

    Only runs when the summary table is empty and checks exist, i.e. on a
    database that predates it. The counts match the raw check rows.
    """
    await cur.execute("SELECT EXISTS (SELECT 1 FROM check_hourly)")
    if (await cur.fetchone())[0]:
        return
    await cur.execute("SELECT EXISTS (SELECT 1 FROM checks)")
    if not (await cur.fetchone())[0]:
        return
    logger.info(
        "Building the hourly summary of existing checks (one time; this can "
        "take a while on a large history)"
    )
    counted = (
        "status != 'maintenance' AND status != 'inactive' "
        f"AND {_EXCLUDE_VERIFYING_SQL}"
    )
    await cur.execute(
        f"""
        INSERT INTO check_hourly
            (monitor_name, hour_start, up_count, down_count, stat_total,
             stat_up, stat_down, resp_sum, resp_count)
        SELECT monitor_name,
               DATE_FORMAT(check_time, '%Y-%m-%d %H:00:00'),
               SUM(CASE WHEN {_IS_UP_SQL} THEN 1 ELSE 0 END),
               SUM(CASE WHEN {_IS_DOWN_SQL} THEN 1 ELSE 0 END),
               SUM(CASE WHEN {counted} THEN 1 ELSE 0 END),
               SUM(CASE WHEN {counted} AND {_IS_UP_SQL} THEN 1 ELSE 0 END),
               SUM(CASE WHEN {counted} AND {_IS_DOWN_SQL} THEN 1 ELSE 0 END),
               COALESCE(SUM(CASE WHEN {counted} AND {_IS_UP_SQL}
                                  AND response_ms IS NOT NULL
                            THEN response_ms END), 0),
               SUM(CASE WHEN {counted} AND {_IS_UP_SQL}
                         AND response_ms IS NOT NULL THEN 1 ELSE 0 END)
        FROM checks
        GROUP BY monitor_name, DATE_FORMAT(check_time, '%Y-%m-%d %H:00:00')
        """
    )
    logger.info("Hourly summary built: %d row(s)", cur.rowcount)


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
        state_changed=bool(row.get("state_changed", False)),
    )


# --------------------------------------------------------------------------------------
async def load_state(name: str, monitor_type: str) -> MonitorState:
    """Load a monitor's aggregate state, or return a fresh state if not found."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM monitors WHERE name = %s", (name,)
            )
            row = await cur.fetchone()
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
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM monitors")
            rows = {row["name"]: row for row in await cur.fetchall()}
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

    Yields a backend connection to pass as the `conn=` argument of the write
    functions below, so they all run on the same connection inside one
    transaction:

        async with db.transaction() as conn:
            await db.save_state(state, conn=conn)
            await db.insert_check(..., conn=conn)

    Commits when the block exits normally; rolls back and re-raises if
    anything inside it raises. Without this, save_state() and insert_check()
    would each acquire their own pooled connection and commit independently,
    so a failure on the second write would leave the first applied --
    permanently desynchronising monitors' lifetime counters from the checks
    table, with nothing in the app to reconcile them.

    The pool is created with autocommit=True, but an explicit begin() issues a
    real BEGIN that overrides it for the duration, and the connection is
    always committed or rolled back before it returns to the pool.

    `conn` is an opaque handle: this backend's own aiomysql connection
    (db_postgres.transaction() yields an asyncpg one). Callers only pass it
    straight back into these functions, so nothing outside the backend needs
    to know which it is.
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        await conn.begin()
        try:
            yield conn
        except BaseException:
            await conn.rollback()
            raise
        else:
            await conn.commit()


# --------------------------------------------------------------------------------------
@asynccontextmanager
async def _cursor(conn, cursor_class=None):
    """Yield a cursor on `conn` when given, else on a pooled connection.

    Lets every write function below take an optional `conn=` and join a
    caller's transaction without duplicating its SQL for the two cases.
    """
    args = (cursor_class,) if cursor_class is not None else ()
    if conn is not None:
        async with conn.cursor(*args) as cur:
            yield cur
        return
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as own_conn:
        async with own_conn.cursor(*args) as cur:
            yield cur


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
    async with _cursor(conn) as cur:
        await cur.execute(
            """
            INSERT INTO monitors
                (name, type, status, last_check, last_change,
                 first_check, total_checks, total_up, total_down,
                 uptime_pct, last_alert_sent, state_changed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                type = VALUES(type),
                status = VALUES(status),
                last_check = VALUES(last_check),
                last_change = VALUES(last_change),
                first_check = VALUES(first_check),
                total_checks = VALUES(total_checks),
                total_up = VALUES(total_up),
                total_down = VALUES(total_down),
                uptime_pct = VALUES(uptime_pct),
                last_alert_sent = VALUES(last_alert_sent),
                state_changed = VALUES(state_changed)
            """,
            (
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
            ),
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
    async with _cursor(conn) as cur:
        # The monitor's previous check, for the state-change log (below).
        await cur.execute(
            "SELECT is_up FROM checks WHERE monitor_name = %s "
            "ORDER BY check_time DESC LIMIT 1",
            (monitor_name,),
        )
        previous = await cur.fetchone()
        await cur.execute(
            """
            INSERT INTO checks
                (monitor_name, check_time, status, is_up, response_ms,
                 error, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (monitor_name, dt, status, is_up, response_ms, error, details_json),
        )
        check_id = cur.lastrowid
        if is_state_change(
            previous is not None,
            None if previous is None or previous[0] is None else bool(previous[0]),
            is_up, details,
        ):
            await cur.execute(
                "INSERT INTO check_changes (check_id, monitor_name, check_time) "
                "VALUES (%s, %s, %s)",
                (check_id, monitor_name, dt),
            )
        if dt is not None:
            await _add_to_check_hourly(
                cur, monitor_name, dt, status, is_up, response_ms, details
            )


# --------------------------------------------------------------------------------------
async def _add_to_check_hourly(
    cur, monitor_name, dt, status, is_up, response_ms, details
) -> None:
    """Add one check to its hour in check_hourly (see the table's comment)."""
    up = is_up is not None and bool(is_up)
    down = is_up is not None and not is_up
    counted = counts_toward_stats(status, details)
    counted_response = counted and up and response_ms is not None
    await cur.execute(
        """
        INSERT INTO check_hourly
            (monitor_name, hour_start, up_count, down_count, stat_total,
             stat_up, stat_down, resp_sum, resp_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            up_count = up_count + VALUES(up_count),
            down_count = down_count + VALUES(down_count),
            stat_total = stat_total + VALUES(stat_total),
            stat_up = stat_up + VALUES(stat_up),
            stat_down = stat_down + VALUES(stat_down),
            resp_sum = resp_sum + VALUES(resp_sum),
            resp_count = resp_count + VALUES(resp_count)
        """,
        (
            monitor_name, hour_floor(dt), int(up), int(down), int(counted),
            int(counted and up), int(counted and down),
            float(response_ms) if counted_response else 0.0,
            int(counted_response),
        ),
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
    # Defense in depth against a negative OFFSET (also validated at the
    # FastAPI route level with Query(ge=1)) — a page <= 0 here would
    # otherwise produce a negative OFFSET, which the database rejects with a
    # syntax error instead of a clean empty result.
    page = max(1, page)
    offset = (page - 1) * per_page
    async with _pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if state_changes_only:
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM check_changes "
                    "WHERE monitor_name = %s",
                    (monitor_name,),
                )
                total = (await cur.fetchone())["cnt"]

                await cur.execute(
                    """
                    SELECT c.check_time, c.status, c.is_up, c.response_ms,
                           c.error, c.details
                    FROM check_changes s
                    JOIN checks c ON c.id = s.check_id
                    WHERE s.monitor_name = %s
                    ORDER BY s.check_time DESC
                    LIMIT %s OFFSET %s
                    """,
                    (monitor_name, per_page, offset),
                )
            else:
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM checks "
                    "WHERE monitor_name = %s",
                    (monitor_name,),
                )
                total = (await cur.fetchone())["cnt"]

                await cur.execute(
                    """
                    SELECT check_time, status, is_up, response_ms, error, details
                    FROM checks
                    WHERE monitor_name = %s
                    ORDER BY check_time DESC
                    LIMIT %s OFFSET %s
                    """,
                    (monitor_name, per_page, offset),
                )
            rows = await cur.fetchall()

    checks = []
    for row in rows:
        entry = {
            "time": _dt_to_iso(row["check_time"]),
            "status": row["status"],
            # None for maintenance/inactive log rows (never up or down) -- the
            # frontend doesn't re-derive up/down from the raw status code (a
            # fixed "2xx = up" rule can't know about a monitor's expected_status
            # override), it uses this stored classification directly.
            # aiomysql returns BOOLEAN/TINYINT(1) columns as a raw Python
            # int (0/1), not bool -- coerce explicitly so this serializes
            # as JSON true/false/null, not 1/0/null (the frontend does a
            # strict `=== true` check, which int 1 would silently fail).
            "is_up": None if row["is_up"] is None else bool(row["is_up"]),
            # `is not None`, not a truthiness test: a check that never
            # completed (timeout, DNS failure) stores response_ms NULL, and
            # coercing that to 0.0 would show "0.0ms" in the check log for a
            # request that never returned, and a real measured 0.0 would look
            # the same. None is what the check-log JS renders as an em dash.
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
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if state_changes_only:
                await cur.execute("SELECT COUNT(*) AS cnt FROM check_changes")
                total = (await cur.fetchone())["cnt"]

                await cur.execute(
                    """
                    SELECT c.monitor_name, c.check_time, c.status, c.is_up,
                           c.response_ms, c.error, c.details
                    FROM check_changes s
                    JOIN checks c ON c.id = s.check_id
                    ORDER BY s.check_time DESC
                    LIMIT %s OFFSET %s
                    """,
                    (per_page, offset),
                )
            else:
                await cur.execute("SELECT COUNT(*) AS cnt FROM checks")
                total = (await cur.fetchone())["cnt"]

                await cur.execute(
                    """
                    SELECT monitor_name, check_time, status, is_up,
                           response_ms, error, details
                    FROM checks
                    ORDER BY check_time DESC
                    LIMIT %s OFFSET %s
                    """,
                    (per_page, offset),
                )
            rows = await cur.fetchall()

    checks = []
    for row in rows:
        entry = {
            "monitor": row["monitor_name"],
            "time": _dt_to_iso(row["check_time"]),
            "status": row["status"],
            # aiomysql returns BOOLEAN/TINYINT(1) columns as a raw Python
            # int (0/1), not bool -- coerce explicitly so this serializes
            # as JSON true/false/null, not 1/0/null (the frontend does a
            # strict `=== true` check, which int 1 would silently fail).
            "is_up": None if row["is_up"] is None else bool(row["is_up"]),
            # `is not None`, not a truthiness test: a NULL response_ms (a check
            # that never completed) must stay None, not become 0.0. See
            # get_checks_paginated.
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
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT check_time, status, response_ms, error, details
                FROM checks
                WHERE monitor_name = %s AND check_time >= %s
                ORDER BY check_time ASC
                """,
                (monitor_name, cutoff),
            )
            rows = await cur.fetchall()

    checks = []
    for row in rows:
        entry = {
            "time": _dt_to_iso(row["check_time"]),
            "status": row["status"],
            # `is not None`, not a truthiness test: a NULL response_ms (a check
            # that never completed) must stay None, not become 0.0. See
            # get_checks_paginated.
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
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT m.name,
                       (SELECT c.response_ms FROM checks c
                        WHERE c.monitor_name = m.name
                        ORDER BY c.check_time DESC LIMIT 1)
                FROM monitors m
                """
            )
            rows = {row[0]: row[1] for row in await cur.fetchall()}
    return {
        name: float(rows[name]) if rows.get(name) is not None else None
        for name in monitor_names
    }


# --------------------------------------------------------------------------------------
def _uptime_pct(total, up_count, decimals: int) -> float:
    # SUM() comes back as a Decimal (or None when there are no rows).
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
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT monitor_name, SUM(stat_total), SUM(stat_up)
                FROM check_hourly
                WHERE hour_start >= %s
                GROUP BY monitor_name
                """,
                (start,),
            )
            rows = {row[0]: (row[1], row[2]) for row in await cur.fetchall()}
    return {
        name: _uptime_pct(*rows.get(name, (0, 0)), decimals)
        for name in monitor_names
    }


# --------------------------------------------------------------------------------------
async def prune_old_checks(monitor_name: str, retention_days: int) -> None:
    """Delete checks older than retention_days for a monitor."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM checks WHERE monitor_name = %s AND check_time < %s",
                (monitor_name, cutoff),
            )
            # The hour the cutoff falls in stays: part of it is still in
            # `checks`, and the summary should keep the whole hour's counts.
            await cur.execute(
                "DELETE FROM check_hourly "
                "WHERE monitor_name = %s AND hour_start < %s",
                (monitor_name, hour_floor(cutoff)),
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
        async with conn.cursor() as cur:
            await cur.execute("SELECT name FROM monitors")
            rows = await cur.fetchall()
    return [row[0] for row in rows]


# --------------------------------------------------------------------------------------
async def delete_monitor(name: str) -> None:
    """Delete a monitor and all its checks (CASCADE handles check rows).

    The checks table has ON DELETE CASCADE on the foreign key to monitors,
    so deleting the monitor row automatically removes all its check history.
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM monitors WHERE name = %s", (name,))
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
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT
                    SUM(stat_total) AS total,
                    SUM(stat_up) AS up_count,
                    SUM(stat_down) AS down_count,
                    SUM(resp_sum) / NULLIF(SUM(resp_count), 0) AS avg_response
                FROM check_hourly
                WHERE monitor_name = %s AND hour_start >= %s
                """,
                (monitor_name, cutoff),
            )
            row = await cur.fetchone()

            # Deliberately its own query, not folded into the one above:
            # a timeout is just a raw event, not a claim about being
            # down, so unlike total/up_count/down_count it does NOT go
            # through _EXCLUDE_VERIFYING_SQL -- DOWN requires the retry
            # mechanism to confirm it first (avoiding false-positive
            # fatigue), a timeout doesn't need that same confirmation to
            # be worth counting. status='timeout' can never also be
            # 'maintenance'/'inactive', so those exclusions don't apply
            # here either.
            await cur.execute(
                """
                SELECT COUNT(*) AS timeout_count
                FROM checks
                WHERE monitor_name = %s AND check_time >= %s
                  AND status = 'timeout'
                """,
                (monitor_name, cutoff),
            )
            timeout_row = await cur.fetchone()

            await cur.execute(
                f"""
                SELECT COUNT(*) AS outages
                FROM (
                    SELECT is_up,
                           LAG(is_up) OVER (ORDER BY check_time) AS prev_is_up
                    FROM checks
                    WHERE monitor_name = %s AND check_time >= %s
                      AND status != 'maintenance' AND status != 'inactive'
                      AND {_EXCLUDE_VERIFYING_SQL}
                    ORDER BY check_time
                ) AS t
                WHERE {_IS_DOWN_SQL} AND {_PREV_IS_UP_SQL}
                """,
                (monitor_name, cutoff),
            )
            outage_row = await cur.fetchone()

    total = int(row["total"] or 0)
    up_count = int(row["up_count"] or 0)
    down_count = int(row["down_count"] or 0)
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

    # Initialise results with zeros for all periods
    uptime_pct = {key: 0.0 for key, _ in periods}
    avg_latency = {key: 0.0 for key, _ in periods}

    if not monitor_names:
        return {"uptime_pct": uptime_pct, "avg_latency": avg_latency}

    now = datetime.now(timezone.utc)
    starts = [period_window_start(now, days) for _, days in periods]
    placeholders = ",".join(["%s"] * len(monitor_names))
    # One column group per period; the `hour_start >= %s` conditions pick the
    # hours that belong to each period out of the single scan.
    columns = ", ".join(
        "COALESCE(SUM(CASE WHEN hour_start >= %s THEN stat_total END), 0), "
        "COALESCE(SUM(CASE WHEN hour_start >= %s THEN stat_up END), 0), "
        "COALESCE(SUM(CASE WHEN hour_start >= %s THEN resp_sum END), 0), "
        "COALESCE(SUM(CASE WHEN hour_start >= %s THEN resp_count END), 0)"
        for _ in periods
    )
    params: list[object] = [value for start in starts for value in (start,) * 4]
    params.append(min(starts))
    params.extend(monitor_names)

    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {columns}
                FROM check_hourly
                WHERE hour_start >= %s AND monitor_name IN ({placeholders})
                """,
                params,
            )
            row = await cur.fetchone()

    for index, (key, _) in enumerate(periods):
        total, up_count, resp_sum, resp_count = row[index * 4:index * 4 + 4]
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
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"""
                SELECT check_time, response_ms
                FROM checks
                WHERE monitor_name = %s AND check_time >= %s
                  AND {_IS_UP_SQL}
                  AND response_ms IS NOT NULL
                ORDER BY check_time ASC
                """,
                (monitor_name, cutoff),
            )
            rows = await cur.fetchall()
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
    get_uptime_pct_many), verifying checks are deliberately counted here:
    the bars show what is actually happening right now, including the
    current hour of a monitor that is mid-retry-sequence, which would
    otherwise consist entirely of excluded "verifying" rows and show as
    "nodata" (see up_count/down_count in the check_hourly comment).
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised")

    now = now or datetime.now(timezone.utc)
    slot_zero = hour_floor(now)
    cutoff = hour_window_start(now, hours)

    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT hour_start, up_count, down_count FROM check_hourly
                WHERE monitor_name = %s AND hour_start >= %s
                """,
                (monitor_name, cutoff),
            )
            db_buckets = {
                row[0].strftime("%Y-%m-%d %H:00"): (row[1], row[2])
                for row in await cur.fetchall()
            }

    # Build full bucket list (including nodata hours). The labels come
        # from the shared, cached scaffold in state.py -- they're identical
        # for every monitor, so computing them per monitor would dominate the
        # cost of a dashboard render (see hourly_bar_scaffold()).
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
    # The database packs each monitor's hours into one string (so the driver
    # parses one row per monitor, not one per monitor per hour, which would
    # dominate the time at 1000 monitors): per hour, its age in hours
    # (0 = the current hour) zero-padded to a fixed width, then a 1/0 flag
    # for "has up checks" and one for "has down checks".
    width = len(str(hours))
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT monitor_name,
                       GROUP_CONCAT(CONCAT(
                           LPAD(TIMESTAMPDIFF(HOUR, hour_start, %s), {width}, '0'),
                           IF(up_count > 0, '1', '0'),
                           IF(down_count > 0, '1', '0')
                       ) SEPARATOR '')
                FROM check_hourly
                WHERE hour_start >= %s AND hour_start <= %s
                GROUP BY monitor_name
                """,
                (slot_zero, hour_window_start(now, hours), slot_zero),
            )
            packed = {name: text for name, text in await cur.fetchall()}

    return {
        name: _unpack_bar_statuses(packed.get(name), hours, width)
        for name in monitor_names
    }


# --------------------------------------------------------------------------------------
async def get_server_version() -> str:
    """Return the database server's own version string (SELECT VERSION())."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT VERSION()")
            row = await cur.fetchone()
    return str(row[0])


# --------------------------------------------------------------------------------------
async def has_system_notice(key: str) -> bool:
    """Whether the system notice with this key has already been sent."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM system_notices WHERE notice_key = %s", (key,)
            )
            row = await cur.fetchone()
    return row is not None


# --------------------------------------------------------------------------------------
async def record_system_notice(key: str) -> None:
    """Remember that the system notice with this key has been sent."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT IGNORE INTO system_notices (notice_key, sent_at) "
                "VALUES (%s, UTC_TIMESTAMP(3))",
                (key,),
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
