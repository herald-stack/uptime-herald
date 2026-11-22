# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
http.py - The HTTP monitor check.
"""

import logging
import ssl
import time

import httpx

from src.monitors.base import CheckResult, elapsed_ms

logger = logging.getLogger(__name__)

# SSL contexts, cached by the verify_ssl setting they were built for.
#
# Building one means reading and parsing the system CA bundle, which
# measured ~80ms on an ARM SBC -- and it is synchronous CPU work, so
# constructing a client per check stalled the whole event loop (every
# other monitor, and every in-flight dashboard request) for that long on
# every single HTTP check. The context depends on nothing but verify_ssl,
# so there are at most two of them for the life of the process.
#
# Only the context is shared, not the AsyncClient: a client is still
# created per check so connection reuse, cookies and redirect state stay
# per-check.
_SSL_CONTEXTS: dict[bool, ssl.SSLContext] = {}


# --------------------------------------------------------------------------------------
def _ssl_context(verify_ssl: bool):
    """Return the shared SSL context for this verify_ssl setting."""
    if verify_ssl not in _SSL_CONTEXTS:
        _SSL_CONTEXTS[verify_ssl] = httpx.create_ssl_context(verify=verify_ssl)
    return _SSL_CONTEXTS[verify_ssl]


# --------------------------------------------------------------------------------------
async def check_http(
    url: str,
    timeout: float = 10.0,
    verify_ssl: bool = True,
    contact_email: str | None = None,
    auth: tuple[str, str] | None = None,
) -> CheckResult:
    """Check an HTTP endpoint and return the actual status code.

    Returns a CheckResult where status is the HTTP status code as a
    string (e.g. "200", "503") or one of the fixed error values
    "timeout" or "failed" for failures. The is_up() helper in base.py
    determines health from this value.
    """
    start = time.monotonic()

    # Build a User-Agent that identifies Uptime Herald so analytics
    # tools can filter it. If contact_email is configured, append it
    # for site owners to reach you.
    user_agent = "UptimeHerald/1.0 (uptime monitor)"
    if contact_email:
        user_agent = f"UptimeHerald/1.0 (uptime monitor; +mailto:{contact_email})"

    headers = {"User-Agent": user_agent}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            verify=_ssl_context(verify_ssl),
        ) as client:
            response = await client.get(url, headers=headers, auth=auth)
            return CheckResult(
                status=str(response.status_code),
                response_ms=elapsed_ms(start),
                details={
                    "status_code": response.status_code,
                    "url": str(response.url),
                },
            )
    except httpx.TimeoutException:
        return CheckResult(
            status="timeout",
            response_ms=elapsed_ms(start),
            error="Request timed out",
        )
    except httpx.ConnectError as e:
        return CheckResult(
            status="failed",
            response_ms=elapsed_ms(start),
            error=str(e),
        )
    except Exception as e:
        logger.warning("HTTP check error for %s: %s", url, e)
        return CheckResult(
            status="failed",
            response_ms=elapsed_ms(start),
            error=str(e),
        )
