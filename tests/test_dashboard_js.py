# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_dashboard_js.py - Real-browser tests for dashboard-common.js, via Playwright.

Why a real browser instead of a Node-based unit-test tool (Jest/Vitest):
trimRowBars() measures real rendered layout (container.clientWidth) to
decide how many uptime bars fit -- jsdom (what a Node unit test would
use) doesn't do real CSS layout, so clientWidth there is normally just 0
regardless of what's on screen. A real browser is the only way to test
that function meaningfully at all.
The rest of the file's functions rely on globals (T, CHECKS_PER_PAGE)
set by inline <script> tags in the real templates, not by this file
itself -- loading the real page context gets them for free instead of
needing to hand-fake them per test.

Every test here loads the REAL dashboard-common.js from disk via
page.add_script_tag(path=...), never a copy or a paraphrase of it.

Requires the Playwright Chromium browser to be installed
(`playwright install chromium`). Skipped entirely, with a clear reason,
if it isn't -- this should never fail a local `pytest tests/` run for
someone who hasn't set that up, only in CI where it's actually installed.

MUST be run as its own separate `pytest` invocation, never combined with
the rest of tests/ in one process. Merely having the `pytest-playwright`
package installed (regardless of whether this file's tests actually collect
or run) breaks pytest-asyncio's strict-mode event-loop handling for every
async test elsewhere in the suite, which then fail with "RuntimeError:
Runner.run() cannot be called...". Disabling the plugin (`-p no:playwright`)
makes them pass again, which shows the plugin itself is the cause, not this
file's code. Run as:
    pytest tests/ --ignore=tests/test_dashboard_js.py --ignore=tests/test_responsive.py
    pytest tests/test_dashboard_js.py tests/test_responsive.py  # both browser files together

test_responsive.py is the same story for the same
reason -- it also drives pytest-playwright, so it belongs in the second
command above, not the first. See its own docstring for what it covers.
"""

import json
from pathlib import Path

import pytest

from tests.conftest import browser_tier_skip_reason


# Chromium detection and the skip/fail decision both live in conftest.py,
# shared with test_responsive.py so the two files can't drift apart. See
# browser_tier_skip_reason() there for why a missing browser can be made
# a hard failure instead of a silent skip.
pytestmark = pytest.mark.skipif(
    browser_tier_skip_reason() is not None,
    reason=browser_tier_skip_reason() or "",
)

JS_FILE = (
    Path(__file__).resolve().parent.parent
    / "src" / "static" / "js" / "dashboard-common.js"
)

# The real translation keys get_js_translations() actually produces (see
# src/i18n.py) -- kept in sync by hand since this file can't import Python
# i18n into a browser context; a real page load gets these from the
# server, this fixture stands in for that.
_T = {
    "loading": "Loading…",
    "no_monitors": "No monitors configured.",
    "no_checks": "No checks recorded",
    "failed_load": "Failed to load checks",
    "failed_load_checks": "Failed to load checks",
    "failed_monitor": "Failed to load:",
    "prev": "‹ PREV",
    "next": "NEXT ›",
    "page_of": "PAGE %(page)d OF %(total)d (%(count)d CHECKS)",
    "inactive": "INACTIVE",
    "maint": "MAINT",
    "maintenance": "Maintenance:",
    "verifying": "Verifying Status",
    "verification_confirmed": "Status Change Verified",
    "no_latency": "No latency data",
    "go_to_page": "Go to page",
}


@pytest.fixture
def js_page(page):
    """A blank real page with the real dashboard-common.js loaded and the
    globals (T, CHECKS_PER_PAGE, UPTIME_DECIMALS, LATENCY_DECIMALS,
    SIDEBAR_BARS) it assumes the surrounding template has already defined
    by the time any of its functions get called."""
    page.set_content("<!DOCTYPE html><html><body></body></html>")
    page.add_script_tag(path=str(JS_FILE))
    page.evaluate(
        "(t) => { window.T = t; window.CHECKS_PER_PAGE = 30; "
        "window.UPTIME_DECIMALS = 2; window.LATENCY_DECIMALS = 1; "
        "window.SIDEBAR_BARS = 36; }", _T,
    )
    return page


# --- escapeHtml: the security-relevant one --------------------------------------------
#
# Mirrors the same XSS boundary already tested on the Python side
# (target_is_safe_link in dashboard.py) -- this is the client-side half.


@pytest.mark.parametrize("payload,must_not_contain", [
    ("<script>alert(1)</script>", "<script>"),
    ("<img src=x onerror=alert(1)>", "<img src=x onerror=alert(1)>"),
    ("\"><svg onload=alert(1)>", "<svg onload=alert(1)>"),
    ("<b>bold</b>", "<b>"),
])
def test_escape_html_neutralizes_markup(js_page, payload, must_not_contain):
    result = js_page.evaluate("(s) => escapeHtml(s)", payload)
    assert must_not_contain not in result


def test_escape_html_plain_text_passes_through_unchanged(js_page):
    assert js_page.evaluate("(s) => escapeHtml(s)", "drd01") == "drd01"


def test_escape_html_null_becomes_empty_string(js_page):
    assert js_page.evaluate("() => escapeHtml(null)") == ""


def test_escape_html_number_gets_stringified(js_page):
    assert js_page.evaluate("(s) => escapeHtml(s)", 42) == "42"


def test_escape_html_ampersand_escaped(js_page):
    """A literal & left unescaped would corrupt the surrounding markup
    (or, worse, be interpreted as the start of a different entity)."""
    result = js_page.evaluate("(s) => escapeHtml(s)", "Tom & Jerry")
    assert "&amp;" in result
    assert result != "Tom & Jerry"


# --- trimRowBars: the one that NEEDS a real browser -----------------------------------


def test_trim_row_bars_hides_oldest_bars_that_dont_fit(js_page):
    """Real layout dependency: a jsdom-based Node test could not verify
    this meaningfully at all, since jsdom's clientWidth is always 0."""
    js_page.set_content(
        "<!DOCTYPE html><html><body>"
        '<div class="row-bars" style="width:100px; display:flex; '
        'overflow:hidden;">'
        + "".join('<div class="bar up" style="width:8px; margin-right:2px; flex-shrink:0;"></div>' for _ in range(20))
        + "</div></body></html>"
    )
    js_page.add_script_tag(path=str(JS_FILE))

    js_page.evaluate("() => trimRowBars()")

    bars = js_page.query_selector_all(".row-bars .bar")
    hidden = [b for b in bars if b.evaluate("el => el.style.display") == "none"]
    visible = [b for b in bars if b.evaluate("el => el.style.display") != "none"]
    # 100px container, 8px bar + 2px gap = 10px per bar -> 10 fit.
    assert len(visible) == 10
    assert len(hidden) == 10
    # The OLDEST (leftmost) ones are hidden, not the newest.
    assert hidden == bars[:10]


def test_trim_row_bars_zero_width_container_is_noop(js_page):
    """A container not yet laid out (width 0, e.g. display:none ancestor)
    must not crash or hide everything -- it should just skip."""
    js_page.set_content(
        '<!DOCTYPE html><html><body style="display:none">'
        '<div class="row-bars"><div class="bar up"></div></div>'
        "</body></html>"
    )
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("() => trimRowBars()")  # must not raise


# --- updateMonitorList ----------------------------------------------------------------


def _monitor_row_html(name="web1", health="up"):
    return (
        f'<div class="monitor-row {health}" data-name="{name}" '
        f'data-status="{health}" data-tags="">'
        f'<div class="uptime-pct"></div>'
        f'<div class="row-bars"></div>'
        f"</div>"
    )


def test_update_monitor_list_updates_health_class_and_uptime(js_page):
    js_page.set_content(
        f'<!DOCTYPE html><html><body><div id="monitor-list">'
        f"{_monitor_row_html('web1', 'up')}"
        f"</div></body></html>"
    )
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate(
        "(t) => { window.T = t; }", _T,
    )

    js_page.evaluate(
        "(monitors) => updateMonitorList(monitors)",
        [{
            "name": "web1", "health": "down", "active": True,
            "in_maintenance": False, "uptime_pct": 42.567, "tags": ["prod"],
            "bars": [],
        }],
    )

    row = js_page.query_selector('.monitor-row[data-name="web1"]')
    assert "down" in row.get_attribute("class")
    assert "up" not in row.get_attribute("class").split()
    assert row.get_attribute("data-tags") == "prod"
    pct = js_page.query_selector('.monitor-row[data-name="web1"] .uptime-pct')
    assert pct.inner_text() == "42.57%"


def test_update_monitor_list_inactive_shows_inactive_label(js_page):
    js_page.set_content(
        f'<!DOCTYPE html><html><body><div id="monitor-list">'
        f"{_monitor_row_html('web1', 'up')}"
        f"</div></body></html>"
    )
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; }", _T)

    js_page.evaluate(
        "(monitors) => updateMonitorList(monitors)",
        [{
            "name": "web1", "health": "up", "active": False,
            "in_maintenance": False, "uptime_pct": 99.0, "tags": [],
            "bars": [],
        }],
    )

    row = js_page.query_selector('.monitor-row[data-name="web1"]')
    assert "inactive" in row.get_attribute("class")
    pct = js_page.query_selector('.monitor-row[data-name="web1"] .uptime-pct')
    assert pct.inner_text() == _T["inactive"]


def test_update_monitor_list_never_checked_monitor_gets_nodata_not_up(js_page):
    """A monitor that has never been checked must get 'nodata', not 'up' or
    'down': the server computes health as a 3-way up/down/nodata result, and
    updateMonitorList() (the auto-refresh path) must pass 'nodata' through as
    its own class rather than collapsing it to 'up' -- a monitor that's
    simply never been checked isn't confirmed healthy either."""
    js_page.set_content(
        f'<!DOCTYPE html><html><body><div id="monitor-list">'
        f"{_monitor_row_html('web1', 'up')}"
        f"</div></body></html>"
    )
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; }", _T)

    js_page.evaluate(
        "(monitors) => updateMonitorList(monitors)",
        [{
            "name": "web1", "health": "nodata", "active": True,
            "in_maintenance": False, "uptime_pct": None, "tags": [],
            "bars": [],
        }],
    )

    row = js_page.query_selector('.monitor-row[data-name="web1"]')
    classes = row.get_attribute("class").split()
    assert "nodata" in classes
    assert "up" not in classes
    assert "down" not in classes


def test_update_monitor_list_bar_titles_cannot_inject_markup(js_page):
    """The hover labels are assigned as the bars' title property, never
    concatenated into markup -- confirms a malicious label stays text
    rather than becoming an element."""
    js_page.set_content(
        f'<!DOCTYPE html><html><body><div id="monitor-list">'
        f"{_monitor_row_html('web1', 'up')}"
        f"</div></body></html>"
    )
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate(
        "(t) => { window.T = t; window.SIDEBAR_BARS = 36; }", _T,
    )

    js_page.evaluate(
        "([monitors, labels]) => updateMonitorList(monitors, labels)",
        [[{
            "name": "web1", "health": "up", "active": True,
            "in_maintenance": False, "uptime_pct": 100.0, "tags": [],
            "bars": ["up"],
        }], ['"><img src=x onerror=alert(1)> – 12:00']],
    )

    container = js_page.query_selector('.monitor-row[data-name="web1"] .row-bars')
    assert "<img" not in container.inner_html()
    assert js_page.query_selector(
        '.monitor-row[data-name="web1"] .bar'
    ).get_attribute("title") == '"><img src=x onerror=alert(1)> – 12:00'


def test_update_monitor_list_titles_follow_the_hour_of_each_bar(js_page):
    """bars arrive newest first and the row shows them oldest first, so the
    last bar in the row is the newest and gets the first (newest) label."""
    js_page.set_content(
        f'<!DOCTYPE html><html><body><div id="monitor-list">'
        f"{_monitor_row_html('web1', 'up')}"
        f"</div></body></html>"
    )
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; window.SIDEBAR_BARS = 36; }", _T)

    js_page.evaluate(
        "([monitors, labels]) => updateMonitorList(monitors, labels)",
        [[{
            "name": "web1", "health": "up", "active": True,
            "in_maintenance": False, "uptime_pct": 100.0, "tags": [],
            "bars": ["up", "down", "mixed"],
        }], ["now", "1h ago", "2h ago", "3h ago"]],
    )

    bars = js_page.query_selector_all('.monitor-row[data-name="web1"] .bar')
    assert [b.get_attribute("class") for b in bars] == [
        "bar mixed", "bar down", "bar up",
    ]
    assert [b.get_attribute("title") for b in bars] == [
        "2h ago", "1h ago", "now",
    ]


def test_update_monitor_list_desktop_list_shows_only_what_fits(js_page):
    """The 360 px sidebar has room for SIDEBAR_BARS bars; the mobile list
    can be as wide as the screen and keeps every bar it was sent."""
    row = lambda: _monitor_row_html("web1", "up")  # noqa: E731
    js_page.set_content(
        f'<!DOCTYPE html><html><body>'
        f'<div id="monitor-list">{row()}</div>'
        f'<div id="monitor-list-mobile">{row()}</div>'
        f"</body></html>"
    )
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; window.SIDEBAR_BARS = 36; }", _T)

    js_page.evaluate(
        "(monitors) => updateMonitorList(monitors, [])",
        [{
            "name": "web1", "health": "up", "active": True,
            "in_maintenance": False, "uptime_pct": 100.0, "tags": [],
            "bars": ["up"] * 140,
        }],
    )

    assert len(js_page.query_selector_all("#monitor-list .bar")) == 36
    assert len(js_page.query_selector_all("#monitor-list-mobile .bar")) == 140


# --- updateAggStats -------------------------------------------------------------------


def _agg_stats_dom():
    keys = [
        "sites_up", "sites_down", "sites_inactive", "sites_maintenance",
        "uptime_24h", "uptime_7d", "uptime_30d", "uptime_period",
        "latency_24h", "latency_7d", "latency_30d", "latency_period",
    ]
    cards = "".join(
        f'<div class="metric-card"><div data-stat="{k}"></div></div>'
        for k in keys
    )
    return f"<!DOCTYPE html><html><body>{cards}</body></html>"


def test_update_agg_stats_populates_values(js_page):
    js_page.set_content(_agg_stats_dom())
    js_page.add_script_tag(path=str(JS_FILE))

    js_page.evaluate(
        "(stats) => updateAggStats(stats)",
        {
            "stats_period_key": "90d",
            "sites_up": 8, "sites_down": 2, "sites_inactive": 1,
            "sites_maintenance": 0,
            "uptime_pct": {"24h": 99.5, "7d": 98.1, "30d": 97.0, "90d": 96.4},
            "avg_latency": {"24h": 42, "7d": 45, "30d": 50, "90d": 55},
        },
    )

    assert js_page.query_selector('[data-stat="sites_down"]').inner_text() == "2"
    # UPTIME_DECIMALS/LATENCY_DECIMALS default to 2/1 (see js_page fixture) --
    # matches the real templates' config.py defaults.
    assert js_page.query_selector('[data-stat="uptime_24h"]').inner_text() == "99.50%"
    assert js_page.query_selector('[data-stat="latency_period"]').inner_text() == "55.0ms"


def test_update_agg_stats_toggles_down_card_class(js_page):
    js_page.set_content(_agg_stats_dom())
    js_page.add_script_tag(path=str(JS_FILE))

    js_page.evaluate(
        "(stats) => updateAggStats(stats)",
        {
            "stats_period_key": "90d", "sites_up": 5, "sites_down": 1,
            "sites_inactive": 0, "sites_maintenance": 0,
            "uptime_pct": {"24h": 0, "7d": 0, "30d": 0, "90d": 0},
            "avg_latency": {"24h": 0, "7d": 0, "30d": 0, "90d": 0},
        },
    )

    down_card = js_page.query_selector('[data-stat="sites_down"]')
    card = down_card.evaluate_handle("el => el.closest('.metric-card')")
    assert "down" in card.evaluate("el => el.className")


# --- renderChecks / renderPagination --------------------------------------------------


def _checks_table_dom():
    return (
        "<!DOCTYPE html><html><body>"
        '<table><tbody id="checks-tbody"></tbody></table>'
        '<div id="pagination"></div>'
        "</body></html>"
    )


def _goto_local(page, html):
    """page.set_content() leaves the page on about:blank, which has no real
    origin -- the JS's relative fetch('/api/checks...') calls can't resolve a
    URL against that at all, and even the "network error" test would pass for
    the wrong reason (a URL resolution failure, not the simulated abort() it
    claims to test). Routing the navigation itself gives the page a real http
    origin without any actual network access."""
    page.route(
        "http://uptime-herald.test/",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto("http://uptime-herald.test/")


def test_render_checks_escapes_malicious_error_field(js_page):
    """A monitor's check.error is operator/target-supplied (a real HTTP
    error message, a ping failure string) -- rendered via string
    concatenation into innerHTML, so it must be escaped."""
    js_page.set_content(_checks_table_dom())
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; }", _T)

    js_page.evaluate(
        "(data) => renderChecks(data)",
        {
            "checks": [{
                "monitor": "web1", "status": "down", "time": "2026-08-16T00:00:00",
                "response_ms": None,
                "error": '<img src=x onerror=alert(1)>',
            }],
            "total": 1, "page": 1, "per_page": 30, "total_pages": 1,
        },
    )

    row_html = js_page.query_selector("#checks-tbody tr").inner_html()
    assert "<img src=x onerror=alert(1)>" not in row_html
    assert "onerror=alert(1)" not in row_html or "&lt;img" in row_html


def test_render_checks_empty_shows_no_checks_message(js_page):
    js_page.set_content(_checks_table_dom())
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; }", _T)

    js_page.evaluate(
        "(data) => renderChecks(data)",
        {"checks": [], "total": 0, "page": 1, "per_page": 30, "total_pages": 1},
    )

    assert _T["no_checks"] in js_page.query_selector("#checks-tbody").inner_text()


def test_render_pagination_disables_prev_on_first_page(js_page):
    js_page.set_content(_checks_table_dom())
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; window.CHECKS_PER_PAGE = 30; }", _T)

    js_page.evaluate(
        "(data) => renderPagination(data)",
        {"checks": [], "total": 90, "page": 1, "per_page": 30, "total_pages": 3},
    )

    buttons = js_page.query_selector_all("#pagination button")
    assert buttons[0].is_disabled()  # prev
    assert not buttons[1].is_disabled()  # next


def test_render_pagination_disables_next_on_last_page(js_page):
    js_page.set_content(_checks_table_dom())
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; window.CHECKS_PER_PAGE = 30; }", _T)

    js_page.evaluate(
        "(data) => renderPagination(data)",
        {"checks": [], "total": 90, "page": 3, "per_page": 30, "total_pages": 3},
    )

    buttons = js_page.query_selector_all("#pagination button")
    assert not buttons[0].is_disabled()  # prev
    assert buttons[1].is_disabled()  # next


# --- loadChecks / loadChecksQuiet: real fetch(), mocked response ----------------------


def test_load_checks_renders_fetched_data(js_page):
    """Real fetch() call, real response parsing -- the network layer is
    mocked (page.route), everything downstream of it is real."""
    _goto_local(js_page, _checks_table_dom())
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate(
        "(t) => { window.T = t; window.CHECKS_PER_PAGE = 30; "
        "window.UPTIME_DECIMALS = 2; window.LATENCY_DECIMALS = 1; }", _T,
    )

    js_page.route(
        "**/api/checks*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "checks": [{
                    "monitor": "web1", "status": "up",
                    "time": "2026-08-16T00:00:00", "response_ms": 12.3,
                }],
                "total": 1, "page": 1, "per_page": 30, "total_pages": 1,
            }),
        ),
    )

    js_page.evaluate("() => loadChecks(1)")
    js_page.wait_for_function(
        "() => document.querySelectorAll('#checks-tbody tr').length > 0"
    )

    assert "web1" in js_page.query_selector("#checks-tbody").inner_text()


def test_load_checks_shows_failure_message_on_network_error(js_page):
    _goto_local(js_page, _checks_table_dom())
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate("(t) => { window.T = t; window.CHECKS_PER_PAGE = 30; }", _T)

    js_page.route("**/api/checks*", lambda route: route.abort())

    js_page.evaluate("() => loadChecks(1)")
    js_page.wait_for_function(
        "(msg) => document.getElementById('checks-tbody').innerText.includes(msg)",
        arg=_T["failed_load"],
    )


def test_load_checks_quiet_skips_dom_update_when_unchanged(js_page):
    """The exact behavior this function exists for: an identical response
    on a background poll must not touch the DOM (that's what causes the
    visible flicker the docstring in the source references)."""
    _goto_local(js_page, _checks_table_dom())
    js_page.add_script_tag(path=str(JS_FILE))
    js_page.evaluate(
        "(t) => { window.T = t; window.CHECKS_PER_PAGE = 30; "
        "window.UPTIME_DECIMALS = 2; window.LATENCY_DECIMALS = 1; }", _T,
    )

    same_response = {
        "checks": [{
            "monitor": "web1", "status": "up",
            "time": "2026-08-16T00:00:00", "response_ms": 12.3,
        }],
        "total": 1, "page": 1, "per_page": 30, "total_pages": 1,
    }
    js_page.route(
        "**/api/checks*",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(same_response),
        ),
    )

    js_page.evaluate("() => loadChecksQuiet(1)")
    js_page.wait_for_function(
        "() => document.querySelectorAll('#checks-tbody tr').length > 0"
    )
    # Mark the row so a re-render would be detectable.
    js_page.evaluate(
        "() => document.querySelector('#checks-tbody tr').dataset.marker = 'untouched'"
    )

    js_page.evaluate("() => loadChecksQuiet(1)")
    js_page.wait_for_timeout(200)  # let the fetch promise settle

    marker = js_page.evaluate(
        "() => document.querySelector('#checks-tbody tr')?.dataset.marker"
    )
    assert marker == "untouched"  # row was never rebuilt
