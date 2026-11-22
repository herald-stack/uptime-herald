# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_db_dispatcher.py - Tests for the backend dispatcher, src/db.py.

The dispatch decision (which backend module `src.db` actually becomes)
happens once, at module import time, driven by an environment variable
read at the top of db.py. That can't be exercised by monkeypatching
os.environ after the fact in the normal test process -- src.db is
almost certainly already imported and cached in sys.modules by the time
any test in this file runs (something else in the suite imports it
first). Each case here spawns a genuinely fresh Python subprocess with
DB_BACKEND set beforehand, so the import machinery actually re-runs
db.py's top-level dispatch logic from scratch.
"""

import subprocess
import sys

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _run_with_backend(backend: str | None, code: str) -> subprocess.CompletedProcess:
    """backend=None omits DB_BACKEND from the subprocess environment
    entirely -- distinct from setting it to an empty string, which is a
    real value os.environ.get() would happily return instead of falling
    through to its own default. Passing "" here once (test-side bug, not
    a db.py bug) exercised the wrong path: it correctly hit the
    invalid-backend error, because an empty string isn't "unset"."""
    env = {"PATH": "/usr/bin:/bin"}
    if backend is not None:
        env["DB_BACKEND"] = backend
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=30,
    )


def test_mariadb_backend_selected_by_default():
    """No DB_BACKEND set at all -- must default to mariadb, matching
    every DB_* variable's own default and the bundled docker-compose.yaml
    service."""
    result = _run_with_backend(
        None,  # omitted entirely, not "mariadb" -- proves the default path
        "from src import db, db_mariadb\n"
        "assert db is db_mariadb, 'default backend was not mariadb'\n"
        "print('OK')",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_mariadb_backend_selected_explicitly():
    result = _run_with_backend(
        "mariadb",
        "from src import db, db_mariadb\n"
        "assert db is db_mariadb\n"
        "print('OK')",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_postgresql_backend_selected():
    result = _run_with_backend(
        "postgresql",
        "from src import db, db_postgres\n"
        "assert db is db_postgres\n"
        "print('OK')",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_postgres_alias_also_selects_postgres_backend():
    """'postgres' (not just the full 'postgresql') is accepted too --
    the shorter spelling is common enough to be worth not being a trap."""
    result = _run_with_backend(
        "postgres",
        "from src import db, db_postgres\n"
        "assert db is db_postgres\n"
        "print('OK')",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_backend_selection_is_case_insensitive():
    result = _run_with_backend(
        "PostgreSQL",
        "from src import db, db_postgres\n"
        "assert db is db_postgres\n"
        "print('OK')",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_unknown_backend_raises_clear_error():
    result = _run_with_backend("sqlite", "from src import db")
    assert result.returncode != 0
    assert "Unknown DB_BACKEND" in result.stderr
    assert "sqlite" in result.stderr


def test_dispatched_module_exposes_full_expected_surface():
    """Whichever backend gets selected, `src.db` must expose the same
    function surface -- the whole point of the two-file architecture is
    that main.py/alerts.py/dashboard.py never need to know which one is
    active."""
    expected_names = [
        "MonitorState", "init_pool", "close_pool", "init_schema",
        "load_state", "save_state", "insert_check", "get_checks_paginated",
        "get_all_checks_paginated", "get_recent_checks",
        "load_states", "get_last_response_ms_many", "get_uptime_pct_many",
        "get_hourly_bar_statuses",
        "prune_old_checks", "get_all_monitor_names", "transaction",
        "delete_monitor", "get_monitor_stats", "get_aggregate_stats",
        "get_response_time_series", "get_hourly_bars",
        "get_server_version", "has_system_notice", "record_system_notice",
    ]
    code = (
        "from src import db\n"
        + "\n".join(f"assert hasattr(db, {n!r}), {n!r}" for n in expected_names)
        + "\nprint('OK')"
    )
    for backend in ("mariadb", "postgresql"):
        result = _run_with_backend(backend, code)
        assert result.returncode == 0, f"{backend}: {result.stderr}"
        assert "OK" in result.stdout
