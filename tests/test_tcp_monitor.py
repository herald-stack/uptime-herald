# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_tcp_monitor.py - Tests for the TCP monitor check.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.monitors.tcp import check_tcp


def _mock_writer():
    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    return writer


@pytest.mark.asyncio
async def test_tcp_up():
    """An open port should report status up."""
    reader = MagicMock()
    writer = _mock_writer()
    with patch(
        "asyncio.open_connection", AsyncMock(return_value=(reader, writer))
    ):
        result = await check_tcp(host="httpbin.org", port=80, timeout=10)
    assert result.status == "up"
    # >= 0, not > 0: a fully mocked connection can complete within the clock's
    # resolution, so the measured time can be exactly 0.0.
    assert result.response_ms >= 0


@pytest.mark.asyncio
async def test_tcp_down():
    """An unreachable host should fail with an error."""
    with patch(
        "asyncio.open_connection",
        AsyncMock(side_effect=OSError("Network is unreachable")),
    ):
        result = await check_tcp(host="192.0.2.1", port=9999, timeout=2)
    assert result.status == "down"
    assert result.error is not None


@pytest.mark.asyncio
async def test_tcp_closed_port():
    """A host that's up but with the port closed should fail."""
    with patch(
        "asyncio.open_connection",
        AsyncMock(side_effect=ConnectionRefusedError("Connection refused")),
    ):
        result = await check_tcp(host="httpbin.org", port=9999, timeout=5)
    assert result.status == "down"


async def _raise_timeout(coro, timeout):
    """Stand-in for asyncio.wait_for that raises TimeoutError immediately
    instead of actually waiting, and closes the passed coroutine so it
    doesn't trigger a 'coroutine was never awaited' warning."""
    coro.close()
    raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_tcp_timeout():
    """A connection attempt that never resolves within `timeout` should
    report down with a timeout-specific error, distinct from a refused
    or unreachable connection."""
    with patch("asyncio.wait_for", AsyncMock(side_effect=_raise_timeout)):
        result = await check_tcp(host="192.0.2.1", port=80, timeout=2)
    assert result.status == "down"
    assert "timed out" in result.error.lower()


@pytest.mark.asyncio
async def test_tcp_unexpected_exception_returns_down():
    with patch(
        "asyncio.open_connection", AsyncMock(side_effect=ValueError("boom")),
    ):
        result = await check_tcp(host="8.8.8.8", port=80, timeout=2)
    assert result.status == "down"
    assert result.error == "boom"


@pytest.mark.asyncio
@pytest.mark.parametrize("host, looked_up", [
    ("stra\u00dfe.de", "xn--strae-oqa.de"),      # IDNA 2008, not "strasse.de"
    ("M\u00fcller.DE", "xn--mller-kva.de"),
    ("example.com", "example.com"),
    ("my_database", "my_database"),
    ("192.168.1.42", "192.168.1.42"),
])
async def test_tcp_looks_up_the_dns_form_of_the_host(host, looked_up):
    """Non-ASCII names are converted like ping and http do; ASCII is untouched."""
    connect = AsyncMock(return_value=(MagicMock(), _mock_writer()))
    with patch("asyncio.open_connection", connect):
        result = await check_tcp(host=host, port=80, timeout=10)
    assert result.status == "up"
    connect.assert_called_once_with(looked_up, 80)
