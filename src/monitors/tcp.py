# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
tcp.py - The TCP monitor check.
"""

import asyncio
import logging
import time

from src.hostnames import to_dns_name
from src.monitors.base import CheckResult, elapsed_ms

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
async def check_tcp(
    host: str,
    port: int,
    timeout: float = 10.0,
) -> CheckResult:
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(to_dns_name(host), port),
            timeout=timeout,
        )
        response_ms = elapsed_ms(start)
    except asyncio.TimeoutError:
        return CheckResult(
            status="down",
            response_ms=elapsed_ms(start),
            error=f"Connection to {host}:{port} timed out",
        )
    except OSError as e:
        return CheckResult(
            status="down",
            response_ms=elapsed_ms(start),
            error=f"Connection refused: {e}",
        )
    except Exception as e:
        logger.warning("TCP check error for %s:%s: %s", host, port, e)
        return CheckResult(
            status="down",
            response_ms=elapsed_ms(start),
            error=str(e),
        )

    # The connection succeeded, which is the entire question this monitor
    # asks -- so tearing it down must not be able to change the verdict.
    # Closing inside the try above would let a peer that accepts and then
    # immediately resets (wait_closed() raising ConnectionResetError, an
    # OSError) be reported as "Connection refused" / down for a port that
    # was demonstrably open.
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError) as e:
        logger.debug(
            "TCP check for %s:%s connected but errored on close: %s",
            host, port, e,
        )

    return CheckResult(
        status="up",
        response_ms=response_ms,
        details={"host": host, "port": port},
    )
