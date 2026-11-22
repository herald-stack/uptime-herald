# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
alerts.py - Sends email and push alerts when a monitor changes state.
"""

import asyncio
import base64
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

import aiosmtplib
import httpx

from src import db
from src.config import email_recipient_config, entry_applies_to_monitor
from src.i18n import get_translations
from src.monitors.base import is_up
from src.state import MonitorState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
def _t(config: dict):
    """Get the gettext translation function for the configured language."""
    lang = config.get("global", {}).get("language", "en")
    return get_translations(lang)


# --------------------------------------------------------------------------------------
def _build_markdown_alert_text(
    monitor_name: str,
    status: str,
    error: str | None,
    response_ms: float,
    t,
    bold: str,
    multiline_fence: bool,
    expected_status: int | None = None,
) -> str:
    """Build the up/down message body shared by discord, mattermost,
    rocket.chat, slack, and telegram. They differ only in bold-marker style
    (`**` vs `*`) and code-fence formatting, which are parameters here, so all
    five channels stay in sync."""
    if is_up(status, expected_status):
        # Guarded the same way the DOWN path below already guards it: a
        # None response_ms here would raise TypeError from inside
        # dispatch_alerts, which run_check catches and logs -- so the
        # recovery alert would be silently dropped rather than sent.
        text = f"✅ {bold}{monitor_name}{bold} {bold}{t.gettext('is UP')}{bold}"
        if response_ms is not None:
            text += f" — {response_ms:.0f}ms"
        return text

    text = f"🔴 {bold}{monitor_name}{bold} {bold}{t.gettext('is DOWN')}{bold}"
    if error:
        snippet = error[:500]
        text += f"\n```\n{snippet}\n```" if multiline_fence else f"\n```{snippet}```"
    if response_ms is not None:
        text += f"\n{t.gettext('Latency:')} {response_ms:.0f}ms"
    return text


# --------------------------------------------------------------------------------------
def _safe_format(template: str, **kwargs) -> str:
    """Format a translated string, tolerating a translation with a typo'd
    or missing placeholder rather than crashing the whole alert dispatch."""
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError) as e:
        logger.warning("Translation string format failed (%s): %r", e, template)
        return template


# --------------------------------------------------------------------------------------
def reminder_due(state: MonitorState, cooldown: int) -> bool:
    """True when a monitor that is still down is due another down alert.

    A status change (down or up) is always alerted; this decides only the
    repeat alerts sent while a monitor stays down. One is due once
    `cooldown` seconds have passed since the last alert for this monitor
    (of either kind), or if none has been sent yet.
    """
    if state.last_alert_sent is None:
        return True

    try:
        last = datetime.fromisoformat(state.last_alert_sent)
    except (ValueError, TypeError):
        return True

    elapsed = datetime.now(timezone.utc) - last
    return elapsed.total_seconds() >= cooldown


# --------------------------------------------------------------------------------------
async def send_email_alert(
    config: dict,
    monitor_name: str,
    status: str,
    error: str | None,
    response_ms: float,
    expected_status: int | None = None,
    recipient: dict | None = None,
) -> bool:
    """Send an email alert via SMTP, to one `recipients` entry of the email
    alert (its `to` addresses).

    Returns True if the SMTP server accepted the message for delivery, False if
    it was rejected or the send failed. Acceptance is transport-level only: it
    doesn't mean the email reached an inbox (it could still be spam-filtered
    downstream with no bounce).
    """
    email_config = config["alerts"]["email"]
    if recipient is not None:
        email_config = email_recipient_config(email_config, recipient)

    subject_prefix = email_config.get("subject_prefix", "[Uptime Herald]")
    t = _t(config)
    # Same None guard as _build_markdown_alert_text: formatting None with
    # :.1f raises, and the resulting alert is swallowed by the caller.
    latency = "—" if response_ms is None else f"{response_ms:.1f}ms"
    if is_up(status, expected_status):
        subject = f"{subject_prefix} {monitor_name} {t.gettext('is UP')}"
        body = (
            _safe_format(
                t.gettext("Monitor '{name}' has recovered."), name=monitor_name
            )
            + "\n" + t.gettext("Latency:") + f" {latency}"
        )
    else:
        subject = f"{subject_prefix} {monitor_name} {t.gettext('is DOWN')}"
        body = (
            _safe_format(t.gettext("Monitor '{name}' is DOWN."), name=monitor_name)
            + "\n"
            + t.gettext("Error:") + f" {error or t.gettext('Unknown')}\n"
            + t.gettext("Latency:") + f" {latency}"
        )

    message = EmailMessage()
    from_addr = email_config["from"]
    from_name = email_config.get("from_name", "Uptime Herald")
    message["From"] = f"{from_name} <{from_addr}>"
    message["To"] = ", ".join(email_config["to"])
    message["Subject"] = subject
    message.set_content(body)

    try:
        await aiosmtplib.send(
            message,
            hostname=email_config["smtp_host"],
            port=email_config["smtp_port"],
            username=email_config.get("smtp_user"),
            password=email_config.get("smtp_password"),
            start_tls=True,
            timeout=30,
        )
        logger.info("Email alert sent for '%s': %s", monitor_name, status)
        return True
    except Exception as e:
        logger.error("Failed to send email alert for '%s': %s", monitor_name, e)
        return False


# --------------------------------------------------------------------------------------
async def send_push_alert(
    channel: dict,
    monitor_name: str,
    status: str,
    error: str | None,
    response_ms: float,
    config: dict,
    expected_status: int | None = None,
) -> bool:
    """Send a push alert to a notification channel.

    Each channel dict has a 'type' field that determines the delivery method:
    - discord: Discord webhook (content field)
    - mattermost: Mattermost webhook (text field, markdown)
    - rocket.chat: Rocket.Chat webhook (text field, markdown)
    - slack: Slack webhook (text field, Slack markup)
    - telegram: Telegram Bot API (chat_id + text)
    - ntfy: ntfy notification service (self-hosted or ntfy.sh)
    - callmebot: CallMeBot API (WhatsApp, Signal, Facebook Messenger)
    - generic: raw JSON POST to a webhook URL

    Returns True if the channel's API/webhook confirmed acceptance (HTTP
    status < 400), False otherwise.
    """
    channel_type = channel.get("type", "generic")
    timestamp = datetime.now(timezone.utc).isoformat()
    t = _t(config)

    if channel_type == "telegram":
        return await _send_telegram(
            channel, monitor_name, status, error, response_ms, t, expected_status
        )

    if channel_type == "ntfy":
        return await _send_ntfy(
            channel, monitor_name, status, error, response_ms, t, expected_status
        )

    if channel_type == "callmebot":
        return await _send_callmebot(
            channel, monitor_name, status, error, response_ms, t, expected_status
        )

    # Webhook-based channels (discord, mattermost, rocket.chat, slack, generic)
    url = channel["url"]

    # Declared up front: the per-channel branches below build different
    # shapes -- most a plain {"text": str}, the generic webhook one a
    # mix of float and str | None -- so the union has to be stated once
    # here rather than inferred from whichever branch comes first.
    payload: dict[str, object]
    if channel_type == "discord":
        content = _build_markdown_alert_text(
            monitor_name, status, error, response_ms, t,
            bold="**", multiline_fence=False, expected_status=expected_status,
        )
        payload = {"content": content}
    elif channel_type in ("mattermost", "rocket.chat"):
        text = _build_markdown_alert_text(
            monitor_name, status, error, response_ms, t,
            bold="**", multiline_fence=True, expected_status=expected_status,
        )
        payload = {"text": text}
    elif channel_type == "slack":
        text = _build_markdown_alert_text(
            monitor_name, status, error, response_ms, t,
            bold="*", multiline_fence=True, expected_status=expected_status,
        )
        payload = {"text": text}
    else:
        payload = {
            "monitor": monitor_name,
            "status": status,
            "response_ms": response_ms,
            "error": error,
            "timestamp": timestamp,
        }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "Push alert to %s channel returned %s: %s",
                    channel_type, response.status_code, response.text[:200],
                )
                return False
            logger.info(
                "Push alert sent for '%s' (%s): %s",
                monitor_name, channel_type, status,
            )
            return True
    except Exception as e:
        # Don't log `url` — webhook URLs for discord/slack/mattermost embed
        # a bearer-equivalent secret token directly in the path.
        logger.error(
            "Failed to send push alert for '%s' via %s channel: %s",
            monitor_name, channel_type, e,
        )
        return False


# --------------------------------------------------------------------------------------
async def _send_telegram(
    channel: dict,
    monitor_name: str,
    status: str,
    error: str | None,
    response_ms: float,
    t,
    expected_status: int | None = None,
) -> bool:
    """Send an alert via the Telegram Bot API.

    Returns True if the Bot API confirmed acceptance, False otherwise.
    """
    bot_token = channel["bot_token"]
    chat_id = channel["chat_id"]

    text = _build_markdown_alert_text(
        monitor_name, status, error, response_ms, t,
        bold="*", multiline_fence=True, expected_status=expected_status,
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "Telegram push alert returned %s: %s",
                    response.status_code, response.text[:200],
                )
                return False
            logger.info(
                "Telegram push alert sent for '%s': %s",
                monitor_name, status,
            )
            return True
    except Exception as e:
        logger.error(
            "Failed to send Telegram push alert for '%s': %s",
            monitor_name, e,
        )
        return False


# --------------------------------------------------------------------------------------
async def _send_ntfy(
    channel: dict,
    monitor_name: str,
    status: str,
    error: str | None,
    response_ms: float,
    t,
    expected_status: int | None = None,
) -> bool:
    """Send an alert via ntfy notification service.

    Supports self-hosted ntfy instances or the public ntfy.sh service.
    Authentication is optional:
    - username + password: HTTP Basic auth
    - token: Bearer token auth
    - neither: no auth (public topics)

    Returns True if the ntfy server accepted the message into the topic, False
    otherwise.
    """
    url = channel["url"]
    priority = channel.get("priority", "default")
    token = channel.get("token")
    username = channel.get("username")
    password = channel.get("password")

    if is_up(status, expected_status):
        title = f"{monitor_name} {t.gettext('is UP')}"
        body = f"✅ {t.gettext('Recovered')}"
        # Guarded like the DOWN branch below -- see
        # _build_markdown_alert_text for why a None here loses the alert.
        if response_ms is not None:
            body += f" — {response_ms:.0f}ms"
    else:
        title = f"{monitor_name} {t.gettext('is DOWN')}"
        body = f"🔴 {t.gettext('Error:')} {error or t.gettext('Unknown')}"
        if response_ms is not None:
            body += f"\n{t.gettext('Latency:')} {response_ms:.0f}ms"

    headers = {
        "Title": title,
        "Priority": priority,
    }

    if username is not None:
        creds = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            response = await client.post(
                url,
                content=body.encode("utf-8"),
                headers=headers,
            )
            if response.status_code >= 400:
                logger.warning(
                    "ntfy push alert returned %s: %s",
                    response.status_code, response.text[:200],
                )
                return False
            logger.info(
                "ntfy push alert sent for '%s': %s",
                monitor_name, status,
            )
            return True
    except Exception as e:
        # Don't log `url` — ntfy topic URLs act as an unlisted-secret auth
        # mechanism on the public ntfy.sh service.
        logger.error(
            "Failed to send ntfy push alert for '%s': %s",
            monitor_name, e,
        )
        return False


# --------------------------------------------------------------------------------------
def callmebot_url(channel: dict, text: str) -> str:
    """The request URL for one CallMeBot message. Each delivery method has its
    own address and its own parameters (see the CallMeBot API pages):

    - whatsapp: phone, text, apikey
    - signal: phone, text, apikey (a different host from the others)
    - facebook: text, apikey (Messenger has no phone number)

    The URL carries the API key, so it is never logged.
    """
    method = channel["channel"]
    if method == "signal":
        base = "https://signal.callmebot.com/signal/send.php"
        params = {"phone": channel["phone"], "text": text,
                  "apikey": channel["api_key"]}
    elif method == "facebook":
        base = "https://api.callmebot.com/facebook/send.php"
        params = {"text": text, "apikey": channel["api_key"]}
    else:
        base = "https://api.callmebot.com/whatsapp.php"
        params = {"phone": channel["phone"], "text": text,
                  "apikey": channel["api_key"]}
    return f"{base}?{urlencode(params)}"


# --------------------------------------------------------------------------------------
def callmebot_accepted(status_code: int, text: str) -> bool:
    """Whether CallMeBot's reply means it accepted the message.

    CallMeBot says so in the reply: "Message queued" (WhatsApp) or "Message
    sent" (Signal and Facebook), with HTTP 200. A wrong API key gets an error
    page instead, and its status code is no guide: HTTP 203 for WhatsApp, HTTP
    200 for Facebook and for Signal. Counting only status codes of 400 and up as
    failures would report all of them as sent, although nothing was delivered.
    """
    return status_code == 200 and (
        "Message queued" in text or "Message sent" in text
    )


# --------------------------------------------------------------------------------------
async def _send_callmebot(
    channel: dict,
    monitor_name: str,
    status: str,
    error: str | None,
    response_ms: float,
    t,
    expected_status: int | None = None,
) -> bool:
    """Send an alert via CallMeBot API.

    Supports WhatsApp, Signal, and Facebook Messenger through one API. The
    channel field determines the delivery method:
    - whatsapp: WhatsApp message
    - signal: Signal message
    - facebook: Facebook Messenger message

    Returns True if CallMeBot's API accepted the request, False otherwise.
    CallMeBot is an unofficial third-party bridge, so True doesn't mean
    WhatsApp, Signal, or Messenger delivered the message.
    """
    channel_type = channel["channel"]

    if is_up(status, expected_status):
        text = f"✅ {monitor_name} {t.gettext('is UP')}"
        # Guarded like the DOWN branch below -- see
        # _build_markdown_alert_text for why a None here loses the alert.
        if response_ms is not None:
            text += f" — {response_ms:.0f}ms"
    else:
        text = f"🔴 {monitor_name} {t.gettext('is DOWN')}"
        if error:
            text += f"\n{t.gettext('Error:')} {error[:200]}"
        if response_ms is not None:
            text += f"\n{t.gettext('Latency:')} {response_ms:.0f}ms"

    url = callmebot_url(channel, text)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            response = await client.get(url)
            if not callmebot_accepted(response.status_code, response.text):
                logger.warning(
                    "CallMeBot push alert returned %s: %s",
                    response.status_code, response.text[:200],
                )
                return False
            logger.info(
                "CallMeBot push alert sent for '%s' (%s): %s",
                monitor_name, channel_type, status,
            )
            return True
    except Exception as e:
        logger.error(
            "Failed to send CallMeBot push alert for '%s': %s",
            monitor_name, e,
        )
        return False


# --------------------------------------------------------------------------------------
def _find_monitor_config(config: dict, monitor_name: str) -> dict | None:
    """Find a monitor's config entry by name (used for tag-based routing)."""
    for m in config.get("monitors", []):
        if m["name"] == monitor_name:
            return m
    return None


# --------------------------------------------------------------------------------------
async def _dispatch_email_alert(
    config: dict,
    monitor_name: str,
    new_status: str,
    error: str | None,
    response_ms: float,
    expected_status: int | None = None,
    monitor_tags: set | None = None,
) -> bool | None:
    """Send the email alert to each of its recipients entries that is on and
    for this monitor, isolating failures.

    Isolated so a malformed email config (or an SMTP-unrelated exception
    raised while building the message, e.g. a missing 'from' key) can't
    prevent push-alert channels from being attempted. Each recipients entry
    is also isolated from the others.

    Returns None if no entry is on and for this monitor (not attempted),
    otherwise True if every attempted send was accepted and False if any was
    not.
    """
    email_config = config.get("alerts", {}).get("email", {})
    results = []
    for index, recipient in enumerate(email_config.get("recipients") or []):
        if not recipient.get("enabled", True):
            continue
        if not _channel_applies(
            recipient, monitor_name, monitor_tags or set(),
            label=f"email recipients[{index}]",
        ):
            continue
        try:
            results.append(await send_email_alert(
                config, monitor_name, new_status, error, response_ms,
                expected_status, recipient=recipient,
            ))
        except Exception as e:
            logger.error(
                "Email alert dispatch failed for '%s' (recipients[%d]): %s",
                monitor_name, index, e,
            )
            results.append(False)
    if not results:
        return None
    return all(results)


# --------------------------------------------------------------------------------------
def _channel_applies(
    channel: dict,
    monitor_name: str,
    monitor_tags: set,
    label: str | None = None,
) -> bool:
    """Check whether an alert channel is for this monitor, by the channel's
    `monitors` and `tags` settings (see entry_applies_to_monitor). Logs and
    returns False when it isn't. `label` names the channel in the log line;
    push channels default to their type.
    """
    if entry_applies_to_monitor(channel, monitor_name, monitor_tags):
        return True
    logger.info(
        "Skipping alert for '%s' on %s channel — it isn't one of the "
        "channel's monitors (%s) and the monitor's tags %s don't match the "
        "channel's tags (%s)",
        monitor_name, label or channel.get("type", "generic"),
        channel.get("monitors"), monitor_tags or {}, channel.get("tags"),
    )
    return False


# --------------------------------------------------------------------------------------
async def _dispatch_push_alert_channel(
    channel: dict,
    config: dict,
    monitor_name: str,
    new_status: str,
    error: str | None,
    response_ms: float,
    monitor_tags: set,
    expected_status: int | None = None,
) -> bool | None:
    """Send one push-alert channel, respecting its monitors, tags and enable flag.

    Isolated per-channel: a malformed channel config (e.g. a discord entry
    missing 'url') must not stop remaining channels from being tried.

    Returns None if the channel is disabled or isn't for this monitor (not
    attempted), True/False for whether an attempted send was accepted.
    """
    if not channel.get("enabled", True):
        return None
    if not _channel_applies(channel, monitor_name, monitor_tags):
        return None
    try:
        return await send_push_alert(
            channel=channel,
            monitor_name=monitor_name,
            status=new_status,
            error=error,
            response_ms=response_ms,
            config=config,
            expected_status=expected_status,
        )
    except Exception as e:
        logger.error(
            "Push alert dispatch failed for '%s' on %s channel: %s",
            monitor_name, channel.get("type", "generic"), e,
        )
        return False


# --------------------------------------------------------------------------------------
async def dispatch_alerts(
    config: dict,
    state: MonitorState,
    new_status: str,
    error: str | None,
    response_ms: float,
    repeat_after: int | None = None,
) -> None:
    """Dispatch alerts for a status change, or a repeat down alert.

    A status change (repeat_after=None) is always sent. For a monitor that
    is still down, pass repeat_after (seconds): the alert is then sent only
    once that long has passed since the last alert -- see reminder_due().

    Attempts email and every configured push channel, collecting each
    one's transport-level result: None if it wasn't attempted (disabled
    or not for this monitor), True if the receiving service confirmed
    acceptance, False if it was attempted and rejected/failed. This only
    confirms the message left our hands successfully — not that a human
    ever saw it, which isn't observable from here for any channel.

    state.last_alert_sent (the repeat-alert timestamp) advances whenever at
    least one channel was attempted, whether or not it succeeded: a failed
    dispatch still waits out the normal cooldown instead of retrying on every
    check cycle. It isn't advanced when nothing was attempted (no channels
    configured or enabled for this monitor), since there is nothing to send.
    """
    monitor_name = state.name

    # An http monitor's expected_status changes what "up"/"down" mean for
    # every is_up() call downstream, not just tag-based push routing.
    monitor_config = _find_monitor_config(config, monitor_name)
    expected_status = monitor_config.get("expected_status") if monitor_config else None

    if repeat_after is not None and not reminder_due(state, repeat_after):
        return

    # Push alerts (discord, mattermost, rocket.chat, slack, telegram, ntfy,
    # callmebot, generic)
    monitor_tags = set(monitor_config.get("tags", [])) if monitor_config else set()

    # Every channel is an independent outbound request to a different service,
    # so they run concurrently. Sequentially, each channel's own timeout would
    # stack: eight channels at the default 10s timeout could occupy a single
    # alert dispatch for ~80s, and because this is awaited from run_check()
    # inside the monitoring loop's task, one unreachable webhook would delay
    # every other channel's alert behind it.
    #
    # return_exceptions=True so one channel raising can't cancel the others
    # mid-flight; an exception counts as an attempted-and-failed channel, like
    # the per-channel False result. The individual dispatchers already catch
    # and log their own transport errors, so anything arriving here is
    # unexpected and worth its own log line.
    coros = [
        _dispatch_email_alert(
            config, monitor_name, new_status, error, response_ms, expected_status,
            monitor_tags,
        )
    ]
    coros += [
        _dispatch_push_alert_channel(
            channel, config, monitor_name, new_status, error, response_ms,
            monitor_tags, expected_status,
        )
        for channel in config["alerts"].get("push_alerts", [])
    ]

    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    # None means "not attempted" (channel disabled or not for this monitor),
    # which is distinct from False ("attempted and failed") -- the
    # cooldown only advances when something was actually attempted.
    results: list[bool | None] = []
    for r in raw_results:
        if isinstance(r, BaseException):
            logger.error(
                "Alert channel raised unexpectedly for '%s': %s", monitor_name, r,
            )
            results.append(False)  # attempted, did not succeed
        else:
            results.append(r)

    attempted = [r for r in results if r is not None]
    if not attempted:
        logger.debug(
            "No alert channels configured/enabled for '%s' — nothing to send",
            monitor_name,
        )
        return
    if not any(attempted):
        logger.warning(
            "All %d attempted alert channel(s) failed for '%s' — will "
            "retry on the normal cooldown schedule",
            len(attempted), monitor_name,
        )

    state.last_alert_sent = datetime.now(timezone.utc).isoformat()
    # Persist immediately — callers save `state` before dispatch_alerts()
    # runs (so alert failures don't block the state-change write), which
    # means this mutation is the only place the cooldown timestamp is ever
    # written. Without this, last_alert_sent lives only in memory and is
    # lost on the next db.load_state() reload or process restart, breaking
    # cooldown enforcement.
    await db.save_state(state)
