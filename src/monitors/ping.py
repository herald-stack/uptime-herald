# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
ping.py - The ping monitor check.
"""

import asyncio
import logging
import re
import time

from src.monitors.base import CheckResult, elapsed_ms

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(r"time[=<](\d+\.?\d*)\s*ms", re.IGNORECASE)


# --------------------------------------------------------------------------------------
async def check_ping(
    host: str,
    timeout: float = 10.0,
    count: int = 1,
) -> CheckResult:
    start = time.monotonic()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping",
            "-c", str(count),
            # `ping -W` only takes whole seconds; round rather than
            # truncate so e.g. 4.5s doesn't silently become 4s, and
            # never go below 1s (int(0.x) would pass -W 0, meaning
            # "no timeout" on some ping implementations).
            "-W", str(max(1, round(timeout))),
            # `--` terminates option parsing, so a host that starts with
            # a dash is treated as a host and not as a ping flag. Not an
            # injection vector (create_subprocess_exec never invokes a
            # shell, and the target comes from operator-owned config),
            # but without it a target like "-c" produces a baffling ping
            # usage error rather than a clean "host unreachable".
            "--",
            host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout + 2,
        )
        stdout_str = stdout.decode("utf-8", errors="replace")

        if proc.returncode == 0:
            match = _TIME_RE.search(stdout_str)
            # Not rounded here -- see elapsed_ms()'s docstring (base.py) for
            # why response_ms is rounded at display time.
            ping_ms = float(match.group(1)) if match else elapsed_ms(start)
            return CheckResult(
                status="up",
                response_ms=ping_ms,
                details={"host": host, "output": stdout_str.strip()},
            )
        else:
            stderr_str = stderr.decode("utf-8", errors="replace")
            return CheckResult(
                status="down",
                response_ms=elapsed_ms(start),
                error=stderr_str.strip() or stdout_str.strip(),
            )
    except asyncio.TimeoutError:
        # asyncio.wait_for() only abandons waiting on the subprocess — the
        # process itself (and a hung DNS lookup inside it) keeps running
        # unless we kill it ourselves, leaking a process per timeout.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return CheckResult(
            status="down",
            response_ms=elapsed_ms(start),
            error="Ping command timed out",
        )
    except Exception as e:
        logger.warning("Ping check error for %s: %s", host, e)
        return CheckResult(
            status="down",
            response_ms=elapsed_ms(start),
            error=str(e),
        )
