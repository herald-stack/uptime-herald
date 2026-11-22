# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_main.py - Tests for the monitoring loop in src/main.py.

main.py talks to a real database via src/db.py, which isn't available in the
test environment. Rather than mocking every individual db.* call with
AsyncMock (which can't catch a call-order bug), these tests swap in FakeDB: a
small in-memory stand-in that enforces the same foreign-key relationship the
real schema does (checks.monitor_name references monitors.name). That makes
the regression tests below meaningful: if the load_state()+save_state() calls
in _insert_inactive_log / _insert_maintenance_log were ever removed,
FakeDB.insert_check() would raise exactly like the real database does.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from src import main
from src.monitors.base import CheckResult
from src.state import MonitorState

# --- Test doubles ---------------------------------------------------------------------


class FakeDB:
    """In-memory stand-in for src.db.

    Mirrors just the functions main.py calls. insert_check() raises if the
    monitor has no row yet, mimicking the real checks->monitors FK.

    transaction() is a real rollback, not a no-op: it snapshots both tables on
    entry and restores them if the block raises, like a real BEGIN/ROLLBACK.
    So if the db.transaction() wrapping in _record_check (or either retry path,
    or either log writer) were ever removed, the tests below would see the
    state write survive a failed insert, as the real database would.
    """

    def __init__(self):
        self.monitors: dict[str, MonitorState] = {}
        self.checks: list[dict] = []
        self.pruned: list[tuple] = []
        self.deleted: list[str] = []
        self.transactions = 0  # how many were opened, for assertions

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        monitors_snapshot = {k: replace(v) for k, v in self.monitors.items()}
        checks_snapshot = list(self.checks)
        try:
            yield object()  # opaque connection handle, like the real ones
        except BaseException:
            self.monitors = monitors_snapshot
            self.checks = checks_snapshot
            raise

    async def load_state(self, name, monitor_type):
        if name in self.monitors:
            return replace(self.monitors[name])
        return MonitorState(name=name, type=monitor_type)

    async def save_state(self, state, conn=None):
        self.monitors[state.name] = replace(state)

    async def insert_check(
        self, monitor_name, check_time, status, is_up=None,
        response_ms=None, error=None, details=None, conn=None,
    ):
        if monitor_name not in self.monitors:
            raise RuntimeError(
                f"IntegrityError: FOREIGN KEY constraint fails — no "
                f"monitors row for '{monitor_name}' (mirrors "
                f"checks.monitor_name FK in the real schema)"
            )
        if status is not None and len(status) > 20:
            # Mirrors checks.status VARCHAR(20). Both real backends reject
            # an over-length value (MariaDB 1406 under strict mode,
            # Postgres 22001) -- which is how the atomicity test forces
            # insert_check to fail after save_state has already run, so the
            # fake has to model it.
            raise RuntimeError(
                f"DataError: value too long for checks.status "
                f"({len(status)} > 20) (mirrors VARCHAR(20) in the real "
                f"schema)"
            )
        self.checks.append({
            "monitor_name": monitor_name,
            "check_time": check_time,
            "status": status,
            "is_up": is_up,
            "response_ms": response_ms,
            "error": error,
            "details": details,
        })

    async def prune_old_checks(self, monitor_name, retention_days):
        self.pruned.append((monitor_name, retention_days))

    async def get_all_monitor_names(self):
        return list(self.monitors.keys())

    async def delete_monitor(self, name):
        self.deleted.append(name)
        self.monitors.pop(name, None)


class _FakeLoop:
    """Stand-in for the object asyncio.get_event_loop() returns, exposing
    only the .time() method _cleanup_removed_monitors actually calls."""

    def __init__(self, now: float = 0.0):
        self._now = now

    def time(self):
        return self._now


def _base_config(**global_overrides) -> dict:
    global_cfg = {
        "check_interval_minutes": 5,
        "stagger_seconds": 30,
        "retention_days": 90,
        "alert_cooldown_minutes": 5,
        "alert_retries": 2,
        "retry_interval_minutes": 0,  # keep test retry sequences instant
        "retry_on_up": True,
        "cleanup_removed_monitors": True,
        "cleanup_grace_minutes": 20,
    }
    global_cfg.update(global_overrides)
    return {
        "global": global_cfg,
        "alerts": {"email": {"enabled": False}, "push_alerts": []},
        "monitors": [],
        "tags": [],
        "maintenance": [],
    }


@pytest.fixture(autouse=True)
def _reset_main_module_state():
    """main.py tracks in-flight monitor state in module-level dicts that
    persist for the life of the process. Clear them before and after every
    test so tests can't leak state into each other."""
    for d in (main._retrying, main._busy, main._last_check_time,
              main._removal_timers, main._in_maintenance):
        d.clear()
    yield
    for d in (main._retrying, main._busy, main._last_check_time,
              main._removal_timers, main._in_maintenance):
        d.clear()


# --- Foreign-key regression tests -----------------------------------------------------


@pytest.mark.asyncio
async def test_fakedb_models_the_fk_constraint():
    """Sanity check that FakeDB enforces the foreign key -- otherwise the two
    regression tests below wouldn't be proving anything.
    Inserting a check for a monitor with no `monitors` row must raise,
    exactly like the real schema's foreign key would."""
    fake = FakeDB()
    with pytest.raises(RuntimeError, match="FOREIGN KEY"):
        await fake.insert_check(
            monitor_name="ghost",
            check_time="2026-08-11T00:00:00+00:00",
            status="inactive",
        )


@pytest.mark.asyncio
async def test_fakedb_transaction_actually_rolls_back():
    """Sanity check for the atomicity regression tests below, in the same
    spirit as test_fakedb_models_the_fk_constraint: if FakeDB.transaction()
    didn't really roll back, those tests would pass vacuously."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(name="m1", type="http", total_checks=1)

    with pytest.raises(ValueError):
        async with fake.transaction():
            fake.monitors["m1"] = MonitorState(
                name="m1", type="http", total_checks=99,
            )
            raise ValueError("boom")

    assert fake.monitors["m1"].total_checks == 1


@pytest.mark.asyncio
async def test_record_check_state_write_rolls_back_if_insert_fails(monkeypatch):
    """_record_check() writes the monitor's lifetime counters (a `monitors`
    write) and inserts the check row (a `checks` write) inside one
    db.transaction(). Without that, each would commit independently, so a
    failure on the insert would leave the counter bump applied, with
    monitors.total_checks drifting above COUNT(*) on checks and nothing in
    the app to reconcile them.

    Forces the insert to fail the way a real database does -- a status
    longer than checks.status VARCHAR(20), which both real backends
    reject -- so save_state has already run when the insert blows up.
    Then asserts the counter bump did not survive.
    """
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    # An existing monitor with its counters in agreement: 1 check recorded.
    fake.monitors["m1"] = MonitorState(
        name="m1", type="http", status="200", total_checks=1, total_up=1,
    )
    fake.checks.append({"monitor_name": "m1", "status": "200"})

    state = await fake.load_state("m1", "http")
    result = CheckResult(status="x" * 25, response_ms=1.0)  # rejected on insert

    with pytest.raises(RuntimeError, match="too long"):
        await main._record_check(
            {"name": "m1", "type": "http"}, state, result,
            "2026-09-11T00:00:00+00:00",
        )

    assert fake.transactions == 1, "the writes must run inside a transaction"
    # The counter bump must not have stuck: both tables still agree at 1.
    assert fake.monitors["m1"].total_checks == 1, (
        f"save_state survived a failed insert_check -- the two writes are "
        f"not atomic (total_checks={fake.monitors['m1'].total_checks} vs "
        f"{len(fake.checks)} check rows)"
    )
    assert len(fake.checks) == 1


@pytest.mark.asyncio
async def test_all_check_write_paths_use_a_transaction(monkeypatch):
    """Every place that pairs a state write with a check-row insert must
    go through db.transaction(), not just _record_check. Guards against a
    new write path being added later without atomicity."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    await main._insert_inactive_log(
        "m-inactive", "http", "2026-09-11T00:00:00+00:00", retention=30,
    )
    assert fake.transactions == 1

    await main._insert_maintenance_log(
        "m-maint", "ping", "2026-09-11T00:00:00+00:00", "W", retention=30,
    )
    assert fake.transactions == 2


@pytest.mark.asyncio
async def test_insert_inactive_log_new_monitor_no_prior_state(monkeypatch):
    """_insert_inactive_log must create the `monitors` row before inserting
    for a monitor that has never had one: calling db.insert_check() directly
    would raise an IntegrityError against checks' FK to monitors(name). It
    does that with load_state()+save_state() before the insert; if those two
    calls were ever removed, FakeDB.insert_check() would raise here and this
    test would fail."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    await main._insert_inactive_log(
        "brand-new-monitor", "http", "2026-08-11T00:00:00+00:00", retention=30,
    )

    assert "brand-new-monitor" in fake.monitors
    assert len(fake.checks) == 1
    assert fake.checks[0]["monitor_name"] == "brand-new-monitor"
    assert fake.checks[0]["status"] == "inactive"


@pytest.mark.asyncio
async def test_insert_maintenance_log_new_monitor_no_prior_state(monkeypatch):
    """Same regression as above, for _insert_maintenance_log."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    await main._insert_maintenance_log(
        "another-new-monitor", "ping", "2026-08-11T00:00:00+00:00",
        "Weekly Window", retention=30,
    )

    assert "another-new-monitor" in fake.monitors
    assert len(fake.checks) == 1
    assert fake.checks[0]["monitor_name"] == "another-new-monitor"
    assert fake.checks[0]["status"] == "maintenance"
    assert fake.checks[0]["details"] == {"maintenance": "Weekly Window"}


# --- run_check ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_check_first_check_no_state_change_no_alert(monkeypatch):
    """A monitor's very first check (old_status='unknown') should never be
    treated as a state change, even if the result is "down"."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="up", response_ms=10.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {"name": "m1", "type": "tcp", "host": "h", "port": 80}
    config = _base_config()

    await main.run_check(monitor_config, config)

    assert fake.monitors["m1"].status == "up"
    assert len(fake.checks) == 1
    assert fake.checks[0]["status"] == "up"
    assert "m1" not in main._retrying
    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_check_still_down_asks_for_a_repeat_alert(monkeypatch):
    """A monitor that was down and is still down is handed to
    dispatch_alerts as a repeat alert, with the cooldown in seconds."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(name="m1", type="tcp", status="down")
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="down", response_ms=10.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    await main.run_check(
        {"name": "m1", "type": "tcp", "host": "h", "port": 80}, _base_config(),
    )

    dispatch_mock.assert_called_once()
    assert dispatch_mock.call_args.kwargs["repeat_after"] == 300


@pytest.mark.asyncio
async def test_run_check_repeat_alert_uses_the_monitors_own_cooldown(monkeypatch):
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(name="m1", type="tcp", status="down")
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="down", response_ms=10.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    await main.run_check(
        {"name": "m1", "type": "tcp", "host": "h", "port": 80,
         "alert_cooldown_minutes": 60},
        _base_config(),
    )

    assert dispatch_mock.call_args.kwargs["repeat_after"] == 3600


@pytest.mark.asyncio
async def test_run_check_first_ever_check_down_sends_no_repeat_alert(monkeypatch):
    """A monitor with no history (old_status 'unknown') has not been down
    yet, so its first check being down is not a repeat of anything."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="down", response_ms=10.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    await main.run_check(
        {"name": "m1", "type": "tcp", "host": "h", "port": 80}, _base_config(),
    )

    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_check_still_up_sends_no_alert(monkeypatch):
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(name="m1", type="tcp", status="up")
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="up", response_ms=10.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    await main.run_check(
        {"name": "m1", "type": "tcp", "host": "h", "port": 80}, _base_config(),
    )

    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_check_expected_status_classifies_non_2xx_as_up(monkeypatch):
    """A monitor with expected_status=401 configured should record a 401
    response as up (state.status="up" in the health sense, is_up stored
    True on the check row) -- not down, which is what the default 2xx
    rule would otherwise say about a 401."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="401", response_ms=10.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"http": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {
        "name": "m1", "type": "http", "url": "http://x.com",
        "expected_status": 401,
    }
    config = _base_config()

    await main.run_check(monitor_config, config)

    assert fake.checks[0]["status"] == "401"
    assert fake.checks[0]["is_up"] is True
    assert fake.monitors["m1"].total_up == 1
    assert fake.monitors["m1"].total_down == 0


@pytest.mark.asyncio
async def test_run_check_expected_status_classifies_2xx_as_down(monkeypatch):
    """The flip side -- expected_status replaces the 2xx rule entirely,
    it doesn't add to it. A real 200 from a monitor configured to expect
    401 is down, since that's not the response that means "healthy" for
    this specific endpoint."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="200", response_ms=10.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"http": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {
        "name": "m1", "type": "http", "url": "http://x.com",
        "expected_status": 401,
    }
    config = _base_config()

    await main.run_check(monitor_config, config)

    assert fake.checks[0]["status"] == "200"
    assert fake.checks[0]["is_up"] is False
    assert fake.monitors["m1"].total_up == 0
    assert fake.monitors["m1"].total_down == 1


@pytest.mark.asyncio
async def test_run_check_alert_retries_zero_dispatches_immediately(monkeypatch):
    """With alert_retries=0, a state change should dispatch immediately
    with no retry sequence."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(
        name="m1", type="tcp", status="up",
        total_checks=5, total_up=5, total_down=0,
    )
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(
        return_value=CheckResult(status="down", response_ms=5.0, error="refused")
    )
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {
        "name": "m1", "type": "tcp", "host": "h", "port": 80, "alert_retries": 0,
    }
    config = _base_config()

    await main.run_check(monitor_config, config)

    assert "m1" not in main._retrying
    assert fake.monitors["m1"].status == "down"
    check_fn.assert_awaited_once()
    dispatch_mock.assert_called_once()


@pytest.mark.asyncio
async def test_run_check_state_change_confirmed_after_retries_dispatches(monkeypatch):
    """A state change should start a retry sequence; once every retry
    confirms the new status, the monitor's confirmed status updates and
    an alert dispatches."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(
        name="m1", type="tcp", status="up",
        total_checks=5, total_up=5, total_down=0,
        first_check="2026-08-01T00:00:00+00:00",
    )
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(side_effect=[
        CheckResult(status="down", response_ms=5.0, error="refused"),  # initial
        CheckResult(status="down", response_ms=5.0, error="refused"),  # retry
    ])
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {
        "name": "m1", "type": "tcp", "host": "h", "port": 80,
        "alert_retries": 1, "retry_interval_minutes": 0,
    }
    config = _base_config()

    await main.run_check(monitor_config, config)

    assert "m1" in main._retrying
    await main._retrying["m1"]

    assert fake.monitors["m1"].status == "down"
    assert len(fake.checks) == 2
    assert fake.checks[0]["details"] == {"verifying": True}
    assert fake.checks[1]["details"]["passed"] is True
    dispatch_mock.assert_called_once()
    kwargs = dispatch_mock.call_args.kwargs
    assert kwargs["new_status"] == "down"


@pytest.mark.asyncio
async def test_run_check_retry_false_alarm_aborts_no_alert(monkeypatch):
    """If a retry check returns the OLD status, the sequence should abort
    as a false alarm: no confirmed state change, no alert dispatched."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(
        name="m1", type="tcp", status="up",
        total_checks=5, total_up=5, total_down=0,
    )
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(side_effect=[
        CheckResult(status="down", response_ms=5.0, error="refused"),  # initial
        CheckResult(status="up", response_ms=5.0),  # retry: back to old status
    ])
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {
        "name": "m1", "type": "tcp", "host": "h", "port": 80,
        "alert_retries": 1, "retry_interval_minutes": 0,
    }
    config = _base_config()

    await main.run_check(monitor_config, config)
    await main._retrying["m1"]

    # Confirmed status never got overwritten -- it's still "up".
    assert fake.monitors["m1"].status == "up"
    assert len(fake.checks) == 2
    assert fake.checks[1]["details"]["aborted"] is True
    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_check_retry_on_up_false_skips_retry_on_recovery(monkeypatch):
    """When retry_on_up is False, a down->up recovery should dispatch
    immediately without starting a retry sequence."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(
        name="m1", type="tcp", status="down",
        total_checks=3, total_up=0, total_down=3,
    )
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="up", response_ms=5.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {
        "name": "m1", "type": "tcp", "host": "h", "port": 80,
        "alert_retries": 2, "retry_on_up": False,
    }
    config = _base_config()

    await main.run_check(monitor_config, config)

    assert "m1" not in main._retrying
    assert fake.monitors["m1"].status == "up"
    check_fn.assert_awaited_once()
    dispatch_mock.assert_called_once()


@pytest.mark.asyncio
async def test_run_check_force_verify_starts_retry_without_state_change(monkeypatch):
    """force_verify=True should start a retry sequence even when the check
    result matches the monitor's already-confirmed status (state_changed
    would otherwise be False) -- used on maintenance-window exit."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(
        name="m1", type="tcp", status="up",
        total_checks=5, total_up=5, total_down=0,
    )
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(side_effect=[
        CheckResult(status="up", response_ms=5.0),  # initial -- no real change
        CheckResult(status="up", response_ms=5.0),  # retry
    ])
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {
        "name": "m1", "type": "tcp", "host": "h", "port": 80,
        "alert_retries": 1, "retry_interval_minutes": 0,
    }
    config = _base_config()

    await main.run_check(monitor_config, config, force_verify=True)

    assert "m1" in main._retrying
    await main._retrying["m1"]

    # The retry sequence genuinely ran (proving force_verify overrode
    # state_changed=False) -- both checks happened.
    assert check_fn.await_count == 2
    assert len(fake.checks) == 2


# --- _run_retry_sequence (direct) -----------------------------------------------------


@pytest.mark.asyncio
async def test_run_retry_sequence_zero_retries_is_noop(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock()
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {"name": "m1", "type": "tcp", "host": "h", "port": 80}
    config = _base_config()

    await main._run_retry_sequence(
        monitor_config, config, old_status="up",
        expected_new_status="down", retries=0, retry_interval_minutes=0,
    )

    check_fn.assert_not_called()
    dispatch_mock.assert_not_called()
    assert fake.checks == []


@pytest.mark.asyncio
async def test_run_retry_sequence_multiple_retries_marks_only_last_as_passed(
    monkeypatch,
):
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(
        name="m1", type="tcp", status="up",
        total_checks=5, total_up=5, total_down=0,
    )
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(side_effect=[
        CheckResult(status="down", response_ms=5.0, error="e1"),
        CheckResult(status="down", response_ms=5.0, error="e2"),
    ])
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {"name": "m1", "type": "tcp", "host": "h", "port": 80}
    config = _base_config()

    await main._run_retry_sequence(
        monitor_config, config, old_status="up",
        expected_new_status="down", retries=2, retry_interval_minutes=0,
    )

    assert len(fake.checks) == 2
    assert "passed" not in fake.checks[0].get("details", {})
    assert fake.checks[1]["details"]["passed"] is True
    assert fake.monitors["m1"].status == "down"
    dispatch_mock.assert_called_once()
    kwargs = dispatch_mock.call_args.kwargs
    assert kwargs["new_status"] == "down"


# --- _cleanup_removed_monitors --------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_removed_monitors_starts_timer_for_orphaned(monkeypatch):
    fake = FakeDB()
    fake.monitors["gone"] = MonitorState(name="gone", type="http", status="up")
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main.asyncio, "get_event_loop", lambda: _FakeLoop(now=100.0))

    config = _base_config()
    config["monitors"] = []  # "gone" no longer configured

    await main._cleanup_removed_monitors(config)

    assert main._removal_timers.get("gone") == 100.0
    assert fake.deleted == []


@pytest.mark.asyncio
async def test_cleanup_removed_monitors_cancels_timer_on_return(monkeypatch):
    fake = FakeDB()
    fake.monitors["back"] = MonitorState(name="back", type="http", status="up")
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main.asyncio, "get_event_loop", lambda: _FakeLoop(now=100.0))
    main._removal_timers["back"] = 50.0

    config = _base_config()
    config["monitors"] = [{"name": "back", "type": "http", "url": "http://x"}]

    await main._cleanup_removed_monitors(config)

    assert "back" not in main._removal_timers
    assert fake.deleted == []


@pytest.mark.asyncio
async def test_cleanup_removed_monitors_deletes_after_grace_period(monkeypatch):
    fake = FakeDB()
    fake.monitors["expired"] = MonitorState(name="expired", type="http", status="up")
    monkeypatch.setattr(main, "db", fake)
    grace_minutes = 20
    monkeypatch.setattr(
        main.asyncio, "get_event_loop",
        lambda: _FakeLoop(now=grace_minutes * 60),
    )
    main._removal_timers["expired"] = 0.0  # timer started at t=0

    config = _base_config(cleanup_grace_minutes=grace_minutes)
    config["monitors"] = []

    await main._cleanup_removed_monitors(config)

    assert fake.deleted == ["expired"]
    assert "expired" not in main._removal_timers


@pytest.mark.asyncio
async def test_cleanup_removed_monitors_not_yet_expired(monkeypatch):
    """A monitor still within its grace period should not be deleted."""
    fake = FakeDB()
    fake.monitors["waiting"] = MonitorState(name="waiting", type="http", status="up")
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main.asyncio, "get_event_loop", lambda: _FakeLoop(now=100.0))
    main._removal_timers["waiting"] = 50.0  # only 50s elapsed, grace is 20min

    config = _base_config(cleanup_grace_minutes=20)
    config["monitors"] = []

    await main._cleanup_removed_monitors(config)

    assert fake.deleted == []
    assert "waiting" in main._removal_timers


@pytest.mark.asyncio
async def test_cleanup_removed_monitors_disabled_clears_all_timers(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    main._removal_timers["a"] = 0.0
    main._removal_timers["b"] = 10.0

    config = _base_config(cleanup_removed_monitors=False)

    await main._cleanup_removed_monitors(config)

    assert main._removal_timers == {}
    assert fake.deleted == []


# --- _build_kwargs --------------------------------------------------------------------


def test_build_kwargs_http():
    monitor_config = {
        "name": "m1", "type": "http", "url": "https://example.com",
        "verify_ssl": False, "timeout": 5,
    }
    config = _base_config()
    config["global"]["contact_email"] = "ops@example.com"

    kwargs = main._build_kwargs(monitor_config, config)

    assert kwargs == {
        "url": "https://example.com", "verify_ssl": False,
        "contact_email": "ops@example.com", "timeout": 5,
    }


def test_build_kwargs_http_passes_the_named_credential():
    monitor_config = {
        "name": "m1", "type": "http", "url": "https://example.com/admin",
        "credentials": "wiki-login",
    }
    config = _base_config()
    config["credentials"] = {"wiki-login": {"username": "monitor", "password": "pw"}}

    kwargs = main._build_kwargs(monitor_config, config)

    assert kwargs["auth"] == ("monitor", "pw")


def test_build_kwargs_http_without_credentials_has_no_auth():
    monitor_config = {"name": "m1", "type": "http", "url": "https://example.com/"}
    config = _base_config()
    config["credentials"] = {}

    assert "auth" not in main._build_kwargs(monitor_config, config)


def test_build_kwargs_ping():
    monitor_config = {"name": "m1", "type": "ping", "host": "1.2.3.4", "count": 3}
    config = _base_config()

    kwargs = main._build_kwargs(monitor_config, config)

    assert kwargs == {"host": "1.2.3.4", "count": 3, "timeout": 10}


def test_build_kwargs_uses_the_global_timeout_when_the_monitor_has_none():
    monitor_config = {"name": "m1", "type": "tcp", "host": "h", "port": 80}
    config = _base_config(timeout=25)

    assert main._build_kwargs(monitor_config, config)["timeout"] == 25


def test_build_kwargs_monitor_timeout_wins_over_the_global_one():
    monitor_config = {
        "name": "m1", "type": "tcp", "host": "h", "port": 80, "timeout": 7,
    }
    config = _base_config(timeout=25)

    assert main._build_kwargs(monitor_config, config)["timeout"] == 7


def test_build_kwargs_ping_defaults_count_to_one():
    monitor_config = {"name": "m1", "type": "ping", "host": "1.2.3.4"}
    config = _base_config()

    kwargs = main._build_kwargs(monitor_config, config)

    assert kwargs["count"] == 1


# --- run_check / retry-sequence error isolation ---------------------------------------


@pytest.mark.asyncio
async def test_run_check_dispatch_failure_does_not_propagate(monkeypatch):
    """A raising dispatch_alerts() must not blow up run_check() itself --
    the alert failure is logged and swallowed, not left to crash the
    caller (which would otherwise take the whole scheduling pass down)."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(
        name="m1", type="tcp", status="up", total_checks=5, total_up=5,
    )
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="down", response_ms=5.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    dispatch_mock = AsyncMock(side_effect=RuntimeError("smtp exploded"))
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    monitor_config = {
        "name": "m1", "type": "tcp", "host": "h", "port": 80, "alert_retries": 0,
    }
    config = _base_config()

    await main.run_check(monitor_config, config)  # must not raise

    dispatch_mock.assert_called_once()
    assert fake.monitors["m1"].status == "down"


@pytest.mark.asyncio
async def test_dispatch_retry_confirmation_alert_none_result_is_noop(monkeypatch):
    """No result from the retry sequence (nothing to alert on) means no
    dispatch attempt at all."""
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    await main._dispatch_retry_confirmation_alert(
        config=_base_config(),
        name="m1",
        state=MonitorState(name="m1", type="tcp"),
        expected_new_status="down",
        result=None,
    )

    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_retry_confirmation_alert_swallows_failure(monkeypatch):
    dispatch_mock = AsyncMock(side_effect=RuntimeError("webhook down"))
    monkeypatch.setattr(main, "dispatch_alerts", dispatch_mock)

    await main._dispatch_retry_confirmation_alert(
        config=_base_config(),
        name="m1",
        state=MonitorState(name="m1", type="tcp"),
        expected_new_status="down",
        result=CheckResult(status="down", response_ms=5.0),
    )  # must not raise

    dispatch_mock.assert_called_once()


# --- _interval_has_elapsed ------------------------------------------------------------


def test_interval_has_elapsed_no_prior_check_is_due():
    assert main._interval_has_elapsed(
        {"name": "m1"}, "m1", now=1000.0, config=_base_config(),
    ) is True


def test_interval_has_elapsed_not_yet_due():
    main._last_check_time["m1"] = 1000.0
    config = _base_config(check_interval_minutes=5)
    assert main._interval_has_elapsed(
        {"name": "m1"}, "m1", now=1100.0, config=config,  # only 100s elapsed
    ) is False


def test_interval_has_elapsed_due():
    main._last_check_time["m1"] = 1000.0
    config = _base_config(check_interval_minutes=5)
    assert main._interval_has_elapsed(
        {"name": "m1"}, "m1", now=1301.0, config=config,  # 301s elapsed, >300
    ) is True


def test_interval_has_elapsed_per_monitor_override():
    main._last_check_time["m1"] = 1000.0
    config = _base_config(check_interval_minutes=5)
    # Monitor overrides to 1 minute -- 61s elapsed should already be due,
    # even though the 5-minute global default wouldn't be.
    assert main._interval_has_elapsed(
        {"name": "m1", "check_interval_minutes": 1}, "m1", now=1061.0, config=config,
    ) is True


# --- _handle_inactive_monitor ---------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_inactive_monitor_not_due_does_nothing(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    main._last_check_time["m1"] = 1000.0
    config = _base_config(check_interval_minutes=5)

    await main._handle_inactive_monitor(
        {"name": "m1", "type": "http"}, "m1", now=1050.0, retention=30, config=config,
    )

    assert fake.checks == []


@pytest.mark.asyncio
async def test_handle_inactive_monitor_due_inserts_log(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    config = _base_config()  # no _last_check_time entry -- due immediately

    await main._handle_inactive_monitor(
        {"name": "m1", "type": "http"}, "m1", now=1050.0, retention=30, config=config,
    )

    assert len(fake.checks) == 1
    assert fake.checks[0]["status"] == "inactive"
    assert main._last_check_time["m1"] == 1050.0


# --- _handle_active_maintenance -------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_active_maintenance_tracks_entry(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    config = _base_config()

    await main._handle_active_maintenance(
        {"name": "m1", "type": "http"}, "m1", now=1000.0, retention=30,
        config=config, active_windows=["Weekly"],
    )

    assert main._in_maintenance["m1"] == ["Weekly"]
    assert len(fake.checks) == 1
    assert fake.checks[0]["details"] == {"maintenance": "Weekly"}


@pytest.mark.asyncio
async def test_handle_active_maintenance_already_tracked_no_duplicate_log(
    monkeypatch,
):
    """Already-known maintenance entry: log insertion is still gated by the
    normal check interval, same as any other monitor."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    main._in_maintenance["m1"] = ["Weekly"]
    main._last_check_time["m1"] = 1000.0
    config = _base_config(check_interval_minutes=5)

    await main._handle_active_maintenance(
        {"name": "m1", "type": "http"}, "m1", now=1050.0, retention=30,
        config=config, active_windows=["Weekly"],
    )

    assert fake.checks == []


# --- _find_maintenance_resume_mode ----------------------------------------------------


def test_find_maintenance_resume_mode_found():
    config = _base_config()
    config["maintenance"] = [{"name": "Weekly", "resume": "normal"}]
    assert main._find_maintenance_resume_mode(config, ["Weekly"]) == "normal"


def test_find_maintenance_resume_mode_defaults_to_verify():
    config = _base_config()
    config["maintenance"] = []
    assert main._find_maintenance_resume_mode(config, ["Unknown"]) == "verify"


# --- _handle_maintenance_exit ---------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_maintenance_exit_verify_schedules_check(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="up", response_ms=1.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"http": check_fn})
    main._in_maintenance["m1"] = ["Weekly"]
    config = _base_config()
    config["maintenance"] = [{"name": "Weekly", "resume": "verify"}]
    monitor = {"name": "m1", "type": "http", "url": "http://x", "alert_retries": 0}

    handled = await main._handle_maintenance_exit(
        monitor, config, "m1", i=0, count=1, stagger=30, now=1000.0,
    )

    assert handled is True
    assert "m1" not in main._in_maintenance
    assert "m1" in main._busy
    await main._busy["m1"]
    check_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_maintenance_exit_normal_falls_through(monkeypatch):
    """resume: "normal" means the caller's normal check-scheduling path
    should still run -- this function only returns True when it fully
    handled the pass itself.

    "normal" is the documented value; the code distinguishes only
    "verify" from everything else, so this is the real fallback path."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    main._in_maintenance["m1"] = ["Weekly"]
    config = _base_config()
    config["maintenance"] = [{"name": "Weekly", "resume": "normal"}]
    monitor = {"name": "m1", "type": "http", "url": "http://x"}

    handled = await main._handle_maintenance_exit(
        monitor, config, "m1", i=0, count=1, stagger=30, now=1000.0,
    )

    assert handled is False
    assert "m1" not in main._in_maintenance
    assert "m1" not in main._busy  # no check was scheduled by this function


# --- _maybe_schedule_check ------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_schedule_check_skips_if_retrying(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    main._retrying["m1"] = object()  # any truthy task stand-in
    config = _base_config()

    await main._maybe_schedule_check(
        {"name": "m1", "type": "http"}, config, "m1", now=1000.0, i=0, count=1,
        stagger=30,
    )

    assert "m1" not in main._busy


@pytest.mark.asyncio
async def test_maybe_schedule_check_skips_if_not_due(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    main._last_check_time["m1"] = 1000.0
    config = _base_config(check_interval_minutes=5)

    await main._maybe_schedule_check(
        {"name": "m1", "type": "http"}, config, "m1", now=1050.0, i=0, count=1,
        stagger=30,
    )

    assert "m1" not in main._busy


@pytest.mark.asyncio
async def test_maybe_schedule_check_schedules_when_due(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="up", response_ms=1.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"http": check_fn})
    config = _base_config()
    monitor = {"name": "m1", "type": "http", "url": "http://x", "alert_retries": 0}

    await main._maybe_schedule_check(
        monitor, config, "m1", now=1000.0, i=0, count=1, stagger=30,
    )

    assert "m1" in main._busy
    assert main._last_check_time["m1"] == 1000.0
    await main._busy["m1"]
    check_fn.assert_awaited_once()


# --- _process_monitor_scheduling_pass -------------------------------------------------


@pytest.mark.asyncio
async def test_scheduling_pass_routes_inactive_monitor(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    config = _base_config()
    monitor = {"name": "m1", "type": "http", "active": False}

    await main._process_monitor_scheduling_pass(
        monitor, "m1", i=0, count=1, stagger=30, now=1000.0, config=config,
    )

    assert len(fake.checks) == 1
    assert fake.checks[0]["status"] == "inactive"


@pytest.mark.asyncio
async def test_scheduling_pass_routes_active_maintenance(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(
        main, "get_active_maintenance", lambda config, name: ["Weekly"],
    )
    config = _base_config()
    monitor = {"name": "m1", "type": "http"}

    await main._process_monitor_scheduling_pass(
        monitor, "m1", i=0, count=1, stagger=30, now=1000.0, config=config,
    )

    assert main._in_maintenance["m1"] == ["Weekly"]
    assert len(fake.checks) == 1
    assert fake.checks[0]["status"] == "maintenance"


@pytest.mark.asyncio
async def test_scheduling_pass_routes_normal_check_when_no_maintenance(
    monkeypatch,
):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "get_active_maintenance", lambda config, name: [])
    check_fn = AsyncMock(return_value=CheckResult(status="up", response_ms=1.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"http": check_fn})
    config = _base_config()
    monitor = {"name": "m1", "type": "http", "url": "http://x", "alert_retries": 0}

    await main._process_monitor_scheduling_pass(
        monitor, "m1", i=0, count=1, stagger=30, now=1000.0, config=config,
    )

    assert "m1" in main._busy
    await main._busy["m1"]
    check_fn.assert_awaited_once()


# --- monitoring_loop: per-monitor isolation -------------------------------------------


@pytest.mark.asyncio
async def test_monitoring_loop_isolates_one_monitors_scheduling_failure(
    monkeypatch,
):
    """One monitor's scheduling pass raising must not stop the monitors
    listed after it in config from ever being scheduled, on this pass or any
    future one. This is what monitoring_loop()'s per-monitor try/except is
    for."""
    processed = []

    async def fake_pass(monitor, name, i, count, stagger, now, config):
        if name == "broken":
            raise RuntimeError("boom")
        processed.append(name)

    monkeypatch.setattr(main, "_process_monitor_scheduling_pass", fake_pass)
    monkeypatch.setattr(
        main.asyncio, "get_event_loop", lambda: _FakeLoop(now=0.0),
    )

    config = _base_config()
    config["monitors"] = [
        {"name": "before", "type": "http"},
        {"name": "broken", "type": "http"},
        {"name": "after-1", "type": "http"},
        {"name": "after-2", "type": "http"},
    ]

    # monitoring_loop() runs forever -- break out after the first pass via
    # asyncio.sleep(10), which we replace with something that raises to
    # stop the loop right after the scheduling pass we care about.
    async def sleep_and_stop(_seconds):
        raise asyncio.CancelledError()

    with monkeypatch.context() as m:
        m.setattr(main, "load_config", lambda path: config)
        m.setattr(main.asyncio, "sleep", sleep_and_stop)
        with pytest.raises(asyncio.CancelledError):
            await main.monitoring_loop()

    # Every monitor except the broken one was still processed, including
    # the ones listed AFTER it -- without the isolation, everything after the
    # broken monitor in the list would be starved.
    assert processed == ["before", "after-1", "after-2"]


@pytest.mark.asyncio
async def test_monitoring_loop_idle_when_no_monitors_configured(monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda path: _base_config())

    async def sleep_and_stop(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", sleep_and_stop)

    with pytest.raises(asyncio.CancelledError):
        await main.monitoring_loop()


@pytest.mark.asyncio
async def test_scheduling_pass_routes_maintenance_exit_verify(monkeypatch):
    """A monitor tracked as in-maintenance whose active window list has
    gone empty (exited) routes through _handle_maintenance_exit, not the
    plain _maybe_schedule_check path."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "get_active_maintenance", lambda config, name: [])
    check_fn = AsyncMock(return_value=CheckResult(status="up", response_ms=1.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"http": check_fn})
    main._in_maintenance["m1"] = ["Weekly"]
    config = _base_config()
    config["maintenance"] = [{"name": "Weekly", "resume": "verify"}]
    monitor = {"name": "m1", "type": "http", "url": "http://x", "alert_retries": 0}

    await main._process_monitor_scheduling_pass(
        monitor, "m1", i=0, count=1, stagger=30, now=1000.0, config=config,
    )

    assert "m1" not in main._in_maintenance
    assert "m1" in main._busy
    await main._busy["m1"]
    check_fn.assert_awaited_once()


# --- Small direct-branch tests --------------------------------------------------------


def test_log_task_error_cb_logs_on_failed_task():
    class _FakeTask:
        def cancelled(self):
            return False

        def exception(self):
            return RuntimeError("boom")

    # Must not raise -- just exercises the logging branch.
    main._log_task_error_cb(_FakeTask(), "m1")


def test_log_task_error_cb_ignores_cancelled_task():
    class _FakeTask:
        def cancelled(self):
            return True

        def exception(self):
            raise AssertionError("should not be called on a cancelled task")

    main._log_task_error_cb(_FakeTask(), "m1")  # must not raise


@pytest.mark.asyncio
async def test_record_check_verifying_merges_result_details(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    state = MonitorState(name="m1", type="http", status="up")
    result = CheckResult(
        status="down", response_ms=5.0, error="refused",
        details={"status_code": 503},
    )

    await main._record_check(
        {"name": "m1"}, state, result, "2026-08-15T00:00:00+00:00", verifying=True,
    )

    assert fake.checks[0]["details"] == {"verifying": True, "status_code": 503}


def test_build_verification_check_details_merges_result_details():
    result = CheckResult(status="down", response_ms=5.0, details={"code": 503})
    details = main._build_verification_check_details(1, retries=2, result=result)
    assert details == {"verifying": True, "code": 503}


@pytest.mark.asyncio
async def test_run_check_honors_initial_delay(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(return_value=CheckResult(status="up", response_ms=1.0))
    monkeypatch.setattr(main, "MONITOR_TYPES", {"http": check_fn})
    sleep_mock = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep_mock)
    monitor_config = {
        "name": "m1", "type": "http", "url": "http://x", "alert_retries": 0,
    }

    await main.run_check(monitor_config, _base_config(), delay=5.0)

    sleep_mock.assert_awaited_once_with(5.0)


@pytest.mark.asyncio
async def test_run_check_retry_task_failure_is_logged_not_raised(monkeypatch):
    """If the spawned retry-sequence task itself raises, the done-callback
    must log it rather than let it become an unhandled exception."""
    fake = FakeDB()
    fake.monitors["m1"] = MonitorState(
        name="m1", type="tcp", status="up", total_checks=5, total_up=5,
    )
    monkeypatch.setattr(main, "db", fake)
    check_fn = AsyncMock(side_effect=[
        CheckResult(status="down", response_ms=5.0),  # initial
        RuntimeError("check function exploded"),  # retry -- raises directly
    ])
    monkeypatch.setattr(main, "MONITOR_TYPES", {"tcp": check_fn})
    monkeypatch.setattr(main, "dispatch_alerts", AsyncMock())

    monitor_config = {
        "name": "m1", "type": "tcp", "host": "h", "port": 80,
        "alert_retries": 1, "retry_interval_minutes": 0,
    }

    await main.run_check(monitor_config, _base_config())
    task = main._retrying["m1"]
    with pytest.raises(RuntimeError, match="exploded"):
        await task
    await asyncio.sleep(0)  # let the task's done-callback actually run

    # The done-callback's cleanup still ran despite the failure -- the
    # monitor isn't left permanently stuck in _retrying.
    assert "m1" not in main._retrying


# --- Lifespan (app startup/shutdown) --------------------------------------------------
#
# These run the real FastAPI lifespan in-process by entering TestClient as
# a context manager. They cover the startup/shutdown path, including the
# shutdown ordering (stop the monitoring loop BEFORE closing the pool it
# uses). The Docker-stack tier exercises it end-to-end, but takes ~80s and a
# Docker daemon; these take milliseconds and pin the ordering precisely.


@pytest.mark.asyncio
async def test_lifespan_startup_then_shutdown_ordering(monkeypatch):
    """Startup must init the pool, then the schema, then start the
    supervisor -- and shutdown must stop the supervisor BEFORE closing
    the pool.

    Closing the pool first would leave the still-running monitoring loop making
    calls against a closed pool, and the supervisor would log CRITICAL
    "monitoring_loop() crashed, restarting in 5s" in the middle of an
    otherwise clean SIGTERM.
    """
    events = []

    async def fake_init_pool():
        events.append("init_pool")

    async def fake_init_schema():
        events.append("init_schema")

    async def fake_close_pool():
        events.append("close_pool")

    async def fake_supervisor():
        events.append("supervisor_started")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("supervisor_cancelled")
            raise

    monkeypatch.setattr(main.db, "init_pool", fake_init_pool)
    monkeypatch.setattr(main.db, "init_schema", fake_init_schema)
    monkeypatch.setattr(main.db, "close_pool", fake_close_pool)
    monkeypatch.setattr(main, "_monitoring_loop_supervisor", fake_supervisor)

    await main.startup()
    await asyncio.sleep(0)  # let the supervisor task actually start
    await main.shutdown()

    assert events == [
        "init_pool",
        "init_schema",
        "supervisor_started",
        "supervisor_cancelled",
        "close_pool",
    ], events


@pytest.mark.asyncio
async def test_shutdown_cancels_in_flight_check_tasks(monkeypatch):
    """The loop schedules checks as independent tasks, so cancelling the
    supervisor alone would leave an in-flight check running straight into
    the closed pool."""
    cancelled = []

    async def long_check(label):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(label)
            raise

    monkeypatch.setattr(main.db, "close_pool", AsyncMock())
    main._supervisor_task = None
    busy_task = asyncio.create_task(long_check("busy"))
    retry_task = asyncio.create_task(long_check("retrying"))
    main._busy["m1"] = busy_task
    main._retrying["m2"] = retry_task
    await asyncio.sleep(0)
    try:
        await main.shutdown()
        assert sorted(cancelled) == ["busy", "retrying"]
        assert busy_task.cancelled() and retry_task.cancelled()
    finally:
        main._busy.clear()
        main._retrying.clear()


def test_lifespan_runs_via_testclient_context_manager(monkeypatch):
    """The same path uvicorn drives, through the real ASGI lifespan
    protocol rather than by calling startup()/shutdown() directly."""
    from fastapi.testclient import TestClient

    events = []
    monkeypatch.setattr(
        main.db, "init_pool", AsyncMock(side_effect=lambda: events.append("up")))
    monkeypatch.setattr(main.db, "init_schema", AsyncMock())
    monkeypatch.setattr(
        main.db, "close_pool", AsyncMock(side_effect=lambda: events.append("down")))

    async def idle_supervisor():
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "_monitoring_loop_supervisor", idle_supervisor)

    with TestClient(main.app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert events == ["up"]

    assert events == ["up", "down"]


# --- Scheduling-state lifecycle -------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_removes_in_memory_scheduling_state(monkeypatch):
    """Deleting a removed monitor's DB rows must also drop its in-memory
    scheduling state.

    _in_maintenance is only popped when a monitor exits a window, which
    requires the monitor to still be in config, so a monitor removed while
    inside a window would leave its entry behind permanently. Entries would
    grow without bound across config churn, and a stale _in_maintenance entry
    would make a re-added monitor look like it had just exited maintenance: a
    spurious log line plus an unnecessary verification check on its first
    pass back.
    """
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    name = "ghost"
    fake.monitors[name] = MonitorState(name=name, type="http")
    main._last_check_time[name] = 123.0
    main._in_maintenance[name] = ["Some Window"]
    main._removal_timers.clear()

    cfg = {"global": {"cleanup_removed_monitors": True,
                      "cleanup_grace_minutes": 0},
           "monitors": []}
    try:
        # First pass starts the timer, second expires it (zero grace).
        await main._cleanup_removed_monitors(cfg)
        await main._cleanup_removed_monitors(cfg)

        assert name in fake.deleted, "DB rows should have been deleted"
        assert name not in main._last_check_time, "_last_check_time leaked"
        assert name not in main._in_maintenance, "_in_maintenance leaked"
    finally:
        main._last_check_time.pop(name, None)
        main._in_maintenance.pop(name, None)
        main._removal_timers.clear()


@pytest.mark.asyncio
async def test_cleanup_keeps_state_for_monitors_still_in_config(monkeypatch):
    """The cleanup must only drop state for monitors that actually went
    away -- an over-broad clear would reset every monitor's interval timer
    on each pass, re-checking everything constantly."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)
    fake.monitors["kept"] = MonitorState(name="kept", type="http")
    main._last_check_time["kept"] = 123.0
    main._removal_timers.clear()

    cfg = {"global": {"cleanup_removed_monitors": True,
                      "cleanup_grace_minutes": 0},
           "monitors": [{"name": "kept", "type": "http"}]}
    try:
        await main._cleanup_removed_monitors(cfg)
        await main._cleanup_removed_monitors(cfg)
        assert "kept" not in fake.deleted
        assert main._last_check_time.get("kept") == 123.0
    finally:
        main._last_check_time.pop("kept", None)
        main._removal_timers.clear()
