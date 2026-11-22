/*
 * Uptime Herald - https://uptimeherald.org
 * Copyright (C) 2026 Uptime Herald Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 *
 * dashboard-common.js - Dashboard and kiosk behavior in the browser.
 */
/*
 * Shared client-side logic for dashboard.html and kiosk.html.
 *
 * Shared by both pages rather than duplicated in each page's inline <script>
 * block. Loaded as a plain <script src> tag, synchronously,
 * before each page's own remaining inline <script> block — so by the time
 * any of these functions is actually CALLED, the calling page has already
 * defined the globals they read (T, CHECKS_PER_PAGE). Tag order matters
 * for load/definition, not for these function bodies, which only look up
 * T / CHECKS_PER_PAGE at call time.
 */

// -------------------------------------------------------------------------------------
// Escapes for both element-content and quoted-attribute contexts. The
// textContent/innerHTML round-trip alone only escapes &, < and > -- the
// serializer leaves quotes untouched, since they're harmless in text.
// They are not harmless in `title="..."`, which is exactly how this is
// used in updateMonitorList()'s bar markup, so the quote pair is
// escaped explicitly rather than relying on the caller's context.
function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// --- Hide uptime bars that don't fit container width (no partials) -------------------
const BAR_WIDTH = 8;
const BAR_GAP = 2;
const BAR_STEP = BAR_WIDTH + BAR_GAP;

// The hover label of each list bar, newest bar first. Every monitor shares
// them (a label depends only on the hour), so the page and /api/status carry
// them once instead of once per bar. Assigned as a property, never as markup,
// so nothing in a label can be read as HTML.
let BAR_LABELS = [];

// -------------------------------------------------------------------------------------
function applyBarTitles() {
    document.querySelectorAll('.row-bars').forEach(container => {
        const bars = container.children;
        const last = bars.length - 1;
        // The row shows its bars oldest first, so the newest is the last one.
        for (let i = 0; i <= last; i++) bars[i].title = BAR_LABELS[last - i] || '';
    });
}

// -------------------------------------------------------------------------------------
function trimRowBars() {
    document.querySelectorAll('.row-bars').forEach(container => {
        const w = container.clientWidth;
        if (w <= 0) return;
        const maxBars = Math.floor((w + BAR_GAP) / BAR_STEP);
        const bars = container.querySelectorAll('.bar');
        bars.forEach(b => b.style.display = ''); // reset
        const hideCount = bars.length - maxBars;
        for (let i = 0; i < hideCount; i++) {
            bars[i].style.display = 'none'; // hide oldest (leftmost)
        }
    });
}

// --- Monitor list in-place update ----------------------------------------------------
// 'monitor-list' is the desktop sidebar; 'monitor-list-mobile' is the
// mobile build's own list (dashboard.html only — absent on kiosk.html,
// getElementById just returns null and that iteration is skipped).
// Both hold identical rows for identical data, so one update pass
// covers whichever is actually in the page.
function updateMonitorList(monitors, barLabels) {
    if (barLabels) BAR_LABELS = barLabels;
    ['monitor-list', 'monitor-list-mobile'].forEach(listId => {
        const list = document.getElementById(listId);
        if (!list) return;

        // Build a map of existing rows
        const existing = {};
        list.querySelectorAll('.monitor-row').forEach(row => {
            existing[row.dataset.name] = row;
        });

        monitors.forEach(m => {
            const row = existing[m.name];
            if (!row) return;  // new monitors appear on next full page load

            // Update health class. m.health is one of 'up'/'down'/'nodata'
            // (see dashboard.py's _gather_one -- 'nodata' is a monitor that
            // has never been checked, not a failure). It must be passed
            // through as the initial Jinja-rendered row does, or such a
            // monitor would flip to a wrongly-confident 'up' class on the
            // next auto-refresh poll.
            // Mirror the Jinja row exactly: the health class is ALWAYS
            // applied, and 'inactive' is added *alongside* it rather than
            // instead of it. The templates emit
            // `{{ m.health }}{{ " inactive" if ... }}`, and
            // _style_common.html deliberately keys on
            // `.monitor-row.inactive:not(.down)` so a monitor that is both
            // inactive AND has genuine down history keeps its red instead of
            // falling back to the neutral inactive treatment. Replacing the
            // health class with 'inactive' here would drop that red on the
            // first auto-refresh poll.
            row.classList.remove('up', 'down', 'nodata', 'inactive');
            row.classList.add(m.health);
            if (!m.active || m.in_maintenance) {
                row.classList.add('inactive');
            }

            // Update data-status for status filter
            row.dataset.status = !m.active ? 'inactive' : m.in_maintenance ? 'maintenance' : m.health;

            // Update data-tags for tag filter
            row.dataset.tags = (m.tags || []).join(',');

            // Update uptime percentage
            const pctEl = row.querySelector('.uptime-pct');
            if (pctEl) {
                if (!m.active) {
                    pctEl.textContent = T.inactive;
                } else if (m.in_maintenance) {
                    pctEl.textContent = T.maint;
                } else if (m.uptime_pct != null) {
                    pctEl.textContent = m.uptime_pct.toFixed(UPTIME_DECIMALS) + '%';
                } else {
                    pctEl.textContent = '';
                }
            }

            // Update bars
            const barsContainer = row.querySelector('.row-bars');
            if (barsContainer && m.bars) {
                // The desktop and kiosk sidebar only has room for SIDEBAR_BARS
                // bars; the mobile list can be as wide as the screen.
                const shown = listId === 'monitor-list'
                    ? m.bars.slice(0, SIDEBAR_BARS) : m.bars;
                barsContainer.innerHTML = shown.slice().reverse().map(
                    status => '<div class="bar ' + status + '"></div>'
                ).join('');
            }
        });

        // Re-order rows to match server-sorted order from the API
        monitors.forEach(m => {
            const row = existing[m.name];
            if (row) list.appendChild(row);
        });
    });

    // Re-trim bars after update (covers every .row-bars in the page,
    // desktop and mobile alike)
    trimRowBars();
    applyBarTitles();

    // Re-apply filters after row updates, if this page has filters
    // (kiosk has no filter UI, so applyFilters won't exist there)
    if (typeof applyFilters === 'function') applyFilters();
}

// --- Aggregate stats update ----------------------------------------------------------
function updateAggStats(stats) {
    const periodKey = stats.stats_period_key;

    const updates = {
        'sites_up': stats.sites_up,
        'sites_down': stats.sites_down,
        'sites_inactive': stats.sites_inactive,
        'sites_maintenance': stats.sites_maintenance,
        'uptime_24h': stats.uptime_pct['24h'] != null ? stats.uptime_pct['24h'].toFixed(UPTIME_DECIMALS) + '%' : '—',
        'uptime_7d': stats.uptime_pct['7d'] != null ? stats.uptime_pct['7d'].toFixed(UPTIME_DECIMALS) + '%' : '—',
        'uptime_30d': stats.uptime_pct['30d'] != null ? stats.uptime_pct['30d'].toFixed(UPTIME_DECIMALS) + '%' : '—',
        'uptime_period': stats.uptime_pct[periodKey] != null ? stats.uptime_pct[periodKey].toFixed(UPTIME_DECIMALS) + '%' : '—',
        'latency_24h': stats.avg_latency['24h'] != null ? stats.avg_latency['24h'].toFixed(LATENCY_DECIMALS) + 'ms' : '—',
        'latency_7d': stats.avg_latency['7d'] != null ? stats.avg_latency['7d'].toFixed(LATENCY_DECIMALS) + 'ms' : '—',
        'latency_30d': stats.avg_latency['30d'] != null ? stats.avg_latency['30d'].toFixed(LATENCY_DECIMALS) + 'ms' : '—',
        'latency_period': stats.avg_latency[periodKey] != null ? stats.avg_latency[periodKey].toFixed(LATENCY_DECIMALS) + 'ms' : '—',
    };

    for (const [key, value] of Object.entries(updates)) {
        const el = document.querySelector('[data-stat="' + key + '"]');
        if (el) el.textContent = value;
    }

    // Toggle down state on the DOWN card
    const downCard = document.querySelector('[data-stat="sites_down"]')?.closest('.metric-card');
    if (downCard) downCard.classList.toggle('down', stats.sites_down > 0);

    // Toggle up state on the UP card (green when all monitors up)
    const upCard = document.querySelector('[data-stat="sites_up"]')?.closest('.metric-card');
    if (upCard) upCard.classList.toggle('up', stats.sites_down === 0 && stats.sites_up > 0);
}

// -------------------------------------------------------------------------------------
// Silent refresh of monitor detail (no loading flash)
// 'detail-panel' (desktop) and 'mobile-detail-view' (mobile build) both
// get fed the exact same /monitor/{name} fragment — refresh whichever
// one is actually showing it, not just the desktop panel.
function refreshMonitorDetail(name) {
    fetch('/monitor/' + encodeURIComponent(name) + '?_t=' + Date.now())
        .then(r => {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
        })
        .then(html => {
            ['detail-panel', 'mobile-detail-view'].forEach(panelId => {
                const panel = document.getElementById(panelId);
                if (!panel || !panel.querySelector('.detail-view')) return;
                panel.innerHTML = html;
                // Scripts injected via innerHTML don't execute —
                // manually re-run them
                panel.querySelectorAll('script').forEach(old => {
                    const s = document.createElement('script');
                    if (old.src) s.src = old.src;
                    else s.textContent = old.textContent;
                    old.parentNode.replaceChild(s, old);
                });
            });
        })
        .catch(err => {
            // Silent fail — keep existing content
            console.warn('Detail refresh failed:', err);
        });
}

// --- Default check log (all monitors) ------------------------------------------------

// Set by renderChecks() every time it actually draws the table, so
// loadChecksQuiet() below can tell "nothing changed" from "something did"
// without guessing.
let _lastChecksSnapshot = null;

// -------------------------------------------------------------------------------------
function loadChecks(page) {
    const tbody = document.getElementById('checks-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="muted">' + T.loading + '</td></tr>';
    fetch('/api/checks?page=' + page + '&per_page=' + CHECKS_PER_PAGE)
        .then(r => r.json())
        .then(data => {
            renderChecks(data);
        })
        .catch(() => {
            tbody.innerHTML = '<tr><td colspan="5" class="muted">' + T.failed_load + '</td></tr>';
        });
}

// -------------------------------------------------------------------------------------
// Background-poll variant used by autoRefresh(): fetches quietly (no
// "Loading…" flash) and only touches the DOM if the fetched page actually
// differs from what's already shown — otherwise the check log would visibly
// reload every auto-refresh cycle even when nothing new happened, the same
// flicker the monitor detail view avoids with the state_changed gate in
// autoRefresh.
function loadChecksQuiet(page) {
    const tbody = document.getElementById('checks-tbody');
    if (!tbody) return;
    fetch('/api/checks?page=' + page + '&per_page=' + CHECKS_PER_PAGE)
        .then(r => r.json())
        .then(data => {
            const snapshot = JSON.stringify(data);
            if (snapshot === _lastChecksSnapshot) return;
            renderChecks(data);
        })
        .catch(err => {
            // Silent fail — keep showing existing content
            console.warn('Quiet check-log refresh failed:', err);
        });
}

// -------------------------------------------------------------------------------------
function renderChecks(data) {
    _lastChecksSnapshot = JSON.stringify(data);
    const tbody = document.getElementById('checks-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (data.checks.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">' + T.no_checks + '</td></tr>';
    } else {
        data.checks.forEach(c => {
            const tr = document.createElement('tr');
            // Trust the server's stored classification (which knows about a
            // monitor's expected_status override) rather than re-deriving
            // it here from the raw status code -- a client-side "2xx = up"
            // guess has no way to know a monitor might expect e.g. 401
            // instead. null for maintenance/inactive log rows, which
            // aren't a real check either way.
            const isUp = c.is_up === true;
            const isMaintenance = c.status === 'maintenance';
            const isInactive = c.status === 'inactive';
            let cls;
            if (isUp) {
                cls = 'up';
            } else if (isMaintenance || isInactive) {
                cls = '';
            } else {
                cls = 'down';
            }
            let statusLabel = escapeHtml(c.status);
            // Both row kinds get a translated label, so a non-English UI doesn't
            // show a translated MAINT next to an untranslated "inactive" in the
            // same column.
            if (isMaintenance) statusLabel = T.maint;
            if (isInactive) statusLabel = T.inactive;
            let details = '';
            if (c.details && c.details.maintenance) {
                details = '<span style="color: var(--status-mixed);">' + T.maintenance + ' ' + escapeHtml(c.details.maintenance) + '</span>';
            } else if (c.details && c.details.passed) {
                const confirmColor = isUp ? 'var(--status-up)' : 'var(--status-down)';
                details = '<span style="color: ' + confirmColor + ';">' + T.verification_confirmed + '</span>';
            } else if (c.details && c.details.verifying) {
                details = '<span style="color: var(--status-mixed);">' + T.verifying + '</span>';
                if (c.error) {
                    details += ' - <span style="color: var(--status-down);">' + escapeHtml(c.error) + '</span>';
                }
            } else if (c.error) {
                details = '<span style="color: var(--status-down);">' + escapeHtml(c.error) + '</span>';
            }
            tr.innerHTML =
                '<td>' + escapeHtml(c.monitor) + '</td>' +
                '<td class="' + cls + '">' + statusLabel + '</td>' +
                '<td>' + escapeHtml(c.time) + '</td>' +
                '<td>' + (c.response_ms != null ? c.response_ms.toFixed(LATENCY_DECIMALS) + 'ms' : '—') + '</td>' +
                '<td>' + details + '</td>';
            tbody.appendChild(tr);
        });
    }
    renderPagination(data);
}

// -------------------------------------------------------------------------------------
function renderPagination(data) {
    const div = document.getElementById('pagination');
    if (!div) return;
    const tp = data.total_pages;
    div.innerHTML = '';
    const prev = document.createElement('button');
    prev.textContent = T.prev;
    prev.disabled = data.page <= 1;
    prev.onclick = () => loadChecks(data.page - 1);
    div.appendChild(prev);

    const info = document.createElement('span');
    info.className = 'page-info';
    info.textContent = T.page_of
        .replace('%(page)d', data.page)
        .replace('%(total)d', tp)
        .replace('%(count)d', data.total);
    div.appendChild(info);

    const input = document.createElement('input');
    input.type = 'text';
    input.inputMode = 'numeric';
    input.value = data.page;
    input.className = 'page-input';
    input.title = T.go_to_page;
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            const p = parseInt(input.value, 10);
            if (p >= 1 && p <= tp && p !== data.page) loadChecks(p);
        }
    });
    div.appendChild(input);

    const next = document.createElement('button');
    next.textContent = T.next;
    next.disabled = data.page >= tp;
    next.onclick = () => loadChecks(data.page + 1);
    div.appendChild(next);
}
