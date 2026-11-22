# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_db_support.py - Tests for the database end-of-support notices.

Tests for the database end-of-support notices (src/db_support.py and
src/system_notice.py).
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from src import db_support
from src.config import ConfigError, _validate_push_alert_channels
from src.db_support import (
    DOCS_URL,
    MIDNIGHT_SLACK_SECONDS,
    RETRY_SECONDS,
    STARTUP_DELAY_SECONDS,
    _add_months,
    build_message,
    check_and_notify,
    due_threshold,
    load_lifecycle,
    local_today,
    parse_mariadb_version,
    parse_postgres_version,
    parse_server_version,
    seconds_until_next_check,
    seconds_until_next_midnight,
)
from src.system_notice import send_system_notice, system_targets

CONFIG = {"global": {"language": "en"}}


# --- Version parsing ------------------------------------------------------------------

@pytest.mark.parametrize("raw, cycle", [
    ("12.3.3-MariaDB-ubu2404", "12.3"),
    ("11.8.9-MariaDB-log", "11.8"),
    ("11.4.13-MariaDB", "11.4"),
    # Older servers prefix a replication-compatibility "5.5.5-".
    ("5.5.5-10.11.19-MariaDB-ubu2204", "10.11"),
    ("13.0.2-MariaDB", "13.0"),
])
def test_parse_mariadb_version(raw, cycle):
    v = parse_mariadb_version(raw)
    assert v is not None
    assert (v.engine, v.cycle) == ("mariadb", cycle)


@pytest.mark.parametrize("raw", ["8.4.3", "8.0.39-0ubuntu0.22.04.1", "", "garbage"])
def test_parse_mariadb_version_rejects_non_mariadb(raw):
    """A MySQL server (or anything else) is not in the table."""
    assert parse_mariadb_version(raw) is None


@pytest.mark.parametrize("num, major", [
    ("180006", "18"), ("170011", "17"), ("150019", "15"), ("100023", "10"),
])
def test_parse_postgres_version(num, major):
    v = parse_postgres_version(num)
    assert v is not None
    assert (v.engine, v.cycle) == ("postgresql", major)


@pytest.mark.parametrize("raw", ["90624", "abc", "", None])
def test_parse_postgres_version_rejects(raw):
    assert parse_postgres_version(raw) is None


def test_parse_server_version_picks_parser_by_shape():
    assert parse_server_version("180006").engine == "postgresql"
    assert parse_server_version("12.3.3-MariaDB").engine == "mariadb"
    assert parse_server_version("8.4.3") is None


# --- Date arithmetic ------------------------------------------------------------------

def test_add_months_clamps_to_month_end():
    assert _add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert _add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)  # leap year


def test_add_months_crosses_years_in_both_directions():
    assert _add_months(date(2028, 11, 9), 3) == date(2029, 2, 9)
    assert _add_months(date(2028, 11, 9), -12) == date(2027, 11, 9)
    assert _add_months(date(2028, 2, 15), -3) == date(2027, 11, 15)


# --- The schedule ---------------------------------------------------------------------

EOL = date(2028, 11, 9)


def _key(today):
    due = due_threshold(EOL, today)
    return due[0] if due else None


def test_nothing_due_until_twelve_months_before():
    assert _key(date(2027, 11, 8)) is None
    assert _key(date(2027, 11, 9)) == "pre-12m"


@pytest.mark.parametrize("days_before, expected", [
    (28, "pre-28d"), (21, "pre-21d"), (14, "pre-14d"),
    (7, "pre-7d"), (1, "pre-1d"),
])
def test_weekly_countdown_before_end_of_support(days_before, expected):
    assert _key(EOL - timedelta(days=days_before)) == expected


def test_only_the_most_recent_threshold_is_due():
    """20 days out has already passed 12m, 6m, 3m, 28d and 21d: a late
    install gets the 21-day notice only, not a batch."""
    assert _key(EOL - timedelta(days=20)) == "pre-21d"
    # 85 days out is past the 3-month mark (2028-08-09) but not the 28-day one.
    assert _key(EOL - timedelta(days=85)) == "pre-3m"
    # 100 days out is before the 3-month mark, so the 6-month one is latest.
    assert _key(EOL - timedelta(days=100)) == "pre-6m"


def test_day_of_and_after_end_of_support():
    assert _key(EOL) == "eol"
    assert _key(EOL + timedelta(days=1)) == "post-1d"
    assert _key(EOL + timedelta(days=7)) == "post-7d"
    assert _key(EOL + timedelta(days=14)) == "post-14d"
    assert _key(EOL + timedelta(days=21)) == "post-21d"
    assert _key(EOL + timedelta(days=28)) == "post-28d"
    assert _key(EOL + timedelta(days=60)) == "post-28d"
    assert _key(date(2029, 2, 9)) == "post-3m"
    assert _key(date(2029, 5, 9)) == "post-6m"
    assert _key(date(2029, 11, 9)) == "post-1y"


def test_yearly_after_the_first_year():
    assert _key(date(2030, 11, 8)) == "post-1y"
    assert _key(date(2030, 11, 9)) == "post-2y"
    assert _key(date(2031, 11, 9)) == "post-3y"
    assert _key(date(2040, 6, 1)) == "post-11y"


def test_full_schedule_is_in_order_and_has_no_gaps():
    """Walking forward a day at a time must produce each threshold exactly
    once, in the documented order."""
    expected = [
        "pre-12m", "pre-6m", "pre-3m", "pre-28d", "pre-21d", "pre-14d",
        "pre-7d", "pre-1d", "eol", "post-1d", "post-7d", "post-14d",
        "post-21d", "post-28d", "post-3m", "post-6m", "post-1y", "post-2y",
    ]
    seen = []
    day = EOL - timedelta(days=400)
    end = date(2030, 12, 31)
    while day <= end:
        k = _key(day)
        if k and (not seen or seen[-1] != k):
            seen.append(k)
        day += timedelta(days=1)
    assert seen == expected


# --- The shipped lifecycle table ------------------------------------------------------

def test_lifecycle_table_has_the_supported_versions():
    table = load_lifecycle()
    maria = table["engines"]["mariadb"]["cycles"]
    for cycle in ("10.11", "11.4", "11.8", "12.3"):
        assert maria[cycle]["lts"] is True
        date.fromisoformat(maria[cycle]["eol"])
    pg = table["engines"]["postgresql"]["cycles"]
    for major in ("15", "16", "17", "18", "19"):
        date.fromisoformat(pg[major]["eol"])


# --- Message text ---------------------------------------------------------------------

def _msg(today, eol=EOL):
    v = parse_postgres_version("160015")
    return build_message(v, "PostgreSQL", eol, today, CONFIG)


def test_message_before_end_of_support():
    title, body = _msg(EOL - timedelta(days=90))
    assert title == "PostgreSQL 16: upstream end of support"
    assert "reaches upstream end of support in 90 days (2028-11-09)" in body
    assert DOCS_URL in body


def test_message_day_before_day_of_and_after():
    assert "upstream end of support tomorrow (2028-11-09)" in _msg(EOL - timedelta(days=1))[1]
    assert "reaches upstream end of support today" in _msg(EOL)[1]
    after = _msg(EOL + timedelta(days=30))[1]
    assert "reached upstream end of support on 2028-11-09 (30 days ago)" in after
    assert "no longer receives upstream security fixes" in after
    assert DOCS_URL in after


# --- check_and_notify -----------------------------------------------------------------

@pytest.fixture
def fake_db(monkeypatch):
    """In-memory stand-ins for the three DB calls, plus a send recorder."""
    state = {"sent_keys": set(), "version": "160015", "sends": []}

    async def get_version():
        return state["version"]

    async def has_notice(key):
        return key in state["sent_keys"]

    async def record_notice(key):
        state["sent_keys"].add(key)

    async def send(config, title, body):
        state["sends"].append((title, body))
        return 1

    monkeypatch.setattr(db_support.db, "get_server_version", get_version)
    monkeypatch.setattr(db_support.db, "has_system_notice", has_notice)
    monkeypatch.setattr(db_support.db, "record_system_notice", record_notice)
    monkeypatch.setattr(db_support, "send_system_notice", send)
    db_support._logged_unknown.clear()
    return state


@pytest.mark.asyncio
async def test_check_sends_once_per_threshold(fake_db):
    pg16_eol = date.fromisoformat(
        load_lifecycle()["engines"]["postgresql"]["cycles"]["16"]["eol"]
    )
    today = pg16_eol - timedelta(days=7)
    first = await check_and_notify(CONFIG, today=today)
    assert first == "db-eol:postgresql:16:pre-7d"
    assert len(fake_db["sends"]) == 1
    # Same day again, e.g. after a restart: nothing more is sent.
    assert await check_and_notify(CONFIG, today=today) is None
    assert len(fake_db["sends"]) == 1
    # The next threshold is a new notice.
    assert await check_and_notify(
        CONFIG, today=pg16_eol - timedelta(days=1)
    ) == "db-eol:postgresql:16:pre-1d"
    assert len(fake_db["sends"]) == 2


@pytest.mark.asyncio
async def test_check_sends_nothing_when_end_of_support_is_far_off(fake_db):
    assert await check_and_notify(CONFIG, today=date(2026, 10, 4)) is None
    assert fake_db["sends"] == []


@pytest.mark.asyncio
async def test_check_logs_even_with_no_channels(fake_db, caplog):
    """The log line is unconditional; delivery is separate."""
    pg16_eol = date.fromisoformat(
        load_lifecycle()["engines"]["postgresql"]["cycles"]["16"]["eol"]
    )
    with caplog.at_level(logging.WARNING, logger="src.db_support"):
        await check_and_notify(CONFIG, today=pg16_eol)
    assert any("end of support" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_check_records_notice_even_if_nothing_was_delivered(
    fake_db, monkeypatch,
):
    """Thresholds, not retries: a dead webhook doesn't cause daily resends."""
    async def send_none(config, title, body):
        return 0
    monkeypatch.setattr(db_support, "send_system_notice", send_none)
    pg16_eol = date.fromisoformat(
        load_lifecycle()["engines"]["postgresql"]["cycles"]["16"]["eol"]
    )
    key = await check_and_notify(CONFIG, today=pg16_eol)
    assert key in fake_db["sent_keys"]


@pytest.mark.asyncio
async def test_unknown_version_is_logged_once_and_never_notified(
    fake_db, caplog,
):
    fake_db["version"] = "8.4.3"  # a MySQL server
    with caplog.at_level(logging.INFO, logger="src.db_support"):
        for _ in range(3):
            assert await check_and_notify(CONFIG, today=date(2040, 1, 1)) is None
    assert fake_db["sends"] == []
    msgs = [r.getMessage() for r in caplog.records if "8.4.3" in r.getMessage()]
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_version_missing_from_table_is_not_notified(fake_db):
    fake_db["version"] = "990000"  # a Postgres "99" this build has never heard of
    assert await check_and_notify(CONFIG, today=date(2040, 1, 1)) is None
    assert fake_db["sends"] == []


@pytest.mark.asyncio
async def test_mariadb_version_uses_series_key(fake_db):
    fake_db["version"] = "5.5.5-10.11.19-MariaDB-ubu2204"
    eol = date.fromisoformat(
        load_lifecycle()["engines"]["mariadb"]["cycles"]["10.11"]["eol"]
    )
    key = await check_and_notify(CONFIG, today=eol)
    assert key == "db-eol:mariadb:10.11:eol"


# --- Which channels get a system notice -----------------------------------------------

def _cfg(**alerts):
    return {"alerts": alerts}


def test_targets_skip_channels_that_say_system_false():
    cfg = _cfg(push_alerts=[
        {"type": "ntfy", "url": "http://a", "system": True},
        {"type": "slack", "url": "http://b", "system": False},
        {"type": "discord", "url": "http://c"},
    ])
    targets = system_targets(cfg)
    assert [t[1]["url"] for t in targets] == ["http://a", "http://c"]


def _email(*recipients):
    return {"from": "a@b.c", "smtp_host": "h", "smtp_port": 587,
            "recipients": list(recipients)}


def test_targets_empty_when_every_channel_says_system_false():
    cfg = _cfg(
        email=_email({"to": ["d@e.f"], "system": False}),
        push_alerts=[{"type": "ntfy", "url": "http://a", "system": False}],
    )
    assert system_targets(cfg) == []


def test_each_email_recipients_entry_is_its_own_target():
    cfg = _cfg(email=_email(
        {"to": ["a@x.y"]},
        {"to": ["b@x.y", "c@x.y"], "system": False},
        {"to": ["d@x.y"], "system": True},
        {"to": ["e@x.y"], "enabled": False},
    ))
    targets = system_targets(cfg)
    assert [(k, c["to"]) for k, c in targets] == [
        ("email", ["a@x.y"]), ("email", ["d@x.y"]),
    ]
    assert targets[0][1]["smtp_host"] == "h" and targets[0][1]["from"] == "a@b.c"


def test_targets_are_every_enabled_channel_by_default():
    cfg = _cfg(
        email=_email({"to": ["d@e.f"]}),
        push_alerts=[
            {"type": "ntfy", "url": "http://a"},
            {"type": "slack", "url": "http://b", "enabled": False},
        ],
    )
    kinds = [(k, c.get("url")) for k, c in system_targets(cfg)]
    assert kinds == [("email", None), ("push", "http://a")]


def test_targets_empty_when_nothing_configured():
    assert system_targets({}) == []
    assert system_targets(_cfg(email={"recipients": []}, push_alerts=[])) == []


def test_system_flag_on_a_disabled_channel_does_not_count():
    cfg = _cfg(push_alerts=[
        {"type": "ntfy", "url": "http://a", "system": True, "enabled": False},
        {"type": "slack", "url": "http://b"},
    ])
    assert [c["url"] for _, c in system_targets(cfg)] == ["http://b"]


def test_system_flag_must_be_a_bool():
    with pytest.raises(ConfigError, match="system must be true or false"):
        _validate_push_alert_channels(
            {"push_alerts": [{"type": "ntfy", "url": "http://a", "system": "no"}]}
        )
    _validate_push_alert_channels(
        {"push_alerts": [{"type": "ntfy", "url": "http://a", "system": True}]}
    )


# --- Sending --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_goes_to_every_target_and_counts_successes():
    cfg = _cfg(push_alerts=[
        {"type": "slack", "url": "http://slack.example/hook"},
        {"type": "discord", "url": "http://discord.example/hook"},
    ])
    with patch("src.system_notice.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200
        sent = await send_system_notice(cfg, "T", "B")
    assert sent == 2
    payloads = [c[1]["json"] for c in mock_post.call_args_list]
    assert {"text": "*T*\nB"} in payloads
    assert {"content": "**T**\nB"} in payloads


@pytest.mark.asyncio
async def test_email_subject_uses_the_notice_prefix_not_the_alert_prefix():
    cfg = _cfg(email={
        "smtp_host": "smtp.example.com", "smtp_port": 587,
        "from": "a@example.com", "subject_prefix": "[ALERT]",
        "recipients": [{"to": ["b@example.com"]}, {"to": ["c@example.com"]}],
    })
    with patch("src.system_notice.aiosmtplib.send") as mock_send:
        sent = await send_system_notice(cfg, "PostgreSQL 15: upstream end of support", "B")
    assert sent == 2
    messages = [c[0][0] for c in mock_send.call_args_list]
    assert [m["To"] for m in messages] == ["b@example.com", "c@example.com"]
    assert messages[0]["Subject"] == (
        "[Uptime Herald Notice] PostgreSQL 15: upstream end of support"
    )


@pytest.mark.asyncio
async def test_one_failing_channel_does_not_stop_the_others():
    cfg = _cfg(push_alerts=[
        {"type": "slack", "url": "http://bad.example/hook"},
        {"type": "mattermost", "url": "http://good.example/hook"},
    ])

    async def fake_post(self, url, **kwargs):
        class R:
            status_code = 500 if "bad" in url else 200
            text = "x"
        return R()

    with patch("src.system_notice.httpx.AsyncClient.post", new=fake_post):
        assert await send_system_notice(cfg, "T", "B") == 1


@pytest.mark.asyncio
async def test_send_ntfy_uses_title_header_and_plain_body():
    cfg = _cfg(push_alerts=[
        {"type": "ntfy", "url": "http://ntfy.example/t", "priority": "high"},
    ])
    with patch("src.system_notice.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200
        await send_system_notice(cfg, "The title", "The body")
    kwargs = mock_post.call_args[1]
    assert kwargs["headers"]["Title"] == "The title"
    assert kwargs["headers"]["Priority"] == "high"
    assert kwargs["content"] == b"The body"


@pytest.mark.asyncio
async def test_send_generic_payload_shape():
    cfg = _cfg(push_alerts=[{"type": "generic", "url": "http://x.example/h"}])
    with patch("src.system_notice.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200
        await send_system_notice(cfg, "T", "B")
    payload = mock_post.call_args[1]["json"]
    assert payload["type"] == "system_notice"
    assert payload["title"] == "T" and payload["message"] == "B"


@pytest.mark.asyncio
async def test_send_with_no_targets_returns_zero():
    assert await send_system_notice({}, "T", "B") == 0


# --- When the check runs --------------------------------------------------------------

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")


def test_midnight_is_in_the_given_timezone():
    # 19:30 UTC on 2026-10-05 is 14:30 CDT: 9.5 hours to local midnight.
    now = datetime(2026, 10, 5, 19, 30, tzinfo=UTC)
    assert seconds_until_next_midnight(CHICAGO, now) == (
        9.5 * 3600 + MIDNIGHT_SLACK_SECONDS
    )
    assert seconds_until_next_midnight(UTC, now) == (
        4.5 * 3600 + MIDNIGHT_SLACK_SECONDS
    )


def test_midnight_just_before_midnight_waits_for_the_next_one_only_briefly():
    now = datetime(2026, 10, 5, 23, 59, 30, tzinfo=UTC)
    assert seconds_until_next_midnight(UTC, now) == 30 + MIDNIGHT_SLACK_SECONDS


def test_midnight_is_correct_across_a_daylight_saving_change():
    """US clocks fall back on 2026-11-01: that local day has 25 hours, so
    from 00:30 CDT the next midnight is 24.5 hours of real time away. A
    naive wall-clock subtraction would say 23.5."""
    now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 00:30 CDT
    assert seconds_until_next_midnight(CHICAGO, now) == (
        24.5 * 3600 + MIDNIGHT_SLACK_SECONDS
    )


def test_failed_check_retries_within_the_hour_or_at_midnight_if_sooner():
    early = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)    # 21h to midnight
    late = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)   # 30 min to midnight
    assert seconds_until_next_check(UTC, False, early) == RETRY_SECONDS
    assert seconds_until_next_check(UTC, False, late) == 1800 + MIDNIGHT_SLACK_SECONDS
    # A successful check always waits for midnight.
    assert seconds_until_next_check(UTC, True, early) == 21 * 3600 + MIDNIGHT_SLACK_SECONDS


def test_today_follows_the_configured_display_timezone():
    now = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)  # still the 4th in Chicago
    assert local_today({"global": {"timezone": "America/Chicago"}}, now) == date(2026, 10, 4)
    assert local_today({"global": {"timezone": "UTC"}}, now) == date(2026, 10, 5)
    # Unset or unusable falls back to UTC rather than failing.
    assert local_today({}, now) == date(2026, 10, 5)
    assert local_today({"global": {"timezone": "Not/AZone"}}, now) == date(2026, 10, 5)


@pytest.mark.asyncio
async def test_loop_checks_after_startup_then_retries_a_failure_then_waits_for_midnight(
    monkeypatch,
):
    """Startup delay -> failing check -> short retry -> good check -> wait
    for midnight, in that order."""
    from src import main

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    calls = []

    async def fake_check(config):
        calls.append(config)
        if len(calls) == 1:
            raise RuntimeError("database unreachable")

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main, "load_config", lambda path: {"global": {}})
    monkeypatch.setattr(main.db_support, "check_and_notify", fake_check)
    monkeypatch.setattr(
        main.db_support, "seconds_until_next_check",
        lambda tz, ok: 111 if ok else 222,
    )
    with pytest.raises(asyncio.CancelledError):
        await main._db_support_notice_loop()

    assert sleeps == [STARTUP_DELAY_SECONDS, 222, 111]
    assert len(calls) == 2
