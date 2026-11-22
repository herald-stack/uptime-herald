# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
config.py - Loads and validates the configuration files.
"""

import configparser
import copy
import functools
import ipaddress
import logging
import math
import os
import re
import string
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from src.config_keys import find_unknown_settings
from src.hostnames import to_dns_name
from src.i18n import DOMAIN, LOCALES_DIR

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
class ConfigError(Exception):
    """Configuration error."""


# How long a check waits for a response when neither the monitor nor
# config.yaml sets a timeout.
DEFAULT_MONITOR_TIMEOUT = 10

# The longest timeout allowed, in seconds, for config.yaml and for a monitor.
# Nothing sensible waits longer than five minutes for a monitor to answer.
MAX_TIMEOUT_SECONDS = 300

# A monitor's name is the primary key of the monitors table, a
# VARCHAR(255) in both the MariaDB and PostgreSQL schemas.
MAX_MONITOR_NAME_LENGTH = 255
MAX_TAG_LENGTH = 255

# The text that means "every monitor" in the `monitors` setting and "every
# monitor that has a tag" in the `tags` setting of an alert or a maintenance
# window. It is not allowed as a tag or a monitor name, so `tags: all` and
# `tags: [all]` can never be mistaken for each other.
RESERVED_ALL = "all"

DEFAULTS = {
    "global": {
        "check_interval_minutes": 5,
        "timeout": DEFAULT_MONITOR_TIMEOUT,
        "stagger_seconds": 30,
        "retention_days": 90,
        "stats_period_days": 90,
        "sidebar_uptime_period": "period",
        "alert_cooldown_minutes": 5,
        "ui_refresh_minutes": 2,
        "latency_graph_days": 1,
        "checks_per_page": 30,
        "kiosk_refresh_minutes": 1,
        "view_all_checks": False,
        "alert_retries": 2,
        "retry_interval_minutes": 1,
        "retry_on_up": True,
        "pin_down_monitors": True,
        "cleanup_removed_monitors": True,
        "cleanup_grace_minutes": 20,
        "timezone": "UTC",
        "language": "en",
        "uptime_decimals": 2,
        "latency_decimals": 1,
        # No "dashboard": {"host", "port"} default here on purpose. uvicorn binds
        # before the app loads this config, so those keys could not take effect;
        # Docker's port mapping sets the port.
    },
    "alerts": {
        "email": {"recipients": []},
        "push_alerts": [],
    },
    "monitors": [],
    "tags": [],
    "maintenance": [],
}

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")

# --- Maintenance-window vocabulary ----------------------------------------------------
#
# Defined here rather than in maintenance.py so config validation and
# window evaluation share one definition of what a valid day/time looks
# like. maintenance.py already imports from this module (get_display_tz),
# so this direction carries no circular-import risk; the reverse would.
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# "1st Tuesday", "2nd Wednesday", "last Friday", etc. The ordinal set is
# deliberately 1st-4th plus "last" -- see _nth_weekday_date() in
# maintenance.py, which relies on every month having at least four of
# every weekday.
MONTHLY_PATTERN_RE = re.compile(r"^(1st|2nd|3rd|4th|last)\s+(\w+)$", re.IGNORECASE)
# "following Wednesday" in end_day: the first Wednesday after the start day.
FOLLOWING_PATTERN_RE = re.compile(r"^following\s+(\w+)$", re.IGNORECASE)

_WEEKDAY_NAMES = frozenset(WEEKDAYS)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------------------
def _substitute_env_vars(value):
    """Recursively substitute ${VAR} patterns in strings."""
    if isinstance(value, str):
        def replacer(match):
            var_name = match.group(1)
            val = os.environ.get(var_name)
            if val is None:
                raise ConfigError(
                    f"Environment variable '{var_name}' referenced in config "
                    f"but not set."
                )
            return val
        return _ENV_VAR_RE.sub(replacer, value)
    elif isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]
    return value


# Global settings that must be numeric (minutes/seconds/day counts).
_NUMERIC_GLOBAL_FIELDS = (
    "check_interval_minutes", "stagger_seconds", "retention_days",
    "stats_period_days", "alert_cooldown_minutes", "ui_refresh_minutes",
    "latency_graph_days", "checks_per_page", "kiosk_refresh_minutes",
    "alert_retries", "retry_interval_minutes", "cleanup_grace_minutes",
    "timeout",
)

# Per-monitor override fields that must be numeric when present.
_NUMERIC_MONITOR_FIELDS = (
    "check_interval_minutes", "retention_days", "alert_retries",
    "retry_interval_minutes", "alert_cooldown_minutes", "timeout",
    "expected_status", "count",
)

# Display-precision fields: how many decimal places uptime %/latency ms
# are rounded to wherever they're shown (sidebar, stat cards, check log).
# Capped at 6 -- comfortably past any real "how many nines" use case
# (99.999999% is 6 decimals) without letting a typo'd huge value produce
# a nonsensical-looking display.
_DECIMAL_FIELDS = ("uptime_decimals", "latency_decimals")
_MAX_DECIMALS = 6

# Settings that count whole things: a retry or a row cannot be 1.5. A fraction
# would break range(1, 2.5) for the retries, and the dashboard rejects a
# ?per_page=1.5 request (422) for the check log.
_WHOLE_GLOBAL_FIELDS = ("alert_retries", "checks_per_page")
_WHOLE_MONITOR_FIELDS = ("alert_retries",)

# Maximums for these settings. Above them a value is a typo, or means "never":
# a check every 1000000000000 minutes is no check at all, and alert_retries of
# a million is a retry sequence that never ends. They are generous on purpose:
# one week for the intervals and the grace period, one day for retry spacing,
# one hour for stagger, 100 retries.
_MAX_MORE_FIELDS = {
    "check_interval_minutes": 10080,
    "alert_cooldown_minutes": 10080,
    "retry_interval_minutes": 1440,
    "stagger_seconds": 3600,
    "alert_retries": 100,
    "cleanup_grace_minutes": 10080,
}

# Global fields where a value below 1 is not a valid setting but a broken one,
# so it is rejected at load rather than reaching the code that divides or
# loops on it:
#   checks_per_page        -- 0 is a ZeroDivisionError in the pagination
#                             helpers on every check-log request.
#   check_interval_minutes -- 0 makes every monitor re-check on every ~10s
#                             loop pass instead of on its interval.
#   retry_interval_minutes -- 0 spacing fires all retries at once, which
#                             defeats the retry sequence.
#   latency_graph_days     -- becomes the ?hours= window the graph fetches.
#   kiosk_refresh_minutes  -- 0 stops the kiosk page rotating between monitors.
#   timeout                -- 0 or less makes every TCP and HTTP check report
#                             down at once, however healthy the target is.
#   alert_cooldown_minutes -- checks are at least a minute apart, so 0 would
#                             only mean "alert on every check", which 1
#                             already does.
# Fields where 0 is a real setting are in _MIN_ZERO_GLOBAL_FIELDS below.
_MIN_ONE_GLOBAL_FIELDS = (
    "check_interval_minutes", "checks_per_page",
    "retry_interval_minutes", "latency_graph_days",
    "kiosk_refresh_minutes", "alert_cooldown_minutes", "timeout",
)

# Global fields where 0 is a real setting but a negative value is a typo:
#   ui_refresh_minutes     -- 0 turns dashboard auto-refresh off.
#   stagger_seconds        -- 0 turns staggering off.
#   alert_retries          -- 0 alerts on the first observed change.
#   cleanup_grace_minutes  -- 0 deletes a removed monitor's history at the
#                             next config re-read.
# alert_retries has the same floor as a per-monitor override
# (_MIN_ZERO_MONITOR_FIELDS).
_MIN_ZERO_GLOBAL_FIELDS = (
    "ui_refresh_minutes", "stagger_seconds",
    "alert_retries", "cleanup_grace_minutes",
)
_MIN_ZERO_MONITOR_FIELDS = ("alert_retries",)

# A floor above 1, for a setting whose meaning needs it:
#   stats_period_days -- the dashboard always shows 24h, 7d and 30d stat
#                        cards; this is the one extra "N Days" card beside
#                        them, so a value of 30 or less would repeat a
#                        card that is already there.
_MIN_GLOBAL_FIELDS = {
    "stats_period_days": 31,
}

# Ceilings for the two global settings the templates pass straight to a bounded
# JSON endpoint as a query parameter. Above them every dashboard request is
# rejected with a 422 and the JS shows only a generic "failed to load", with
# nothing pointing at the config value. Rejecting at load gives a startup error
# naming the field.
#   checks_per_page    -> ?per_page=, bounded le=1000 in dashboard.py
#   latency_graph_days -> ?hours= as days*24, bounded le=87600 (10 years)
_MAX_GLOBAL_FIELDS = {
    "checks_per_page": 1000,
    "latency_graph_days": 3650,
}

# ui_refresh_minutes and kiosk_refresh_minutes are capped at one day. The
# templates turn them into millisecond timers, and a browser timer longer
# than about 24.8 days (35,791 minutes) overflows and fires continuously,
# so a huge value would make the page reload (or the kiosk rotate) non-stop.
_MAX_REFRESH_MINUTES = 1440

# retention_days (global and per-monitor) and stats_period_days are capped at
# ten years. The prune cutoff is `now - timedelta(days=retention_days)`, which
# raises OverflowError once the date would pass year 1, so an unbounded value
# would fail on every prune. There is no "keep forever" value; 0 is rejected
# like any value below stats_period_days. stats_period_days gets the same cap
# because retention_days has to be at least as long, so a larger stats period
# could never be satisfied and would surface as a retention_days error.
_MAX_HISTORY_DAYS = 3650


# --------------------------------------------------------------------------------------
def _coerce_numeric(value, field_label):
    """Coerce a config value to int/float, raising ConfigError if it can't be."""
    if isinstance(value, bool):
        # bool is a subclass of int in Python — reject it explicitly so a
        # stray `true`/`false` doesn't silently become 1/0.
        raise ConfigError(f"'{field_label}' must be a number, got boolean")
    if isinstance(value, (int, float)):
        return _require_finite(value, field_label)
    if isinstance(value, str):
        try:
            number = float(value) if "." in value else int(value)
        except ValueError:
            raise ConfigError(
                f"'{field_label}' must be a number, got {value!r} "
                f"(this often happens with ${{ENV_VAR}} substitution — "
                f"check the referenced environment variable's value)"
            )
        return _require_finite(number, field_label)
    raise ConfigError(f"'{field_label}' must be a number, got {type(value).__name__}")


# --------------------------------------------------------------------------------------
def _require_finite(number, field_label):
    """YAML reads .nan and .inf as numbers. NaN is neither above nor below
    any limit, so it would pass every minimum and maximum check, and infinity
    passes any setting without a maximum. Neither is a usable setting."""
    if isinstance(number, float) and not math.isfinite(number):
        raise ConfigError(
            f"'{field_label}' must be a finite number, got {number!r}"
        )
    return number


# --------------------------------------------------------------------------------------
def _require_whole(number, field_label):
    """A setting that counts things (retries, rows) has to be a whole number.
    2.0 is accepted as 2; 1.5 is not, because it would break the loop or the
    page request that uses it."""
    if isinstance(number, float):
        if not number.is_integer():
            raise ConfigError(
                f"'{field_label}' must be a whole number, got {number}"
            )
        return int(number)
    return number


# Settings that are on/off switches. They are checked at load: a quoted "false"
# is text, which counts as true when tested for truthiness, so
# cleanup_removed_monitors: "false" would otherwise still delete history.
_BOOLEAN_GLOBAL_FIELDS = (
    "pin_down_monitors", "cleanup_removed_monitors", "retry_on_up",
    "view_all_checks",
)
_BOOLEAN_MONITOR_FIELDS = ("active", "retry_on_up", "verify_ssl")


# --------------------------------------------------------------------------------------
def _coerce_boolean(value, field_label):
    """Return a bool, or raise ConfigError if the value isn't true or false.

    The text "true" and "false" (any capitalization) are accepted because
    ${ENV_VAR} substitution always produces text, the same reason
    _coerce_numeric accepts numeric text.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ConfigError(f"'{field_label}' must be true or false, got {value!r}")


# --------------------------------------------------------------------------------------
def _validate_boolean_fields(config: dict) -> None:
    """Coerce and check the on/off settings in place, after env var
    substitution."""
    global_cfg = config.get("global", {})
    for field in _BOOLEAN_GLOBAL_FIELDS:
        if field in global_cfg:
            global_cfg[field] = _coerce_boolean(
                global_cfg[field], f"global.{field}"
            )
    for monitor in config.get("monitors", []):
        mname = monitor.get("name", "?")
        for field in _BOOLEAN_MONITOR_FIELDS:
            if field in monitor:
                monitor[field] = _coerce_boolean(
                    monitor[field], f"monitors[{mname}].{field}"
                )


# The values sidebar_uptime_period understands (see _build_monitor_list in
# dashboard.py). Anything else is rejected rather than falling back to
# stats_period_days.
_SIDEBAR_UPTIME_PERIODS = ("24h", "7d", "30d", "period", "none")


# --------------------------------------------------------------------------------------
def _validate_sidebar_uptime_period(config: dict) -> None:
    """Require one of the documented sidebar_uptime_period values."""
    value = config.get("global", {}).get("sidebar_uptime_period")
    if value not in _SIDEBAR_UPTIME_PERIODS:
        options = ", ".join(f'"{v}"' for v in _SIDEBAR_UPTIME_PERIODS)
        raise ConfigError(
            f"'global.sidebar_uptime_period' must be one of {options}, "
            f"got {value!r}"
        )


# --------------------------------------------------------------------------------------
def _supported_languages() -> list[str]:
    """The languages that ship with the software: one folder per language
    under locales/ holding a compiled catalog. A folder with only the .po
    would load and then show English, the silent fallback this check exists
    to prevent. Adding a compiled language there makes it valid here."""
    return sorted(
        p.name for p in LOCALES_DIR.iterdir()
        if (p / "LC_MESSAGES" / f"{DOMAIN}.mo").is_file()
    )


# --------------------------------------------------------------------------------------
def _validate_language(config: dict) -> None:
    """Require a language that has a locales/ folder. An unknown one would show
    English with no message, so a user who set "fr" would never learn why
    nothing changed."""
    value = config.get("global", {}).get("language")
    supported = _supported_languages()
    if value not in supported:
        options = ", ".join(f'"{v}"' for v in supported)
        raise ConfigError(
            f"'global.language' must be one of {options}, got {value!r}"
        )


# contact_email ends up in the User-Agent header of every HTTP check, so a
# value the header can't carry (a non-ASCII letter, a newline) would make every
# HTTP monitor report "failed". The rule is a deliberate subset of RFC 5322
# and RFC 5321: the unquoted ASCII local part (letters, digits and
# ! # $ % & ' * + - / = ? ^ _ { } ~, dot-separated, no leading, trailing
# or doubled dot), and a domain of dot-separated labels (letters, digits and
# hyphens, none starting or ending with a hyphen) with at least one dot.
# Quoted local parts, domain literals, single-label domains and non-ASCII
# addresses are not accepted. The backtick and the pipe are legal in the
# RFC but left out on purpose: they mean something to a shell, and the
# value is not worth the risk. Limits: 64 for the local part, 63 per
# label, 255 for the domain, 320 overall.
_EMAIL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_{}~-]+"
_EMAIL_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL_RE = re.compile(
    rf"(?P<local>{_EMAIL_ATOM}(?:\.{_EMAIL_ATOM})*)"
    rf"@(?P<domain>{_EMAIL_LABEL}(?:\.{_EMAIL_LABEL})+)"
)


# --------------------------------------------------------------------------------------
def _is_email_address(value) -> bool:
    if not isinstance(value, str) or len(value) > 320:
        return False
    match = _EMAIL_RE.fullmatch(value)
    return bool(
        match and len(match["local"]) <= 64 and len(match["domain"]) <= 255
    )


# --------------------------------------------------------------------------------------
def _validate_contact_email(config: dict) -> None:
    """Require contact_email, when it is listed, to be an email address. Leaving
    it out is fine; listing it with nothing after it is an error, like any
    other setting."""
    settings = config.get("global", {})
    if "contact_email" not in settings:
        return
    value = settings["contact_email"]
    if value is None:
        raise ConfigError(
            "'global.contact_email' has nothing after it — write an email "
            "address such as you@example.com, or delete the line"
        )
    if not _is_email_address(value):
        raise ConfigError(
            f"'global.contact_email' must be an email address such as "
            f"you@example.com, got {value!r}"
        )


# The status codes expected_status accepts. A number that is not a real
# response code (0, 299, 99999, 401.5) would leave the monitor unable to
# ever be "up", so only actual codes are allowed. The list is documented at
# https://uptimeherald.org/docs/response-codes/. To accept another code, add it
# here and to that page.
#
# Registered codes: the IANA HTTP Status Code Registry
# (https://www.iana.org/assignments/http-status-codes).
# Left out on purpose: 306 (unused), 510 (obsoleted), 104 (only a temporary
# registration). Included on purpose: 418 (listed as unused in the registry).
_REGISTERED_STATUS_CODES = frozenset(
    {*range(100, 104), *range(200, 209), 226, *range(300, 306), 307, 308}
    | {*range(400, 419), *range(421, 427), 428, 429, 431, 451}
    | {*range(500, 509), 511}
)

# Well-known codes that servers send without an official registration, from
# the "Nonstandard codes" section of Wikipedia's "List of HTTP status codes"
# (https://en.wikipedia.org/wiki/List_of_HTTP_status_codes). Left out on
# purpose: the AWS load balancer's "000", which is not a three-digit status
# code. Included on purpose: 218 ("This is fine", attributed to Apache, not from
# any specification).
_UNOFFICIAL_STATUS_CODES = frozenset({
    444, 494, 495, 496, 497, 499,                     # nginx
    *range(520, 528), 530,                            # Cloudflare
    440, 449, 450, 451,                               # Microsoft IIS
    460, 463, 464, 561,                               # AWS load balancer
    509, 508, 218,                                    # Apache and cPanel
    419,                                              # Laravel
    420,                                              # Spring, Twitter
    430, 540, 783,                                    # Shopify
    498,                                              # ArcGIS Server
    529,                                              # Qualys
    999,                                              # LinkedIn
    598, 599,                                         # some HTTP proxies
})

_VALID_EXPECTED_STATUS_CODES = _REGISTERED_STATUS_CODES | _UNOFFICIAL_STATUS_CODES


# --------------------------------------------------------------------------------------
def _check_global_floors(global_cfg: dict) -> None:
    """Enforce the minimums on global numeric settings."""
    for field in _MIN_ONE_GLOBAL_FIELDS:
        if field in global_cfg and global_cfg[field] < 1:
            raise ConfigError(
                f"'global.{field}' must be at least 1, got {global_cfg[field]}"
            )

    for field in _MIN_ZERO_GLOBAL_FIELDS:
        if field in global_cfg and global_cfg[field] < 0:
            raise ConfigError(
                f"'global.{field}' must be 0 or more, got {global_cfg[field]}"
            )

    for field, floor in _MIN_GLOBAL_FIELDS.items():
        if field in global_cfg and global_cfg[field] < floor:
            raise ConfigError(
                f"'global.{field}' must be at least {floor}, got "
                f"{global_cfg[field]}"
            )


# --------------------------------------------------------------------------------------
def _check_global_ceilings(global_cfg: dict) -> None:
    """Enforce the maximums on global numeric settings."""
    if global_cfg.get("timeout", 0) > MAX_TIMEOUT_SECONDS:
        raise ConfigError(
            f"'global.timeout' must be at most {MAX_TIMEOUT_SECONDS}, "
            f"got {global_cfg['timeout']}"
        )

    for field in ("ui_refresh_minutes", "kiosk_refresh_minutes"):
        if global_cfg.get(field, 0) > _MAX_REFRESH_MINUTES:
            raise ConfigError(
                f"'global.{field}' must be at most {_MAX_REFRESH_MINUTES}, "
                f"got {global_cfg[field]}"
            )

    for field in ("retention_days", "stats_period_days"):
        if global_cfg.get(field, 0) > _MAX_HISTORY_DAYS:
            raise ConfigError(
                f"'global.{field}' must be at most {_MAX_HISTORY_DAYS}, "
                f"got {global_cfg[field]}"
            )

    _check_more_global_ceilings(global_cfg)

    for field, ceiling in _MAX_GLOBAL_FIELDS.items():
        if field in global_cfg and global_cfg[field] > ceiling:
            raise ConfigError(
                f"'global.{field}' must be at most {ceiling}, got "
                f"{global_cfg[field]} — above this the dashboard's own API "
                f"requests are rejected and the page shows only a generic "
                f"load failure"
            )


# --------------------------------------------------------------------------------------
def _check_more_global_ceilings(global_cfg: dict) -> None:
    for field, ceiling in _MAX_MORE_FIELDS.items():
        if field in global_cfg and global_cfg[field] > ceiling:
            raise ConfigError(
                f"'global.{field}' must be at most {ceiling}, got "
                f"{global_cfg[field]}"
            )


# --------------------------------------------------------------------------------------
def _validate_global_numeric_bounds(global_cfg: dict) -> None:
    """Enforce the floors and ceilings on global numeric settings."""
    _check_global_floors(global_cfg)
    _check_global_ceilings(global_cfg)


# --------------------------------------------------------------------------------------
def _validate_monitor_numeric_fields(monitors: list) -> None:
    """Coerce and bounds-check the per-monitor numeric overrides."""
    for monitor in monitors:
        mname = monitor.get("name", "?")
        _coerce_monitor_numbers(monitor, mname)
        _check_monitor_ceilings(monitor, mname)
        _check_monitor_floors(monitor, mname)


# --------------------------------------------------------------------------------------
def _coerce_monitor_numbers(monitor: dict, mname: str) -> None:
    """Turn the overrides into numbers, and require whole numbers where a
    count is meant."""
    for field in _NUMERIC_MONITOR_FIELDS:
        if field in monitor:
            monitor[field] = _coerce_numeric(
                monitor[field], f"monitors[{mname}].{field}"
            )
    for field in _WHOLE_MONITOR_FIELDS:
        if field in monitor:
            monitor[field] = _require_whole(
                monitor[field], f"monitors[{mname}].{field}"
            )


# --------------------------------------------------------------------------------------
def _check_monitor_ceilings(monitor: dict, mname: str) -> None:
    for field, ceiling in _MAX_MORE_FIELDS.items():
        if field in monitor and monitor[field] > ceiling:
            raise ConfigError(
                f"'monitors[{mname}].{field}' must be at most {ceiling}, "
                f"got {monitor[field]}"
            )
    if monitor.get("retention_days", 0) > _MAX_HISTORY_DAYS:
        raise ConfigError(
            f"'monitors[{mname}].retention_days' must be at most "
            f"{_MAX_HISTORY_DAYS}, got {monitor['retention_days']}"
        )
    if monitor.get("timeout", 0) > MAX_TIMEOUT_SECONDS:
        raise ConfigError(
            f"'monitors[{mname}].timeout' must be at most "
            f"{MAX_TIMEOUT_SECONDS}, got {monitor['timeout']}"
        )


# --------------------------------------------------------------------------------------
def _check_monitor_floors(monitor: dict, mname: str) -> None:
    for field in _MIN_ZERO_MONITOR_FIELDS:
        if field in monitor and monitor[field] < 0:
            raise ConfigError(
                f"'monitors[{mname}].{field}' must be 0 or more, "
                f"got {monitor[field]}"
            )
    # Per-monitor overrides of the same fields need the same floor --
    # a `check_interval_minutes: 0` on one monitor hammers that target
    # just as hard as the global setting would hammer every target.
    # timeout: 0 or less makes every TCP and HTTP check report down
    # (timed out at once), however healthy the target is.
    for field in (
        "check_interval_minutes", "retry_interval_minutes",
        "alert_cooldown_minutes", "timeout",
    ):
        if field in monitor and monitor[field] < 1:
            raise ConfigError(
                f"'monitors[{mname}].{field}' must be at least 1, "
                f"got {monitor[field]}"
            )


# --------------------------------------------------------------------------------------
def _validate_retention_rules(config: dict) -> None:
    """Keep history at least as long as the windows that read from it.

    Every retention_days (the global one and any a monitor sets for
    itself) must be at least stats_period_days and at least
    latency_graph_days, otherwise those windows promise more days than are
    kept. That also rejects 0 and negative values, since the windows
    are at least a day long.
    Expects the numeric fields to be coerced already.
    """
    global_cfg = config["global"]
    stats_days = global_cfg["stats_period_days"]
    graph_days = global_cfg["latency_graph_days"]
    sources = [("global.retention_days", global_cfg["retention_days"])]
    sources += [
        (f"monitors[{m.get('name', '?')}].retention_days", m["retention_days"])
        for m in config.get("monitors", [])
        if "retention_days" in m
    ]
    for label, retention in sources:
        if retention < stats_days:
            raise ConfigError(
                f"'{label}' is {retention}, but must be at least "
                f"global.stats_period_days ({stats_days})"
            )
        if retention < graph_days:
            raise ConfigError(
                f"'{label}' is {retention}, but must be at least "
                f"global.latency_graph_days ({graph_days})"
            )


# --------------------------------------------------------------------------------------
def _validate_numeric_fields(config: dict) -> None:
    """Validate and coerce numeric fields in-place, after env var substitution.

    Raises ConfigError with a specific field name on the first bad value
    found, rather than letting a bad type reach arithmetic deep in the
    monitoring loop at runtime.
    """
    global_cfg = config.get("global", {})
    for field in _NUMERIC_GLOBAL_FIELDS:
        if field in global_cfg:
            global_cfg[field] = _coerce_numeric(global_cfg[field], f"global.{field}")
    for field in _WHOLE_GLOBAL_FIELDS:
        if field in global_cfg:
            global_cfg[field] = _require_whole(
                global_cfg[field], f"global.{field}"
            )

    _validate_global_numeric_bounds(global_cfg)
    _validate_monitor_numeric_fields(config.get("monitors", []))


# --------------------------------------------------------------------------------------
def _validate_decimal_fields(config: dict) -> None:
    """Validate global.uptime_decimals/latency_decimals are non-negative
    integers within range, after env var substitution.

    Unlike _NUMERIC_GLOBAL_FIELDS (which just needs a number), these two
    are used as a round()/format() precision argument -- a float, a
    negative value, or an unreasonably large one would either crash or
    produce a nonsensical display, so they get their own stricter check.
    """
    global_cfg = config.get("global", {})
    for field in _DECIMAL_FIELDS:
        if field not in global_cfg:
            continue
        value = global_cfg[field]
        # ${ENV_VAR} substitution always yields a string (see the numeric
        # coercion above) -- accept a clean integer string the same way,
        # but don't allow "2.5" through: str.isdigit() rejects the dot.
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                f"'global.{field}' must be a non-negative integer, "
                f"got {value!r}"
            )
        if not (0 <= value <= _MAX_DECIMALS):
            raise ConfigError(
                f"'global.{field}' must be between 0 and {_MAX_DECIMALS}, "
                f"got {value}"
            )
        global_cfg[field] = value


# --------------------------------------------------------------------------------------
def _deep_merge(base, override):
    """Merge override dict into base dict recursively.

    Deep-copies `base`, which is the module-level DEFAULTS dict. With a
    shallow copy, a value the override didn't replace would be the same
    object as the one in DEFAULTS, and a caller changing the returned config
    (appending to config["tags"], say) would edit DEFAULTS itself for every
    later load_config() call. Copying here keeps this function safe without
    depending on what the callers do afterwards.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# --------------------------------------------------------------------------------------
def _load_yaml_file(path: Path, default):
    """Load a YAML file, returning a deep copy of default if not found.

    Every failure mode is converted to ConfigError, because that is the
    only exception type the dashboard/kiosk routes turn into the
    operator-facing config-error page -- anything else escapes as a bare
    500 with nothing to act on.

    OSError matters as much as YAMLError here: `path.exists()` is True
    for a *directory*, and Docker creates a missing bind-mount source as an
    empty directory. open() then raises IsADirectoryError, which is an
    OSError, not a YAMLError.
    """
    if not path.exists():
        return copy.deepcopy(default)
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path.name}: {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {path.name}: {e}")
    if raw is None:
        return copy.deepcopy(default)
    # Every caller either merges this into a dict or pulls a named
    # section out of it, so a file whose top level is a list or a scalar
    # would otherwise surface as a raw AttributeError from _deep_merge
    # ("'list' object has no attribute 'items'") rather than a ConfigError.
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path.name} must be a mapping at its top level, got "
            f"{type(raw).__name__} — the file's contents should sit under a "
            f"top-level key (e.g. `monitors:`), not start as a bare list "
            f"or value"
        )
    return raw


# --------------------------------------------------------------------------------------
def _reject_unknown_settings(
    raw_config, raw_alerts, raw_monitors, raw_tags, raw_maintenance
) -> None:
    """Raise a ConfigError naming every setting name the files do not accept.

    A name that is misspelled, in the wrong place, or not a setting would load
    and do nothing. See src/config_keys.py.
    """
    unknown = find_unknown_settings(
        raw_config, raw_alerts, raw_monitors, raw_tags, raw_maintenance
    )
    if unknown:
        shown = "; ".join(unknown[:10])
        more = f"; and {len(unknown) - 10} more" if len(unknown) > 10 else ""
        raise ConfigError(f"Unknown setting name(s) — {shown}{more}")


# --------------------------------------------------------------------------------------
def _empty_heading_error(name: str, fix: str) -> ConfigError:
    """A heading in alerts.yaml with nothing under it. Writing an empty list
    says there is nothing on purpose; a heading whose entries were all
    commented out may be a mistake, so it is an error rather than "none".
    `fix` is what to write, always a `[]` (the config files never use `{}`)."""
    return ConfigError(
        f"'{name}' has nothing under it — {fix}, or delete it — check "
        f"alerts.yaml"
    )


# --------------------------------------------------------------------------------------
def _alerts_section(raw_alerts) -> dict:
    """The contents of alerts.yaml's `alerts:` key. Left empty is an error;
    anything but a mapping is an error too (the merge below would otherwise
    raise a raw AttributeError). A file with no content at all is no alerts."""
    section = _section_or_default(raw_alerts, "alerts", {})
    if section is None:
        raise _empty_heading_error(
            "alerts", "put push_alerts: [] under it to mean no alerts"
        )
    if not isinstance(section, dict):
        raise ConfigError(
            f"'alerts' must be a mapping of settings, got "
            f"{type(section).__name__} — check alerts.yaml"
        )
    return section


# --------------------------------------------------------------------------------------
def _section_or_default(raw, key: str, default):
    """Pull a top-level section out of a raw loaded YAML file, or default."""
    return raw.get(key, default) if isinstance(raw, dict) else default


# --------------------------------------------------------------------------------------
def _validate_alerts_shape(config: dict) -> None:
    """`alerts` is a mapping, `email` a mapping, `push_alerts` a list of
    mappings. Checked before anything indexes into them."""
    alerts_cfg = config.get("alerts")
    if not isinstance(alerts_cfg, dict):
        raise ConfigError(
            f"'alerts' must be a mapping of settings, got "
            f"{type(alerts_cfg).__name__} — check alerts.yaml"
        )
    email_cfg = alerts_cfg.get("email")
    if email_cfg is None:
        raise _empty_heading_error(
            "email", "put recipients: [] under it to mean no email alerts"
        )
    if not isinstance(email_cfg, dict):
        raise ConfigError(
            f"'alerts.email' must be a mapping of settings, got "
            f"{type(email_cfg).__name__} — check alerts.yaml"
        )
    push_cfg = alerts_cfg.get("push_alerts")
    if push_cfg is None:
        raise _empty_heading_error("push_alerts", "write it as push_alerts: []")
    if not isinstance(push_cfg, list):
        raise ConfigError(
            f"'alerts.push_alerts' must be a list of channels, got "
            f"{type(push_cfg).__name__} — check alerts.yaml"
        )
    for i, channel in enumerate(push_cfg):
        if not isinstance(channel, dict):
            raise ConfigError(
                f"alerts.push_alerts[{i}] must be a mapping of fields, got "
                f"{type(channel).__name__} — check alerts.yaml"
            )


# --------------------------------------------------------------------------------------
def _validate_section_shapes(config: dict) -> None:
    """Check the list-shaped sections really are lists of mappings.

    Runs before any validation that indexes into those entries. A YAML file
    that parses to something else (the common cause is a trailing empty list
    item, "  -" on its own line, which YAML reads as None) would raise a raw
    AttributeError/TypeError from those validators or from maintenance.py's
    window evaluation, and dashboard()/kiosk() only turn ConfigError into the
    config-error page, so anything else is a bare 500.
    """
    # `global` is a mapping, unlike the three list sections below. A list or a
    # scalar there would raise a raw AttributeError ('list' object has no
    # attribute 'get') in whichever validator or route reached it first,
    # instead of showing the operator-facing config-error page.
    global_cfg = config.get("global")
    if not isinstance(global_cfg, dict):
        raise ConfigError(
            f"'global' must be a mapping of settings, got "
            f"{type(global_cfg).__name__} — check config.yaml"
        )

    _validate_alerts_shape(config)

    for section in ("monitors", "tags", "maintenance"):
        value = config.get(section)
        if not isinstance(value, list):
            raise ConfigError(
                f"'{section}' must be a list, got "
                f"{type(value).__name__} — check {section}.yaml"
            )

    for section in ("monitors", "maintenance"):
        for i, entry in enumerate(config[section]):
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"{section}[{i}] must be a mapping of fields, got "
                    f"{type(entry).__name__} — check {section}.yaml for a "
                    f"stray list item or bad indentation"
                )



# Fields each monitor type requires beyond name/type, checked in
# _validate_monitors_config. Without this, a typo'd or missing field (e.g.
# an http monitor with no `url`) isn't caught at config-load time -- it
# surfaces as a raw KeyError deep in the check function the first time
# that monitor is actually checked.
_REQUIRED_MONITOR_FIELDS = {
    "http": ("url",),
    "tcp": ("host", "port"),
    "ping": ("host",),
}


# --------------------------------------------------------------------------------------
def _validate_monitor_name(name, seen_names: set) -> None:
    """Validate one monitor's name and record it for duplicate detection.

    monitors.name is the primary key of the `monitors` table and the key of
    every in-memory scheduling dict in main.py, so a duplicate is not a
    cosmetic problem: two monitors sharing one name share a single state
    row and overwrite each other's counters, last_check and status on every
    pass, while the dashboard shows one row flickering between them.
    """
    if not isinstance(name, str):
        raise ConfigError(
            f"Monitor name {name!r} must be text — a numeric-looking "
            f"name needs quoting (name: \"{name}\")"
        )
    if not name.strip():
        raise ConfigError(
            "A monitor has an empty 'name'. Names identify monitors in "
            "the database and the dashboard, so they can't be blank."
        )
    if name != name.strip():
        raise ConfigError(
            f"Monitor name {name!r} starts or ends with a space — remove it. "
            f"A name with a space at either end would not match the same name "
            f"written without it in an alert or maintenance window."
        )
    if any(unicodedata.category(c) == "Cc" for c in name):
        raise ConfigError(
            f"Monitor name {name!r} contains a control character, such as a "
            f"line break or a tab — names appear in alert emails, where a "
            f"line break breaks the message."
        )
    if name.lower() == RESERVED_ALL:
        raise ConfigError(
            f"Monitor name {name!r} is reserved: \"{RESERVED_ALL}\" means "
            f"every monitor in an alert or a maintenance window. Choose "
            f"another name."
        )
    if len(name) > MAX_MONITOR_NAME_LENGTH:
        raise ConfigError(
            f"Monitor name {name[:40]!r}... is {len(name)} characters, but "
            f"the longest allowed is {MAX_MONITOR_NAME_LENGTH}. Names are "
            f"stored in a {MAX_MONITOR_NAME_LENGTH}-character database "
            f"column, so a longer one would fail when it is saved."
        )
    if name.lower() in seen_names:
        raise ConfigError(
            f"Duplicate monitor name {name!r}. Names must be unique, "
            f"ignoring capitalization — they're the primary key for a "
            f"monitor's stored history, so two monitors sharing one would "
            f"overwrite each other's state."
        )
    seen_names.add(name.lower())


# --------------------------------------------------------------------------------------
# The host of a ping or TCP monitor must be a full IP address or a hostname.
# Documented at https://uptimeherald.org/docs/hosts-urls/.
#   * IP address: checked with the ipaddress module, which wants all four
#     parts of an IPv4 address. Old shortened forms ("127.1", "127.0.1", a
#     single number such as 2130706433, hex such as 0x7f.1) are refused,
#     because ping and the TCP connection would read them as other
#     addresses (127.1 is 127.0.0.1), so a typo like "192.168.1" would
#     quietly monitor 192.168.0.1.
#   * Hostname: dot-separated labels of letters, digits, hyphens and
#     underscores, none starting or ending with a hyphen. Underscores are
#     allowed because Docker container and service names use them and
#     resolve. Non-ASCII names are allowed and are checked in the "xn--" form
#     that DNS uses, converted by src/hostnames.py with the IDNA 2008 rules,
#     the same ones ping and httpx use, so all three checks ask DNS for the
#     same name. Symbols and emoji are refused by that conversion, and so is
#     an underscore in a name that also has non-ASCII letters. At most 63
#     characters per label and 253 overall, counted in that form.
#     A single label such as "myserver" is fine. No trailing dot, no spaces.
#   * The last label may not be all digits, which would be a shortened
#     IPv4 address (see above) and is never a real top-level domain.
def _is_word_character(char: str) -> bool:
    """A letter, digit or combining mark in any language (not a symbol, emoji or
    punctuation). Used for the non-ASCII characters of paths and queries:
    people can write names in their own language, but a symbol or emoji is not
    accepted."""
    return unicodedata.category(char)[0] in "LMN"


_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?")


# --------------------------------------------------------------------------------------
def _is_valid_host(value) -> bool:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    if value.endswith("."):
        return False
    try:
        ascii_name = to_dns_name(value)
    except UnicodeError:
        return False
    labels = ascii_name.split(".")
    return (
        len(ascii_name) <= 253
        and all(
            len(label) <= 63 and _HOST_LABEL_RE.fullmatch(label)
            for label in labels
        )
        and not labels[-1].isdigit()
    )


# credentials.ini holds the basic-authentication credentials that http monitors
# use, one section per credential, named with a label the user chooses:
#
#     [wiki-login]
#     username = your-username-here
#     password = p@ss w0rd#1
#
# A monitor names a credential with `credentials: "wiki-login"`, and several
# monitors can name the same one. Documented at
# https://uptimeherald.org/docs/credentials/. The file is read with
# the standard library's configparser, with its extras turned off so a
# password is taken exactly as written: no % expansion, no inline comments,
# "=" is the only separator, and a section named DEFAULT is not special.
# Error messages never include the text of a line, because that text can be
# a password and error messages are shown on the dashboard.
CREDENTIALS_FILE = "credentials.ini"
_CREDENTIAL_KEYS = ("username", "password")


# --------------------------------------------------------------------------------------
def _load_credentials_file(path: Path) -> dict:
    """Read credentials.ini into {label: {"username": ..., "password": ...}}.

    A missing file is an empty result. Every credential needs both keys written
    out, and nothing else is accepted in a section. A value may be empty
    (some services take an API key as the username and require no password), but
    the key has to be there, so a forgotten line is still caught.
    """
    if not path.exists():
        return {}
    parser = configparser.ConfigParser(
        interpolation=None,
        delimiters=("=",),
        comment_prefixes=("#",),
        inline_comment_prefixes=None,
        default_section="\x00no-default-section",
        empty_lines_in_values=False,
        strict=True,
    )
    try:
        parser.read_string(path.read_text(encoding="utf-8"), source=CREDENTIALS_FILE)
    except OSError as e:
        raise ConfigError(
            f"{CREDENTIALS_FILE} can't be read: {e.strerror or 'unknown error'}"
        ) from None
    except UnicodeDecodeError:
        raise ConfigError(f"{CREDENTIALS_FILE} must be a UTF-8 text file") from None
    except configparser.DuplicateSectionError as e:
        raise ConfigError(
            f"{CREDENTIALS_FILE} has the credential [{e.section}] more than once"
        ) from None
    except configparser.DuplicateOptionError as e:
        raise ConfigError(
            f"{CREDENTIALS_FILE} sets {e.option!r} twice in [{e.section}]"
        ) from None
    except configparser.Error as e:
        line = getattr(e, "lineno", None)
        where = f" near line {line}" if line else ""
        raise ConfigError(
            f"{CREDENTIALS_FILE} is not valid{where}: each credential is a [label] "
            f"line followed by username = ... and password = ... lines"
        ) from None
    credentials = {}
    written: dict[str, str] = {}
    for label in parser.sections():
        _check_credential_label(label)
        key = label.lower()
        if key in written:
            raise ConfigError(
                f"{CREDENTIALS_FILE} has [{written[key]}] and [{label}], which are "
                f"the same label — labels are not case sensitive"
            )
        written[key] = label
        credentials[key] = _credential_values(label, dict(parser.items(label)))
    return credentials


# --------------------------------------------------------------------------------------
def _check_credential_label(label: str) -> None:
    """A label follows the rules for a monitor's name (see _validate_monitor_name):
    not blank, no control character such as a tab, and no space at either end."""
    if not label.strip():
        raise ConfigError(
            f"{CREDENTIALS_FILE} has a blank label, [{label}] — write a label "
            f"between the brackets"
        )
    if label != label.strip():
        raise ConfigError(
            f"{CREDENTIALS_FILE} has the label [{label}], which starts or ends "
            f"with a space — remove it"
        )
    if _has_control_character(label):
        raise ConfigError(
            f"{CREDENTIALS_FILE} has the label [{label!r}], which holds a tab or "
            f"another control character — remove it"
        )


# --------------------------------------------------------------------------------------
def _credential_values(label: str, values: dict) -> dict:
    """Check one credential's settings and return its username and password."""
    unknown = sorted(set(values) - set(_CREDENTIAL_KEYS))
    if unknown:
        raise ConfigError(
            f"{CREDENTIALS_FILE} [{label}] has an unknown setting "
            f"{unknown[0]!r} — only username and password are accepted"
        )
    missing = [k for k in _CREDENTIAL_KEYS if k not in values]
    if missing:
        raise ConfigError(
            f"{CREDENTIALS_FILE} [{label}] is missing "
            f"{' and '.join(missing)} — write both username and password; "
            f"a value may be left empty (for example 'password =')"
        )
    return {k: values[k] for k in _CREDENTIAL_KEYS}


# Monitors already warned about for sending a credential over plain http://. The
# config is re-read on every dashboard request and every minute by the
# monitoring loop, so without this the warning would repeat in the logs
# without end. Keyed by monitor name and url, so changing either warns again.
_PLAIN_HTTP_CREDENTIAL_WARNED: set[tuple[str, str]] = set()


# Credentials already warned about as unused, so the warning is logged once while
# the credential stays unused (the config is re-read constantly). A credential that is
# used again is forgotten, so it warns again if it becomes unused later.
_UNUSED_CREDENTIAL_WARNED: set[str] = set()


# --------------------------------------------------------------------------------------
def _warn_unused_credentials(config: dict) -> None:
    """Log a warning for each credential in credentials.ini that no monitor uses.

    Not an error: a credential may be waiting for a monitor or left over from one
    that was removed. The warning names the label and never shows the
    username or password.
    """
    used = {m["credentials"] for m in config["monitors"] if "credentials" in m}
    for label in config["credentials"]:
        if label in used:
            _UNUSED_CREDENTIAL_WARNED.discard(label)
        elif label not in _UNUSED_CREDENTIAL_WARNED:
            _UNUSED_CREDENTIAL_WARNED.add(label)
            logger.warning(
                "The credential [%s] in %s is not used by any monitor. Remove it, "
                "or name it with 'credentials' on a monitor.",
                label, CREDENTIALS_FILE,
            )


# --------------------------------------------------------------------------------------
def _validate_monitor_credentials(monitor: dict, credentials: dict) -> None:
    """Check a monitor's `credentials` label and that its url carries none."""
    name = monitor["name"]
    if monitor["type"] == "http" and "@" in urlsplit(monitor["url"]).netloc:
        raise ConfigError(
            f"Monitor '{name}' has a username or password in its url — "
            f"remove it and use the 'credentials' setting with a credential in "
            f"{CREDENTIALS_FILE} instead"
        )
    if "credentials" not in monitor:
        return
    if monitor["type"] != "http":
        raise ConfigError(
            f"Monitor '{name}' (type '{monitor['type']}') sets 'credentials', "
            f"which only applies to http monitors"
        )
    label = monitor["credentials"]
    if not isinstance(label, str) or not label.strip():
        raise ConfigError(
            f"Monitor '{name}' has credentials {label!r} — expected the label "
            f"of a credential in {CREDENTIALS_FILE}"
        )
    if label.lower() not in credentials:
        raise ConfigError(
            f"Monitor '{name}' uses credentials '{label}', which is not a "
            f"credential in {CREDENTIALS_FILE}"
        )
    # Labels are not case sensitive. Stored lowercase, like the labels in
    # credentials.ini, so the lookups in main.py and the unused check match.
    monitor["credentials"] = label.lower()
    # Allowed on purpose, since plenty of internal devices only speak http://,
    # but logged once: the credential is sent unencrypted on every check.
    if monitor["url"].lower().startswith("http://") and (
        (name, monitor["url"]) not in _PLAIN_HTTP_CREDENTIAL_WARNED
    ):
        _PLAIN_HTTP_CREDENTIAL_WARNED.add((name, monitor["url"]))
        logger.warning(
            "Monitor '%s' sends its credential over plain http://, so the username "
            "and password are not encrypted on the network. Use an https:// "
            "url if the site supports one.",
            name,
        )


# The url of an http monitor. Documented at
# https://uptimeherald.org/docs/hosts-urls/.
#   * Scheme: http or https, lowercase.
#   * No username or password in the url (use the `credentials` setting).
#   * No fragment (the part after #): it is never sent to the server, so in a
#     monitor it can only mislead.
#   * No spaces or control characters anywhere: a URL has no legal way to
#     hold them, they must be percent-encoded (%20 for a space).
#   * Host: required, and the same rule as the `host` setting (a hostname or a
#     full IP address). An IPv6 address needs its square brackets.
#   * Port: optional, 1 to 65535. A colon with nothing after it is an error.
#   * Path and query: anything else is accepted, including non-ASCII letters,
#     which the HTTP library percent-encodes itself (as for hostnames).
# Error messages say which part is wrong and do not repeat the whole url,
# which can carry a token in its query.
_URL_AUTHORITY_RE = re.compile(r"(?P<host>\[[^\]]*\]|[^:\[\]]*)(?::(?P<port>.*))?")


# What a path or query may contain, from RFC 3986: letters, digits and
# - . _ ~ ! $ & ' ( ) * + , ; = : @ / ?, plus %XX for anything else. Non-ASCII
# letters and digits are accepted on top of that (see _is_word_character).
_URL_RAW_ASCII = frozenset(string.ascii_letters + string.digits + "-._~!$&'()*+,;=:@/?")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


# --------------------------------------------------------------------------------------
def _url_part_is_valid(text: str) -> bool:
    if _BAD_PERCENT_RE.search(text):
        return False
    return all(
        c == "%" or c in _URL_RAW_ASCII or (ord(c) > 127 and _is_word_character(c))
        for c in text
    )


# --------------------------------------------------------------------------------------
def _url_host_problem(host: str, port: str | None) -> str | None:
    """Return what is wrong with the host part of an http monitor's url."""
    if host.startswith("["):
        host = host[1:-1]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            # urlsplit already refuses brackets around an IPv4 address or
            # anything that is not an address, but still lets an "IPvFuture"
            # form such as [v1.abc] through.
            return (
                "the address inside the square brackets is not a valid IPv6 address"
            )
    elif ":" in host:
        return "an IPv6 address needs square brackets, as in http://[::1]/"
    if not host:
        if port and ":" in port:
            return "an IPv6 address needs square brackets, as in http://[::1]/"
        return "it has no host"
    if not _is_valid_host(host):
        return (
            f"the host {host!r} is not a hostname or a full IP address "
            f"(shortened addresses such as 127.1 are not accepted)"
        )
    return None


# --------------------------------------------------------------------------------------
def _url_port_problem(port: str | None) -> str | None:
    """Return what is wrong with the port of an http monitor's url."""
    if port is not None and not (
        port.isascii() and port.isdigit() and 1 <= int(port) <= 65535
    ):
        return (
            "the port must be a whole number from 1 to 65535 "
            "(a colon needs one after it)"
        )
    return None


# --------------------------------------------------------------------------------------
def _url_path_problem(path: str, query: str) -> str | None:
    """Return what is wrong with the path or query of an http monitor's url."""
    if not (_url_part_is_valid(path) and _url_part_is_valid(query)):
        return (
            "its path or query has a character the url standard does not "
            "allow, such as a quote, < > ^ ` { | } [ ] \\, a % that is not "
            "followed by two hex digits, or an emoji or symbol. Write it "
            "percent-encoded instead, for example %5B for [ (servers decode "
            "it back, so the request means the same)"
        )
    return None


# --------------------------------------------------------------------------------------
def _url_problem(url: str) -> str | None:
    """Return what is wrong with an http monitor's url, or None if it is fine."""
    if not url.startswith(("http://", "https://")):
        return "it must start with http:// or https://"
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in url):
        return (
            "it contains a space or control character, which a url can't hold "
            "(write a space as %20)"
        )
    try:
        parts = urlsplit(url)
    except ValueError:
        return (
            "it is not a valid url (check the square brackets of an IPv6 address)"
        )
    if "@" in parts.netloc:
        return None  # reported by _validate_monitor_credentials, with advice
    if "#" in url:
        return (
            "it has a fragment (the part after #), which is never sent to "
            "the server and does nothing in a monitor"
        )
    authority = _URL_AUTHORITY_RE.fullmatch(parts.netloc)
    if authority is None:
        return "its host or port is not valid"
    host, port = authority["host"], authority["port"]
    return (
        _url_host_problem(host, port)
        or _url_port_problem(port)
        or _url_path_problem(parts.path, parts.query)
    )


# --------------------------------------------------------------------------------------
def _validate_tcp_target(name: str, monitor: dict) -> None:
    port = monitor["port"]
    # Already coerced to a number by _validate_monitor_numeric_fields; this
    # checks that it is a usable port.
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(
            f"Monitor '{name}' has port {port!r} — "
            f"expected a whole number from 1 to 65535"
        )


# --------------------------------------------------------------------------------------
def _validate_ping_target(name: str, monitor: dict, default_timeout: float) -> None:
    # Already coerced to a number by _validate_monitor_numeric_fields.
    # Without this, `ping -c 0` (or a negative or fractional count)
    # is rejected by ping itself and the monitor reports "down" on
    # every check, with ping's usage error as the only clue.
    count = monitor.get("count", 1)
    if not isinstance(count, int) or count < 1:
        raise ConfigError(
            f"Monitor '{name}' has count {count!r} — "
            f"expected a whole number of 1 or more"
        )
    # Pings go out one per second, so a count of N takes about N - 1
    # seconds, and a ping check is killed after timeout + 2 seconds
    # (see check_ping). A count above the timeout therefore ends up
    # killed and reported down even when the host is fine. Capping count at
    # the timeout leaves a margin for real network latency.
    timeout = monitor.get("timeout", default_timeout)
    if count > timeout:
        raise ConfigError(
            f"Monitor '{name}' has count {count} but timeout {timeout} "
            f"— pings are sent one per second, so count can't be larger "
            f"than timeout. Raise timeout or lower count"
        )


# --------------------------------------------------------------------------------------
def _validate_http_target(name: str, monitor: dict) -> None:
    url = monitor["url"]
    # httpx rejects a scheme-less URL at request time, so without this
    # the monitor fails every check with "Request URL is missing an
    # 'http://' or 'https://' protocol" and no sign the config caused it.
    problem = (
        "it must start with http:// or https://"
        if not isinstance(url, str) else _url_problem(url)
    )
    if problem:
        raise ConfigError(
            f"Monitor '{name}' has a url that is not valid — {problem}"
        )
    # Already coerced to a number by _validate_monitor_numeric_fields.
    if "expected_status" in monitor:
        status = monitor["expected_status"]
        if (
            not isinstance(status, int)
            or status not in _VALID_EXPECTED_STATUS_CODES
        ):
            raise ConfigError(
                f"Monitor '{name}' has expected_status {status!r} — it "
                f"must be a registered HTTP status code or a well-known "
                f"unofficial one, such as 200, 401 or 404"
            )


# --------------------------------------------------------------------------------------
def _validate_monitor_target(monitor: dict, default_timeout: float) -> None:
    """Validate the type-specific target values a check actually needs.

    A bad value here would load and then fail every check, reported per check
    instead of at startup.
    """
    name, monitor_type = monitor["name"], monitor["type"]
    if monitor_type in ("tcp", "ping") and not _is_valid_host(monitor["host"]):
        raise ConfigError(
            f"Monitor '{name}' has host {monitor['host']!r} — expected a "
            f"hostname or a full IP address (shortened addresses such as "
            f"127.1 are not accepted)"
        )
    if monitor_type == "tcp":
        _validate_tcp_target(name, monitor)
    elif monitor_type == "ping":
        _validate_ping_target(name, monitor, default_timeout)
    elif monitor_type == "http":
        _validate_http_target(name, monitor)


# --------------------------------------------------------------------------------------
def _validate_monitors_config(monitors: list, default_timeout: float) -> None:
    """Validate that each monitor has a unique name, a supported type, and
    every field its type requires to actually run a check."""
    valid_types = {"http", "ping", "tcp"}
    seen_names: set[str] = set()
    for monitor in monitors:
        if "name" not in monitor:
            raise ConfigError("Each monitor must have a 'name' field")
        _validate_monitor_name(monitor["name"], seen_names)
        if "type" not in monitor:
            raise ConfigError(f"Monitor '{monitor['name']}' missing 'type'")
        if not isinstance(monitor["type"], str) or monitor["type"] not in valid_types:
            raise ConfigError(
                f"Monitor '{monitor['name']}': type must be one of "
                f"{valid_types}, got {monitor['type']!r}"
            )
        required = _REQUIRED_MONITOR_FIELDS[monitor["type"]]
        missing = [f for f in required if monitor.get(f) in (None, "")]
        if missing:
            raise ConfigError(
                f"Monitor '{monitor['name']}' (type '{monitor['type']}') "
                f"missing required field(s): {', '.join(missing)}"
            )
        _validate_monitor_target(monitor, default_timeout)
        if "expected_status" in monitor and monitor["type"] != "http":
            raise ConfigError(
                f"Monitor '{monitor['name']}' (type '{monitor['type']}') "
                f"sets 'expected_status', which only applies to http monitors"
            )


# --------------------------------------------------------------------------------------
def _check_tag_name(tag, entity_label: str) -> None:
    """Raise unless `tag` is text made only of letters, digits, hyphens and
    underscores. Letters and digits from any language are accepted, the same
    way as in the non-ASCII part of a URL (see _is_word_character); spaces,
    other punctuation, symbols and emoji are not.

    The dashboard stores a monitor's tags as one comma-separated string and
    splits it again in the browser, so a tag with a comma in it would turn
    into two tags there.
    """
    if not isinstance(tag, str):
        raise ConfigError(
            f"{entity_label} has the tag {tag!r}, which is not text — "
            f"put it in quotes"
        )
    if not tag:
        raise ConfigError(f"{entity_label} has an empty tag")
    if len(tag) > MAX_TAG_LENGTH:
        raise ConfigError(
            f"{entity_label} has a tag that starts {tag[:40]!r}... and is "
            f"{len(tag)} characters long, but the longest allowed is "
            f"{MAX_TAG_LENGTH}"
        )
    if tag.lower() == RESERVED_ALL:
        raise ConfigError(
            f"{entity_label} has the tag {tag!r}, which is reserved: "
            f"\"{RESERVED_ALL}\" is what an alert or a maintenance window "
            f"uses for every monitor that has a tag. Choose another name."
        )
    bad = sorted({c for c in tag if c not in "-_" and not _is_word_character(c)})
    if bad:
        found = ", ".join(repr(c) for c in bad)
        raise ConfigError(
            f"{entity_label} has the tag {tag!r}, which contains {found} — "
            f"a tag can only contain letters, digits, hyphens (-) and "
            f"underscores (_)"
        )


# --------------------------------------------------------------------------------------
def _check_tags_allowed(
    tags, allowed_tags: set, entity_label: str, allow_all: bool = False,
) -> None:
    """Raise if `tags` isn't a list of tags that are all in the allowlist.

    An alert or a maintenance window (`allow_all=True`) may also have
    `tags: "all"`, meaning every monitor that has at least one tag. Any other
    text is an error: a bare string would otherwise be read letter by letter
    (`for tag in "critical"`), and "none" is not a special value, it is just
    text like any other.
    """
    if allow_all and tags == RESERVED_ALL:
        return
    if not isinstance(tags, list):
        expected = (
            f"a list of tag names, or \"{RESERVED_ALL}\"" if allow_all
            else "a list of tag names"
        )
        raise ConfigError(
            f"{entity_label} has an invalid 'tags' value {tags!r} — "
            f"expected {expected}"
        )
    for tag in tags:
        _check_tag_name(tag, entity_label)
        if tag not in allowed_tags:
            raise ConfigError(
                f"{entity_label} references unknown tag "
                f"'{tag}' — add it to tags.yaml or remove it"
            )


# --------------------------------------------------------------------------------------
def _lowercase_tag_list(tags):
    """Return `tags` with every text tag in lowercase.

    Anything that is not a list is returned unchanged, and so is any list
    item that is not text: _check_tags_allowed() reports those.
    """
    if not isinstance(tags, list):
        return tags
    return [tag.lower() if isinstance(tag, str) else tag for tag in tags]


# --------------------------------------------------------------------------------------
def _normalize_tag_case(config: dict) -> None:
    """Make tags case insensitive by lowercasing every tag when the config
    loads: in tags.yaml, monitors, the email alert, push alerts, and
    maintenance windows.

    The dashboard shows tags in capitals, so "Critical" and "critical" would
    look identical there, and someone who writes both almost certainly means
    one tag. Doing this once at load means tag matching, the tags.yaml check,
    and the dashboard filter only ever see one spelling.
    """
    config["tags"] = _lowercase_tag_list(config["tags"])
    entries = list(config["monitors"]) + list(config["maintenance"])
    email = config["alerts"].get("email")
    if isinstance(email, dict) and isinstance(email.get("recipients"), list):
        entries.extend(email["recipients"])
    push_alerts = config["alerts"].get("push_alerts")
    if isinstance(push_alerts, list):
        entries.extend(push_alerts)
    for entry in entries:
        if isinstance(entry, dict) and "tags" in entry:
            entry["tags"] = _lowercase_tag_list(entry["tags"])


# --------------------------------------------------------------------------------------
def _check_monitors_field(
    value, known_monitors: set, entity_label: str, consequence: str,
) -> None:
    """Raise unless `value` (the `monitors` setting of an alert or a
    maintenance window) is "all" or a list of names of monitors that exist.

    A typo in a name would otherwise load cleanly and `consequence` (what the
    entry would then fail to do) would only show up in the field.
    """
    if value == RESERVED_ALL:
        return
    if not isinstance(value, list):
        raise ConfigError(
            f"{entity_label} has an invalid 'monitors' value {value!r} — "
            f"expected a list of monitor names, or \"{RESERVED_ALL}\""
        )
    unknown = [
        m for m in value
        if not isinstance(m, str) or m.lower() not in known_monitors
    ]
    if unknown:
        raise ConfigError(
            f"{entity_label} targets unknown monitor(s): "
            f"{', '.join(repr(u) for u in unknown)}. Add them to "
            f"monitors.yaml or correct the spelling — as written "
            f"{consequence}."
        )


# --------------------------------------------------------------------------------------
def _alert_and_window_entries(config: dict):
    """Yield (label, entry, consequence of a wrong monitor name, in use) for
    each recipients entry of the email alert, each push alert and each
    maintenance window, the entries that choose monitors with `monitors` and
    `tags`. `in use` is False for an alert that is turned off (an entry is on
    unless its `enabled` is false)."""
    email = config["alerts"].get("email")
    if isinstance(email, dict) and isinstance(email.get("recipients"), list):
        for i, recipient in enumerate(email["recipients"]):
            if isinstance(recipient, dict):
                yield (
                    f"Email alert recipients[{i}]", recipient,
                    "the alert would never be sent for it",
                    recipient.get("enabled", True) is not False,
                )
    for channel in config["alerts"].get("push_alerts", []):
        yield (
            f"Alert channel '{channel.get('type', 'generic')}'", channel,
            "the alert would never be sent for it",
            channel.get("enabled", True) is not False,
        )
    for window in config.get("maintenance", []):
        yield (
            f"Maintenance window '{window.get('name', '?')}'", window,
            "the window would never suppress anything", True,
        )


# --------------------------------------------------------------------------------------
def _validate_tag_references(config: dict, allowed_tags: set) -> None:
    """Validate the tags and monitor names that monitors, alerts and
    maintenance windows refer to.

    tags.yaml is the only source of tags: a tag used anywhere has to be
    listed there, so an empty (or missing) tags.yaml allows no tags. Alerts
    and maintenance windows may also say "all" for `tags` or `monitors`.
    """
    for tag in config["tags"]:
        _check_tag_name(tag, "tags.yaml")
    known_monitors = {m["name"].lower() for m in config["monitors"]}
    for monitor in config["monitors"]:
        _check_tags_allowed(
            monitor.get("tags", []), allowed_tags, f"Monitor '{monitor['name']}'"
        )
    for label, entry, consequence, _ in _alert_and_window_entries(config):
        if "tags" in entry:
            _check_tags_allowed(entry["tags"], allowed_tags, label, allow_all=True)
        if "monitors" in entry:
            _check_monitors_field(
                entry["monitors"], known_monitors, label, consequence,
            )


# --------------------------------------------------------------------------------------
def _has_target(entry: dict) -> bool:
    """True if `monitors` or `tags` is "all" or a list with something in it."""
    for field in ("monitors", "tags"):
        value = entry.get(field)
        if value == RESERVED_ALL or (isinstance(value, list) and value):
            return True
    return False


# --------------------------------------------------------------------------------------
def _require_alert_and_window_targets(config: dict) -> None:
    """An alert that is turned on and every maintenance window must say which
    monitors it is for, with `monitors` or `tags` set to "all" or to a list.
    Leaving both out means no monitor, so the entry could never do anything
    and would look as if it was working."""
    for label, entry, _, in_use in _alert_and_window_entries(config):
        if in_use and not _has_target(entry):
            raise ConfigError(
                f"{label} doesn't say which monitors it is for — set "
                f"'monitors' or 'tags' to \"{RESERVED_ALL}\" or to a list"
            )


# --------------------------------------------------------------------------------------
def entry_applies_to_monitor(entry: dict, monitor_name: str, monitor_tags) -> bool:
    """Whether an alert or a maintenance window is for this monitor, by its
    `monitors` and `tags` settings. Either one matching is enough.

    `monitors` is "all" or a list of names, which are not case sensitive.
    `tags` is "all" (any monitor that has at least one tag) or a list (any
    monitor that has at least one of them). A setting that is left out
    matches nothing.
    """
    monitors = entry.get("monitors")
    if monitors == RESERVED_ALL:
        return True
    if isinstance(monitors, list) and monitor_name.lower() in {
        m.lower() for m in monitors if isinstance(m, str)
    }:
        return True
    tags = entry.get("tags")
    if tags == RESERVED_ALL:
        return bool(monitor_tags)
    return isinstance(tags, list) and bool(set(monitor_tags).intersection(tags))


# --------------------------------------------------------------------------------------
def _validate_enabled_flag(entry: dict, label: str) -> None:
    """`enabled` turns an alert channel on or off. A quoted "false" is text,
    which is truthy, so it would leave the channel on; require true or false
    (text forms are converted, as for the other on/off settings)."""
    if "enabled" in entry:
        entry["enabled"] = _coerce_boolean(entry["enabled"], f"{label}.enabled")


# --------------------------------------------------------------------------------------
def _validate_system_flag(entry: dict, label: str) -> None:
    """`system` marks a channel as the one for system notices (such as a
    database nearing end of support). A string like "false" would be truthy
    and silently route notices to the wrong place, so require a real bool."""
    if "system" in entry and not isinstance(entry["system"], bool):
        raise ConfigError(
            f"{label}.system must be true or false, got "
            f"{entry['system']!r} — check alerts.yaml"
        )


# --------------------------------------------------------------------------------------
def _apply_smtp_credentials_from_environment(email_cfg: dict) -> None:
    """The SMTP username and password come only from SMTP_USER and
    SMTP_PASSWORD in the environment, which the compose files pass in from
    .env (an unset one arrives as an empty string, which counts as not set).
    They are not alerts.yaml settings, so the credential is set in one place,
    as a pair. A smtp_user or smtp_password written in alerts.yaml is rejected
    earlier, as an unknown setting."""
    for setting, variable in (
        ("smtp_user", "SMTP_USER"),
        ("smtp_password", "SMTP_PASSWORD"),
    ):
        email_cfg.pop(setting, None)
        value = os.environ.get(variable)
        if value:
            email_cfg[setting] = value


# The settings of one `recipients` entry of the email alert. Everything else
# in the `email:` block is shared server and sender settings.
_EMAIL_RECIPIENT_KEYS = ("enabled", "to", "monitors", "tags", "system")


# --------------------------------------------------------------------------------------
def email_recipient_config(email_cfg: dict, recipient: dict) -> dict:
    """The settings needed to send one message: the shared server and sender
    settings of the email alert, with one `recipients` entry's own settings
    (`enabled`, `to`, `monitors`, `tags`, `system`) on top."""
    shared = {
        key: value for key, value in email_cfg.items()
        if key != "recipients" and key not in _EMAIL_RECIPIENT_KEYS
    }
    return {**shared, **recipient}


# --------------------------------------------------------------------------------------
def _validate_email_recipient(index: int, recipient) -> None:
    """Validate one entry of the email alert's `recipients` list."""
    label = f"alerts.email.recipients[{index}]"
    if not isinstance(recipient, dict):
        raise ConfigError(
            f"{label} must be a mapping of settings, got "
            f"{type(recipient).__name__} — check alerts.yaml"
        )
    _validate_system_flag(recipient, label)
    _validate_enabled_flag(recipient, label)
    to = recipient.get("to")
    if not to:
        raise ConfigError(
            f"{label} is missing the required 'to' list — set it in alerts.yaml"
        )
    # `to` is documented as a list and the To header is built with
    # ", ".join(...). A bare string passes the truthiness check above but
    # then gets joined character-by-character ("a@b.c" -> "a, @, b, ., c"),
    # producing a silently unusable header rather than any visible error.
    if not isinstance(to, list):
        raise ConfigError(
            f"'{label}.to' must be a list of addresses, e.g. "
            f"to: [\"you@example.com\"] — got {type(to).__name__}"
        )
    # Checked with the same rule as contact_email: well-formed addresses only
    # (whether one is real is never checked).
    for address in to:
        if not _is_email_address(address):
            raise ConfigError(
                f"'{label}.to' must contain only email addresses such as "
                f"you@example.com, got {address!r}"
            )


# --------------------------------------------------------------------------------------
def _has_control_character(text: str) -> bool:
    """True if `text` holds a control character: a line break, a tab, and so
    on. In an email header or a request header one of these breaks the
    message, or lets whatever follows be read as a header of its own."""
    return any(unicodedata.category(c) == "Cc" for c in text)


# --------------------------------------------------------------------------------------
def _check_header_text(value, label: str, max_length: int = 255) -> None:
    """Raise unless `value` is text that is safe to put in an email header:
    no control characters, and not unreasonably long."""
    if not isinstance(value, str):
        raise ConfigError(
            f"'{label}' must be text, got {type(value).__name__} — put it in "
            f"quotes"
        )
    if _has_control_character(value):
        raise ConfigError(
            f"'{label}' contains a control character, such as a line break or "
            f"a tab — it is put in the header of every alert email"
        )
    if len(value) > max_length:
        raise ConfigError(
            f"'{label}' is {len(value)} characters, but the longest allowed "
            f"is {max_length}"
        )


# A subject prefix is a short tag in front of the monitor's name, so it is
# held to a shorter limit than the name shown next to the From address.
_HEADER_TEXT_LONGEST = {"from_name": 255, "subject_prefix": 50}


# --------------------------------------------------------------------------------------
def _check_email_header_settings(email_cfg: dict) -> None:
    """from_name and subject_prefix go into the header of every alert email."""
    for field, longest in _HEADER_TEXT_LONGEST.items():
        if field in email_cfg:
            _check_header_text(
                email_cfg[field], f"alerts.email.{field}", longest
            )


# --------------------------------------------------------------------------------------
def _validate_email_alert_config(email_cfg: dict) -> None:
    """Validate email alert config — the required fields are easy to leave
    unset since they ship commented-out in alerts.default.yaml, and a
    missing field would only surface as a KeyError the first time a
    real alert needs to fire.

    Email is on when at least one entry in `recipients` is on. Every entry is
    checked, on or off; the server and sender settings are required only when
    an entry is on.
    """
    _check_email_header_settings(email_cfg)
    recipients = email_cfg.get("recipients", [])
    if recipients is None:
        raise _empty_heading_error("recipients", "write it as recipients: []")
    if not isinstance(recipients, list):
        raise ConfigError(
            "'alerts.email.recipients' must be a list of entries, each with a "
            f"'to' list — got {type(recipients).__name__}"
        )
    for index, recipient in enumerate(recipients):
        _validate_email_recipient(index, recipient)
    if not any(r.get("enabled", True) for r in recipients):
        return
    required = ("smtp_host", "smtp_port", "from")
    missing = [f for f in required if not email_cfg.get(f)]
    if missing:
        raise ConfigError(
            f"Email alerts are on (an entry in recipients is enabled) but "
            f"missing required field(s): {', '.join(missing)} — set them in "
            f"alerts.yaml"
        )
    if not _is_email_address(email_cfg["from"]):
        raise ConfigError(
            "'alerts.email.from' must be an email address such as "
            f"you@example.com, got {email_cfg['from']!r}"
        )
    # The SMTP server follows the same rule as the host of a ping or TCP
    # monitor: a hostname or a full IP address.
    if not _is_valid_host(email_cfg["smtp_host"]):
        raise ConfigError(
            "'alerts.email.smtp_host' must be a hostname or a full IP address "
            f"such as smtp.example.com, got {email_cfg['smtp_host']!r}"
        )
    # ${ENV_VAR} substitution yields text, so a port written that way ("587")
    # is converted to a number first.
    port = _coerce_numeric(email_cfg["smtp_port"], "alerts.email.smtp_port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(
            "'alerts.email.smtp_port' must be a whole number from 1 to 65535, "
            f"got {email_cfg['smtp_port']!r}"
        )
    email_cfg["smtp_port"] = port


# Push-alert channel types, and the fields each one indexes directly in
# alerts.py's senders. Validated at load for the same reason monitors are
# (_REQUIRED_MONITOR_FIELDS above): send_push_alert() dispatches on the
# type string and falls through to the "generic" webhook branch for
# anything it doesn't recognise, so a typo'd `type: telgram` would load
# and then fail at the first real outage -- and because a failed attempt
# still advances the cooldown, the alert would never be retried.
_REQUIRED_CHANNEL_FIELDS = {
    "telegram": ("bot_token", "chat_id"),
    "ntfy": ("url",),
    "callmebot": (),  # depends on the `channel`, see _CALLMEBOT_CHANNEL_FIELDS
    "discord": ("url",),
    "mattermost": ("url",),
    "rocket.chat": ("url",),
    "slack": ("url",),
    "generic": ("url",),
}

# The delivery methods of a CallMeBot push alert, and what each one needs.
# Each method has its own address and its own parameters at CallMeBot, so the
# required settings depend on the `channel`: WhatsApp and Signal need a phone
# number and an API key, Facebook Messenger needs only an API key (it has no
# phone number).
_CALLMEBOT_CHANNEL_FIELDS = {
    "whatsapp": ("phone", "api_key"),
    "signal": ("phone", "api_key"),
    "facebook": ("api_key",),
}
_CALLMEBOT_CHANNELS = tuple(_CALLMEBOT_CHANNEL_FIELDS)


# --------------------------------------------------------------------------------------
def _validate_callmebot_channel(index: int, channel: dict) -> None:
    """`channel` is required, and has to be one of the methods CallMeBot
    offers. There is no default, so the person chooses how they are reached.
    The settings that method needs are required too."""
    if "channel" not in channel:
        raise ConfigError(
            f"alerts.push_alerts[{index}] (type 'callmebot') is missing "
            f"required field(s): channel — set it to one of: "
            f"{', '.join(_CALLMEBOT_CHANNELS)}"
        )
    value = channel["channel"]
    if not isinstance(value, str) or value not in _CALLMEBOT_CHANNEL_FIELDS:
        raise ConfigError(
            f"alerts.push_alerts[{index}] (type 'callmebot') has an invalid "
            f"channel {value!r} — expected one of: "
            f"{', '.join(_CALLMEBOT_CHANNELS)}"
        )
    missing = [
        f for f in _CALLMEBOT_CHANNEL_FIELDS[value] if not channel.get(f)
    ]
    if missing:
        raise ConfigError(
            f"alerts.push_alerts[{index}] (type 'callmebot', channel "
            f"{value!r}) is missing required field(s): "
            f"{', '.join(missing)} — set them in alerts.yaml"
        )
    if value in ("whatsapp", "signal"):
        _check_callmebot_phone(index, channel["phone"], value == "signal")


# What a CallMeBot push alert may use to say who is reached. A phone number is
# a plus sign, the country code, and the number, as digits only: international
# numbers have at most 15 digits, and the 7 is the shortest real one. Spaces,
# dashes, and brackets are not accepted, as for the other values that are
# checked at startup. Signal also takes a UUID in place of a phone number. The
# check never repeats the value, because it is someone's phone number.
_CALLMEBOT_PHONE = re.compile(r"\+[0-9]{7,15}")
_CALLMEBOT_SIGNAL_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)


# --------------------------------------------------------------------------------------
def _check_callmebot_phone(index: int, phone, signal: bool) -> None:
    """Raise unless the `phone` of a CallMeBot push alert is a phone number
    (or, for Signal, a Signal UUID)."""
    label = f"alerts.push_alerts[{index}] (type 'callmebot')"
    if not isinstance(phone, str):
        raise ConfigError(
            f"{label} has a phone that is not text — put it in quotes, for "
            f"example \"+15551234567\""
        )
    if _CALLMEBOT_PHONE.fullmatch(phone):
        return
    if signal and _CALLMEBOT_SIGNAL_UUID.fullmatch(phone):
        return
    allowed = (
        "a phone number with a plus sign, the country code, and digits only "
        "(7 to 15 digits), such as \"+15551234567\""
    )
    if signal:
        allowed += ", or a Signal UUID"
    raise ConfigError(f"{label} has a phone that is not valid — use {allowed}")


# The priorities ntfy accepts by name, one for each of its numbers 1 to 5
# (ntfy also takes "urgent" for "max"; it is left out so each priority has one
# name). The numbers are not accepted: YAML reads them as integers, and a
# header value has to be text.
_NTFY_PRIORITIES = ("min", "low", "default", "high", "max")


# --------------------------------------------------------------------------------------
def _validate_ntfy_priority(index: int, channel: dict) -> None:
    """`priority` is optional ("default" when left out), but when it is written
    it has to be one of the names ntfy knows."""
    if "priority" not in channel:
        return
    value = channel["priority"]
    if value not in _NTFY_PRIORITIES:
        raise ConfigError(
            f"alerts.push_alerts[{index}] (type 'ntfy') has an invalid "
            f"priority {value!r} — expected one of: "
            f"{', '.join(_NTFY_PRIORITIES)}"
        )


# --------------------------------------------------------------------------------------
def _validate_push_alert_url(index: int, ctype: str, channel: dict) -> None:
    """The url of a push alert, for the types that have one, follows the same
    rule as the url of an http monitor. The error says which part is wrong and
    never repeats the url, since a webhook url carries a secret token."""
    if "url" not in _REQUIRED_CHANNEL_FIELDS[ctype]:
        return
    url = channel["url"]
    problem = (
        "it must start with http:// or https://"
        if not isinstance(url, str) else _url_problem(url)
    )
    if problem:
        raise ConfigError(
            f"alerts.push_alerts[{index}] (type {ctype!r}) has a url that is "
            f"not valid — {problem}"
        )


# The values a push alert uses to reach its service. Their formats are checked
# so that a typo, a stray space, or a line break is reported at startup and not
# as an alert that never arrives. None of these errors repeat the value, because
# it is a token, a password, or someone's account.
_TELEGRAM_TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")
_TELEGRAM_CHAT_ID_RE = re.compile(r"-?[0-9]+|@[A-Za-z0-9_]{5,32}")


# --------------------------------------------------------------------------------------
def _check_secret_text(label: str, name: str, value, *, spaces: bool) -> None:
    """Raise unless `value` is text with no control characters (and, unless
    `spaces`, no whitespace at all), at most 255 characters."""
    if not isinstance(value, str):
        raise ConfigError(
            f"{label} has a {name} that is not text — put it in quotes"
        )
    if _has_control_character(value) or (
        not spaces and any(c.isspace() for c in value)
    ):
        raise ConfigError(
            f"{label} has a {name} with "
            f"{'a control character' if spaces else 'a space or a control character'}"
            f" in it, which is not valid"
        )
    if len(value) > 255:
        raise ConfigError(
            f"{label} has a {name} that is too long (the longest allowed is "
            f"255 characters)"
        )


# --------------------------------------------------------------------------------------
def _check_telegram_credentials(label: str, channel: dict) -> None:
    token = channel["bot_token"]
    if not isinstance(token, str) or not _TELEGRAM_TOKEN_RE.fullmatch(token):
        raise ConfigError(
            f"{label} has a bot_token that is not valid — it looks like "
            f"123456789:ABCdef... (digits, a colon, then letters, digits, "
            f"hyphens or underscores)"
        )
    chat = channel["chat_id"]
    if isinstance(chat, bool) or not (
        isinstance(chat, int)
        or (isinstance(chat, str) and _TELEGRAM_CHAT_ID_RE.fullmatch(chat))
    ):
        raise ConfigError(
            f"{label} has a chat_id that is not valid — use the chat's "
            f"number (a group's is negative), or @name for a channel"
        )


# --------------------------------------------------------------------------------------
def _check_ntfy_credentials(label: str, channel: dict) -> None:
    for name in ("username", "password"):
        if name in channel:
            _check_secret_text(label, name, channel[name], spaces=True)
    if "token" in channel:
        _check_secret_text(label, "token", channel["token"], spaces=False)


# --------------------------------------------------------------------------------------
def _check_callmebot_api_key(label: str, key) -> None:
    """The key may be written as a number, since that is what it looks like."""
    if isinstance(key, bool) or not isinstance(key, (str, int)):
        raise ConfigError(
            f"{label} has an api_key that is not text — put it in quotes"
        )
    text = str(key)
    if not text or any(c.isspace() for c in text) or _has_control_character(text):
        raise ConfigError(
            f"{label} has an api_key with a space or a control character "
            f"in it, which is not valid"
        )
    if len(text) > 128:
        raise ConfigError(
            f"{label} has an api_key that is too long (the longest "
            f"allowed is 128 characters)"
        )


# --------------------------------------------------------------------------------------
def _validate_push_alert_credentials(index: int, ctype: str, channel: dict) -> None:
    """The token, chat, credential, and key settings of a push alert."""
    label = f"alerts.push_alerts[{index}] (type {ctype!r})"
    if ctype == "telegram":
        _check_telegram_credentials(label, channel)
    elif ctype == "ntfy":
        _check_ntfy_credentials(label, channel)
    elif ctype == "callmebot" and "api_key" in channel:
        _check_callmebot_api_key(label, channel["api_key"])


# --------------------------------------------------------------------------------------
def _validate_push_alert_channels(alerts_cfg: dict) -> None:
    """Validate each configured push-alert channel's type and fields."""
    channels = alerts_cfg.get("push_alerts", [])
    if not isinstance(channels, list):
        raise ConfigError(
            f"'alerts.push_alerts' must be a list of channels, got "
            f"{type(channels).__name__} — check alerts.yaml"
        )
    for i, channel in enumerate(channels):
        if not isinstance(channel, dict):
            raise ConfigError(
                f"alerts.push_alerts[{i}] must be a mapping of fields, got "
                f"{type(channel).__name__} — check alerts.yaml"
            )
        _validate_system_flag(channel, f"alerts.push_alerts[{i}]")
        _validate_enabled_flag(channel, f"alerts.push_alerts[{i}]")
        # `type` is required. Without it, a mistyped key (`typ: slack`) would
        # quietly turn a Slack entry into a generic JSON webhook.
        if "type" not in channel:
            raise ConfigError(
                f"alerts.push_alerts[{i}] is missing the required 'type' — "
                f"expected one of: {', '.join(sorted(_REQUIRED_CHANNEL_FIELDS))}"
            )
        ctype = channel["type"]
        if not isinstance(ctype, str) or ctype not in _REQUIRED_CHANNEL_FIELDS:
            raise ConfigError(
                f"alerts.push_alerts[{i}] has unknown type {ctype!r} — "
                f"expected one of: "
                f"{', '.join(sorted(_REQUIRED_CHANNEL_FIELDS))}"
            )
        missing = [
            f for f in _REQUIRED_CHANNEL_FIELDS[ctype] if not channel.get(f)
        ]
        if missing:
            raise ConfigError(
                f"alerts.push_alerts[{i}] (type {ctype!r}) is missing "
                f"required field(s): {', '.join(missing)} — set them in "
                f"alerts.yaml"
            )
        _validate_push_alert_url(i, ctype, channel)
        if ctype == "ntfy":
            _validate_ntfy_priority(i, channel)
        if ctype == "callmebot":
            _validate_callmebot_channel(i, channel)
        _validate_push_alert_credentials(i, ctype, channel)


# "verify" and "normal" are the documented values. main.py only tests
# `resume_mode != "verify"`, so any other value would behave like "normal".
_VALID_RESUME_MODES = ("verify", "normal")


# --------------------------------------------------------------------------------------
def _validate_maintenance_windows(windows: list) -> None:
    """Validate maintenance windows at load rather than at evaluation.

    maintenance.py's _check_window_active() catches the exceptions a malformed
    window raises and logs a warning, resolving it to "not in maintenance" --
    so a typo'd window silently lets alerts fire straight through the outage
    window the operator configured, with only a log line to say so. A
    malformed window is therefore an error at load, like every other config
    section.
    """
    seen_names: set[str] = set()
    for i, window in enumerate(windows):
        _check_window_name(i, window)
        label = window["name"]
        for field in ("start_day", "end_day", "start_time", "end_time"):
            if field not in window:
                raise ConfigError(
                    f"Maintenance window '{label}' is missing required "
                    f"field '{field}' — check maintenance.yaml"
                )
        _check_window_times(label, window)
        _check_window_days(label, window)
        resume = window.get("resume", "verify")
        if resume not in _VALID_RESUME_MODES:
            raise ConfigError(
                f"Maintenance window '{label}' has an invalid resume mode "
                f"{resume!r} — expected one of: "
                f"{', '.join(_VALID_RESUME_MODES)}"
            )
        _validate_window_day_pair(label, window)
        _check_window_name_is_unique(window, seen_names)


# --------------------------------------------------------------------------------------
def _check_window_name(index: int, window: dict) -> None:
    """A window has a name, and it is text that is not blank. (A numeric name
    such as 5 would otherwise reach code that joins names as text.)"""
    if "name" not in window:
        raise ConfigError(
            f"maintenance[{index}] is missing the required 'name' — check "
            f"maintenance.yaml"
        )
    name = window["name"]
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(
            f"maintenance[{index}] has a name that is blank or is not text "
            f"({name!r}) — a numeric-looking name needs quoting"
        )
    # The same rules as a monitor's name (see _validate_monitor_name): the name
    # is shown on the dashboard and written to the check log.
    if name != name.strip():
        raise ConfigError(
            f"maintenance[{index}] has the name {name!r}, which starts or ends "
            f"with a space — remove it"
        )
    if _has_control_character(name):
        raise ConfigError(
            f"maintenance[{index}] has the name {name!r}, which contains a "
            f"control character, such as a line break or a tab"
        )
    if len(name) > MAX_MONITOR_NAME_LENGTH:
        raise ConfigError(
            f"maintenance[{index}] has a name of {len(name)} characters, but "
            f"the longest allowed is {MAX_MONITOR_NAME_LENGTH}"
        )


# --------------------------------------------------------------------------------------
def _check_window_name_is_unique(window: dict, seen_names: set) -> None:
    """The resume setting of a window is found by its name when a monitor
    leaves it, so two windows cannot share one."""
    name = window["name"]
    if name.casefold() in seen_names:
        raise ConfigError(
            f"Two maintenance windows are named '{name}' — give each window "
            f"its own name"
        )
    seen_names.add(name.casefold())


# --------------------------------------------------------------------------------------
def _check_window_times(label: str, window: dict) -> None:
    for field in ("start_time", "end_time"):
        value = window[field]
        if not isinstance(value, str) or not _TIME_RE.match(value):
            raise ConfigError(
                f"Maintenance window '{label}' has an invalid {field} "
                f"{value!r} — expected \"HH:MM\" (24-hour, quoted)"
            )


# --------------------------------------------------------------------------------------
def _check_window_days(label: str, window: dict) -> None:
    """start_day and end_day are each a weekday, an ordinal weekday made of a
    real weekday, or a date. end_day can also be "following <weekday>"."""
    for field in ("start_day", "end_day"):
        value = window[field]
        if not isinstance(value, str):
            raise ConfigError(
                f"Maintenance window '{label}' has a non-text {field} "
                f"{value!r} — a bare date needs quoting "
                f"({field}: \"2026-07-15\")"
            )
        following = FOLLOWING_PATTERN_RE.match(value)
        if following:
            _check_following_day(label, field, value, following.group(1))
            continue
        monthly = MONTHLY_PATTERN_RE.match(value)
        if not (
            value.lower() in _WEEKDAY_NAMES or monthly or _ISO_DATE_RE.match(value)
        ):
            raise ConfigError(
                f"Maintenance window '{label}' has an unrecognized "
                f"{field} {value!r} — expected a weekday name "
                f"(\"sunday\"), an ordinal weekday (\"2nd tuesday\", "
                f"\"last friday\"), or a date (\"2026-07-15\"). end_day can "
                f"also be \"following monday\"."
            )
        if monthly and monthly.group(2).lower() not in _WEEKDAY_NAMES:
            raise ConfigError(
                f"Maintenance window '{label}' has an unrecognized "
                f"weekday {monthly.group(2)!r} in {field} {value!r} — "
                f"expected a weekday name such as \"tuesday\""
            )


# --------------------------------------------------------------------------------------
def following_days_ahead(start_weekday: int, end_weekday: int) -> int:
    """Days from a start on one weekday to the first `end_weekday` after it,
    1 to 7 (the same weekday means a week later). Weekdays are 0=Monday."""
    return (end_weekday - start_weekday) % 7 or 7


# --------------------------------------------------------------------------------------
def _check_weekly_following_length(window: dict) -> None:
    """A weekly window repeats every week, so one that lasts a week or longer
    would be active all the time."""
    def minutes(hhmm: str) -> int:
        hour, minute = hhmm.split(":")
        return int(hour) * 60 + int(minute)

    start_wd = WEEKDAYS[window["start_day"].lower()]
    end_wd = WEEKDAYS[window["end_day"].split()[-1].lower()]
    lasts = (
        following_days_ahead(start_wd, end_wd) * 1440
        + minutes(window["end_time"]) - minutes(window["start_time"])
    )
    if lasts >= 7 * 1440:
        raise ConfigError(
            f"Maintenance window '{window['name']}' runs from "
            f"{window['start_day']!r} {window['start_time']} to "
            f"{window['end_day']!r} {window['end_time']}, which is a week or "
            f"longer, so a weekly window would always be active"
        )


# --------------------------------------------------------------------------------------
def _check_following_day(label: str, field: str, value: str, weekday: str) -> None:
    """"following Wednesday" is only for end_day, and needs a real weekday."""
    if field != "end_day":
        raise ConfigError(
            f"Maintenance window '{label}' has {field} {value!r}, but "
            f"\"following\" only works in end_day — it means the first such "
            f"weekday after start_day"
        )
    if weekday.lower() not in _WEEKDAY_NAMES:
        raise ConfigError(
            f"Maintenance window '{label}' has an unrecognized weekday "
            f"{weekday!r} in {field} {value!r} — expected a weekday name such "
            f"as \"following wednesday\""
        )


# --------------------------------------------------------------------------------------
def _day_kind(value: str) -> str:
    """Classify a start_day/end_day as 'weekday', 'monthly', 'following' or
    'date'."""
    if value.lower() in _WEEKDAY_NAMES:
        return "weekday"
    if MONTHLY_PATTERN_RE.match(value):
        return "monthly"
    if FOLLOWING_PATTERN_RE.match(value):
        return "following"
    return "date"


# --------------------------------------------------------------------------------------
def _parse_window_date(label: str, field: str, value: str) -> datetime:
    """A start_day or end_day date that has the YYYY-MM-DD shape is a real date."""
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ConfigError(
            f"Maintenance window '{label}' has a {field} of {value!r}, "
            f"which matches the YYYY-MM-DD shape but is not a real "
            f"date"
        ) from None


# --------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=256)
def _monthly_end_order(start_day: str, end_day: str) -> frozenset:
    """Whether the end of a monthly window falls before its start, over every
    month of a full 400-year calendar cycle. The result holds True when it does
    in some month and False when it does not in some other."""
    # Imported here: maintenance.py imports this module.
    from src.maintenance import _nth_weekday_date, _parse_monthly_pattern

    start_ord, start_wd = _parse_monthly_pattern(start_day)
    end_ord, end_wd = _parse_monthly_pattern(end_day)
    found = set()
    for year in range(2000, 2400):
        for month in range(1, 13):
            start = _nth_weekday_date(year, month, start_ord, start_wd)
            end = _nth_weekday_date(year, month, end_ord, end_wd)
            found.add(end < start)
        if len(found) == 2:
            break
    return frozenset(found)


# --------------------------------------------------------------------------------------
def _check_monthly_pair(label: str, window: dict) -> None:
    """A monthly window whose end is before its start in some months and not in
    others would be read as ending in the next month in the first kind of month,
    so a one-night window would run for about four weeks."""
    start_day, end_day = window["start_day"], window["end_day"]
    if len(_monthly_end_order(" ".join(start_day.lower().split()),
                              " ".join(end_day.lower().split()))) == 2:
        raise ConfigError(
            f"Maintenance window '{label}' runs from {start_day!r} to "
            f"{end_day!r}, and in some months the end comes before the start, "
            f"so the window would run into the next month. Use the same day at "
            f"both ends, or write end_day as \"following <weekday>\" (for "
            f"example \"following wednesday\")"
        )


# --------------------------------------------------------------------------------------
def _validate_window_day_pair(label: str, window: dict) -> None:
    """Reject day pairs that load fine but can never activate, or that would
    be active for far longer than intended.

    The field-level checks above accept each day independently, which
    leaves combinations that pass validation and then misbehave:

      * Mismatched kinds ("Monday" -> "2nd Tuesday"). _window_active_now()
        only has branches for weekday/weekday, monthly/monthly and
        date/date, so a mixed pair falls through to the ISO-date branch
        and strptime() raises on the weekday name. Caught, logged once,
        never active. ("following <weekday>" works with every start.)
      * A date that passes the YYYY-MM-DD shape check but isn't a real
        day ("2026-02-30"). Same outcome, one exception later.
      * A one-off window whose end is at or before its start. Nothing
        raises here at all -- the comparison is simply never true, so the
        window is inert with no log line whatsoever.
      * A monthly window whose end is before its start in some months only.
    """
    start_day, end_day = window["start_day"], window["end_day"]
    start_kind, end_kind = _day_kind(start_day), _day_kind(end_day)
    if end_kind == "following":
        # The end is always after the start, by definition.
        if start_kind == "date":
            _parse_window_date(label, "start_day", start_day)
        elif start_kind == "weekday":
            _check_weekly_following_length(window)
        return
    if start_kind != end_kind:
        raise ConfigError(
            f"Maintenance window '{label}' mixes day types: start_day "
            f"{start_day!r} is a {start_kind} but end_day {end_day!r} is a "
            f"{end_kind}. Both must be the same kind — two weekday names, "
            f"two ordinal weekdays, or two dates — or end_day can be "
            f"\"following <weekday>\"."
        )
    if start_kind == "date":
        _check_date_pair(label, window)
    else:
        _check_recurring_pair(label, window, start_kind)


# --------------------------------------------------------------------------------------
def _check_recurring_pair(label: str, window: dict, kind: str) -> None:
    """A weekly or monthly pair of days of the same kind."""
    start_day, end_day = window["start_day"], window["end_day"]
    # The same day at both ends has to run forwards, as it does for dates.
    # Otherwise a weekly window that ends earlier in the day than it starts
    # would run for almost a whole week, and one with equal times, or a
    # monthly one that ends no later than it starts, would never be active.
    same = " ".join(start_day.lower().split()) == " ".join(end_day.lower().split())
    if not same:
        if kind == "monthly":
            _check_monthly_pair(label, window)
        return
    if window["end_time"] <= window["start_time"]:
        raise ConfigError(
            f"Maintenance window '{label}' starts and ends on "
            f"{start_day} but end_time {window['end_time']!r} is not after "
            f"start_time {window['start_time']!r} — a window that starts "
            f"and ends on the same day must end later in the day"
        )


# --------------------------------------------------------------------------------------
def _check_date_pair(label: str, window: dict) -> None:
    """A one-off window: both days are real dates, and it ends after it starts."""
    start_day, end_day = window["start_day"], window["end_day"]
    start = _parse_window_date(label, "start_day", start_day)
    end = _parse_window_date(label, "end_day", end_day)
    if end < start:
        raise ConfigError(
            f"Maintenance window '{label}' ends before it starts "
            f"({start_day} → {end_day}), so it can never be active"
        )
    if end == start and window["end_time"] <= window["start_time"]:
        # Same calendar day is fine, but the times have to run forwards.
        # "HH:MM" strings compare correctly as text, and both were
        # shape-checked by _TIME_RE above.
        raise ConfigError(
            f"Maintenance window '{label}' starts and ends on "
            f"{start_day} but end_time {window['end_time']!r} is not after "
            f"start_time {window['start_time']!r}, so it can never be active"
        )


# --------------------------------------------------------------------------------------
def _validate_timezone(tz_name) -> None:
    """Reject a timezone the display layer can't use.

    get_display_tz() falls back to UTC with a warning, which is the right
    behaviour at render time -- but a config-level typo should be a startup
    error naming the field, not a silent 5-hour shift in every timestamp
    the dashboard shows. A non-string (an unquoted `timezone: 5`) or an
    empty string makes ZoneInfo() raise TypeError/ValueError instead of a
    lookup error, so those are rejected here too.
    """
    if not isinstance(tz_name, str) or not tz_name.strip():
        raise ConfigError(
            f"'global.timezone' must be a non-empty IANA timezone name, got "
            f"{tz_name!r} (e.g. \"UTC\", \"America/Chicago\")"
        )
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        raise ConfigError(
            f"'global.timezone' is {tz_name!r}, which this system's timezone "
            f"database doesn't recognise — expected an IANA name such as "
            f"\"UTC\" or \"Europe/Berlin\""
        ) from None


# --------------------------------------------------------------------------------------
def load_config(path: str = "config.yaml") -> dict:
    """Load and validate config from multiple files.

    Reads config.yaml (required) plus optional alerts.yaml, monitors.yaml,
    tags.yaml, and maintenance.yaml from the same directory.
    Assembles them into a single dict with the same shape the app expects.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {path}")
    base_dir = config_path.parent

    # Load config.yaml (global + dashboard)
    raw_config = _load_yaml_file(config_path, {})

    # Load optional files from same directory
    raw_alerts = _load_yaml_file(base_dir / "alerts.yaml", {})
    raw_monitors = _load_yaml_file(base_dir / "monitors.yaml", {})
    raw_tags = _load_yaml_file(base_dir / "tags.yaml", {})
    raw_maintenance = _load_yaml_file(base_dir / "maintenance.yaml", {})

    # Merge global defaults with raw config
    config = _deep_merge(DEFAULTS, raw_config)

    _reject_unknown_settings(
        raw_config, raw_alerts, raw_monitors, raw_tags, raw_maintenance
    )

    # Assemble sections from their respective files
    # Each file wraps its contents under a single top-level key matching
    # the section name.
    config["alerts"] = _deep_merge(DEFAULTS["alerts"], _alerts_section(raw_alerts))
    config["monitors"] = _section_or_default(raw_monitors, "monitors", [])
    config["tags"] = _section_or_default(raw_tags, "tags", [])
    config["maintenance"] = _section_or_default(raw_maintenance, "maintenance", [])

    # Env var substitution across all loaded config
    config = _substitute_env_vars(config)

    # credentials.ini is read after substitution and kept out of
    # config["monitors"], so a password is never part of a monitor's own
    # settings. It is read as written: no ${...} expansion.
    config["credentials"] = _load_credentials_file(base_dir / CREDENTIALS_FILE)

    # ${ENV_VAR} substitution always yields a string, even for fields that
    # must be numeric (check_interval_minutes, retention_days, etc.) — coerce and
    # validate here so a bad value fails loudly at startup instead of
    # crashing the monitoring loop with a silent TypeError at runtime.
    _validate_section_shapes(config)
    _validate_numeric_fields(config)
    _validate_boolean_fields(config)
    _validate_sidebar_uptime_period(config)
    _validate_language(config)
    _validate_contact_email(config)
    _validate_retention_rules(config)
    _validate_decimal_fields(config)

    _validate_monitors_config(config["monitors"], config["global"]["timeout"])
    for monitor in config["monitors"]:
        _validate_monitor_credentials(monitor, config["credentials"])
    _warn_unused_credentials(config)

    # Tags are not case sensitive: lowercase them all, then validate against
    # tags.yaml
    _normalize_tag_case(config)
    # An entry that is not text (a list or a mapping cannot go in a set) is
    # reported by _validate_tag_references, as "not text".
    allowed_tags = {t for t in config.get("tags", []) if isinstance(t, str)}
    _validate_tag_references(config, allowed_tags)

    _apply_smtp_credentials_from_environment(config["alerts"].setdefault("email", {}))
    _validate_email_alert_config(config["alerts"]["email"])
    _validate_push_alert_channels(config["alerts"])
    _validate_maintenance_windows(config["maintenance"])
    _validate_timezone(config["global"].get("timezone", "UTC"))
    _require_alert_and_window_targets(config)

    return config


THEME_DEFAULTS = {
    "backgrounds": {
        "page": "#161616",
        "header": "#161616",
        "list_row": "#0d0d0d",
        "list_row_hover": "#212621",
        "main": "#212621",
        "cards": "#0d0d0d",
        "input": "#161616",
        "footer": "#0d0d0d",
    },
    "text": {
        "main": "#ffffff",
        "muted": "#999999",
        "accent": "#00dd00",
    },
    "links": {
        "color": "#00dd00",
        "hover_line": "dotted",
    },
    "borders": {
        "main": "#070707",
        "accent": "#00dd00",
        "table": "#161616",
        # No separate `footer` border color: the sidebar footer's border reuses
        # `main`.
        "dropdown": "#212621",
    },
    "status": {
        "up": "#00dd00",
        "down": "#990000",
        "mixed": "#ffbf00",
        "nodata": "#313331",
    },
    "graphs": {
        "lines": "#161616",
    },
}


# Theme values are interpolated into an inline <style> block in the
# templates via Jinja, which HTML-entity-escapes them (autoescape is on
# for .html templates) — but this validation is a second, independent
# layer, not a substitute for that escaping. It restricts values to a
# narrow known-safe character set (digits/commas/percent for rgb(),
# letters-only for named colors, hex digits for hex colors) so a
# malicious or malformed theme.yaml value can't break out of the
# <style> block even if it somehow bypassed template escaping.
# The theme docs (https://uptimeherald.org/docs/theme/) and theme.default.yaml
# document hex, rgb()/rgba(), and named colors as supported, so all three are
# accepted here.
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3}$|^#[0-9a-fA-F]{6}$|^#[0-9a-fA-F]{8}$")
_RGB_COLOR_RE = re.compile(
    r"^rgba?\(\s*\d{1,3}%?\s*,\s*\d{1,3}%?\s*,\s*\d{1,3}%?\s*"
    r"(,\s*(0|1|0?\.\d+)\s*)?\)$"
)
# The color names CSS defines (CSS Color Module Level 4), plus transparent and
# currentcolor. A word that is not one of them is rejected: a typo such as
# "gren" would stay in the theme, the browser would ignore it, and that part of
# the dashboard would silently lose its color.
_CSS_COLOR_NAMES = frozenset("""
    aliceblue antiquewhite aqua aquamarine azure beige bisque black
    blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse
    chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
    darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta
    darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
    darkslateblue darkslategray darkslategrey darkturquoise darkviolet
    deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite
    forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green
    greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender
    lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
    lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon
    lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue
    lightyellow lime limegreen linen magenta maroon mediumaquamarine
    mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue
    mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
    mistyrose moccasin navajowhite navy oldlace olive olivedrab orange
    orangered orchid palegoldenrod palegreen paleturquoise palevioletred
    papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red
    rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna
    silver skyblue slateblue slategray slategrey snow springgreen steelblue
    tan teal thistle tomato turquoise violet wheat white whitesmoke yellow
    yellowgreen transparent currentcolor
""".split())
_VALID_BORDER_STYLES = {
    "none", "solid", "dotted", "dashed", "double",
    "groove", "ridge", "inset", "outset",
}


# --------------------------------------------------------------------------------------
def _is_safe_css_color(value) -> bool:
    if not isinstance(value, str):
        return False
    return bool(
        _HEX_COLOR_RE.match(value)
        or _RGB_COLOR_RE.match(value)
        or value.lower() in _CSS_COLOR_NAMES
    )


# --------------------------------------------------------------------------------------
def _validate_theme_value(section: str, key: str, value, defaults: dict):
    """Return `value` if it's safe to interpolate into CSS, else the default."""
    label = f"{section}.{key}"
    if section == "links" and key == "hover_line":
        if isinstance(value, str) and value.lower() in _VALID_BORDER_STYLES:
            return value
        logger.warning(
            "Theme value '%s' = %r is not a recognized border style, "
            "using default", label, value,
        )
        return defaults[section][key]

    if _is_safe_css_color(value):
        return value
    logger.warning(
        "Theme value '%s' = %r is not a valid hex/rgb()/named color, "
        "using default", label, value,
    )
    return defaults[section][key]


# --------------------------------------------------------------------------------------
def _sanitize_theme(merged: dict, defaults: dict) -> dict:
    """Walk a merged theme dict and replace any unsafe value with its default."""
    result: dict = {}
    for section, fields in defaults.items():
        result[section] = {}
        merged_section = merged.get(section, {}) if isinstance(merged, dict) else {}
        if not isinstance(merged_section, dict):
            # "backgrounds: red" -- the section has to be a mapping of settings.
            logger.warning(
                "Theme section '%s' must be a mapping of settings, got %s, "
                "using defaults", section, type(merged_section).__name__,
            )
            merged_section = {}
        for key in fields:
            value = merged_section.get(key, fields[key])
            result[section][key] = _validate_theme_value(section, key, value, defaults)
    return result


# Unknown theme names already warned about. The theme is read on every
# dashboard request, so without this the same warning would repeat endlessly.
_THEME_UNKNOWN_WARNED: set[tuple] = set()


# --------------------------------------------------------------------------------------
def _unknown_theme_names(raw: dict) -> list:
    """(section, None) for a section theme.yaml does not have, and
    (section, name) for a setting a section does not have."""
    unknown: list = []
    for section, fields in raw.items():
        if section not in THEME_DEFAULTS:
            unknown.append((section, None))
        elif isinstance(fields, dict):
            unknown.extend(
                (section, key) for key in fields
                if key not in THEME_DEFAULTS[section]
            )
    return unknown


# --------------------------------------------------------------------------------------
def _warn_unknown_theme_settings(raw: dict) -> None:
    """Log a warning, once, for each section or setting name that theme.yaml
    does not have. It is only a warning, not an error as in the other files,
    because a bad value in the theme falls back to its default and a typo
    here should not stop monitoring."""
    for section, key in _unknown_theme_names(raw):
        if (section, key) in _THEME_UNKNOWN_WARNED:
            continue
        _THEME_UNKNOWN_WARNED.add((section, key))
        if key is None:
            logger.warning(
                "theme.yaml has an unknown section '%s', which is ignored "
                "(the sections are: %s)", section, ", ".join(THEME_DEFAULTS),
            )
        else:
            logger.warning(
                "theme.yaml has an unknown setting '%s' under '%s:', "
                "which is ignored", key, section,
            )


# --------------------------------------------------------------------------------------
def load_theme(path: str = "theme.yaml") -> dict:
    """Load theme.yaml, merge with defaults, return theme dict.

    If the file is missing or invalid, returns the hardcoded defaults
    and logs a warning. Every value is validated as a safe CSS color (or
    border-style keyword) before use — see _sanitize_theme.
    """
    theme_path = Path(path)
    if not theme_path.exists():
        logger.warning("Theme file not found: %s, using defaults", path)
        return copy.deepcopy(THEME_DEFAULTS)

    try:
        with open(theme_path) as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML in theme file %s: %s, using defaults", path, e)
        return copy.deepcopy(THEME_DEFAULTS)
    except OSError as e:
        # "Missing or invalid" has to include unreadable, not just
        # unparseable -- theme.yaml existing as a directory (Docker
        # auto-creating an absent bind-mount source) raises
        # IsADirectoryError here. This function is called from
        # _render_config_error(), i.e. the handler for a config that has
        # already failed, so letting anything escape here 500s the very
        # page meant to explain the problem.
        logger.warning("Could not read theme file %s: %s, using defaults", path, e)
        return copy.deepcopy(THEME_DEFAULTS)

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        # Valid YAML, wrong shape (a bare list or scalar). _deep_merge
        # below would raise AttributeError on it, contradicting this
        # function's documented "returns the defaults on an invalid file".
        logger.warning(
            "Theme file %s must be a mapping at its top level, got %s — "
            "using defaults", path, type(raw).__name__,
        )
        return copy.deepcopy(THEME_DEFAULTS)

    _warn_unknown_theme_settings(raw)
    merged = _deep_merge(THEME_DEFAULTS, raw)
    return _sanitize_theme(merged, THEME_DEFAULTS)


# --------------------------------------------------------------------------------------
def get_display_tz(config: dict) -> ZoneInfo:
    """Resolve the display timezone from config.

    Returns a ZoneInfo for the configured timezone, falling back to UTC
    if the name is invalid or missing.
    """
    tz_name = config.get("global", {}).get("timezone", "UTC")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError):
        # TypeError/ValueError as well as the lookup errors: ZoneInfo()
        # raises TypeError for a non-string (an unquoted `timezone: 5`) and
        # ValueError for an empty string. load_config() rejects both at
        # load, but this is also called from routes that may hold a config
        # this function didn't validate, and a display-timezone problem must
        # never be what turns a dashboard render into a 500.
        logger.warning(
            "Timezone %r unusable, falling back to UTC", tz_name,
        )
        return ZoneInfo("UTC")
