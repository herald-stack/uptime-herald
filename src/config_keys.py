# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
config_keys.py - Which setting names each configuration file accepts.

A name that is not on these lists is a startup error. Without the check, a typo
such as `monitor:` in monitors.yaml or `chek_interval_minutes` on a monitor would
load and do nothing, so the setting the person meant to make would silently be
missing. Each error names the file and the place, and says either where the
name belongs ("'timeout' belongs in ...") or what it is probably a typo of
("did you mean ...?").

theme.yaml is not checked here: its design is that a bad value falls back to
its default, so an unknown name there is only logged (see load_theme).
credentials.ini checks its own names.

The lists have to match the code that reads each setting. A test compares the
global list with DEFAULTS, so a new global setting cannot be left off it.
"""

import difflib

# The one top-level key each file's contents sit under.
TOP_LEVEL = {
    "config.yaml": ("global",),
    "monitors.yaml": ("monitors",),
    "tags.yaml": ("tags",),
    "alerts.yaml": ("alerts",),
    "maintenance.yaml": ("maintenance",),
}

GLOBAL_KEYS = (
    "check_interval_minutes", "timeout", "stagger_seconds", "stats_period_days",
    "retention_days", "sidebar_uptime_period", "alert_cooldown_minutes",
    "alert_retries", "retry_interval_minutes", "retry_on_up",
    "pin_down_monitors", "cleanup_removed_monitors", "cleanup_grace_minutes",
    "ui_refresh_minutes", "latency_graph_days", "checks_per_page",
    "kiosk_refresh_minutes", "view_all_checks", "timezone", "language",
    "uptime_decimals", "latency_decimals", "contact_email",
)

MONITOR_COMMON_KEYS = (
    "name", "type", "active", "tags", "check_interval_minutes", "timeout",
    "retention_days", "alert_cooldown_minutes", "alert_retries",
    "retry_interval_minutes", "retry_on_up",
)
MONITOR_TYPE_KEYS = {
    "ping": ("host", "count"),
    "tcp": ("host", "port"),
    "http": ("url", "verify_ssl", "expected_status", "credentials"),
}

ALERTS_KEYS = ("email", "push_alerts")
EMAIL_KEYS = (
    "smtp_host", "smtp_port", "from", "from_name", "subject_prefix",
    "recipients",
)
RECIPIENT_KEYS = ("to", "monitors", "tags", "enabled", "system")

PUSH_COMMON_KEYS = ("type", "enabled", "monitors", "tags", "system")
PUSH_TYPE_KEYS = {
    "discord": ("url",),
    "mattermost": ("url",),
    "rocket.chat": ("url",),
    "slack": ("url",),
    "generic": ("url",),
    "telegram": ("bot_token", "chat_id"),
    "ntfy": ("url", "username", "password", "token", "priority"),
    "callmebot": ("channel", "phone", "api_key"),
}
# A CallMeBot alert takes only the settings its delivery method uses.
CALLMEBOT_CHANNEL_KEYS = {
    "whatsapp": ("channel", "phone", "api_key"),
    "signal": ("channel", "phone", "api_key"),
    "facebook": ("channel", "api_key"),
}

WINDOW_KEYS = (
    "name", "start_day", "end_day", "start_time", "end_time", "monitors",
    "tags", "resume",
)

# Names that are no longer settings, with what to do instead.
_RENAMED = {
    "interval_minutes": "'check_interval_minutes'",
    "apikey": "'api_key'",
    "webhooks": "'push_alerts'",
    "smtp_user": "SMTP_USER in your .env file",
    "smtp_password": "SMTP_PASSWORD in your .env file",
}

# Where each setting name is valid, to tell someone whose setting is in the
# wrong place where it belongs.
_HOMES: dict[str, list[str]] = {}


# --------------------------------------------------------------------------------------
def _home(names, where: str) -> None:
    for name in names:
        homes = _HOMES.setdefault(name, [])
        if where not in homes:
            homes.append(where)


_home(GLOBAL_KEYS, "config.yaml under global:")
_home(MONITOR_COMMON_KEYS, "a monitor in monitors.yaml")
for _keys in MONITOR_TYPE_KEYS.values():
    _home(_keys, "a monitor in monitors.yaml")
_home(ALERTS_KEYS, "alerts.yaml under alerts:")
_home(EMAIL_KEYS, "the email: settings in alerts.yaml")
_home(RECIPIENT_KEYS, "an entry under recipients: in alerts.yaml")
_home(PUSH_COMMON_KEYS, "a push alert in alerts.yaml")
for _keys in PUSH_TYPE_KEYS.values():
    _home(_keys, "a push alert in alerts.yaml")
_home(WINDOW_KEYS, "a window in maintenance.yaml")


# --------------------------------------------------------------------------------------
def _hint(name: str, valid_here, *, top_level_of=None) -> str:
    """What to tell someone who wrote `name` where it is not accepted."""
    if name in _RENAMED:
        return f" — use {_RENAMED[name]} instead"
    if top_level_of is not None:
        for other_file, keys in TOP_LEVEL.items():
            if name in keys and other_file != top_level_of:
                return f" — it belongs in {other_file}"
    homes = _HOMES.get(name)
    if homes:
        return " — it belongs in " + " or ".join(homes)
    close = difflib.get_close_matches(str(name), list(valid_here), n=1, cutoff=0.6)
    if close:
        return f" — did you mean '{close[0]}'?"
    return ""


# --------------------------------------------------------------------------------------
def _check_keys(entry, valid, where: str, found: list) -> None:
    """Add a message to `found` for each key of `entry` not in `valid`."""
    if not isinstance(entry, dict):
        return
    for key in entry:
        if key not in valid:
            found.append(f"{where}: unknown setting '{key}'{_hint(key, valid)}")


# --------------------------------------------------------------------------------------
def _label(entry, index: int, what: str) -> str:
    name = entry.get("name") if isinstance(entry, dict) else None
    if isinstance(name, str) and name:
        return f"{what} '{name}'"
    return f"{what} {index + 1}"


# --------------------------------------------------------------------------------------
def _check_monitor(monitor, index: int, found: list) -> None:
    if not isinstance(monitor, dict):
        return
    where = f"monitors.yaml, {_label(monitor, index, 'monitor')}"
    monitor_type = monitor.get("type")
    everything = set(MONITOR_COMMON_KEYS)
    for keys in MONITOR_TYPE_KEYS.values():
        everything.update(keys)
    if isinstance(monitor_type, str) and monitor_type in MONITOR_TYPE_KEYS:
        valid = set(MONITOR_COMMON_KEYS) | set(MONITOR_TYPE_KEYS[monitor_type])
    else:
        valid = everything  # a bad type is reported on its own
    for key in monitor:
        if key in valid:
            continue
        if key in everything:
            types = [t for t, keys in MONITOR_TYPE_KEYS.items() if key in keys]
            found.append(
                f"{where}: '{key}' only applies to {' and '.join(types)} "
                f"monitors, not '{monitor_type}'"
            )
        else:
            found.append(f"{where}: unknown setting '{key}'{_hint(key, valid)}")


# --------------------------------------------------------------------------------------
def _push_valid_keys(alert: dict):
    """(keys this push alert may have, keys valid for some other type)."""
    everything = set(PUSH_COMMON_KEYS)
    for keys in PUSH_TYPE_KEYS.values():
        everything.update(keys)
    alert_type = alert.get("type")
    if not (isinstance(alert_type, str) and alert_type in PUSH_TYPE_KEYS):
        return everything, everything  # a bad type is reported on its own
    if alert_type == "callmebot":
        channel = alert.get("channel")
        if isinstance(channel, str) and channel in CALLMEBOT_CHANNEL_KEYS:
            valid = set(PUSH_COMMON_KEYS) | set(CALLMEBOT_CHANNEL_KEYS[channel])
            return valid, everything
    return set(PUSH_COMMON_KEYS) | set(PUSH_TYPE_KEYS[alert_type]), everything


# --------------------------------------------------------------------------------------
def _misplaced_push_setting(where: str, alert: dict, key: str) -> str:
    """The message for a setting that is valid on some push alert, but not on
    this one."""
    channel = alert.get("channel")
    if (
        alert.get("type") == "callmebot"
        and isinstance(channel, str)
        and channel in CALLMEBOT_CHANNEL_KEYS
    ):
        uses = [c for c, keys in CALLMEBOT_CHANNEL_KEYS.items() if key in keys]
        return (
            f"{where}: '{key}' does not apply to the channel '{channel}' — it "
            f"is used by {' and '.join(repr(c) for c in uses) or 'other types'}"
        )
    uses = [t for t, keys in PUSH_TYPE_KEYS.items() if key in keys]
    return (
        f"{where}: '{key}' does not apply to this type — it is used by "
        f"{', '.join(repr(t) for t in uses)}"
    )


# --------------------------------------------------------------------------------------
def _check_push_alert(alert, index: int, found: list) -> None:
    if not isinstance(alert, dict):
        return
    alert_type = alert.get("type")
    where = f"alerts.yaml, push alert {index + 1}"
    if isinstance(alert_type, str):
        where += f" (type '{alert_type}')"
    valid, everything = _push_valid_keys(alert)
    for key in alert:
        if key in valid:
            continue
        if key in everything:
            found.append(_misplaced_push_setting(where, alert, key))
        else:
            found.append(f"{where}: unknown setting '{key}'{_hint(key, valid)}")


# --------------------------------------------------------------------------------------
def _check_top_level(raws: dict, found: list) -> None:
    for file_name, raw in raws.items():
        if not isinstance(raw, dict):
            continue
        valid = TOP_LEVEL[file_name]
        for key in raw:
            if key not in valid:
                found.append(
                    f"{file_name}: unknown top-level setting '{key}'"
                    f"{_hint(key, valid, top_level_of=file_name)}"
                )


# --------------------------------------------------------------------------------------
def _section(raw, key: str):
    """raw[key], or None when raw is not a mapping."""
    return raw.get(key) if isinstance(raw, dict) else None


# --------------------------------------------------------------------------------------
def _check_alerts(raw_alerts, found: list) -> None:
    alerts = _section(raw_alerts, "alerts")
    if not isinstance(alerts, dict):
        return
    _check_keys(alerts, ALERTS_KEYS, "alerts.yaml, alerts settings", found)
    email = alerts.get("email")
    _check_keys(email, EMAIL_KEYS, "alerts.yaml, email settings", found)
    recipients = _section(email, "recipients")
    if not isinstance(recipients, list):
        recipients = []
    for index, recipient in enumerate(recipients):
        _check_keys(
            recipient, RECIPIENT_KEYS,
            f"alerts.yaml, recipients entry {index + 1}", found,
        )
    push_alerts = alerts.get("push_alerts")
    if not isinstance(push_alerts, list):
        push_alerts = []
    for index, alert in enumerate(push_alerts):
        _check_push_alert(alert, index, found)


# --------------------------------------------------------------------------------------
def find_unknown_settings(
    raw_config, raw_alerts, raw_monitors, raw_tags, raw_maintenance,
) -> list[str]:
    """Return a message for every setting name the files do not accept.

    The arguments are the loaded contents of config.yaml, alerts.yaml,
    monitors.yaml, tags.yaml and maintenance.yaml. Anything that is not the
    expected shape is skipped here, because the shape checks report it.
    """
    found: list[str] = []
    _check_top_level(
        {
            "config.yaml": raw_config, "alerts.yaml": raw_alerts,
            "monitors.yaml": raw_monitors, "tags.yaml": raw_tags,
            "maintenance.yaml": raw_maintenance,
        },
        found,
    )
    _check_keys(
        _section(raw_config, "global"), GLOBAL_KEYS,
        "config.yaml, global settings", found,
    )
    monitors = _section(raw_monitors, "monitors")
    for index, monitor in enumerate(monitors if isinstance(monitors, list) else []):
        _check_monitor(monitor, index, found)
    _check_alerts(raw_alerts, found)
    windows = _section(raw_maintenance, "maintenance")
    for index, window in enumerate(windows if isinstance(windows, list) else []):
        _check_keys(
            window, WINDOW_KEYS,
            f"maintenance.yaml, {_label(window, index, 'window')}", found,
        )
    return found
