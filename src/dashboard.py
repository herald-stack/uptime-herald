# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
dashboard.py - Renders the dashboard, kiosk, and monitor pages.
"""

import asyncio
import gettext
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src import db
from src.config import VERSION, ConfigError, get_display_tz, load_config, load_theme
from src.db import get_aggregate_stats
from src.i18n import format_datetime, get_js_translations, get_translations
from src.maintenance import get_active_maintenance
from src.monitors.base import is_up
from src.state import hourly_bar_scaffold

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
# cache_size is deliberately NOT 0 (Jinja's "disable the cache" value).
# Compiling dashboard.html and its two big CSS includes takes ~330ms on a small
# ARM board, and get_template() is synchronous -- so an uncached env would block
# the event loop, and therefore every monitor check, for that long on every page
# request. The templates are read-only at runtime, so there is nothing to pick
# up by recompiling. Caching is safe alongside the per-request
# env.install_gettext_translations() below: the i18n extension resolves gettext
# at render time, not compile time, so a cached template still renders in
# whatever language was installed most recently.
env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    cache_size=64,
    extensions=["jinja2.ext.i18n"],
    autoescape=select_autoescape(("html",)),
)

# ORDERING DEPENDENCY: `env` is a single module-level Environment shared by
# every request, and env.install_gettext_translations() MUTATES it. Each route
# that renders a template must therefore install the configured language's
# translations immediately before its own render() call, which all of them do.
#
# This is safe because `language` comes only from config["global"]["language"],
# never from the request, so every request in a process installs the same
# catalog and two requests interleaving can't render each other's language.
# The template cache above is unaffected: the i18n extension resolves gettext
# through env.globals at render time, not compile time.
#
# It would break if `language` ever varied per request (per-user or per-URL
# locale). The shared Environment would then be a real race -- an await between
# install and render would let another request's install win -- and the fix is
# a per-language Environment (or Jinja's newstyle gettext with an explicit
# context), not a lock around the render.
#
# A template rendered before ANY route has installed translations raises
# UndefinedError on its first {% trans %}, which is why _render_config_error()
# below installs English explicitly.

# --------------------------------------------------------------------------------------
class _I18nEnvironment(Protocol):
    """The one method jinja2.ext.i18n adds to an Environment.

    The extension installs install_gettext_translations() onto the Environment
    instance at load time, so it isn't part of jinja2.Environment's own type
    and mypy rightly reports the attribute as missing. Declaring the method we
    depend on states that contract explicitly, instead of suppressing the error
    at each call site, and is the one place to update if the extension's
    signature changes.
    """

    def install_gettext_translations(
        self, translations: gettext.NullTranslations,
    ) -> None: ...


# --------------------------------------------------------------------------------------
def _install_translations(translations: gettext.NullTranslations) -> None:
    """Install a gettext catalog on the shared `env`.

    Every route that renders a template must call this immediately before
    its own render() -- see the ordering-dependency note above `env`.
    """
    cast(_I18nEnvironment, env).install_gettext_translations(translations)


DISPLAY_TIME_FORMAT = "%Y-%m-%d / %I:%M:%S %p"


# --------------------------------------------------------------------------------------
def _format_display_time(
    iso_str: str | None, lang: str, display_tz,
) -> str | None:
    """Convert a UTC ISO string to the given display timezone and format.

    Takes an already-resolved display_tz rather than loading config itself:
    calling load_config() per row would re-parse 5 YAML files from disk for
    every single check in a page, and would be unguarded against a config
    error the caller has already handled.
    """
    if iso_str is None:
        return None
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(lang, dt, format="medium", tzinfo=display_tz)

CONFIG_PATH = Path("config/config.yaml")
THEME_PATH = Path("config/theme.yaml")


# --------------------------------------------------------------------------------------
def _render_config_error(message: str) -> HTMLResponse:
    """Render the shared config-error page for dashboard()/kiosk().

    Used when load_config() raises ConfigError -- a predictable, operator-
    fixable problem (bad YAML, a monitor missing a required field, an unset
    ${ENV_VAR}, etc.) -- instead of falling back to an empty monitor list and
    rendering a normal-looking page with no sign anything is wrong. Other
    exceptions propagate as a real 500.

    Always installs English translations first: config_error.html uses
    {% trans %}, which needs a gettext function installed on the shared `env`,
    and the configured language isn't available because the config failed to
    load. In a fresh process with no earlier request to have installed
    translations, skipping this would crash the page with a raw 500 instead of
    showing the banner.
    """
    template = env.get_template("config_error.html")
    theme = load_theme(str(THEME_PATH))
    _install_translations(get_translations("en"))
    return HTMLResponse(
        template.render(theme=theme, error_message=message), status_code=503,
    )


# --------------------------------------------------------------------------------------
def _load_config_or_503() -> dict:
    """Load config for a JSON route, or fail loudly with a 503.

    A fabricated partial config (`{"monitors": []}`, say) would make a
    monitoring tool lie about its own health: /api/status would return HTTP 200
    with zero monitors and 0.00% uptime, and the dashboard's auto-refresh would
    paint an all-zero, healthy-looking install over a real one. Substituting
    default checks_per_page/view_all_checks values for the operator's own would
    be just as misleading.

    A 503 matches the HTML routes (via the config-error page), so a broken
    config produces one consistent, visible failure everywhere.
    """
    try:
        return load_config(str(CONFIG_PATH))
    except ConfigError as e:
        raise HTTPException(status_code=503, detail=f"Configuration error: {e}")


# The three fixed stat windows every page shows, alongside the operator's
# configured stats_period_days.
_FIXED_STAT_PERIODS = [("24h", 1), ("7d", 7), ("30d", 30)]


# --------------------------------------------------------------------------------------
def _stats_periods(stats_period: int) -> tuple[list[tuple[str, int]], str]:
    """Build get_aggregate_stats()'s period list, plus the key the
    templates should read for the configured stats period.

    De-duplicated by day count: appending the configured period as
    (f"{stats_period}d", stats_period) would collide with the fixed list for
    stats_period_days of 30 or 7 (the same window listed twice, so the
    identical query would run twice on every dashboard render and /api/status
    poll) and of 1 (("24h", 1) and ("1d", 1): two keys for one window).

    On a collision the pre-existing key is returned as stats_period_key, so
    callers and templates just read whichever key this hands back rather than
    assuming f"{stats_period}d".
    """
    periods = list(_FIXED_STAT_PERIODS)
    key_for_days = {days: key for key, days in _FIXED_STAT_PERIODS}
    if stats_period in key_for_days:
        return periods, key_for_days[stats_period]
    stats_period_key = f"{stats_period}d"
    periods.append((stats_period_key, stats_period))
    return periods, stats_period_key


# --------------------------------------------------------------------------------------
def _is_confirmed_down(status, expected_status: int | None = None) -> bool:
    """True only for a genuinely failed check. Unlike a bare `not is_up(status)`,
    this returns False for "unknown", MonitorState's sentinel (src/state.py) for
    "no check has ever run for this monitor yet" (main.py's alert-skip logic
    guards on the same sentinel): a monitor with no history isn't confirmed
    anything, up or down.

    Used everywhere in this file that needs an "is this really down" boolean.
    The sidebar health field needs a 3-way up/down/nodata result instead, so it
    doesn't use this helper, but follows the same "unknown" carve-out.
    """
    return status != "unknown" and not is_up(status, expected_status)


# How many hourly bars the monitor lists carry. A bar is 8 px wide with a 2 px
# gap (BAR_WIDTH/BAR_GAP in dashboard-common.js), and the page hides whichever
# bars do not fit, so sending more than can ever be visible only costs size: 360
# bars per monitor in two lists would make the dashboard page of 1000 monitors
# 112 MB and /api/status 40 MB.
#   * The desktop and kiosk sidebar is a fixed 360 px wide: 36 bars.
#   * The mobile list can be as wide as the screen, up to the 1365 px where the
#     desktop layout takes over (see _style_mobile.html): 136 bars at most, so
#     140. This is the number of bars the list data carries.
# The monitor detail page keeps its own 360 bars (one monitor, one request).
SIDEBAR_BARS = 36
LIST_BARS = 140


# --------------------------------------------------------------------------------------
def _bar_labels(config: dict, now: datetime) -> list[str]:
    """The hover label of each list bar, newest bar first, shared by every
    monitor (the labels depend only on the hour, never on the monitor)."""
    slot_zero = now.replace(minute=0, second=0, microsecond=0)
    return [
        f"{slot['hour_display']} – {slot['hour_display_end']}"
        for slot in hourly_bar_scaffold(slot_zero, LIST_BARS, get_display_tz(config))
    ]


# --------------------------------------------------------------------------------------
async def _build_monitor_list(config: dict, now: datetime) -> list[dict]:
    """Build the monitor list for sidebar rendering.

    Shared between the dashboard and kiosk routes to avoid
    duplicating the data-gathering logic.
    """
    display_tz = get_display_tz(config)
    sidebar_period = config.get("global", {}).get(
        "sidebar_uptime_period", "period"
    )
    stats_period = config.get("global", {}).get("stats_period_days", 90)
    uptime_decimals = config.get("global", {}).get("uptime_decimals", 2)
    period_days_map = {
        "24h": 1, "7d": 7, "30d": 30, "period": stats_period,
    }
    sidebar_days = period_days_map.get(sidebar_period, stats_period)
    monitor_configs = config.get("monitors", [])
    uptime_names = [
        m["name"] for m in monitor_configs
        if m.get("active", True) and sidebar_period != "none"
    ]
    # One query each for all the monitors (not four per monitor): at 1000
    # monitors that would be 4000 round trips on every page load and refresh.
    states, responses, bars_by_name, uptimes = await asyncio.gather(
        db.load_states([(m["name"], m["type"]) for m in monitor_configs]),
        db.get_last_response_ms_many([m["name"] for m in monitor_configs]),
        db.get_hourly_bar_statuses(
            [m["name"] for m in monitor_configs], hours=LIST_BARS, now=now
        ),
        db.get_uptime_pct_many(uptime_names, sidebar_days, uptime_decimals)
        if uptime_names else _no_uptimes(),
    )

    def _build_one(m: dict) -> dict:
        active = m.get("active", True)
        expected_status = m.get("expected_status")
        state = states[m["name"]]
        response_ms = responses[m["name"]]
        bars = bars_by_name[m["name"]]
        uptime_pct = uptimes.get(m["name"])
        last_check = state.last_check
        if last_check:
            utc_dt = datetime.fromisoformat(last_check).replace(
                tzinfo=timezone.utc
            )
            last_check = utc_dt.astimezone(display_tz).strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            )
        active_windows = get_active_maintenance(
            config, m["name"]
        ) if active else []
        return {
            "name": m["name"],
            "type": m["type"],
            "active": active,
            "in_maintenance": bool(active_windows),
            "maintenance_name": (
                active_windows[0] if active_windows else None
            ),
            "status": state.status,
            # Carried on the built dict (not just the raw config) so
            # _build_agg_stats and the pin_down_monitors sort below can
            # classify health correctly without needing config back in
            # scope -- both operate on this function's output, not on
            # config["monitors"] directly.
            "expected_status": expected_status,
            # 3-way rather than a bare is_up()-derived boolean -- see
            # _is_confirmed_down()'s docstring for why "unknown" (never checked)
            # needs its own case. "nodata" matches the --status-nodata
            # color/legend/bar convention used elsewhere in this UI for "no
            # data available".
            "health": (
                "nodata" if state.status == "unknown"
                else "up" if is_up(state.status, expected_status)
                else "down"
            ),
            "state_changed": state.state_changed,
            "uptime_pct": uptime_pct,
            "last_check": last_check,
            "response_ms": response_ms,
            # Each bar's status, newest first; the hover labels are the
            # same for every monitor and travel once (see _bar_labels).
            "bars": bars,
            "tags": m.get("tags", []),
        }

    monitors = [_build_one(m) for m in monitor_configs]

    if config.get("global", {}).get("pin_down_monitors", True):
        monitors.sort(
            key=lambda m: not (
                m["active"]
                and not m["in_maintenance"]
                and _is_confirmed_down(m["status"], m["expected_status"])
            )
        )
    return monitors


# --------------------------------------------------------------------------------------
async def _no_uptimes() -> dict:
    """Placeholder coroutine for an asyncio.gather() slot that isn't needed."""
    return {}


# --------------------------------------------------------------------------------------
def _build_agg_stats(
    config: dict, monitors: list[dict]
) -> tuple[list[str], list[tuple[str, int]], dict]:
    """Compute aggregate stats from the monitor list."""
    active_names = [
        m["name"]
        for m in config.get("monitors", [])
        if m.get("active", True)
    ]
    inactive_count = len(monitors) - len(active_names)
    sites_up = sum(
        1 for m in monitors
        if m["active"] and not m["in_maintenance"]
        and is_up(m["status"], m["expected_status"])
    )
    # _is_confirmed_down(), not a bare `not is_up(...)`, so a monitor that has
    # never been checked ("unknown" status, the brief window between being
    # added and its first check) isn't counted as DOWN -- see
    # _is_confirmed_down()'s docstring. It is also excluded from sites_up
    # (is_up("unknown") is False) and is neither INACTIVE nor MAINTENANCE, so
    # for that window it is uncounted by all 4 stat cards instead of
    # miscounted into one.
    sites_down = sum(
        1 for m in monitors
        if m["active"] and not m["in_maintenance"]
        and _is_confirmed_down(m["status"], m["expected_status"])
    )
    sites_maintenance = sum(1 for m in monitors if m["in_maintenance"])
    stats_period = config.get("global", {}).get("stats_period_days", 90)
    periods, stats_period_key = _stats_periods(stats_period)
    return active_names, periods, {
        "sites_up": sites_up,
        "sites_down": sites_down,
        "sites_inactive": inactive_count,
        "sites_maintenance": sites_maintenance,
        "sites_total": len(monitors),
        "stats_period": stats_period,
        # Also included as its own key (not just the top-level template
        # kwarg the page render passes separately) so /api/status's polled
        # agg_stats has the same shape the dashboard's own JS expects
        # (dashboard.html reads `stats.stats_period_key` from poll data).
        "stats_period_key": stats_period_key,
    }


# --------------------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def dashboard():
    """Main dashboard — status grid of all monitors."""
    try:
        config = load_config(str(CONFIG_PATH))
    except ConfigError as e:
        return _render_config_error(str(e))

    now = datetime.now(timezone.utc)
    monitors = await _build_monitor_list(config, now)
    uptime_decimals = config.get("global", {}).get("uptime_decimals", 2)
    latency_decimals = config.get("global", {}).get("latency_decimals", 1)

    active_names, periods, counts = _build_agg_stats(config, monitors)
    agg_stats = await get_aggregate_stats(
        active_names, periods, uptime_decimals, latency_decimals
    )
    agg_stats.update(counts)

    # Collect unique monitor types for sidebar filter
    monitor_types = sorted(
        {m["type"] for m in config.get("monitors", [])}
    )
    show_type_filter = len(monitor_types) > 1

    # Collect unique tags for sidebar filter
    all_tags = sorted(
        {
            t for m in config.get("monitors", [])
            for t in m.get("tags", [])
        }
    )
    show_tag_filter = len(all_tags) > 0

    template = env.get_template("dashboard.html")
    theme = load_theme(str(THEME_PATH))
    lang = config.get("global", {}).get("language", "en")
    translations = get_translations(lang)
    _install_translations(translations)
    html = template.render(
        monitors=monitors,
        bar_labels=_bar_labels(config, now),
        sidebar_bars=SIDEBAR_BARS,
        agg_stats=agg_stats,
        theme=theme,
        version=VERSION,
        stats_period_days=agg_stats["stats_period"],
        stats_period_key=agg_stats["stats_period_key"],
        ui_refresh_minutes=config.get("global", {}).get(
            "ui_refresh_minutes", 2
        ),
        view_all_checks=config.get("global", {}).get(
            "view_all_checks", False
        ),
        js_translations=get_js_translations(lang),
        monitor_types=monitor_types,
        show_type_filter=show_type_filter,
        monitor_tags=all_tags,
        show_tag_filter=show_tag_filter,
        checks_per_page=config.get("global", {}).get(
            "checks_per_page", 30
        ),
        uptime_decimals=uptime_decimals,
        latency_decimals=latency_decimals,
    )
    return HTMLResponse(html)


# --------------------------------------------------------------------------------------
@router.get("/kiosk", response_class=HTMLResponse)
async def kiosk():
    """Kiosk mode — auto-rotating display for always-on screens.

    Same layout as the dashboard but without sidebar filter controls
    or the sidebar footer. Auto-rotates between monitors on a timer.
    Down monitors get priority: a single down monitor stays on screen
    until it recovers; multiple down monitors rotate between themselves.
    """
    try:
        config = load_config(str(CONFIG_PATH))
    except ConfigError as e:
        return _render_config_error(str(e))

    now = datetime.now(timezone.utc)
    monitors = await _build_monitor_list(config, now)
    uptime_decimals = config.get("global", {}).get("uptime_decimals", 2)
    latency_decimals = config.get("global", {}).get("latency_decimals", 1)

    active_names, periods, counts = _build_agg_stats(config, monitors)
    agg_stats = await get_aggregate_stats(
        active_names, periods, uptime_decimals, latency_decimals
    )
    agg_stats.update(counts)

    template = env.get_template("kiosk.html")
    theme = load_theme(str(THEME_PATH))
    lang = config.get("global", {}).get("language", "en")
    translations = get_translations(lang)
    _install_translations(translations)
    html = template.render(
        monitors=monitors,
        bar_labels=_bar_labels(config, now),
        sidebar_bars=SIDEBAR_BARS,
        agg_stats=agg_stats,
        theme=theme,
        version=VERSION,
        stats_period_days=agg_stats["stats_period"],
        stats_period_key=agg_stats["stats_period_key"],
        kiosk_refresh_minutes=config.get("global", {}).get(
            "kiosk_refresh_minutes", 1
        ),
        ui_refresh_minutes=config.get("global", {}).get(
            "ui_refresh_minutes", 2
        ),
        view_all_checks=config.get("global", {}).get(
            "view_all_checks", False
        ),
        js_translations=get_js_translations(lang),
        checks_per_page=config.get("global", {}).get(
            "checks_per_page", 30
        ),
        uptime_decimals=uptime_decimals,
        latency_decimals=latency_decimals,
    )
    return HTMLResponse(html)


# --------------------------------------------------------------------------------------
def _find_monitor_config(config: dict, name: str):
    """Find a monitor's config entry by name."""
    for m in config.get("monitors", []):
        if m["name"] == name:
            return m
    return None


# --------------------------------------------------------------------------------------
def _compute_display_status(
    active: bool, in_maintenance: bool, status, expected_status: int | None = None,
) -> str:
    """Determine the display status for the monitor detail view.

    Carries the same "unknown" carve-out as _is_confirmed_down() and the
    sidebar's health field: without the nodata branch, a monitor that has
    never been checked would fall through to `return "down"` (because
    is_up("unknown") is False) and its detail page would announce a red
    (DOWN) for something nothing has ever confirmed, while the sidebar row
    for that same monitor shows grey NO DATA.
    """
    if not active:
        return "inactive"
    if in_maintenance:
        return "maintenance"
    if status == "unknown":
        return "nodata"
    if is_up(status, expected_status):
        return "up"
    return "down"


# --------------------------------------------------------------------------------------
def _compute_monitor_target(monitor_config: dict):
    """Build a human-readable target string for the monitor, plus whether it's
    safe to render as a clickable link.

    `target_is_safe_link` gates whether monitor.html renders this as a
    clickable <a href>: an http monitor's `url` is operator-supplied config,
    and a scheme like `javascript:` would otherwise become a click-to-execute
    XSS vector.
    """
    monitor_type = monitor_config["type"]
    if monitor_type == "http":
        target = monitor_config.get("url")
        target_is_safe_link = isinstance(target, str) and target.lower().startswith(
            ("http://", "https://")
        )
        return target, target_is_safe_link
    if monitor_type == "tcp":
        host = monitor_config.get("host", "")
        port = monitor_config.get("port", "")
        target = f"{host}:{port}" if host else None
        return target, False
    if monitor_type == "ping":
        return monitor_config.get("host"), False
    return None, False


# --------------------------------------------------------------------------------------
@router.get("/monitor/{name}", response_class=HTMLResponse)
async def monitor_detail(name: str):
    """Per-monitor detail page with check history."""
    # Same config-error page the dashboard/kiosk routes show. Fabricating
    # `{"monitors": []}` instead would make a broken config look like "that
    # monitor doesn't exist" (a 404) rather than "the config is broken".
    try:
        config = load_config(str(CONFIG_PATH))
    except ConfigError as e:
        return _render_config_error(str(e))

    monitor_config = _find_monitor_config(config, name)

    if monitor_config is None:
        return HTMLResponse("Monitor not found", status_code=404)

    display_tz = get_display_tz(config)
    stats_period = config.get("global", {}).get("stats_period_days", 90)
    periods, stats_period_key = _stats_periods(stats_period)
    uptime_decimals = config.get("global", {}).get("uptime_decimals", 2)
    latency_decimals = config.get("global", {}).get("latency_decimals", 1)

    # These four queries are independent of each other, so they run
    # concurrently instead of one after the other. Three of them read the
    # hourly summary (check_hourly); get_monitor_stats also reads this one
    # monitor's raw checks of the last 30 days for its timeout and outage
    # counts.
    state, bars, stats, agg = await asyncio.gather(
        db.load_state(name, monitor_config["type"]),
        db.get_hourly_bars(name, hours=360, display_tz=display_tz),
        db.get_monitor_stats(
            name, days=30,
            uptime_decimals=uptime_decimals, latency_decimals=latency_decimals,
        ),
        get_aggregate_stats([name], periods, uptime_decimals, latency_decimals),
    )

    # Determine display status for the detail view
    active = monitor_config.get("active", True)
    in_maintenance = bool(get_active_maintenance(config, name))
    display_status = _compute_display_status(
        active, in_maintenance, state.status, monitor_config.get("expected_status")
    )

    # Determine interval and whether it's the default
    global_interval = config.get("global", {}).get("check_interval_minutes", 5)
    monitor_interval = monitor_config.get("check_interval_minutes", global_interval)
    interval_is_default = monitor_interval == global_interval

    target, target_is_safe_link = _compute_monitor_target(monitor_config)

    template = env.get_template("monitor.html")
    theme = load_theme(str(THEME_PATH))
    lang = config.get("global", {}).get("language", "en")
    translations = get_translations(lang)
    _install_translations(translations)
    html = template.render(
        monitor={
            "name": name,
            "type": monitor_config["type"],
            "target": target,
            "target_is_safe_link": target_is_safe_link,
            "active": monitor_config.get("active", True),
            "status": state.status,
            "total_up": stats["total_up"],
            "total_down": stats["total_down"],
            "total_timeout": stats["total_timeout"],
            "outages": stats["outages"],
            "tags": monitor_config.get("tags", []),
            "expected_status": monitor_config.get("expected_status"),
            "display_status": display_status,
            "interval_minutes": monitor_interval,
            "interval_is_default": interval_is_default,
        },
        agg_stats={
            "uptime_pct": agg["uptime_pct"],
            "avg_latency": agg["avg_latency"],
        },
        bars=bars,
        theme=theme,
        stats_period_days=stats_period,
        stats_period_key=stats_period_key,
        latency_graph_days=config["global"]["latency_graph_days"],
        checks_per_page=config.get("global", {}).get("checks_per_page", 30),
        view_all_checks=config.get("global", {}).get("view_all_checks", False),
        display_timezone=str(get_display_tz(config)),
        js_translations=get_js_translations(lang),
        version=VERSION,
        uptime_decimals=uptime_decimals,
        latency_decimals=latency_decimals,
    )
    return HTMLResponse(html)


# --------------------------------------------------------------------------------------
@router.get("/api/monitor/{name}/checks")
async def get_checks(
    name: str,
    page: int = Query(1, ge=1),
    # 0 means "use global.checks_per_page"; the upper bound stops a
    # ?per_page=100000000 from turning into a LIMIT that buffers the
    # entire checks table into memory for one request.
    per_page: int = Query(0, ge=0, le=1000),
):
    """Return paginated checks for a monitor as JSON."""
    config = _load_config_or_503()
    lang = config.get("global", {}).get("language", "en")
    display_tz = get_display_tz(config)
    if per_page <= 0:
        per_page = config.get("global", {}).get("checks_per_page", 30)
    view_all = config.get("global", {}).get("view_all_checks", False)
    result = await db.get_checks_paginated(
        name, page, per_page, state_changes_only=not view_all
    )
    for c in result["checks"]:
        c["time"] = _format_display_time(c["time"], lang, display_tz)
    return result


# --------------------------------------------------------------------------------------
@router.get("/api/monitor/{name}/response-times")
async def get_response_times(
    name: str,
    # Bounded, not a bare `int = 168`: `hours` becomes a timedelta the
    # query subtracts from now(), and an absurd value (?hours=999999999)
    # pushes the cutoff below datetime.MINYEAR and raises OverflowError
    # -- an unhandled 500 from a public endpoint. 87600h is 10 years,
    # comfortably past any real retention setting.
    hours: int = Query(168, ge=1, le=87600),
):
    """Return response time data for the line graph."""
    data = await db.get_response_time_series(name, hours)
    return {"data": data}


# --------------------------------------------------------------------------------------
@router.get("/api/checks")
async def get_all_checks(
    page: int = Query(1, ge=1),
    # See get_checks() for why this is bounded.
    per_page: int = Query(0, ge=0, le=1000),
):
    """Return paginated checks across all monitors as JSON."""
    config = _load_config_or_503()
    lang = config.get("global", {}).get("language", "en")
    display_tz = get_display_tz(config)
    if per_page <= 0:
        per_page = config.get("global", {}).get("checks_per_page", 30)
    view_all = config.get("global", {}).get("view_all_checks", False)
    result = await db.get_all_checks_paginated(
        page, per_page, state_changes_only=not view_all
    )
    for c in result["checks"]:
        c["time"] = _format_display_time(c["time"], lang, display_tz)
    return result


# --------------------------------------------------------------------------------------
@router.get("/api/status")
async def get_status():
    """Return lightweight status for all monitors.

    Used by dashboard auto-refresh to update row backgrounds, uptime
    bars, and aggregate stats in place without a full page reload.

    Reuses the same _build_monitor_list()/_build_agg_stats() helpers the
    dashboard/kiosk routes use, so the polled JSON always matches the
    initial page render's field names and values exactly.
    """
    config = _load_config_or_503()

    now = datetime.now(timezone.utc)
    monitors = await _build_monitor_list(config, now)
    active_names, periods, counts = _build_agg_stats(config, monitors)
    agg_stats = await get_aggregate_stats(
        active_names, periods,
        config.get("global", {}).get("uptime_decimals", 2),
        config.get("global", {}).get("latency_decimals", 1),
    )
    agg_stats.update(counts)

    return {
        "monitors": monitors,
        "bar_labels": _bar_labels(config, now),
        "agg_stats": agg_stats,
    }
