# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
system_notice.py - Delivers system notices through the alert channels.

Deliver system notices -- messages about Uptime Herald itself (such as a
database approaching end of support) rather than about a monitor.

They go out through the same channels as monitor alerts. Which ones:

  * every enabled channel (each entry in the email block's recipients, and
    each entry in push_alerts), except one that says `system: false` in
    alerts.yaml.

`system` is true when it is left out, so a channel has to say no. If every
enabled channel says `system: false`, a notice is only written to the log.

alerts.py's senders are built around a monitor name, status and latency,
so this module has its own small senders using the same wire formats.
"""

import base64
import logging
from datetime import datetime, timezone
from email.message import EmailMessage

import aiosmtplib
import httpx

from src.alerts import _t, callmebot_accepted, callmebot_url
from src.config import email_recipient_config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
def system_targets(config: dict) -> list[tuple[str, dict]]:
    """The ("email" | "push", channel config) pairs a notice should go to.

    Every enabled channel, except one with `system: false`. Possibly empty.
    """
    alerts_cfg = config.get("alerts", {}) or {}
    candidates: list[tuple[str, dict]] = []
    email_cfg = alerts_cfg.get("email") or {}
    for recipient in email_cfg.get("recipients") or []:
        if recipient.get("enabled", True):
            candidates.append(("email", email_recipient_config(email_cfg, recipient)))
    for channel in alerts_cfg.get("push_alerts") or []:
        if channel.get("enabled", True):
            candidates.append(("push", channel))
    # `system` is true unless the channel says false.
    return [c for c in candidates if c[1].get("system", True)]


# --------------------------------------------------------------------------------------
async def send_system_notice(config: dict, title: str, body: str) -> int:
    """Send a notice to its targets; returns how many accepted it.

    Each target is isolated: one broken channel can't stop the others.
    """
    sent = 0
    # Notices use their own subject prefix, not the email alert's
    # subject_prefix, which belongs to monitor alerts. It is not configurable.
    subject_prefix = _t(config).gettext("[Uptime Herald Notice]")
    for kind, channel in system_targets(config):
        try:
            if kind == "email":
                ok = await _send_email(channel, subject_prefix, title, body)
            else:
                ok = await _send_push(channel, title, body)
        except Exception as e:
            logger.error(
                "System notice failed on %s channel: %s",
                channel.get("type", kind), e,
            )
            ok = False
        if ok:
            sent += 1
    if not sent and system_targets(config):
        logger.warning("No channel accepted the system notice %r", title)
    return sent


# --------------------------------------------------------------------------------------
async def _send_email(
    email_cfg: dict, subject_prefix: str, title: str, body: str,
) -> bool:
    message = EmailMessage()
    from_name = email_cfg.get("from_name", "Uptime Herald")
    message["From"] = f"{from_name} <{email_cfg['from']}>"
    message["To"] = ", ".join(email_cfg["to"])
    message["Subject"] = f"{subject_prefix} {title}"
    message.set_content(body)
    await aiosmtplib.send(
        message,
        hostname=email_cfg["smtp_host"],
        port=email_cfg["smtp_port"],
        username=email_cfg.get("smtp_user"),
        password=email_cfg.get("smtp_password"),
        start_tls=True,
        timeout=30,
    )
    return True


# Webhook channels that take a single text field: type -> (field, bold mark).
_TEXT_WEBHOOKS = {
    "discord": ("content", "**"),
    "mattermost": ("text", "**"),
    "rocket.chat": ("text", "**"),
    "slack": ("text", "*"),
}


# --------------------------------------------------------------------------------------
def _ntfy_request(channel: dict, title: str, body: str) -> tuple[str, str, dict]:
    headers = {
        "Title": title,
        "Priority": channel.get("priority", "default"),
    }
    if channel.get("username") is not None:
        creds = base64.b64encode(
            f"{channel['username']}:{channel.get('password') or ''}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {creds}"
    elif channel.get("token") is not None:
        headers["Authorization"] = f"Bearer {channel['token']}"
    return "POST", channel["url"], {
        "content": body.encode("utf-8"), "headers": headers,
    }


# --------------------------------------------------------------------------------------
def _callmebot_request(
    channel: dict, title: str, body: str,
) -> tuple[str, str, dict]:
    return "GET", callmebot_url(channel, f"{title}\n{body}"), {}


# --------------------------------------------------------------------------------------
def _build_push_request(
    channel: dict, title: str, body: str,
) -> tuple[str, str, dict]:
    """(method, url, httpx kwargs) for one push channel."""
    ctype = channel.get("type", "generic")
    if ctype in _TEXT_WEBHOOKS:
        field, mark = _TEXT_WEBHOOKS[ctype]
        return "POST", channel["url"], {
            "json": {field: f"{mark}{title}{mark}\n{body}"},
        }
    if ctype == "telegram":
        url = f"https://api.telegram.org/bot{channel['bot_token']}/sendMessage"
        # No parse_mode: the text is plain, and Markdown parsing would choke
        # on underscores or brackets in a version string or URL.
        return "POST", url, {
            "json": {"chat_id": channel["chat_id"], "text": f"{title}\n{body}"},
        }
    if ctype == "ntfy":
        return _ntfy_request(channel, title, body)
    if ctype == "callmebot":
        return _callmebot_request(channel, title, body)
    return "POST", channel["url"], {
        "json": {
            "type": "system_notice",
            "title": title,
            "message": body,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }


# --------------------------------------------------------------------------------------
async def _send_push(channel: dict, title: str, body: str) -> bool:
    method, url, kwargs = _build_push_request(channel, title, body)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
        if method == "GET":
            response = await client.get(url)
        else:
            response = await client.post(url, **kwargs)
    if channel.get("type") == "callmebot":
        accepted = callmebot_accepted(response.status_code, response.text)
    else:
        accepted = response.status_code < 400
    if not accepted:
        # The URL is deliberately not logged: webhook and ntfy URLs carry
        # a secret in the path.
        logger.warning(
            "System notice to %s channel returned %s: %s",
            channel.get("type", "generic"), response.status_code,
            response.text[:200],
        )
        return False
    return True
