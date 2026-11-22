# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_ping_monitor.py - Tests for the ping monitor check.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.monitors.ping import check_ping


def _mock_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    """Build a fake asyncio subprocess handle with no real process behind it."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@pytest.mark.asyncio
async def test_ping_up():
    """A host that responds should report status up with a parsed latency."""
    proc = _mock_proc(
        returncode=0,
        stdout=b"64 bytes from 8.8.8.8: icmp_seq=1 ttl=115 time=12.3 ms\n",
    )
    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        result = await check_ping(host="8.8.8.8", timeout=5)
    assert result.status == "up"
    assert result.response_ms > 0


@pytest.mark.asyncio
async def test_ping_down():
    """An unreachable host should report status down with an error."""
    proc = _mock_proc(
        returncode=1,
        stderr=b"ping: sendto: Host is unreachable\n",
    )
    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        result = await check_ping(host="192.0.2.1", timeout=2)
    assert result.status == "down"
    assert result.error is not None


async def _raise_timeout(coro, timeout):
    """Stand-in for asyncio.wait_for that raises TimeoutError immediately
    instead of actually waiting, and closes the passed coroutine so it
    doesn't trigger a 'coroutine was never awaited' warning."""
    coro.close()
    raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_ping_timeout_kills_still_running_process():
    """A ping that hangs past the timeout should kill the subprocess
    (it's still running -- returncode is None) and report down with a
    timeout error, not leak the process."""
    proc = _mock_proc(returncode=None)

    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ), patch(
        "src.monitors.ping.asyncio.wait_for",
        AsyncMock(side_effect=_raise_timeout),
    ):
        result = await check_ping(host="192.0.2.1", timeout=2)

    assert result.status == "down"
    assert "timed out" in result.error.lower()
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_ping_timeout_process_already_exited_skips_kill():
    """If the process already finished (returncode is set) by the time the
    timeout fires, kill()/wait() must not be called."""
    proc = _mock_proc(returncode=0)

    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ), patch(
        "src.monitors.ping.asyncio.wait_for",
        AsyncMock(side_effect=_raise_timeout),
    ):
        result = await check_ping(host="192.0.2.1", timeout=2)

    assert result.status == "down"
    proc.kill.assert_not_called()
    proc.wait.assert_not_called()


@pytest.mark.asyncio
async def test_ping_timeout_kill_process_lookup_error_is_swallowed():
    """A process that exits in the race window between the timeout check
    and kill() raises ProcessLookupError -- this must be swallowed, not
    propagate out of check_ping."""
    proc = _mock_proc(returncode=None)
    proc.kill = MagicMock(side_effect=ProcessLookupError())

    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ), patch(
        "src.monitors.ping.asyncio.wait_for",
        AsyncMock(side_effect=_raise_timeout),
    ):
        result = await check_ping(host="192.0.2.1", timeout=2)

    assert result.status == "down"
    assert "timed out" in result.error.lower()


@pytest.mark.asyncio
async def test_ping_latency_regex_parses_time_field():
    """The reported latency should come from ping's own `time=` field, not
    our own wall-clock measurement of the whole subprocess round-trip."""
    proc = _mock_proc(
        returncode=0,
        stdout=b"64 bytes from 8.8.8.8: icmp_seq=1 ttl=115 time=42.7 ms\n",
    )
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await check_ping(host="8.8.8.8", timeout=5)
    assert result.response_ms == 42.7


@pytest.mark.asyncio
async def test_ping_latency_preserves_sub_decimal_precision():
    """response_ms is not rounded to 1 decimal in ping.py itself, so the
    configurable latency_decimals display setting (config.py) sees real
    precision. Rounding happens only at display/aggregation time."""
    proc = _mock_proc(
        returncode=0,
        stdout=b"64 bytes from 8.8.8.8: icmp_seq=1 ttl=115 time=42.738 ms\n",
    )
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await check_ping(host="8.8.8.8", timeout=5)
    assert result.response_ms == 42.738


@pytest.mark.asyncio
async def test_ping_latency_falls_back_when_output_unparseable():
    """If ping's stdout doesn't contain a recognizable time= field (e.g. a
    ping variant with a different output format), fall back to our own
    elapsed-time measurement instead of crashing on match.group(1)."""
    proc = _mock_proc(
        returncode=0,
        stdout=b"some unrecognized ping output with no time field\n",
    )
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await check_ping(host="8.8.8.8", timeout=5)
    assert result.status == "up"
    assert result.response_ms >= 0  # came from elapsed_ms(), not a regex match


@pytest.mark.asyncio
async def test_ping_count_greater_than_one_passed_to_subprocess():
    proc = _mock_proc(returncode=0, stdout=b"time=5.0 ms\n")
    mock_exec = AsyncMock(return_value=proc)
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await check_ping(host="8.8.8.8", timeout=5, count=3)

    args = mock_exec.call_args[0]
    assert "-c" in args
    assert args[args.index("-c") + 1] == "3"


@pytest.mark.asyncio
async def test_ping_unexpected_exception_returns_down():
    """An unanticipated exception (e.g. subprocess creation failing
    outright) should be caught and reported as a down result, not raised."""
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await check_ping(host="8.8.8.8", timeout=2)

    assert result.status == "down"
    assert result.error == "boom"
