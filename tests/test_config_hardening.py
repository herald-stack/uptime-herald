# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_config_hardening.py - Tests for settings that would otherwise load and break.

Settings that would load cleanly and then break at runtime, and names that
would be silently ignored.

Each test here loads a real set of files through load_config, because the
point is what a person who writes the value gets: an error at startup that says
what to fix.
"""

import logging
import re

import pytest
import yaml

import src.config as config_module
from src.config import ConfigError, load_config, load_theme
from src.config_keys import GLOBAL_KEYS
from src.main import _find_maintenance_resume_mode

_HTTP = {"name": "M", "type": "http", "url": "http://example.com"}
_EMAIL = {
    "from": "a@b.com", "smtp_host": "smtp.example.com", "smtp_port": 25,
    "recipients": [{"to": ["a@b.com"], "monitors": "all"}],
}


def _load(tmp_path, global_=None, monitors=None, tags=None, alerts=None,
          maintenance=None, files=None):
    """Write the files, load them, and return the loaded config."""
    def write(name, data):
        (tmp_path / name).write_text(
            data if isinstance(data, str) else yaml.dump(data)
        )

    write("config.yaml", {"global": global_ or {}})
    write("monitors.yaml", {"monitors": [_HTTP] if monitors is None else monitors})
    if tags is not None:
        write("tags.yaml", {"tags": tags})
    if alerts is not None:
        write("alerts.yaml", {"alerts": alerts})
    if maintenance is not None:
        write("maintenance.yaml", {"maintenance": maintenance})
    for name, data in (files or {}).items():
        write(name, data)
    return load_config(str(tmp_path / "config.yaml"))


def _push(**fields):
    return {"push_alerts": [{"monitors": "all", **fields}]}


# --- Numbers --------------------------------------------------------------------------

_GLOBAL_NUMBERS = [
    "check_interval_minutes", "timeout", "stagger_seconds", "stats_period_days",
    "retention_days", "alert_cooldown_minutes", "alert_retries",
    "retry_interval_minutes", "cleanup_grace_minutes", "ui_refresh_minutes",
    "latency_graph_days", "checks_per_page", "kiosk_refresh_minutes",
]


@pytest.mark.parametrize("field", _GLOBAL_NUMBERS)
@pytest.mark.parametrize("text", [".nan", ".inf", "-.inf"])
def test_a_global_number_that_is_not_finite_is_rejected(tmp_path, field, text):
    """NaN passes every minimum and maximum check, and infinity passes any
    setting without a maximum."""
    with pytest.raises(ConfigError, match=rf"'global.{field}'.*finite number"):
        _load(tmp_path, global_={field: yaml.safe_load(text)})


@pytest.mark.parametrize("field", ["check_interval_minutes", "alert_retries",
                                   "alert_cooldown_minutes", "timeout"])
def test_a_monitor_number_that_is_not_finite_is_rejected(tmp_path, field):
    with pytest.raises(ConfigError, match=rf"monitors\[M\].{field}'.*finite number"):
        _load(tmp_path, monitors=[{**_HTTP, field: float("nan")}])


@pytest.mark.parametrize("field", ["alert_retries", "checks_per_page"])
def test_a_fraction_is_rejected_where_a_whole_number_is_needed(tmp_path, field):
    """A fraction is rejected: alert_retries of 1.5 would break at the first
    outage (range(1, 2.5)), and checks_per_page of 1.5 would make the check
    log's own request fail."""
    with pytest.raises(ConfigError, match=rf"'global.{field}'.*whole number"):
        _load(tmp_path, global_={field: 1.5})


def test_a_monitor_alert_retries_fraction_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"monitors\[M\].alert_retries'.*whole number"):
        _load(tmp_path, monitors=[{**_HTTP, "alert_retries": 1.5}])


def test_a_whole_number_written_with_a_point_is_accepted_as_a_whole_number(tmp_path):
    config = _load(tmp_path, global_={"alert_retries": 3.0, "checks_per_page": "40"})
    assert config["global"]["alert_retries"] == 3
    assert isinstance(config["global"]["alert_retries"], int)
    assert config["global"]["checks_per_page"] == 40


@pytest.mark.parametrize("field,ceiling", [
    ("check_interval_minutes", 10080), ("alert_cooldown_minutes", 10080),
    ("retry_interval_minutes", 1440), ("stagger_seconds", 3600),
    ("alert_retries", 100), ("cleanup_grace_minutes", 10080),
])
def test_the_settings_that_had_no_maximum_have_one(tmp_path, field, ceiling):
    _load(tmp_path, global_={field: ceiling})  # the maximum itself is fine
    with pytest.raises(ConfigError, match=rf"'global.{field}' must be at most {ceiling}"):
        _load(tmp_path, global_={field: ceiling + 1})


@pytest.mark.parametrize("field,ceiling", [
    ("check_interval_minutes", 10080), ("alert_cooldown_minutes", 10080),
    ("retry_interval_minutes", 1440), ("alert_retries", 100),
])
def test_a_monitor_override_has_the_same_maximum(tmp_path, field, ceiling):
    with pytest.raises(ConfigError, match=rf"'monitors\[M\].{field}' must be at most"):
        _load(tmp_path, monitors=[{**_HTTP, field: ceiling + 1}])


# --- Text -----------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["language", "sidebar_uptime_period"])
def test_a_text_setting_left_empty_is_rejected(tmp_path, field):
    """`language:` with nothing after it is null, which would otherwise load
    and become nothing."""
    with pytest.raises(ConfigError, match=rf"'global.{field}' must be one of"):
        _load(tmp_path, global_={field: None})


@pytest.mark.parametrize("name", ["a\nb", "a\tb", "a\rb"])
def test_a_monitor_name_with_a_control_character_is_rejected(tmp_path, name):
    """A line break in a name breaks the subject line of every alert email."""
    with pytest.raises(ConfigError, match="contains a control character"):
        _load(tmp_path, monitors=[{"name": name, "type": "ping", "host": "example.com"}])


@pytest.mark.parametrize("name", [" M", "M ", " M "])
def test_a_monitor_name_with_a_space_at_either_end_is_rejected(tmp_path, name):
    with pytest.raises(ConfigError, match="starts or ends with a space"):
        _load(tmp_path, monitors=[{"name": name, "type": "ping", "host": "example.com"}])


@pytest.mark.parametrize("field", ["from_name", "subject_prefix"])
def test_email_header_text_with_a_line_break_is_rejected(tmp_path, field):
    with pytest.raises(ConfigError, match=rf"'alerts.email.{field}'.*control character"):
        _load(tmp_path, alerts={"email": {**_EMAIL, field: "x\nBcc: someone@example.com"}})


@pytest.mark.parametrize("field", ["from_name", "subject_prefix"])
@pytest.mark.parametrize("value,message", [
    (5, "must be text"), (["a"], "must be text"), (None, "must be text"),
])
def test_email_header_text_must_be_text(tmp_path, field, value, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, alerts={"email": {**_EMAIL, field: value}})


@pytest.mark.parametrize("field,longest", [("from_name", 255), ("subject_prefix", 50)])
def test_email_header_text_has_a_longest_length(tmp_path, field, longest):
    _load(tmp_path, alerts={"email": {**_EMAIL, field: "x" * longest}})
    with pytest.raises(ConfigError, match=f"longest allowed is {longest}"):
        _load(tmp_path, alerts={"email": {**_EMAIL, field: "x" * (longest + 1)}})


def test_email_header_text_that_is_fine_is_accepted(tmp_path):
    _load(tmp_path, alerts={"email": {
        **_EMAIL, "from_name": "Uptime Herald", "subject_prefix": "[ALERT]",
    }})
    _load(tmp_path, alerts={"email": {**_EMAIL, "subject_prefix": ""}})


# --- Push alert credentials -----------------------------------------------------------


@pytest.mark.parametrize("token", [
    "123456789:ABCdefGHIjklMNOpqrsTUVwxyz", "1:a", "12:A-b_c",
])
def test_a_telegram_bot_token_in_the_right_shape_is_accepted(tmp_path, token):
    _load(tmp_path, alerts=_push(type="telegram", bot_token=token, chat_id="1"))


@pytest.mark.parametrize("token", ["nope", "123456789", "123:", ":abc", "1:a b", "1:a\n", 5])
def test_a_telegram_bot_token_in_the_wrong_shape_is_rejected(tmp_path, token):
    with pytest.raises(ConfigError, match="bot_token that is not valid"):
        _load(tmp_path, alerts=_push(type="telegram", bot_token=token, chat_id="1"))


@pytest.mark.parametrize("chat", ["123456789", "-100123456", -100123456, 5, "@mychannel"])
def test_a_telegram_chat_id_in_the_right_shape_is_accepted(tmp_path, chat):
    _load(tmp_path, alerts=_push(type="telegram", bot_token="1:a", chat_id=chat))


@pytest.mark.parametrize("chat", ["hello world", "@ab", "1.5", ["1"], True, "12 34"])
def test_a_telegram_chat_id_in_the_wrong_shape_is_rejected(tmp_path, chat):
    with pytest.raises(ConfigError, match="chat_id that is not valid"):
        _load(tmp_path, alerts=_push(type="telegram", bot_token="1:a", chat_id=chat))


def test_ntfy_login_and_token_are_accepted(tmp_path):
    _load(tmp_path, alerts=_push(
        type="ntfy", url="https://ntfy.sh/t", username="user",
        password="a pass phrase", token="tk_abc123",
    ))


@pytest.mark.parametrize("fields,message", [
    ({"username": "a\nb", "password": "x"}, "username with a control character"),
    ({"username": "u", "password": "a\nb"}, "password with a control character"),
    ({"username": "u", "password": 123}, "password that is not text"),
    ({"token": "a\nb"}, "token with a space or a control character"),
    ({"token": "a b"}, "token with a space or a control character"),
    ({"token": ["a"]}, "token that is not text"),
    ({"token": "x" * 256}, "token that is too long"),
])
def test_bad_ntfy_login_and_token_are_rejected(tmp_path, fields, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, alerts=_push(type="ntfy", url="https://ntfy.sh/t", **fields))


def test_a_callmebot_api_key_may_be_written_as_a_number(tmp_path):
    _load(tmp_path, alerts=_push(
        type="callmebot", channel="whatsapp", phone="+15551234567",
        api_key=1234567890,
    ))


@pytest.mark.parametrize("key,message", [
    ("a b", "api_key with a space or a control character"),
    ("a\nb", "api_key with a space or a control character"),
    (["a"], "api_key that is not text"),
    (True, "api_key that is not text"),
    ("x" * 129, "api_key that is too long"),
])
def test_a_bad_callmebot_api_key_is_rejected(tmp_path, key, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, alerts=_push(
            type="callmebot", channel="whatsapp", phone="+15551234567", api_key=key,
        ))


def test_push_alert_credential_errors_never_repeat_the_value(tmp_path):
    with pytest.raises(ConfigError) as err:
        _load(tmp_path, alerts=_push(
            type="telegram", bot_token="SECRETVALUE", chat_id="1",
        ))
    assert "SECRETVALUE" not in str(err.value)
    with pytest.raises(ConfigError) as err:
        _load(tmp_path, alerts=_push(
            type="ntfy", url="https://ntfy.sh/t", token="SECRET VALUE",
        ))
    assert "SECRET" not in str(err.value)


# --- A list or a mapping where text is expected ---------------------------------------


@pytest.mark.parametrize("value", [[["a"]], [{"a": 1}], [None]])
def test_a_tag_that_is_not_text_in_tags_yaml_is_a_config_error(tmp_path, value):
    """Unchecked, these would crash the loader with a raw TypeError (unhashable)."""
    with pytest.raises(ConfigError, match="which is not text"):
        _load(tmp_path, tags=value)


@pytest.mark.parametrize("value", [[["a"]], {"a": 1}, ["a"]])
def test_a_monitor_type_that_is_not_text_is_a_config_error(tmp_path, value):
    with pytest.raises(ConfigError, match="type must be one of"):
        _load(tmp_path, monitors=[{**_HTTP, "type": value}])


@pytest.mark.parametrize("value", [[["a"]], {"a": 1}])
def test_a_push_alert_type_that_is_not_text_is_a_config_error(tmp_path, value):
    with pytest.raises(ConfigError, match="unknown type"):
        _load(tmp_path, alerts=_push(type=value, url="http://x.com"))


def test_a_push_alert_entry_that_is_not_a_mapping_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match=r"push_alerts\[0\] must be a mapping"):
        _load(tmp_path, alerts={"push_alerts": [["a"]]})


def test_alerts_that_is_not_a_mapping_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="'alerts' must be a mapping"):
        _load(tmp_path, files={"alerts.yaml": "alerts: hello\n"})


@pytest.mark.parametrize("text,heading,written", [
    ("alerts:\n", "alerts", "put push_alerts: [] under it"),
    ("alerts:\n  email:\n    # from: 'a@b.com'\n", "email", "put recipients: [] under it"),
    ("alerts:\n  push_alerts:\n    # - type: slack\n", "push_alerts", "write it as push_alerts: []"),
    ("alerts:\n  email:\n    recipients:\n      # - to: ['a@b.com']\n",
     "recipients", "write it as recipients: []"),
])
def test_a_heading_in_alerts_with_nothing_under_it_is_a_config_error(
    tmp_path, text, heading, written
):
    message = rf"'{heading}' has nothing under it.*{re.escape(written)}"
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, files={"alerts.yaml": text})


@pytest.mark.parametrize("text", [
    "alerts: {}\n",
    "alerts:\n  email: {}\n  push_alerts: []\n",
    "alerts:\n  email:\n    recipients: []\n  push_alerts: []\n",
    "",
])
def test_a_heading_in_alerts_written_as_empty_on_purpose_is_accepted(tmp_path, text):
    config = _load(tmp_path, files={"alerts.yaml": text})
    assert config["alerts"]["push_alerts"] == []


def test_email_that_is_not_a_mapping_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="'alerts.email' must be a mapping"):
        _load(tmp_path, alerts={"email": "hello"})


# --- Maintenance windows --------------------------------------------------------------


def _window(**fields):
    return {
        "name": "W", "start_day": "Sunday", "end_day": "Sunday",
        "start_time": "01:00", "end_time": "02:00", "monitors": "all", **fields,
    }


@pytest.mark.parametrize("fields", [
    {"start_time": "04:00", "end_time": "02:00"},   # a weekday window that wraps the week
    {"start_time": "04:00", "end_time": "04:00"},   # never active
    {"start_day": "2nd Tuesday", "end_day": "2nd tuesday",
     "start_time": "23:00", "end_time": "01:00"},   # a monthly window, never active
    {"start_day": "LAST   Friday", "end_day": "last friday",
     "start_time": "10:00", "end_time": "10:00"},
])
def test_a_window_that_starts_and_ends_on_one_day_must_end_later_that_day(tmp_path, fields):
    with pytest.raises(ConfigError, match="must end later in the day"):
        _load(tmp_path, maintenance=[_window(**fields)])


@pytest.mark.parametrize("fields", [
    {},  # Sunday 01:00 to 02:00
    {"start_day": "Saturday", "end_day": "Monday",
     "start_time": "23:00", "end_time": "01:00"},
    {"start_time": "23:00", "end_day": "Monday", "end_time": "02:00"},
    {"start_day": "2nd Tuesday", "end_day": "following Wednesday",
     "start_time": "23:00", "end_time": "01:00"},
])
def test_windows_that_run_forwards_are_accepted(tmp_path, fields):
    _load(tmp_path, maintenance=[_window(**fields)])


@pytest.mark.parametrize("day", ["2nd Tuesdy", "last Fryday", "1st Moonday"])
def test_a_misspelled_weekday_in_an_ordinal_is_rejected(tmp_path, day):
    """Unchecked, these would load, and the window would never become active."""
    with pytest.raises(ConfigError, match="unrecognized weekday"):
        _load(tmp_path, maintenance=[_window(
            start_day=day, end_day="2nd Wednesday",
            start_time="23:00", end_time="01:00",
        )])


@pytest.mark.parametrize("name", [5, "", "   ", None, ["a"]])
def test_a_window_name_must_be_text(tmp_path, name):
    with pytest.raises(ConfigError, match="name that is blank or is not text"):
        _load(tmp_path, maintenance=[_window(name=name)])


@pytest.mark.parametrize("name,message", [
    (" Sunday", "starts or ends with a space"),
    ("Sunday ", "starts or ends with a space"),
    ("Sun\nday", "control character"),
    ("Sun\tday", "control character"),
    ("x" * 256, "longest allowed is 255"),
])
def test_a_window_name_follows_the_monitor_name_rules(tmp_path, name, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, maintenance=[_window(name=name)])


def test_a_window_name_of_255_characters_is_accepted(tmp_path):
    _load(tmp_path, maintenance=[_window(name="x" * 255)])


def test_two_windows_cannot_share_a_name(tmp_path):
    with pytest.raises(ConfigError, match="Two maintenance windows are named 'a'"):
        _load(tmp_path, maintenance=[
            _window(name="A"), _window(name="a", start_time="03:00", end_time="04:00"),
        ])


def test_a_window_must_have_a_name(tmp_path):
    unnamed = {k: v for k, v in _window().items() if k != "name"}
    with pytest.raises(ConfigError, match=r"maintenance\[0\] is missing the required 'name'"):
        _load(tmp_path, maintenance=[unnamed])


def test_resume_is_found_for_a_named_window():
    config = {"maintenance": [{"name": "A", "resume": "normal"}, {"name": "B"}]}
    assert _find_maintenance_resume_mode(config, ["A"]) == "normal"
    assert _find_maintenance_resume_mode(config, ["B"]) == "verify"


# --- Monthly windows and "following" --------------------------------------------------

@pytest.mark.parametrize("start_day,end_day,start_time,end_time", [
    ("2nd Tuesday", "2nd Wednesday", "23:00", "01:00"),   # the 2nd Wednesday is before the 2nd Tuesday in April and July 2026
    ("last Friday", "last Saturday", "22:00", "23:00"),   # the last Saturday is before the last Friday when a month ends on a Friday
    ("1st Friday", "1st Monday", "22:00", "23:00"),       # either can come first
])
def test_a_monthly_pair_whose_order_varies_by_month_is_rejected(
    tmp_path, start_day, end_day, start_time, end_time
):
    with pytest.raises(ConfigError, match="in some months the end comes before the start"):
        _load(tmp_path, maintenance=[_window(
            start_day=start_day, end_day=end_day,
            start_time=start_time, end_time=end_time,
        )])


@pytest.mark.parametrize("start_day,end_day,start_time,end_time", [
    ("2nd Tuesday", "2nd Tuesday", "02:00", "05:00"),     # the same day
    ("last Friday", "1st Monday", "23:00", "02:00"),      # always into the next month
    ("1st Monday", "2nd Monday", "23:00", "02:00"),       # always a week later
    ("1st Monday", "3rd Friday", "22:00", "23:00"),       # always later in the month
])
def test_a_monthly_pair_that_is_always_in_the_same_order_is_accepted(
    tmp_path, start_day, end_day, start_time, end_time
):
    _load(tmp_path, maintenance=[_window(
        start_day=start_day, end_day=end_day,
        start_time=start_time, end_time=end_time,
    )])


@pytest.mark.parametrize("start_day", ["Sunday", "2nd Tuesday", "last Friday", "2026-07-15"])
def test_following_is_accepted_after_any_kind_of_start_day(tmp_path, start_day):
    _load(tmp_path, maintenance=[_window(
        start_day=start_day, end_day="following Wednesday",
        start_time="23:00", end_time="01:00",
    )])


def test_following_is_not_case_or_space_sensitive(tmp_path):
    _load(tmp_path, maintenance=[_window(end_day="FOLLOWING   monday")])


def test_following_cannot_be_a_start_day(tmp_path):
    with pytest.raises(ConfigError, match='"following" only works in end_day'):
        _load(tmp_path, maintenance=[_window(start_day="following Monday", end_day="Monday")])


def test_following_needs_a_real_weekday(tmp_path):
    with pytest.raises(ConfigError, match="unrecognized weekday 'Blursday'"):
        _load(tmp_path, maintenance=[_window(end_day="following Blursday")])


def test_following_after_a_date_needs_a_real_date(tmp_path):
    with pytest.raises(ConfigError, match="not a real date"):
        _load(tmp_path, maintenance=[_window(
            start_day="2026-02-30", end_day="following Monday",
        )])


def test_a_weekly_window_that_lasts_a_week_or_longer_is_rejected(tmp_path):
    """Sunday to "following Sunday" is seven days, so it would always be active."""
    with pytest.raises(ConfigError, match="a week or longer"):
        _load(tmp_path, maintenance=[_window(
            start_day="Sunday", end_day="following Sunday",
            start_time="01:00", end_time="02:00",
        )])


def test_a_weekly_window_that_ends_on_the_next_day_with_following_is_accepted(tmp_path):
    _load(tmp_path, maintenance=[_window(
        start_day="Sunday", end_day="following Monday",
        start_time="23:30", end_time="00:30",
    )])


# --- Theme ----------------------------------------------------------------------------


def _theme(tmp_path, data):
    path = tmp_path / "theme.yaml"
    path.write_text(yaml.dump(data))
    return load_theme(str(path))


@pytest.mark.parametrize("color", [
    "red", "Red", "REBECCAPURPLE", "transparent", "currentcolor",
    "lightgoldenrodyellow", "#fff", "#ffffff", "#ffffffaa", "rgb(1,2,3)",
    "rgba(0, 0, 0, 0.5)",
])
def test_real_colors_are_accepted(tmp_path, color):
    assert _theme(tmp_path, {"backgrounds": {"page": color}})["backgrounds"]["page"] == color


@pytest.mark.parametrize("color", ["gren", "notacolor", "redd", "var", "#ffff", "RGB(1,2,3)"])
def test_a_word_that_is_not_a_css_color_is_replaced_with_the_default(tmp_path, color):
    """A "named color" is not just any word of letters: a typo would stay in
    the theme and the browser would silently ignore it."""
    theme = _theme(tmp_path, {"backgrounds": {"page": color}})
    assert theme["backgrounds"]["page"] == "#161616"


def test_an_unknown_theme_name_is_warned_about_once(tmp_path, caplog):
    config_module._THEME_UNKNOWN_WARNED.clear()
    data = {"backgrounds": {"pagee": "red"}, "bogus": {"x": 1}}
    with caplog.at_level(logging.WARNING, logger="src.config"):
        _theme(tmp_path, data)
        _theme(tmp_path, data)  # the theme is read on every request
    messages = [r.getMessage() for r in caplog.records]
    assert sum("unknown setting 'pagee' under 'backgrounds:'" in m for m in messages) == 1
    assert sum("unknown section 'bogus'" in m for m in messages) == 1


def test_a_theme_section_that_is_not_a_mapping_uses_the_defaults(tmp_path):
    theme = _theme(tmp_path, {"backgrounds": "red"})
    assert theme["backgrounds"]["page"] == "#161616"


# --- Unknown settings -----------------------------------------------------------------


def test_the_list_of_global_settings_matches_the_defaults():
    """A new global setting that is not on the list would be rejected."""
    assert set(GLOBAL_KEYS) == set(config_module.DEFAULTS["global"]) | {"contact_email"}


@pytest.mark.parametrize("file_name,text,hint", [
    ("monitors.yaml", "monitor:\n  - name: M\n    type: ping\n    host: example.com\n",
     "unknown top-level setting 'monitor' — did you mean 'monitors'"),
    ("alerts.yaml", "alert:\n  push_alerts: []\n",
     "unknown top-level setting 'alert' — did you mean 'alerts'"),
    ("maintenance.yaml", "maintenence: []\n",
     "unknown top-level setting 'maintenence' — did you mean 'maintenance'"),
    ("tags.yaml", "tag:\n  - prod\n",
     "unknown top-level setting 'tag' — did you mean 'tags'"),
    ("config.yaml", "globl:\n  timeout: 5\n",
     "unknown top-level setting 'globl' — did you mean 'global'"),
])
def test_a_misspelled_top_level_key_is_rejected(tmp_path, file_name, text, hint):
    """Unchecked, the whole file would be ignored, so every alert, monitor, or
    window in it would silently disappear."""
    with pytest.raises(ConfigError, match=hint):
        _load(tmp_path, files={file_name: text})


@pytest.mark.parametrize("section,file_name", [
    ("monitors", "monitors.yaml"), ("alerts", "alerts.yaml"),
    ("tags", "tags.yaml"), ("maintenance", "maintenance.yaml"),
])
def test_a_section_in_config_yaml_is_rejected_with_where_it_belongs(
    tmp_path, section, file_name
):
    with pytest.raises(ConfigError, match=rf"'{section}' — it belongs in {file_name}"):
        _load(tmp_path, files={"config.yaml": f"global: {{}}\n{section}: []\n"})


def test_a_misspelled_global_setting_says_what_it_probably_is(tmp_path):
    with pytest.raises(ConfigError, match="'check_intervall_minutes' — did you mean 'check_interval_minutes'"):
        _load(tmp_path, global_={"check_intervall_minutes": 1})


def test_a_removed_global_setting_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown setting 'dashboard'"):
        _load(tmp_path, global_={"dashboard": {"port": 8000}})


def test_a_misspelled_monitor_setting_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="monitor 'M': unknown setting 'chek_interval_minutes' — did you mean 'check_interval_minutes'"):
        _load(tmp_path, monitors=[{**_HTTP, "chek_interval_minutes": 1}])


def test_a_renamed_setting_says_what_replaced_it(tmp_path):
    with pytest.raises(ConfigError, match="'interval_minutes' — use 'check_interval_minutes' instead"):
        _load(tmp_path, monitors=[{**_HTTP, "interval_minutes": 1}])
    with pytest.raises(ConfigError, match="'apikey' — use 'api_key' instead"):
        _load(tmp_path, alerts=_push(
            type="callmebot", channel="whatsapp", phone="+15551234567",
            apikey="k", api_key="k",
        ))


@pytest.mark.parametrize("monitor,message", [
    ({"name": "M", "type": "ping", "host": "example.com", "port": 80},
     "'port' only applies to tcp monitors, not 'ping'"),
    ({"name": "M", "type": "ping", "host": "example.com", "url": "http://x"},
     "'url' only applies to http monitors, not 'ping'"),
    ({"name": "M", "type": "tcp", "host": "example.com", "port": 80, "count": 3},
     "'count' only applies to ping monitors, not 'tcp'"),
    ({"name": "M", "type": "tcp", "host": "example.com", "port": 80, "verify_ssl": False},
     "'verify_ssl' only applies to http monitors, not 'tcp'"),
    ({**_HTTP, "host": "example.com"},
     "'host' only applies to ping and tcp monitors, not 'http'"),
])
def test_a_monitor_setting_for_another_type_is_rejected(tmp_path, monitor, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, monitors=[monitor])


def test_a_setting_in_the_wrong_file_says_where_it_belongs(tmp_path):
    with pytest.raises(ConfigError, match="'timeout' — it belongs in config.yaml under global:"):
        _load(tmp_path, alerts={"timeout": 5})


def test_the_old_block_level_email_settings_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="'to' — it belongs in an entry under recipients"):
        _load(tmp_path, alerts={"email": {"to": ["a@b.com"], "recipients": []}})


def test_smtp_login_in_alerts_yaml_points_at_the_env_file(tmp_path):
    with pytest.raises(ConfigError, match="'smtp_user' — use SMTP_USER in your .env file"):
        _load(tmp_path, alerts={"email": {"smtp_user": "x", "recipients": []}})


def test_a_misspelled_setting_in_a_recipients_entry_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="recipients entry 1: unknown setting 'sytem' — did you mean 'system'"):
        _load(tmp_path, alerts={"email": {"recipients": [
            {"to": ["a@b.com"], "monitors": "all", "sytem": False},
        ]}})


@pytest.mark.parametrize("alert,message", [
    ({"type": "generic", "url": "http://x.com", "monitor": "x"},
     "push alert 1 \\(type 'generic'\\): unknown setting 'monitor' — did you mean 'monitors'"),
    ({"type": "telegram", "bot_token": "1:a", "chat_id": "1", "url": "http://x.com"},
     "'url' does not apply to this type"),
    ({"type": "slack", "url": "https://hooks.slack.com/x", "bot_token": "1:a"},
     "'bot_token' does not apply to this type — it is used by 'telegram'"),
    ({"type": "callmebot", "channel": "facebook", "api_key": "k", "phone": "+15551234567"},
     "'phone' does not apply to the channel 'facebook' — it is used by 'whatsapp' and 'signal'"),
    ({"typ": "slack", "url": "https://x"},
     "push alert 1: unknown setting 'typ' — did you mean 'type'"),
])
def test_unknown_and_misplaced_push_alert_settings_are_rejected(tmp_path, alert, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, alerts={"push_alerts": [{"monitors": "all", **alert}]})


def test_a_misspelled_window_setting_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="window 'W': unknown setting 'ressume' — did you mean 'resume'"):
        _load(tmp_path, maintenance=[_window(ressume="normal")])


def test_every_unknown_name_is_reported_at_once(tmp_path):
    with pytest.raises(ConfigError) as err:
        _load(
            tmp_path, global_={"first": 1, "second": 2},
            monitors=[{**_HTTP, "third": 3}],
        )
    for name in ("'first'", "'second'", "'third'"):
        assert name in str(err.value)


def test_a_name_that_is_valid_for_its_place_is_never_rejected(tmp_path):
    """Every setting the documentation lists, in the place it belongs."""
    config = _load(
        tmp_path,
        global_={name: value for name, value in config_module.DEFAULTS["global"].items()},
        monitors=[
            {"name": "A", "type": "ping", "host": "example.com", "count": 2,
             "active": True, "tags": ["a"], "check_interval_minutes": 5,
             "timeout": 10, "retention_days": 90, "alert_cooldown_minutes": 5,
             "alert_retries": 2, "retry_interval_minutes": 1, "retry_on_up": True},
            {"name": "B", "type": "tcp", "host": "example.com", "port": 80},
            {**_HTTP, "name": "C", "verify_ssl": False, "expected_status": 401},
        ],
        tags=["a"],
        alerts={
            "email": {**_EMAIL, "from_name": "X", "subject_prefix": "[X]"},
            "push_alerts": [
                {"type": "ntfy", "url": "https://ntfy.sh/t", "username": "u",
                 "password": "p", "token": "t", "priority": "high",
                 "enabled": True, "system": True, "monitors": ["A"], "tags": ["a"]},
                {"type": "callmebot", "channel": "whatsapp",
                 "phone": "+15551234567", "api_key": "k", "monitors": "all"},
            ],
        },
        maintenance=[_window(resume="normal")],
    )
    assert len(config["monitors"]) == 3
