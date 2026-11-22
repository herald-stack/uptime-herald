# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_kiosk.py - Tests for the kiosk page.
"""

from fastapi.testclient import TestClient

from src.main import app
from tests.test_dashboard import (
    _isolated_config,
    _mock_agg_stats,
    _mock_monitor_state,
    mock_db,
)

client = TestClient(app)


def test_kiosk_loads(mock_db):
    """Kiosk page should return 200 and contain kiosk-specific content."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "Uptime Herald" in response.text
    assert "kiosk" in response.text.lower()
    # Should not contain sidebar filter controls
    assert "sidebar-filter" not in response.text
    assert "status-filter" not in response.text
    assert "type-filter" not in response.text
    assert "filter-reset" not in response.text


def test_kiosk_sidebar_footer(mock_db):
    """Kiosk page should have a sidebar footer with 'Kiosk Mode' text and version."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "sidebar-footer" in response.text
    assert "Kiosk Mode" in response.text
    assert "footer-version" in response.text
    # Should NOT contain the dashboard's footer links
    assert "footer-links" not in response.text


def test_kiosk_has_rotation_config(mock_db):
    """Kiosk page should contain the rotation interval config."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "ROTATION_SECONDS" in response.text
    assert "* 60" in response.text


def test_kiosk_has_legend(mock_db):
    """Kiosk page should still have the sidebar legend."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "sidebar-legend" in response.text


def test_kiosk_has_aggregate_stats(mock_db):
    """Kiosk page should contain aggregate stats section."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "agg-stats" in response.text
    assert "sites_up" in response.text
    assert "sites_down" in response.text


def test_kiosk_has_monitor_list(mock_db):
    """Kiosk page should have the monitor list for navigation."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "monitor-list" in response.text


def test_kiosk_has_auto_refresh(mock_db):
    """Kiosk page should contain auto-refresh logic."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "autoRefresh" in response.text
    assert "/api/status" in response.text


def test_kiosk_has_down_priority_logic(mock_db):
    """Kiosk page should contain down-monitor priority logic."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "downMonitors" in response.text
    assert "updateRotationList" in response.text


def test_kiosk_has_no_user_controls(mock_db):
    """Kiosk page should not have pause/play or manual navigation controls."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    # No filter controls
    assert "filterByStatus" not in response.text
    assert "filterByType" not in response.text
    assert "filterByTag" not in response.text
    assert "resetFilters" not in response.text


def test_kiosk_no_select_monitor_reference(mock_db):
    """Kiosk page should not reference the undefined selectMonitor function.

    The kiosk uses showMonitor() instead — selectMonitor is a dashboard-only
    function. An onclick referencing selectMonitor would throw at runtime.
    """
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "selectMonitor" not in response.text
    assert "showMonitor" in response.text


def test_kiosk_title(mock_db):
    """Kiosk page should have a distinct title."""
    response = client.get("/kiosk")
    assert response.status_code == 200
    assert "Kiosk" in response.text
