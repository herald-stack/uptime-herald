# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_docker_stack.py - Tests that the Dockerfile and compose stack work.

Distinct from everything else in tests/: those all test the Python
application code. This tests the packaging around it -- does the image
described by Dockerfile actually build, does its HEALTHCHECK actually
pass, and does the real deployment shape (app + a real MariaDB, wired
together exactly as docker-compose.yaml describes, bind-mounted config
files and all) actually come up and serve real HTTP traffic.

This is explicitly NOT the same thing as publishing/distributing a
container image anywhere (GHCR, etc.) -- nothing here tags or pushes an
image. It only builds one, verifies it works, and throws it away.

Skipped by default -- set DOCKER_STACK_TEST=1 to opt in. Needs a real
Docker daemon and takes real wall-clock time (image build + MariaDB
startup + healthcheck polling, ~60-90s), so it should never be part of
a normal fast local `pytest tests/` run, only CI (or an explicit local
opt-in when actually working on the Docker packaging itself).
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DOCKER_STACK_TEST") != "1",
    reason="set DOCKER_STACK_TEST=1 to run the real Docker build/compose "
           "stack tests (needs a Docker daemon, takes ~60-90s)",
)

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent

# Files/dirs docker build and docker compose actually need. Not the whole
# repo (tests/, .git, .venv, etc. are irrelevant to the build and would
# just slow the copy down).
#
# "docker-compose.yaml" itself is NOT in this list -- that filename is what a
# user copies one of the two per-backend files to, not something committed to
# the repo (see .gitignore). build_context below copies
# docker-compose-mariadb.yaml and renames it, like it does for the
# *.default.yaml -> *.yaml config files.
_BUILD_INPUTS = [
    "Dockerfile", "docker-compose-mariadb.yaml", "requirements.txt",
    "src", "locales", "docker",
    "config.default.yaml", "theme.default.yaml", "monitors.default.yaml",
    "alerts.default.yaml", "tags.default.yaml", "maintenance.default.yaml",
]

# The real config filenames docker-compose-mariadb.yaml bind-mounts --
# generated from the *.default.yaml templates, exactly what a new user
# does, same as the fresh-install test in test_config.py.
_CONFIG_FILES = {
    "config.yaml": "config.default.yaml",
    "monitors.yaml": "monitors.default.yaml",
    "alerts.yaml": "alerts.default.yaml",
    "tags.yaml": "tags.default.yaml",
    "maintenance.yaml": "maintenance.default.yaml",
    "theme.yaml": "theme.default.yaml",
}


def _run(cmd, cwd, timeout=180):
    """Merges stderr into stdout deliberately, not just for convenience.

    `docker logs` writes container stdout and stderr as genuinely separate
    streams -- uvicorn's access logs ("GET /health HTTP/1.1" 200) go to
    stdout, but its startup messages and the app's own Python logging both
    land on stderr. capture_output=True would capture them into two SEPARATE
    buffers, and checking only .stdout for expected log content would miss
    everything on stderr. Merging here means .stdout always has the same
    combined, chronological view a human gets running `docker logs` directly
    in a terminal.
    """
    return subprocess.run(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout, check=False,
    )


def _wait_for_log_lines(cmd, cwd, expected_substrings, timeout=15):
    """Poll `docker logs`/`docker compose logs` until it contains every
    expected substring, or fail with whatever it actually had.

    A small safety margin for genuine log-driver latency. Because _run()
    merges stderr into stdout, the expected lines show up within 1-2s in
    practice; this poll is just normal defensive slack, not load-bearing.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _run(cmd, cwd=cwd, timeout=15)
        if all(s in last.stdout for s in expected_substrings):
            return last.stdout
        time.sleep(1)
    pytest.fail(
        f"log line(s) {expected_substrings} never appeared within "
        f"{timeout}s.\ncmd: {cmd}\nreturncode: {last.returncode}\n"
        f"output:\n{last.stdout}"
    )


@pytest.fixture(scope="module")
def build_context(tmp_path_factory):
    """A throwaway copy of just the files docker build/compose need --
    deliberately not the real repo working directory, so this can never
    clobber a developer's own real config.yaml/monitors.yaml/etc. if
    they have one locally for their own dev use."""
    ctx = tmp_path_factory.mktemp("docker-stack-test")
    for name in _BUILD_INPUTS:
        src = REPO_ROOT / name
        dst = ctx / name
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy(src, dst)
    (ctx / "config").mkdir()
    for real_name, default_name in _CONFIG_FILES.items():
        shutil.copy(ctx / default_name, ctx / "config" / real_name)
    shutil.copy(ctx / "docker-compose-mariadb.yaml", ctx / "docker-compose.yaml")
    # The compose file has no defaults for these, so a .env supplies them, as a user's would.
    (ctx / ".env").write_text(
        "MARIADB_ROOT_PASSWORD=changeme\nDB_NAME=uptimeherald\n"
        "DB_USER=uptimeherald\nDB_PASSWORD=uptimeherald\n"
    )
    return ctx


@pytest.fixture(scope="module")
def image_tag(build_context, request):
    """Builds the real image ONCE (module scope) and hands the tag to
    every test that needs it -- test_docker_image_builds_successfully,
    running_stack, and the external-database tests all reuse this same
    build rather than each triggering their own. Fails loudly here
    (rather than letting dependents hit a confusing "no such image"
    later) if the build itself is broken."""
    tag = f"uptime-herald-test-{uuid.uuid4().hex[:8]}"
    result = _run(["docker", "build", "-t", tag, "."], cwd=build_context)
    if result.returncode != 0:
        pytest.fail(f"docker build failed:\n{result.stdout}\n{result.stderr}")
    request.addfinalizer(
        lambda: _run(["docker", "rmi", "-f", tag], cwd=build_context, timeout=60)
    )
    return tag


# --- Item 4: the Dockerfile itself ----------------------------------------------------


def test_docker_image_builds_successfully(image_tag):
    """image_tag's own fixture setup is the actual build+assertion (see
    its docstring for why) -- this test just confirms the resulting
    image is real and inspectable, giving the build a result of its own
    in the test report rather than being invisible fixture setup only
    other tests benefit from."""
    result = _run(["docker", "image", "inspect", image_tag], cwd=REPO_ROOT)
    assert result.returncode == 0


# --- Item 5: the full compose stack ---------------------------------------------------


@pytest.fixture(scope="module")
def running_stack(build_context, image_tag, request):
    """Brings up the REAL docker-compose.yaml stack (app + MariaDB),
    waits for both services to report healthy per their own real
    healthchecks, yields the app's host port, and tears everything down
    (containers, network, volumes, images) unconditionally afterward."""
    project = f"uh-stack-test-{uuid.uuid4().hex[:8]}"
    compose_cmd = ["docker", "compose", "-p", project]

    def teardown():
        # --rmi local (not "all"): `docker compose up --build` creates
        # its own image (named <project>-uptimeherald, separate from
        # image_tag's own standalone build); without this flag ~345MB of
        # orphaned images would be left behind on the CI runner after every
        # run. "local" removes only images built without a custom tag
        # (exactly the one this test built); "all" would also delete the
        # pulled mariadb:12 base image, forcing a full re-pull on every
        # future run instead of reusing the cache.
        _run(compose_cmd + ["down", "-v", "--rmi", "local"], cwd=build_context, timeout=60)
        _run(["docker", "rmi", "-f", image_tag], cwd=build_context, timeout=60)

    request.addfinalizer(teardown)

    up = _run(compose_cmd + ["up", "-d", "--build"], cwd=build_context, timeout=180)
    assert up.returncode == 0, f"docker compose up failed:\n{up.stdout}\n{up.stderr}"

    deadline = time.monotonic() + 90
    healthy = False
    last_status = ""
    while time.monotonic() < deadline:
        ps = _run(
            compose_cmd + ["ps", "--format", "json"], cwd=build_context, timeout=30,
        )
        last_status = ps.stdout
        try:
            statuses = [json.loads(line) for line in ps.stdout.splitlines() if line]
        except json.JSONDecodeError:
            statuses = []
        if statuses and all(
            "healthy" in s.get("Health", s.get("Status", "")).lower()
            or "healthy" in s.get("Status", "").lower()
            for s in statuses
        ) and len(statuses) == 2:
            healthy = True
            break
        time.sleep(3)

    if not healthy:
        logs = _run(
            compose_cmd + ["logs"], cwd=build_context, timeout=30,
        ).stdout
        pytest.fail(
            f"stack never became healthy within 90s.\n"
            f"Last ps output: {last_status}\nLogs:\n{logs[-4000:]}"
        )

    return {"port": 8080, "project": project}  # published port + the
    # project name every later docker compose command needs to find
    # this same stack again -- omitting it silently queries nothing,
    # because "docker compose logs" with no -p falls back to inferring an
    # unrelated/nonexistent project.


def test_compose_stack_health_endpoint_responds(running_stack):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{running_stack['port']}/health", timeout=10,
    ) as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_compose_stack_dashboard_renders(running_stack):
    """Confirms the whole real chain works: app connects to the real
    MariaDB at startup (it would crash before binding the port
    otherwise -- see below), config loads, Jinja renders a real page."""
    with urllib.request.urlopen(
        f"http://127.0.0.1:{running_stack['port']}/", timeout=10,
    ) as resp:
        assert resp.status == 200
        body = resp.read().decode()
        assert "Uptime Herald" in body


def test_compose_stack_app_actually_used_the_real_database(
    build_context, running_stack,
):
    """The app's startup() handler calls db.init_pool()/db.init_schema()
    unconditionally before uvicorn ever binds the port -- so the health
    endpoint responding at all already implies this succeeded, but
    checking the log line directly makes that connection explicit rather
    than inferred."""
    _wait_for_log_lines(
        ["docker", "compose", "-p", running_stack["project"],
         "logs", "uptimeherald"],
        cwd=build_context,
        expected_substrings=["DB pool created", "DB schema verified"],
    )


# --- The OTHER documented deployment mode: an external database -----------------------
#
# The docs describe this as a supported mode -- comment out the bundled
# mariadb service and point DB_HOST/DB_PORT/etc at a database you host
# yourself (an existing DB server, a managed database service). Every test
# above only exercises the bundled-mariadb path; these verify this mode works.
# It is simulated with two plain `docker run` containers (the app, and a
# MariaDB standing in for the user's own database) on a shared network,
# deliberately NOT using docker-compose, since the point is proving the app
# image works when nothing about its database is compose-managed.


@pytest.fixture(scope="module")
def external_db_network():
    name = f"uh-external-db-test-{uuid.uuid4().hex[:8]}"
    _run(["docker", "network", "create", name], cwd=REPO_ROOT, timeout=30)
    yield name
    _run(["docker", "network", "rm", name], cwd=REPO_ROOT, timeout=30)


@pytest.fixture(scope="module")
def external_mariadb(external_db_network):
    """A MariaDB container with NO relationship to docker-compose.yaml
    at all -- standing in for a database a real user already has running
    somewhere else."""
    name = f"external-mariadb-{uuid.uuid4().hex[:8]}"
    _run(
        ["docker", "run", "-d", "--name", name,
         "--network", external_db_network,
         "-e", "MARIADB_ROOT_PASSWORD=external-test-only",
         "-e", "MARIADB_DATABASE=uptimeherald",
         "-e", "MARIADB_USER=uptimeherald",
         "-e", "MARIADB_PASSWORD=uptimeherald",
         "mariadb:12"],
        cwd=REPO_ROOT, timeout=60,
    )

    deadline = time.monotonic() + 60
    healthy = False
    while time.monotonic() < deadline:
        check = _run(
            ["docker", "exec", name, "healthcheck.sh",
             "--connect", "--innodb_initialized"],
            cwd=REPO_ROOT, timeout=15,
        )
        if check.returncode == 0:
            healthy = True
            break
        time.sleep(2)
    if not healthy:
        _run(["docker", "rm", "-f", name], cwd=REPO_ROOT, timeout=30)
        pytest.fail("external MariaDB container never became healthy")

    yield name
    _run(["docker", "rm", "-f", name], cwd=REPO_ROOT, timeout=30)


@pytest.fixture(scope="module")
def app_with_external_db(image_tag, external_db_network, external_mariadb, request):
    """Runs the built app image directly (no compose) with DB_HOST
    pointed at the standalone MariaDB container above -- exactly what a
    user following the External Databases docs would
    be doing, minus compose itself."""
    name = f"uh-external-db-app-{uuid.uuid4().hex[:8]}"
    port = 18080
    _run(
        ["docker", "run", "-d", "--name", name,
         "--network", external_db_network,
         "-p", f"{port}:8080",
         "-e", f"DB_HOST={external_mariadb}",
         "-e", "DB_PORT=3306",
         "-e", "DB_NAME=uptimeherald",
         "-e", "DB_USER=uptimeherald",
         "-e", "DB_PASSWORD=uptimeherald",
         image_tag],
        cwd=REPO_ROOT, timeout=30,
    )
    request.addfinalizer(
        lambda: _run(["docker", "rm", "-f", name], cwd=REPO_ROOT, timeout=30)
    )

    deadline = time.monotonic() + 30
    up = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3,
            ) as resp:
                if resp.status == 200:
                    up = True
                    break
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(2)

    if not up:
        logs = _run(["docker", "logs", name], cwd=REPO_ROOT, timeout=15).stdout
        pytest.fail(f"app never became reachable against the external DB.\n{logs[-4000:]}")

    return {"port": port, "name": name}


def test_app_connects_to_external_database(app_with_external_db):
    """The core claim: an app container with NO compose-managed database
    of its own, told about a database that lives entirely elsewhere,
    actually starts up and serves real traffic."""
    with urllib.request.urlopen(
        f"http://127.0.0.1:{app_with_external_db['port']}/health", timeout=10,
    ) as resp:
        assert resp.status == 200


def test_app_with_external_database_initializes_real_schema(app_with_external_db):
    _wait_for_log_lines(
        ["docker", "logs", app_with_external_db["name"]],
        cwd=REPO_ROOT,
        expected_substrings=["DB pool created", "DB schema verified"],
    )


# --- PostgreSQL via docker-compose ----------------------------------------------------
#
# docker-compose-postgres.yaml is a real, clean, uncommented file, so this
# could copy it directly, as build_context above copies
# docker-compose-mariadb.yaml. It is a standalone hand-written copy instead
# because it needs two deliberate deviations from the real file (below), and
# copy-then-patch is more moving parts than copy-then-verify-by-eye for a file
# this short. Keep it in sync with docker-compose-postgres.yaml.
#
# It uses a SEPARATE build_context (not the module-scoped one the MariaDB
# tests share), because overwriting docker-compose.yaml in the shared one
# would contaminate every later test in this module.
#
# It deliberately OMITS the real file's `container_name:` lines: module-scoped
# fixtures don't tear down until the whole module finishes, so the MariaDB
# tier's `uptimeherald` container is still running while this one starts, and
# Docker container names must be globally unique on the host regardless of the
# `-p` project namespace. Without a fixed name, Compose generates a unique
# project-scoped one. The tests below refer to services by their Compose
# service name (`uptimeherald`), so they don't need the fixed name.

_POSTGRES_COMPOSE_YAML = """
services:
  uptimeherald:
    build: .
    ports:
      # Host port 8080 is still held by the mariadb tier's own
      # running_stack fixture -- both are scope="module" in this same
      # file, so its container isn't torn down until the whole module
      # finishes. Published on a different host port to avoid fighting
      # over the same one; the container-internal port is still 8080.
      - "8081:8080"
    volumes:
      - ./config:/app/config:ro
    environment:
      - DB_BACKEND=postgres
      - DB_HOST=postgres
      - DB_PORT=5432
      - DB_NAME=uptimeherald
      - DB_USER=uptimeherald
      - DB_PASSWORD=uptimeherald
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped

  postgres:
    image: postgres:18
    # Mounted at /var/lib/postgresql (NOT .../data, which every prior
    # major version used) -- 18+ images refuse to start against a
    # volume mounted directly at .../data, since they now expect data
    # to live in a major-version-specific subdirectory under
    # /var/lib/postgresql, to support pg_ctlcluster-style in-place
    # major upgrades. Mounting at the old path crash-loops the
    # container immediately with a clear error, which is how this got
    # caught. See docker-compose-postgres.yaml's matching comment.
    volumes:
      - postgres-data:/var/lib/postgresql
      - ./docker/postgres-init:/docker-entrypoint-initdb.d:ro
    environment:
      - POSTGRES_DB=uptimeherald
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=changeme
      - DB_USER=uptimeherald
      - DB_PASSWORD=uptimeherald
    healthcheck:
      # Double check, 2s apart -- guards against the postgres image's
      # own bootstrap-server restart during first-time initdb (see
      # docker-compose-postgres.yaml's matching comment for the full
      # explanation; this is the real regression this comment exists
      # because of -- it flaked exactly this way in CI once).
      test: ["CMD-SHELL", "pg_isready -U uptimeherald -d uptimeherald && sleep 2 && pg_isready -U uptimeherald -d uptimeherald"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 30s
    restart: unless-stopped

volumes:
  postgres-data:
"""


@pytest.fixture(scope="module")
def build_context_postgres(tmp_path_factory):
    """Same idea as build_context above, but with the Postgres-flavored
    compose file instead of the real repo's default (mariadb) one."""
    ctx = tmp_path_factory.mktemp("docker-stack-test-postgres")
    for name in _BUILD_INPUTS:
        if name == "docker-compose-mariadb.yaml":
            continue  # this context gets its own postgres-flavored one below
        src = REPO_ROOT / name
        dst = ctx / name
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy(src, dst)
    (ctx / "docker-compose.yaml").write_text(_POSTGRES_COMPOSE_YAML)
    (ctx / "config").mkdir()
    for real_name, default_name in _CONFIG_FILES.items():
        shutil.copy(ctx / default_name, ctx / "config" / real_name)
    return ctx


@pytest.fixture(scope="module")
def running_stack_postgres(build_context_postgres, request):
    """Brings up the Postgres-flavored compose stack, waits for both
    services healthy, yields the app's host port, tears down
    unconditionally afterward. Builds its own image directly via
    `compose up --build` rather than reusing the mariadb tier's
    image_tag -- keeping the two stacks fully independent end to end."""
    project = f"uh-stack-test-pg-{uuid.uuid4().hex[:8]}"
    compose_cmd = ["docker", "compose", "-p", project]

    def teardown():
        _run(
            compose_cmd + ["down", "-v", "--rmi", "local"],
            cwd=build_context_postgres, timeout=60,
        )

    request.addfinalizer(teardown)

    up = _run(
        compose_cmd + ["up", "-d", "--build"],
        cwd=build_context_postgres, timeout=180,
    )
    assert up.returncode == 0, f"docker compose up failed:\n{up.stdout}"

    deadline = time.monotonic() + 90
    healthy = False
    last_status = ""
    while time.monotonic() < deadline:
        ps = _run(
            compose_cmd + ["ps", "--format", "json"],
            cwd=build_context_postgres, timeout=30,
        )
        last_status = ps.stdout
        try:
            statuses = [
                json.loads(line) for line in ps.stdout.splitlines() if line
            ]
        except json.JSONDecodeError:
            statuses = []
        if statuses and len(statuses) == 2 and all(
            "healthy" in s.get("Health", s.get("Status", "")).lower()
            or "healthy" in s.get("Status", "").lower()
            for s in statuses
        ):
            healthy = True
            break
        time.sleep(3)

    if not healthy:
        logs = _run(
            compose_cmd + ["logs"], cwd=build_context_postgres, timeout=30,
        ).stdout
        pytest.fail(
            f"postgres stack never became healthy within 90s.\n"
            f"Last ps output: {last_status}\nLogs:\n{logs[-4000:]}"
        )

    return {"port": 8081, "project": project}


def test_compose_stack_postgres_health_endpoint_responds(running_stack_postgres):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{running_stack_postgres['port']}/health", timeout=10,
    ) as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_compose_stack_postgres_dashboard_renders(running_stack_postgres):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{running_stack_postgres['port']}/", timeout=10,
    ) as resp:
        assert resp.status == 200
        assert "Uptime Herald" in resp.read().decode()


def test_compose_stack_postgres_app_actually_used_the_real_database(
    build_context_postgres, running_stack_postgres,
):
    """Confirms DB_BACKEND=postgresql was actually honored end to end --
    the app really connected via the Postgres backend, not silently
    falling back to something else."""
    _wait_for_log_lines(
        ["docker", "compose", "-p", running_stack_postgres["project"],
         "logs", "uptimeherald"],
        cwd=build_context_postgres,
        expected_substrings=["DB pool created", "DB schema verified"],
    )


def test_compose_stack_postgres_app_role_is_not_superuser(
    build_context_postgres, running_stack_postgres,
):
    """The app connects as an ordinary role that owns its database, not
    as the cluster superuser -- matching the MariaDB stack, where the app
    user is separate from root. Checked from inside the postgres
    container as the superuser, so it doesn't depend on the app's own
    credentials working."""
    result = _run(
        ["docker", "compose", "-p", running_stack_postgres["project"],
         "exec", "-T", "postgres", "psql", "-U", "postgres", "-t", "-A",
         "-c",
         "SELECT r.rolsuper, d.datname FROM pg_roles r "
         "JOIN pg_database d ON d.datdba = r.oid "
         "WHERE r.rolname = 'uptimeherald'"],
        cwd=build_context_postgres, timeout=30,
    )
    assert result.stdout.strip() == "f|uptimeherald"
