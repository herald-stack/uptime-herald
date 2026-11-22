# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_upgrade_paths.py - Tests that databases with real data upgrade between versions.

Upgrade-path tests: move a database with real data from one supported
version to the next, the way a user would, and check nothing was lost and
the app's own startup code still works on the result.

Each case is one hop -- every older supported version to every newer one --
with fresh data in throwaway containers:

  * PostgreSQL goes through scripts/upgrade-postgres.sh, because a Postgres
    major version can't run against the previous one's data directory.
  * MariaDB is upgraded in place by swapping the image tag; the image's
    MARIADB_AUTO_UPGRADE=1 runs mariadb-upgrade over the existing data.

Skipped by default -- set UPGRADE_PATH_TEST=1 to opt in. Needs a real
Docker daemon with the compose plugin and network access to pull images
(CI sets it).
"""

import itertools
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("UPGRADE_PATH_TEST") != "1",
    reason="set UPGRADE_PATH_TEST=1 to run the real database upgrade tests "
           "(needs Docker and network access)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
UPGRADE_SCRIPT = REPO_ROOT / "scripts" / "upgrade-postgres.sh"

# Every older -> newer combination of the supported versions, not just
# neighbours: a user can be on any supported version and move to any newer
# one, and "A to C works" isn't implied by "A to B and B to C work".
PG_VERSIONS = [15, 16, 17, 18, 19]
MARIADB_VERSIONS = ["10.11", "11.4", "11.8", "12.3", "latest"]
PG_PAIRS = list(itertools.combinations(PG_VERSIONS, 2))
MARIADB_PAIRS = list(itertools.combinations(MARIADB_VERSIONS, 2))

APP_USER = "uptimeherald"
APP_PASSWORD = "uptimeherald"
APP_DB = "uptimeherald"
ROOT_PASSWORD = "upgrade-test-only"

MONITORS = 5
CHECKS = 300


def _run(cmd, cwd=None, timeout=300, env=None):
    """stderr merged into stdout so failures show one chronological log."""
    return subprocess.run(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False,
    )


def _pull(stack, attempts=3):
    """`docker compose pull`, retried: Docker Hub's anonymous token endpoint
    occasionally times out, which says nothing about the upgrade under test."""
    result = None
    for attempt in range(attempts):
        result = stack.compose("pull", "--quiet", timeout=600)
        if result.returncode == 0:
            return
        time.sleep(10 * (attempt + 1))
    pytest.fail(f"image pull kept failing:\n{result.stdout}")


class Stack:
    """One throwaway compose project in its own directory."""

    def __init__(self, tmp_path: Path, prefix: str):
        self.dir = tmp_path
        self.project = f"{prefix}-{uuid.uuid4().hex[:8]}"

    def compose(self, *args, timeout=300):
        return _run(["docker", "compose", *args], cwd=self.dir, timeout=timeout)

    def destroy(self):
        self.compose("down", "-v", "--remove-orphans")

    def container(self, service: str) -> str:
        cid = self.compose("ps", "-q", service).stdout.strip()
        assert cid, f"no running {service} container"
        return cid

    def wait_healthy(self, service: str, timeout=180):
        deadline = time.monotonic() + timeout
        status = "unknown"
        while time.monotonic() < deadline:
            cid = self.compose("ps", "-q", service).stdout.strip()
            if cid:
                status = _run(
                    ["docker", "inspect", "-f",
                     "{{if .State.Health}}{{.State.Health.Status}}"
                     "{{else}}{{.State.Status}}{{end}}", cid],
                ).stdout.strip()
                if status == "healthy":
                    return
            time.sleep(2)
        logs = self.compose("logs", "--tail", "30", service).stdout
        pytest.fail(f"{service} never became healthy ({status}):\n{logs}")

    def host_port(self, service: str, container_port: int) -> str:
        out = self.compose("port", service, str(container_port)).stdout.strip()
        match = re.search(r":(\d+)\s*$", out.splitlines()[0]) if out else None
        assert match, f"couldn't find the published port: {out!r}"
        return match.group(1)


def _app_schema(backend: str, port: str) -> None:
    """Run the app's own startup path (pool + schema creation/migrations)
    against the database, exactly as the app does on boot."""
    code = (
        "import asyncio\n"
        "from src import db\n"
        "async def main():\n"
        "    await db.init_pool()\n"
        "    await db.init_schema()\n"
        "    await db.close_pool()\n"
        "asyncio.run(main())\n"
        "print('app startup ok')\n"
    )
    env = {
        **os.environ, "DB_BACKEND": backend, "DB_HOST": "127.0.0.1",
        "DB_PORT": port, "DB_NAME": APP_DB, "DB_USER": APP_USER,
        "DB_PASSWORD": APP_PASSWORD, "PYTHONPATH": str(REPO_ROOT),
    }
    result = _run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env, timeout=60)
    assert result.returncode == 0 and "app startup ok" in result.stdout, result.stdout


def _seed_sql(table_ts: str) -> str:
    monitors = ",".join(f"('m{i}','http')" for i in range(1, MONITORS + 1))
    checks = ",".join(
        f"('m{(i % MONITORS) + 1}',{table_ts},'up')" for i in range(CHECKS)
    )
    return (
        f"INSERT INTO monitors (name, type) VALUES {monitors};"
        f"INSERT INTO checks (monitor_name, check_time, status) VALUES {checks};"
    )


# --- PostgreSQL -----------------------------------------------------------------------

def _pg_compose(project: str, major: int) -> str:
    mount = "/var/lib/postgresql" if major >= 18 else "/var/lib/postgresql/data"
    return f"""name: {project}
services:
  postgres:
    image: postgres:{major}
    container_name: {project}-postgres
    ports: ["127.0.0.1::5432"]
    volumes:
      - postgres-data:{mount}
      - ./postgres-init:/docker-entrypoint-initdb.d:ro
    environment:
      - POSTGRES_DB={APP_DB}
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD={ROOT_PASSWORD}
      - DB_USER={APP_USER}
      - DB_PASSWORD={APP_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U {APP_USER} -d {APP_DB} && sleep 2 && pg_isready -U {APP_USER} -d {APP_DB}"]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 15s
volumes:
  postgres-data:
"""


def _pg_sql(stack: Stack, sql: str, db: str = APP_DB) -> str:
    result = stack.compose(
        "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", db,
        "-v", "ON_ERROR_STOP=1", "-tAc", sql,
    )
    assert result.returncode == 0, result.stdout
    return result.stdout.strip()


@pytest.fixture
def pg_stack(tmp_path, request):
    stack = Stack(tmp_path, "uhupg")
    request.addfinalizer(stack.destroy)
    init_dir = tmp_path / "postgres-init"
    init_dir.mkdir()
    script = (REPO_ROOT / "docker" / "postgres-init" / "01-create-app-role.sh")
    (init_dir / script.name).write_bytes(script.read_bytes())
    (init_dir / script.name).chmod(0o755)
    return stack


@pytest.mark.parametrize("old, new", PG_PAIRS, ids=[f"pg{a}-to-pg{b}" for a, b in PG_PAIRS])
def test_postgres_upgrade_script_moves_data_between_majors(pg_stack, old, new):
    stack = pg_stack
    (stack.dir / "docker-compose.yaml").write_text(_pg_compose(stack.project, old))
    _pull(stack)
    assert stack.compose("up", "-d", "--quiet-pull").returncode == 0
    stack.wait_healthy("postgres")

    # Real schema from the app's own startup path, then some history.
    _app_schema("postgres", stack.host_port("postgres", 5432))
    _pg_sql(stack, _seed_sql("now()"))
    assert _pg_sql(stack, "SELECT count(*) FROM checks") == str(CHECKS)

    result = _run(
        [str(UPGRADE_SCRIPT), str(new), "--yes"], cwd=stack.dir, timeout=900,
    )
    assert result.returncode == 0, result.stdout
    assert "the data matches" in result.stdout, result.stdout

    stack.wait_healthy("postgres")
    assert int(_pg_sql(stack, "SHOW server_version_num")) // 10000 == new
    assert _pg_sql(stack, "SELECT count(*) FROM monitors") == str(MONITORS)
    assert _pg_sql(stack, "SELECT count(*) FROM checks") == str(CHECKS)
    # The app role must own the restored tables and still not be a superuser.
    owners = _pg_sql(
        stack, "SELECT string_agg(DISTINCT tableowner, ',') FROM pg_tables "
               "WHERE schemaname = 'public'",
    )
    assert owners == APP_USER
    assert _pg_sql(
        stack, f"SELECT rolsuper FROM pg_roles WHERE rolname = '{APP_USER}'",
    ) == "f"
    assert list((stack.dir / "backups").glob("postgres-*.sql")), "dump was not kept"

    # The app can start on the upgraded database.
    _app_schema("postgres", stack.host_port("postgres", 5432))


# --- MariaDB --------------------------------------------------------------------------

def _maria_compose(project: str, tag: str) -> str:
    return f"""name: {project}
services:
  mariadb:
    image: mariadb:{tag}
    container_name: {project}-mariadb
    ports: ["127.0.0.1::3306"]
    volumes:
      - mariadb-data:/var/lib/mysql
    environment:
      - MARIADB_ROOT_PASSWORD={ROOT_PASSWORD}
      - MARIADB_DATABASE={APP_DB}
      - MARIADB_USER={APP_USER}
      - MARIADB_PASSWORD={APP_PASSWORD}
      - MARIADB_AUTO_UPGRADE=1
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 30s
volumes:
  mariadb-data:
"""


def _maria_sql(stack: Stack, sql: str) -> str:
    result = stack.compose(
        "exec", "-T", "-e", f"MYSQL_PWD={ROOT_PASSWORD}", "mariadb",
        "mariadb", "-uroot", "-N", "-e", sql, APP_DB,
    )
    assert result.returncode == 0, result.stdout
    return result.stdout.strip()


@pytest.fixture
def maria_stack(tmp_path, request):
    stack = Stack(tmp_path, "uhupg")
    request.addfinalizer(stack.destroy)
    return stack


@pytest.mark.parametrize(
    "old, new", MARIADB_PAIRS, ids=[f"maria{a}-to-maria{b}" for a, b in MARIADB_PAIRS],
)
def test_mariadb_upgrades_in_place_between_series(maria_stack, old, new):
    stack = maria_stack
    compose_file = stack.dir / "docker-compose.yaml"
    compose_file.write_text(_maria_compose(stack.project, old))
    _pull(stack)
    assert stack.compose("up", "-d", "--quiet-pull").returncode == 0
    stack.wait_healthy("mariadb")

    _app_schema("mariadb", stack.host_port("mariadb", 3306))
    _maria_sql(stack, _seed_sql("NOW(3)"))
    assert _maria_sql(stack, "SELECT COUNT(*) FROM checks") == str(CHECKS)
    old_version = _maria_sql(stack, "SELECT VERSION()")

    # The upgrade: same data volume, newer image.
    compose_file.write_text(_maria_compose(stack.project, new))
    _pull(stack)
    up = stack.compose("up", "-d", "--quiet-pull")
    assert up.returncode == 0, up.stdout
    stack.wait_healthy("mariadb", timeout=300)

    new_version = _maria_sql(stack, "SELECT VERSION()")
    assert new_version != old_version
    if new != "latest":
        assert new_version.startswith(f"{new}."), new_version
    assert _maria_sql(stack, "SELECT COUNT(*) FROM monitors") == str(MONITORS)
    assert _maria_sql(stack, "SELECT COUNT(*) FROM checks") == str(CHECKS)

    # The app can start on the upgraded database.
    _app_schema("mariadb", stack.host_port("mariadb", 3306))
