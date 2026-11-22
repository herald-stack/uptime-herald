# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_base.py - Tests for is_up() and elapsed_ms() in src/monitors/base.py.

is_up() drives every status decision in the app -- sidebar/detail bar
colors, alert routing, retry-sequence abort logic, uptime stat queries --
but would otherwise be exercised only indirectly through monitor/alert
tests, so its edge cases are pinned down here.
"""

from unittest.mock import patch

import pytest

from src.monitors.base import elapsed_ms, is_up


def test_is_up_none_is_down():
    """A monitor with no check yet (None status) is not up."""
    assert is_up(None) is False


def test_is_up_empty_string_is_down():
    assert is_up("") is False


def test_is_up_literal_up():
    assert is_up("up") is True


def test_is_up_literal_down():
    assert is_up("down") is False


def test_is_up_http_2xx_is_up():
    assert is_up("200") is True
    assert is_up("201") is True
    assert is_up("299") is True


def test_is_up_http_below_200_is_down():
    assert is_up("199") is False


def test_is_up_http_3xx_4xx_5xx_are_down():
    assert is_up("301") is False
    assert is_up("404") is False
    assert is_up("500") is False


def test_is_up_non_numeric_error_strings_are_down():
    assert is_up("timeout") is False
    assert is_up("failed") is False


def test_is_up_maintenance_and_inactive_are_down():
    """These aren't real health states -- is_up() has no special case for
    them, they just fall through the non-numeric branch to False, same as
    any other unrecognized string."""
    assert is_up("maintenance") is False
    assert is_up("inactive") is False


# --- expected_status ------------------------------------------------------------------


def test_is_up_expected_status_matches_exactly():
    """With expected_status set, only that exact code counts as up --
    not the default 2xx range."""
    assert is_up("401", expected_status=401) is True


def test_is_up_expected_status_rejects_2xx_when_not_the_expected_code():
    """expected_status replaces the 2xx rule entirely, it doesn't add to
    it -- a real 200 from a monitor configured to expect 401 is DOWN,
    since that's not the response the operator said means this endpoint
    is healthy."""
    assert is_up("200", expected_status=401) is False


def test_is_up_expected_status_rejects_other_codes():
    assert is_up("500", expected_status=401) is False
    assert is_up("403", expected_status=401) is False


def test_is_up_expected_status_none_preserves_default_2xx_behavior():
    """expected_status=None (the default -- a monitor that never set the
    field) must behave identically to calling is_up() without it at all,
    so every pre-existing monitor's classification is unaffected by this
    option's mere existence."""
    assert is_up("200", expected_status=None) is is_up("200")
    assert is_up("404", expected_status=None) is is_up("404")


def test_is_up_expected_status_ignored_for_non_numeric_status():
    """expected_status only applies to http status codes -- "up"/"down"
    (ping/tcp) and error strings (timeout/failed) are unaffected by it,
    same as they're unaffected by the default 2xx rule."""
    assert is_up("up", expected_status=401) is True
    assert is_up("down", expected_status=401) is False
    assert is_up("timeout", expected_status=401) is False


def test_elapsed_ms_returns_unrounded_value():
    """Deliberately unrounded -- rounding for display happens at
    aggregation/display time (config.py's latency_decimals, threaded
    through db_mariadb.py/db_postgres.py and the dashboard templates/JS),
    not at measurement time, so real sub-millisecond precision reaches
    storage. See elapsed_ms()'s own docstring for the full reasoning."""
    with patch("src.monitors.base.time.monotonic", return_value=1.0423571):
        result = elapsed_ms(1.0)
    assert result == pytest.approx(42.3571)
    # Not pre-rounded to 1 decimal -- rounding would collapse
    # this to exactly 42.4.
    assert round(result, 4) != round(result, 1)
