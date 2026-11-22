/*
 * Uptime Herald - https://uptimeherald.org
 * Copyright (C) 2026 Uptime Herald Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 *
 * dashboard-mobile.js - Mobile dashboard behavior in the browser.
 */
/*
 * Mobile-specific view logic for dashboard.html (< 1366px layout, see
 * _style_mobile.html). Structurally independent from the desktop path
 * in dashboard.html's own inline <script> — this file only reuses the
 * global filter state/functions (currentSelectedMonitor, T,
 * filterByStatus/filterByType/filterByTag) that script declares, not
 * its DOM structure or click handlers. Loaded by dashboard.html only,
 * never kiosk.html.
 */

// -------------------------------------------------------------------------------------
// Restores the list view's visual state. The one and only place that
// actually does so -- both the header-logo tap (mobileGoHome) and a
// real browser/device back button route through the popstate handler
// below rather than duplicating this logic, so the two can never get
// out of sync with each other.
function _showMobileListView() {
    const shell = document.querySelector('.mobile-shell');
    if (shell) shell.classList.remove('showing-detail');
    document.querySelectorAll('.monitor-row').forEach(r => r.classList.remove('selected'));
    currentSelectedMonitor = null;
}

// Selecting a monitor never causes a real page navigation -- it's a
// fetch() + innerHTML swap, same page the whole time -- so without
// deliberately pushing a history entry, the browser has nothing of
// ours to land "back" on. It falls straight through to whatever page
// was open before the dashboard, taking the user out of the site
// entirely instead of back to the monitor list. The same gap exists in
// desktop's selectMonitor() too, just less visible there since both
// panes are already on screen at once.
window.addEventListener('popstate', (event) => {
    // dashboard.html's own inline script registers an equivalent
    // listener for the desktop view, and both fire on every popstate
    // event regardless of which one is actually on screen -- neither
    // file knows about the other. Checking actual computed visibility
    // (rather than duplicating the 1366px breakpoint as a second
    // magic number here) keeps this a no-op whenever desktop's
    // .layout is the one currently shown.
    const shell = document.querySelector('.mobile-shell');
    if (!shell || getComputedStyle(shell).display === 'none') return;
    if (!event.state || event.state.mobileView !== 'detail') {
        _showMobileListView();
    } else {
        // Forward navigation (or any pop that lands back ON a detail
        // entry). Without this branch, pressing forward again after going
        // back would leave the list showing while the URL said
        // #monitor/<name>, so the view and the address bar would disagree
        // until a manual reload. Restores the view only; the history entry
        // being replayed already exists, so this must not touch history
        // itself.
        _showMobileDetailView(event.state.monitor);
    }
});

// -------------------------------------------------------------------------------------
// The mobile header's logo doubles as the back-to-list control while
// the detail view is showing. Always intercepts the click (even from
// the list view, where it's a harmless no-op) — href="/" stays as a
// plain-navigation fallback for anyone/anything without JS.
function mobileGoHome() {
    const shell = document.querySelector('.mobile-shell');
    if (shell && shell.classList.contains('showing-detail')) {
        // Goes through the same popstate-driven path a real back
        // press takes, rather than calling _showMobileListView()
        // directly here -- one code path for "leave detail view",
        // so this can never drift out of sync with what the
        // hardware/gesture back button does.
        history.back();
    }
    return false;
}

// -------------------------------------------------------------------------------------
// Restores the detail view's visual state and loads its content, with
// NO history side effects. The counterpart to _showMobileListView()
// above, and split out for the same reason: both a fresh tap
// (selectMonitorMobile) and a forward navigation (the popstate handler)
// need to show a monitor, but only the tap should touch history --
// calling the full selectMonitorMobile() from popstate would push a new
// entry while the browser is mid-navigation, corrupting the very stack
// it was replaying.
function _showMobileDetailView(name, rowEl) {
    currentSelectedMonitor = name;
    const shell = document.querySelector('.mobile-shell');
    const panel = document.getElementById('mobile-detail-view');
    if (!panel) return;

    document.querySelectorAll('.monitor-row').forEach(r => r.classList.remove('selected'));
    // On a forward navigation there's no clicked element to pass, so
    // find the row by name to keep the list's selected highlight in
    // sync with what the detail view is showing.
    const row = rowEl || document.querySelector(
        '.monitor-row[data-name="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]'
    );
    if (row) row.classList.add('selected');

    if (shell) shell.classList.add('showing-detail');

    panel.innerHTML = '<div class="empty-state">' + T.loading + '</div>';

    fetch('/monitor/' + encodeURIComponent(name))
        .then(r => {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
        })
        .then(html => {
            panel.innerHTML = html;
            // Scripts injected via innerHTML don't execute —
            // manually re-run them (same pattern as selectMonitor()
            // in dashboard.html's own inline script)
            panel.querySelectorAll('script').forEach(old => {
                const s = document.createElement('script');
                if (old.src) s.src = old.src;
                else s.textContent = old.textContent;
                old.parentNode.replaceChild(s, old);
            });
        })
        .catch(err => {
            panel.innerHTML = '<div class="empty-state">' + T.failed_monitor + ' ' + err.message + '</div>';
        });
}

// -------------------------------------------------------------------------------------
function selectMonitorMobile(name, rowEl) {
    const shell = document.querySelector('.mobile-shell');
    const alreadyShowingDetail = shell && shell.classList.contains('showing-detail');

    // The URL itself is deliberately changed too (a #monitor/<name> hash), not
    // left blank. A state-only push with an unchanged URL isn't reliably
    // treated as a real, back-navigable step on mobile: Firefox doesn't offer
    // a back affordance for it at all, and DuckDuckGo's back button skips past
    // it entirely, landing on the browser's new-tab page. Giving each state a
    // distinct URL avoids that -- the same technique every single-page-app
    // router uses. #monitor/... rather than a bare #<name> specifically to
    // avoid any risk of colliding with the browser's native
    // scroll-to-matching-element-id behavior if a monitor's name ever
    // happened to match an element's id.
    //
    // One history entry represents "detail view is open", regardless of which
    // monitor. Switching directly between monitors while already in detail
    // view replaces that entry instead of stacking a new one on top --
    // otherwise a single back press would only step back through
    // previously-viewed monitors one at a time instead of returning straight to
    // the list, which isn't what a "back" press from a detail view reads as on
    // a phone.
    const url = '#monitor/' + encodeURIComponent(name);
    if (alreadyShowingDetail) {
        history.replaceState({mobileView: 'detail', monitor: name}, '', url);
    } else {
        history.pushState({mobileView: 'detail', monitor: name}, '', url);
    }

    _showMobileDetailView(name, rowEl);
}
