# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
db_support.py - End-of-support notices for the database in use.

The running database's version is compared against a lifecycle table that
ships inside each release (src/db_lifecycle.json, regenerated before each
release from endoflife.date). Nothing here calls out to
the network: end-of-support dates are published years ahead, so an
install that never updates still gets warned about its own version, and
newer versions arrive with the next app update.

The schedule, relative to the version's end-of-support date:

    before:  12 months, 6 months, 3 months, 28, 21, 14, 7 and 1 day
    on the day itself
    after:   1, 7, 14, 21 and 28 days, then 3, 6 and 12 months,
             then once a year for as long as it stays in use

Only the most recently passed threshold is ever sent, never a batch -- so
an install (or an upgrade to the release that adds this) that lands
mid-schedule gets one notice, not one per threshold it has already
passed. Which thresholds have gone out is remembered in the database.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

from src import db
from src.alerts import _safe_format, _t
from src.config import get_display_tz
from src.system_notice import send_system_notice

logger = logging.getLogger(__name__)

# The supported-databases documentation page. This string is printed in
# notifications users have already received, so it must not change.
DOCS_URL = "https://uptimeherald.org/docs/databases/"

LIFECYCLE_PATH = Path(__file__).parent / "db_lifecycle.json"

# When the background loop looks: shortly after startup (so a fresh install
# or a restart doesn't wait for midnight), then at midnight in the
# dashboard's configured timezone every day. The thresholds are calendar
# dates, so checking at a fixed time of day -- not "24 hours after the last
# check", which drifts and could eventually skip a whole day -- guarantees
# every date gets looked at. If a check fails (the database being
# unreachable, say), it is retried after RETRY_SECONDS, or at midnight if
# that comes sooner.
STARTUP_DELAY_SECONDS = 30
RETRY_SECONDS = 60 * 60
# Lands the check just after midnight rather than a hair before it.
MIDNIGHT_SLACK_SECONDS = 30

# (key, months, days) offsets from the end-of-support date. Negative is
# before it. Yearly notices after the first year are generated, not listed.
_FIXED_THRESHOLDS = [
    ("pre-12m", -12, 0),
    ("pre-6m", -6, 0),
    ("pre-3m", -3, 0),
    ("pre-28d", 0, -28),
    ("pre-21d", 0, -21),
    ("pre-14d", 0, -14),
    ("pre-7d", 0, -7),
    ("pre-1d", 0, -1),
    ("eol", 0, 0),
    ("post-1d", 0, 1),
    ("post-7d", 0, 7),
    ("post-14d", 0, 14),
    ("post-21d", 0, 21),
    ("post-28d", 0, 28),
    ("post-3m", 3, 0),
    ("post-6m", 6, 0),
    ("post-1y", 12, 0),
]


# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DbVersion:
    engine: str   # "mariadb" | "postgresql"
    cycle: str    # the lifecycle-table key: "11.8" for MariaDB, "18" for Postgres
    raw: str      # the server's own version string, for logging


# --------------------------------------------------------------------------------------
def parse_mariadb_version(raw: str) -> DbVersion | None:
    """Parse the string from SELECT VERSION() on a MariaDB server.

    MariaDB reports e.g. "12.3.3-MariaDB-ubu2404"; older 10.x servers
    prefix a replication-compatibility "5.5.5-" ("5.5.5-10.11.19-MariaDB").
    Anything that isn't MariaDB returns None -- it isn't in the table.
    """
    if "mariadb" not in raw.lower():
        return None
    cleaned = raw[len("5.5.5-"):] if raw.startswith("5.5.5-") else raw
    m = re.match(r"(\d+)\.(\d+)\.\d+", cleaned)
    if not m:
        return None
    return DbVersion("mariadb", f"{m.group(1)}.{m.group(2)}", raw)


# --------------------------------------------------------------------------------------
def parse_postgres_version(version_num: str) -> DbVersion | None:
    """Parse PostgreSQL's server_version_num ("180006" -> major 18)."""
    try:
        num = int(version_num)
    except (TypeError, ValueError):
        return None
    if num < 100000:  # pre-10 numbering isn't in the table or supported
        return None
    return DbVersion("postgresql", str(num // 10000), str(version_num))


# --------------------------------------------------------------------------------------
def local_today(config: dict, now: datetime | None = None) -> date:
    """Today's date in the dashboard's configured timezone (UTC if unset or
    unusable), so thresholds flip at the same midnight the user sees."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(get_display_tz(config)).date()


# --------------------------------------------------------------------------------------
def seconds_until_next_midnight(tz: tzinfo, now: datetime | None = None) -> float:
    """Seconds until the next midnight in `tz`, plus a small slack.

    Both instants are converted to UTC before subtracting: subtracting two
    datetimes that share a tzinfo does naive wall-clock arithmetic and would
    be an hour wrong across a daylight-saving change.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    tomorrow = now.date() + timedelta(days=1)
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz)
    delta = midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return delta.total_seconds() + MIDNIGHT_SLACK_SECONDS


# --------------------------------------------------------------------------------------
def seconds_until_next_check(
    tz: tzinfo, last_check_ok: bool, now: datetime | None = None,
) -> float:
    """How long to wait before the next pass."""
    until_midnight = seconds_until_next_midnight(tz, now)
    if last_check_ok:
        return until_midnight
    return min(RETRY_SECONDS, until_midnight)


# --------------------------------------------------------------------------------------
def load_lifecycle(path: Path = LIFECYCLE_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------------------
def _add_months(d: date, months: int) -> date:
    """Calendar-month arithmetic, clamping the day (31 Jan + 1 month ->
    28/29 Feb) so the result is always a real date."""
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    # Last day of the target month.
    if month == 12:
        last = 31
    else:
        last = (date(year, month + 1, 1) - date(year, month, 1)).days
    return date(year, month, min(d.day, last))


# --------------------------------------------------------------------------------------
def _threshold_date(eol: date, months: int, days: int) -> date:
    return _add_months(eol, months) + timedelta(days=days)


# --------------------------------------------------------------------------------------
def due_threshold(eol: date, today: date) -> tuple[str, date] | None:
    """The most recently passed threshold as (key, its date), or None if
    the first one (12 months before) hasn't been reached yet."""
    passed: list[tuple[date, str]] = []
    for key, months, days in _FIXED_THRESHOLDS:
        when = _threshold_date(eol, months, days)
        if when <= today:
            passed.append((when, key))
    # Yearly after the first year: 2y, 3y, ...
    year = 2
    while _add_months(eol, 12 * year) <= today and year < 500:
        passed.append((_add_months(eol, 12 * year), f"post-{year}y"))
        year += 1
    if not passed:
        return None
    when, key = max(passed)
    return key, when


# --------------------------------------------------------------------------------------
def build_message(
    version: DbVersion, label: str, eol: date, today: date, config: dict,
) -> tuple[str, str]:
    """(title, body) for the notice, in the configured language."""
    t = _t(config)
    name = f"{label} {version.cycle}"
    days = (eol - today).days
    title = _safe_format(t.gettext("{name}: upstream end of support"), name=name)
    if days > 1:
        body = _safe_format(
            t.gettext(
                "{name} reaches upstream end of support in {days} days ({date})."
            ),
            name=name, days=days, date=eol.isoformat(),
        )
    elif days == 1:
        body = _safe_format(
            t.gettext("{name} reaches upstream end of support tomorrow ({date})."),
            name=name, date=eol.isoformat(),
        )
    elif days == 0:
        body = _safe_format(
            t.gettext("{name} reaches upstream end of support today."), name=name,
        )
    else:
        body = _safe_format(
            t.gettext(
                "{name} reached upstream end of support on {date} ({days} days "
                "ago) and no longer receives upstream security fixes."
            ),
            name=name, date=eol.isoformat(), days=-days,
        )
    body += "\n" + _safe_format(
        t.gettext("See {url} for supported versions and how to upgrade."),
        url=DOCS_URL,
    )
    return title, body


# --------------------------------------------------------------------------------------
def parse_server_version(raw: str) -> DbVersion | None:
    """Map whatever get_server_version() returned to a table key.

    The two backends return different shapes -- MariaDB a dotted string
    that says "MariaDB", Postgres a bare number -- so the shape picks the
    parser, rather than asking the (aliased-away) dispatcher which backend
    is active. Anything else, such as a MySQL server, matches neither.
    """
    return parse_mariadb_version(raw) or parse_postgres_version(raw)


# Versions already reported as "not in the table", so the repeated checks
# doesn't repeat the same log line forever.
_logged_unknown: set[str] = set()


# --------------------------------------------------------------------------------------
async def check_and_notify(config: dict, today: date | None = None) -> str | None:
    """Run one pass. Returns the key of the notice sent, or None.

    Always logs the outcome of a send. A version the table doesn't know
    (a fork, a release newer than this build) is logged once and never
    notified about.
    """
    today = today or local_today(config)
    raw = await db.get_server_version()
    version = parse_server_version(raw)
    if version is None:
        if raw not in _logged_unknown:
            _logged_unknown.add(raw)
            logger.info(
                "Database version %r isn't one this release tracks "
                "end-of-support dates for; no notices will be sent for it",
                raw,
            )
        return None

    lifecycle = load_lifecycle()
    engine = lifecycle["engines"][version.engine]
    entry = engine["cycles"].get(version.cycle)
    if entry is None:
        if version.raw not in _logged_unknown:
            _logged_unknown.add(version.raw)
            logger.info(
                "%s %s isn't in this release's end-of-support table "
                "(newer than the build, or not a tracked release); no "
                "notices will be sent for it",
                engine["label"], version.cycle,
            )
        return None

    eol = date.fromisoformat(entry["eol"])
    due = due_threshold(eol, today)
    if due is None:
        return None
    threshold_key, _when = due
    notice_key = f"db-eol:{version.engine}:{version.cycle}:{threshold_key}"
    if await db.has_system_notice(notice_key):
        return None

    title, body = build_message(version, engine["label"], eol, today, config)
    # Logged whether or not any channel is configured: a user with no
    # notification channel still gets this in the application log.
    logger.warning("%s -- %s", title, body.replace("\n", " "))
    sent = await send_system_notice(config, title, body)
    # Recorded even when nothing was delivered (no channels, or every send
    # failed): the schedule is thresholds, not retries, and re-trying
    # daily against a dead webhook would just be noise.
    await db.record_system_notice(notice_key)
    logger.info("End-of-support notice %s sent to %d channel(s)", notice_key, sent)
    return notice_key
