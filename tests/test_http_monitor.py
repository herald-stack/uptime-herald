# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_http_monitor.py - Tests for the HTTP monitor check.
"""

import ssl
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.monitors.base import is_up
from src.monitors.http import check_http


def _fake_response(status_code: int, url: str) -> httpx.Response:
    """Build a real httpx.Response with no network I/O behind it."""
    return httpx.Response(status_code=status_code, request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_http_up():
    url = "http://httpbin.org/get"
    with patch(
        "httpx.AsyncClient.get", AsyncMock(return_value=_fake_response(200, url))
    ):
        result = await check_http(
            url=url,
            timeout=10,
        )
    assert result.status == "200"
    assert is_up(result.status) is True
    assert result.response_ms > 0
    assert result.details.get("status_code") == 200


@pytest.mark.asyncio
async def test_http_wrong_status():
    url = "http://httpbin.org/status/404"
    with patch(
        "httpx.AsyncClient.get", AsyncMock(return_value=_fake_response(404, url))
    ):
        result = await check_http(
            url=url,
            timeout=10,
        )
    assert result.status == "404"
    assert is_up(result.status) is False
    assert result.details.get("status_code") == 404


@pytest.mark.asyncio
async def test_http_timeout():
    url = "http://192.0.2.1:9999/"
    with patch(
        "httpx.AsyncClient.get",
        AsyncMock(side_effect=httpx.TimeoutException("timed out")),
    ):
        result = await check_http(
            url=url,
            timeout=2,
        )
    assert result.status == "timeout"
    assert is_up(result.status) is False
    assert result.error is not None


@pytest.mark.asyncio
async def test_https_valid_cert():
    url = "https://httpbin.org/get"
    with patch(
        "httpx.AsyncClient.get", AsyncMock(return_value=_fake_response(200, url))
    ):
        result = await check_http(
            url=url,
            timeout=10,
        )
    assert result.status == "200"
    assert is_up(result.status) is True


@pytest.mark.asyncio
async def test_http_connect_error():
    """A refused/unreachable connection is a distinct failure mode from a
    timeout -- check_http has a specific handler for it."""
    url = "http://192.0.2.1:9999/"
    with patch(
        "httpx.AsyncClient.get",
        AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    ):
        result = await check_http(url=url, timeout=2)
    assert result.status == "failed"
    assert is_up(result.status) is False
    assert "refused" in result.error


@pytest.mark.asyncio
async def test_http_5xx_is_down():
    url = "http://example.com/"
    with patch(
        "httpx.AsyncClient.get", AsyncMock(return_value=_fake_response(503, url))
    ):
        result = await check_http(url=url, timeout=10)
    assert result.status == "503"
    assert is_up(result.status) is False


class _RecordingAsyncClient:
    """Stand-in for httpx.AsyncClient that records constructor kwargs and
    the .get() call's headers, so verify_ssl and the User-Agent contents
    can be asserted without depending on real network I/O."""

    last_init_kwargs = None
    last_get_headers = None
    response = None

    def __init__(self, **kwargs):
        type(self).last_init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, auth=None):
        type(self).last_get_headers = headers
        return type(self).response


# check_http passes a shared, cached ssl.SSLContext rather than the raw
# verify_ssl bool (building one per check cost ~80ms of blocking CPU work
# on the event loop). These assert the setting's actual effect on the
# context handed to httpx, which is a stronger check than asserting the bool
# was passed through: it would catch a context built with the wrong verify
# mode, where asserting `verify is False` would only check the argument's shape.
@pytest.mark.asyncio
async def test_http_verify_ssl_false_disables_verification():
    _RecordingAsyncClient.response = _fake_response(200, "https://example.com")
    with patch("httpx.AsyncClient", _RecordingAsyncClient):
        await check_http(url="https://example.com", timeout=10, verify_ssl=False)
    ctx = _RecordingAsyncClient.last_init_kwargs["verify"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode is ssl.CERT_NONE


@pytest.mark.asyncio
async def test_http_verify_ssl_defaults_to_verifying():
    _RecordingAsyncClient.response = _fake_response(200, "https://example.com")
    with patch("httpx.AsyncClient", _RecordingAsyncClient):
        await check_http(url="https://example.com", timeout=10)
    ctx = _RecordingAsyncClient.last_init_kwargs["verify"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode is ssl.CERT_REQUIRED


@pytest.mark.asyncio
async def test_http_ssl_context_is_reused_across_checks():
    """The whole point of the cache -- two checks with the same
    verify_ssl setting must not each build a fresh context."""
    _RecordingAsyncClient.response = _fake_response(200, "https://example.com")
    with patch("httpx.AsyncClient", _RecordingAsyncClient):
        await check_http(url="https://example.com", timeout=10)
        first = _RecordingAsyncClient.last_init_kwargs["verify"]
        await check_http(url="https://example.com", timeout=10)
        second = _RecordingAsyncClient.last_init_kwargs["verify"]
    assert first is second


@pytest.mark.asyncio
async def test_http_contact_email_appended_to_user_agent():
    _RecordingAsyncClient.response = _fake_response(200, "https://example.com")
    with patch("httpx.AsyncClient", _RecordingAsyncClient):
        await check_http(
            url="https://example.com", timeout=10,
            contact_email="ops@example.com",
        )
    ua = _RecordingAsyncClient.last_get_headers["User-Agent"]
    assert "mailto:ops@example.com" in ua


@pytest.mark.asyncio
async def test_http_unexpected_exception_falls_back_to_failed():
    """Any exception besides TimeoutException/ConnectError (e.g. a DNS
    resolution error) is still caught by the broad handler and reported
    as a 'failed' check rather than crashing the monitoring loop."""
    url = "http://does-not-resolve.invalid/"
    with patch(
        "httpx.AsyncClient.get",
        AsyncMock(side_effect=ValueError("unexpected httpx internal error")),
    ):
        result = await check_http(url=url, timeout=2)
    assert result.status == "failed"
    assert is_up(result.status) is False
    assert "unexpected" in result.error


@pytest.mark.asyncio
async def test_http_no_contact_email_omits_mailto():
    _RecordingAsyncClient.response = _fake_response(200, "https://example.com")
    with patch("httpx.AsyncClient", _RecordingAsyncClient):
        await check_http(url="https://example.com", timeout=10)
    ua = _RecordingAsyncClient.last_get_headers["User-Agent"]
    assert "mailto:" not in ua


@pytest.mark.asyncio
async def test_http_sends_the_credential_when_given_one():
    url = "http://example.com/admin"
    get = AsyncMock(return_value=_fake_response(200, url))
    with patch("httpx.AsyncClient.get", get):
        result = await check_http(url=url, timeout=10, auth=("monitor", "p@ss:w0rd"))
    assert get.call_args.kwargs["auth"] == ("monitor", "p@ss:w0rd")
    assert result.details["url"] == url  # the stored url never carries the credential


@pytest.mark.asyncio
async def test_http_sends_no_credential_by_default():
    url = "http://example.com/"
    get = AsyncMock(return_value=_fake_response(200, url))
    with patch("httpx.AsyncClient.get", get):
        await check_http(url=url, timeout=10)
    assert get.call_args.kwargs["auth"] is None
