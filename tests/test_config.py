# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_config.py - Tests for src/config.py.
"""

import shutil
from pathlib import Path

import pytest

from src.config import ConfigError, load_config, load_theme

# --- Split config tests ---------------------------------------------------------------


def test_split_config_loads_monitors_from_separate_file(tmp_path):
    """Monitors should load from monitors.yaml, not config.yaml."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"check_interval_minutes": 5}}, f)
    monitors_path = tmp_path / "monitors.yaml"
    with open(monitors_path, "w") as f:
        yaml.dump(
            {"monitors": [{"name": "Test", "type": "http", "url": "http://example.com"}]},
            f,
        )
    config = load_config(str(config_path))
    assert len(config["monitors"]) == 1
    assert config["monitors"][0]["name"] == "Test"


def test_split_config_loads_alerts_from_separate_file(tmp_path):
    """Alerts should load from alerts.yaml."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    alerts_path = tmp_path / "alerts.yaml"
    with open(alerts_path, "w") as f:
        yaml.dump(
            {
                "alerts": {
                    "email": {
                        "smtp_host": "smtp.example.com",
                        "smtp_port": 587,
                        "from": "alerts@example.com",
                        "recipients": [
                            {"to": ["me@example.com"], "monitors": "all"},
                        ],
                    },
                    "push_alerts": [
                        {"type": "generic", "url": "http://example.com", "monitors": "all"},
                    ],
                }
            },
            f,
        )
    config = load_config(str(config_path))
    assert len(config["alerts"]["email"]["recipients"]) == 1
    assert len(config["alerts"]["push_alerts"]) == 1


def test_split_config_loads_tags_from_separate_file(tmp_path):
    """Tags should load from tags.yaml."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    tags_path = tmp_path / "tags.yaml"
    with open(tags_path, "w") as f:
        yaml.dump({"tags": ["external", "critical"]}, f)
    config = load_config(str(config_path))
    assert config["tags"] == ["external", "critical"]


def test_split_config_loads_maintenance_from_separate_file(tmp_path):
    """Maintenance should load from maintenance.yaml."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"timezone": "UTC"}}, f)
    maint_path = tmp_path / "maintenance.yaml"
    with open(maint_path, "w") as f:
        yaml.dump(
            {
                "maintenance": [
                    {
                        "name": "Test",
                        "start_day": "Sunday",
                        "start_time": "02:00",
                        "end_day": "Sunday",
                        "end_time": "04:00",
                        "monitors": "all",
                    }
                ]
            },
            f,
        )
    config = load_config(str(config_path))
    assert len(config["maintenance"]) == 1
    assert config["maintenance"][0]["name"] == "Test"


def test_split_config_missing_optional_files_use_defaults(tmp_path):
    """Missing alerts.yaml, monitors.yaml, tags.yaml, maintenance.yaml use defaults."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    config = load_config(str(config_path))
    assert config["monitors"] == []
    assert config["tags"] == []
    assert config["maintenance"] == []
    assert config["alerts"]["email"]["recipients"] == []
    assert config["alerts"]["push_alerts"] == []


def test_split_config_env_var_in_alerts_yaml(tmp_path, monkeypatch):
    """Env var substitution should work in alerts.yaml."""
    monkeypatch.setenv("TEST_SMTP_HOST", "smtp.from-env.example.com")
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    alerts_path = tmp_path / "alerts.yaml"
    with open(alerts_path, "w") as f:
        f.write(
            "alerts:\n"
            "  email:\n"
            "    smtp_host: ${TEST_SMTP_HOST}\n"
            "    smtp_port: 587\n"
            "    from: alerts@example.com\n"
            "    recipients:\n"
            "      - to: [me@example.com]\n"
            "        monitors: all\n"
        )
    config = load_config(str(config_path))
    assert config["alerts"]["email"]["smtp_host"] == "smtp.from-env.example.com"


# --- Tag validation tests -------------------------------------------------------------


def test_tag_validation_rejects_unknown_tag_on_monitor(tmp_path):
    """Monitor with tag not in tags.yaml should raise ConfigError."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump(
            {"monitors": [{"name": "T", "type": "http", "url": "http://x.com", "tags": ["external", "unknown-tag"]}]},
            f,
        )
    with open(tmp_path / "tags.yaml", "w") as f:
        yaml.dump({"tags": ["external"]}, f)
    with pytest.raises(ConfigError, match="unknown-tag"):
        load_config(str(config_path))


def test_tag_validation_rejects_unknown_tag_on_alert_channel(tmp_path):
    """Alert channel with tag not in tags.yaml should raise ConfigError."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "alerts.yaml", "w") as f:
        yaml.dump({"alerts": {"push_alerts": [{"type": "generic", "url": "http://x.com", "tags": ["unknown-tag"]}]}}, f)
    with open(tmp_path / "tags.yaml", "w") as f:
        yaml.dump({"tags": ["external"]}, f)
    with pytest.raises(ConfigError, match="unknown-tag"):
        load_config(str(config_path))


def test_tag_validation_rejects_unknown_tag_on_maintenance(tmp_path):
    """Maintenance window with tag not in tags.yaml should raise ConfigError."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"timezone": "UTC"}}, f)
    with open(tmp_path / "maintenance.yaml", "w") as f:
        yaml.dump(
            {"maintenance": [{"name": "Test", "start_day": "Sunday", "start_time": "02:00", "end_day": "Sunday", "end_time": "04:00", "tags": ["unknown-tag"]}]},
            f,
        )
    with open(tmp_path / "tags.yaml", "w") as f:
        yaml.dump({"tags": ["external"]}, f)
    with pytest.raises(ConfigError, match="unknown-tag"):
        load_config(str(config_path))


def test_tag_validation_passes_with_valid_tags(tmp_path):
    """All tag references valid should load without error."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"timezone": "UTC"}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "http", "url": "http://x.com", "tags": ["external"]}]}, f)
    with open(tmp_path / "alerts.yaml", "w") as f:
        yaml.dump({"alerts": {"push_alerts": [{"type": "generic", "url": "http://x.com", "tags": ["external"]}]}}, f)
    with open(tmp_path / "tags.yaml", "w") as f:
        yaml.dump({"tags": ["external"]}, f)
    config = load_config(str(config_path))
    assert config["monitors"][0]["tags"] == ["external"]


def test_tag_without_a_tags_yaml_is_rejected(tmp_path):
    """tags.yaml is the only source of tags: with no tags.yaml (or an empty
    one) no tag is allowed, so a tag on a monitor stops startup."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "http", "url": "http://x.com", "tags": ["anything"]}]}, f)
    with pytest.raises(ConfigError, match="unknown tag 'anything'.*tags.yaml"):
        load_config(str(config_path))


def test_email_alert_tag_must_be_listed_in_tags_yaml(tmp_path):
    """The email alert's tags follow the same rule as a push alert's tags."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "alerts.yaml", "w") as f:
        yaml.dump({"alerts": {"email": {"recipients": [{"to": ["a@example.com"], "enabled": False, "tags": ["critical"]}]}}}, f)
    with pytest.raises(ConfigError, match="Email alert.*unknown tag 'critical'.*tags.yaml"):
        load_config(str(config_path))
    with open(tmp_path / "tags.yaml", "w") as f:
        yaml.dump({"tags": ["critical"]}, f)
    assert load_config(str(config_path))["alerts"]["email"]["recipients"][0]["tags"] == ["critical"]


def test_tags_are_not_case_sensitive(tmp_path):
    """"Critical" and "critical" are the same tag everywhere, and are stored
    in lowercase once the config is loaded."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "tags.yaml", "w") as f:
        yaml.dump({"tags": ["Critical", "critical", "Testing"]}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [
            {"name": "A", "type": "http", "url": "http://x.com", "tags": ["CRITICAL"]},
            {"name": "B", "type": "http", "url": "http://x.com", "tags": ["critical", "testing"]},
        ]}, f)
    with open(tmp_path / "alerts.yaml", "w") as f:
        yaml.dump({"alerts": {
            "email": {"recipients": [{"to": ["a@example.com"], "enabled": False, "tags": ["Critical"]}]},
            "push_alerts": [{"type": "generic", "url": "http://x.com", "tags": ["cRiTiCaL"]}],
        }}, f)
    with open(tmp_path / "maintenance.yaml", "w") as f:
        yaml.dump({"maintenance": [{
            "name": "W", "start_day": "Sunday", "end_day": "Sunday",
            "start_time": "01:00", "end_time": "02:00",
            "tags": ["TESTING"],
        }]}, f)
    config = load_config(str(config_path))
    assert config["tags"] == ["critical", "critical", "testing"]
    assert config["monitors"][0]["tags"] == ["critical"]
    assert config["monitors"][1]["tags"] == ["critical", "testing"]
    assert config["alerts"]["email"]["recipients"][0]["tags"] == ["critical"]
    assert config["alerts"]["push_alerts"][0]["tags"] == ["critical"]
    assert config["maintenance"][0]["tags"] == ["testing"]


def test_unknown_tag_is_still_rejected_when_case_differs_only_elsewhere(tmp_path):
    """Ignoring case doesn't turn a tag that isn't in tags.yaml into a valid one."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "tags.yaml", "w") as f:
        yaml.dump({"tags": ["Critical"]}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "http", "url": "http://x.com", "tags": ["Testing"]}]}, f)
    with pytest.raises(ConfigError, match="unknown tag 'testing'.*tags.yaml"):
        load_config(str(config_path))


def _write_tag_config(tmp_path, tags_yaml_tags, monitor_tags=None):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "tags.yaml", "w") as f:
        yaml.dump({"tags": tags_yaml_tags}, f)
    if monitor_tags is not None:
        with open(tmp_path / "monitors.yaml", "w") as f:
            yaml.dump({"monitors": [{
                "name": "T", "type": "http", "url": "http://x.com",
                "tags": monitor_tags,
            }]}, f)
    return config_path


@pytest.mark.parametrize("tag", [
    "critical", "high-priority", "web_1", "2024", "-", "_",
    "é", "é", "日本語", "Ünï-çode_1", "٣",
])
def test_tag_names_accept_letters_digits_hyphens_underscores(tmp_path, tag):
    """Letters and digits from any language, hyphens and underscores are fine."""
    config_path = _write_tag_config(tmp_path, [tag], [tag])
    config = load_config(str(config_path))
    assert config["monitors"][0]["tags"] == [tag.lower()]


@pytest.mark.parametrize("tag", [
    "high priority", "a,b", "a.b", "a/b", "a:b", "a!", "50%", "a'b", 'a"b',
    "🔥", "★", "tab\there", "new\nline", "a b",
])
def test_tag_names_reject_other_characters(tmp_path, tag):
    """A space, other punctuation, a symbol or emoji is a configuration error
    that names the tag, wherever the tag is written."""
    config_path = _write_tag_config(tmp_path, [tag])
    with pytest.raises(ConfigError, match=r"tags\.yaml has the tag .*a tag can only contain"):
        load_config(str(config_path))


def test_tag_with_a_space_is_reported_for_the_monitor_that_uses_it(tmp_path):
    """The error names the monitor and the character, instead of saying the
    tag is unknown (adding it to tags.yaml would not make it valid)."""
    config_path = _write_tag_config(tmp_path, [], ["high priority"])
    with pytest.raises(ConfigError, match=r"Monitor 'T' has the tag 'high priority', which contains ' '"):
        load_config(str(config_path))


def test_tag_names_are_limited_to_255_characters(tmp_path):
    config_path = _write_tag_config(tmp_path, ["a" * 255], ["a" * 255])
    assert load_config(str(config_path))["tags"] == ["a" * 255]
    config_path = _write_tag_config(tmp_path, ["a" * 256])
    with pytest.raises(ConfigError, match=r"tags\.yaml has a tag .* 256 characters long, but the longest allowed is 255"):
        load_config(str(config_path))


def test_tag_names_reject_empty_and_non_text_tags(tmp_path):
    config_path = _write_tag_config(tmp_path, [""])
    with pytest.raises(ConfigError, match=r"tags\.yaml has an empty tag"):
        load_config(str(config_path))
    config_path = _write_tag_config(tmp_path, [True])
    with pytest.raises(ConfigError, match=r"tags\.yaml has the tag True, which is not text"):
        load_config(str(config_path))


def test_tag_names_are_checked_on_alerts_and_maintenance_too(tmp_path):
    import yaml
    config_path = _write_tag_config(tmp_path, ["ok"])
    with open(tmp_path / "alerts.yaml", "w") as f:
        yaml.dump({"alerts": {"email": {"recipients": [{"to": ["a@example.com"], "enabled": False, "tags": ["a,b"]}]}}}, f)
    with pytest.raises(ConfigError, match=r"Email alert recipients\[0\] has the tag 'a,b'"):
        load_config(str(config_path))
    with open(tmp_path / "alerts.yaml", "w") as f:
        yaml.dump({"alerts": {"push_alerts": [{"type": "generic", "url": "http://x.com", "tags": ["a b"]}]}}, f)
    with pytest.raises(ConfigError, match=r"Alert channel 'generic' has the tag 'a b'"):
        load_config(str(config_path))
    (tmp_path / "alerts.yaml").unlink()
    with open(tmp_path / "maintenance.yaml", "w") as f:
        yaml.dump({"maintenance": [{
            "name": "W", "start_day": "Sunday", "end_day": "Sunday",
            "start_time": "01:00", "end_time": "02:00",
            "tags": ["a!"],
        }]}, f)
    with pytest.raises(ConfigError, match=r"Maintenance window 'W' has the tag 'a!'"):
        load_config(str(config_path))


# --- monitors and tags on alerts and maintenance windows ------------------------------


_WEB = {"name": "web", "type": "http", "url": "http://x.com"}
_WINDOW = {
    "name": "W", "start_day": "Sunday", "end_day": "Sunday",
    "start_time": "01:00", "end_time": "02:00",
}


def _load_tree(tmp_path, monitors=None, tags=None, alerts=None, maintenance=None):
    import yaml
    for stale in ("alerts.yaml", "tags.yaml", "maintenance.yaml"):
        (tmp_path / stale).unlink(missing_ok=True)  # one tree per call
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"global": {}}))
    (tmp_path / "monitors.yaml").write_text(
        yaml.dump({"monitors": [_WEB] if monitors is None else monitors})
    )
    if tags is not None:
        (tmp_path / "tags.yaml").write_text(yaml.dump({"tags": tags}))
    if alerts is not None:
        (tmp_path / "alerts.yaml").write_text(yaml.dump({"alerts": alerts}))
    if maintenance is not None:
        (tmp_path / "maintenance.yaml").write_text(yaml.dump({"maintenance": maintenance}))
    return load_config(str(config_path))


_PUSH = {"type": "generic", "url": "http://x.com"}
# An email recipients entry that is turned off (it still needs a `to`).
_OFF_GROUP = {"to": ["a@example.com"], "enabled": False}


@pytest.mark.parametrize("targets", [
    {"monitors": "all"},
    {"tags": "all"},
    {"monitors": ["web"]},
    {"tags": ["prod"]},
    {"monitors": ["web"], "tags": ["prod"]},
    {"monitors": "all", "tags": "all"},
])
def test_alerts_and_windows_accept_all_or_a_list(tmp_path, targets):
    config = _load_tree(
        tmp_path, tags=["prod"],
        alerts={"email": {"recipients": [{**_OFF_GROUP, **targets}]},
                "push_alerts": [{**_PUSH, **targets}]},
        maintenance=[{**_WINDOW, **targets}],
    )
    assert config["alerts"]["push_alerts"][0] == {**_PUSH, **targets}


@pytest.mark.parametrize("field", ["monitors", "tags"])
@pytest.mark.parametrize("value", ["none", "al", "All", "web", "prod", 5, True])
def test_text_other_than_all_where_a_list_is_expected_is_an_error(
    tmp_path, field, value,
):
    """`none` is not special, and text that is not exactly "all" (a typo, a
    name, a tag) is an error, not a list of letters or a silent no-match."""
    entry = {field: value}
    for kwargs in (
        {"alerts": {"push_alerts": [{**_PUSH, **entry}]}},
        {"alerts": {"email": {"recipients": [{**_OFF_GROUP, **entry}]}}},
        {"maintenance": [{**_WINDOW, **entry}]},
    ):
        with pytest.raises(ConfigError, match=rf"invalid '{field}' value"):
            _load_tree(tmp_path, tags=["prod"], **kwargs)


@pytest.mark.parametrize("field", ["monitors", "tags"])
def test_empty_monitors_or_tags_is_an_error(tmp_path, field):
    """An uncommented setting with no value is an error; it should be deleted
    or commented out."""
    entry = {field: None}
    for kwargs in (
        {"alerts": {"push_alerts": [{**_PUSH, **entry}]}},
        {"alerts": {"email": {"recipients": [{**_OFF_GROUP, **entry}]}}},
        {"maintenance": [{**_WINDOW, **entry}]},
    ):
        with pytest.raises(ConfigError, match=rf"invalid '{field}' value None"):
            _load_tree(tmp_path, tags=["prod"], **kwargs)


def test_a_monitors_own_tags_must_be_a_list(tmp_path):
    """"all" is for alerts and windows. A monitor lists its own tags."""
    for value in ("all", "none", "prod", None):
        monitor = {**_WEB, "tags": value}
        with pytest.raises(ConfigError, match=r"Monitor 'web' has an invalid 'tags' value"):
            _load_tree(tmp_path, monitors=[monitor], tags=["prod"])


def test_alert_and_window_monitor_names_ignore_case(tmp_path):
    """`WEB` names the monitor `web`, in alerts and in maintenance windows."""
    _load_tree(
        tmp_path,
        alerts={
            "push_alerts": [{**_PUSH, "monitors": ["WEB"]}],
            "email": {"recipients": [{**_OFF_GROUP, "monitors": ["Web"]}]},
        },
        maintenance=[{**_WINDOW, "monitors": ["wEb"]}],
    )


def test_entry_applies_to_monitor_ignores_case_of_names():
    from src.config import entry_applies_to_monitor
    entry = {"monitors": ["Primary Server"]}
    assert entry_applies_to_monitor(entry, "primary server", set())
    assert entry_applies_to_monitor(entry, "PRIMARY SERVER", set())
    assert not entry_applies_to_monitor(entry, "Secondary Server", set())


def test_alert_and_window_monitor_names_must_exist(tmp_path):
    for kwargs, label in (
        ({"alerts": {"push_alerts": [{**_PUSH, "monitors": ["web", "wbe"]}]}},
         "Alert channel 'generic'"),
        ({"alerts": {"email": {"recipients": [{**_OFF_GROUP, "monitors": ["wbe"]}]}}},
         r"Email alert recipients\[0\]"),
        ({"maintenance": [{**_WINDOW, "monitors": ["wbe"]}]},
         "Maintenance window 'W'"),
    ):
        with pytest.raises(ConfigError, match=rf"{label} targets unknown monitor\(s\): 'wbe'"):
            _load_tree(tmp_path, **kwargs)


@pytest.mark.parametrize("targets", [{}, {"monitors": []}, {"tags": []}, {"monitors": [], "tags": []}])
def test_alert_and_window_in_use_need_monitors_or_tags(tmp_path, targets):
    """Neither setting, or only empty lists, means no monitor: an error for
    every push alert that is on, the email alert when it is on, and every
    maintenance window."""
    for kwargs, label in (
        ({"alerts": {"push_alerts": [{**_PUSH, **targets}]}}, "Alert channel 'generic'"),
        ({"alerts": {"push_alerts": [{**_PUSH, **targets, "enabled": True}]}}, "Alert channel 'generic'"),
        ({"alerts": {"email": {"smtp_host": "h", "smtp_port": 587,
                               "from": "a@example.com",
                               "recipients": [{**targets, "to": ["b@example.com"]}]}}},
         r"Email alert recipients\[0\]"),
        ({"maintenance": [{**_WINDOW, **targets}]}, "Maintenance window 'W'"),
    ):
        with pytest.raises(ConfigError, match=rf"{label} doesn't say which monitors it is for"):
            _load_tree(tmp_path, **kwargs)


def test_alerts_that_are_turned_off_need_no_monitors_or_tags(tmp_path):
    """The shipped alerts.yaml has the email alert off with nothing else set,
    and a push alert can be turned off without deleting it."""
    config = _load_tree(
        tmp_path,
        alerts={"email": {"recipients": [dict(_OFF_GROUP)]},
                "push_alerts": [{**_PUSH, "enabled": False}]},
    )
    assert config["alerts"]["email"]["recipients"][0]["enabled"] is False
    assert _load_tree(tmp_path)["alerts"]["email"]["recipients"] == []


@pytest.mark.parametrize("name", ["all", "All", "ALL"])
def test_all_is_reserved_as_a_tag_and_a_monitor_name(tmp_path, name):
    with pytest.raises(ConfigError, match=r"tags.yaml has the tag .* which is reserved"):
        _load_tree(tmp_path, tags=[name])
    with pytest.raises(ConfigError, match=r"Monitor 'web' has the tag .* which is reserved"):
        _load_tree(tmp_path, monitors=[{**_WEB, "tags": [name]}], tags=["x"])
    with pytest.raises(ConfigError, match=r"Monitor name .* is reserved"):
        _load_tree(tmp_path, monitors=[{**_WEB, "name": name}])


def test_names_that_only_contain_all_are_fine(tmp_path):
    config = _load_tree(
        tmp_path, monitors=[{**_WEB, "name": "All Services", "tags": ["all-clear", "overall"]}],
        tags=["all-clear", "overall"],
    )
    assert config["monitors"][0]["name"] == "All Services"


def test_monitor_without_tags_is_fine_with_no_tags_yaml(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "http", "url": "http://x.com"}]}, f)
    assert load_config(str(config_path))["monitors"][0]["name"] == "T"


def test_empty_tags_list_and_all_need_no_tags_yaml(tmp_path):
    """`tags: []` on a monitor and `tags: all` on a window name no tag, so
    tags.yaml doesn't need to list any."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "http", "url": "http://x.com", "tags": []}]}, f)
    with open(tmp_path / "maintenance.yaml", "w") as f:
        yaml.dump({"maintenance": [{
            "name": "w", "monitors": "all", "tags": "all",
            "start_day": "monday", "start_time": "01:00",
            "end_day": "monday", "end_time": "02:00",
        }]}, f)
    load_config(str(config_path))


def test_loads_valid_config(sample_config, tmp_path):
    """The sample config, split across the files each section belongs in."""
    import yaml
    for section, file_name in (
        ("global", "config.yaml"), ("alerts", "alerts.yaml"),
        ("monitors", "monitors.yaml"), ("tags", "tags.yaml"),
        ("maintenance", "maintenance.yaml"),
    ):
        if section in sample_config:
            with open(tmp_path / file_name, "w") as f:
                yaml.dump({section: sample_config[section]}, f)
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["global"]["check_interval_minutes"] == 1


def test_env_var_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "smtp.hunter2.example.com")
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        f.write("global: {}\n")
    alerts_path = tmp_path / "alerts.yaml"
    with open(alerts_path, "w") as f:
        f.write("alerts:\n  email:\n    recipients: []\n    smtp_host: ${TEST_SECRET}\n")
    config = load_config(str(config_path))
    assert config["alerts"]["email"]["smtp_host"] == "smtp.hunter2.example.com"


def test_missing_env_var_raises(tmp_path):
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        f.write("global: {}\n")
    alerts_path = tmp_path / "alerts.yaml"
    with open(alerts_path, "w") as f:
        f.write("alerts:\n  email:\n    recipients: []\n    smtp_host: ${MISSING_VAR}\n")
    with pytest.raises(ConfigError, match="MISSING_VAR"):
        load_config(str(config_path))


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/config.yaml")


def test_invalid_yaml_raises(tmp_path):
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        f.write(": invalid yaml: :")
    with pytest.raises(ConfigError):
        load_config(str(config_path))


def test_defaults_applied(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": []}, f)
    config = load_config(str(config_path))
    assert config["global"]["check_interval_minutes"] == 5
    assert config["global"]["retention_days"] == 90
    assert config["global"]["alert_cooldown_minutes"] == 5


# --- Theme tests ----------------------------------------------------------------------


def test_load_theme_valid_file(tmp_path):
    """load_theme should load a valid theme.yaml and return merged dict."""
    import yaml
    theme_path = tmp_path / "theme.yaml"
    custom = {
        "backgrounds": {"page": "#000000"},
        "status": {"up": "#00ff00"},
    }
    with open(theme_path, "w") as f:
        yaml.dump(custom, f)
    theme = load_theme(str(theme_path))
    # Custom values override defaults
    assert theme["backgrounds"]["page"] == "#000000"
    assert theme["status"]["up"] == "#00ff00"
    # Unspecified keys fall back to defaults
    assert theme["text"]["main"] == "#ffffff"
    assert theme["status"]["down"] == "#990000"


def test_load_theme_missing_file_returns_defaults():
    """load_theme should return defaults when file is missing."""
    theme = load_theme("/nonexistent/theme.yaml")
    assert theme["backgrounds"]["page"] == "#161616"
    assert theme["status"]["up"] == "#00dd00"
    assert theme["borders"]["table"] == "#161616"


def test_load_theme_invalid_yaml_returns_defaults(tmp_path):
    """load_theme should return defaults when YAML is invalid."""
    theme_path = tmp_path / "theme.yaml"
    with open(theme_path, "w") as f:
        f.write(": invalid yaml: :")
    theme = load_theme(str(theme_path))
    assert theme["backgrounds"]["page"] == "#161616"
    assert theme["status"]["up"] == "#00dd00"


def test_load_theme_empty_file_returns_defaults(tmp_path):
    """load_theme should return defaults when file is empty."""
    theme_path = tmp_path / "theme.yaml"
    theme_path.write_text("")
    theme = load_theme(str(theme_path))
    assert theme["backgrounds"]["page"] == "#161616"
    assert theme["status"]["up"] == "#00dd00"


# --- Numeric field coercion -----------------------------------------------------------


def test_numeric_field_boolean_rejected(tmp_path):
    """A stray `true`/`false` for a numeric field must be rejected, not
    silently coerced to 1/0 (bool is an int subclass in Python)."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"check_interval_minutes": True}}, f)
    with pytest.raises(ConfigError, match="boolean"):
        load_config(str(config_path))


def test_numeric_field_unparseable_string_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"check_interval_minutes": "not-a-number"}}, f)
    with pytest.raises(ConfigError, match="check_interval_minutes"):
        load_config(str(config_path))


def test_numeric_field_env_substituted_string_coerced(tmp_path, monkeypatch):
    """An ${ENV_VAR}-substituted numeric field arrives as a string and must be
    coerced to a real number, not left as str and crash arithmetic later
    deep in the monitoring loop."""
    monkeypatch.setenv("INTERVAL", "5")
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        f.write("global:\n  check_interval_minutes: ${INTERVAL}\n")
    config = load_config(str(config_path))
    assert config["global"]["check_interval_minutes"] == 5
    assert isinstance(config["global"]["check_interval_minutes"], int)


def test_numeric_field_wrong_type_rejected(tmp_path):
    """A YAML list/mapping for a numeric field (not a bool, not a string,
    not already a number) hits the final catch-all type check."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"check_interval_minutes": [1, 2]}}, f)
    with pytest.raises(ConfigError, match="must be a number"):
        load_config(str(config_path))


def test_numeric_field_monitor_level_override_coerced(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({
            "monitors": [{
                "name": "T", "type": "http", "url": "http://x.com",
                "check_interval_minutes": "10",
            }],
        }, f)
    config = load_config(str(config_path))
    assert config["monitors"][0]["check_interval_minutes"] == 10


# --- Display decimal-places fields (uptime_decimals/latency_decimals) -----------------


def test_decimal_fields_default_when_unset(tmp_path):
    """Unset uptime_decimals/latency_decimals fall back to 2 and 1."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    config = load_config(str(config_path))
    assert config["global"]["uptime_decimals"] == 2
    assert config["global"]["latency_decimals"] == 1


def test_decimal_fields_valid_override_accepted(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(
            {"global": {"uptime_decimals": 4, "latency_decimals": 0}}, f,
        )
    config = load_config(str(config_path))
    assert config["global"]["uptime_decimals"] == 4
    assert config["global"]["latency_decimals"] == 0


def test_decimal_field_negative_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"uptime_decimals": -1}}, f)
    with pytest.raises(ConfigError, match="uptime_decimals"):
        load_config(str(config_path))


def test_decimal_field_above_max_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"latency_decimals": 7}}, f)
    with pytest.raises(ConfigError, match="latency_decimals"):
        load_config(str(config_path))


def test_decimal_field_float_rejected(tmp_path):
    """round()/format() precision must be a whole number of places --
    2.5 decimals isn't a meaningful value."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"uptime_decimals": 2.5}}, f)
    with pytest.raises(ConfigError, match="uptime_decimals"):
        load_config(str(config_path))


def test_decimal_field_boolean_rejected(tmp_path):
    """Same bool-is-an-int-subclass trap as the general numeric fields."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {"uptime_decimals": True}}, f)
    with pytest.raises(ConfigError, match="uptime_decimals"):
        load_config(str(config_path))


def test_decimal_field_env_substituted_string_coerced(tmp_path, monkeypatch):
    monkeypatch.setenv("LATENCY_DP", "3")
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        f.write("global:\n  latency_decimals: ${LATENCY_DP}\n")
    config = load_config(str(config_path))
    assert config["global"]["latency_decimals"] == 3
    assert isinstance(config["global"]["latency_decimals"], int)


# --- Monitor type/name validation -----------------------------------------------------


def test_monitor_missing_name_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"type": "http", "url": "http://x.com"}]}, f)
    with pytest.raises(ConfigError, match="name"):
        load_config(str(config_path))


def test_monitor_missing_type_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T"}]}, f)
    with pytest.raises(ConfigError, match="missing 'type'"):
        load_config(str(config_path))


def test_monitor_unsupported_type_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "carrier-pigeon"}]}, f)
    with pytest.raises(ConfigError, match="type must be one of"):
        load_config(str(config_path))


# --- expected_status ------------------------------------------------------------------


def test_http_monitor_expected_status_accepted(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({
            "monitors": [{
                "name": "T", "type": "http", "url": "http://x.com",
                "expected_status": 401,
            }],
        }, f)
    config = load_config(str(config_path))
    assert config["monitors"][0]["expected_status"] == 401


def test_http_monitor_expected_status_string_coerced_to_int(tmp_path):
    """Same numeric-coercion path as check_interval_minutes etc -- matters for
    ${ENV_VAR}-substituted values, which always come out of substitution
    as strings even when the field is conceptually numeric."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({
            "monitors": [{
                "name": "T", "type": "http", "url": "http://x.com",
                "expected_status": "401",
            }],
        }, f)
    config = load_config(str(config_path))
    assert config["monitors"][0]["expected_status"] == 401


def test_tcp_monitor_expected_status_rejected(tmp_path):
    """expected_status only means something for http monitors -- a tcp
    or ping monitor setting it is almost certainly a copy-paste mistake,
    catch it at load time rather than silently ignoring it forever."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({
            "monitors": [{
                "name": "T", "type": "tcp", "host": "x.com", "port": 443,
                "expected_status": 401,
            }],
        }, f)
    with pytest.raises(ConfigError, match="expected_status"):
        load_config(str(config_path))


def test_ping_monitor_expected_status_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({
            "monitors": [{
                "name": "T", "type": "ping", "host": "x.com",
                "expected_status": 401,
            }],
        }, f)
    with pytest.raises(ConfigError, match="expected_status"):
        load_config(str(config_path))


# --- ping count -----------------------------------------------------------------------


def _ping_count_config(tmp_path, count):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({
            "monitors": [{
                "name": "T", "type": "ping", "host": "x.com", "count": count,
            }],
        }, f)
    return str(config_path)


def _ping_count_timeout_config(tmp_path, count, timeout=None):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    monitor = {"name": "T", "type": "ping", "host": "x.com", "count": count}
    if timeout is not None:
        monitor["timeout"] = timeout
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [monitor]}, f)
    return str(config_path)


@pytest.mark.parametrize("count,timeout", [
    (10, None),   # the default timeout is 10
    (1, 1),
    (3, 3),
    (20, 20),
    (3, 10),
])
def test_ping_count_up_to_timeout_is_accepted(tmp_path, count, timeout):
    path = _ping_count_timeout_config(tmp_path, count, timeout)
    assert load_config(path)["monitors"][0]["count"] == count


@pytest.mark.parametrize("count,timeout", [
    (11, None),   # the default timeout is 10
    (5, 4),
    (3, 2),
    (20, 10),
])
def test_ping_count_above_timeout_is_rejected(tmp_path, count, timeout):
    """Pings go out one per second, so a count larger than the timeout gets
    the check killed and reported down even when the host is fine."""
    path = _ping_count_timeout_config(tmp_path, count, timeout)
    with pytest.raises(ConfigError, match=r"T.*count.*can't be larger than timeout"):
        load_config(path)


def test_ping_monitor_count_accepted(tmp_path):
    config = load_config(_ping_count_config(tmp_path, 3))
    assert config["monitors"][0]["count"] == 3


def test_ping_monitor_count_string_coerced_to_int(tmp_path):
    config = load_config(_ping_count_config(tmp_path, "3"))
    assert config["monitors"][0]["count"] == 3


@pytest.mark.parametrize("count", [0, -1, 1.5])
def test_ping_monitor_count_below_one_or_fractional_rejected(tmp_path, count):
    """ping itself rejects these, which would leave the monitor reporting
    down on every check -- catch it at load time instead."""
    with pytest.raises(ConfigError, match="count"):
        load_config(_ping_count_config(tmp_path, count))


# --- Misplaced sections ---------------------------------------------------------------


def test_http_monitor_missing_url_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "http"}]}, f)
    with pytest.raises(ConfigError, match="missing required field.*url"):
        load_config(str(config_path))


def test_tcp_monitor_missing_host_and_port_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "tcp"}]}, f)
    with pytest.raises(ConfigError, match="host.*port"):
        load_config(str(config_path))


def test_tcp_monitor_missing_port_only_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "tcp", "host": "db.internal"}]}, f)
    with pytest.raises(ConfigError, match="missing required field.*port"):
        load_config(str(config_path))


def test_ping_monitor_missing_host_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [{"name": "T", "type": "ping"}]}, f)
    with pytest.raises(ConfigError, match="missing required field.*host"):
        load_config(str(config_path))


def test_monitors_with_all_required_fields_accepted(tmp_path):
    """Sanity check that the validation doesn't reject well-formed
    monitors of every type."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "monitors.yaml", "w") as f:
        yaml.dump({"monitors": [
            {"name": "web", "type": "http", "url": "https://example.com"},
            {"name": "db", "type": "tcp", "host": "db.internal", "port": 3306},
            {"name": "gw", "type": "ping", "host": "192.168.1.1"},
        ]}, f)
    config = load_config(str(config_path))  # must not raise
    assert len(config["monitors"]) == 3


def test_misplaced_monitors_section_is_rejected(tmp_path):
    """monitors:/alerts:/tags:/maintenance: placed directly in config.yaml
    (instead of their own file) are an error that says where they belong,
    since the content is ignored otherwise."""
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}, "monitors": [{"name": "ignored"}]}, f)
    with pytest.raises(ConfigError, match="'monitors'.*belongs in monitors.yaml"):
        load_config(str(config_path))


# --- Email alert required-field validation --------------------------------------------


def test_email_on_without_server_settings_is_rejected(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "alerts.yaml", "w") as f:
        yaml.dump({"alerts": {"email": {"recipients": [
            {"to": ["a@example.com"], "monitors": "all"},
        ]}}}, f)
    with pytest.raises(ConfigError, match="smtp_host"):
        load_config(str(config_path))


def test_email_with_every_entry_off_skips_server_settings(tmp_path):
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump({"global": {}}, f)
    with open(tmp_path / "alerts.yaml", "w") as f:
        yaml.dump({"alerts": {"email": {"recipients": [dict(_OFF_GROUP)]}}}, f)
    config = load_config(str(config_path))  # must not raise
    assert config["alerts"]["email"]["recipients"][0]["enabled"] is False


def _email_alert_config(tmp_path, to=None, **email):
    """Write an alerts.yaml with an email alert that has one recipients entry
    (`to` is that entry's address list)."""
    import yaml
    (tmp_path / "config.yaml").write_text("global: {}\n")
    settings = {
        "smtp_host": "smtp.example.com", "smtp_port": 587,
        "from": "alerts@example.com",
        "recipients": [{"to": to or ["you@example.com"], "monitors": "all"}],
    }
    settings.update(email)
    (tmp_path / "alerts.yaml").write_text(
        yaml.dump({"alerts": {"email": settings}}, allow_unicode=True)
    )
    return str(tmp_path / "config.yaml")


def test_email_alert_valid_addresses_are_accepted(tmp_path):
    config = load_config(_email_alert_config(
        tmp_path, to=["you@example.com", "a.b+tag@sub.example.co.uk"],
    ))
    recipient = config["alerts"]["email"]["recipients"][0]
    assert recipient["to"][1] == "a.b+tag@sub.example.co.uk"


@pytest.mark.parametrize("value", [
    "alerts", "alerts@localhost", "Alerts <alerts@example.com>",
    "alerts@example.com ", "m\u00e9@example.com", 5,
])
def test_email_alert_from_must_be_an_email_address(tmp_path, value):
    with pytest.raises(ConfigError, match="alerts.email.from.*email address"):
        load_config(_email_alert_config(tmp_path, **{"from": value}))


@pytest.mark.parametrize("to", [
    ["not-an-email"],
    ["you@example.com", "oncall@localhost"],
    ["you@example.com", "you@example.com\nBcc: x@example.com"],
    ["you@example.com", ""],
    ["you@example.com", 5],
    [["you@example.com"]],
])
def test_email_alert_every_to_entry_must_be_an_email_address(tmp_path, to):
    with pytest.raises(
        ConfigError, match=r"alerts.email.recipients\[0\].to.*email addresses",
    ):
        load_config(_email_alert_config(tmp_path, to=to))


@pytest.mark.parametrize("host", [
    "smtp.example.com", "mail", "192.0.2.10", "my_relay",
])
def test_email_alert_smtp_host_valid_values_are_accepted(tmp_path, host):
    config = load_config(_email_alert_config(tmp_path, smtp_host=host))
    assert config["alerts"]["email"]["smtp_host"] == host


@pytest.mark.parametrize("host", [
    "smtp example.com", "smtp.example.com.", "smtp.example.com:587",
    "http://smtp.example.com", "127.1", "-bad.example.com", 587,
])
def test_email_alert_smtp_host_must_be_a_hostname_or_ip(tmp_path, host):
    with pytest.raises(ConfigError, match="alerts.email.smtp_host.*hostname"):
        load_config(_email_alert_config(tmp_path, smtp_host=host))


@pytest.mark.parametrize("port, expected", [(587, 587), (25, 25), (65535, 65535), ("587", 587)])
def test_email_alert_smtp_port_valid_values_are_accepted(tmp_path, port, expected):
    config = load_config(_email_alert_config(tmp_path, smtp_port=port))
    assert config["alerts"]["email"]["smtp_port"] == expected


@pytest.mark.parametrize("port", ["abc", 65536, -1, 587.5, "587.5", True])
def test_email_alert_smtp_port_must_be_a_port_number(tmp_path, port):
    with pytest.raises(ConfigError, match="alerts.email.smtp_port"):
        load_config(_email_alert_config(tmp_path, smtp_port=port))


def _write_alerts(tmp_path, alerts_yaml):
    (tmp_path / "config.yaml").write_text("global: {}\n")
    (tmp_path / "alerts.yaml").write_text(alerts_yaml)
    return str(tmp_path / "config.yaml")


def test_email_recipients_entry_without_enabled_is_on(tmp_path):
    path = _write_alerts(tmp_path, (
        "alerts:\n  email:\n    smtp_host: smtp.example.com\n    smtp_port: 587\n"
        "    from: alerts@example.com\n    recipients:\n"
        "      - to: [you@example.com]\n        monitors: all\n"
    ))
    entry = load_config(path)["alerts"]["email"]["recipients"][0]
    assert entry.get("enabled", True) is True


def test_email_recipients_entry_that_says_enabled_false_is_off(tmp_path):
    path = _write_alerts(tmp_path, (
        "alerts:\n  email:\n    recipients:\n"
        "      - to: [you@example.com]\n        enabled: false\n"
    ))
    assert load_config(path)["alerts"]["email"]["recipients"][0]["enabled"] is False


def test_email_with_no_recipients_is_off(tmp_path):
    for yaml_text in ("alerts:\n  push_alerts: []\n",
                      "alerts:\n  email:\n    recipients: []\n"):
        path = _write_alerts(tmp_path, yaml_text)
        assert load_config(path)["alerts"]["email"]["recipients"] == []


def test_email_recipients_entry_enabled_text_false_is_converted(tmp_path):
    path = _write_alerts(tmp_path, (
        'alerts:\n  email:\n    recipients:\n'
        '      - to: [you@example.com]\n        enabled: "false"\n'
    ))
    assert load_config(path)["alerts"]["email"]["recipients"][0]["enabled"] is False


def test_email_recipients_entry_enabled_rejects_other_values(tmp_path):
    path = _write_alerts(tmp_path, (
        "alerts:\n  email:\n    recipients:\n"
        "      - to: [you@example.com]\n        enabled: maybe\n"
    ))
    with pytest.raises(ConfigError, match=r"recipients\[0\]\.enabled.*true or false"):
        load_config(path)


def test_email_alert_recipients_must_be_a_list_of_mappings(tmp_path):
    with pytest.raises(ConfigError, match=r"alerts.email.recipients' must be a list"):
        load_config(_email_alert_config(tmp_path, recipients="you@example.com"))
    with pytest.raises(ConfigError, match=r"recipients\[0\] must be a mapping"):
        load_config(_email_alert_config(tmp_path, recipients=["you@example.com"]))


def test_email_alert_recipients_entry_needs_a_to_list(tmp_path):
    with pytest.raises(ConfigError, match=r"recipients\[0\] is missing the required 'to'"):
        load_config(_email_alert_config(
            tmp_path, recipients=[{"monitors": "all"}],
        ))
    with pytest.raises(ConfigError, match=r"recipients\[0\].to' must be a list"):
        load_config(_email_alert_config(
            tmp_path, recipients=[{"to": "you@example.com", "monitors": "all"}],
        ))


def test_email_alert_recipients_each_choose_their_own_monitors(tmp_path):
    config = load_config(_email_alert_config(tmp_path, recipients=[
        {"to": ["a@example.com"], "monitors": "all"},
        {"to": ["b@example.com", "c@example.com"], "monitors": "all",
         "system": False},
    ]))
    recipients = config["alerts"]["email"]["recipients"]
    assert [r["to"] for r in recipients] == [
        ["a@example.com"], ["b@example.com", "c@example.com"],
    ]


def test_email_alert_recipients_entry_system_must_be_a_bool(tmp_path):
    with pytest.raises(ConfigError, match=r"recipients\[1\].system must be true or false"):
        load_config(_email_alert_config(tmp_path, recipients=[
            {"to": ["a@example.com"], "monitors": "all"},
            {"to": ["b@example.com"], "monitors": "all", "system": "no"},
        ]))


def test_email_alert_recipients_entry_needs_monitors_or_tags(tmp_path):
    with pytest.raises(
        ConfigError,
        match=r"Email alert recipients\[1\] doesn't say which monitors it is for",
    ):
        load_config(_email_alert_config(tmp_path, recipients=[
            {"to": ["a@example.com"], "monitors": "all"},
            {"to": ["b@example.com"]},
        ]))


def test_email_alert_to_addresses_are_checked_even_when_the_entry_is_off(tmp_path):
    with pytest.raises(ConfigError, match=r"recipients\[0\].to.*email addresses"):
        load_config(_email_alert_config(tmp_path, recipients=[
            {"to": ["nope"], "enabled": False},
        ]))


def test_email_from_and_server_are_not_checked_when_every_entry_is_off(tmp_path):
    config = load_config(_email_alert_config(
        tmp_path, recipients=[dict(_OFF_GROUP)],
        smtp_host="not a host", smtp_port="abc", **{"from": "nope"},
    ))
    assert config["alerts"]["email"]["recipients"][0]["enabled"] is False


# --- Theme value validation -----------------------------------------------------------


def test_theme_invalid_color_falls_back_to_default(tmp_path):
    import yaml
    theme_path = tmp_path / "theme.yaml"
    with open(theme_path, "w") as f:
        yaml.dump({"backgrounds": {"page": "not-a-real-color"}}, f)
    theme = load_theme(str(theme_path))
    assert theme["backgrounds"]["page"] == "#161616"  # default, not the bad value


def test_theme_non_string_color_falls_back_to_default(tmp_path):
    """A YAML value that parses to a non-string (e.g. a bare number) for a
    color field must fall back rather than being interpolated into CSS."""
    import yaml
    theme_path = tmp_path / "theme.yaml"
    with open(theme_path, "w") as f:
        yaml.dump({"backgrounds": {"page": 141414}}, f)
    theme = load_theme(str(theme_path))
    assert theme["backgrounds"]["page"] == "#161616"


def test_theme_valid_hex_color_accepted(tmp_path):
    import yaml
    theme_path = tmp_path / "theme.yaml"
    with open(theme_path, "w") as f:
        yaml.dump({"backgrounds": {"page": "#abc123"}}, f)
    theme = load_theme(str(theme_path))
    assert theme["backgrounds"]["page"] == "#abc123"


def test_theme_invalid_border_style_falls_back_to_default(tmp_path):
    import yaml
    theme_path = tmp_path / "theme.yaml"
    with open(theme_path, "w") as f:
        yaml.dump({"links": {"hover_line": "javascript:alert(1)"}}, f)
    theme = load_theme(str(theme_path))
    # Falls back to whatever the real default border style is, rather
    # than interpolating the unrecognized/unsafe value into CSS.
    from src.config import THEME_DEFAULTS
    assert theme["links"]["hover_line"] == THEME_DEFAULTS["links"]["hover_line"]


# --- get_display_tz -------------------------------------------------------------------


def test_get_display_tz_valid():
    from zoneinfo import ZoneInfo

    from src.config import get_display_tz
    tz = get_display_tz({"global": {"timezone": "America/Chicago"}})
    assert tz == ZoneInfo("America/Chicago")


def test_get_display_tz_invalid_falls_back_to_utc():
    from zoneinfo import ZoneInfo

    from src.config import get_display_tz
    tz = get_display_tz({"global": {"timezone": "Not/A_Real_Zone"}})
    assert tz == ZoneInfo("UTC")


def test_get_display_tz_missing_defaults_to_utc():
    from zoneinfo import ZoneInfo

    from src.config import get_display_tz
    assert get_display_tz({"global": {}}) == ZoneInfo("UTC")


# --- The actual shipped *.default.yaml files ------------------------------------------

# These aren't just docs -- config.default.yaml and theme.default.yaml get
# COPYed directly into the Docker image (renamed to config.yaml/theme.yaml)
# in every build, and the other four are the copy-paste starting point a new
# user copies into monitors.yaml/alerts.yaml/tags.yaml/maintenance.yaml.
# A typo in one of them would ship straight to every new install and every
# image rebuild, undetected until someone's container fails to start, so
# these tests check that every one of them parses.

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shipped_config_default_and_theme_default_load_as_baked_into_docker(
    tmp_path,
):
    """Mirrors the Dockerfile exactly: COPY config.default.yaml config.yaml
    + COPY theme.default.yaml theme.yaml, then load both for real."""
    shutil.copy(_REPO_ROOT / "config.default.yaml", tmp_path / "config.yaml")
    shutil.copy(_REPO_ROOT / "theme.default.yaml", tmp_path / "theme.yaml")

    config = load_config(str(tmp_path / "config.yaml"))  # must not raise
    assert "global" in config

    theme = load_theme(str(tmp_path / "theme.yaml"))  # must not raise
    assert theme["backgrounds"]["page"]


def test_shipped_default_yaml_files_all_load_as_a_full_fresh_install(
    tmp_path,
):
    """Mirrors what a brand-new user does: cp every
    *.default.yaml to its real name, then start the app. Loads all six
    together, not just config.yaml/theme.yaml in isolation, since a
    valid config.yaml alongside a broken monitors.yaml would still crash
    a real install."""
    mapping = {
        "config.default.yaml": "config.yaml",
        "monitors.default.yaml": "monitors.yaml",
        "alerts.default.yaml": "alerts.yaml",
        "tags.default.yaml": "tags.yaml",
        "maintenance.default.yaml": "maintenance.yaml",
        "theme.default.yaml": "theme.yaml",
    }
    for src_name, dest_name in mapping.items():
        shutil.copy(_REPO_ROOT / src_name, tmp_path / dest_name)

    config = load_config(str(tmp_path / "config.yaml"))  # must not raise
    # The shipped monitors file has one live monitor, the app's own health page
    assert [m["name"] for m in config["monitors"]] == ["Uptime Herald"]
    assert config["alerts"]["email"]["recipients"] == []
    assert config["tags"] == []
    assert config["maintenance"] == []

    theme = load_theme(str(tmp_path / "theme.yaml"))  # must not raise
    assert theme["backgrounds"]["page"]


# --- Config load boundary: every failure must be a ConfigError ------------------------
#
# dashboard()/kiosk()/monitor_detail() turn ConfigError into the
# operator-facing 503 page and the JSON routes turn it into a 503 body;
# anything else escapes as a bare 500 with nothing to act on. These pin
# the shapes that could otherwise escape.


def _minimal_config(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  check_interval_minutes: 5\n")
    return tmp_path / "config.yaml"


@pytest.mark.parametrize("body", ["- one\n- two\n", "just a string\n"])
def test_config_yaml_wrong_top_level_shape_is_config_error(tmp_path, body):
    """A config.yaml that parses to a list or scalar must raise ConfigError,
    not a raw AttributeError ('list' object has no attribute 'items') from
    _deep_merge."""
    (tmp_path / "config.yaml").write_text(body)
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(str(tmp_path / "config.yaml"))


def test_config_file_present_as_directory_is_config_error(tmp_path):
    """Docker auto-creates a missing bind-mount source as an empty
    DIRECTORY. Path.exists() is True for it, and open() then raises
    IsADirectoryError -- an OSError, not a YAMLError."""
    config_path = _minimal_config(tmp_path)
    (tmp_path / "monitors.yaml").mkdir()
    with pytest.raises(ConfigError, match="Could not read monitors.yaml"):
        load_config(str(config_path))


def test_load_theme_returns_defaults_for_wrong_shape(tmp_path):
    """load_theme documents "missing or invalid -> defaults", and it is
    called from _render_config_error(), so anything escaping it 500s the
    page meant to explain a config problem."""
    theme = tmp_path / "theme.yaml"
    theme.write_text("- red\n- blue\n")
    assert load_theme(str(theme))["backgrounds"]["page"]


def test_load_theme_returns_defaults_for_directory(tmp_path):
    (tmp_path / "theme.yaml").mkdir()
    assert load_theme(str(tmp_path / "theme.yaml"))["backgrounds"]["page"]


# --- Bounds that the JSON endpoints also enforce --------------------------------------


@pytest.mark.parametrize("field,value", [
    ("checks_per_page", 2000),
    ("latency_graph_days", 4000),
])
def test_global_field_above_api_ceiling_is_rejected(tmp_path, field, value):
    """Above these the dashboard's own API requests are rejected as 422
    and the page shows only a generic load failure."""
    (tmp_path / "config.yaml").write_text(f"global:\n  {field}: {value}\n")
    with pytest.raises(ConfigError, match="must be at most"):
        load_config(str(tmp_path / "config.yaml"))


@pytest.mark.parametrize("value", [30, 29, 7, 1, 0])
def test_stats_period_days_of_30_or_less_is_rejected(tmp_path, value):
    """The dashboard always shows 24h, 7d and 30d cards; the configured
    period is the extra one, so it has to be longer than 30."""
    (tmp_path / "config.yaml").write_text(
        f"global:\n  stats_period_days: {value}\n"
    )
    with pytest.raises(ConfigError, match="stats_period_days.*at least 31"):
        load_config(str(tmp_path / "config.yaml"))


def test_stats_period_days_of_31_is_accepted(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 31\n")
    cfg = load_config(str(tmp_path / "config.yaml"))
    assert cfg["global"]["stats_period_days"] == 31


# --- Retention has to cover the windows that read from it -----------------------------


def _write_retention_config(tmp_path, global_lines, monitor_lines=""):
    (tmp_path / "config.yaml").write_text("global:\n" + global_lines)
    (tmp_path / "monitors.yaml").write_text(
        "monitors:\n"
        "  - name: m1\n    type: tcp\n    host: h\n    port: 80\n" + monitor_lines
    )
    return str(tmp_path / "config.yaml")


def test_global_retention_shorter_than_stats_period_is_rejected(tmp_path):
    path = _write_retention_config(
        tmp_path, "  retention_days: 60\n  stats_period_days: 90\n"
    )
    with pytest.raises(ConfigError, match=r"global\.retention_days.*stats_period_days \(90\)"):
        load_config(path)


def test_global_retention_shorter_than_latency_graph_is_rejected(tmp_path):
    path = _write_retention_config(
        tmp_path,
        "  retention_days: 90\n  stats_period_days: 40\n  latency_graph_days: 91\n",
    )
    with pytest.raises(ConfigError, match=r"global\.retention_days.*latency_graph_days \(91\)"):
        load_config(path)


def test_retention_equal_to_both_windows_is_accepted(tmp_path):
    path = _write_retention_config(
        tmp_path,
        "  retention_days: 90\n  stats_period_days: 90\n  latency_graph_days: 90\n",
    )
    assert load_config(path)["global"]["retention_days"] == 90


@pytest.mark.parametrize("value", [0, -1])
def test_global_retention_of_zero_or_less_is_rejected(tmp_path, value):
    path = _write_retention_config(tmp_path, f"  retention_days: {value}\n")
    with pytest.raises(ConfigError, match=r"global\.retention_days.*stats_period_days"):
        load_config(path)


def test_global_retention_at_the_cap_is_accepted(tmp_path):
    path = _write_retention_config(tmp_path, "  retention_days: 3650\n")
    assert load_config(path)["global"]["retention_days"] == 3650


def test_global_retention_above_the_cap_is_rejected(tmp_path):
    path = _write_retention_config(tmp_path, "  retention_days: 3651\n")
    with pytest.raises(ConfigError, match=r"global\.retention_days.*at most 3650"):
        load_config(path)


def test_stats_period_at_the_cap_is_accepted(tmp_path):
    path = _write_retention_config(
        tmp_path, "  retention_days: 3650\n  stats_period_days: 3650\n"
    )
    assert load_config(path)["global"]["stats_period_days"] == 3650


def test_stats_period_above_the_cap_is_rejected(tmp_path):
    """Retention can't exceed 3650, so a longer stats period could never be
    satisfied -- name stats_period_days, not retention_days."""
    path = _write_retention_config(
        tmp_path, "  retention_days: 3650\n  stats_period_days: 3651\n"
    )
    with pytest.raises(ConfigError, match=r"global\.stats_period_days.*at most 3650"):
        load_config(path)


def test_monitor_retention_shorter_than_stats_period_is_rejected(tmp_path):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", "    retention_days: 45\n"
    )
    with pytest.raises(ConfigError, match=r"monitors\[m1\]\.retention_days.*stats_period_days"):
        load_config(path)


def test_monitor_retention_shorter_than_latency_graph_is_rejected(tmp_path):
    path = _write_retention_config(
        tmp_path,
        "  retention_days: 400\n  stats_period_days: 31\n  latency_graph_days: 365\n",
        "    retention_days: 100\n",
    )
    with pytest.raises(ConfigError, match=r"monitors\[m1\]\.retention_days.*latency_graph_days"):
        load_config(path)


@pytest.mark.parametrize("value", [0, -1])
def test_monitor_retention_of_zero_or_less_is_rejected(tmp_path, value):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", f"    retention_days: {value}\n"
    )
    with pytest.raises(ConfigError, match=r"monitors\[m1\]\.retention_days.*stats_period_days"):
        load_config(path)


def test_monitor_retention_above_the_cap_is_rejected(tmp_path):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", "    retention_days: 3651\n"
    )
    with pytest.raises(ConfigError, match=r"monitors\[m1\]\.retention_days.*at most 3650"):
        load_config(path)


@pytest.mark.parametrize("value", [0, -1])
def test_kiosk_refresh_minutes_below_1_is_rejected(tmp_path, value):
    (tmp_path / "config.yaml").write_text(
        f"global:\n  kiosk_refresh_minutes: {value}\n"
    )
    with pytest.raises(ConfigError, match="kiosk_refresh_minutes.*at least 1"):
        load_config(str(tmp_path / "config.yaml"))


# --- Settings where 0 is real but a negative value is a typo --------------------------


@pytest.mark.parametrize("field", [
    "ui_refresh_minutes", "stagger_seconds",
    "alert_retries", "cleanup_grace_minutes",
])
def test_global_negative_value_is_rejected(tmp_path, field):
    (tmp_path / "config.yaml").write_text(f"global:\n  {field}: -1\n")
    with pytest.raises(ConfigError, match=f"global.{field}.*0 or more"):
        load_config(str(tmp_path / "config.yaml"))


@pytest.mark.parametrize("field", [
    "ui_refresh_minutes", "stagger_seconds",
    "alert_retries", "cleanup_grace_minutes",
])
def test_global_zero_is_accepted(tmp_path, field):
    (tmp_path / "config.yaml").write_text(f"global:\n  {field}: 0\n")
    assert load_config(str(tmp_path / "config.yaml"))["global"][field] == 0


@pytest.mark.parametrize("field", ["alert_retries"])
def test_monitor_negative_override_is_rejected(tmp_path, field):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", f"    {field}: -1\n")
    with pytest.raises(ConfigError, match=f"monitors.m1..{field}.*0 or more"):
        load_config(path)


@pytest.mark.parametrize("field", ["alert_retries"])
def test_monitor_zero_override_is_accepted(tmp_path, field):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", f"    {field}: 0\n")
    assert load_config(path)["monitors"][0][field] == 0


@pytest.mark.parametrize("value", [0, -1])
def test_alert_cooldown_minutes_below_1_is_rejected(tmp_path, value):
    (tmp_path / "config.yaml").write_text(
        f"global:\n  alert_cooldown_minutes: {value}\n"
    )
    with pytest.raises(ConfigError, match="alert_cooldown_minutes.*at least 1"):
        load_config(str(tmp_path / "config.yaml"))


def test_alert_cooldown_minutes_of_1_is_accepted(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  alert_cooldown_minutes: 1\n")
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["global"]["alert_cooldown_minutes"] == 1


@pytest.mark.parametrize("value", [0, -1])
def test_monitor_alert_cooldown_minutes_below_1_is_rejected(tmp_path, value):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n",
        f"    alert_cooldown_minutes: {value}\n",
    )
    with pytest.raises(ConfigError, match=r"m1.*alert_cooldown_minutes.*at least 1"):
        load_config(path)


@pytest.mark.parametrize("field", ["ui_refresh_minutes", "kiosk_refresh_minutes"])
def test_refresh_minutes_at_the_cap_is_accepted(tmp_path, field):
    (tmp_path / "config.yaml").write_text(f"global:\n  {field}: 1440\n")
    assert load_config(str(tmp_path / "config.yaml"))["global"][field] == 1440


@pytest.mark.parametrize("field", ["ui_refresh_minutes", "kiosk_refresh_minutes"])
def test_refresh_minutes_above_the_cap_is_rejected(tmp_path, field):
    (tmp_path / "config.yaml").write_text(f"global:\n  {field}: 1441\n")
    with pytest.raises(ConfigError, match=f"global.{field}.*at most 1440"):
        load_config(str(tmp_path / "config.yaml"))


# --- On/off settings must be true or false --------------------------------------------

_BOOLEAN_GLOBAL = [
    "pin_down_monitors", "cleanup_removed_monitors", "retry_on_up",
    "view_all_checks",
]


@pytest.mark.parametrize("field", _BOOLEAN_GLOBAL)
@pytest.mark.parametrize("value", ["maybe", "5", "''"])
def test_global_boolean_rejects_anything_else(tmp_path, field, value):
    (tmp_path / "config.yaml").write_text(f"global:\n  {field}: {value}\n")
    with pytest.raises(ConfigError, match=f"global.{field}.*true or false"):
        load_config(str(tmp_path / "config.yaml"))


@pytest.mark.parametrize("field", _BOOLEAN_GLOBAL)
@pytest.mark.parametrize("text,expected", [
    ("true", True), ("false", False), ('"false"', False), ('"True"', True),
])
def test_global_boolean_accepts_true_false_and_their_text_forms(
    tmp_path, field, text, expected
):
    """Quoted text (what ${ENV_VAR} substitution produces) is converted,
    so a quoted "false" is false and not truthy text."""
    (tmp_path / "config.yaml").write_text(f"global:\n  {field}: {text}\n")
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["global"][field] is expected


@pytest.mark.parametrize("field", ["active", "retry_on_up"])
def test_monitor_boolean_rejects_anything_else(tmp_path, field):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", f"    {field}: maybe\n"
    )
    with pytest.raises(ConfigError, match=rf"m1.*{field}.*true or false"):
        load_config(path)


@pytest.mark.parametrize("field", ["active", "retry_on_up"])
def test_monitor_boolean_text_form_is_converted(tmp_path, field):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", f'    {field}: "false"\n'
    )
    assert load_config(path)["monitors"][0][field] is False


@pytest.mark.parametrize("text,expected", [
    ("false", False), ('"false"', False), ("true", True),
])
def test_monitor_verify_ssl_boolean_forms(tmp_path, text, expected):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(
        "monitors:\n  - name: m1\n    type: http\n    url: http://x\n"
        f"    verify_ssl: {text}\n"
    )
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["monitors"][0]["verify_ssl"] is expected


def test_monitor_verify_ssl_rejects_other_values(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(
        "monitors:\n  - name: m1\n    type: http\n    url: http://x\n"
        "    verify_ssl: maybe\n"
    )
    with pytest.raises(ConfigError, match=r"m1.*verify_ssl.*true or false"):
        load_config(str(tmp_path / "config.yaml"))


def test_push_alert_enabled_text_false_is_converted(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "alerts.yaml").write_text(
        'alerts:\n  push_alerts:\n    - type: generic\n      url: "https://x"\n'
        '      enabled: "false"\n'
    )
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["alerts"]["push_alerts"][0]["enabled"] is False


def test_push_alert_enabled_rejects_other_values(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "alerts.yaml").write_text(
        'alerts:\n  push_alerts:\n    - type: generic\n      url: "https://x"\n'
        "      enabled: maybe\n"
    )
    with pytest.raises(ConfigError, match=r"push_alerts\[0\]\.enabled.*true or false"):
        load_config(str(tmp_path / "config.yaml"))


@pytest.mark.parametrize("value", ["24h", "7d", "30d", "period", "none"])
def test_sidebar_uptime_period_valid_values_are_accepted(tmp_path, value):
    (tmp_path / "config.yaml").write_text(
        f'global:\n  sidebar_uptime_period: "{value}"\n'
    )
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["global"]["sidebar_uptime_period"] == value


@pytest.mark.parametrize("value", ['"bogus"', '"24H"', '"90d"', "7", '""'])
def test_sidebar_uptime_period_other_values_are_rejected(tmp_path, value):
    (tmp_path / "config.yaml").write_text(
        f"global:\n  sidebar_uptime_period: {value}\n"
    )
    with pytest.raises(ConfigError, match="sidebar_uptime_period.*must be one of"):
        load_config(str(tmp_path / "config.yaml"))


def test_language_en_is_accepted(tmp_path):
    (tmp_path / "config.yaml").write_text('global:\n  language: "en"\n')
    assert load_config(str(tmp_path / "config.yaml"))["global"]["language"] == "en"


@pytest.mark.parametrize("value", ['"fr"', '"xx"', '"EN"', "5", '""'])
def test_language_without_a_locales_folder_is_rejected(tmp_path, value):
    (tmp_path / "config.yaml").write_text(f"global:\n  language: {value}\n")
    with pytest.raises(ConfigError, match='global.language.*must be one of "en"'):
        load_config(str(tmp_path / "config.yaml"))


@pytest.mark.parametrize("value", [
    "me@example.com", "a.b+tag@sub.example.co.uk", "O'Neil@example.com",
    "x_y-z@my-host.example.org",
    # The longest allowed: 64 + "@" + 189 = 254 characters.
    "x" * 64 + "@" + ".".join(["a" * 63] * 2 + ["b" * 61]),
    # A domain of 252 characters, the longest that fits in 254.
    "x@" + ".".join(["a" * 63] * 3 + ["b" * 60]),
])
def test_contact_email_valid_addresses_are_accepted(tmp_path, value):
    (tmp_path / "config.yaml").write_text(f'global:\n  contact_email: "{value}"\n')
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["global"]["contact_email"] == value


def test_contact_email_unset_is_accepted(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["global"].get("contact_email") is None


@pytest.mark.parametrize("value", [
    "me@localhost", "not-an-email", "@example.com", "me@", "me@@example.com",
    "me@example..com", ".me@example.com", "me@example.com ", "me @example.com",
    "m\u00e9@example.com", "me@-example.com", "me@[192.0.2.1]", "",
    "me`id`@example.com", "me|cat@example.com",
    "x" * 64 + "@" + ".".join(["a" * 63] * 4) + ".com",
    "x" * 64 + "@" + ".".join(["a" * 63] * 2 + ["b" * 62]),  # 255 characters
    "x" * 65 + "@example.com",
])
def test_contact_email_other_values_are_rejected(tmp_path, value):
    import yaml
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"global": {"contact_email": value}}, allow_unicode=True)
    )
    with pytest.raises(ConfigError, match="global.contact_email.*email address"):
        load_config(str(tmp_path / "config.yaml"))


def test_contact_email_listed_with_nothing_after_it_is_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  contact_email:\n")
    with pytest.raises(ConfigError, match="contact_email.*nothing after it"):
        load_config(str(tmp_path / "config.yaml"))


def test_contact_email_with_a_newline_is_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text(
        'global:\n  contact_email: "me@example.com\\nX-Evil: 1"\n'
    )
    with pytest.raises(ConfigError, match="global.contact_email.*email address"):
        load_config(str(tmp_path / "config.yaml"))


@pytest.mark.parametrize("value", [0, -5])
def test_monitor_timeout_below_1_is_rejected(tmp_path, value):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", f"    timeout: {value}\n"
    )
    with pytest.raises(ConfigError, match=r"m1.*timeout.*at least 1"):
        load_config(path)


@pytest.mark.parametrize("value", [1, 10, 30])
def test_monitor_timeout_of_1_or_more_is_accepted(tmp_path, value):
    path = _write_retention_config(
        tmp_path, "  stats_period_days: 90\n", f"    timeout: {value}\n"
    )
    assert load_config(path)["monitors"][0]["timeout"] == value


# --- expected_status must be an actual status code ------------------------------------


def _write_expected_status_config(tmp_path, value):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(
        "monitors:\n  - name: m1\n    type: http\n    url: http://x\n"
        f"    expected_status: {value}\n"
    )
    return str(tmp_path / "config.yaml")


@pytest.mark.parametrize("value", [
    200, 204, 301, 401, 403, 404, 503, 418,   # registered (418 is accepted on purpose)
    218, 444, 499, 521, 530, 599, 999,        # well-known unofficial codes
])
def test_expected_status_actual_codes_are_accepted(tmp_path, value):
    path = _write_expected_status_config(tmp_path, value)
    assert load_config(path)["monitors"][0]["expected_status"] == value


@pytest.mark.parametrize("value", [
    0, -1, 99, 104, 299, 306, 510, 600, 99999, 401.5, '"abc"', '"401.5"',
])
def test_expected_status_other_values_are_rejected(tmp_path, value):
    path = _write_expected_status_config(tmp_path, value)
    with pytest.raises(ConfigError, match="m1.*expected_status"):
        load_config(path)


def test_expected_status_text_number_is_converted(tmp_path):
    path = _write_expected_status_config(tmp_path, '"401"')
    assert load_config(path)["monitors"][0]["expected_status"] == 401


# --- Global timeout -------------------------------------------------------------------


def _write_timeout_config(tmp_path, global_timeout=None, monitor=""):
    global_lines = "global:\n  stats_period_days: 90\n"
    if global_timeout is not None:
        global_lines += f"  timeout: {global_timeout}\n"
    (tmp_path / "config.yaml").write_text(global_lines)
    (tmp_path / "monitors.yaml").write_text(
        "monitors:\n  - name: m1\n    type: ping\n    host: h\n" + monitor
    )
    return str(tmp_path / "config.yaml")


def test_global_timeout_defaults_to_10(tmp_path):
    config = load_config(_write_timeout_config(tmp_path))
    assert config["global"]["timeout"] == 10


@pytest.mark.parametrize("value", [1, 2, 300])
def test_global_timeout_in_range_is_accepted(tmp_path, value):
    config = load_config(_write_timeout_config(tmp_path, value))
    assert config["global"]["timeout"] == value


@pytest.mark.parametrize("value", [0, -1])
def test_global_timeout_below_1_is_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="global.timeout.*at least 1"):
        load_config(_write_timeout_config(tmp_path, value))


@pytest.mark.parametrize("value", [301, 100000])
def test_global_timeout_above_300_is_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="global.timeout.*at most 300"):
        load_config(_write_timeout_config(tmp_path, value))


def test_monitor_timeout_above_300_is_rejected(tmp_path):
    path = _write_timeout_config(tmp_path, monitor="    timeout: 301\n")
    with pytest.raises(ConfigError, match=r"m1.*timeout.*at most 300"):
        load_config(path)


def test_ping_count_is_checked_against_the_global_timeout(tmp_path):
    path = _write_timeout_config(tmp_path, 30, "    count: 20\n")
    assert load_config(path)["monitors"][0]["count"] == 20
    path = _write_timeout_config(tmp_path, 30, "    count: 31\n")
    with pytest.raises(ConfigError, match=r"count 31 but timeout 30"):
        load_config(path)


def test_ping_count_is_checked_against_the_monitor_timeout_when_it_sets_one(tmp_path):
    path = _write_timeout_config(tmp_path, 30, "    timeout: 5\n    count: 6\n")
    with pytest.raises(ConfigError, match=r"count 6 but timeout 5"):
        load_config(path)


# --- Monitor name length --------------------------------------------------------------


def _write_named_monitor_config(tmp_path, name):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(
        f'monitors:\n  - name: "{name}"\n    type: tcp\n    host: h\n    port: 80\n'
    )
    return str(tmp_path / "config.yaml")


def test_monitor_name_of_255_characters_is_accepted(tmp_path):
    path = _write_named_monitor_config(tmp_path, "n" * 255)
    assert len(load_config(path)["monitors"][0]["name"]) == 255


def test_monitor_name_of_256_characters_is_rejected(tmp_path):
    path = _write_named_monitor_config(tmp_path, "n" * 256)
    with pytest.raises(ConfigError, match=r"256 characters.*longest allowed is 255"):
        load_config(path)


# --- host of ping and TCP monitors ----------------------------------------------------


def _write_host_config(tmp_path, monitor_type, host_line):
    extra = "    port: 80\n" if monitor_type == "tcp" else ""
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(
        f"monitors:\n  - name: m1\n    type: {monitor_type}\n{host_line}{extra}"
    )
    return str(tmp_path / "config.yaml")


@pytest.mark.parametrize("monitor_type", ["ping", "tcp"])
@pytest.mark.parametrize("host", [
    "myserver", "my_database", "db1", "db.internal", "Example.COM",
    "1password.example", "a-b.c-d.example", "_dmarc.example.com",
    "b\u00fccher.test", "xn--bcher-kva.test",          # non-ASCII and its xn-- form
    "stra\u00dfe.de", "\u03b2\u03cc\u03bb\u03bf\u03c2.com", "M\u00fcller.DE",   # ß, final sigma, capitals
    "192.168.1.42", "::1", "2001:db8::1", "fe80::1%eth0",
    "x" * 63 + ".com",
])
def test_host_valid_values_are_accepted(tmp_path, monitor_type, host):
    path = _write_host_config(tmp_path, monitor_type, f'    host: "{host}"\n')
    assert load_config(path)["monitors"][0]["host"] == host


@pytest.mark.parametrize("monitor_type", ["ping", "tcp"])
@pytest.mark.parametrize("host", [
    '""', "5", '"5"',
    '"127.1"', '"127.0.1"', '"2130706433"', '"0x7f.1"',   # shortened IPv4 forms
    '"192.168.1"', '"1.2.3.4.5"', '"999.1.1.1"', '"example.123"',
    '"example.com."', '"my server"', '" myserver"', '"myserver "',
    '"a..b"', '".a"', '"-a.com"', '"a-.com"',
    '"\U0001F600.example"', '"a\U0001F600b.test"', '"ex\u2122ample.com"',   # emoji, symbols
    '"my_s\u00ebrver.example"',                          # underscore with non-ASCII letters
    '"[::1]"', '"user@host"', '"http://x.com"',
    '"' + "x" * 64 + '.com"', '"' + ".".join(["a" * 63] * 4 + ["b"]) + '"',
])
def test_host_other_values_are_rejected(tmp_path, monitor_type, host):
    path = _write_host_config(tmp_path, monitor_type, f"    host: {host}\n")
    with pytest.raises(ConfigError, match=r"m1.*host"):
        load_config(path)


def test_host_with_a_newline_is_rejected(tmp_path):
    path = _write_host_config(tmp_path, "tcp", '    host: "a\\nb"\n')
    with pytest.raises(ConfigError, match=r"m1.*host"):
        load_config(path)


def test_host_on_an_http_monitor_is_rejected(tmp_path):
    """http monitors use url, not host. A stray host would be ignored, so a
    monitor meant as ping or tcp would quietly check a url instead."""
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(
        "monitors:\n  - name: m1\n    type: http\n    url: http://x\n"
        '    host: "my server"\n'
    )
    with pytest.raises(ConfigError, match="'host' only applies to ping and tcp"):
        load_config(str(tmp_path / "config.yaml"))


# --- SMTP credential from the environment ---------------------------------------------


def _email_login_from(tmp_path, alerts_yaml=""):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    if alerts_yaml:
        (tmp_path / "alerts.yaml").write_text(alerts_yaml)
    config = load_config(str(tmp_path / "config.yaml"))
    email = config["alerts"]["email"]
    return email.get("smtp_user"), email.get("smtp_password")


_EMAIL_OFF = "alerts:\n  email:\n    recipients: []\n"


def test_smtp_login_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "user-env")
    monkeypatch.setenv("SMTP_PASSWORD", "pass-env")
    assert _email_login_from(tmp_path, _EMAIL_OFF) == ("user-env", "pass-env")


def test_smtp_login_from_the_environment_works_without_an_alerts_file(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SMTP_USER", "user-env")
    monkeypatch.setenv("SMTP_PASSWORD", "pass-env")
    assert _email_login_from(tmp_path) == ("user-env", "pass-env")


@pytest.mark.parametrize("value", [None, ""])
def test_smtp_login_stays_unset_when_the_environment_has_none(
    tmp_path, monkeypatch, value
):
    """Compose passes an unset SMTP_USER / SMTP_PASSWORD in as an empty string."""
    for name in ("SMTP_USER", "SMTP_PASSWORD"):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert _email_login_from(tmp_path, _EMAIL_OFF) == (None, None)


def test_smtp_user_alone_in_the_environment_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "user-env")
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    assert _email_login_from(tmp_path, _EMAIL_OFF) == ("user-env", None)


@pytest.mark.parametrize("name,env", [
    ("smtp_user", "SMTP_USER"), ("smtp_password", "SMTP_PASSWORD"),
])
def test_smtp_login_in_the_yaml_is_rejected(tmp_path, monkeypatch, name, env):
    """These two are not settings: the credential comes from .env. A value
    left in the file is an error that says so."""
    monkeypatch.setenv("SMTP_USER", "user-env")
    monkeypatch.setenv("SMTP_PASSWORD", "pass-env")
    yaml_text = _EMAIL_OFF + f'    {name}: "from-yaml"\n'
    with pytest.raises(ConfigError, match=f"'{name}' — use {env} in your .env file"):
        _email_login_from(tmp_path, yaml_text)


# --- Monitor credentials (credentials.ini) --------------------------------------------

_GOOD_CREDENTIAL = "[wiki-login]\nusername = monitor\npassword = p@ss w0rd#1;x%y $z \"q\" =end\n"
_CRED_MONITOR = (
    'monitors:\n  - name: m1\n    type: http\n    url: "https://example.com/admin"\n'
    '    credentials: "wiki-login"\n'
)


def _credentials_config(tmp_path, ini=None, monitors=_CRED_MONITOR):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(monitors)
    if ini is not None:
        (tmp_path / "credentials.ini").write_text(ini)
    return str(tmp_path / "config.yaml")


def test_credentials_are_read_exactly_as_written(tmp_path):
    config = load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL))
    assert config["credentials"] == {
        "wiki-login": {"username": "monitor", "password": 'p@ss w0rd#1;x%y $z "q" =end'}
    }


def test_credentials_are_not_part_of_the_monitor_settings(tmp_path):
    config = load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL))
    assert "password" not in config["monitors"][0]
    assert "username" not in config["monitors"][0]


def test_several_monitors_can_share_one_credential(tmp_path):
    monitors = _CRED_MONITOR + _CRED_MONITOR.split("\n", 1)[1].replace("m1", "m2")
    config = load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL, monitors))
    assert [m["credentials"] for m in config["monitors"]] == ["wiki-login"] * 2


def test_no_credentials_file_is_fine_when_no_monitor_needs_one(tmp_path):
    monitors = 'monitors:\n  - name: m1\n    type: http\n    url: "https://x.com"\n'
    config = load_config(_credentials_config(tmp_path, None, monitors))
    assert config["credentials"] == {}


def test_a_monitor_naming_a_credential_that_does_not_exist_is_rejected(tmp_path):
    monitors = _CRED_MONITOR.replace("wiki-login", "nope")
    with pytest.raises(ConfigError, match=r"m1.*credentials 'nope'.*credentials.ini"):
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL, monitors))


def test_a_monitor_naming_a_credential_with_no_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"m1.*credentials 'wiki-login'"):
        load_config(_credentials_config(tmp_path, None))


@pytest.mark.parametrize("ini", [
    "[wiki-login]\nusername = monitor\n",
    "[wiki-login]\npassword = pw\n",
    "[wiki-login]\n",
])
def test_a_credential_needs_both_keys_written(tmp_path, ini):
    with pytest.raises(ConfigError, match=r"\[wiki-login\] is missing"):
        load_config(_credentials_config(tmp_path, ini))


@pytest.mark.parametrize("ini,expected", [
    ("[wiki-login]\nusername = key\npassword =\n", ("key", "")),
    ("[wiki-login]\nusername =\npassword = pw\n", ("", "pw")),
    ("[wiki-login]\nusername =\npassword =\n", ("", "")),
])
def test_either_value_may_be_left_empty_on_purpose(tmp_path, ini, expected):
    credential = load_config(_credentials_config(tmp_path, ini))["credentials"]["wiki-login"]
    assert (credential["username"], credential["password"]) == expected


def test_an_unknown_setting_in_a_credential_is_rejected(tmp_path):
    ini = _GOOD_CREDENTIAL + "token = abc\n"
    with pytest.raises(ConfigError, match=r"unknown setting 'token'"):
        load_config(_credentials_config(tmp_path, ini))


def test_a_credential_defined_twice_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"\[wiki-login\] more than once"):
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL + _GOOD_CREDENTIAL))


def test_credential_labels_are_not_case_sensitive(tmp_path):
    """`Wiki-Login` on a monitor finds [wiki-login] in the file, and the other way round."""
    for written, used in (("[WIKI-login]", "wiki-login"), ("[wiki-login]", "Wiki-Login")):
        ini = _GOOD_CREDENTIAL.replace("[wiki-login]", written)
        monitors = _CRED_MONITOR.replace('"wiki-login"', f'"{used}"')
        config = load_config(_credentials_config(tmp_path, ini, monitors))
        assert config["monitors"][0]["credentials"] == "wiki-login"
        assert config["credentials"]["wiki-login"]["username"] == "monitor"


def test_labels_that_differ_only_in_case_are_rejected(tmp_path):
    ini = _GOOD_CREDENTIAL + _GOOD_CREDENTIAL.replace("wiki-login", "Wiki-Login")
    with pytest.raises(ConfigError, match=r"\[wiki-login\] and \[Wiki-Login\].*same label"):
        load_config(_credentials_config(tmp_path, ini))


@pytest.mark.parametrize("written, message", [
    ("[ ]", "blank label"),
    ("[ wiki-login]", "starts or ends with a space"),
    ("[wiki-login ]", "starts or ends with a space"),
    ("[wiki\tlogin]", "tab or another control character"),
])
def test_a_label_follows_the_monitor_name_rules(tmp_path, written, message):
    ini = _GOOD_CREDENTIAL.replace("[wiki-login]", written)
    with pytest.raises(ConfigError, match=message):
        load_config(_credentials_config(tmp_path, ini))


def test_an_empty_label_is_rejected(tmp_path):
    ini = _GOOD_CREDENTIAL.replace("[wiki-login]", "[]")
    with pytest.raises(ConfigError, match=r"not valid near line"):
        load_config(_credentials_config(tmp_path, ini))


def test_a_setting_given_twice_is_rejected(tmp_path):
    ini = "[wiki-login]\nusername = a\nusername = b\npassword = c\n"
    with pytest.raises(ConfigError, match=r"'username' twice"):
        load_config(_credentials_config(tmp_path, ini))


@pytest.mark.parametrize("ini", [
    "username = a\npassword = SECRETPASS123\n",       # no [label] line
    "[wiki-login]\nusername = a\nSECRETPASS123\n",   # not key = value
])
def test_a_badly_formed_file_is_rejected_without_showing_its_text(tmp_path, ini):
    with pytest.raises(ConfigError, match="credentials.ini is not valid") as err:
        load_config(_credentials_config(tmp_path, ini))
    assert "SECRETPASS123" not in str(err.value)


def test_a_section_named_default_is_an_ordinary_credential(tmp_path):
    ini = "[DEFAULT]\nusername = a\npassword = b\n"
    monitors = _CRED_MONITOR.replace("wiki-login", "DEFAULT")
    config = load_config(_credentials_config(tmp_path, ini, monitors))
    assert config["credentials"] == {"default": {"username": "a", "password": "b"}}


@pytest.mark.parametrize("monitor_type_lines", [
    '    type: tcp\n    host: h\n    port: 80\n',
    '    type: ping\n    host: h\n',
])
def test_credentials_on_a_non_http_monitor_are_rejected(tmp_path, monitor_type_lines):
    monitors = 'monitors:\n  - name: m1\n' + monitor_type_lines + '    credentials: "wiki-login"\n'
    with pytest.raises(ConfigError, match=r"m1.*'credentials'.*only applies to http"):
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL, monitors))


@pytest.mark.parametrize("value", ["5", '""'])
def test_a_credentials_label_must_be_text(tmp_path, value):
    monitors = _CRED_MONITOR.replace('"wiki-login"', value)
    with pytest.raises(ConfigError, match=r"m1.*credentials"):
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL, monitors))


@pytest.mark.parametrize("url", [
    "https://bob:pw@example.com/x", "https://bob@example.com/", "http://:pw@example.com/",
])
def test_a_credential_inside_a_url_is_rejected(tmp_path, url):
    monitors = f'monitors:\n  - name: m1\n    type: http\n    url: "{url}"\n'
    with pytest.raises(ConfigError, match=r"m1.*username or password in its url"):
        load_config(_credentials_config(tmp_path, None, monitors))


def test_an_at_sign_in_a_url_path_is_fine(tmp_path):
    monitors = 'monitors:\n  - name: m1\n    type: http\n    url: "https://example.com/u/@me"\n'
    config = load_config(_credentials_config(tmp_path, None, monitors))
    assert config["monitors"][0]["url"] == "https://example.com/u/@me"


def test_a_credential_over_plain_http_is_allowed_and_warned_about_once(tmp_path, caplog):
    import src.config as config_module
    config_module._PLAIN_HTTP_CREDENTIAL_WARNED.clear()
    monitors = _CRED_MONITOR.replace("https://", "http://")
    path = _credentials_config(tmp_path, _GOOD_CREDENTIAL, monitors)
    with caplog.at_level("WARNING", logger="src.config"):
        load_config(path)
        load_config(path)   # the config is re-read constantly; warn only once
    warnings = [r for r in caplog.records if "plain http://" in r.getMessage()]
    assert len(warnings) == 1
    assert "'m1'" in warnings[0].getMessage()
    assert "p@ss" not in warnings[0].getMessage()


def test_a_credential_over_https_is_not_warned_about(tmp_path, caplog):
    import src.config as config_module
    config_module._PLAIN_HTTP_CREDENTIAL_WARNED.clear()
    with caplog.at_level("WARNING", logger="src.config"):
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL))
    assert not [r for r in caplog.records if "plain http://" in r.getMessage()]


def test_plain_http_without_a_credential_is_not_warned_about(tmp_path, caplog):
    import src.config as config_module
    config_module._PLAIN_HTTP_CREDENTIAL_WARNED.clear()
    monitors = 'monitors:\n  - name: m1\n    type: http\n    url: "http://example.com/"\n'
    with caplog.at_level("WARNING", logger="src.config"):
        load_config(_credentials_config(tmp_path, None, monitors))
    assert not [r for r in caplog.records if "plain http://" in r.getMessage()]


def test_a_credential_no_monitor_uses_is_warned_about_once(tmp_path, caplog):
    import src.config as config_module
    config_module._UNUSED_CREDENTIAL_WARNED.clear()
    ini = _GOOD_CREDENTIAL + "[spare]\nusername = spare-user\npassword = spare-pass\n"
    path = _credentials_config(tmp_path, ini)
    with caplog.at_level("WARNING", logger="src.config"):
        load_config(path)
        load_config(path)   # the config is re-read constantly; warn only once
    warnings = [r for r in caplog.records if "not used by any monitor" in r.getMessage()]
    assert len(warnings) == 1
    assert "[spare]" in warnings[0].getMessage()
    assert "spare-user" not in warnings[0].getMessage()
    assert "spare-pass" not in warnings[0].getMessage()


def test_a_credential_that_is_used_is_not_warned_about(tmp_path, caplog):
    import src.config as config_module
    config_module._UNUSED_CREDENTIAL_WARNED.clear()
    with caplog.at_level("WARNING", logger="src.config"):
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL))
    assert not [r for r in caplog.records if "not used by any monitor" in r.getMessage()]


def test_a_credential_warns_again_if_it_becomes_unused_after_being_used(tmp_path, caplog):
    import src.config as config_module
    config_module._UNUSED_CREDENTIAL_WARNED.clear()
    no_credentials = (
        'monitors:\n  - name: m1\n    type: http\n    url: "https://example.com/"\n'
    )
    with caplog.at_level("WARNING", logger="src.config"):
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL))                  # used
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL, no_credentials))  # unused
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL))                  # used
        load_config(_credentials_config(tmp_path, _GOOD_CREDENTIAL, no_credentials))  # unused
    warnings = [r for r in caplog.records if "not used by any monitor" in r.getMessage()]
    assert len(warnings) == 2


# --- url of an http monitor -----------------------------------------------------------


def _write_url_config(tmp_path, url):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(
        f'monitors:\n  - name: m1\n    type: http\n    url: "{url}"\n'
    )
    return str(tmp_path / "config.yaml")


@pytest.mark.parametrize("url", [
    "http://example.com", "https://example.com/", "https://example.com/health?x=1&y=2",
    "http://localhost:8080/health", "http://my_database:3000/", "https://192.168.1.42/",
    "http://[::1]/", "http://[2001:db8::1]:8080/x", "https://example.com:443/",
    "http://b\u00fccher.test/p\u00e4th?q=\u00fc",     # non-ASCII is accepted
    "https://example.com/u/@me", "http://example.com/a%20b", "http://Example.COM/",
    "http://example.com/%F0%9F%98%80", "http://example.com/a-b_c.d~e!$&'()*+,;=:@/?",
    "http://example.com/\u65e5\u672c\u8a9e",
])
def test_url_valid_values_are_accepted(tmp_path, url):
    assert load_config(_write_url_config(tmp_path, url))["monitors"][0]["url"] == url


@pytest.mark.parametrize("url,reason", [
    ("example.com", "must start with http:// or https://"),
    ("ftp://example.com", "must start with http:// or https://"),
    ("HTTP://example.com", "must start with http:// or https://"),
    ("http://", "no host"),
    ("http:///path", "no host"),
    ("http://:80/", "no host"),
    ("http://a b", "space or control character"),
    ("http://example.com/pa th", "space or control character"),
    ("http://example.com/?q=a b", "space or control character"),
    ("http://example.com/#frag", "fragment"),
    ("http://example.com/page#", "fragment"),
    ("http://example.com:/", "port must be a whole number"),
    ("http://example.com:0/", "port must be a whole number"),
    ("http://example.com:99999/", "port must be a whole number"),
    ("http://example.com:abc/", "port must be a whole number"),
    ("http://127.1/", "not a hostname or a full IP address"),
    ("http://1.2.3/", "not a hostname or a full IP address"),
    ("http://example.com./", "not a hostname or a full IP address"),
    ("http://example..com/", "not a hostname or a full IP address"),
    ("http://-a.com/", "not a hostname or a full IP address"),
    ("http://::1/", "needs square brackets"),
    ("http://[::1/", "not a valid url"),
    ("http://[1.2.3.4]/", "not a valid url"),
    ("http://[abc]/", "not a valid url"),
    ("http://[v1.abc]/", "not a valid IPv6 address"),
    ("http://example.com/\U0001F600", "path or query has a character"),
    ("http://example.com/?q=\U0001F600", "path or query has a character"),
    ("http://\U0001F600.example/", "not a hostname or a full IP address"),
    ("http://example.com/a|b", "path or query has a character"),
    ("http://example.com/a{b}", "path or query has a character"),
    ("http://example.com/a^b", "path or query has a character"),
    ("http://example.com/?a[]=1", "path or query has a character"),
    ("http://example.com/a\\\\b", "path or query has a character"),
    ("http://example.com/100%", "path or query has a character"),
    ("http://example.com/%zz", "path or query has a character"),
    ("http://example.com/\u2122", "path or query has a character"),
])
def test_url_other_values_are_rejected(tmp_path, url, reason):
    with pytest.raises(ConfigError, match=rf"m1.*url that is not valid.*{reason}"):
        load_config(_write_url_config(tmp_path, url))


def test_a_tab_or_newline_in_a_url_is_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("global:\n  stats_period_days: 90\n")
    (tmp_path / "monitors.yaml").write_text(
        'monitors:\n  - name: m1\n    type: http\n    url: "http://example.com/a\\tb"\n'
    )
    with pytest.raises(ConfigError, match=r"space or control character"):
        load_config(str(tmp_path / "config.yaml"))


def test_a_url_error_does_not_repeat_the_url(tmp_path):
    """A url can carry a token in its query, and errors are shown on the dashboard."""
    url = "http://example.com:99999/?token=SECRETTOKEN123"
    with pytest.raises(ConfigError) as err:
        load_config(_write_url_config(tmp_path, url))
    assert "SECRETTOKEN123" not in str(err.value)


# --- Push-alert channel validation ----------------------------------------------------


@pytest.mark.parametrize("channel,expected", [
    ({"type": "telgram", "bot_token": "t", "chat_id": "1"}, "unknown type"),
    ({"type": "discrod", "url": "https://x"}, "unknown type"),
    ({"url": "https://x"}, "missing the required 'type'"),
    ({"type": "slack", "url": "not a url"}, "url that is not valid"),
    ({"type": "generic", "url": "ftp://example.com/hook"}, "must start with http"),
    ({"type": "discord", "url": "https://example.com/hook with space"}, "space or control"),
    ({"type": "ntfy", "url": "https://"}, "url that is not valid"),
    ({"type": "slack", "url": 5}, "must start with http"),
    ({"type": "ntfy", "url": "https://ntfy.sh/t", "priority": 5}, "invalid priority 5"),
    ({"type": "ntfy", "url": "https://ntfy.sh/t", "priority": "High"}, "invalid priority 'High'"),
    ({"type": "ntfy", "url": "https://ntfy.sh/t", "priority": "5"}, "invalid priority '5'"),
    ({"type": "ntfy", "url": "https://ntfy.sh/t", "priority": "urgent"}, "invalid priority 'urgent'"),
    ({"type": "ntfy", "url": "https://ntfy.sh/t", "priority": None}, "invalid priority None"),
    ({"typ": "slack", "url": "https://x"}, "unknown setting 'typ' — did you mean 'type'"),
    ({"type": "ntfy"}, "missing required field"),
    ({"type": "telegram", "bot_token": "t"}, "missing required field"),
    ({"type": "callmebot", "phone": "+15551234567", "api_key": "k"},
        "missing required field.*channel"),
    ({"type": "callmebot", "phone": "+15551234567", "api_key": "k", "channel": "whatsap"},
     "invalid channel 'whatsap'"),
    ({"type": "callmebot", "phone": "+15551234567", "api_key": "k", "channel": "WhatsApp"},
     "invalid channel 'WhatsApp'"),
    ({"type": "callmebot", "phone": "+15551234567", "api_key": "k", "channel": "a/b?c=d"},
     "invalid channel 'a/b\\?c=d'"),
    ({"type": "callmebot", "phone": "+15551234567", "api_key": "k", "channel": ""},
     "invalid channel ''"),
    ({"type": "callmebot", "phone": "+15551234567", "api_key": "k", "channel": 5},
     "invalid channel 5"),
    ({"type": "callmebot", "phone": "+15551234567", "api_key": "k", "channel": None},
     "invalid channel None"),
    ({"type": "callmebot", "api_key": "k", "channel": "whatsapp"},
     "channel 'whatsapp'\\) is missing required field\\(s\\): phone"),
    ({"type": "callmebot", "phone": "+15551234567", "channel": "whatsapp"},
     "channel 'whatsapp'\\) is missing required field\\(s\\): api_key"),
    ({"type": "callmebot", "api_key": "k", "channel": "signal"},
     "channel 'signal'\\) is missing required field\\(s\\): phone"),
    ({"type": "callmebot", "phone": "+15551234567", "channel": "signal"},
     "channel 'signal'\\) is missing required field\\(s\\): api_key"),
    ({"type": "callmebot", "channel": "facebook"},
     "channel 'facebook'\\) is missing required field\\(s\\): api_key"),
    ({"type": "callmebot", "channel": ["whatsapp"], "phone": "+15551234567", "api_key": "k"},
     "invalid channel"),
])
def test_bad_push_alert_channel_is_rejected(tmp_path, channel, expected):
    """A typo'd channel type is an error: it would otherwise fall through to
    the generic-webhook branch and drop the alert at the first real outage --
    while still advancing the cooldown, so it would never be retried."""
    import yaml
    config_path = _minimal_config(tmp_path)
    (tmp_path / "alerts.yaml").write_text(
        yaml.dump({"alerts": {"push_alerts": [channel]}})
    )
    with pytest.raises(ConfigError, match=expected):
        load_config(str(config_path))


@pytest.mark.parametrize("channel", [
    {"type": "telegram", "bot_token": "1:a", "chat_id": "1"},
    {"type": "ntfy", "url": "https://ntfy.sh/topic"},
    {"type": "ntfy", "url": "https://ntfy.sh/topic", "priority": "min"},
    {"type": "ntfy", "url": "https://ntfy.sh/topic", "priority": "max"},
    {"type": "callmebot", "phone": "+15551234567", "api_key": "k", "channel": "whatsapp"},
    {"type": "callmebot", "phone": "+15551234567", "api_key": "k", "channel": "signal"},
    {"type": "callmebot", "api_key": "k", "channel": "facebook"},
    {"type": "rocketchat", "url": "https://x"},
    {"type": "rocket.chat", "url": "https://x"},
    {"type": "generic", "url": "https://x"},
    {"type": "slack", "url": "https://hooks.slack.com/services/T000/B000/XXXX"},
    {"type": "discord", "url": "http://discord.lan:8080/api/webhooks/1/abc?wait=true"},
])
def test_valid_push_alert_channels_load(tmp_path, channel):
    import yaml
    config_path = _minimal_config(tmp_path)
    (tmp_path / "alerts.yaml").write_text(
        yaml.dump({"alerts": {"push_alerts": [{**channel, "monitors": "all"}]}})
    )
    assert len(load_config(str(config_path))["alerts"]["push_alerts"]) == 1


_UUID = "25a7d32b-0c51-44d8-b11b-c5bc73294abc"


def _callmebot_load(tmp_path, **fields):
    import yaml
    config_path = _minimal_config(tmp_path)
    (tmp_path / "alerts.yaml").write_text(yaml.dump({"alerts": {"push_alerts": [
        {"type": "callmebot", "monitors": "all", **fields},
    ]}}))
    return load_config(str(config_path))


@pytest.mark.parametrize("channel,fields", [
    ("whatsapp", {"phone": "+15551234567", "api_key": "k"}),
    ("whatsapp", {"phone": "+1234567", "api_key": "k"}),
    ("whatsapp", {"phone": "+123456789012345", "api_key": "k"}),
    ("signal", {"phone": "+15551234567", "api_key": "k"}),
    ("signal", {"phone": _UUID, "api_key": "k"}),
])
def test_callmebot_recipients_that_are_accepted(tmp_path, channel, fields):
    config = _callmebot_load(tmp_path, channel=channel, **fields)
    assert len(config["alerts"]["push_alerts"]) == 1


@pytest.mark.parametrize("channel,fields,expected", [
    ("whatsapp", {"phone": "15551234567", "api_key": "k"}, "phone that is not valid"),
    ("whatsapp", {"phone": "+1555 123 4567", "api_key": "k"}, "phone that is not valid"),
    ("whatsapp", {"phone": "+1-555-123-4567", "api_key": "k"}, "phone that is not valid"),
    ("whatsapp", {"phone": "+123456", "api_key": "k"}, "phone that is not valid"),
    ("whatsapp", {"phone": "+1234567890123456", "api_key": "k"}, "phone that is not valid"),
    ("whatsapp", {"phone": "+123****7890", "api_key": "k"}, "phone that is not valid"),
    ("whatsapp", {"phone": _UUID, "api_key": "k"}, "phone that is not valid"),
    ("whatsapp", {"phone": 15551234567, "api_key": "k"}, "phone that is not text"),
    ("signal", {"phone": "not a number", "api_key": "k"}, "or a Signal UUID"),
])
def test_callmebot_recipients_that_are_rejected(tmp_path, channel, fields, expected):
    with pytest.raises(ConfigError, match=expected):
        _callmebot_load(tmp_path, channel=channel, **fields)


def test_callmebot_recipient_errors_never_repeat_the_value(tmp_path):
    """A phone number or account name is personal, and errors are shown on the
    dashboard."""
    with pytest.raises(ConfigError) as err:
        _callmebot_load(
            tmp_path, channel="whatsapp", phone="+1 555 SECRET", api_key="k",
        )
    assert "SECRET" not in str(err.value)


# --- Maintenance window validation ----------------------------------------------------


def _window(**overrides):
    w = {
        "name": "Window", "start_day": "sunday", "end_day": "sunday",
        "start_time": "02:00", "end_time": "04:00",
    }
    w.update(overrides)
    return w


@pytest.mark.parametrize("window,expected", [
    (_window(start_time="0200"), "invalid start_time"),
    (_window(start_time="25:00"), "invalid start_time"),
    (_window(start_day="sundy"), "unrecognized start_day"),
    (_window(resume="verifyy"), "invalid resume mode"),
])
def test_bad_maintenance_window_is_rejected(tmp_path, window, expected):
    """A malformed window is an error at load: it would otherwise resolve to
    "not in maintenance", so alerts would fire straight through the configured
    outage window with only a log line to say so."""
    import yaml
    config_path = _minimal_config(tmp_path)
    (tmp_path / "maintenance.yaml").write_text(
        yaml.dump({"maintenance": [window]})
    )
    with pytest.raises(ConfigError, match=expected):
        load_config(str(config_path))


def test_maintenance_window_missing_field_is_rejected(tmp_path):
    import yaml
    config_path = _minimal_config(tmp_path)
    window = _window()
    del window["start_day"]
    (tmp_path / "maintenance.yaml").write_text(
        yaml.dump({"maintenance": [window]})
    )
    with pytest.raises(ConfigError, match="missing required field 'start_day'"):
        load_config(str(config_path))


def test_maintenance_window_unquoted_date_is_rejected(tmp_path):
    """An unquoted `start_day: 2026-07-15` is parsed by YAML as a date
    object, not a string, and reached _parse_time as a non-string."""
    config_path = _minimal_config(tmp_path)
    (tmp_path / "maintenance.yaml").write_text(
        'maintenance:\n  - name: W\n    start_day: 2026-07-15\n'
        '    end_day: sunday\n    start_time: "02:00"\n    end_time: "04:00"\n'
    )
    with pytest.raises(ConfigError, match="non-text start_day"):
        load_config(str(config_path))


@pytest.mark.parametrize("day", [
    "sunday", "Sunday", "2nd Tuesday", "last friday", "2026-07-15",
])
def test_valid_maintenance_day_vocabulary_loads(tmp_path, day):
    """Covers the ordinal and ISO-date branches as well as weekday names, so a
    mistake in the validator's ordinal path is caught."""
    import yaml
    config_path = _minimal_config(tmp_path)
    (tmp_path / "maintenance.yaml").write_text(
        yaml.dump({"maintenance": [_window(start_day=day, end_day=day)]})
    )
    assert len(load_config(str(config_path))["maintenance"]) == 1


def test_documented_tags_all_loads(tmp_path):
    """`tags: "all"` is not a tag, so it is not checked against tags.yaml
    (a bare string would otherwise be read letter by letter)."""
    import yaml
    config_path = _minimal_config(tmp_path)
    (tmp_path / "tags.yaml").write_text(yaml.dump({"tags": ["external"]}))
    (tmp_path / "maintenance.yaml").write_text(
        yaml.dump({"maintenance": [_window(tags="all")]})
    )
    assert len(load_config(str(config_path))["maintenance"]) == 1


# --- Problems that must be caught at load ---------------------------------------------
#
# Each of these would otherwise load cleanly and then produce a silently
# inert maintenance window, two monitors fighting over one database row, a
# check that fails forever with a confusing runtime error, or a raw 500 from
# the dashboard.


def _write_split_config(tmp_path, *, glob=None, monitors=None, maintenance=None,
                        tags=None):
    """Write a minimal valid split-config tree, overriding chosen parts."""
    import yaml
    files = {
        "config.yaml": {"global": glob if glob is not None
                        else {"timezone": "UTC", "language": "en"}},
        "monitors.yaml": {"monitors": monitors or []},
        "maintenance.yaml": {"maintenance": maintenance or []},
        "tags.yaml": {"tags": tags or []},
        "alerts.yaml": {"alerts": {"email": {"recipients": []}, "push_alerts": []}},
    }
    for fname, data in files.items():
        (tmp_path / fname).write_text(yaml.dump(data))
    return str(tmp_path / "config.yaml")


def _http(name, url="http://example.com"):
    return {"name": name, "type": "http", "url": url}


def test_duplicate_monitor_names_rejected(tmp_path):
    """monitors.name is the `monitors` table's primary key and the key of
    every scheduling dict in main.py, so duplicates share one state row and
    overwrite each other's counters on every pass."""
    path = _write_split_config(tmp_path, monitors=[_http("dup"), _http("dup")])
    with pytest.raises(ConfigError, match="Duplicate monitor name"):
        load_config(path)


def test_monitor_names_differing_only_in_case_are_duplicates(tmp_path):
    """Names are not case sensitive, so `Web` and `web` are one name."""
    path = _write_split_config(tmp_path, monitors=[_http("Web"), _http("web")])
    with pytest.raises(ConfigError, match="Duplicate monitor name"):
        load_config(path)


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_monitor_name_rejected(tmp_path, name):
    path = _write_split_config(tmp_path, monitors=[_http(name)])
    with pytest.raises(ConfigError, match="empty 'name'"):
        load_config(path)


def test_non_string_monitor_name_rejected(tmp_path):
    """An unquoted numeric name parses as an int and breaks string handling
    downstream."""
    path = _write_split_config(tmp_path, monitors=[_http(123)])
    with pytest.raises(ConfigError, match="must be text"):
        load_config(path)


@pytest.mark.parametrize("port", [0, -1, 99999, "80"])
def test_invalid_tcp_port_rejected(tmp_path, port):
    """An out-of-range port would fail every check, reported per check rather
    than at startup."""
    path = _write_split_config(tmp_path, monitors=[
        {"name": "m", "type": "tcp", "host": "h", "port": port}])
    with pytest.raises(ConfigError, match="expected a whole number from 1 to 65535"):
        load_config(path)


def test_tcp_port_at_range_edges_accepted(tmp_path):
    path = _write_split_config(tmp_path, monitors=[
        {"name": "lo", "type": "tcp", "host": "h", "port": 1},
        {"name": "hi", "type": "tcp", "host": "h", "port": 65535}])
    assert len(load_config(path)["monitors"]) == 2


def test_http_url_without_scheme_rejected(tmp_path):
    """httpx rejects a scheme-less URL at request time, so the monitor would
    just fail every check with no sign the config was at fault."""
    path = _write_split_config(tmp_path, monitors=[_http("m", "example.com")])
    with pytest.raises(ConfigError, match="must start with http:// or https://"):
        load_config(path)


@pytest.mark.parametrize("tz,match", [
    ("Mars/Olympus", "timezone database doesn't recognise"),
    (5, "must be a non-empty IANA timezone name"),
    ("", "must be a non-empty IANA timezone name"),
])
def test_invalid_timezone_rejected(tmp_path, tz, match):
    """A typo'd timezone would silently shift every displayed timestamp; a
    non-string or empty one would escape as a raw TypeError/ValueError from
    ZoneInfo() during a dashboard render."""
    path = _write_split_config(tmp_path, glob={"timezone": tz, "language": "en"})
    with pytest.raises(ConfigError, match=match):
        load_config(path)


def test_global_as_non_mapping_rejected(tmp_path):
    """`global:` written as a list would produce a raw AttributeError rather
    than the operator-facing config-error page."""
    path = _write_split_config(tmp_path, glob=["a", "b"])
    with pytest.raises(ConfigError, match="'global' must be a mapping"):
        load_config(path)


# --- Maintenance windows that could never activate ------------------------------------


def _window(**kw):
    base = {"name": "w", "start_day": "Monday", "start_time": "10:00",
            "end_day": "Monday", "end_time": "11:00", "monitors": "all"}
    base.update(kw)
    return base


def test_maintenance_mixed_day_kinds_rejected(tmp_path):
    """_window_active_now() has branches for weekday/weekday,
    monthly/monthly and date/date only -- a mixed pair would fall through to
    the ISO-date branch, raise, get logged once, and never be active."""
    path = _write_split_config(tmp_path, maintenance=[
        _window(start_day="Monday", end_day="2nd Tuesday")])
    with pytest.raises(ConfigError, match="mixes day types"):
        load_config(path)


def test_maintenance_impossible_iso_date_rejected(tmp_path):
    """Passes the YYYY-MM-DD shape check but isn't a real day."""
    path = _write_split_config(tmp_path, maintenance=[
        _window(start_day="2026-02-30", end_day="2026-03-01")])
    with pytest.raises(ConfigError, match="not a real\n?\\s*date|not a real date"):
        load_config(path)


def test_maintenance_one_off_ending_before_start_rejected(tmp_path):
    """Nothing would raise for this one at all -- the comparison would simply
    never be true, so the window would be inert with no log line whatsoever."""
    path = _write_split_config(tmp_path, maintenance=[
        _window(start_day="2026-07-10", end_day="2026-07-09")])
    with pytest.raises(ConfigError, match="ends before it starts"):
        load_config(path)


def test_maintenance_same_day_with_backwards_times_rejected(tmp_path):
    path = _write_split_config(tmp_path, maintenance=[
        _window(start_day="2026-07-10", start_time="11:00",
                end_day="2026-07-10", end_time="09:00")])
    with pytest.raises(ConfigError, match="is not after"):
        load_config(path)


def test_maintenance_same_day_forwards_times_accepted(tmp_path):
    path = _write_split_config(tmp_path, maintenance=[
        _window(start_day="2026-07-10", start_time="09:00",
                end_day="2026-07-10", end_time="11:00")])
    assert len(load_config(path)["maintenance"]) == 1


def test_maintenance_unknown_monitor_reference_rejected(tmp_path):
    """A typo in a monitor name, in the same position as a tag reference,
    would produce a window matching nothing."""
    path = _write_split_config(
        tmp_path, monitors=[_http("web")],
        maintenance=[_window(monitors=["web-srever"])])
    with pytest.raises(ConfigError, match="targets unknown monitor"):
        load_config(path)


def test_maintenance_all_and_real_names_still_accepted(tmp_path):
    """"all" is the one special value, and a correctly-spelled name must
    still load -- the check above must not over-reject."""
    import yaml
    for targets in ("all", ["web"]):
        path = _write_split_config(
            tmp_path, monitors=[_http("web")],
            maintenance=[_window(monitors=targets)])
        assert len(load_config(path)["maintenance"]) == 1, targets
    assert yaml is not None  # keep the import meaningful for linters


def test_month_crossing_monthly_window_still_accepted(tmp_path):
    """The mixed-kind check must not reject the documented month-crossing
    form, where both days are ordinal weekdays in different months."""
    path = _write_split_config(tmp_path, maintenance=[
        _window(start_day="last Friday", start_time="23:00",
                end_day="1st Monday", end_time="02:00")])
    assert len(load_config(path)["maintenance"]) == 1


# --- resume mode vocabulary -----------------------------------------------------------


@pytest.mark.parametrize("mode", ["verify", "normal"])
def test_documented_resume_modes_load(tmp_path, mode):
    """Both documented values must load.

    "verify" (the default) and "normal" are the documented values, so
    `resume: "normal"`, copied straight out of the docs, has to load. main.py
    tests only `resume_mode != "verify"`, so nothing distinguishes any other
    spelling.
    """
    path = _write_split_config(tmp_path, maintenance=[_window(resume=mode)])
    assert load_config(path)["maintenance"][0]["resume"] == mode


def test_undocumented_resume_mode_rejected(tmp_path):
    """"immediate" is not a mode and must not be quietly accepted --
    otherwise the allowlist drifts out of step with the docs."""
    path = _write_split_config(tmp_path, maintenance=[_window(resume="immediate")])
    with pytest.raises(ConfigError, match="invalid resume mode"):
        load_config(path)


def test_resume_defaults_to_verify_when_absent(tmp_path):
    path = _write_split_config(tmp_path, maintenance=[_window()])
    assert "resume" not in load_config(path)["maintenance"][0]
