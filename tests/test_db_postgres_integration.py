# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_db_postgres_integration.py - Tests for the PostgreSQL backend on a real server.

(src/db_mariadb.py has its own parallel real-database integration tier,
test_db_integration.py, against a real MariaDB instance -- this file
mirrors it closely, adapted for asyncpg's exception types and Postgres's
own dialect.)

Every other test in test_db_postgres.py fakes out asyncpg entirely
(FakeConn just records the SQL text handed to it -- it never actually
executes anything). That's fast and dependency-free, but it has a real
blind spot: a typo'd column name, invalid SQL syntax, or a
Postgres-version incompatibility (window functions, the ->>'key' JSONB
operator, the ~ regex operator in the is_up backfill) would sail
through every fake-cursor test without a single failure, because the
fake never checks whether the SQL it was handed is actually valid.

These tests run the real queries against a real database instead. They
are SKIPPED by default -- set DB_POSTGRES_INTEGRATION_TEST=1 (plus the
usual DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD env vars) to opt in.
CI sets these against a disposable PostgreSQL container on the CI
runner; nothing here should ever need a real
database for a normal local `pytest tests/` run.
"""

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from src import db_postgres as db
from src import db_support

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("DB_POSTGRES_INTEGRATION_TEST") != "1",
        reason="set DB_POSTGRES_INTEGRATION_TEST=1 (+ DB_HOST/DB_USER/etc) "
               "to run against a real PostgreSQL instance",
    ),
]


@pytest_asyncio.fixture(autouse=True)
async def _real_pool():
    """A real connection pool + schema, fresh per test.

    Function-scoped (not module-scoped) deliberately -- see the matching
    fixture in test_db_integration.py for why (pytest-asyncio's default
    event loop is also function-scoped).

    Also doubles as an implicit smoke test that init_schema()'s
    ADD COLUMN IF NOT EXISTS migration behaves correctly across
    repeated runs against a real Postgres instance, not just the mocked
    version in test_db_postgres.py.
    """
    await db.init_pool()
    await db.init_schema()
    yield
    await db.close_pool()


def _unique_name(label: str) -> str:
    """A fresh monitor name per test, so tests can run against a shared
    real database without cleaning up after each other or colliding."""
    return f"itest-pg-{label}-{uuid.uuid4().hex[:8]}"


def _now_iso(minutes_offset: int = 0) -> str:
    """A real "now" (or now +/- minutes_offset) timestamp -- deliberately
    NOT a hardcoded literal. See test_db_integration.py's matching
    helper for why (a hardcoded date falls outside a "last 24 hours" query
    once the actual clock moves past it)."""
    return (
        datetime.now(timezone.utc) + timedelta(minutes=minutes_offset)
    ).isoformat()


async def test_real_fk_constraint_blocks_orphan_check():
    """The actual regression this whole test tier exists to catch: a
    real ForeignKeyViolationError from PostgreSQL's own foreign key,
    not FakeConn's hand-written mimic of one."""
    import asyncpg
    name = _unique_name("orphan")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.insert_check(
            monitor_name=name, check_time=_now_iso(),
            status="up",
        )


async def test_load_state_save_state_roundtrip_real_types():
    """Verifies the row-to-dataclass mapping survives a real round trip
    through Postgres's actual column types (DECIMAL for uptime_pct,
    TIMESTAMPTZ for timestamps, BOOLEAN for state_changed) -- exactly
    the kind of type mismatch FakeConn's plain Python dicts can't
    catch, since it never converts anything."""
    name = _unique_name("roundtrip")
    state = await db.load_state(name, "http")
    assert state.status == "unknown"

    state.status = "up"
    state.total_checks = 10
    state.total_up = 8
    state.total_down = 2
    state.state_changed = True
    await db.save_state(state)

    reloaded = await db.load_state(name, "http")
    assert reloaded.status == "up"
    assert reloaded.total_checks == 10
    assert reloaded.uptime_pct == 80.0
    assert isinstance(reloaded.uptime_pct, float)  # not Decimal
    assert reloaded.state_changed is True


async def test_save_state_upsert_real_conflict_clause():
    """ON CONFLICT (name) DO UPDATE, against a real second write to the
    same monitor -- confirms the upsert actually updates in place rather
    than raising a duplicate-key error, which FakeConn can't verify
    since it doesn't enforce the primary key at all."""
    name = _unique_name("upsert")
    state = await db.load_state(name, "http")
    state.status = "up"
    await db.save_state(state)

    state.status = "down"
    state.total_checks = 5
    state.total_up = 0
    state.total_down = 5
    await db.save_state(state)  # must not raise a duplicate-key error

    reloaded = await db.load_state(name, "http")
    assert reloaded.status == "down"
    assert reloaded.total_checks == 5


async def test_insert_check_jsonb_cast_real_roundtrip():
    """The ::jsonb cast on insert_check's details parameter, against a
    real column -- confirms asyncpg's str-to-jsonb handling actually
    works, not just that the SQL string contains the cast."""
    name = _unique_name("jsonb")
    state = await db.load_state(name, "http")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(),
        status="down", is_up=False, response_ms=None, error="x",
        details={"verifying": True, "nested": {"a": 1}},
    )
    checks = await db.get_recent_checks(name, hours=24)
    assert checks[0]["details"] == {"verifying": True, "nested": {"a": 1}}


async def test_insert_inactive_log_style_flow_against_real_fk():
    """Mirrors main.py's _insert_inactive_log() ordering directly
    against the real schema: load_state()+save_state() must create the
    monitors row before insert_check() is attempted, or this raises."""
    name = _unique_name("inactive")
    state = await db.load_state(name, "ping")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(),
        status="inactive",
    )
    checks = await db.get_recent_checks(name, hours=24)
    assert len(checks) == 1
    assert checks[0]["status"] == "inactive"


async def test_transaction_commits_both_writes_together():
    """The happy path of main.py's _record_check() pairing: both writes
    inside one db.transaction() must be visible after it commits."""
    name = _unique_name("tx-commit")
    state = await db.load_state(name, "http")
    state.total_checks = 1
    state.total_up = 1
    async with db.transaction() as conn:
        await db.save_state(state, conn=conn)
        await db.insert_check(
            monitor_name=name, check_time=_now_iso(),
            status="200", is_up=True, response_ms=5.0, conn=conn,
        )

    reloaded = await db.load_state(name, "http")
    checks = await db.get_recent_checks(name, hours=24)
    assert reloaded.total_checks == 1
    assert len(checks) == 1


async def test_transaction_rolls_back_state_write_when_insert_fails():
    """The Postgres half of the atomicity guarantee -- see the MariaDB
    twin in test_db_integration.py for the full description of the
    desynchronisation this prevents.

    Forces the insert to fail with a status longer than
    checks.status VARCHAR(20); Postgres rejects it with 22001.
    """
    name = _unique_name("tx-rollback")
    state = await db.load_state(name, "http")
    state.total_checks = 1
    state.total_up = 1
    async with db.transaction() as conn:
        await db.save_state(state, conn=conn)
        await db.insert_check(
            monitor_name=name, check_time=_now_iso(),
            status="200", is_up=True, response_ms=5.0, conn=conn,
        )
    assert (await db.load_state(name, "http")).total_checks == 1

    state = await db.load_state(name, "http")
    state.total_checks += 1
    state.total_down += 1
    with pytest.raises(Exception):
        async with db.transaction() as conn:
            await db.save_state(state, conn=conn)
            await db.insert_check(
                monitor_name=name, check_time=_now_iso(),
                status="x" * 25,  # exceeds VARCHAR(20) -- DB rejects it
                is_up=False, conn=conn,
            )

    reloaded = await db.load_state(name, "http")
    checks = await db.get_recent_checks(name, hours=24)
    assert reloaded.total_checks == 1, (
        f"state write survived a failed insert: total_checks="
        f"{reloaded.total_checks} vs COUNT(*)={len(checks)}"
    )
    assert len(checks) == 1
    assert reloaded.total_down == 0


async def test_get_hourly_bars_real_sql_syntax():
    """Exercises the real TO_CHAR/GROUP BY query -- validates the SQL is
    actually syntactically correct against this Postgres version, which
    a fake connection can never verify."""
    name = _unique_name("bars")
    state = await db.load_state(name, "http")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(),
        status="up", is_up=True, response_ms=10.0,
    )
    bars = await db.get_hourly_bars(name, hours=24)
    assert len(bars) == 24
    # See the MariaDB twin: an `any(... in ("up", "nodata"))` assertion would
    # be satisfied by the 23 empty buckets alone, so it could not fail even
    # with _IS_UP_SQL/_IS_DOWN_SQL inverted. Pinning the single populated
    # bucket also covers this backend's TO_CHAR bucket-key timezone: if
    # the SQL-side key drifts from the Python-side lookup key, the check
    # lands in no bucket at all and with_data comes back empty.
    with_data = [b for b in bars if b["status"] != "nodata"]
    assert len(with_data) == 1, (
        f"exactly one bucket should hold the seeded check, got {with_data}"
    )
    assert with_data[0]["status"] == "up"


async def test_get_monitor_stats_real_window_functions():
    """get_monitor_stats' outage-counting query uses LAG() OVER (...) --
    a real window function. Validates it against the actual Postgres
    version in use, not just that FakeConn accepted the string.

    Also covers total_timeout against a real, still-*verifying* timeout
    row: a monitor times out, and the retry catches it and recovers before it
    is ever confirmed down, so it never counts toward total_down/outages
    (_EXCLUDE_VERIFYING_SQL correctly excludes it, matching what "confirmed
    down" means) -- but a raw timeout isn't itself a claim about being down,
    so it still counts in total_timeout regardless of whether retry ever
    confirms it. That's the behavior this test locks in."""
    name = _unique_name("stats")
    state = await db.load_state(name, "http")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(-10),
        status="up", is_up=True, response_ms=10.0,
    )
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(-5),
        status="down", is_up=False, response_ms=None, error="refused",
    )
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(),
        status="timeout", is_up=False, response_ms=None, error="timed out",
        details={"verifying": True},
    )
    stats = await db.get_monitor_stats(name, days=365)
    assert stats["total_checks"] == 2  # the verifying timeout is excluded here
    assert stats["total_down"] == 1  # ...and here, same reason
    assert stats["total_timeout"] == 1  # but still counted here


async def test_get_monitor_stats_confirmed_retry_check_counts_toward_its_state():
    """The pre-confirmation retry attempts don't count (see the test above),
    but the retry that actually confirms the new state is a real transition
    and counts toward it -- that's details.passed=true (set only on the
    confirming retry, see _build_verification_check_details in main.py),
    distinct from a plain details.verifying=true probe that hasn't confirmed
    anything yet. _EXCLUDE_VERIFYING_SQL carves this one case back in."""
    name = _unique_name("stats")
    state = await db.load_state(name, "http")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(-10),
        status="up", is_up=True, response_ms=10.0,
    )
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(-5),
        status="timeout", is_up=False, response_ms=None, error="timed out",
        details={"verifying": True},  # unconfirmed probe -- excluded
    )
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(),
        status="down", is_up=False, response_ms=None, error="refused",
        details={"verifying": True, "passed": True},  # confirms it -- counts
    )
    stats = await db.get_monitor_stats(name, days=365)
    assert stats["total_checks"] == 2  # up + the confirming down, not the probe
    assert stats["total_down"] == 1  # the confirming check counts as down
    assert stats["outages"] == 1  # and is detected as the real up->down transition
    assert stats["outages"] == 1


async def test_get_aggregate_stats_real_any_array_param():
    """The = ANY($2) array-param pattern (Postgres-idiomatic replacement
    for MariaDB's hand-built IN (...) placeholder list), against real
    multi-monitor data."""
    name_a = _unique_name("agg-a")
    name_b = _unique_name("agg-b")
    for name in (name_a, name_b):
        state = await db.load_state(name, "http")
        await db.save_state(state)
        await db.insert_check(
            monitor_name=name, check_time=_now_iso(),
            status="up", is_up=True, response_ms=20.0,
        )
    result = await db.get_aggregate_stats([name_a, name_b], [("365d", 365)])
    assert result["uptime_pct"]["365d"] == 100.0


async def test_get_checks_paginated_state_changes_only_real_lag():
    """The state_changes_only branch's LAG()-based transition-detection
    query, against real data."""
    name = _unique_name("paginated")
    state = await db.load_state(name, "http")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(-10),
        status="up", is_up=True, response_ms=10.0,
    )
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(-5),
        status="up", is_up=True, response_ms=10.0,  # no transition -- should be excluded
    )
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(),
        status="down", is_up=False, response_ms=None, error="x",  # transition -- included
    )
    result = await db.get_checks_paginated(name, state_changes_only=True)
    # First check (no prior status) + the down transition = 2, the
    # repeated "up" in between is correctly excluded.
    assert result["total"] == 2


async def test_delete_monitor_cascades_to_checks():
    name = _unique_name("cascade")
    state = await db.load_state(name, "http")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(),
        status="up", is_up=True, response_ms=10.0,
    )
    await db.delete_monitor(name)
    assert name not in await db.get_all_monitor_names()
    # ON DELETE CASCADE should have removed the check row too, not left
    # it orphaned.
    checks = await db.get_recent_checks(name, hours=24)
    assert checks == []


async def test_prune_old_checks_real_delete():
    name = _unique_name("prune")
    state = await db.load_state(name, "http")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name, check_time="2020-01-01T00:00:00+00:00",
        status="up", is_up=True, response_ms=10.0,
    )
    await db.prune_old_checks(name, retention_days=30)
    checks = await db.get_recent_checks(name, hours=999999)
    assert checks == []


async def _count_notice_rows(key: str) -> int:
    async with db._pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM system_notices WHERE notice_key = $1", key
        )


async def _forget_notice(key: str) -> None:
    async with db._pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM system_notices WHERE notice_key = $1", key
        )


# --- System notices and version detection (src/db_support.py) -------------------------
#
# The three database functions behind the end-of-support notices
# (get_server_version / has_system_notice / record_system_notice) have to
# work on every supported server version, not just the newest. These run
# against whichever version CI points this file at.


def _lifecycle_cycles() -> dict:
    return db_support.load_lifecycle()["engines"]["postgresql"]["cycles"]


async def test_server_version_is_recognised_by_the_support_table():
    """What the real server reports parses to a series that the shipped
    end-of-support table knows. If this fails on a brand-new release, the
    table (src/db_lifecycle.json) needs refreshing from endoflife.date."""
    raw = await db.get_server_version()
    version = db_support.parse_server_version(raw)
    assert version is not None, f"unrecognised version string {raw!r}"
    assert version.engine == "postgresql", raw
    assert version.cycle in _lifecycle_cycles(), (
        f"{raw!r} -> {version.cycle} is missing from src/db_lifecycle.json"
    )


async def test_system_notice_is_remembered_and_recording_twice_is_harmless():
    key = f"itest-{uuid.uuid4().hex}"
    assert await db.has_system_notice(key) is False
    await db.record_system_notice(key)
    assert await db.has_system_notice(key) is True
    await db.record_system_notice(key)  # a second record must not raise
    assert await db.has_system_notice(key) is True
    assert await _count_notice_rows(key) == 1
    await _forget_notice(key)
    assert await db.has_system_notice(key) is False


async def test_check_and_notify_end_to_end_on_the_real_server(monkeypatch):
    """The whole path -- ask the server its version, find it in the table,
    pick the due threshold, send, remember -- against the real database."""
    sent = []

    async def fake_send(config, title, body):
        sent.append((title, body))
        return 1

    # db_support talks to whatever `src.db` resolved to; point it at the
    # backend this file is testing.
    monkeypatch.setattr(db_support, "db", db)
    monkeypatch.setattr(db_support, "send_system_notice", fake_send)

    version = db_support.parse_server_version(await db.get_server_version())
    assert version is not None
    eol = date.fromisoformat(_lifecycle_cycles()[version.cycle]["eol"])
    key = f"db-eol:{version.engine}:{version.cycle}:eol"
    await _forget_notice(key)  # a previous run on this database mustn't hide it

    config = {"global": {}}
    assert await db_support.check_and_notify(config, today=eol) == key
    assert len(sent) == 1
    assert version.cycle in sent[0][0]
    # Same day again (a restart, say): remembered, so nothing more is sent.
    assert await db_support.check_and_notify(config, today=eol) is None
    assert len(sent) == 1
    await _forget_notice(key)


# --- check_hourly: the summary the dashboard statistics read --------------------------


async def _insert_mixed_history(name: str) -> None:
    """One monitor with every kind of check the statistics treat differently."""
    state = await db.load_state(name, "http")
    await db.save_state(state)
    # (minutes ago, status, is_up, response_ms, details)
    history = [
        (5, "200", True, 12.5, None),
        (6, "200", True, None, None),
        (7, "503", False, None, {"status_code": 503}),
        (8, "timeout", False, None, None),
        (9, "maintenance", None, None, None),
        (10, "inactive", None, None, None),
        (11, "503", False, None, {"verifying": True}),
        (12, "503", False, None, {"verifying": True, "passed": True}),
        (13, "200", True, 7.0, {"verifying": False}),
        (125, "200", True, 100.0, None),
        (125, "503", False, None, None),
        (50 * 60, "200", True, 50.0, None),
    ]
    for minutes_ago, status, is_up, response_ms, details in history:
        await db.insert_check(
            monitor_name=name, check_time=_now_iso(-minutes_ago), status=status,
            is_up=is_up, response_ms=response_ms, details=details,
        )


async def _hourly_rows(name: str) -> list[tuple]:
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT hour_start, up_count, down_count, stat_total, stat_up, "
            "stat_down, resp_sum, resp_count FROM check_hourly "
            "WHERE monitor_name = $1 ORDER BY hour_start",
            name,
        )
    return [tuple(row) for row in rows]


async def test_hourly_summary_built_by_backfill_matches_the_one_kept_by_insert_check():
    """The summary is kept up to date two ways: incrementally by
    insert_check() (Python rules) and, on a database that predates it, by
    one INSERT ... SELECT over the existing checks (SQL rules). They must
    agree on what each check counts for."""
    name = _unique_name("hourly-backfill")
    await _insert_mixed_history(name)
    incremental = await _hourly_rows(name)
    assert incremental

    async with db._pool.acquire() as conn:
        await conn.execute("DELETE FROM check_hourly")
        await db._backfill_check_hourly(conn)

    rebuilt = await _hourly_rows(name)
    assert [row[:6] for row in rebuilt] == [row[:6] for row in incremental]
    assert [row[6] for row in rebuilt] == pytest.approx([row[6] for row in incremental])
    assert [row[7] for row in rebuilt] == [row[7] for row in incremental]


async def test_bulk_functions_return_each_monitors_own_values():
    name_a = _unique_name("bulk-a")
    name_b = _unique_name("bulk-b")
    await _insert_mixed_history(name_a)
    state = await db.load_state(name_b, "ping")
    await db.save_state(state)
    await db.insert_check(
        monitor_name=name_b, check_time=_now_iso(-3), status="up",
        is_up=True, response_ms=2.5,
    )
    names = [name_a, name_b, "never-existed"]
    now = datetime.now(timezone.utc)

    states = await db.load_states([(name_a, "http"), (name_b, "ping"), ("never-existed", "tcp")])
    assert states[name_a] == await db.load_state(name_a, "http")
    assert states["never-existed"].status == "unknown"

    # the newest check of each monitor was inserted with these response times
    assert await db.get_last_response_ms_many(names) == {
        name_a: 12.5, name_b: 2.5, "never-existed": None,
    }

    for days in (1, 7, 90):
        many = await db.get_uptime_pct_many(names, days, 2)
        for name in names:
            stats = await db.get_monitor_stats(name, days=days)
            assert many[name] == stats["uptime_pct"]

    bars = await db.get_hourly_bar_statuses(names, 60, now=now)
    for name in names:
        single = await db.get_hourly_bars(name, hours=60, now=now)
        assert bars[name] == [bar["status"] for bar in single]


async def test_aggregate_stats_combine_the_monitors_counts():
    name_a = _unique_name("agg-a")
    name_b = _unique_name("agg-b")
    await _insert_mixed_history(name_a)
    state = await db.load_state(name_b, "ping")
    await db.save_state(state)
    for minutes_ago in (3, 4):
        await db.insert_check(
            monitor_name=name_b, check_time=_now_iso(-minutes_ago), status="down",
            is_up=False,
        )

    periods = [("24h", 1), ("7d", 7), ("90d", 90)]
    agg = await db.get_aggregate_stats([name_a, name_b], periods, 4, 1)
    for key, days in periods:
        stats_a = await db.get_monitor_stats(name_a, days=days, uptime_decimals=4)
        stats_b = await db.get_monitor_stats(name_b, days=days, uptime_decimals=4)
        total = stats_a["total_checks"] + stats_b["total_checks"]
        up = stats_a["total_up"] + stats_b["total_up"]
        assert agg["uptime_pct"][key] == round(up / total * 100, 4)


async def test_prune_old_checks_also_prunes_the_hourly_summary():
    name = _unique_name("hourly-prune")
    await _insert_mixed_history(name)
    before = await _hourly_rows(name)
    assert len(before) >= 3

    await db.prune_old_checks(name, 1)

    after = await _hourly_rows(name)
    # the 50-hour-old hour is gone; the hours inside the last day remain
    assert 0 < len(after) < len(before)
    assert min(row[0] for row in after) >= min(row[0] for row in before)
    assert (await db.get_uptime_pct_many([name], 90, 2))[name] > 0


# --- check_changes: the state-change check log ----------------------------------------


_STATE_CHANGE_HISTORY = [
    # (minutes ago, status, is_up, details): oldest first
    (60, "200", True, None),
    (50, "200", True, None),
    (40, "503", False, None),
    (30, "503", False, None),
    (25, "503", False, {"verifying": True}),
    (20, "200", True, None),
    (15, "maintenance", None, None),
    (14, "maintenance", None, None),
    (10, "200", True, None),
    (5, "200", True, {"verifying": False}),
]


async def _insert_state_change_history(name: str) -> None:
    state = await db.load_state(name, "http")
    await db.save_state(state)
    for minutes_ago, status, is_up, details in _STATE_CHANGE_HISTORY:
        await db.insert_check(
            monitor_name=name, check_time=_now_iso(-minutes_ago), status=status,
            is_up=is_up, details=details,
        )


async def _window_function_state_changes(name: str) -> list[tuple]:
    """The window-function rule that check_changes replaces, run directly over
    the checks (kept here as the reference implementation)."""
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT check_time, status FROM (
                SELECT check_time, status, is_up, details,
                       LAG(status) OVER (ORDER BY check_time) AS prev_status,
                       LAG(is_up) OVER (ORDER BY check_time) AS prev_is_up
                FROM checks WHERE monitor_name = $1
            ) AS t
            WHERE prev_status IS NULL
                OR is_up IS DISTINCT FROM prev_is_up
                OR (details->>'verifying')::boolean IS TRUE
            ORDER BY check_time DESC
            """,
            name,
        )
    return [(row["check_time"], row["status"]) for row in rows]


async def test_state_change_log_matches_the_window_function_rule():
    name = _unique_name("changes")
    await _insert_state_change_history(name)

    expected = await _window_function_state_changes(name)
    assert len(expected) >= 5  # first check, down, verifying, up, maintenance edges

    result = await db.get_checks_paginated(name, page=1, per_page=50, state_changes_only=True)
    assert result["total"] == len(expected)
    assert [c["status"] for c in result["checks"]] == [status for _, status in expected]


async def test_state_change_list_built_by_backfill_matches_the_one_kept_by_insert_check():
    name = _unique_name("changes-backfill")
    await _insert_state_change_history(name)
    before = await db.get_checks_paginated(name, page=1, per_page=50, state_changes_only=True)

    async with db._pool.acquire() as conn:
        await conn.execute("DELETE FROM check_changes")
        await db._backfill_check_changes(conn)

    after = await db.get_checks_paginated(name, page=1, per_page=50, state_changes_only=True)
    assert after["total"] == before["total"]
    assert [(c["time"], c["status"]) for c in after["checks"]] == [
        (c["time"], c["status"]) for c in before["checks"]
    ]


async def test_all_monitors_state_change_log_includes_each_monitors_changes():
    name_a = _unique_name("changes-all-a")
    name_b = _unique_name("changes-all-b")
    await _insert_state_change_history(name_a)
    await _insert_state_change_history(name_b)

    result = await db.get_all_checks_paginated(page=1, per_page=1000, state_changes_only=True)

    by_monitor = {}
    for check in result["checks"]:
        by_monitor.setdefault(check["monitor"], []).append(check["status"])
    expected = [status for _, status in await _window_function_state_changes(name_a)]
    assert by_monitor[name_a] == expected
    assert by_monitor[name_b] == expected


async def test_pruning_a_check_removes_its_state_change_row():
    name = _unique_name("changes-prune")
    await _insert_state_change_history(name)
    await db.insert_check(
        monitor_name=name, check_time=_now_iso(-50 * 60), status="503", is_up=False,
    )
    before = await db.get_checks_paginated(name, page=1, per_page=50, state_changes_only=True)

    await db.prune_old_checks(name, 1)

    after = await db.get_checks_paginated(name, page=1, per_page=50, state_changes_only=True)
    assert after["total"] == before["total"] - 1
