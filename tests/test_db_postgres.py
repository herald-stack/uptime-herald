# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_db_postgres.py - Tests for the PostgreSQL backend, src/db_postgres.py.

(src/db_mariadb.py, the other backend, has its own parallel test file,
test_db.py.)

No real PostgreSQL is available in this environment for THIS file
specifically -- the real-Postgres integration tier is
test_db_postgres_integration.py. These tests instead fake out asyncpg's
pool/connection protocol with a minimal in-memory stand-in, so
db_postgres.py's own logic is what's under test, not mocked away.

asyncpg has no separate cursor object (unlike aiomysql) -- connections
expose fetch()/fetchrow()/fetchval()/execute() directly, which is why
this fake's shape looks different from test_db.py's FakeCursor.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src import db_postgres as db


# --- Fakes for asyncpg's pool/connection protocol -------------------------------------


class _AsyncCM:
    """Minimal async context manager that yields a fixed object."""

    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """Stand-in for an asyncpg Connection. Records every call and
    replays queued fetchrow/fetch/fetchval results in call order."""

    def __init__(self, fetchrow_results=None, fetch_results=None,
                 fetchval_results=None):
        self._fetchrow_results = list(fetchrow_results or [])
        self._fetch_results = list(fetch_results or [])
        self._fetchval_results = list(fetchval_results or [])
        self.executed = []
        # Transaction bookkeeping for the transaction() tests.
        self.calls = []

    def transaction(self):
        return _FakeTransaction(self)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        if self._fetchrow_results:
            return self._fetchrow_results.pop(0)
        return None

    async def fetch(self, sql, *args):
        self.executed.append((sql, args))
        if self._fetch_results:
            return self._fetch_results.pop(0)
        return []

    async def fetchval(self, sql, *args):
        self.executed.append((sql, args))
        if self._fetchval_results:
            return self._fetchval_results.pop(0)
        return None


class _FakeTransaction:
    """Stand-in for asyncpg's Connection.transaction() context manager.

    The real one commits on clean exit and rolls back if the block
    raises; this just records which happened so the transaction() tests
    can assert it without a real database.
    """

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.calls.append("begin")
        return self

    async def __aexit__(self, exc_type, *_):
        self._conn.calls.append("rollback" if exc_type else "commit")
        return False  # never swallow -- asyncpg re-raises too


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AsyncCM(self._conn)

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _restore_pool():
    """db_postgres._pool is a module-level global these tests monkeypatch
    directly -- restore it to None afterward so tests can't leak a fake
    pool into other test files."""
    yield
    db._pool = None


# --- _dt_to_iso / _iso_to_dt ----------------------------------------------------------
#
# Pure functions with logic identical to the MariaDB backend, but worth their own
# direct coverage since this module has its own copy.


def test_dt_to_iso_none():
    assert db._dt_to_iso(None) is None


def test_dt_to_iso_naive_datetime_assumes_utc():
    dt = datetime(2026, 8, 16, 10, 30, 0)
    assert db._dt_to_iso(dt) == "2026-08-16T10:30:00+00:00"


def test_dt_to_iso_aware_datetime_preserves_tzinfo():
    dt = datetime(2026, 8, 16, 10, 30, 0, tzinfo=timezone.utc)
    assert db._dt_to_iso(dt) == "2026-08-16T10:30:00+00:00"


def test_iso_to_dt_none():
    assert db._iso_to_dt(None) is None


def test_iso_to_dt_valid_string():
    dt = db._iso_to_dt("2026-08-16T10:30:00+00:00")
    assert dt.year == 2026
    assert dt.hour == 10


def test_iso_to_dt_invalid_string_returns_none():
    assert db._iso_to_dt("not-a-date") is None


# --- transaction(): atomicity primitive -----------------------------------------------
#
# The Postgres half of the same guarantee tested in test_db.py. Unlike
# the MariaDB backend (which must issue an explicit BEGIN to override an
# autocommit pool), this delegates to asyncpg's own
# Connection.transaction(), so these tests assert the delegation and the
# shared-connection plumbing rather than re-testing asyncpg itself.


@pytest.mark.asyncio
async def test_transaction_raises_without_pool(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(RuntimeError, match="not initialised"):
        async with db.transaction():
            pass


@pytest.mark.asyncio
async def test_transaction_commits_on_clean_exit(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    async with db.transaction() as tx:
        assert tx is conn

    assert conn.calls == ["begin", "commit"]


@pytest.mark.asyncio
async def test_transaction_rolls_back_and_reraises_on_error(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    with pytest.raises(ValueError, match="boom"):
        async with db.transaction():
            raise ValueError("boom")

    assert conn.calls == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_writes_inside_transaction_use_the_shared_connection(monkeypatch):
    """save_state + insert_check with conn= must run on the transaction's
    connection rather than acquiring their own."""
    tx_conn = FakeConn()
    other_conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(other_conn))

    await db.save_state(
        db.MonitorState(name="m1", type="http", total_checks=1, total_up=1),
        conn=tx_conn,
    )
    await db.insert_check(
        monitor_name="m1", check_time="2026-09-11T00:00:00+00:00",
        status="200", is_up=True, conn=tx_conn,
    )

    # save_state, then insert_check: look up the previous check, insert the
    # check, record it as a state change (it is the monitor's first), and add
    # it to its hour in the summary -- all on the same connection, so they
    # stay atomic
    assert len(tx_conn.executed) == 5
    assert "INSERT INTO monitors" in tx_conn.executed[0][0]
    assert "SELECT is_up FROM checks" in tx_conn.executed[1][0]
    assert "INSERT INTO checks" in tx_conn.executed[2][0]
    assert "INSERT INTO check_changes" in tx_conn.executed[3][0]
    assert "INSERT INTO check_hourly" in tx_conn.executed[4][0]
    assert other_conn.executed == []


@pytest.mark.asyncio
async def test_writes_with_conn_do_not_need_a_pool(monkeypatch):
    """Joining a caller's transaction must not require _pool."""
    tx_conn = FakeConn()
    monkeypatch.setattr(db, "_pool", None)

    await db.insert_check(
        monitor_name="m1", check_time="2026-09-11T00:00:00+00:00",
        status="200", is_up=True, conn=tx_conn,
    )

    assert len(tx_conn.executed) == 4


# --- Guard: functions must require an initialised pool --------------------------------


@pytest.mark.asyncio
async def test_load_state_raises_without_pool(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(RuntimeError):
        await db.load_state("m1", "http")


@pytest.mark.asyncio
async def test_insert_check_raises_without_pool(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(RuntimeError):
        await db.insert_check(monitor_name="m1", check_time="x", status="up")


# --- load_state -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_state_not_found_returns_fresh_state(monkeypatch):
    conn = FakeConn(fetchrow_results=[None])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    state = await db.load_state("nope", "http")

    assert state.name == "nope"
    assert state.type == "http"
    assert state.status == "unknown"
    assert state.total_checks == 0


@pytest.mark.asyncio
async def test_load_state_parses_existing_row(monkeypatch):
    row = {
        "name": "m1", "type": "http", "status": "up",
        "last_check": datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        "last_change": datetime(2026, 7, 30, 8, 0, 0, tzinfo=timezone.utc),
        "first_check": datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc),
        "total_checks": 100, "total_up": 99, "total_down": 1,
        "uptime_pct": 99.0, "last_alert_sent": None, "state_changed": True,
    }
    conn = FakeConn(fetchrow_results=[row])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    state = await db.load_state("m1", "http")

    assert state.status == "up"
    assert state.total_checks == 100
    assert state.uptime_pct == 99.0
    assert state.state_changed is True
    assert state.last_check == "2026-08-01T12:00:00+00:00"
    assert state.last_alert_sent is None


# --- save_state -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_state_computes_uptime_pct(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))
    state = db.MonitorState(
        name="m1", type="http", total_checks=4, total_up=3, total_down=1,
    )

    await db.save_state(state)

    assert state.uptime_pct == 75.0
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "INSERT INTO monitors" in sql
    assert "ON CONFLICT" in sql
    assert args[0] == "m1"
    assert args[9] == 75.0  # uptime_pct is the 10th positional param


@pytest.mark.asyncio
async def test_save_state_zero_checks_leaves_uptime_pct_unchanged(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))
    state = db.MonitorState(name="m1", type="http", total_checks=0, uptime_pct=42.0)

    await db.save_state(state)

    assert state.uptime_pct == 42.0


# --- insert_check ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_check_serializes_details_to_json(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    await db.insert_check(
        monitor_name="m1", check_time="2026-08-16T00:00:00+00:00",
        status="up", is_up=True, response_ms=12.5, error=None,
        details={"verifying": True},
    )

    sql, args = conn.executed[1]
    assert "INSERT INTO checks" in sql
    assert "::jsonb" in sql
    assert args[0] == "m1"
    assert args[2] == "up"
    assert args[3] is True
    assert args[4] == 12.5
    assert args[6] == '{"verifying": true}'


@pytest.mark.asyncio
async def test_insert_check_no_details_is_null(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    await db.insert_check(
        monitor_name="m1", check_time="2026-08-16T00:00:00+00:00",
        status="down", is_up=False, response_ms=None, error="timeout", details=None,
    )

    _, args = conn.executed[1]
    assert args[3] is False
    assert args[5] == "timeout"
    assert args[6] is None


# --- prune_old_checks -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_old_checks_deletes_when_positive(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    await db.prune_old_checks("m1", 30)

    assert len(conn.executed) == 2
    sql, args = conn.executed[0]
    assert "DELETE FROM checks" in sql
    assert args[0] == "m1"
    # the hourly summary is pruned too, keeping the hour the cutoff is in
    sql, args = conn.executed[1]
    assert "DELETE FROM check_hourly" in sql
    assert args[0] == "m1"
    assert args[1].minute == 0 and args[1].second == 0


# --- get_all_monitor_names / delete_monitor -------------------------------------------


@pytest.mark.asyncio
async def test_get_all_monitor_names(monkeypatch):
    conn = FakeConn(fetch_results=[[{"name": "a"}, {"name": "b"}]])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    names = await db.get_all_monitor_names()

    assert names == ["a", "b"]


@pytest.mark.asyncio
async def test_delete_monitor(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    await db.delete_monitor("m1")

    sql, args = conn.executed[0]
    assert "DELETE FROM monitors" in sql
    assert args == ("m1",)


# --- get_checks_paginated -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_checks_paginated_all_checks(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 16, tzinfo=timezone.utc),
        "status": "up", "is_up": True, "response_ms": 12.5, "error": None,
        "details": None,
    }]
    conn = FakeConn(fetchval_results=[1], fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_checks_paginated("m1", page=1, per_page=25)

    assert result["total"] == 1
    assert result["checks"][0]["status"] == "up"
    assert result["checks"][0]["is_up"] is True
    assert "error" not in result["checks"][0]
    assert "details" not in result["checks"][0]


@pytest.mark.asyncio
async def test_get_checks_paginated_is_up_coerced_to_bool(monkeypatch):
    """Defensive test mirroring the MariaDB backend: even
    though asyncpg maps BOOLEAN to Python bool natively, the entry dict
    should coerce explicitly so both backends' JSON output is identical
    and doesn't silently depend on driver behavior."""
    rows = [{
        "check_time": datetime(2026, 8, 16, tzinfo=timezone.utc),
        "status": "200", "is_up": 1, "response_ms": 5.0, "error": None,
        "details": None,
    }]
    conn = FakeConn(fetchval_results=[1], fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_checks_paginated("m1", page=1, per_page=25)

    assert result["checks"][0]["is_up"] is True
    assert type(result["checks"][0]["is_up"]) is bool


@pytest.mark.asyncio
async def test_get_checks_paginated_state_changes_only(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 16, tzinfo=timezone.utc),
        "status": "down", "is_up": False, "response_ms": 5.0, "error": None,
        "details": None,
    }]
    conn = FakeConn(fetchval_results=[1], fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_checks_paginated(
        "m1", page=1, per_page=25, state_changes_only=True,
    )

    assert result["total"] == 1
    assert "FROM check_changes" in conn.executed[0][0]
    assert "FROM check_changes" in conn.executed[1][0]


@pytest.mark.asyncio
async def test_get_checks_paginated_negative_page_clamped(monkeypatch):
    conn = FakeConn(fetchval_results=[0], fetch_results=[[]])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_checks_paginated("m1", page=-5, per_page=25)

    assert result["page"] == 1
    _, args = conn.executed[1]
    assert args[-1] == 0  # OFFSET must never go negative


# --- get_all_checks_paginated ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_checks_paginated_includes_monitor_name(monkeypatch):
    rows = [{
        "monitor_name": "m1",
        "check_time": datetime(2026, 8, 16, tzinfo=timezone.utc),
        "status": "up", "is_up": True, "response_ms": 12.5, "error": None,
        "details": None,
    }]
    conn = FakeConn(fetchval_results=[1], fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_all_checks_paginated(page=1, per_page=25)

    assert result["checks"][0]["monitor"] == "m1"


@pytest.mark.asyncio
async def test_get_all_checks_paginated_is_up_coerced_to_bool(monkeypatch):
    """Same coercion as the per-monitor query, covered
    separately since get_all_checks_paginated builds its own entry dict."""
    rows = [{
        "monitor_name": "m1",
        "check_time": datetime(2026, 8, 16, tzinfo=timezone.utc),
        "status": "200", "is_up": 0, "response_ms": 12.5, "error": None,
        "details": None,
    }]
    conn = FakeConn(fetchval_results=[1], fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_all_checks_paginated(page=1, per_page=25)

    assert result["checks"][0]["is_up"] is False
    assert type(result["checks"][0]["is_up"]) is bool


@pytest.mark.asyncio
async def test_get_all_checks_paginated_state_changes_only(monkeypatch):
    conn = FakeConn(fetchval_results=[0], fetch_results=[[]])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    await db.get_all_checks_paginated(page=1, per_page=25, state_changes_only=True)

    assert "FROM check_changes" in conn.executed[0][0]
    assert "FROM check_changes" in conn.executed[1][0]


# --- get_recent_checks ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_checks(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 16, tzinfo=timezone.utc),
        "status": "down", "response_ms": 5.0, "error": "timeout",
        "details": '{"foo": "bar"}',
    }]
    conn = FakeConn(fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    checks = await db.get_recent_checks("m1", hours=24)

    assert len(checks) == 1
    assert checks[0]["error"] == "timeout"
    assert checks[0]["details"] == {"foo": "bar"}


# --- get_monitor_stats ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_monitor_stats_with_data_and_outage(monkeypatch):
    stats_row = {"total": 100, "up_count": 95, "down_count": 5, "avg_response": 42.345}
    timeout_row = {"timeout_count": 2}
    outage_row = {"outages": 2}
    conn = FakeConn(fetchrow_results=[stats_row, timeout_row, outage_row])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    stats = await db.get_monitor_stats("m1", days=30)

    assert stats["uptime_pct"] == 95.0
    assert stats["total_checks"] == 100
    assert stats["outages"] == 2
    assert stats["avg_response"] == 42.3


@pytest.mark.asyncio
async def test_get_monitor_stats_counts_every_timeout_unverified(monkeypatch):
    """down_count folds timeout in via is_up=FALSE (see _IS_DOWN_SQL's
    comment) but only after retry-verification confirms it -- a raw
    timeout isn't itself a claim about being down (that's what the
    retry mechanism is for), so total_timeout is queried completely
    separately, with no _EXCLUDE_VERIFYING_SQL filter at all. This test
    picks deliberately unrelated numbers (timeout_count higher than
    down_count) to prove it isn't derived from or capped by down_count."""
    stats_row = {"total": 10, "up_count": 6, "down_count": 4, "avg_response": 10.0}
    timeout_row = {"timeout_count": 7}
    outage_row = {"outages": 1}
    conn = FakeConn(fetchrow_results=[stats_row, timeout_row, outage_row])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    stats = await db.get_monitor_stats("m1", days=30)

    assert stats["total_down"] == 4
    assert stats["total_timeout"] == 7


@pytest.mark.asyncio
async def test_get_monitor_stats_no_data(monkeypatch):
    stats_row = {
        "total": 0, "up_count": None, "down_count": None, "avg_response": None,
    }
    timeout_row = {"timeout_count": None}
    outage_row = {"outages": None}
    conn = FakeConn(fetchrow_results=[stats_row, timeout_row, outage_row])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    stats = await db.get_monitor_stats("m1", days=30)

    assert stats["uptime_pct"] == 0.0
    assert stats["outages"] == 0
    assert stats["total_timeout"] == 0
    assert stats["avg_response"] is None


@pytest.mark.asyncio
async def test_get_monitor_stats_respects_decimals_params(monkeypatch):
    stats_row = {"total": 3, "up_count": 2, "down_count": 1, "avg_response": 42.345}
    timeout_row = {"timeout_count": 1}
    outage_row = {"outages": 1}
    conn = FakeConn(fetchrow_results=[stats_row, timeout_row, outage_row])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    stats = await db.get_monitor_stats(
        "m1", days=30, uptime_decimals=4, latency_decimals=2,
    )

    assert stats["uptime_pct"] == round(2 / 3 * 100, 4)
    assert stats["avg_response"] == round(42.345, 2)


# --- get_aggregate_stats --------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_aggregate_stats_empty_monitor_list_short_circuits(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_aggregate_stats([], [("24h", 1)])

    assert result["uptime_pct"]["24h"] == 0.0
    assert conn.executed == []  # no query run at all


@pytest.mark.asyncio
async def test_get_aggregate_stats_computes_per_period(monkeypatch):
    # one row, four numbers per period: checks, up, response sum, response count
    row = (10, 9, 500.0, 10, 0, 0, 0, 0)
    conn = FakeConn(fetchrow_results=[row])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_aggregate_stats(
        ["m1", "m2"], [("24h", 1), ("7d", 7)],
    )

    assert result["uptime_pct"]["24h"] == 90.0
    assert result["avg_latency"]["24h"] == 50.0
    assert result["uptime_pct"]["7d"] == 0.0
    # Uses = ANY($n) with the list passed straight through, not
    # hand-built numbered placeholders per monitor name; one query for
    # all the periods.
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "= ANY(" in sql
    assert args[-1] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_get_aggregate_stats_respects_decimals_params(monkeypatch):
    row = (3, 1, 42.345, 1)
    conn = FakeConn(fetchrow_results=[row])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_aggregate_stats(
        ["m1"], [("24h", 1)], uptime_decimals=4, latency_decimals=2,
    )

    assert result["uptime_pct"]["24h"] == round(1 / 3 * 100, 4)
    assert result["avg_latency"]["24h"] == round(42.345, 2)


@pytest.mark.asyncio
async def test_get_hourly_bar_statuses_for_many_monitors_in_one_query(monkeypatch):
    # The database sends one packed string per monitor: for each hour its age
    # (0 = current hour), then a has-up-checks flag and a has-down-checks flag.
    rows = [
        {"monitor_name": "a", "packed": "010" "111"},
        {"monitor_name": "b", "packed": "201"},
        {"monitor_name": "not-asked-for", "packed": "010"},
    ]
    conn = FakeConn(fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_hourly_bar_statuses(["a", "b", "c"], hours=3)

    assert len(conn.executed) == 1
    assert result == {
        "a": ["up", "mixed", "nodata"],
        "b": ["nodata", "nodata", "down"],
        "c": ["nodata", "nodata", "nodata"],
    }


@pytest.mark.asyncio
async def test_get_uptime_pct_many_in_one_query(monkeypatch):
    rows = [
        {"monitor_name": "a", "total": 100, "up_count": 95},
        {"monitor_name": "b", "total": 3, "up_count": 2},
    ]
    conn = FakeConn(fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_uptime_pct_many(["a", "b", "c"], days=7, decimals=4)

    assert len(conn.executed) == 1
    assert result == {"a": 95.0, "b": round(2 / 3 * 100, 4), "c": 0.0}


@pytest.mark.asyncio
async def test_load_states_returns_a_state_for_every_monitor_asked_for(monkeypatch):
    row = {
        "name": "a", "type": "http", "status": "200",
        "last_check": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "last_change": None, "first_check": None,
        "total_checks": 4, "total_up": 3, "total_down": 1,
        "uptime_pct": 75.0, "last_alert_sent": None, "state_changed": True,
    }
    conn = FakeConn(fetch_results=[[row]])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    states = await db.load_states([("a", "http"), ("new", "ping")])

    assert len(conn.executed) == 1
    assert states["a"].status == "200"
    assert states["a"].total_checks == 4
    assert states["a"].state_changed is True
    # no row yet: a fresh state, same as load_state()
    assert states["new"] == db.MonitorState(name="new", type="ping")


@pytest.mark.asyncio
async def test_get_last_response_ms_many_in_one_query(monkeypatch):
    rows = [
        {"name": "a", "response_ms": 12.5},
        {"name": "b", "response_ms": None},
    ]
    conn = FakeConn(fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    result = await db.get_last_response_ms_many(["a", "b", "c"])

    assert len(conn.executed) == 1
    assert result == {"a": 12.5, "b": None, "c": None}


# --- check_hourly (the hourly summary the dashboard statistics read) ------------------
#
# insert_check() adds each check to its hour. These pin down what each kind
# of check adds, matching what the raw-row queries counted before:
# (up, down, stat_total, stat_up, stat_down, resp_sum, resp_count).


async def _hourly_values(monkeypatch, **check):
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))
    await db.insert_check(monitor_name="m1", check_time="2026-09-11T10:42:07+00:00", **check)
    sql, args = conn.executed[-1]
    assert "INSERT INTO check_hourly" in sql
    assert args[0] == "m1"
    assert args[1] == datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    return args[2:]


@pytest.mark.asyncio
async def test_hourly_summary_counts_an_up_check_with_its_response_time(monkeypatch):
    values = await _hourly_values(
        monkeypatch, status="200", is_up=True, response_ms=12.5,
    )
    assert values == (1, 0, 1, 1, 0, 12.5, 1)


@pytest.mark.asyncio
async def test_hourly_summary_counts_a_down_check(monkeypatch):
    values = await _hourly_values(monkeypatch, status="503", is_up=False)
    assert values == (0, 1, 1, 0, 1, 0.0, 0)


@pytest.mark.asyncio
async def test_hourly_summary_ignores_maintenance_log_rows(monkeypatch):
    values = await _hourly_values(monkeypatch, status="maintenance", is_up=None)
    assert values == (0, 0, 0, 0, 0, 0.0, 0)


@pytest.mark.asyncio
async def test_hourly_summary_unconfirmed_verifying_row_counts_for_bars_only(monkeypatch):
    values = await _hourly_values(
        monkeypatch, status="503", is_up=False, details={"verifying": True},
    )
    assert values == (0, 1, 0, 0, 0, 0.0, 0)


@pytest.mark.asyncio
async def test_hourly_summary_confirming_verifying_row_counts_everywhere(monkeypatch):
    values = await _hourly_values(
        monkeypatch, status="503", is_up=False,
        details={"verifying": True, "passed": True},
    )
    assert values == (0, 1, 1, 0, 1, 0.0, 0)


# --- check_changes (the "state changes" check log) ------------------------------------
#
# insert_check() looks at the monitor's previous check and records the new one
# in check_changes when it belongs in the log (state.is_state_change).


async def _changes_recorded(monkeypatch, previous, **check):
    conn = FakeConn(fetchrow_results=[previous])
    monkeypatch.setattr(db, "_pool", FakePool(conn))
    await db.insert_check(
        monitor_name="m1", check_time="2026-09-11T10:42:07+00:00", **check
    )
    return any("INSERT INTO check_changes" in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_the_first_check_of_a_monitor_is_a_state_change(monkeypatch):
    assert await _changes_recorded(monkeypatch, None, status="200", is_up=True) is True


@pytest.mark.asyncio
async def test_a_check_with_the_same_health_as_the_previous_one_is_not(monkeypatch):
    previous = {"is_up": True}
    assert await _changes_recorded(monkeypatch, previous, status="200", is_up=True) is False


@pytest.mark.asyncio
async def test_a_check_whose_health_differs_from_the_previous_one_is(monkeypatch):
    assert await _changes_recorded(
        monkeypatch, {"is_up": True}, status="503", is_up=False,
    ) is True
    assert await _changes_recorded(
        monkeypatch, {"is_up": False}, status="200", is_up=True,
    ) is True


@pytest.mark.asyncio
async def test_a_verifying_check_is_always_a_state_change(monkeypatch):
    assert await _changes_recorded(
        monkeypatch, {"is_up": False}, status="503", is_up=False,
        details={"verifying": True},
    ) is True


@pytest.mark.asyncio
async def test_backfill_builds_the_summary_once_on_an_existing_history():
    conn = FakeConn(fetchval_results=[False, True])
    await db._backfill_check_hourly(conn)

    assert len(conn.executed) == 3
    assert "INSERT INTO check_hourly" in conn.executed[2][0]
    assert "GROUP BY monitor_name" in conn.executed[2][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fetchval_results", [
    [True],            # the summary already has rows
    [False, False],    # no checks to summarise
])
async def test_backfill_does_nothing_when_not_needed(fetchval_results):
    conn = FakeConn(fetchval_results=fetchval_results)
    await db._backfill_check_hourly(conn)

    assert all("INSERT" not in sql for sql, _ in conn.executed)


# --- get_response_time_series ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_response_time_series(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 16, tzinfo=timezone.utc),
        "response_ms": 42.5,
    }]
    conn = FakeConn(fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    series = await db.get_response_time_series("m1", hours=24)

    assert series == [{"time": "2026-08-16T00:00:00+00:00", "response_ms": 42.5}]


# --- get_hourly_bars (bucketing/classification logic) ---------------------------------


@pytest.mark.asyncio
async def test_get_hourly_bars_classifies_status_and_fills_nodata(monkeypatch):
    now = datetime.now(timezone.utc)
    slot_zero = now.replace(minute=0, second=0, microsecond=0)
    hour1 = slot_zero - timedelta(hours=1)
    hour2 = slot_zero - timedelta(hours=2)  # left with no data on purpose

    rows = [
        {"hour_start": slot_zero, "up_count": 5, "down_count": 0},
        {"hour_start": hour1, "up_count": 2, "down_count": 3},
    ]
    conn = FakeConn(fetch_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    bars = await db.get_hourly_bars("m1", hours=3)

    assert len(bars) == 3
    by_hour = {b["hour"]: b for b in bars}
    assert by_hour[slot_zero.strftime("%Y-%m-%dT%H:%M")]["status"] == "up"
    assert by_hour[hour1.strftime("%Y-%m-%dT%H:%M")]["status"] == "mixed"
    assert by_hour[hour2.strftime("%Y-%m-%dT%H:%M")]["status"] == "nodata"
    # Reads the hourly summary, not the raw checks
    assert "FROM check_hourly" in conn.executed[0][0]


@pytest.mark.asyncio
async def test_get_hourly_bars_converts_to_display_timezone(monkeypatch):
    from zoneinfo import ZoneInfo
    conn = FakeConn(fetch_results=[[]])
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    bars = await db.get_hourly_bars("m1", hours=1, display_tz=ZoneInfo("America/Chicago"))

    assert bars[0]["hour_display"] != bars[0]["hour"]
    assert bars[0]["hour_display_end"] != ""


# --- init_schema: Postgres's own migration handling -----------------------------------


@pytest.mark.asyncio
async def test_init_schema_raises_without_pool(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(RuntimeError, match="not initialised"):
        await db.init_schema()


@pytest.mark.asyncio
async def test_init_schema_runs_all_statements_and_migration(monkeypatch):
    """Unlike the MariaDB backend, Postgres's ADD COLUMN IF NOT EXISTS
    needs no duplicate-column error handling at all -- this just
    confirms the migration statement actually runs, every time, without
    needing a try/except dance."""
    conn = FakeConn()
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    await db.init_schema()

    all_sql = " ".join(sql for sql, _ in conn.executed)
    assert "CREATE TABLE IF NOT EXISTS monitors" in all_sql
    assert "CREATE TABLE IF NOT EXISTS checks" in all_sql
    assert "CREATE INDEX IF NOT EXISTS idx_monitor_time" in all_sql
    assert "ADD COLUMN IF NOT EXISTS state_changed" in all_sql
