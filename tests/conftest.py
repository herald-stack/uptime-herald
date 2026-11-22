# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
conftest.py - Fixtures shared by the tests.
"""

import os
import urllib.error
import urllib.request
import warnings
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# --- Default files from get.uptimeherald.org ------------------------------------------
#
# A few tests read the default config files and the starter PostgreSQL compose
# file, which are published on get.uptimeherald.org rather than kept in this
# repository. Before the first test, any of them missing from the repository
# folder is downloaded from the folder for this version (VERSION in
# src/config.py). The project's own CI copies the files in first, so it
# downloads nothing. The downloaded files are ignored by git.

_DEFAULT_FILES = {  # path in the repository -> path under releases/<version>/
    "alerts.default.yaml": "config/alerts.default.yaml",
    "config.default.yaml": "config/config.default.yaml",
    "maintenance.default.yaml": "config/maintenance.default.yaml",
    "monitors.default.yaml": "config/monitors.default.yaml",
    "tags.default.yaml": "config/tags.default.yaml",
    "theme.default.yaml": "config/theme.default.yaml",
    "deploy/postgres/docker-compose.yaml": "compose/postgres/docker-compose.yaml",
}


@pytest.fixture(scope="session", autouse=True)
def default_files():
    from src.config import VERSION

    root = Path(__file__).resolve().parent.parent
    for name, remote in _DEFAULT_FILES.items():
        target = root / name
        if target.exists():
            continue
        url = f"https://get.uptimeherald.org/releases/{VERSION}/{remote}"
        # The server turns away Python's default client name, so send our own.
        request = urllib.request.Request(  # noqa: S310
            url, headers={"User-Agent": "uptimeherald-tests"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:  # noqa: S310
                data = reply.read()
        except (urllib.error.URLError, TimeoutError) as error:
            warnings.warn(f"could not download {url}: {error}", stacklevel=1)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


# --- Browser-tier gating --------------------------------------------------------------
#
# Shared by test_dashboard_js.py and test_responsive.py.
#
# Pure stdlib on purpose: this must NOT import playwright. Both browser
# files document why (the sync Playwright API's greenlet bridge corrupts
# asyncio's event-loop state for every async test collected afterwards if
# it's touched outside pytest-playwright's own fixtures), and merely
# importing it here would pull that into every tier.


def chromium_search_roots() -> list[Path]:
    """Where Playwright may have installed its browsers.

    PLAYWRIGHT_BROWSERS_PATH is Playwright's documented override. Ignoring it
    would report "not installed" whenever browsers live anywhere but the
    default cache, and because a missing browser SKIPS rather than fails,
    that would produce a fully green run in which every browser test had
    quietly done nothing.
    Honour the override the same way Playwright itself does:
      - unset            -> the per-user default cache directory
      - "0"              -> installed next to the playwright package
      - any other value  -> that directory
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env == "0":
        try:
            import playwright  # noqa: PLC0415 - optional, path lookup only
            return [Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"]
        except Exception:
            return []
    if env:
        return [Path(env)]
    return [Path.home() / ".cache" / "ms-playwright"]


def chromium_is_installed() -> bool:
    """True if a Chromium build is present on disk.

    Deliberately a disk check, never a real sync_playwright() launch at
    collection time -- see the module comment above for what that breaks.
    The executable's parent directory name varies by architecture
    (chrome-linux64 on x86_64, chrome-linux on ARM), so glob for the
    binary rather than hardcoding one platform's layout. headless_shell
    counts too: newer Playwright runs headless Chromium from it.
    """
    for root in chromium_search_roots():
        if not root.is_dir():
            continue
        if any(root.glob("chromium-*/chrome-*/chrome")):
            return True
        if any(root.glob("chromium_headless_shell-*/chrome-*/headless_shell")):
            return True
    return False


def browser_tier_skip_reason() -> str | None:
    """None if the browser tier should run, else the reason to skip.

    Set REQUIRE_BROWSER_TESTS=1 to turn a missing browser into a hard
    collection error instead of a skip. CI sets it, so the tier can never
    silently pass by testing nothing; a local run without Chromium still
    just skips.
    """
    if chromium_is_installed():
        return None
    searched = ", ".join(str(p) for p in chromium_search_roots())
    if os.environ.get("REQUIRE_BROWSER_TESTS") == "1":
        raise RuntimeError(
            f"REQUIRE_BROWSER_TESTS=1 but Playwright's Chromium was not "
            f"found (searched: {searched}). These tests would otherwise "
            f"skip green and report nothing. Run "
            f"`playwright install --with-deps chromium`, or unset "
            f"REQUIRE_BROWSER_TESTS to allow skipping."
        )
    return (
        f"Playwright's Chromium isn't installed (searched: {searched}) -- "
        f"run `playwright install chromium` to enable these tests. Set "
        f"REQUIRE_BROWSER_TESTS=1 to make this a failure instead of a skip."
    )


@pytest.fixture
def sample_config():
    """Return a minimal valid config dict for tests."""
    return {
        "global": {
            "check_interval_minutes": 1,
            "retention_days": 180,
            "alert_cooldown_minutes": 5,
        },
        "alerts": {
            "email": {"recipients": []},
            "push_alerts": [],
        },
        "monitors": [],
        "tags": [],
        "maintenance": [],
    }


# --- Dashboard database calls ---------------------------------------------------------

_uptime_pct_holder: dict = {"mock": None}


@contextmanager
def uptime_pct_mock(return_value=None):
    """The per-monitor uptime % the dashboard tests want from the database.

    Yields an AsyncMock that bulk_db_calls() consults for each monitor (it is
    called as mock(name, days, decimals)), so a test can set its
    return_value or assert how it was called.
    """
    mock = AsyncMock(return_value=return_value)
    previous = _uptime_pct_holder["mock"]
    _uptime_pct_holder["mock"] = mock
    try:
        yield mock
    finally:
        _uptime_pct_holder["mock"] = previous


@contextmanager
def bulk_db_calls(load_state, last_response, hourly_bars):
    """Make the dashboard's bulk database calls answer from per-monitor mocks.

    The monitor list asks the database for all its monitors at once
    (load_states, get_last_response_ms_many, get_hourly_bar_statuses,
    get_uptime_pct_many). Most dashboard tests only care what one monitor
    gets back, so they set up per-monitor mocks and this adapter routes the
    bulk calls through them: each takes the names it was given and asks the
    matching mock for each. The uptime one asks the mock of the enclosing
    uptime_pct_mock(), if a test opened one (None per monitor otherwise).
    """
    async def load_states(monitors):
        return {
            name: await load_state(name, monitor_type)
            for name, monitor_type in monitors
        }

    async def last_response_ms_many(names):
        return {name: await last_response(name) for name in names}

    async def hourly_bar_statuses(names, hours, now=None):
        return {
            name: [
                bar["status"]
                for bar in await hourly_bars(
                    name, hours=hours, display_tz=None, now=now
                )
            ]
            for name in names
        }

    async def uptime_pct_many(names, days, decimals=2):
        mock = _uptime_pct_holder["mock"]
        return {
            name: await mock(name, days, decimals) if mock else None
            for name in names
        }

    with patch("src.dashboard.db.load_states", load_states), \
         patch("src.dashboard.db.get_last_response_ms_many", last_response_ms_many), \
         patch("src.dashboard.db.get_hourly_bar_statuses", hourly_bar_statuses), \
         patch("src.dashboard.db.get_uptime_pct_many", uptime_pct_many):
        yield
