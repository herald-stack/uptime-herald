# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
main.py - Starts the web app and runs the monitoring loop.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import partial
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src import db, db_support
from src.alerts import dispatch_alerts
from src.config import DEFAULT_MONITOR_TIMEOUT, get_display_tz, load_config
from src.dashboard import router as dashboard_router
from src.maintenance import get_active_maintenance
from src.monitors import MONITOR_TYPES
from src.monitors.base import is_up

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """App startup/shutdown, as a lifespan handler.

    startup() runs before the app serves traffic and shutdown() after the last
    request. Both are defined further down this file, next to the
    monitoring-loop code they drive.
    """
    await startup()
    try:
        yield
    finally:
        # finally, not a bare trailing call: a crash while serving must
        # still stop the monitoring loop and close the pool, otherwise
        # shutdown's careful ordering is bypassed exactly when the app is
        # already in trouble.
        await shutdown()


app = FastAPI(title="Uptime Monitor", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(dashboard_router)

CONFIG_PATH = Path("config/config.yaml")

# Track monitors currently in a retry sequence so the monitoring loop
# can skip regular checks for them until retries resolve.
_retrying: dict[str, asyncio.Task] = {}

# Tracks a run_check task from the moment it is created, through its stagger
# delay, until it finishes. _retrying is only populated once run_check's body
# decides a retry is needed, which can be up to `stagger_seconds` after task
# creation. Without this, if check_interval_minutes is smaller than a
# monitor's stagger delay, a later loop pass could see _last_check_time
# already advanced (set when the task is created) but _retrying still empty,
# and start a second run_check for the same monitor before the first has run.
_busy: dict[str, asyncio.Task] = {}

# Track the last check time (including retries) for each monitor so the
# monitoring loop schedules the next regular check from the last retry.
_last_check_time: dict[str, float] = {}

# Track when a monitor was first seen missing from config.
# After cleanup_grace_minutes, its DB records are deleted.
_removal_timers: dict[str, float] = {}

# Track monitors currently in maintenance so we know when they exit.
# Maps monitor name -> list of active window names.
_in_maintenance: dict[str, list[str]] = {}


# --------------------------------------------------------------------------------------
def _log_task_error_cb(t, _name=""):
    """Callback to log errors from monitor check tasks."""
    if not t.cancelled() and t.exception() is not None:
        logger.error("Monitor check task failed for '%s': %s",
                     _name, t.exception())


# --------------------------------------------------------------------------------------
def _discard_busy_cb(_task: asyncio.Task, name: str) -> None:
    """Drop a finished check task from the busy set.

    A named function rather than a lambda so its signature is checkable --
    see _schedule_check_task() for why these are bound with partial().
    """
    _busy.pop(name, None)


# --------------------------------------------------------------------------------------
def _build_kwargs(monitor_config: dict, config: dict) -> dict:
    """Build the keyword arguments for a monitor's check function.

    Raises ValueError for a type this function doesn't know how to build
    arguments for. Otherwise check_fn(**kwargs) would fail with a confusing
    TypeError, which monitoring_loop()'s per-monitor handler logs as a generic
    "Scheduling error" while _last_check_time advances as though the check had
    run, so the monitor would silently never be checked again.

    Reachable whenever MONITOR_TYPES gains an entry not handled here: config
    validation checks the type against MONITOR_TYPES, not this function, so
    the two can drift.
    """
    monitor_type = monitor_config["type"]
    kwargs = {}
    if monitor_type == "http":
        kwargs["url"] = monitor_config["url"]
        kwargs["verify_ssl"] = monitor_config.get("verify_ssl", True)
        kwargs["contact_email"] = config["global"].get("contact_email")
        label = monitor_config.get("credentials")
        if label:
            credential = config["credentials"][label]
            kwargs["auth"] = (credential["username"], credential["password"])
    elif monitor_type == "tcp":
        kwargs["host"] = monitor_config["host"]
        kwargs["port"] = monitor_config["port"]
    elif monitor_type == "ping":
        kwargs["host"] = monitor_config["host"]
        kwargs["count"] = monitor_config.get("count", 1)
    else:
        raise ValueError(
            f"Monitor type '{monitor_type}' is registered in MONITOR_TYPES "
            f"but _build_kwargs() doesn't know how to build its check "
            f"arguments -- add a branch for it in src/main.py"
        )
    kwargs["timeout"] = monitor_config.get(
        "timeout", config["global"].get("timeout", DEFAULT_MONITOR_TIMEOUT)
    )
    return kwargs


# --------------------------------------------------------------------------------------
async def _execute_check(monitor_config: dict, config: dict) -> tuple:
    """Run a single check and return (result, old_status, state).

    Does NOT update state, insert checks, or dispatch alerts.
    The caller is responsible for persisting results.
    Needs config to resolve per-monitor overrides like contact_email.
    """
    name = monitor_config["name"]
    monitor_type = monitor_config["type"]
    state = await db.load_state(name, monitor_type)
    check_fn = MONITOR_TYPES[monitor_type]
    kwargs = _build_kwargs(monitor_config, config)
    result = await check_fn(**kwargs)
    return result, state.status, state


# --------------------------------------------------------------------------------------
async def _record_check(
    monitor_config: dict,
    state,
    result,
    now_iso: str,
    verifying: bool = False,
) -> None:
    """Record a check result: update in-memory state, persist to DB.

    When verifying=True, inserts the check row with a verifying marker
    in the details JSON but does NOT update the monitor's confirmed
    status, last_change, or dispatch alerts.
    """
    name = monitor_config["name"]
    old_status = state.status
    expected_status = monitor_config.get("expected_status")
    result_is_up = is_up(result.status, expected_status)

    state.last_check = now_iso
    state.total_checks += 1
    if state.first_check is None:
        state.first_check = now_iso
    if result_is_up:
        state.total_up += 1
    else:
        state.total_down += 1

    if not verifying:
        # Only update confirmed status and last_change on a real
        # health transition (not during verification checks).
        if (
            result_is_up != is_up(old_status, expected_status)
            and old_status != "unknown"
        ):
            state.last_change = now_iso
            logger.info(
                "Monitor '%s' changed: %s → %s", name, old_status, result.status
            )
        state.status = result.status

    details = result.details if result.details else None
    if verifying:
        details = {"verifying": True}
        if result.details:
            details.update(result.details)

    # One transaction for both writes. These two are a single logical
    # fact -- "this check happened" -- split across two tables: the
    # running counters on `monitors`, and the detail row on `checks`.
    # Committed separately, a failure on the insert would leave the counter
    # bump applied, so monitors.total_checks would drift above COUNT(*) on
    # checks with nothing in the app to reconcile them.
    async with db.transaction() as conn:
        await db.save_state(state, conn=conn)
        await db.insert_check(
            monitor_name=name,
            check_time=now_iso,
            status=result.status,
            is_up=result_is_up,
            response_ms=result.response_ms,
            error=result.error,
            details=details,
            conn=conn,
        )

    # Retention pruning is handled by the caller, which has full
    # config access to resolve per-monitor overrides.


# --------------------------------------------------------------------------------------
async def _insert_maintenance_log(
    monitor_name: str,
    monitor_type: str,
    now_iso: str,
    window_name: str,
    retention: int,
) -> None:
    """Insert a maintenance log row (no real check, no state update).

    Same monitors-row-must-exist-first requirement as
    _insert_inactive_log — see its docstring for why.
    """
    state = await db.load_state(monitor_name, monitor_type)
    # Atomic for the same reason as _record_check: the save_state here
    # exists purely so the monitors row is present for the check row's
    # foreign key, so half-applying the pair is never useful.
    async with db.transaction() as conn:
        await db.save_state(state, conn=conn)
        await db.insert_check(
            monitor_name=monitor_name,
            check_time=now_iso,
            status="maintenance",
            is_up=None,  # not a real check -- never counted as up or down
            response_ms=None,
            error=None,
            details={"maintenance": window_name},
            conn=conn,
        )
    # Pruning stays outside the transaction -- it's independent
    # retention cleanup, not part of "this log row happened", and
    # including it would hold the write locks for a DELETE that may
    # touch many rows.
    await db.prune_old_checks(monitor_name, retention)


# --------------------------------------------------------------------------------------
async def _insert_inactive_log(
    monitor_name: str,
    monitor_type: str,
    now_iso: str,
    retention: int,
) -> None:
    """Insert an inactive log row (no real check, no state update).

    Ensures the monitor's `monitors` row exists first: a monitor that has
    never been active has no row yet, and `checks.monitor_name` has a foreign
    key against it, so inserting without this raises an IntegrityError.
    monitoring_loop()'s per-monitor try/except would catch that and skip the
    pass, but the check row would never be written.
    """
    state = await db.load_state(monitor_name, monitor_type)
    # Atomic: the save_state is only here to satisfy the check row's
    # foreign key (see the docstring above), so the two must land
    # together or not at all.
    async with db.transaction() as conn:
        await db.save_state(state, conn=conn)
        await db.insert_check(
            monitor_name=monitor_name,
            check_time=now_iso,
            status="inactive",
            is_up=None,  # not a real check -- never counted as up or down
            response_ms=None,
            error=None,
            details=None,
            conn=conn,
        )
    # Pruning stays outside the transaction -- see _insert_maintenance_log.
    await db.prune_old_checks(monitor_name, retention)


# --------------------------------------------------------------------------------------
async def _cleanup_removed_monitors(
    config: dict,
) -> None:
    """Delete DB records for monitors removed from config after the grace period.

    Compares monitor names in config against the monitors table. Monitors
    that exist in the DB but not in config are tracked with a removal
    timestamp. Once the configured grace period passes, the monitor row
    is deleted (CASCADE removes its checks automatically). If a monitor
    reappears in config before the grace period expires, its timer is
    cancelled and its history is preserved.
    """
    enabled = config["global"].get("cleanup_removed_monitors", True)
    if not enabled:
        _removal_timers.clear()
        return

    grace_minutes = config["global"].get("cleanup_grace_minutes", 20)
    grace_seconds = grace_minutes * 60
    now = asyncio.get_event_loop().time()

    # Get monitor names currently in config
    config_names = {
        m["name"] for m in config.get("monitors", [])
    }

    # Get monitor names that exist in the DB
    db_names = set(await db.get_all_monitor_names())

    # Track monitors in DB but not in config
    orphaned = db_names - config_names
    for name in orphaned:
        if name not in _removal_timers:
            _removal_timers[name] = now
            logger.info(
                "Monitor '%s' removed from config, cleanup timer started "
                "(grace: %d min)",
                name, grace_minutes,
            )

    # Cancel timers for monitors that came back to config
    returned = set(_removal_timers.keys()) - orphaned
    for name in returned:
        del _removal_timers[name]
        logger.info(
            "Monitor '%s' returned to config before cleanup, timer cancelled",
            name,
        )

    # Delete monitors whose grace period has expired
    for name, removal_time in list(_removal_timers.items()):
        if now - removal_time >= grace_seconds:
            await db.delete_monitor(name)
            del _removal_timers[name]
            # Drop the monitor's in-memory scheduling state too, not just its
            # database rows. A removed monitor can no longer reach the code that
            # cleans this state up (_in_maintenance is only popped when a monitor
            # exits a window, which needs it to still be in config), so entries
            # would grow without bound across config churn. A stale
            # _in_maintenance entry would also make a monitor removed mid-window
            # and later re-added look as though it had just exited maintenance:
            # a spurious log line and an unnecessary verification check on its
            # first pass back.
            #
            # _retrying/_busy are deliberately NOT touched: their tasks'
            # done-callbacks own them, and a task still in flight needs its
            # entry until it finishes.
            _last_check_time.pop(name, None)
            _in_maintenance.pop(name, None)


# --------------------------------------------------------------------------------------
async def run_check(
    monitor_config: dict,
    config: dict,
    delay: float = 0,
    force_verify: bool = False,
) -> None:
    """Run a single monitor check with optional retry verification.

    Normal flow:
        1. Run the check.
        2. If health changed (up→down or down→up) from a known state,
           start a retry sequence — as long as alert_retries > 0.
        3. Retries run at retry_interval_minutes spacing. If any retry's
           health classification (is_up()) matches the OLD status's
           health classification, the sequence aborts (false alarm).
           This is a health comparison, not a status-string comparison:
           e.g. "down" -> "timeout" still aborts, since both are down.
        4. After all retries confirm the new status, update state.status,
           set last_change, and dispatch alerts.
        5. Each check (including retries) is recorded in the checks table.
           Retry rows are marked with details.verifying = true.
        6. A monitor that stays down is alerted again each time a check
           finds it still down and alert_cooldown_minutes have passed since
           its last alert.

    When force_verify=True, the first check is recorded as a verification
    step if the resolved alert_retries for this monitor is > 0 — same
    should_retry gate as a real state change. With alert_retries == 0 it
    falls through to the normal (non-verifying) path instead, same as
    any other check with retries disabled. Used when a monitor exits a
    maintenance window with resume="verify".

    The monitoring loop tracks active retry tasks in _retrying and
    skips regular checks for monitors that are retrying.
    """
    if delay > 0:
        await asyncio.sleep(delay)

    name = monitor_config["name"]

    result, old_status, state = await _execute_check(monitor_config, config)
    now = datetime.now(timezone.utc).isoformat()

    # Resolve per-monitor overrides
    alert_retries = monitor_config.get(
        "alert_retries", config["global"]["alert_retries"]
    )
    retry_on_up = monitor_config.get(
        "retry_on_up", config["global"]["retry_on_up"]
    )
    retention = monitor_config.get(
        "retention_days", config["global"]["retention_days"]
    )
    expected_status = monitor_config.get("expected_status")
    result_is_up = is_up(result.status, expected_status)

    # State change = health change (up→down or down→up), not a status
    # code change (200→201 is still up, not a state change)
    state_changed = (
        result_is_up != is_up(old_status, expected_status)
        and old_status != "unknown"
    )

    # Determine if we should run a retry sequence for this state change
    should_retry = (state_changed or force_verify) and alert_retries > 0
    if should_retry and result_is_up and not retry_on_up and not force_verify:
        # Recovery alerts skip retries when retry_on_up is false
        should_retry = False

    if should_retry:
        # Record this check as a verification step (don't update
        # confirmed status yet). Set the dirty flag so the UI refreshes
        # to show the verifying check in the detail panel.
        state.state_changed = True
        await _record_check(monitor_config, state, result, now, verifying=True)
        await db.prune_old_checks(name, retention)
        _last_check_time[name] = asyncio.get_event_loop().time()

        retry_interval = monitor_config.get(
            "retry_interval_minutes",
            config["global"]["retry_interval_minutes"],
        )
        logger.info(
            "Monitor '%s' potential change %s → %s, starting %d retry checks "
            "at %d min intervals",
            name, old_status, result.status, alert_retries, retry_interval,
        )

        # Launch retry task
        task = asyncio.create_task(
            _run_retry_sequence(
                monitor_config, config, old_status, result.status,
                alert_retries, retry_interval,
            )
        )
        _retrying[name] = task

        def _cleanup(t, _name=name):
            _retrying.pop(_name, None)
            if not t.cancelled() and t.exception() is not None:
                logger.error("Retry sequence failed for '%s': %s",
                             _name, t.exception())

        task.add_done_callback(_cleanup)
        return

    # --- No retry sequence (or this IS a retry check) ---------------------------------
    # Record the check normally and update confirmed status
    # Reset the dirty flag — if it was set true by a previous state
    # change, the UI has had one refresh cycle to pick it up.
    state.state_changed = False
    await _record_check(monitor_config, state, result, now, verifying=False)
    await db.prune_old_checks(name, retention)
    _last_check_time[name] = asyncio.get_event_loop().time()

    # Dispatch alerts on confirmed state change
    if state_changed:
        # Set the dirty flag so the UI knows to refresh the detail view
        state.state_changed = True
        await db.save_state(state)
        try:
            await dispatch_alerts(
                config=config,
                state=state,
                new_status=result.status,
                error=result.error,
                response_ms=result.response_ms,
            )
        except Exception as e:
            logger.error("Alert dispatch failed for '%s': %s", name, e)
    elif not result_is_up and old_status != "unknown":
        # Still down: repeat the down alert once the cooldown has passed
        # since the last alert. ("unknown" = never checked before, which is
        # not a monitor that has been down.)
        cooldown = monitor_config.get(
            "alert_cooldown_minutes",
            config["global"]["alert_cooldown_minutes"],
        ) * 60
        try:
            await dispatch_alerts(
                config=config,
                state=state,
                new_status=result.status,
                error=result.error,
                response_ms=result.response_ms,
                repeat_after=cooldown,
            )
        except Exception as e:
            logger.error("Alert dispatch failed for '%s': %s", name, e)


# --------------------------------------------------------------------------------------
async def _record_verification_check_state(
    state, result, now: str, expected_status: int | None = None, conn=None,
) -> None:
    """Update a monitor's running totals for a retry-sequence verification
    check and persist the state. Shared by both the abort and confirm paths
    of _run_retry_sequence — they update state identically.

    Both callers pass `conn` from a db.transaction() so this counter
    update is atomic with the check row they insert alongside it — the
    same save_state/insert_check pairing as _record_check."""
    state.last_check = now
    state.total_checks += 1
    state.state_changed = True  # new verifying check entry — refresh UI
    if is_up(result.status, expected_status):
        state.total_up += 1
    else:
        state.total_down += 1
    await db.save_state(state, conn=conn)


# --------------------------------------------------------------------------------------
def _build_verification_check_details(attempt: int, retries: int, result) -> dict:
    """Build the `details` payload for a retry-sequence check row. The last
    retry is marked as having passed verification; earlier ones aren't."""
    if attempt == retries:
        check_details = {"verifying": True, "passed": True}
    else:
        check_details = {"verifying": True}
    if result.details:
        check_details.update(result.details)
    return check_details


# --------------------------------------------------------------------------------------
async def _dispatch_retry_confirmation_alert(
    config: dict,
    name: str,
    state,
    expected_new_status: str,
    result,
) -> None:
    """Dispatch alerts once a retry sequence has confirmed a state change.

    `result` is from the last successful retry; no result means there's
    nothing to alert on.
    """
    if result is None:
        return
    try:
        await dispatch_alerts(
            config=config,
            state=state,
            new_status=expected_new_status,
            error=result.error,
            response_ms=result.response_ms,
        )
    except Exception as e:
        logger.error("Alert dispatch failed for '%s': %s", name, e)


# --------------------------------------------------------------------------------------
async def _run_retry_sequence(
    monitor_config: dict,
    config: dict,
    old_status: str,
    expected_new_status: str,
    retries: int,
    retry_interval_minutes: int,
) -> None:
    """Run retry checks for a monitor that had a potential state change.

    Runs up to `retries` additional checks, spaced `retry_interval_minutes`
    apart. If any check's health classification (is_up()) matches
    `old_status`'s health classification, the sequence aborts (false
    alarm) — this is a health comparison, not a status-string comparison,
    so e.g. a retry returning "timeout" still aborts a "down" old_status.
    If all retries confirm the same health classification as
    `expected_new_status`, the monitor's state is updated and alerts are
    dispatched.
    """
    name = monitor_config["name"]
    retry_interval_sec = retry_interval_minutes * 60
    retention = monitor_config.get(
        "retention_days", config["global"]["retention_days"]
    )
    expected_status = monitor_config.get("expected_status")

    if retries < 1:
        return

    result = None
    for attempt in range(1, retries + 1):
        await asyncio.sleep(retry_interval_sec)

        result, _old_status, state = await _execute_check(monitor_config, config)
        now = datetime.now(timezone.utc).isoformat()
        result_is_up = is_up(result.status, expected_status)

        # False alarm: health state didn't actually change
        # (e.g. "200" → "200", or "down" → "timeout" — both still down)
        if result_is_up == is_up(old_status, expected_status):
            # False alarm — monitor returned to its previous status
            logger.info(
                "Monitor '%s' retry %d/%d returned old status (%s), "
                "aborting retry sequence",
                name, attempt, retries, old_status,
            )
            # Record this check as a verification step, atomically
            async with db.transaction() as conn:
                await _record_verification_check_state(
                    state, result, now, expected_status, conn=conn
                )
                await db.insert_check(
                    monitor_name=name,
                    check_time=now,
                    status=result.status,
                    is_up=result_is_up,
                    response_ms=result.response_ms,
                    error=result.error,
                    details={"verifying": True, "aborted": True},
                    conn=conn,
                )
            await db.prune_old_checks(name, retention)
            _last_check_time[name] = asyncio.get_event_loop().time()
            return

        # Retry confirms the new status — record it, atomically
        check_details = _build_verification_check_details(attempt, retries, result)
        async with db.transaction() as conn:
            await _record_verification_check_state(
                state, result, now, expected_status, conn=conn
            )
            await db.insert_check(
                monitor_name=name,
                check_time=now,
                status=result.status,
                is_up=result_is_up,
                response_ms=result.response_ms,
                error=result.error,
                details=check_details,
                conn=conn,
            )
        await db.prune_old_checks(name, retention)
        _last_check_time[name] = asyncio.get_event_loop().time()

    # All retries confirmed the new status — commit the state change
    logger.info(
        "Monitor '%s' state change confirmed after %d retries: %s → %s",
        name, retries, old_status, expected_new_status,
    )

    # Reload state to get fresh totals, then update confirmed fields
    state = await db.load_state(name, monitor_config["type"])
    state.status = expected_new_status
    state.last_change = datetime.now(timezone.utc).isoformat()
    state.state_changed = True  # dirty flag for UI detail refresh
    await db.save_state(state)

    # Dispatch alerts (result is from the last successful retry)
    await _dispatch_retry_confirmation_alert(
        config, name, state, expected_new_status, result,
    )


# --------------------------------------------------------------------------------------
def _interval_has_elapsed(monitor: dict, name: str, now: float, config: dict) -> bool:
    """True if enough time has passed since `name`'s last check, or if
    there's no recorded last check yet (in which case it's due now)."""
    if name not in _last_check_time:
        return True
    elapsed = now - _last_check_time[name]
    default_interval = config["global"]["check_interval_minutes"]
    interval = monitor.get("check_interval_minutes", default_interval) * 60
    return elapsed >= interval


# --------------------------------------------------------------------------------------
async def _handle_inactive_monitor(
    monitor: dict, name: str, now: float, retention: int, config: dict
) -> None:
    """Insert an inactive-monitor log row at the configured interval."""
    if not _interval_has_elapsed(monitor, name, now, config):
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    await _insert_inactive_log(name, monitor["type"], now_iso, retention)
    _last_check_time[name] = now


# --------------------------------------------------------------------------------------
async def _handle_active_maintenance(
    monitor: dict,
    name: str,
    now: float,
    retention: int,
    config: dict,
    active_windows: list,
) -> None:
    """Track maintenance entry and insert a maintenance log row at interval."""
    if name not in _in_maintenance:
        _in_maintenance[name] = active_windows
        logger.info(
            "Monitor '%s' entering maintenance: %s", name, ", ".join(active_windows),
        )

    if not _interval_has_elapsed(monitor, name, now, config):
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    await _insert_maintenance_log(
        name, monitor["type"], now_iso, active_windows[0], retention
    )
    _last_check_time[name] = now


# --------------------------------------------------------------------------------------
def _find_maintenance_resume_mode(config: dict, exited_windows: list) -> str:
    """Look up the resume behavior of whichever maintenance window a
    monitor just exited."""
    for w in config.get("maintenance", []):
        if w["name"] in exited_windows:
            return w.get("resume", "verify")
    return "verify"


# --------------------------------------------------------------------------------------
def _schedule_check_task(
    monitor: dict,
    config: dict,
    name: str,
    delay: float,
    force_verify: bool = False,
) -> None:
    """Create and track a check task for `monitor`, with the standard
    done-callbacks for error logging and busy-set cleanup."""
    task = asyncio.create_task(
        run_check(monitor, config, delay, force_verify=force_verify)
    )
    _busy[name] = task
    # functools.partial rather than `lambda t, _n=name: ...`: the
    # default-argument idiom for binding the loop variable leaves the
    # lambda's own type uninferable, and partial states the binding
    # explicitly while keeping the same late-binding-safe behaviour.
    task.add_done_callback(partial(_log_task_error_cb, _name=name))
    task.add_done_callback(partial(_discard_busy_cb, name=name))


# --------------------------------------------------------------------------------------
async def _handle_maintenance_exit(
    monitor: dict,
    config: dict,
    name: str,
    i: int,
    count: int,
    stagger: float,
    now: float,
) -> bool:
    """Handle a monitor that just exited maintenance.

    Returns True if this fully handles the scheduling pass for this monitor
    (caller should move on to the next monitor), False if the normal
    check-scheduling logic below should still run.
    """
    exited_windows = _in_maintenance.pop(name)
    logger.info(
        "Monitor '%s' exited maintenance: %s", name, ", ".join(exited_windows),
    )
    resume_mode = _find_maintenance_resume_mode(config, exited_windows)
    if resume_mode != "verify":
        return False
    # Run check with verification sequence
    delay = (i / count) * stagger if count > 1 else 0
    _schedule_check_task(monitor, config, name, delay, force_verify=True)
    _last_check_time[name] = now
    return True


# --------------------------------------------------------------------------------------
async def _maybe_schedule_check(
    monitor: dict,
    config: dict,
    name: str,
    now: float,
    i: int,
    count: int,
    stagger: float,
) -> None:
    """Schedule a regular check for `monitor` if it's due and not already
    busy or in a retry sequence.

    Skip if this monitor is currently in a retry sequence, or if a check
    task was just created for it and hasn't run yet (see _busy's docstring
    above for why both are needed).
    """
    if name in _retrying or name in _busy:
        return
    if not _interval_has_elapsed(monitor, name, now, config):
        return
    delay = (i / count) * stagger if count > 1 else 0
    _schedule_check_task(monitor, config, name, delay)
    _last_check_time[name] = now


# --------------------------------------------------------------------------------------
async def _process_monitor_scheduling_pass(
    monitor: dict,
    name: str,
    i: int,
    count: int,
    stagger: float,
    now: float,
    config: dict,
) -> None:
    """Run one monitor's scheduling logic for a single monitoring_loop pass."""
    retention = monitor.get("retention_days", config["global"]["retention_days"])

    # --- Inactive monitors: insert log row at interval --------------------------------
    if not monitor.get("active", True):
        await _handle_inactive_monitor(monitor, name, now, retention, config)
        return

    # --- Maintenance window check -----------------------------------------------------
    active_windows = get_active_maintenance(config, name)
    if active_windows:
        await _handle_active_maintenance(
            monitor, name, now, retention, config, active_windows
        )
        return

    # --- Monitor exited maintenance ---------------------------------------------------
    if name in _in_maintenance:
        handled = await _handle_maintenance_exit(
            monitor, config, name, i, count, stagger, now
        )
        if handled:
            return
        # else: fall through to normal check

    await _maybe_schedule_check(monitor, config, name, now, i, count, stagger)


# --------------------------------------------------------------------------------------
async def monitoring_loop() -> None:
    """Main monitoring loop — runs checks on configured intervals.

    Monitors that are currently in a retry sequence are skipped until
    their retries complete. The next regular check after retries is
    scheduled from the time the retry task finishes.
    """
    config = load_config(str(CONFIG_PATH))
    monitors = config.get("monitors", [])

    # Idle rather than return, but keep re-reading config, so a monitor added
    # to an empty monitors.yaml is picked up within ~60s like every other
    # config change.
    while not monitors:
        logger.warning(
            "No monitors configured. Monitoring loop idle, re-checking "
            "config in 60s."
        )
        await asyncio.sleep(60)
        try:
            config = load_config(str(CONFIG_PATH))
            monitors = config.get("monitors", [])
        except Exception as e:
            logger.warning("Failed to reload config while idle: %s", e)

    logger.info("Starting monitoring loop with %d monitors", len(monitors))

    while True:
        now = asyncio.get_event_loop().time()

        stagger = config["global"].get("stagger_seconds", 30)
        count = len(monitors)

        for i, monitor in enumerate(monitors):
            name = monitor["name"]
            # Each monitor's scheduling logic runs in its own try/except so
            # one broken monitor (bad config, a DB error, anything) can
            # never take down scheduling for every other monitor. Without
            # this, a single monitor raising here would crash
            # monitoring_loop() entirely, silently starving every monitor
            # listed after it in monitors.yaml.
            try:
                await _process_monitor_scheduling_pass(
                    monitor, name, i, count, stagger, now, config
                )
            except Exception as e:
                logger.error(
                    "Scheduling error for monitor '%s', skipping this pass: %s",
                    name, e,
                )

        await asyncio.sleep(10)

        # Re-read config every ~60s
        if int(now) % 60 < 10:
            try:
                config = load_config(str(CONFIG_PATH))
                monitors = config.get("monitors", [])
                await _cleanup_removed_monitors(config)
            except Exception as e:
                logger.warning("Failed to reload config: %s", e)


# --------------------------------------------------------------------------------------
async def _monitoring_loop_supervisor() -> None:
    """Run monitoring_loop(), restarting it if it ever raises.

    monitoring_loop() is expected to run forever. If it dies from an
    unanticipated bug, the app would otherwise keep serving the dashboard
    while silently never checking anything again — this supervisor makes
    that failure loud (logged at CRITICAL) and self-healing (restarts
    after a short backoff) instead of a silent, permanent outage.
    """
    backoff = 5
    while True:
        try:
            await monitoring_loop()
            # monitoring_loop() only returns normally in the "no monitors
            # configured" branch, which itself loops forever — reaching
            # here means it returned some other way, which shouldn't
            # happen, but don't spin hot if it does.
            logger.critical(
                "monitoring_loop() returned unexpectedly, restarting in %ds",
                backoff,
            )
        except Exception:
            logger.critical(
                "monitoring_loop() crashed, restarting in %ds", backoff,
                exc_info=True,
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


# --------------------------------------------------------------------------------------
def _supervisor_exited_cb(t: asyncio.Task) -> None:
    """Log the supervisor task's exit, loudly only when it's abnormal.

    A cancelled task is the *expected* outcome here -- that's how shutdown()
    stops the loop -- so it must not be reported at CRITICAL, or every clean
    shutdown would emit a fake emergency.
    """
    if t.cancelled():
        logger.info("Monitoring supervisor task cancelled (shutdown)")
        return
    exc = t.exception()
    if exc is not None:
        logger.critical(
            "Monitoring supervisor task itself exited: %s", exc, exc_info=exc,
        )
    else:
        logger.critical(
            "Monitoring supervisor task itself returned unexpectedly"
        )


# --------------------------------------------------------------------------------------
async def _db_support_notice_loop() -> None:
    """Check whether the database is nearing (or past) the end of its
    support, and send the scheduled notice if one is due.

    Runs shortly after startup, then at midnight in the dashboard's
    configured timezone every day. A check that fails (the database being
    unreachable, say) is logged and retried within the hour -- or at
    midnight, if that comes first. It must never be able to affect
    monitoring, so it runs as its own task instead of inside
    monitoring_loop().
    """
    # Let startup finish and the first checks begin before doing anything.
    await asyncio.sleep(db_support.STARTUP_DELAY_SECONDS)
    while True:
        tz: tzinfo = timezone.utc
        ok = True
        try:
            config = load_config(str(CONFIG_PATH))
            tz = get_display_tz(config)
            await db_support.check_and_notify(config)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            ok = False
            logger.warning("Database end-of-support check failed: %s", e)
        await asyncio.sleep(db_support.seconds_until_next_check(tz, ok))


# Module-level so the task isn't only referenced by a local that goes out
# of scope -- asyncio holds no strong reference of its own, so a task left
# to a local can be garbage collected mid-flight. Also what shutdown()
# needs in order to stop the loop before the pool it uses goes away.
_supervisor_task: asyncio.Task | None = None
_db_support_task: asyncio.Task | None = None


# --------------------------------------------------------------------------------------
async def startup():
    """Start the monitoring loop on app startup.

    Called by the lifespan handler (see `lifespan` near the top of this
    file), not registered as an event handler itself.
    """
    global _supervisor_task, _db_support_task
    await db.init_pool()
    await db.init_schema()
    _supervisor_task = asyncio.create_task(_monitoring_loop_supervisor())
    _supervisor_task.add_done_callback(_supervisor_exited_cb)
    _db_support_task = asyncio.create_task(_db_support_notice_loop())


# --------------------------------------------------------------------------------------
async def shutdown():
    """Stop the monitoring loop, then close the DB pool.

    Order matters: closing the pool first would leave the still-running
    monitoring loop making calls against a closed pool, so the supervisor
    would log "monitoring_loop() crashed" and schedule a restart in the
    middle of a normal shutdown.
    """
    global _supervisor_task, _db_support_task
    if _db_support_task is not None:
        _db_support_task.cancel()
        try:
            await _db_support_task
        except asyncio.CancelledError:
            pass
        _db_support_task = None

    if _supervisor_task is not None:
        _supervisor_task.cancel()
        try:
            await _supervisor_task
        except asyncio.CancelledError:
            pass
        _supervisor_task = None

    # The loop schedules checks as independent tasks, so cancelling the
    # supervisor alone leaves any in-flight check (or retry sequence)
    # running straight into the closed pool below.
    pending = [t for t in (*_busy.values(), *_retrying.values()) if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    await db.close_pool()


# --------------------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}
