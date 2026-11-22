# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_responsive.py - Real-browser tests for the mobile layout.

Real-browser tests for the mobile responsive layout (dashboard.html's
`.mobile-shell` + monitor.html's check-log accordion, see
_style_mobile.html).

Unlike test_dashboard_js.py (which tests dashboard-common.js's functions
against small synthetic DOM fixtures), these tests render the REAL
dashboard.html and monitor.html templates via the app's own routes (with
the DB mocked, same fixture pattern as test_dashboard.py) and load that
actual HTML in a real Chromium tab, resized to real viewport widths. The
thing under test here is fundamentally "does the CSS media query at
1366px actually show/hide the right DOM, in a real browser's layout
engine" — a synthetic snippet or a Node/jsdom test can't answer that.

Requires the Playwright Chromium browser to be installed
(`playwright install chromium`). Skipped entirely, with a clear reason,
if it isn't.

MUST run in its own separate pytest invocation from the main async
suite — merely having pytest-playwright active breaks pytest-asyncio's
event-loop handling for unrelated async tests (see test_dashboard_js.py
for the full story; that same constraint applies here for the same
reason, since this file also drives pytest-playwright's fixtures). It's
fine to combine with test_dashboard_js.py in one run since neither uses
pytest-asyncio. Run as:
    pytest tests/ --ignore=tests/test_dashboard_js.py --ignore=tests/test_responsive.py
    pytest tests/test_dashboard_js.py tests/test_responsive.py
"""

import copy
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import DEFAULTS
from src.config import load_theme as _real_load_theme
from src.main import app
from src.state import MonitorState

from tests.conftest import browser_tier_skip_reason, bulk_db_calls, uptime_pct_mock

DESKTOP_WIDTH = 1920
MOBILE_WIDTH = 390
STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "static"
CONTENT_TYPES = {".js": "application/javascript", ".woff2": "font/woff2", ".png": "image/png"}


# See test_dashboard_js.py / conftest.py -- one shared implementation.
pytestmark = pytest.mark.skipif(
    browser_tier_skip_reason() is not None,
    reason=browser_tier_skip_reason() or "",
)


@pytest.fixture(scope="module")
def rendered_pages():
    """Render the real dashboard.html (2 monitors) and one real
    monitor.html detail fragment, via the actual FastAPI routes with the
    DB mocked -- same approach as test_dashboard.py, just capturing the
    HTML instead of asserting on it directly here."""
    config = copy.deepcopy(DEFAULTS)
    config["monitors"] = [
        {"name": "web-server", "type": "http", "url": "https://example.com", "tags": ["prod"]},
        {"name": "db-server", "type": "tcp", "host": "db.local", "port": 5432, "tags": ["prod"]},
    ]

    state = MonitorState(name="web-server", type="http")
    state.status = "up"
    state.uptime_pct = 99.9
    state.last_check = None

    agg = {
        "uptime_pct": {"24h": 99.9, "7d": 99.8, "30d": 99.5, "90d": 99.3},
        "avg_latency": {"24h": 45.0, "7d": 48.0, "30d": 52.0, "90d": 55.0},
        "sites_maintenance": 0,
    }
    # 360 bars (15 days), not some small round number -- matches the
    # real db.get_hourly_bars(hours=360) call every route actually
    # makes. A short list would hide a layout bug: the un-trimmed bars only
    # overflow their flex container wide enough to matter at something close
    # to the real count (see
    # test_mobile_shell_is_not_widened_by_untrimmed_bar_content below).
    bars = [
        {"status": "up", "hour_display": f"h{h}", "hour_display_end": f"h{h + 1}"}
        for h in range(360)
    ]

    with patch("src.dashboard.load_config", return_value=config), \
         patch("src.dashboard.load_theme",
               side_effect=lambda *a, **kw: _real_load_theme("/nonexistent/theme.yaml")), \
         patch("src.dashboard.db.load_state", new_callable=AsyncMock) as mock_load, \
         nullcontext(AsyncMock()) as mock_resp, \
         patch("src.dashboard.db.get_hourly_bars", new_callable=AsyncMock) as mock_bars, \
         uptime_pct_mock() as mock_uptime, \
         patch("src.dashboard.get_aggregate_stats", new_callable=AsyncMock) as mock_agg, \
         patch("src.dashboard.db.get_monitor_stats", new_callable=AsyncMock) as mock_stats, \
         bulk_db_calls(mock_load, mock_resp, mock_bars):
        mock_load.return_value = state
        mock_resp.return_value = 45.0
        mock_bars.return_value = bars
        mock_uptime.return_value = 99.9
        mock_agg.return_value = agg
        mock_stats.return_value = {"total_checks": 100, "total_up": 99, "total_down": 1, "total_timeout": 0, "outages": 1}

        client = TestClient(app)
        dashboard_html = client.get("/").text
        monitor_html = client.get("/monitor/web-server").text

    return dashboard_html, monitor_html


@pytest.fixture
def responsive_page(page, rendered_pages):
    """A real page loaded with the real rendered dashboard.html, its
    real static JS served from disk, and the real /monitor/{name}
    fragment wired up behind a fake origin (page.goto() rather than
    set_content() -- relative-path <script src> tags need a real
    origin to resolve against, set_content()'s about:blank doesn't
    give them one)."""
    dashboard_html, monitor_html = rendered_pages

    def serve_static(route):
        # Split off any ?v=<version> cache-busting query string (see
        # dashboard.html's matching comment) before resolving a file
        # path -- a real static file server ignores query strings for
        # file lookup too, they're purely a hint to the browser's own
        # cache, not part of the actual path on disk.
        rel_path = route.request.url.split("/static/", 1)[1].split("?", 1)[0]
        fpath = STATIC_DIR / rel_path
        if fpath.exists():
            route.fulfill(
                status=200,
                content_type=CONTENT_TYPES.get(fpath.suffix, "application/octet-stream"),
                body=fpath.read_bytes(),
            )
        else:
            route.fulfill(status=404, body=b"")

    page.route(
        "http://uptime-herald.test/",
        lambda route: route.fulfill(status=200, content_type="text/html", body=dashboard_html),
    )
    page.route("**/static/**", serve_static)
    # Registered after the checks-specific route below so it wins for
    # everything else (Playwright dispatches matching routes most-
    # recently-registered-first) -- the graph/response-times endpoint,
    # in particular, just needs *some* valid JSON, not real data.
    page.route(
        "**/api/**",
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"data":[]}'),
    )
    page.route(
        "**/api/monitor/**/checks*",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"checks":[{"time":"2026-08-16 09:00:00","status":"up",'
                 '"response_ms":42.3,"details":{},"error":null}],'
                 '"total":1,"total_pages":1,"page":1}',
        ),
    )
    page.route(
        "**/monitor/*",
        lambda route: route.fulfill(status=200, content_type="text/html", body=monitor_html),
    )
    page.goto("http://uptime-herald.test/")
    return page


# --- Breakpoint: which build shows at which width -------------------------------------

def test_desktop_width_shows_two_pane_layout(responsive_page):
    responsive_page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
    assert responsive_page.is_visible(".layout")
    assert not responsive_page.is_visible(".mobile-shell")


def test_mobile_width_shows_mobile_shell(responsive_page):
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    assert not responsive_page.is_visible(".layout")
    assert responsive_page.is_visible(".mobile-shell")


def test_breakpoint_boundary_is_exactly_1366(responsive_page):
    """1365px is the last mobile width, 1366px is the first desktop width,
    so desktop monitors get the two-pane layout and everything else gets
    mobile."""
    responsive_page.set_viewport_size({"width": 1365, "height": 900})
    assert responsive_page.is_visible(".mobile-shell")
    responsive_page.set_viewport_size({"width": 1366, "height": 900})
    assert responsive_page.is_visible(".layout")


# --- Mobile: list view is home, filters live only there -------------------------------

def test_mobile_default_state_is_list_view(responsive_page):
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    assert responsive_page.is_visible(".mobile-list-view")
    assert not responsive_page.is_visible("#mobile-detail-view")


def test_mobile_list_view_has_filters_and_monitor_rows(responsive_page):
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    assert responsive_page.is_visible("#status-filter-mobile")
    assert responsive_page.locator(".mobile-list-view .monitor-row").count() == 2


def test_footer_present_and_scrolls_with_list_and_detail_view(responsive_page):
    """The footer is present exactly once in each view's DOM (list view and
    detail view), and it is ordinary scrolling content -- it moves with the
    container's scroll position rather than staying fixed."""
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 300})  # short, forces scroll

    assert responsive_page.locator(".mobile-list-view .sidebar-footer").count() == 1

    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".mobile-detail-view .detail-view")
    assert responsive_page.locator(".mobile-detail-view .detail-footer-mobile").count() == 1

    responsive_page.eval_on_selector("#mobile-detail-view", "el => el.scrollTo(0, 0)")
    top_at_scroll_top = responsive_page.eval_on_selector(
        ".mobile-detail-view .detail-footer-mobile", "el => el.getBoundingClientRect().top"
    )
    responsive_page.eval_on_selector(
        "#mobile-detail-view", "el => el.scrollTo(0, el.scrollHeight)"
    )
    top_at_scroll_bottom = responsive_page.eval_on_selector(
        ".mobile-detail-view .detail-footer-mobile", "el => el.getBoundingClientRect().top"
    )
    assert top_at_scroll_top != top_at_scroll_bottom  # moved with scroll, not fixed


def test_list_footer_sits_at_true_bottom_when_content_is_short(responsive_page):
    """With only a couple monitors configured, the footer sits at the bottom
    of the screen, not hugging the bottom of the short list. That needs
    .monitor-list to have flex:1 to grow into the leftover space and
    .mobile-list-view (its parent) to be display:flex, because the `flex`
    property only means anything on an actual flex item. A tall viewport
    with the fixture's default 2-monitor list is short relative to it either
    way, so this needs no special content shaping."""
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    footer_bottom = responsive_page.eval_on_selector(
        ".mobile-list-view .sidebar-footer", "el => el.getBoundingClientRect().bottom"
    )
    assert footer_bottom == 844


def test_detail_footer_sits_at_true_bottom_when_content_is_short(responsive_page):
    """Same, detail view side -- .mobile-detail-view is a flex container
    (display:flex) when shown, with .detail-view given flex:1 to grow into
    it, for the same reason as the list view above.
    A single monitor's real detail content (stats, bars, latency
    graph, check log) is comfortably taller than an ordinary phone
    viewport regardless of check-log length, so this uses an
    artificially tall viewport to actually exercise the "less content
    than available space" branch rather than the "content
    overflows, scroll together" branch every other detail-view test
    in this file already covers."""
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 2500})
    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".mobile-detail-view .detail-footer-mobile")
    footer_bottom = responsive_page.eval_on_selector(
        ".mobile-detail-view .detail-footer-mobile", "el => el.getBoundingClientRect().bottom"
    )
    assert footer_bottom == 2500


def test_detail_footer_hidden_on_desktop(responsive_page):
    """The same monitor.html fragment feeds both #detail-panel (desktop)
    and #mobile-detail-view (mobile) -- its mobile-only footer must stay
    hidden on desktop, where the persistent sidebar already has its own,
    not appear a second time inside the detail content itself."""
    responsive_page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
    responsive_page.click(".sidebar .monitor-row >> nth=0")
    responsive_page.wait_for_selector("#detail-panel .detail-view")
    assert not responsive_page.is_visible("#detail-panel .detail-footer-mobile")


# --- Mobile: tap a monitor -> detail view, no aggregate landing page ------------------

def test_selecting_monitor_shows_detail_and_hides_list(responsive_page):
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".mobile-detail-view .detail-view")
    assert responsive_page.is_visible("#mobile-detail-view")
    assert not responsive_page.is_visible(".mobile-list-view")


def test_detail_view_check_log_is_accordion_not_table(responsive_page):
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".check-item")
    assert not responsive_page.is_visible(".mobile-detail-view .check-log table")
    assert responsive_page.is_visible(".mobile-detail-view .check-log-mobile")


def test_accordion_item_expands_on_click(responsive_page):
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".check-item")
    item = responsive_page.locator(".check-item").first
    assert item.get_attribute("open") is None
    item.locator("summary").click()
    assert item.get_attribute("open") is not None
    assert "42.3ms" in item.inner_text()


# --- Header logo doubles as back-to-list on mobile ------------------------------------

def test_header_logo_returns_to_list_view(responsive_page):
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".mobile-detail-view .detail-view")

    responsive_page.click(".mobile-shell .sidebar-header")

    # The logo tap goes through history.back() (see
    # test_real_back_button_returns_to_list_view_not_the_previous_page
    # below for why) rather than synchronously toggling a CSS class,
    # so the visual update lands on the next popstate event, not
    # necessarily before click() returns.
    responsive_page.wait_for_selector(".mobile-list-view:visible")
    assert responsive_page.is_visible(".mobile-list-view")
    assert not responsive_page.is_visible("#mobile-detail-view")


def test_logo_tap_in_list_view_is_a_harmless_noop(responsive_page):
    """The logo's onclick always intercepts the click, even when
    there's no detail view open to back out of -- confirms it doesn't
    call history.back() in that case (which would navigate the tab
    away from the dashboard entirely, popping past this page load)."""
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    responsive_page.click(".mobile-shell .sidebar-header")
    responsive_page.wait_for_timeout(200)
    assert responsive_page.is_visible(".mobile-list-view")


def test_real_back_button_returns_to_list_view_not_the_previous_page(responsive_page):
    """On mobile, the device's own back gesture returns to the monitor list
    rather than leaving the site. Selecting a monitor is a fetch()+innerHTML
    swap, never a real navigation, so the page pushes a history entry when
    entering detail view and restores the list view on popstate. This test
    drives the browser's actual go_back() (not a click on our own logo
    element), so it covers the real gesture, not just the in-page control
    that has its own test above."""
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".mobile-detail-view .detail-view")

    responsive_page.go_back()

    responsive_page.wait_for_selector(".mobile-list-view:visible")
    assert responsive_page.is_visible(".mobile-list-view")
    assert not responsive_page.is_visible("#mobile-detail-view")


def test_selecting_monitor_actually_changes_the_url(responsive_page):
    """Each detail view state has a real, distinct URL. A pushState with a
    blank URL is not treated as a real, back-navigable step by some mobile
    browsers (Firefox offers no back affordance for it; DuckDuckGo's back
    button skips past it to the browser's own new-tab page), although
    Chromium, which this whole suite runs against, is lenient about it. This
    test pins down that the URL actually changes."""
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    url_before = responsive_page.url

    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".mobile-detail-view .detail-view")

    assert responsive_page.url != url_before
    assert "#monitor/" in responsive_page.url


def test_switching_monitors_in_detail_view_then_back_goes_straight_to_list(responsive_page):
    """Selecting a second monitor while already in detail view must
    replace the pushed history entry, not stack a new one on top of
    it -- otherwise a single back press from B's detail view would
    land back on A's detail view instead of the list, one step at a
    time through every previously-viewed monitor."""
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    rows = responsive_page.locator(".mobile-list-view .monitor-row")
    assert rows.count() >= 2, "fixture needs at least 2 monitors for this test"

    rows.nth(0).click()
    responsive_page.wait_for_selector(".mobile-detail-view .detail-view")

    # The list markup is still in the DOM underneath the detail
    # overlay (just display:none via CSS), so a real Playwright
    # .click() -- which requires an actionable, visible element --
    # won't reach it. Dispatch the click event directly instead, the
    # same event the browser would fire, without requiring visibility.
    rows.nth(1).dispatch_event("click")
    responsive_page.wait_for_timeout(200)

    responsive_page.go_back()

    responsive_page.wait_for_selector(".mobile-list-view:visible")
    assert responsive_page.is_visible(".mobile-list-view")
    assert not responsive_page.is_visible("#mobile-detail-view")


# --- Regression: .mobile-shell must not be widened by its own content -----------------

def test_mobile_shell_is_not_widened_by_untrimmed_bar_content(responsive_page):
    """.mobile-shell must not be widened by its own content. It is a flex item
    of <body>'s row layout and needs the flex:1 + min-width:0 that .layout has
    for the exact same reason; without them, a flex item sizes to its own
    content's natural width, and 360 un-shrunk detail bars (8px + 2px gap
    each) are ~3600px of un-trimmed content before trimDetailBars() ever gets
    a correct width to measure against. Every width:100% descendant (the
    graph included) would inherit that inflated size. This pins it down at
    the DOM level, not just "does it look right in a screenshot"."""
    responsive_page.set_viewport_size({"width": MOBILE_WIDTH, "height": 844})
    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".mobile-detail-view .detail-view")

    shell_width = responsive_page.eval_on_selector(
        ".mobile-shell", "el => el.getBoundingClientRect().width"
    )
    graph_width = responsive_page.eval_on_selector(
        "#response-graph", "el => el.getBoundingClientRect().width"
    )
    body_scroll_width = responsive_page.eval_on_selector(
        "body", "el => el.scrollWidth"
    )

    assert shell_width == MOBILE_WIDTH
    assert graph_width <= MOBILE_WIDTH
    assert body_scroll_width == MOBILE_WIDTH  # no horizontal overflow anywhere


def test_worst_case_check_log_row_fits_360px_baseline(responsive_page):
    """At 360px, the narrowest width designed for (the rest of this suite
    tests at 390px), the check-log rows must not clip. Overrides the shared
    checks route with the actual worst case -- the longest realistic
    date/time format (babel "medium", 23-25 chars) plus "timeout" (the
    longest real status value) -- rather than the short fixture data the
    rest of this file uses."""
    responsive_page.route(
        "**/api/monitor/**/checks*",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"checks":[{"time":"Jan 16, 2026, 12:41:32 PM",'
                 '"status":"timeout","response_ms":null,"details":{},'
                 '"error":"Connection timed out"}],'
                 '"total":1,"total_pages":1,"page":1}',
        ),
    )
    responsive_page.set_viewport_size({"width": 360, "height": 900})
    responsive_page.click(".mobile-list-view .monitor-row >> nth=0")
    responsive_page.wait_for_selector(".check-item")

    body_scroll_width = responsive_page.eval_on_selector("body", "el => el.scrollWidth")
    assert body_scroll_width == 360


# --- Desktop path is unaffected -------------------------------------------------------

def test_desktop_selection_still_works_after_mobile_markup_added(responsive_page):
    """Regression check: adding .mobile-shell alongside .layout in the
    same document shouldn't change desktop's own selectMonitor()/
    #detail-panel behavior."""
    responsive_page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
    responsive_page.click(".sidebar .monitor-row >> nth=0")
    responsive_page.wait_for_selector("#detail-panel .detail-view")
    assert "detail-view" in responsive_page.eval_on_selector(
        "#detail-panel", "el => el.innerHTML"
    )


# --- Desktop back button --------------------------------------------------------------
#
# Going to the detail view and then pressing the back button returns to the
# main view instead of leaving the page, as on mobile (selectMonitor() pushes
# a history entry too), even though both panes are always on screen at once.

def test_desktop_real_back_button_returns_to_combined_view(responsive_page):
    responsive_page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
    responsive_page.click(".sidebar .monitor-row >> nth=0")
    responsive_page.wait_for_selector("#detail-panel .detail-view")

    responsive_page.go_back()

    # [data-stat] only exists in the combined view's aggregate-stats
    # markup (dashboard.html), never in a single monitor's own
    # fragment (monitor.html) -- a reliable, distinguishing signal
    # that the restore actually happened rather than just leaving
    # stale detail-view content on screen.
    responsive_page.wait_for_selector('#detail-panel [data-stat="sites_up"]')
    assert not responsive_page.is_visible("#detail-panel .detail-view")
    assert not responsive_page.locator(".sidebar .monitor-row.selected").count()


def test_desktop_switching_monitors_then_back_goes_straight_to_combined_view(responsive_page):
    """Mirrors test_switching_monitors_in_detail_view_then_back_goes_
    straight_to_list above, for desktop's history.replaceState() path
    instead of mobile's."""
    responsive_page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
    rows = responsive_page.locator(".sidebar .monitor-row")
    assert rows.count() >= 2, "fixture needs at least 2 monitors for this test"

    rows.nth(0).click()
    responsive_page.wait_for_selector("#detail-panel .detail-view")
    rows.nth(1).click()
    responsive_page.wait_for_selector("#detail-panel .detail-view")

    responsive_page.go_back()

    responsive_page.wait_for_selector('#detail-panel [data-stat="sites_up"]')
    assert not responsive_page.is_visible("#detail-panel .detail-view")


def test_desktop_back_then_forward_returns_to_the_same_monitor(responsive_page):
    """Confirms the pushed history entry round-trips both ways, not
    just back -- selecting a monitor, going back to the combined view,
    then forward again should land on that same monitor's detail, the
    same way real browser history navigation is expected to behave."""
    responsive_page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
    responsive_page.click(".sidebar .monitor-row >> nth=0")
    responsive_page.wait_for_selector("#detail-panel .detail-view")
    selected_name = responsive_page.eval_on_selector(
        "#detail-panel .detail-view", "el => el.dataset.monitor"
    )

    responsive_page.go_back()
    responsive_page.wait_for_selector('#detail-panel [data-stat="sites_up"]')

    responsive_page.go_forward()
    responsive_page.wait_for_selector("#detail-panel .detail-view")
    assert (
        responsive_page.eval_on_selector("#detail-panel .detail-view", "el => el.dataset.monitor")
        == selected_name
    )


# --- Themed filter dropdown option-hover color ----------------------------------------
#
# The sidebar filter dropdowns' open-option-list hover highlight is
# themed to match the app, rather than each browser's own native
# (often blue) accent color. Uses the new "customizable select"
# CSS (appearance: base-select on both the <select> and its own
# ::picker(select)) -- a real progressive enhancement: browsers that
# don't recognize the property (such as Firefox and Safari, until they
# support it) just ignore it and keep rendering their ordinary native
# dropdown, so this can never make things worse, only better where it's
# supported.

def test_filter_dropdown_option_hover_uses_theme_accent_color(responsive_page):
    responsive_page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})

    appearance = responsive_page.eval_on_selector(
        "#status-filter", "el => getComputedStyle(el).appearance"
    )
    assert appearance == "base-select"

    responsive_page.click("#status-filter")
    responsive_page.wait_for_selector('#status-filter option[value="down"]')
    responsive_page.hover('#status-filter option[value="down"]')
    responsive_page.wait_for_timeout(100)

    bg = responsive_page.eval_on_selector(
        '#status-filter option[value="down"]', "el => getComputedStyle(el).backgroundColor"
    )
    accent_color = _get_normalized_variable_color(responsive_page, "--border-accent")
    assert bg == accent_color


# --- Kiosk shares monitor.html but not the mobile CSS ---------------------------------

def test_mobile_only_fragment_elements_hidden_on_kiosk(page, rendered_pages):
    """The kiosk view renders the same monitor.html fragment as dashboard.html
    (same /monitor/{name} route, same template) but deliberately does NOT
    include _style_mobile.html (kiosk isn't responsive). The
    accordion/mobile-footer's "hidden by default" base rule therefore lives in
    _style_common.html, which both pages include; in _style_mobile.html it
    would never apply on kiosk, and the elements would render fully unstyled
    instead of hidden."""
    _, monitor_html = rendered_pages
    kiosk_html = client_get_kiosk_html()

    page.set_content(kiosk_html)
    page.evaluate(
        "(html) => { document.getElementById('detail-panel').innerHTML = html; }",
        monitor_html,
    )

    assert not page.is_visible(".check-log-mobile")
    assert not page.is_visible(".detail-footer-mobile")
    assert page.is_visible("#detail-panel .check-log table")


def client_get_kiosk_html() -> str:
    """Separate mini-fixture, not folded into `rendered_pages`, since
    that one's module-scoped around dashboard.html/monitor.html and
    every other test in this file relies on its exact shape -- easier
    to keep kiosk's one-off render self-contained here than reshape a
    shared fixture for a single test."""
    config = copy.deepcopy(DEFAULTS)
    config["monitors"] = [
        {"name": "web-server", "type": "http", "url": "https://example.com", "tags": ["prod"]},
    ]
    state = MonitorState(name="web-server", type="http")
    state.status = "up"
    state.uptime_pct = 99.9
    state.last_check = None
    agg = {
        "uptime_pct": {"24h": 99.9, "7d": 99.8, "30d": 99.5, "90d": 99.3},
        "avg_latency": {"24h": 45.0, "7d": 48.0, "30d": 52.0, "90d": 55.0},
        "sites_maintenance": 0,
    }
    bars = [
        {"status": "up", "hour_display": f"h{h}", "hour_display_end": f"h{h + 1}"}
        for h in range(24)
    ]
    with patch("src.dashboard.load_config", return_value=config), \
         patch("src.dashboard.load_theme",
               side_effect=lambda *a, **kw: _real_load_theme("/nonexistent/theme.yaml")), \
         patch("src.dashboard.db.load_state", new_callable=AsyncMock) as mock_load, \
         nullcontext(AsyncMock()) as mock_resp, \
         patch("src.dashboard.db.get_hourly_bars", new_callable=AsyncMock) as mock_bars, \
         uptime_pct_mock() as mock_uptime, \
         patch("src.dashboard.get_aggregate_stats", new_callable=AsyncMock) as mock_agg, \
         bulk_db_calls(mock_load, mock_resp, mock_bars):
        mock_load.return_value = state
        mock_resp.return_value = 45.0
        mock_bars.return_value = bars
        mock_uptime.return_value = 99.9
        mock_agg.return_value = agg
        return TestClient(app).get("/kiosk").text


# --- Never-checked monitor shouldn't render as DOWN -----------------------------------
#
# A monitor that has never been checked has MonitorState's "unknown" status,
# which the sidebar health field reports as "nodata", not "down". Collapsed
# into "down", an inactive+never-checked row would carry BOTH .down and
# .inactive classes; .inactive wins while unselected (later in the stylesheet,
# same specificity), but .monitor-row.down.selected (3 classes) outranks
# .monitor-row.inactive (2 classes) the moment it's clicked, and the
# background turns red. Health is therefore a 3-way result (up/down/nodata).

def _render_inactive_monitor_dashboard(status: str | None = None) -> str:
    """A standalone render, not the shared rendered_pages fixture --
    needs a single inactive monitor with a specific/controlled status,
    which none of the shared fixture's monitors have. status=None
    leaves MonitorState's real default ("unknown", src/state.py) in
    place -- this monitor has never actually been checked."""
    config = copy.deepcopy(DEFAULTS)
    config["monitors"] = [
        {"name": "inactive-mon", "type": "http",
         "url": "https://example.com", "active": False},
    ]
    state = MonitorState(name="inactive-mon", type="http")
    if status is not None:
        state.status = status
    state.last_check = None
    # Aggregate stats are across ALL monitors, not this one -- with the
    # sole configured monitor inactive (excluded from up/down math),
    # a real get_aggregate_stats() would still return real numbers
    # here (0.0-ish), never None; using realistic values rather than
    # None avoids an unrelated Jinja "%.1f"|format(None) crash.
    agg = {
        "uptime_pct": {"24h": 0.0, "7d": 0.0, "30d": 0.0, "90d": 0.0},
        "avg_latency": {"24h": 0.0, "7d": 0.0, "30d": 0.0, "90d": 0.0},
        "sites_maintenance": 0,
    }
    with patch("src.dashboard.load_config", return_value=config), \
         patch("src.dashboard.load_theme",
               side_effect=lambda *a, **kw: _real_load_theme("/nonexistent/theme.yaml")), \
         patch("src.dashboard.db.load_state", new_callable=AsyncMock) as mock_load, \
         nullcontext(AsyncMock()) as mock_resp, \
         patch("src.dashboard.db.get_hourly_bars", new_callable=AsyncMock) as mock_bars, \
         patch("src.dashboard.get_aggregate_stats", new_callable=AsyncMock) as mock_agg, \
         bulk_db_calls(mock_load, mock_resp, mock_bars):
        mock_load.return_value = state
        mock_resp.return_value = None
        mock_bars.return_value = []
        mock_agg.return_value = agg
        return TestClient(app).get("/").text


def _get_bg(page, selector: str) -> str:
    return page.eval_on_selector(selector, "el => getComputedStyle(el).backgroundColor")


def _get_normalized_variable_color(page, var_name: str) -> str:
    """Return a CSS variable's color in the form getComputedStyle() reports.

    Comparing against the raw custom-property text (e.g. "#900") would never
    match, because getComputedStyle() always normalizes colors to rgb(...). So
    the variable is applied to a disposable element and that element's
    computed backgroundColor is read back."""
    return page.evaluate(f"""() => {{
        const d = document.createElement('div');
        d.style.background = 'var({var_name})';
        document.body.appendChild(d);
        const c = getComputedStyle(d).backgroundColor;
        d.remove();
        return c;
    }}""")


def test_never_checked_inactive_monitor_stays_neutral_when_clicked(page):
    dashboard_html = _render_inactive_monitor_dashboard()
    page.set_content(dashboard_html)
    page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
    sel = '.sidebar .monitor-row[data-name="inactive-mon"]'

    row = page.query_selector(sel)
    classes_before = row.get_attribute("class").split()
    assert "down" not in classes_before
    assert "nodata" in classes_before
    assert "inactive" in classes_before

    row.click()
    page.wait_for_timeout(100)

    classes_after = page.eval_on_selector(sel, "el => el.className").split()
    assert "down" not in classes_after
    assert "selected" in classes_after

    assert _get_bg(page, sel) != _get_normalized_variable_color(page, "--status-down")


# --- Inactive rows need real hover/selected feedback too ------------------------------
#
# An inactive row's own background rule can beat both :hover and .selected on
# specificity/source-order, for ANY inactive row. Neutral (nodata/up) inactive
# rows therefore need explicit hover and selected rules, or they show no
# feedback at all.

def test_inactive_neutral_monitor_shows_hover_and_selected_feedback(page):
    """An inactive monitor with nothing else giving it color (nodata,
    or genuinely up) must still show SOME visible feedback on hover
    and on click."""
    for status, label in [(None, "nodata"), ("up", "up")]:
        dashboard_html = _render_inactive_monitor_dashboard(status)
        page.set_content(dashboard_html)
        page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
        sel = '.sidebar .monitor-row[data-name="inactive-mon"]'

        bg_default = _get_bg(page, sel)
        page.hover(sel)
        page.wait_for_timeout(50)
        assert _get_bg(page, sel) != bg_default, f"no hover feedback for inactive+{label}"

        page.click(sel)  # a real click leaves the cursor hovering too
        page.wait_for_timeout(100)
        assert _get_bg(page, sel) != bg_default, f"no selected feedback for inactive+{label}"


def test_inactive_confirmed_down_monitor_keeps_red_when_selected_and_hovering(page):
    """The neutral hover/selected feedback stays scoped to rows with nothing
    else covering the interactive states -- a monitor that's both inactive
    AND has genuine down history keeps its red treatment. Checked with the
    cursor still hovering after the click (what a real click actually leaves
    behind), not just click alone: :hover alone ties .down.selected on CSS
    specificity and, being later in the stylesheet, would win the tie and
    override the red the moment the mouse stays put after a click."""
    dashboard_html = _render_inactive_monitor_dashboard("down")
    page.set_content(dashboard_html)
    page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 1080})
    sel = '.sidebar .monitor-row[data-name="inactive-mon"]'

    page.click(sel)
    page.wait_for_timeout(200)  # clear of the 0.15s background transition

    bg = _get_bg(page, sel)
    down_color = _get_normalized_variable_color(page, "--status-down")
    neutral_hover_color = _get_normalized_variable_color(page, "--bg-list-row-hover")
    assert bg == down_color
    assert bg != neutral_hover_color
