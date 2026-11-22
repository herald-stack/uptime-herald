# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_db.py - Tests for the MariaDB backend, src/db_mariadb.py.

(src/db_postgres.py, the other backend, has its own parallel test file,
test_db_postgres.py.)

No real MariaDB is available in this environment (and the project's own CI
doesn't spin one up either for THIS file specifically — the real-MariaDB
integration tier is test_db_integration.py).
These tests instead fake out aiomysql's pool/connection/cursor
async-context-manager protocol with minimal in-memory stand-ins, so
db_mariadb.py's own logic — SQL parameter binding, JSON (de)serialization,
row-to-MonitorState mapping, the uptime_pct recomputation, the hourly-bar
bucketing/classification loop — is what's actually under test, not mocked
away.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src import db_mariadb as db
from src.state import db_connection_settings, pool_sizes

# --- Fakes for aiomysql's pool/connection/cursor protocol -----------------------------


class _AsyncCM:
    """Minimal async context manager that yields a fixed object."""

    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *exc):
        return False


class FakeCursor:
    """Records every execute() call and replays queued fetchone/fetchall
    results in call order (functions that run 2+ queries pop one result
    list per execute)."""

    def __init__(self, fetchone_results=None, fetchall_results=None):
        self._fetchone_results = list(fetchone_results or [])
        self._fetchall_results = list(fetchall_results or [])
        self.executed = []
        self.lastrowid = 1

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return None

    async def fetchall(self):
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        # Transaction bookkeeping, so the transaction() tests can assert
        # begin/commit/rollback ordering without a real database.
        self.calls = []

    def cursor(self, *args, **kwargs):
        return _AsyncCM(self._cursor)

    async def begin(self):
        self.calls.append("begin")

    async def commit(self):
        self.calls.append("commit")

    async def rollback(self):
        self.calls.append("rollback")


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AsyncCM(self._conn)


@pytest.fixture(autouse=True)
def _restore_pool():
    """db._pool is a module-level global these tests monkeypatch directly
    (rather than via monkeypatch.setattr, since some tests need it None) —
    restore it to None afterward so tests can't leak a fake pool into
    other test files."""
    yield
    db._pool = None


# --- _dt_to_iso / _iso_to_dt (pure functions) -----------------------------------------


def test_dt_to_iso_none():
    assert db._dt_to_iso(None) is None


def test_dt_to_iso_naive_datetime_assumes_utc():
    dt = datetime(2026, 8, 11, 10, 30, 0)
    assert db._dt_to_iso(dt) == "2026-08-11T10:30:00+00:00"


def test_dt_to_iso_aware_datetime_preserves_tzinfo():
    dt = datetime(2026, 8, 11, 10, 30, 0, tzinfo=timezone.utc)
    assert db._dt_to_iso(dt) == "2026-08-11T10:30:00+00:00"


def test_iso_to_dt_none():
    assert db._iso_to_dt(None) is None


def test_iso_to_dt_valid_string():
    dt = db._iso_to_dt("2026-08-11T10:30:00+00:00")
    assert dt.year == 2026
    assert dt.hour == 10


def test_iso_to_dt_invalid_string_returns_none():
    assert db._iso_to_dt("not-a-date") is None


# --- transaction(): atomicity primitive -----------------------------------------------
#
# What this exists to prevent: save_state() and insert_check() each acquiring
# their own pooled connection and committing independently, so a failure on
# the second write would leave the first applied, permanently
# desynchronising monitors' lifetime counters from the checks table.


@pytest.mark.asyncio
async def test_transaction_raises_without_pool(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(RuntimeError, match="not initialised"):
        async with db.transaction():
            pass


@pytest.mark.asyncio
async def test_transaction_commits_on_clean_exit(monkeypatch):
    conn = FakeConn(FakeCursor())
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    async with db.transaction() as tx:
        assert tx is conn

    assert conn.calls == ["begin", "commit"]


@pytest.mark.asyncio
async def test_transaction_rolls_back_and_reraises_on_error(monkeypatch):
    conn = FakeConn(FakeCursor())
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    with pytest.raises(ValueError, match="boom"):
        async with db.transaction():
            raise ValueError("boom")

    assert conn.calls == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_cancellation(monkeypatch):
    """CancelledError inherits from BaseException, not Exception -- the
    handler catches BaseException specifically so a cancelled check task
    (shutdown cancels in-flight tasks) can't release a connection back to
    the pool with an open transaction on it."""
    conn = FakeConn(FakeCursor())
    monkeypatch.setattr(db, "_pool", FakePool(conn))

    with pytest.raises(asyncio.CancelledError):
        async with db.transaction():
            raise asyncio.CancelledError()

    assert conn.calls == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_writes_inside_transaction_use_the_shared_connection(monkeypatch):
    """save_state + insert_check with conn= must run on the transaction's
    connection, not acquire their own -- otherwise they'd commit
    separately and the transaction would be decorative."""
    tx_cursor = FakeCursor()
    tx_conn = FakeConn(tx_cursor)
    other_cursor = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(other_cursor)))

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
    assert len(tx_cursor.executed) == 5
    assert "INSERT INTO monitors" in tx_cursor.executed[0][0]
    assert "SELECT is_up FROM checks" in tx_cursor.executed[1][0]
    assert "INSERT INTO checks" in tx_cursor.executed[2][0]
    assert "INSERT INTO check_changes" in tx_cursor.executed[3][0]
    assert "INSERT INTO check_hourly" in tx_cursor.executed[4][0]
    # Nothing leaked onto a separately-acquired pool connection.
    assert other_cursor.executed == []


@pytest.mark.asyncio
async def test_writes_with_conn_do_not_need_a_pool(monkeypatch):
    """Joining a caller's transaction must not require _pool -- the
    connection was already acquired by transaction()."""
    tx_cursor = FakeCursor()
    monkeypatch.setattr(db, "_pool", None)

    await db.insert_check(
        monitor_name="m1", check_time="2026-09-11T00:00:00+00:00",
        status="200", is_up=True, conn=FakeConn(tx_cursor),
    )

    assert len(tx_cursor.executed) == 4


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
    cur = FakeCursor(fetchone_results=[None])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

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
    cur = FakeCursor(fetchone_results=[row])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

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
    cur = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))
    state = db.MonitorState(
        name="m1", type="http", total_checks=4, total_up=3, total_down=1,
    )

    await db.save_state(state)

    assert state.uptime_pct == 75.0
    assert len(cur.executed) == 1
    sql, params = cur.executed[0]
    assert "INSERT INTO monitors" in sql
    assert params[0] == "m1"
    assert params[9] == 75.0  # uptime_pct is the 10th positional param


@pytest.mark.asyncio
async def test_save_state_zero_checks_leaves_uptime_pct_unchanged(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))
    state = db.MonitorState(name="m1", type="http", total_checks=0, uptime_pct=42.0)

    await db.save_state(state)

    assert state.uptime_pct == 42.0


# --- insert_check ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_check_serializes_details_to_json(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.insert_check(
        monitor_name="m1", check_time="2026-08-11T00:00:00+00:00",
        status="up", is_up=True, response_ms=12.5, error=None,
        details={"verifying": True},
    )

    sql, params = cur.executed[1]
    assert "INSERT INTO checks" in sql
    assert params[0] == "m1"
    assert params[2] == "up"
    assert params[3] is True
    assert params[4] == 12.5
    assert params[6] == '{"verifying": true}'


@pytest.mark.asyncio
async def test_insert_check_no_details_is_null(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.insert_check(
        monitor_name="m1", check_time="2026-08-11T00:00:00+00:00",
        status="down", is_up=False, response_ms=None, error="timeout", details=None,
    )

    _, params = cur.executed[1]
    assert params[3] is False
    assert params[5] == "timeout"
    assert params[6] is None


# --- prune_old_checks -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_old_checks_deletes_when_positive(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.prune_old_checks("m1", 30)

    assert len(cur.executed) == 2
    sql, params = cur.executed[0]
    assert "DELETE FROM checks" in sql
    assert params[0] == "m1"
    # the hourly summary is pruned too, keeping the hour the cutoff is in
    sql, params = cur.executed[1]
    assert "DELETE FROM check_hourly" in sql
    assert params[0] == "m1"
    assert params[1].minute == 0 and params[1].second == 0


# --- get_all_monitor_names / delete_monitor -------------------------------------------


@pytest.mark.asyncio
async def test_get_all_monitor_names(monkeypatch):
    cur = FakeCursor(fetchall_results=[[("a",), ("b",)]])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    names = await db.get_all_monitor_names()

    assert names == ["a", "b"]


@pytest.mark.asyncio
async def test_delete_monitor(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.delete_monitor("m1")

    sql, params = cur.executed[0]
    assert "DELETE FROM monitors" in sql
    assert params == ("m1",)


# --- get_hourly_bars (bucketing/classification logic) ---------------------------------


@pytest.mark.asyncio
async def test_get_hourly_bars_classifies_status_and_fills_nodata(monkeypatch):
    now = datetime.now(timezone.utc)
    slot_zero = now.replace(minute=0, second=0, microsecond=0)
    hour1 = slot_zero - timedelta(hours=1)
    hour2 = slot_zero - timedelta(hours=2)  # left with no data on purpose

    rows = [(slot_zero, 5, 0), (hour1, 2, 3)]
    cur = FakeCursor(fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    bars = await db.get_hourly_bars("m1", hours=3)

    assert len(bars) == 3
    by_hour = {b["hour"]: b for b in bars}
    assert by_hour[slot_zero.strftime("%Y-%m-%dT%H:%M")]["status"] == "up"
    assert by_hour[hour1.strftime("%Y-%m-%dT%H:%M")]["status"] == "mixed"
    assert by_hour[hour2.strftime("%Y-%m-%dT%H:%M")]["status"] == "nodata"


@pytest.mark.asyncio
async def test_get_hourly_bars_all_down_bucket(monkeypatch):
    now = datetime.now(timezone.utc)
    slot_zero = now.replace(minute=0, second=0, microsecond=0)

    rows = [(slot_zero, 0, 4)]
    cur = FakeCursor(fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    bars = await db.get_hourly_bars("m1", hours=1)

    assert bars[0]["status"] == "down"


@pytest.mark.asyncio
async def test_get_hourly_bars_converts_to_display_timezone(monkeypatch):
    from zoneinfo import ZoneInfo
    cur = FakeCursor(fetchall_results=[[]])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    bars = await db.get_hourly_bars("m1", hours=1, display_tz=ZoneInfo("America/Chicago"))

    # hour_display/hour_display_end should differ from the raw UTC label
    # once a display timezone is actually applied.
    assert bars[0]["hour_display"] != bars[0]["hour"]
    assert bars[0]["hour_display_end"] != ""


@pytest.mark.asyncio
async def test_get_hourly_bar_statuses_for_many_monitors_in_one_query(monkeypatch):
    # The database sends one packed string per monitor: for each hour its age
    # (0 = current hour), then a has-up-checks flag and a has-down-checks flag.
    rows = [
        ("a", "010" "111"),      # a: current hour up only, an hour ago both
        ("b", "201"),            # b: two hours ago down only
        ("not-asked-for", "010"),
    ]
    cur = FakeCursor(fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_hourly_bar_statuses(["a", "b", "c"], hours=3)

    assert len(cur.executed) == 1
    # newest hour first
    assert result == {
        "a": ["up", "mixed", "nodata"],
        "b": ["nodata", "nodata", "down"],
        "c": ["nodata", "nodata", "nodata"],
    }


@pytest.mark.asyncio
async def test_get_uptime_pct_many_in_one_query(monkeypatch):
    cur = FakeCursor(fetchall_results=[[("a", 100, 95), ("b", 3, 2)]])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_uptime_pct_many(["a", "b", "c"], days=7, decimals=4)

    assert len(cur.executed) == 1
    assert result == {"a": 95.0, "b": round(2 / 3 * 100, 4), "c": 0.0}


@pytest.mark.asyncio
async def test_load_states_returns_a_state_for_every_monitor_asked_for(monkeypatch):
    row = {
        "name": "a", "type": "http", "status": "200",
        "last_check": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "last_change": None, "first_check": None,
        "total_checks": 4, "total_up": 3, "total_down": 1,
        "uptime_pct": 75.0, "last_alert_sent": None, "state_changed": 1,
    }
    cur = FakeCursor(fetchall_results=[[row]])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    states = await db.load_states([("a", "http"), ("new", "ping")])

    assert len(cur.executed) == 1
    assert states["a"].status == "200"
    assert states["a"].total_checks == 4
    assert states["a"].state_changed is True
    # no row yet: a fresh state, same as load_state()
    assert states["new"] == db.MonitorState(name="new", type="ping")


@pytest.mark.asyncio
async def test_get_last_response_ms_many_in_one_query(monkeypatch):
    cur = FakeCursor(fetchall_results=[[("a", 12.5), ("b", None)]])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_last_response_ms_many(["a", "b", "c"])

    assert len(cur.executed) == 1
    assert result == {"a": 12.5, "b": None, "c": None}


# --- check_hourly (the hourly summary the dashboard statistics read) ------------------
#
# insert_check() adds each check to its hour. These pin down what each kind
# of check adds, matching what the raw check rows hold:
# (up, down, stat_total, stat_up, stat_down, resp_sum, resp_count).


async def _hourly_values(monkeypatch, **check):
    cur = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))
    await db.insert_check(monitor_name="m1", check_time="2026-09-11T10:42:07+00:00", **check)
    sql, params = cur.executed[-1]
    assert "INSERT INTO check_hourly" in sql
    assert params[0] == "m1"
    assert params[1] == datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    return params[2:]


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
async def test_hourly_summary_counts_an_up_check_without_a_response_time(monkeypatch):
    values = await _hourly_values(monkeypatch, status="200", is_up=True)
    assert values == (1, 0, 1, 1, 0, 0.0, 0)


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


async def _changes_recorded(previous, **check):
    cur = FakeCursor(fetchone_results=[previous])
    db._pool = FakePool(FakeConn(cur))
    await db.insert_check(
        monitor_name="m1", check_time="2026-09-11T10:42:07+00:00", **check
    )
    recorded = [sql for sql, _ in cur.executed if "INSERT INTO check_changes" in sql]
    if recorded:
        params = next(p for sql, p in cur.executed if "INSERT INTO check_changes" in sql)
        assert params[0] == cur.lastrowid and params[1] == "m1"
    return bool(recorded)


@pytest.mark.asyncio
async def test_the_first_check_of_a_monitor_is_a_state_change():
    assert await _changes_recorded(None, status="200", is_up=True) is True


@pytest.mark.asyncio
async def test_a_check_with_the_same_health_as_the_previous_one_is_not():
    assert await _changes_recorded((1,), status="200", is_up=True) is False


@pytest.mark.asyncio
async def test_a_check_whose_health_differs_from_the_previous_one_is():
    assert await _changes_recorded((1,), status="503", is_up=False) is True
    assert await _changes_recorded((0,), status="200", is_up=True) is True


@pytest.mark.asyncio
async def test_a_verifying_check_is_always_a_state_change():
    assert await _changes_recorded(
        (0,), status="503", is_up=False, details={"verifying": True},
    ) is True


@pytest.mark.asyncio
async def test_backfill_builds_the_summary_once_on_an_existing_history(monkeypatch):
    cur = FakeCursor(fetchone_results=[(0,), (1,)])
    cur.rowcount = 42
    await db._backfill_check_hourly(cur)

    assert len(cur.executed) == 3
    assert "INSERT INTO check_hourly" in cur.executed[2][0]
    assert "GROUP BY monitor_name" in cur.executed[2][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fetchone_results", [
    [(1,)],            # the summary already has rows
    [(0,), (0,)],      # no checks to summarise
])
async def test_backfill_does_nothing_when_not_needed(fetchone_results):
    cur = FakeCursor(fetchone_results=fetchone_results)
    await db._backfill_check_hourly(cur)

    assert all("INSERT" not in sql for sql, _ in cur.executed)


# --- get_checks_paginated -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_checks_paginated_all_checks(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "up", "is_up": True, "response_ms": 12.5, "error": None,
        "details": None,
    }]
    cur = FakeCursor(fetchone_results=[{"cnt": 1}], fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_checks_paginated("m1", page=1, per_page=25)

    assert result["total"] == 1
    assert result["total_pages"] == 1
    assert result["checks"][0]["status"] == "up"
    # The frontend trusts this directly rather than re-deriving up/down
    # from the raw status code (which has no way to know about a
    # monitor's expected_status override) -- must actually be returned.
    assert result["checks"][0]["is_up"] is True
    assert "error" not in result["checks"][0]  # falsy error omitted
    assert "details" not in result["checks"][0]  # falsy details omitted


@pytest.mark.asyncio
async def test_get_checks_paginated_includes_error_and_details(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "down", "is_up": False, "response_ms": None,
        "error": "timeout", "details": '{"verifying": true}',
    }]
    cur = FakeCursor(fetchone_results=[{"cnt": 1}], fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_checks_paginated("m1", page=1, per_page=25)

    assert result["checks"][0]["error"] == "timeout"
    assert result["checks"][0]["details"] == {"verifying": True}
    # A check that never completed (this row is a timeout) stores
    # response_ms NULL, and that has to survive as None rather than
    # becoming 0.0: the check log renders None as an em dash, whereas
    # 0.0 would read as a real sub-millisecond measurement for a request
    # that never returned, indistinguishable from a genuine 0.0.
    assert result["checks"][0]["response_ms"] is None


@pytest.mark.asyncio
async def test_get_checks_paginated_expected_status_up_is_reflected(monkeypatch):
    """A monitor with expected_status configured can have is_up=True on a
    non-2xx status code (e.g. 401) -- confirms the field passed through
    is whatever was actually stored, not re-derived here from `status`."""
    rows = [{
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "401", "is_up": True, "response_ms": 5.0, "error": None,
        "details": None,
    }]
    cur = FakeCursor(fetchone_results=[{"cnt": 1}], fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_checks_paginated("m1", page=1, per_page=25)

    assert result["checks"][0]["status"] == "401"
    assert result["checks"][0]["is_up"] is True


@pytest.mark.asyncio
async def test_get_checks_paginated_is_up_coerced_from_raw_int(monkeypatch):
    """Regression test: aiomysql returns BOOLEAN/TINYINT(1) columns as a
    raw Python int (0/1), not bool. The JSON response -- and the frontend's
    strict `=== true` check against it -- both depend on this actually
    being a real bool/None, not whatever the driver handed back."""
    rows = [{
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "200", "is_up": 1, "response_ms": 5.0, "error": None,
        "details": None,
    }]
    cur = FakeCursor(fetchone_results=[{"cnt": 1}], fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_checks_paginated("m1", page=1, per_page=25)

    assert result["checks"][0]["is_up"] is True
    assert type(result["checks"][0]["is_up"]) is bool


@pytest.mark.asyncio
async def test_get_checks_paginated_state_changes_only(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "down", "is_up": False, "response_ms": 5.0, "error": None,
        "details": None,
    }]
    cur = FakeCursor(fetchone_results=[{"cnt": 1}], fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_checks_paginated(
        "m1", page=1, per_page=25, state_changes_only=True,
    )

    assert result["total"] == 1
    # The state-changes-only branch reads the recorded state changes,
    # distinct SQL from the plain branch above.
    assert "FROM check_changes" in cur.executed[0][0]
    assert "FROM check_changes" in cur.executed[1][0]


@pytest.mark.asyncio
async def test_get_checks_paginated_negative_page_clamped(monkeypatch):
    cur = FakeCursor(fetchone_results=[{"cnt": 0}], fetchall_results=[[]])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_checks_paginated("m1", page=-5, per_page=25)

    assert result["page"] == 1
    # OFFSET must never go negative
    _, params = cur.executed[1]
    assert params[-1] == 0


# --- get_all_checks_paginated ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_checks_paginated_includes_monitor_name(monkeypatch):
    rows = [{
        "monitor_name": "m1",
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "up", "is_up": True, "response_ms": 12.5, "error": None,
        "details": None,
    }]
    cur = FakeCursor(fetchone_results=[{"cnt": 1}], fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_all_checks_paginated(page=1, per_page=25)

    assert result["checks"][0]["monitor"] == "m1"


@pytest.mark.asyncio
async def test_get_all_checks_paginated_is_up_coerced_from_raw_int(monkeypatch):
    """Same aiomysql int-vs-bool regression as the per-monitor query,
    covered separately here since get_all_checks_paginated builds its own
    entry dict independently."""
    rows = [{
        "monitor_name": "m1",
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "200", "is_up": 0, "response_ms": 12.5, "error": None,
        "details": None,
    }]
    cur = FakeCursor(fetchone_results=[{"cnt": 1}], fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_all_checks_paginated(page=1, per_page=25)

    assert result["checks"][0]["is_up"] is False
    assert type(result["checks"][0]["is_up"]) is bool


@pytest.mark.asyncio
async def test_get_all_checks_paginated_state_changes_only(monkeypatch):
    cur = FakeCursor(fetchone_results=[{"cnt": 0}], fetchall_results=[[]])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.get_all_checks_paginated(page=1, per_page=25, state_changes_only=True)

    assert "FROM check_changes" in cur.executed[0][0]
    assert "FROM check_changes" in cur.executed[1][0]


@pytest.mark.asyncio
async def test_get_all_checks_paginated_includes_error_and_details(monkeypatch):
    rows = [{
        "monitor_name": "m1",
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "down", "is_up": False, "response_ms": None,
        "error": "refused", "details": '{"verifying": true}',
    }]
    cur = FakeCursor(fetchone_results=[{"cnt": 1}], fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_all_checks_paginated(page=1, per_page=25)

    assert result["checks"][0]["error"] == "refused"
    assert result["checks"][0]["details"] == {"verifying": True}


# --- get_recent_checks ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_checks(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "status": "down", "response_ms": 5.0, "error": "timeout",
        "details": '{"foo": "bar"}',
    }]
    cur = FakeCursor(fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

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
    cur = FakeCursor(fetchone_results=[stats_row, timeout_row, outage_row])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

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
    cur = FakeCursor(fetchone_results=[stats_row, timeout_row, outage_row])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

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
    cur = FakeCursor(fetchone_results=[stats_row, timeout_row, outage_row])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

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
    cur = FakeCursor(fetchone_results=[stats_row, timeout_row, outage_row])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    stats = await db.get_monitor_stats(
        "m1", days=30, uptime_decimals=4, latency_decimals=2,
    )

    assert stats["uptime_pct"] == round(2 / 3 * 100, 4)
    assert stats["avg_response"] == round(42.345, 2)


# --- get_aggregate_stats --------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_aggregate_stats_empty_monitor_list_short_circuits(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_aggregate_stats([], [("24h", 1)])

    assert result["uptime_pct"]["24h"] == 0.0
    assert cur.executed == []  # no query run at all


@pytest.mark.asyncio
async def test_get_aggregate_stats_computes_per_period(monkeypatch):
    # one row, four numbers per period: checks, up, response sum, response count
    row = (10, 9, 500.0, 10, 0, 0, 0, 0)
    cur = FakeCursor(fetchone_results=[row])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_aggregate_stats(
        ["m1", "m2"], [("24h", 1), ("7d", 7)],
    )

    assert result["uptime_pct"]["24h"] == 90.0
    assert result["avg_latency"]["24h"] == 50.0
    assert result["uptime_pct"]["7d"] == 0.0
    assert result["avg_latency"]["7d"] == 0.0


@pytest.mark.asyncio
async def test_get_aggregate_stats_respects_decimals_params(monkeypatch):
    row = (3, 1, 42.345, 1)
    cur = FakeCursor(fetchone_results=[row])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    result = await db.get_aggregate_stats(
        ["m1"], [("24h", 1)], uptime_decimals=4, latency_decimals=2,
    )

    assert result["uptime_pct"]["24h"] == round(1 / 3 * 100, 4)
    assert result["avg_latency"]["24h"] == round(42.345, 2)


# --- get_response_time_series ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_response_time_series(monkeypatch):
    rows = [{
        "check_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "response_ms": 42.5,
    }]
    cur = FakeCursor(fetchall_results=[rows])
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    series = await db.get_response_time_series("m1", hours=24)

    assert series == [{"time": "2026-08-11T00:00:00+00:00", "response_ms": 42.5}]


# --- Pool-required guards on the remaining functions ----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("coro_factory", [
    lambda: db.save_state(db.MonitorState(name="m1", type="http")),
    lambda: db.get_checks_paginated("m1"),
    lambda: db.get_all_checks_paginated(),
    lambda: db.get_recent_checks("m1"),
    lambda: db.get_uptime_pct_many(["m1"], 7),
    lambda: db.load_states([("m1", "http")]),
    lambda: db.get_last_response_ms_many(["m1"]),
    lambda: db.get_hourly_bar_statuses(["m1"], 24),
    lambda: db.prune_old_checks("m1", 30),
    lambda: db.get_all_monitor_names(),
    lambda: db.delete_monitor("m1"),
    lambda: db.get_monitor_stats("m1"),
    lambda: db.get_aggregate_stats(["m1"], [("24h", 1)]),
    lambda: db.get_response_time_series("m1"),
    lambda: db.get_hourly_bars("m1"),
])
async def test_functions_require_initialised_pool(monkeypatch, coro_factory):
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(RuntimeError, match="not initialised"):
        await coro_factory()


# --- init_schema: duplicate-column migration handling ---------------------------------


class _SchemaFakeCursor:
    """Fake cursor for init_schema()'s migration-specific error handling:
    every execute() succeeds except ALTER TABLE migration statements,
    which raise whatever error the test configures. rowcount mimics a
    real DB-API cursor after an UPDATE -- 0 here since these tests don't
    seed any check rows for the is_up backfill to actually match.

    status_len is what the information_schema lookup for
    checks.status's CHARACTER_MAXIMUM_LENGTH reports back. It defaults
    to the current schema width (20), i.e. "this DB is already migrated",
    so the widening ALTER is skipped and these tests keep exercising only
    the error handling they were written for. Pass 10 to simulate a DB
    created before the VARCHAR(20) schema and drive the widening path.
    """

    def __init__(self, alter_error=None, status_len=20):
        self._alter_error = alter_error
        self._status_len = status_len
        self.rowcount = 0
        self.executed: list[str] = []

    async def execute(self, sql):
        self.executed.append(sql)
        if "ALTER TABLE" in sql and self._alter_error is not None:
            raise self._alter_error

    async def fetchone(self):
        """Only ever called for the checks.status length lookup, which is
        the single SELECT init_schema() issues."""
        return (self._status_len,)


def _assert_schema_work_happened(cur):
    """Assert init_schema() actually created the schema and ran its
    migrations.

    Several of the tests below only assert "must not raise", which a function
    that does nothing at all would satisfy, so this checks the work actually
    happened. Replacing init_schema()'s body makes every test using this
    helper fail.
    """
    sql = " ".join(cur.executed)
    assert "CREATE TABLE IF NOT EXISTS monitors" in sql
    assert "CREATE TABLE IF NOT EXISTS checks" in sql
    # Both column migrations are attempted on every startup.
    assert "ADD COLUMN state_changed" in sql
    assert "ADD COLUMN is_up" in sql
    # And the status-width lookup that guards the widening ALTER.
    assert "CHARACTER_MAXIMUM_LENGTH" in sql


@pytest.mark.asyncio
async def test_init_schema_raises_without_pool(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(RuntimeError, match="not initialised"):
        await db.init_schema()


@pytest.mark.asyncio
async def test_init_schema_swallows_duplicate_column_error(monkeypatch):
    """Error 1060 (Duplicate column name) is the expected case on every
    startup after the first -- must not raise."""
    import aiomysql
    err = aiomysql.Error(1060, "Duplicate column name 'state_changed'")
    cur = _SchemaFakeCursor(alter_error=err)
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.init_schema()  # must not raise

    # Swallowing 1060 must not mean skipping the rest of the work.
    _assert_schema_work_happened(cur)


@pytest.mark.asyncio
async def test_init_schema_logs_successful_migration(monkeypatch):
    """A fresh DB (first-ever startup) has no state_changed column yet --
    the ALTER TABLE succeeds outright rather than hitting the 1060
    duplicate-column case."""
    cur = _SchemaFakeCursor(alter_error=None)
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.init_schema()  # must not raise

    _assert_schema_work_happened(cur)


@pytest.mark.asyncio
async def test_init_schema_widens_narrow_status_column(monkeypatch):
    """A DB created before the VARCHAR(20) schema reports status length 10,
    which is too narrow for the 11-character 'maintenance' marker -- the
    widening ALTER must run."""
    cur = _SchemaFakeCursor(alter_error=None, status_len=10)
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.init_schema()

    widen = [
        s for s in cur.executed
        if "MODIFY COLUMN status" in s and "VARCHAR(20)" in s
    ]
    assert len(widen) == 1, "narrow status column should be widened exactly once"


@pytest.mark.asyncio
async def test_init_schema_skips_widening_already_wide_status_column(monkeypatch):
    """Already at VARCHAR(20): the ALTER must be skipped, so a large checks
    table isn't rewritten on every single startup."""
    cur = _SchemaFakeCursor(alter_error=None, status_len=20)
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    await db.init_schema()

    # Assert the work happened as well as that the ALTER didn't -- on its
    # own, "no MODIFY COLUMN was issued" is also true of a function that
    # issues nothing at all.
    _assert_schema_work_happened(cur)
    assert not [s for s in cur.executed if "MODIFY COLUMN status" in s]


@pytest.mark.asyncio
async def test_init_schema_reraises_unexpected_migration_error(monkeypatch):
    """Any error other than 1060 (e.g. a permissions problem) must fail
    loudly rather than silently leave the schema possibly inconsistent."""
    import aiomysql
    err = aiomysql.Error(1045, "Access denied")
    cur = _SchemaFakeCursor(alter_error=err)
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(cur)))

    with pytest.raises(aiomysql.Error):
        await db.init_schema()


# --- Connection pool sizing (DB_POOL_MIN_SIZE / DB_POOL_MAX_SIZE) ---------------------
#
# The defaults are minsize=1/maxsize=5. The ceiling matters more than it
# looks: db.transaction() holds a connection for its whole duration, so it
# bounds concurrent check writes.


def test_pool_sizes_defaults_match_the_previous_hardcoded_values(monkeypatch):
    """With neither variable set, the pool is 1 minimum and 5 maximum."""
    monkeypatch.delenv("DB_POOL_MIN_SIZE", raising=False)
    monkeypatch.delenv("DB_POOL_MAX_SIZE", raising=False)
    assert pool_sizes() == (1, 5)


def test_pool_sizes_reads_both_env_vars(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "3")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "40")
    assert pool_sizes() == (3, 40)


def test_pool_sizes_treats_empty_string_as_unset(monkeypatch):
    """docker-compose passes an empty value for a variable that's declared
    but not set in .env, which must mean "use the default" rather than
    blowing up at startup."""
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "   ")
    assert pool_sizes() == (1, 5)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_pool_sizes_rejects_below_one(monkeypatch, value):
    """A pool that can hand out no connections deadlocks the monitoring
    loop on its first check. aiomysql would also read maxsize=0 as
    "unlimited" while asyncpg rejects it, so this is validated here
    rather than left to whichever driver is active."""
    monkeypatch.delenv("DB_POOL_MIN_SIZE", raising=False)
    monkeypatch.setenv("DB_POOL_MAX_SIZE", value)
    with pytest.raises(ValueError, match="at least 1"):
        pool_sizes()


def test_pool_sizes_rejects_non_integer(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "lots")
    with pytest.raises(ValueError, match="must be an integer"):
        pool_sizes()


def test_pool_sizes_rejects_min_above_max(monkeypatch):
    """Both drivers would either error obscurely or silently clamp."""
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "9")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "3")
    with pytest.raises(ValueError, match="cannot exceed"):
        pool_sizes()


def test_both_backends_use_pool_sizes_rather_than_literals():
    """Guards against one backend being updated and the other left with a
    hardcoded literal -- the two-file architecture makes that easy to do.
    """
    import inspect

    from src import db_mariadb, db_postgres

    for module, (min_kw, max_kw) in (
        (db_mariadb, ("minsize", "maxsize")),
        (db_postgres, ("min_size", "max_size")),
    ):
        src = inspect.getsource(module.init_pool)
        assert "pool_sizes()" in src, f"{module.__name__} ignores pool_sizes()"
        assert f"{min_kw}=min_size" in src, f"{module.__name__}: {min_kw}"
        assert f"{max_kw}=max_size" in src, f"{module.__name__}: {max_kw}"


# --- Connection settings --------------------------------------------------------------
#
# DB_HOST, DB_PORT, DB_NAME, DB_USER, and DB_PASSWORD. The software assumes nothing about where the database is or how to log
# in. Every one of these must be declared; compose supplies them.

_CONNECTION_ENV = {
    "DB_HOST": "db.example.com",
    "DB_PORT": "3306",
    "DB_NAME": "appdb",
    "DB_USER": "appuser",
    "DB_PASSWORD": "s3cret pass",
}


def _set_connection_env(monkeypatch, **overrides):
    for name, value in {**_CONNECTION_ENV, **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_db_connection_settings_reads_all_five(monkeypatch):
    _set_connection_env(monkeypatch)
    assert db_connection_settings() == (
        "db.example.com", 3306, "appdb", "appuser", "s3cret pass",
    )


@pytest.mark.parametrize("name", list(_CONNECTION_ENV))
def test_db_connection_settings_has_no_fallback_for_any_setting(
    monkeypatch, name,
):
    _set_connection_env(monkeypatch, **{name: None})
    with pytest.raises(ValueError, match=name):
        db_connection_settings()


@pytest.mark.parametrize("name", list(_CONNECTION_ENV))
def test_db_connection_settings_treats_empty_as_missing(monkeypatch, name):
    """A blank line in .env arrives as an empty string, which must stop
    startup just like an unset variable."""
    _set_connection_env(monkeypatch, **{name: ""})
    with pytest.raises(ValueError, match=name):
        db_connection_settings()


def test_db_connection_settings_names_every_missing_setting(monkeypatch):
    _set_connection_env(monkeypatch, DB_HOST=None, DB_PASSWORD="")
    with pytest.raises(ValueError) as excinfo:
        db_connection_settings()
    assert "DB_HOST" in str(excinfo.value)
    assert "DB_PASSWORD" in str(excinfo.value)
    assert "DB_USER" not in str(excinfo.value)


def test_db_connection_settings_rejects_non_integer_port(monkeypatch):
    _set_connection_env(monkeypatch, DB_PORT="mariadb")
    with pytest.raises(ValueError, match="DB_PORT must be an integer"):
        db_connection_settings()


def test_both_backends_use_db_connection_settings_not_literals():
    """Same two-file guard as the pool sizing one above: one backend
    must not quietly keep its own fallbacks."""
    import inspect

    from src import db_mariadb, db_postgres

    for module in (db_mariadb, db_postgres):
        src = inspect.getsource(module.init_pool)
        assert "db_connection_settings()" in src, module.__name__
        assert "os.environ" not in inspect.getsource(module), module.__name__
