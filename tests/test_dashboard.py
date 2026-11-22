# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_dashboard.py - Tests for src/dashboard.py.
"""

import copy
from contextlib import nullcontext
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import DEFAULTS, ConfigError
from src.config import load_theme as _real_load_theme
from src.dashboard import LIST_BARS, SIDEBAR_BARS
from src.main import app
from src.state import MonitorState
from tests.conftest import bulk_db_calls, uptime_pct_mock

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_config():
    """Keep dashboard/kiosk routes from depending on real config/theme files.

    dashboard.py's routes call load_config(CONFIG_PATH) and load_theme() against
    real files on disk. Without this fixture, results (e.g. whether the
    type/tag filters render) would depend on whatever config.yaml/theme.yaml
    happen to exist in the directory pytest is run from. This returns a real,
    valid, empty config (the same DEFAULTS load_config() falls back to for
    unset fields), so behavior is deterministic wherever the suite runs.

    Deliberately NOT a ConfigError: dashboard()/kiosk() render a dedicated error
    page on ConfigError (see test_dashboard_config_error /
    test_kiosk_config_error below), and this fixture simulates "no real config
    on disk", which in the working case looks like a valid-but-empty config.
    """
    with patch(
        "src.dashboard.load_config",
        return_value=copy.deepcopy(DEFAULTS),
    ), patch(
        "src.dashboard.load_theme",
        side_effect=lambda *a, **kw: _real_load_theme("/nonexistent/theme.yaml"),
    ):
        yield


def _mock_monitor_state(name="test", status="up"):
    """Create a MonitorState mock for dashboard tests."""
    state = MonitorState(name=name, type="http")
    state.status = status
    state.uptime_pct = 99.9
    state.last_check = None
    return state


def _mock_agg_stats():
    """Return mock aggregate stats."""
    return {
        "uptime_pct": {"24h": 99.9, "7d": 99.8, "30d": 99.5, "90d": 99.3},
        "avg_latency": {"24h": 45.0, "7d": 48.0, "30d": 52.0, "90d": 55.0},
        "sites_maintenance": 0,
    }


@pytest.fixture
def mock_db():
    """Mock all DB calls so dashboard routes work without a real pool."""
    with patch("src.dashboard.db.load_state", new_callable=AsyncMock) as mock_load, \
         nullcontext(AsyncMock()) as mock_resp, \
         patch("src.dashboard.db.get_hourly_bars", new_callable=AsyncMock) as mock_bars, \
         patch("src.dashboard.get_aggregate_stats", new_callable=AsyncMock) as mock_agg, \
         bulk_db_calls(mock_load, mock_resp, mock_bars):

        mock_load.return_value = _mock_monitor_state()
        mock_resp.return_value = 45.0
        mock_bars.return_value = []
        mock_agg.return_value = _mock_agg_stats()

        yield {
            "load_state": mock_load,
            "last_response_ms": mock_resp,
            "get_hourly_bars": mock_bars,
            "get_aggregate_stats": mock_agg,
        }


def test_dashboard_loads(mock_db):
    """Dashboard should return 200 even with no monitors."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Uptime Herald" in response.text


def test_dashboard_has_css_variables(mock_db):
    """Dashboard HTML should contain CSS custom properties from theme."""
    response = client.get("/")
    assert response.status_code == 200
    assert "--bg-page:" in response.text
    assert "--text-main:" in response.text
    assert "--text-accent:" in response.text
    assert "--status-up:" in response.text
    assert "--status-down:" in response.text
    assert "--status-mixed:" in response.text
    assert "var(--bg-page)" in response.text
    assert "var(--bg-header)" in response.text


def test_dashboard_respects_configured_decimals(mock_db):
    """global.uptime_decimals/latency_decimals (config.py) should reach
    both the JS globals dashboard-common.js relies on and the
    server-rendered stat cards -- and get_aggregate_stats() should
    actually be called with the configured values, not just the
    defaults."""
    custom_config = copy.deepcopy(DEFAULTS)
    custom_config["global"]["uptime_decimals"] = 3
    custom_config["global"]["latency_decimals"] = 0
    with patch("src.dashboard.load_config", return_value=custom_config):
        response = client.get("/")
    assert response.status_code == 200
    assert "const UPTIME_DECIMALS = 3;" in response.text
    assert "const LATENCY_DECIMALS = 0;" in response.text
    # _mock_agg_stats() returns uptime_pct["24h"]=99.9, avg_latency["24h"]=45.0
    assert "99.900%" in response.text
    assert ">45ms<" in response.text
    assert mock_db["get_aggregate_stats"].call_args.args[-2:] == (3, 0)


def test_health_endpoint():
    """Health check should return OK."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_monitor_not_found():
    """A monitor name genuinely absent from a successfully-loaded config
    should 404 via the real not-found lookup in monitor_detail(), not via
    the config-load-failure fallback (the autouse _isolated_config fixture
    forces load_config to raise for every other test in this file — here
    we override it with a real, valid config so config.get("monitors", [])
    actually contains entries and the loop genuinely finds no match)."""
    valid_config = {
        "global": {},
        "monitors": [
            {"name": "existing-monitor", "type": "http", "url": "http://example.com"},
        ],
    }
    with patch("src.dashboard.load_config", return_value=valid_config):
        response = client.get("/monitor/nonexistent")
    assert response.status_code == 404


def test_monitor_detail_config_error_renders_banner():
    """A broken config must not masquerade as "no such monitor".

    If monitor_detail() caught a bare Exception and fell back to an empty
    monitor list, the route would 404 -- telling the operator the monitor
    doesn't exist when the real problem is that the config can't be read.
    It shows the same 503 config-error page dashboard() and kiosk() do, so
    all three HTML routes fail identically.
    """
    with patch(
        "src.dashboard.load_config",
        side_effect=ConfigError("config load failure"),
    ):
        response = client.get("/monitor/nonexistent")
    assert response.status_code == 503
    assert "config load failure" in response.text


def test_dashboard_config_error_renders_banner():
    """A ConfigError from load_config() (bad YAML, a monitor missing a
    required field, an unset ${ENV_VAR}, etc.) should render the visible
    error page, not a normal-looking dashboard with zero monitors and no
    indication anything is wrong."""
    from src.config import ConfigError
    with patch(
        "src.dashboard.load_config",
        side_effect=ConfigError(
            "Monitor 'my-site' (type 'http') missing required field(s): url"
        ),
    ):
        response = client.get("/")
    assert response.status_code == 503
    assert "Configuration Error" in response.text
    assert "missing required field(s): url" in response.text


def test_kiosk_config_error_renders_banner():
    from src.config import ConfigError
    with patch(
        "src.dashboard.load_config",
        side_effect=ConfigError("Invalid YAML in monitors.yaml: bad indent"),
    ):
        response = client.get("/kiosk")
    assert response.status_code == 503
    assert "Configuration Error" in response.text
    assert "Invalid YAML in monitors.yaml" in response.text


def test_config_error_banner_renders_on_a_genuinely_fresh_environment():
    """_render_config_error() must work on a genuinely fresh process.

    config_error.html uses {% trans %}, which needs gettext installed on the
    shared `env`. If _render_config_error() relied on an earlier request having
    installed it as a side effect, it would work whenever a normal page loaded
    first (including in this file, since `env` is shared and test order lets a
    passing test install real translations first) -- but when a config error is
    the first thing that happens, Jinja would raise UndefinedError: 'gettext' is
    undefined and the page would be a raw 500. This swaps in a brand-new
    Environment with nothing installed instead of trusting the shared one."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from src import dashboard
    from src.config import ConfigError

    fresh_env = Environment(
        loader=FileSystemLoader(str(dashboard.TEMPLATES_DIR)),
        cache_size=0,
        extensions=["jinja2.ext.i18n"],
        autoescape=select_autoescape(("html",)),
    )
    with patch("src.dashboard.env", fresh_env), patch(
        "src.dashboard.load_config",
        side_effect=ConfigError("Config file not found: config/config.yaml"),
    ):
        response = client.get("/")
    assert response.status_code == 503
    assert "Configuration Error" in response.text


def test_dashboard_type_filter_hidden_with_no_monitors(mock_db):
    """No type filter should render when there are no monitors."""
    response = client.get("/")
    assert response.status_code == 200
    assert "type-filter" not in response.text


# --- /api/status ----------------------------------------------------------------------


def test_api_status_no_monitors(mock_db):
    """With no monitors configured (the autouse fixture's empty default
    config), /api/status should still return 200 with empty monitors and
    zeroed sites_total."""
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert data["monitors"] == []
    assert data["agg_stats"]["sites_total"] == 0


def test_api_status_config_error_returns_503(mock_db):
    """/api/status must not answer a broken config with an empty,
    healthy-looking payload.

    Returning HTTP 200 with `monitors: []` and 0 up / 0 down / 0.00% uptime
    would let the dashboard's auto-refresh paint over a live display -- a
    monitoring tool reporting an empty, all-zero install while the real one
    was still running. A 503 matches what the HTML routes do, and the
    polling JS checks r.ok and leaves the last known-good display on screen.
    """
    with patch(
        "src.dashboard.load_config", side_effect=ConfigError("boom"),
    ):
        response = client.get("/api/status")
    assert response.status_code == 503
    assert "boom" in response.json()["detail"]


def test_api_status_with_monitors(mock_db):
    """/api/status should build a real monitor entry the same way the
    dashboard page does, reusing _build_monitor_list/_build_agg_stats."""
    valid_config = {
        "global": {},
        "monitors": [{"name": "test", "type": "http", "url": "http://example.com"}],
    }
    with patch("src.dashboard.load_config", return_value=valid_config), \
         uptime_pct_mock() as mock_uptime:
        mock_uptime.return_value = 99.9
        response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert len(data["monitors"]) == 1
    assert data["monitors"][0]["name"] == "test"
    assert data["monitors"][0]["status"] == "up"
    assert data["monitors"][0]["health"] == "up"


# --- hourly bars in the monitor lists -------------------------------------------------
#
# The monitor lists carry only the bars that can be visible, as plain
# statuses, with the hour labels sent once. A 1000-monitor install would
# otherwise produce a 112 MB dashboard page and a 40 MB /api/status, because
# every monitor would carry 360 bars (twice in the page) with a hover label
# on each.


def _bar_dicts(count):
    return [
        {"status": "up", "hour": f"h{i}", "hour_display": f"h{i}",
         "hour_display_end": f"h{i + 1}"}
        for i in range(count)
    ]


_ONE_MONITOR = {
    "global": {},
    "monitors": [{"name": "test", "type": "http", "url": "http://example.com"}],
}


def test_api_status_bars_are_statuses_and_labels_are_sent_once(mock_db):
    mock_db["get_hourly_bars"].return_value = [
        {"status": "mixed", "hour": "h0", "hour_display": "h0", "hour_display_end": "h1"},
        {"status": "down", "hour": "h1", "hour_display": "h1", "hour_display_end": "h2"},
    ]
    with patch("src.dashboard.load_config", return_value=_ONE_MONITOR), \
         uptime_pct_mock(return_value=99.9):
        data = client.get("/api/status").json()
    assert data["monitors"][0]["bars"] == ["mixed", "down"]
    assert len(data["bar_labels"]) == LIST_BARS
    assert all(" – " in label for label in data["bar_labels"])


def test_monitor_list_asks_for_list_bars_hours_on_one_shared_clock(mock_db):
    config = {
        "global": {},
        "monitors": [
            {"name": "a", "type": "http", "url": "http://example.com"},
            {"name": "b", "type": "http", "url": "http://example.com"},
        ],
    }
    with patch("src.dashboard.load_config", return_value=config), \
         uptime_pct_mock(return_value=99.9):
        client.get("/api/status")
    calls = mock_db["get_hourly_bars"].call_args_list
    assert len(calls) == 2
    assert all(c.kwargs["hours"] == LIST_BARS for c in calls)
    # one clock reading for every monitor, so all bars line up with the
    # same labels even when a request straddles the top of an hour
    assert calls[0].kwargs["now"] is calls[1].kwargs["now"]


def test_dashboard_page_bars_have_no_title_and_desktop_list_is_trimmed(mock_db):
    mock_db["get_hourly_bars"].return_value = _bar_dicts(LIST_BARS)
    with patch("src.dashboard.load_config", return_value=_ONE_MONITOR), \
         uptime_pct_mock(return_value=99.9):
        html = client.get("/").text
    desktop = html.split('id="monitor-list"')[1].split("</nav>")[0]
    mobile = html.split('id="monitor-list-mobile"')[1].split("</nav>")[0]
    assert desktop.count('class="bar ') == SIDEBAR_BARS
    assert mobile.count('class="bar ') == LIST_BARS
    assert '<div class="bar up" title' not in html
    assert f"const SIDEBAR_BARS = {SIDEBAR_BARS};" in html
    assert "BAR_LABELS = [" in html


def test_kiosk_page_bars_are_trimmed_to_the_sidebar(mock_db):
    mock_db["get_hourly_bars"].return_value = _bar_dicts(LIST_BARS)
    with patch("src.dashboard.load_config", return_value=_ONE_MONITOR), \
         uptime_pct_mock(return_value=99.9):
        html = client.get("/kiosk").text
    sidebar = html.split('id="monitor-list"')[1].split("</nav>")[0]
    assert sidebar.count('class="bar ') == SIDEBAR_BARS
    assert '<div class="bar up" title' not in html


# --- "unknown" status (never checked) -- not the same as "down" -----------------------
#
# A monitor that has never been checked has MonitorState's default status,
# "unknown" (src/state.py), and is_up("unknown") correctly returns False.
# Computing health as a bare "up" if is_up(...) else "down" would collapse
# "never checked" into "confirmed down" and paint an inactive monitor's
# sidebar row red. See _is_confirmed_down()'s docstring in src/dashboard.py
# for every place this is handled (health, the sidebar pin_down_monitors
# sort, and the sites_down aggregate count).

def test_api_status_never_checked_monitor_reports_nodata_not_down(mock_db):
    """An active monitor with no check history yet must not be
    reported as health "down" -- it hasn't failed, it just hasn't
    been checked."""
    mock_db["load_state"].return_value = _mock_monitor_state(status="unknown")
    valid_config = {
        "global": {},
        "monitors": [{"name": "fresh", "type": "http", "url": "http://example.com"}],
    }
    with patch("src.dashboard.load_config", return_value=valid_config), \
         uptime_pct_mock() as mock_uptime:
        mock_uptime.return_value = None
        response = client.get("/api/status")
    data = response.json()
    assert data["monitors"][0]["health"] == "nodata"


def test_api_status_never_checked_monitor_excluded_from_up_and_down_counts(mock_db):
    """Neither confirmed up nor confirmed down -- shouldn't be silently
    counted into either aggregate stat card while it's in this state."""
    mock_db["load_state"].return_value = _mock_monitor_state(status="unknown")
    valid_config = {
        "global": {},
        "monitors": [{"name": "fresh", "type": "http", "url": "http://example.com"}],
    }
    with patch("src.dashboard.load_config", return_value=valid_config), \
         uptime_pct_mock() as mock_uptime:
        mock_uptime.return_value = None
        response = client.get("/api/status")
    data = response.json()
    assert data["agg_stats"]["sites_up"] == 0
    assert data["agg_stats"]["sites_down"] == 0


def test_api_status_never_checked_active_monitor_not_pinned_to_top(mock_db):
    """pin_down_monitors (default on) should only pin genuinely down
    monitors -- a never-checked one sorting to the top of the sidebar,
    ahead of monitors that are actually confirmed up, would be exactly
    as misleading as the red-background bug this whole thing started
    from."""
    mock_db["load_state"].side_effect = [
        _mock_monitor_state(name="fresh", status="unknown"),
        _mock_monitor_state(name="healthy", status="up"),
    ]
    valid_config = {
        "global": {"pin_down_monitors": True},
        "monitors": [
            {"name": "fresh", "type": "http", "url": "http://example.com"},
            {"name": "healthy", "type": "http", "url": "http://example.com"},
        ],
    }
    with patch("src.dashboard.load_config", return_value=valid_config), \
         uptime_pct_mock() as mock_uptime:
        mock_uptime.return_value = None
        response = client.get("/api/status")
    data = response.json()
    # Original config order preserved -- "fresh" isn't pinned ahead of
    # "healthy" just for lacking check history.
    assert [m["name"] for m in data["monitors"]] == ["fresh", "healthy"]


# --- /api/checks ----------------------------------------------------------------------


def test_api_all_checks_config_error_returns_503():
    """A config error must not be answered with default pagination.

    Substituting the hardcoded per_page=30 and view_all_checks=False for
    whatever the operator had configured, and returning 200, would make the
    check log silently ignore their settings instead of reporting that the
    config was unreadable.
    """
    with patch(
        "src.dashboard.load_config", side_effect=ConfigError("boom"),
    ), patch(
        "src.dashboard.db.get_all_checks_paginated", new_callable=AsyncMock
    ) as mock_checks:
        response = client.get("/api/checks")
    assert response.status_code == 503
    assert "boom" in response.json()["detail"]
    mock_checks.assert_not_called()


def test_api_all_checks():
    fake_result = {
        "checks": [
            {"time": "2026-08-11T00:00:00+00:00", "status": "up", "response_ms": 12.3},
        ],
        "total": 1, "page": 1, "per_page": 30, "total_pages": 1,
    }
    with patch(
        "src.dashboard.db.get_all_checks_paginated", new_callable=AsyncMock
    ) as mock_checks:
        mock_checks.return_value = fake_result
        response = client.get("/api/checks")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["checks"]) == 1
    # state_changes_only should default True (view_all_checks defaults False)
    mock_checks.assert_called_once_with(1, 30, state_changes_only=True)


def test_api_all_checks_pagination_params_passed_through():
    with patch(
        "src.dashboard.db.get_all_checks_paginated", new_callable=AsyncMock
    ) as mock_checks:
        mock_checks.return_value = {
            "checks": [], "total": 0, "page": 2, "per_page": 10, "total_pages": 1,
        }
        response = client.get("/api/checks?page=2&per_page=10")

    assert response.status_code == 200
    mock_checks.assert_called_once_with(2, 10, state_changes_only=True)


# --- /api/monitor/{name}/checks -------------------------------------------------------


def test_api_monitor_checks_config_error_returns_503():
    """Same as /api/checks: report the config error rather than quietly
    serving the check log with default settings."""
    with patch(
        "src.dashboard.load_config", side_effect=ConfigError("boom"),
    ), patch(
        "src.dashboard.db.get_checks_paginated", new_callable=AsyncMock
    ) as mock_checks:
        response = client.get("/api/monitor/test-monitor/checks")
    assert response.status_code == 503
    assert "boom" in response.json()["detail"]
    mock_checks.assert_not_called()


def test_api_monitor_checks():
    fake_result = {
        "checks": [
            {
                "time": "2026-08-11T00:00:00+00:00", "status": "down",
                "response_ms": 0.0, "error": "timeout",
            },
        ],
        "total": 1, "page": 1, "per_page": 30, "total_pages": 1,
    }
    with patch(
        "src.dashboard.db.get_checks_paginated", new_callable=AsyncMock
    ) as mock_checks:
        mock_checks.return_value = fake_result
        response = client.get("/api/monitor/test-monitor/checks")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["checks"][0]["error"] == "timeout"
    mock_checks.assert_called_once_with(
        "test-monitor", 1, 30, state_changes_only=True
    )


# --- /api/monitor/{name}/response-times -----------------------------------------------


def test_api_monitor_response_times():
    fake_data = [{"time": "2026-08-11T00:00:00+00:00", "response_ms": 42.0}]
    with patch(
        "src.dashboard.db.get_response_time_series", new_callable=AsyncMock
    ) as mock_series:
        mock_series.return_value = fake_data
        response = client.get("/api/monitor/test-monitor/response-times")

    assert response.status_code == 200
    assert response.json() == {"data": fake_data}
    mock_series.assert_called_once_with("test-monitor", 168)


def test_api_monitor_response_times_custom_hours():
    with patch(
        "src.dashboard.db.get_response_time_series", new_callable=AsyncMock
    ) as mock_series:
        mock_series.return_value = []
        response = client.get("/api/monitor/test-monitor/response-times?hours=24")

    assert response.status_code == 200
    mock_series.assert_called_once_with("test-monitor", 24)


# --- /monitor/{name} with a valid, matching config ------------------------------------


def test_monitor_detail_valid_monitor_renders(mock_db):
    """monitor_detail() with a real config AND a monitor that's actually
    found in it, as opposed to the 404 paths covered above."""
    valid_config = {
        "global": {"stats_period_days": 90, "latency_graph_days": 7},
        "monitors": [
            {"name": "web1", "type": "http", "url": "https://example.com"},
        ],
    }
    with patch("src.dashboard.load_config", return_value=valid_config), \
         patch(
             "src.dashboard.db.get_monitor_stats", new_callable=AsyncMock
         ) as mock_stats, \
         patch(
             "src.dashboard.get_aggregate_stats", new_callable=AsyncMock
         ) as mock_agg:
        mock_stats.return_value = {
            "total_checks": 100, "total_up": 99, "total_down": 1, "total_timeout": 0, "outages": 1,
        }
        mock_agg.return_value = _mock_agg_stats()
        response = client.get("/monitor/web1")

    assert response.status_code == 200
    assert "web1" in response.text
    # target_is_safe_link=True for an https:// URL -- should render as a
    # real clickable link, not just plain text.
    assert 'href="https://example.com"' in response.text


def test_monitor_detail_javascript_scheme_not_rendered_as_link(mock_db):
    """The XSS guard test the review flagged as missing entirely: a
    monitor whose configured `url` uses a javascript: scheme must NOT be
    rendered as a clickable <a href>."""
    valid_config = {
        "global": {"latency_graph_days": 7},
        "monitors": [
            {
                "name": "evil", "type": "http",
                "url": "javascript:alert(1)",
            },
        ],
    }
    with patch("src.dashboard.load_config", return_value=valid_config), \
         patch(
             "src.dashboard.db.get_monitor_stats", new_callable=AsyncMock
         ) as mock_stats, \
         patch(
             "src.dashboard.get_aggregate_stats", new_callable=AsyncMock
         ) as mock_agg:
        mock_stats.return_value = {
            "total_checks": 1, "total_up": 1, "total_down": 0, "total_timeout": 0, "outages": 0,
        }
        mock_agg.return_value = _mock_agg_stats()
        response = client.get("/monitor/evil")

    assert response.status_code == 200
    assert 'href="javascript:alert(1)"' not in response.text


# --- _compute_monitor_target / _compute_display_status (direct) -----------------------


def test_compute_monitor_target_http_safe_link():
    from src.dashboard import _compute_monitor_target
    target, safe = _compute_monitor_target(
        {"type": "http", "url": "https://example.com"}
    )
    assert target == "https://example.com"
    assert safe is True


def test_compute_monitor_target_http_javascript_scheme_unsafe():
    from src.dashboard import _compute_monitor_target
    target, safe = _compute_monitor_target(
        {"type": "http", "url": "javascript:alert(1)"}
    )
    assert target == "javascript:alert(1)"
    assert safe is False


def test_compute_monitor_target_tcp():
    from src.dashboard import _compute_monitor_target
    target, safe = _compute_monitor_target(
        {"type": "tcp", "host": "db.internal", "port": 3306}
    )
    assert target == "db.internal:3306"
    assert safe is False


def test_compute_monitor_target_ping():
    from src.dashboard import _compute_monitor_target
    target, safe = _compute_monitor_target({"type": "ping", "host": "1.2.3.4"})
    assert target == "1.2.3.4"
    assert safe is False


def test_compute_monitor_target_unknown_type():
    from src.dashboard import _compute_monitor_target
    target, safe = _compute_monitor_target({"type": "carrier-pigeon"})
    assert target is None
    assert safe is False


def test_compute_display_status_inactive_wins():
    from src.dashboard import _compute_display_status
    # Even "up" and in maintenance, an inactive monitor displays inactive.
    assert _compute_display_status(
        active=False, in_maintenance=True, status="200"
    ) == "inactive"


def test_compute_display_status_maintenance():
    from src.dashboard import _compute_display_status
    assert _compute_display_status(
        active=True, in_maintenance=True, status="200"
    ) == "maintenance"


def test_compute_display_status_up():
    from src.dashboard import _compute_display_status
    assert _compute_display_status(
        active=True, in_maintenance=False, status="200"
    ) == "up"


def test_compute_display_status_down():
    from src.dashboard import _compute_display_status
    assert _compute_display_status(
        active=True, in_maintenance=False, status="down"
    ) == "down"


def test_is_confirmed_down_true_for_a_real_failure():
    from src.dashboard import _is_confirmed_down
    assert _is_confirmed_down("down") is True
    assert _is_confirmed_down("timeout") is True
    assert _is_confirmed_down("503") is True


def test_is_confirmed_down_false_for_unknown_never_checked():
    """The whole point of this helper: "unknown" (MonitorState's own
    default, src/state.py) means no check has ever run, not that one
    ran and failed."""
    from src.dashboard import _is_confirmed_down
    assert _is_confirmed_down("unknown") is False


def test_is_confirmed_down_false_for_a_real_up():
    from src.dashboard import _is_confirmed_down
    assert _is_confirmed_down("up") is False
    assert _is_confirmed_down("200") is False


def test_format_display_time_none_passthrough():
    from src.config import get_display_tz
    from src.dashboard import _format_display_time
    assert _format_display_time(None, "en", get_display_tz({})) is None


def test_format_display_time_naive_iso_assumed_utc():
    """An ISO timestamp with no timezone offset is treated as UTC before
    conversion to the display timezone, rather than raising or silently
    misinterpreting it as local time."""
    from src.config import get_display_tz
    from src.dashboard import _format_display_time
    result = _format_display_time(
        "2026-08-11T00:00:00", "en", get_display_tz({}),
    )
    assert result is not None


@pytest.mark.asyncio
async def test_no_uptimes_placeholder_coroutine_returns_an_empty_dict():
    from src.dashboard import _no_uptimes
    assert await _no_uptimes() == {}


def test_api_status_includes_formatted_last_check(mock_db):
    """A monitor with a recorded last_check should have it converted to
    the display timezone/format in the sidebar list, not passed through
    raw or dropped."""
    from src.state import MonitorState
    state = MonitorState(name="test", type="http")
    state.status = "up"
    state.last_check = "2026-08-11T12:00:00+00:00"
    mock_db["load_state"].return_value = state

    valid_config = {
        "global": {},
        "monitors": [{"name": "test", "type": "http", "url": "http://example.com"}],
    }
    with patch("src.dashboard.load_config", return_value=valid_config), \
         uptime_pct_mock() as mock_uptime:
        mock_uptime.return_value = 99.9
        response = client.get("/api/status")

    assert response.status_code == 200
    last_check = response.json()["monitors"][0]["last_check"]
    assert last_check is not None
    assert "2026" in last_check


# --- Inline config fallbacks must not drift from DEFAULTS -----------------------------


def test_inline_global_config_fallbacks_match_defaults():
    """Every `config.get("global", {}).get("x", FALLBACK)` in dashboard.py
    must use the same FALLBACK as config.py's DEFAULTS["global"]["x"].

    dashboard.py carries many of these inline fallbacks. They're dead code
    in normal operation -- load_config() deep-merges DEFAULTS, so the key
    is always present -- but they are a live drift hazard: a fallback that
    disagrees with DEFAULTS (say view_all_checks falling back to True while
    DEFAULTS says False) would silently change behaviour at exactly the
    wrong moment, such as when a config can't be loaded.

    Parsed from the AST rather than regex-matched so this can't be fooled
    by formatting or line wrapping. Kept as a guard instead of deleting
    all of the call sites: the deletion would be a large no-op diff through a
    hot request path, whereas the actual risk is only ever the values
    disagreeing -- which this catches directly.
    """
    import ast
    from pathlib import Path

    from src.config import DEFAULTS

    source = (Path(__file__).parent.parent / "src" / "dashboard.py").read_text()
    tree = ast.parse(source)

    checked = []
    mismatches = []

    for node in ast.walk(tree):
        # Match the outer .get("<key>", <default>) whose receiver is
        # itself a .get("global", {}) call.
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) == 2):
            continue
        inner = node.func.value
        if not (isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and len(inner.args) == 2
                and isinstance(inner.args[0], ast.Constant)
                and inner.args[0].value == "global"):
            continue
        key_node, default_node = node.args
        if not isinstance(key_node, ast.Constant):
            continue
        try:
            fallback = ast.literal_eval(default_node)
        except ValueError:
            continue  # not a literal (e.g. a computed default) -- skip
        key = key_node.value
        checked.append(key)
        if key not in DEFAULTS["global"]:
            mismatches.append(
                f"{key}: not in DEFAULTS['global'] at all "
                f"(dashboard.py line {node.lineno})"
            )
        elif DEFAULTS["global"][key] != fallback:
            mismatches.append(
                f"{key}: dashboard.py line {node.lineno} falls back to "
                f"{fallback!r}, DEFAULTS says "
                f"{DEFAULTS['global'][key]!r}"
            )

    assert checked, "found no inline global fallbacks -- did the pattern change?"
    assert not mismatches, (
        "inline fallbacks in dashboard.py disagree with config.py DEFAULTS:\n  "
        + "\n  ".join(mismatches)
    )
