# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_postgres_init_image.py - Tests for the postgres-init image and the compose file.

The bundled PostgreSQL stack's one-shot postgres-init service runs its own
image (docker/postgres-init/Dockerfile), not the app image. These tests keep
the compose files, that Dockerfile and the app Dockerfile agreeing about
which files live where.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
INIT_DOCKERFILE = ROOT / "docker" / "postgres-init" / "Dockerfile"
APP_DOCKERFILE = ROOT / "Dockerfile"
DEPLOY_COMPOSE = ROOT / "deploy" / "postgres" / "docker-compose.yaml"
DEV_COMPOSE = ROOT / "docker-compose-postgres.yaml"


def _copies(dockerfile: Path) -> list[tuple[str, str]]:
    """(source, destination) of every COPY line."""
    out = []
    for line in dockerfile.read_text().splitlines():
        m = re.match(r"COPY\s+(\S+)\s+(\S+)\s*$", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def _entrypoint_text(compose: Path) -> str:
    entry = yaml.safe_load(compose.read_text())["services"]["postgres-init"]["entrypoint"]
    return " ".join(entry)


def test_every_file_the_init_image_copies_exists_in_the_repo():
    copies = _copies(INIT_DOCKERFILE)
    assert copies, "no COPY lines found"
    for src, _dst in copies:
        assert (ROOT / src).is_file(), src


def test_the_paths_the_compose_files_copy_from_are_in_the_init_image():
    destinations = {dst for _src, dst in _copies(INIT_DOCKERFILE)}
    for compose in (DEPLOY_COMPOSE, DEV_COMPOSE):
        paths = re.findall(r"/app/\S+", _entrypoint_text(compose))
        assert paths, compose
        for path in paths:
            assert path in destinations, f"{compose.name}: {path}"


def test_the_deploy_compose_runs_the_init_image_not_the_app_image():
    services = yaml.safe_load(DEPLOY_COMPOSE.read_text())["services"]
    init = services["postgres-init"]["image"]
    assert re.fullmatch(r".+/uptimeherald-postgres-init:\S+", init), init
    assert init.split(":")[-1] == services["uptimeherald"]["image"].split(":")[-1]


def test_the_dev_compose_builds_the_init_image():
    build = yaml.safe_load(DEV_COMPOSE.read_text())["services"]["postgres-init"]["build"]
    assert build["dockerfile"] == "docker/postgres-init/Dockerfile"


def test_the_app_image_no_longer_carries_the_init_files():
    for src, dst in _copies(APP_DOCKERFILE):
        assert "postgres-init" not in src + dst
        assert "upgrade-postgres" not in src + dst
