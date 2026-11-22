# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_alerts.py - Tests for src/alerts.py.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src import alerts as alerts_module
from src.alerts import (
    dispatch_alerts,
    reminder_due,
    send_email_alert,
    send_push_alert,
)
from src.state import MonitorState


def test_reminder_due_when_no_alert_was_sent_yet():
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = None
    assert reminder_due(state, cooldown=300) is True


def test_reminder_not_due_within_cooldown():
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = datetime.now(timezone.utc).isoformat()
    assert reminder_due(state, cooldown=300) is False


def test_reminder_due_after_cooldown():
    state = MonitorState(name="test", type="http")
    past = datetime.now(timezone.utc) - timedelta(seconds=301)
    state.last_alert_sent = past.isoformat()
    assert reminder_due(state, cooldown=300) is True


def test_reminder_due_with_zero_cooldown():
    """alert_cooldown_minutes: 0 means no wait between repeat alerts."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = datetime.now(timezone.utc).isoformat()
    assert reminder_due(state, cooldown=0) is True


def test_reminder_due_unparseable_last_alert_sent_sends():
    """A corrupted/unparseable last_alert_sent timestamp should fail open
    (send) rather than silently suppress every future alert forever."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = "not-a-timestamp"
    assert reminder_due(state, cooldown=300) is True


@pytest.mark.asyncio
async def test_send_push_alert_generic():
    """Generic push alert should POST JSON to the configured URL."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={"type": "generic", "url": "http://example.com/hook"},
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
            config={"global": {"language": "en"}},
        )

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert payload["monitor"] == "test-monitor"
        assert payload["status"] == "down"
        assert payload["error"] == "Connection refused"


@pytest.mark.asyncio
async def test_send_push_alert_telegram():
    """Telegram push alert should POST to the Telegram Bot API."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={
                "type": "telegram",
                "bot_token": "123:ABC",
                "chat_id": "456",
            },
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
            config={"global": {"language": "en"}},
        )

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1]["url"]
        assert "api.telegram.org" in url
        assert "123:ABC" in url
        payload = call_args[1]["json"]
        assert payload["chat_id"] == "456"
        assert "test-monitor" in payload["text"]
        assert payload["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_send_push_alert_ntfy_basic_auth():
    """ntfy push alert should POST plain text with Basic auth header."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={
                "type": "ntfy",
                "url": "https://ntfy.example.lan/test-topic",
                "username": "example",
                "password": "secret",
                "priority": "high",
            },
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
            config={"global": {"language": "en"}},
        )

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1]["url"]
        assert "ntfy.example.lan/test-topic" in url
        headers = call_args[1]["headers"]
        assert headers["Priority"] == "high"
        assert "test-monitor" in headers["Title"]
        assert headers["Authorization"].startswith("Basic ")
        body = call_args[1]["content"]
        assert "Connection refused" in body.decode("utf-8")


@pytest.mark.asyncio
async def test_send_push_alert_ntfy_token_auth():
    """ntfy push alert with token should use Bearer auth."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={
                "type": "ntfy",
                "url": "https://ntfy.sh/my-topic",
                "token": "tk_abc123",
            },
            monitor_name="test-monitor",
            status="up",
            error=None,
            response_ms=50.0,
            config={"global": {"language": "en"}},
        )

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tk_abc123"
        assert "test-monitor" in headers["Title"]


@pytest.mark.asyncio
async def test_send_push_alert_ntfy_no_auth():
    """ntfy push alert with no username/token should send no Authorization
    header at all (public topic)."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={
                "type": "ntfy",
                "url": "https://ntfy.sh/public-topic",
            },
            monitor_name="test-monitor",
            status="up",
            error=None,
            response_ms=20.0,
            config={"global": {"language": "en"}},
        )

        mock_post.assert_called_once()
        headers = mock_post.call_args[1]["headers"]
        assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_send_push_alert_callmebot():
    """CallMeBot push alert should GET the API URL with phone, text, and apikey params."""
    with patch("src.alerts.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = 200

        await send_push_alert(
            channel={
                "type": "callmebot",
                "phone": "+123****7890",
                "api_key": "1234567890",
                "channel": "signal",
            },
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
            config={"global": {"language": "en"}},
        )

        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        assert url.startswith("https://signal.callmebot.com/signal/send.php?")
        # urlencode encodes special chars: + → %2B, * → %2A
        assert "phone=%2B123" in url
        assert "apikey=1234567890" in url
        assert "test-monitor" in url


@pytest.mark.asyncio
async def test_send_push_alert_discord_down():
    """Discord alerts should POST {content: ...} with **bold** markers and
    a single-line code fence (multiline_fence=False)."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={"type": "discord", "url": "http://example.com/discordhook"},
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
            config={"global": {"language": "en"}},
        )

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1]["url"]
        assert url == "http://example.com/discordhook"
        payload = call_args[1]["json"]
        assert "content" in payload
        content = payload["content"]
        assert "**test-monitor**" in content
        assert "**is DOWN**" in content
        assert "```Connection refused```" in content
        assert "Latency:" in content and "1500ms" in content


@pytest.mark.asyncio
async def test_send_push_alert_discord_up():
    """Discord recovery alerts should use the UP wording and no error fence."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={"type": "discord", "url": "http://example.com/discordhook"},
            monitor_name="test-monitor",
            status="up",
            error=None,
            response_ms=42.0,
            config={"global": {"language": "en"}},
        )

        payload = mock_post.call_args[1]["json"]
        assert "**is UP**" in payload["content"]
        assert "42ms" in payload["content"]


@pytest.mark.asyncio
async def test_send_push_alert_mattermost():
    """Mattermost alerts should POST {text: ...} with **bold** markers and
    a multi-line code fence (multiline_fence=True)."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={"type": "mattermost", "url": "http://example.com/mmhook"},
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
            config={"global": {"language": "en"}},
        )

        payload = mock_post.call_args[1]["json"]
        assert "text" in payload
        text = payload["text"]
        assert "**test-monitor**" in text
        assert "```\nConnection refused\n```" in text


@pytest.mark.asyncio
async def test_send_push_alert_rocket_chat():
    """Rocket.Chat shares Mattermost's payload shape and formatting."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={"type": "rocket.chat", "url": "http://example.com/rchook"},
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
            config={"global": {"language": "en"}},
        )

        payload = mock_post.call_args[1]["json"]
        text = payload["text"]
        assert "**test-monitor**" in text
        assert "```\nConnection refused\n```" in text


@pytest.mark.asyncio
async def test_send_push_alert_slack():
    """Slack alerts should POST {text: ...} with *single-asterisk* bold
    markers (Slack markup), not Discord/Mattermost's **double**."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={"type": "slack", "url": "http://example.com/slackhook"},
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
            config={"global": {"language": "en"}},
        )

        payload = mock_post.call_args[1]["json"]
        text = payload["text"]
        assert "*test-monitor*" in text
        assert "**test-monitor**" not in text
        assert "```\nConnection refused\n```" in text


@pytest.mark.asyncio
async def test_send_push_alert_telegram_non_2xx_returns_false():
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 401
        mock_post.return_value.text = "Unauthorized"

        result = await send_push_alert(
            channel={"type": "telegram", "bot_token": "bad", "chat_id": "456"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
async def test_send_push_alert_telegram_network_error_returns_false():
    with patch(
        "src.alerts.httpx.AsyncClient.post",
        side_effect=OSError("network down"),
    ):
        result = await send_push_alert(
            channel={"type": "telegram", "bot_token": "t", "chat_id": "456"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
async def test_send_push_alert_ntfy_non_2xx_returns_false():
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 403
        mock_post.return_value.text = "Forbidden"

        result = await send_push_alert(
            channel={"type": "ntfy", "url": "https://ntfy.example.lan/t"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
async def test_send_push_alert_ntfy_network_error_returns_false():
    with patch(
        "src.alerts.httpx.AsyncClient.post",
        side_effect=OSError("network down"),
    ):
        result = await send_push_alert(
            channel={"type": "ntfy", "url": "https://ntfy.example.lan/t"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
async def test_send_push_alert_ntfy_up_status_body():
    """The recovery-message branch (is_up(status) True) is otherwise
    never exercised -- every other ntfy test above uses status='down'."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 200

        await send_push_alert(
            channel={"type": "ntfy", "url": "https://ntfy.example.lan/t"},
            monitor_name="test-monitor", status="up", error=None,
            response_ms=42.0, config={"global": {"language": "en"}},
        )

        body = mock_post.call_args[1]["content"].decode("utf-8")
        assert "Recovered" in body
        headers = mock_post.call_args[1]["headers"]
        assert "is UP" in headers["Title"]


@pytest.mark.parametrize("channel,prefix,expected,absent", [
    ({"channel": "whatsapp", "phone": "+1555", "api_key": "k"},
     "https://api.callmebot.com/whatsapp.php?",
     ["phone=%2B1555", "apikey=k"], []),
    ({"channel": "signal", "phone": "+1555", "api_key": "k"},
     "https://signal.callmebot.com/signal/send.php?",
     ["phone=%2B1555", "apikey=k"], []),
    ({"channel": "facebook", "api_key": "k"},
     "https://api.callmebot.com/facebook/send.php?",
     ["apikey=k"], ["phone="]),
])
def test_callmebot_url_for_each_delivery_method(channel, prefix, expected, absent):
    """Each CallMeBot delivery method has its own address and parameters."""
    from src.alerts import callmebot_url
    url = callmebot_url({"type": "callmebot", **channel}, "hello")
    assert url.startswith(prefix)
    assert "text=hello" in url
    for part in expected:
        assert part in url
    for part in absent:
        assert part not in url


@pytest.mark.asyncio
async def test_send_push_alert_callmebot_up_status_text():
    """Every other CallMeBot test uses status='down' -- cover the
    recovery-message branch too."""
    with patch("src.alerts.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = 200

        await send_push_alert(
            channel={"type": "callmebot", "phone": "+1555", "api_key": "k",
                     "channel": "whatsapp"},
            monitor_name="test-monitor", status="up", error=None,
            response_ms=42.0, config={"global": {"language": "en"}},
        )

        url = mock_get.call_args[0][0]
        assert "is+UP" in url or "is%20UP" in url


@pytest.mark.asyncio
async def test_send_push_alert_callmebot_non_2xx_returns_false():
    with patch("src.alerts.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = 400
        mock_get.return_value.text = "bad request"

        result = await send_push_alert(
            channel={"type": "callmebot", "phone": "+1555", "api_key": "k",
                     "channel": "whatsapp"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
async def test_send_push_alert_callmebot_queued_message_returns_true():
    """CallMeBot answers a message it queued with HTTP 200."""
    with patch("src.alerts.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = "Message queued."

        result = await send_push_alert(
            channel={"type": "callmebot", "phone": "+1555", "api_key": "k",
                     "channel": "whatsapp"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is True


@pytest.mark.asyncio
async def test_send_push_alert_callmebot_invalid_api_key_returns_false():
    """CallMeBot answers a wrong API key with HTTP 203 and "APIKey is
    invalid". That is below 400, but nothing was sent, so it is a failure."""
    with patch("src.alerts.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = 203
        mock_get.return_value.text = "APIKey is invalid."

        result = await send_push_alert(
            channel={"type": "callmebot", "phone": "+1555", "api_key": "k",
                     "channel": "whatsapp"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
async def test_send_push_alert_callmebot_facebook_invalid_api_key_returns_false():
    """For Facebook, CallMeBot answers a wrong API key with HTTP 200 and the
    same "APIKey is invalid" page, so the reply text has to be checked too."""
    with patch("src.alerts.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = (
            '<p style="color:red"><b>APIKey is invalid.</b><br>Please ensure '
            "that the apikey is correct or request a new one."
        )

        result = await send_push_alert(
            channel={"type": "callmebot", "api_key": "k", "channel": "facebook"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
async def test_send_push_alert_callmebot_signal_error_page_returns_false():
    """For Signal, CallMeBot answers a wrong API key with HTTP 200 and an
    error page that does not say the key is invalid."""
    with patch("src.alerts.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = (
            "<p>There was an ERROR!!!<p><u>Error Num:</u>2<p><u>Error Text:</u>"
            "Trying to access array offset on value of type null"
        )

        result = await send_push_alert(
            channel={"type": "callmebot", "phone": "+1555", "api_key": "k",
                     "channel": "signal"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
@pytest.mark.parametrize("channel, reply", [
    ("whatsapp", "<p><b>Message queued.</b> You will receive it in a few seconds."),
    ("signal", "Apikey Correct.<br> <p><b><br>Message sent to 25a7d32b!</b>"),
    ("facebook", "<b><br>Message sent!</b>"),
])
async def test_send_push_alert_callmebot_each_methods_success_reply_is_sent(
    channel, reply,
):
    """The replies CallMeBot gave to real messages on each delivery method."""
    with patch("src.alerts.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = reply

        result = await send_push_alert(
            channel={"type": "callmebot", "phone": "+1555", "api_key": "k",
                     "channel": channel},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code, text, expected", [
    (200, "Message queued.", True),
    (203, "APIKey is invalid.", False),
    (200, "APIKey is invalid.", False),
    (200, "There was an ERROR!!!", False),
])
async def test_system_notice_callmebot_judges_the_reply_like_an_alert(
    status_code, text, expected,
):
    """A system notice judges CallMeBot's reply the same way an alert does."""
    from src.system_notice import _send_push

    with patch("src.system_notice.httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.status_code = status_code
        mock_get.return_value.text = text

        result = await _send_push(
            {"type": "callmebot", "phone": "+1555", "api_key": "k",
             "channel": "whatsapp"},
            "Title", "Body",
        )
        assert result is expected


@pytest.mark.asyncio
async def test_send_push_alert_callmebot_network_error_returns_false():
    with patch(
        "src.alerts.httpx.AsyncClient.get",
        side_effect=OSError("network down"),
    ):
        result = await send_push_alert(
            channel={"type": "callmebot", "phone": "+1555", "api_key": "k",
                     "channel": "whatsapp"},
            monitor_name="test-monitor", status="down", error="x",
            response_ms=10.0, config={"global": {"language": "en"}},
        )
        assert result is False


@pytest.mark.asyncio
async def test_send_push_alert_non_2xx_does_not_raise():
    """A webhook returning an error status should be logged, not raised."""
    with patch("src.alerts.httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = "Internal Server Error"

        await send_push_alert(
            channel={"type": "generic", "url": "http://example.com/hook"},
            monitor_name="test-monitor",
            status="down",
            error="x",
            response_ms=10.0,
            config={"global": {"language": "en"}},
        )
        # No exception raised is the assertion.


@pytest.mark.asyncio
async def test_send_push_alert_network_error_does_not_raise():
    """A network-level failure (connection refused, DNS failure, etc.)
    should be caught and logged, never propagate out of send_push_alert."""
    with patch(
        "src.alerts.httpx.AsyncClient.post", side_effect=OSError("network down")
    ):
        await send_push_alert(
            channel={"type": "generic", "url": "http://example.com/hook"},
            monitor_name="test-monitor",
            status="down",
            error="x",
            response_ms=10.0,
            config={"global": {"language": "en"}},
        )
        # No exception raised is the assertion.


def _email_config(**overrides):
    cfg = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "from": "alerts@example.com",
        "to": ["me@example.com"],
    }
    cfg.update(overrides)
    return {"global": {"language": "en"}, "alerts": {"email": cfg}}


@pytest.mark.asyncio
async def test_send_email_alert_down_sends_via_smtp():
    with patch(
        "src.alerts.aiosmtplib.send", new_callable=AsyncMock
    ) as mock_send:
        result = await send_email_alert(
            config=_email_config(),
            monitor_name="test-monitor",
            status="down",
            error="Connection refused",
            response_ms=1500.0,
        )

        assert result is True
        mock_send.assert_called_once()
        message = mock_send.call_args[0][0]
        assert message["To"] == "me@example.com"
        assert "test-monitor" in message["Subject"]
        assert "DOWN" in message["Subject"]
        body = message.get_content()
        assert "Connection refused" in body

        kwargs = mock_send.call_args[1]
        assert kwargs["hostname"] == "smtp.example.com"
        assert kwargs["port"] == 587


@pytest.mark.asyncio
async def test_send_email_alert_up_recovery():
    with patch(
        "src.alerts.aiosmtplib.send", new_callable=AsyncMock
    ) as mock_send:
        result = await send_email_alert(
            config=_email_config(),
            monitor_name="test-monitor",
            status="up",
            error=None,
            response_ms=42.0,
        )

        assert result is True
        message = mock_send.call_args[0][0]
        assert "UP" in message["Subject"]


@pytest.mark.asyncio
async def test_dispatch_email_alert_skips_an_entry_that_is_off(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    result = await alerts_module._dispatch_email_alert(
        config={"alerts": {"email": {"recipients": [
            {"to": ["off@example.com"], "monitors": "all", "enabled": False},
            {"to": ["on@example.com"], "monitors": "all"},
        ]}}},
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
    )
    assert result is True
    send.assert_awaited_once()
    assert send.await_args.kwargs["recipient"]["to"] == ["on@example.com"]


@pytest.mark.asyncio
async def test_send_email_alert_smtp_failure_does_not_raise():
    """An SMTP-level failure should be caught and logged, not propagate."""
    with patch(
        "src.alerts.aiosmtplib.send",
        new_callable=AsyncMock,
        side_effect=OSError("connection refused"),
    ):
        result = await send_email_alert(
            config=_email_config(),
            monitor_name="test-monitor",
            status="down",
            error="x",
            response_ms=10.0,
        )
        # No exception raised, and the failure is reported via return value.
        assert result is False


def _push_config():
    return {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": "http://example.com/hook", "enabled": True, "monitors": "all"},
            ],
        },
        "monitors": [{"name": "test", "type": "http"}],
        "tags": [],
    }


@pytest.mark.asyncio
async def test_dispatch_alerts_skips_a_repeat_alert_inside_the_cooldown():
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = datetime.now(timezone.utc).isoformat()

    with patch("src.alerts.send_push_alert") as mock_push:
        await dispatch_alerts(
            config=_push_config(),
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
            repeat_after=300,
        )
        mock_push.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_alerts_sends_a_repeat_alert_after_the_cooldown():
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = (
        datetime.now(timezone.utc) - timedelta(seconds=301)
    ).isoformat()

    with patch("src.alerts.send_push_alert", new=AsyncMock(return_value=True)) as mock_push, \
         patch("src.alerts.db.save_state", new=AsyncMock()):
        await dispatch_alerts(
            config=_push_config(),
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
            repeat_after=300,
        )
        mock_push.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_alerts_always_sends_a_status_change():
    """A status change is never held back by an alert sent a moment ago --
    e.g. a monitor that goes down again right after recovering."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = datetime.now(timezone.utc).isoformat()

    with patch("src.alerts.send_push_alert", new=AsyncMock(return_value=True)) as mock_push, \
         patch("src.alerts.db.save_state", new=AsyncMock()):
        await dispatch_alerts(
            config=_push_config(),
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
        )
        mock_push.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_alerts_tag_match():
    """Channel with tags should fire when monitor has matching tag."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = None

    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": "http://example.com/hook", "tags": ["critical"]},
            ],
        },
        "monitors": [{"name": "test", "type": "http", "tags": ["critical"]}],
        "tags": ["critical"],
    }

    with patch("src.alerts.send_push_alert") as mock_push, \
         patch("src.alerts.db.save_state", new_callable=AsyncMock):
        await dispatch_alerts(
            config=config,
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
        )
        mock_push.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_alerts_tag_no_match():
    """Channel with tags should NOT fire when monitor has no matching tag."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = None

    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": "http://example.com/hook", "tags": ["critical"]},
            ],
        },
        "monitors": [{"name": "test", "type": "http", "tags": ["internal"]}],
        "tags": ["critical", "internal"],
    }

    with patch("src.alerts.send_push_alert") as mock_push, \
         patch("src.alerts.db.save_state", new_callable=AsyncMock):
        await dispatch_alerts(
            config=config,
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
        )
        mock_push.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("targets, monitor_tags, sent", [
    ({}, ["internal"], False),                              # neither setting: no monitor
    ({"monitors": "all"}, [], True),
    ({"monitors": "all"}, ["internal"], True),
    ({"monitors": ["test"]}, [], True),
    ({"monitors": ["other"]}, ["internal"], False),
    ({"tags": ["internal"]}, ["internal"], True),
    ({"tags": ["critical"]}, ["internal"], False),
    ({"tags": ["critical"]}, [], False),
    ({"tags": "all"}, ["internal"], True),
    ({"tags": "all"}, [], False),                           # all = monitors that have a tag
    ({"monitors": ["test"], "tags": ["critical"]}, [], True),   # either one is enough
    ({"monitors": ["other"], "tags": ["internal"]}, ["internal"], True),
    ({"monitors": ["other"], "tags": ["critical"]}, ["internal"], False),
])
async def test_dispatch_alerts_chooses_monitors_by_monitors_and_tags(
    targets, monitor_tags, sent,
):
    """A push alert is for the monitors named in `monitors` and for the
    monitors that have one of its `tags`; "all" is every monitor for
    `monitors` and every monitor that has a tag for `tags`; a setting that is
    left out matches nothing."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = None

    monitor = {"name": "test", "type": "http"}
    if monitor_tags:
        monitor["tags"] = monitor_tags
    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": "http://example.com/hook", **targets},
            ],
        },
        "monitors": [monitor],
        "tags": ["internal", "critical"],
    }

    with patch("src.alerts.send_push_alert") as mock_push, \
         patch("src.alerts.db.save_state", new_callable=AsyncMock):
        await dispatch_alerts(
            config=config,
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
        )
        assert mock_push.called is sent


@pytest.mark.asyncio
async def test_dispatch_alerts_sends_channels_concurrently():
    """Channels must be dispatched concurrently, not one after another.

    Each channel is an independent request to a different service with its own
    timeout, so sequential dispatch would stack them: eight channels at the
    default 10s timeout could occupy one alert for ~80s, and a single
    unreachable webhook would delay every other channel's alert. Uses a barrier
    rather than wall-clock timing so it can't flake on a slow machine: all
    eight must be in flight at once for the barrier to release, which
    sequential dispatch can never do.
    """
    n_channels = 8
    barrier = asyncio.Barrier(n_channels)

    async def all_must_be_in_flight(*_args, **_kwargs):
        # Times out (and fails the test) unless every channel gets here
        # before any of them returns.
        async with asyncio.timeout(5):
            await barrier.wait()
        return True

    state = MonitorState(name="test", type="http")
    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": f"http://example.com/hook-{i}", "monitors": "all"}
                for i in range(n_channels)
            ],
        },
        "monitors": [{"name": "test", "type": "http"}],
        "tags": [],
    }

    with patch("src.alerts._dispatch_push_alert_channel", all_must_be_in_flight), \
         patch("src.alerts.db.save_state", new_callable=AsyncMock):
        await dispatch_alerts(
            config=config, state=state, new_status="down", error=None,
            response_ms=1.0,
        )

    assert state.last_alert_sent is not None


@pytest.mark.asyncio
async def test_dispatch_alerts_one_channel_raising_does_not_stop_the_others():
    """gather(return_exceptions=True): a channel raising must not cancel
    its siblings, and must still count as attempted so the cooldown
    advances rather than retrying every check cycle."""
    calls = []

    async def third_one_explodes(channel, *_args, **_kwargs):
        calls.append(channel["url"])
        if channel["url"].endswith("boom"):
            raise RuntimeError("channel exploded")
        return True

    state = MonitorState(name="test", type="http")
    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": "http://example.com/a", "monitors": "all"},
                {"type": "generic", "url": "http://example.com/boom", "monitors": "all"},
                {"type": "generic", "url": "http://example.com/c", "monitors": "all"},
            ],
        },
        "monitors": [{"name": "test", "type": "http"}],
        "tags": [],
    }

    with patch("src.alerts._dispatch_push_alert_channel", third_one_explodes), \
         patch("src.alerts.db.save_state", new_callable=AsyncMock):
        await dispatch_alerts(
            config=config, state=state, new_status="down", error=None,
            response_ms=1.0,
        )

    # All three ran despite the middle one raising.
    assert len(calls) == 3
    assert state.last_alert_sent is not None


@pytest.mark.asyncio
async def test_dispatch_alerts_all_channels_fail_still_advances_cooldown():
    """If every attempted channel fails, last_alert_sent still advances —
    a failed dispatch waits out the normal cooldown before retrying, same
    as a successful one, rather than hammering every check cycle."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = None

    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": "http://example.com/hook-a", "monitors": "all"},
                {"type": "generic", "url": "http://example.com/hook-b", "monitors": "all"},
            ],
        },
        "monitors": [{"name": "test", "type": "http"}],
        "tags": [],
    }

    with patch("src.alerts.send_push_alert", new_callable=AsyncMock) as mock_push, \
         patch("src.alerts.db.save_state", new_callable=AsyncMock) as mock_save:
        mock_push.return_value = False
        await dispatch_alerts(
            config=config,
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
        )
        assert mock_push.call_count == 2
        assert state.last_alert_sent is not None
        mock_save.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_alerts_partial_success_advances_cooldown():
    """If at least one channel succeeds, cooldown should advance even
    though another configured channel failed."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = None

    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": "http://example.com/hook-a", "monitors": "all"},
                {"type": "generic", "url": "http://example.com/hook-b", "monitors": "all"},
            ],
        },
        "monitors": [{"name": "test", "type": "http"}],
        "tags": [],
    }

    with patch("src.alerts.send_push_alert", new_callable=AsyncMock) as mock_push, \
         patch("src.alerts.db.save_state", new_callable=AsyncMock) as mock_save:
        mock_push.side_effect = [False, True]
        await dispatch_alerts(
            config=config,
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
        )
        assert state.last_alert_sent is not None
        mock_save.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_alerts_nothing_configured_does_not_advance_cooldown():
    """No email, no push channels: nothing attempted, cooldown shouldn't
    advance (there's nothing wrong, just nothing to send)."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = None

    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [],
        },
        "monitors": [{"name": "test", "type": "http"}],
        "tags": [],
    }

    with patch("src.alerts.db.save_state", new_callable=AsyncMock) as mock_save:
        await dispatch_alerts(
            config=config,
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
        )
        assert state.last_alert_sent is None
        mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_alerts_monitor_no_tags_skips_tagged_channel():
    """Monitor with no tags field should not fire channels that have tags."""
    state = MonitorState(name="test", type="http")
    state.last_alert_sent = None

    config = {
        "global": {"language": "en"},
        "alerts": {
            "email": {"enabled": False},
            "push_alerts": [
                {"type": "generic", "url": "http://example.com/hook", "tags": ["critical"]},
            ],
        },
        "monitors": [{"name": "test", "type": "http"}],
        "tags": ["critical"],
    }

    with patch("src.alerts.send_push_alert") as mock_push, \
         patch("src.alerts.db.save_state", new_callable=AsyncMock):
        await dispatch_alerts(
            config=config,
            state=state,
            new_status="down",
            error="test error",
            response_ms=100.0,
        )
        mock_push.assert_not_called()


# --- _safe_format ---------------------------------------------------------------------


def test_safe_format_bad_placeholder_falls_back_to_raw_template():
    """A translation string with a typo'd/missing placeholder must not
    crash alert dispatch -- fall back to the raw (untranslated-looking
    but still informative) template instead."""
    result = alerts_module._safe_format("Monitor '{typo}' is down", name="m1")
    assert result == "Monitor '{typo}' is down"


def test_safe_format_valid_placeholder_substitutes():
    result = alerts_module._safe_format("Monitor '{name}' is down", name="m1")
    assert result == "Monitor 'm1' is down"


# --- _find_monitor_config -------------------------------------------------------------


def test_find_monitor_config_not_found_returns_none():
    config = {"monitors": [{"name": "other", "type": "http"}]}
    assert alerts_module._find_monitor_config(config, "missing") is None


def test_find_monitor_config_found():
    config = {"monitors": [{"name": "m1", "type": "http", "url": "x"}]}
    result = alerts_module._find_monitor_config(config, "m1")
    assert result["name"] == "m1"


# --- _dispatch_email_alert / _dispatch_push_alert_channel: isolation ------------------


@pytest.mark.asyncio
async def test_dispatch_email_alert_with_every_entry_off_returns_none():
    result = await alerts_module._dispatch_email_alert(
        config={"alerts": {"email": {"recipients": [
            {"to": ["me@example.com"], "monitors": "all", "enabled": False},
        ]}}},
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
    )
    assert result is None


def _email_tag_config(tags=None, monitors=None):
    """An email alert with one recipients entry that has the given targets."""
    recipient = {"to": ["me@example.com"]}
    if tags is not None:
        recipient["tags"] = tags
    if monitors is not None:
        recipient["monitors"] = monitors
    return {"alerts": {"email": {"recipients": [recipient]}}}


@pytest.mark.asyncio
async def test_dispatch_email_alert_tag_match_sends(monkeypatch):
    """An email alert with tags is sent for a monitor that has one of them."""
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    result = await alerts_module._dispatch_email_alert(
        config=_email_tag_config(["critical", "internal"]),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags={"internal", "testing"},
    )
    assert result is True
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_email_alert_tag_no_match_skips(monkeypatch):
    """An email alert with tags is not sent for a monitor with none of them."""
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    result = await alerts_module._dispatch_email_alert(
        config=_email_tag_config(["critical"]),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags={"testing"},
    )
    assert result is None
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_email_alert_tagged_email_skips_untagged_monitor(monkeypatch):
    """A monitor with no tags never triggers an email alert that has tags."""
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    result = await alerts_module._dispatch_email_alert(
        config=_email_tag_config(["critical"]),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags=set(),
    )
    assert result is None
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("monitor_tags", [set(), {"testing"}])
async def test_dispatch_email_alert_monitors_all_sends_for_every_monitor(
    monkeypatch, monitor_tags,
):
    """An email alert with monitors: "all" is sent for every monitor."""
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    result = await alerts_module._dispatch_email_alert(
        config=_email_tag_config(monitors="all"),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags=monitor_tags,
    )
    assert result is True
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_email_alert_monitors_list_sends_only_for_those(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    sent = await alerts_module._dispatch_email_alert(
        config=_email_tag_config(monitors=["m1"]),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
    )
    skipped = await alerts_module._dispatch_email_alert(
        config=_email_tag_config(monitors=["m1"]),
        monitor_name="m2", new_status="down", error="x", response_ms=10.0,
    )
    assert sent is True and skipped is None
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_email_alert_tags_all_needs_a_tagged_monitor(monkeypatch):
    """tags: "all" is every monitor that has at least one tag."""
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    tagged = await alerts_module._dispatch_email_alert(
        config=_email_tag_config("all"),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags={"testing"},
    )
    untagged = await alerts_module._dispatch_email_alert(
        config=_email_tag_config("all"),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags=set(),
    )
    assert tagged is True and untagged is None
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_email_alert_without_monitors_or_tags_sends_for_none(
    monkeypatch,
):
    """Leaving both out matches no monitor (the config check rejects such an
    alert, so this is only the matching rule)."""
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    result = await alerts_module._dispatch_email_alert(
        config=_email_tag_config(),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags={"testing"},
    )
    assert result is None
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_email_alert_isolates_unexpected_failure(monkeypatch):
    """Even a failure raised outside send_email_alert's own try/except
    (e.g. a malformed config causing a KeyError before SMTP is even
    touched) must not propagate out of the wrapper."""
    monkeypatch.setattr(
        alerts_module, "send_email_alert",
        AsyncMock(side_effect=KeyError("from")),
    )
    result = await alerts_module._dispatch_email_alert(
        config=_email_tag_config(monitors="all"),
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
    )
    assert result is False


def _two_recipient_config():
    return {"alerts": {"email": {"recipients": [
        {"to": ["db@example.com"], "tags": ["database"]},
        {"to": ["ops@example.com", "boss@example.com"], "monitors": "all"},
    ]}}}


@pytest.mark.asyncio
async def test_dispatch_email_alert_sends_one_email_per_matching_recipients_entry(
    monkeypatch,
):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    result = await alerts_module._dispatch_email_alert(
        config=_two_recipient_config(),
        monitor_name="db1", new_status="down", error="x", response_ms=10.0,
        monitor_tags={"database"},
    )
    assert result is True
    assert [c.kwargs["recipient"]["to"] for c in send.await_args_list] == [
        ["db@example.com"], ["ops@example.com", "boss@example.com"],
    ]


@pytest.mark.asyncio
async def test_dispatch_email_alert_skips_recipients_entries_that_do_not_match(
    monkeypatch,
):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    await alerts_module._dispatch_email_alert(
        config=_two_recipient_config(),
        monitor_name="web1", new_status="down", error="x", response_ms=10.0,
        monitor_tags={"web"},
    )
    send.assert_awaited_once()
    assert send.await_args.kwargs["recipient"]["to"] == [
        "ops@example.com", "boss@example.com",
    ]


@pytest.mark.asyncio
async def test_dispatch_email_alert_is_false_if_any_recipients_entry_fails(
    monkeypatch,
):
    send = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(alerts_module, "send_email_alert", send)
    result = await alerts_module._dispatch_email_alert(
        config=_two_recipient_config(),
        monitor_name="db1", new_status="down", error="x", response_ms=10.0,
        monitor_tags={"database"},
    )
    assert result is False
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_send_email_alert_sends_to_the_recipients_entry_it_is_given():
    config = _email_config()
    recipient = {"to": ["team@example.com", "lead@example.com"], "monitors": "all"}
    with patch("src.alerts.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        result = await send_email_alert(
            config=config, monitor_name="m1", status="down", error="x",
            response_ms=10.0, recipient=recipient,
        )
    assert result is True
    message = mock_send.call_args[0][0]
    assert message["To"] == "team@example.com, lead@example.com"
    assert message["From"] == "Uptime Herald <alerts@example.com>"


@pytest.mark.asyncio
async def test_dispatch_push_alert_channel_disabled_returns_none():
    result = await alerts_module._dispatch_push_alert_channel(
        channel={"type": "generic", "url": "http://x", "enabled": False},
        config={"global": {"language": "en"}},
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags=set(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_push_alert_channel_tag_mismatch_returns_none():
    result = await alerts_module._dispatch_push_alert_channel(
        channel={"type": "generic", "url": "http://x", "tags": ["prod"]},
        config={"global": {"language": "en"}},
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags={"staging"},
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_push_alert_channel_isolates_unexpected_failure(
    monkeypatch,
):
    """A malformed channel (e.g. missing a required key for its type)
    raising before send_push_alert's own try/except must not stop other
    channels from being attempted by the caller."""
    monkeypatch.setattr(
        alerts_module, "send_push_alert",
        AsyncMock(side_effect=KeyError("url")),
    )
    result = await alerts_module._dispatch_push_alert_channel(
        channel={"type": "discord", "monitors": "all"},  # missing required 'url'
        config={"global": {"language": "en"}},
        monitor_name="m1", new_status="down", error="x", response_ms=10.0,
        monitor_tags=set(),
    )
    assert result is False
