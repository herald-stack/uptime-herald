# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
__init__.py - Maps each monitor type to its check function.
"""

from collections.abc import Awaitable, Callable

from src.monitors.base import CheckResult
from src.monitors.http import check_http
from src.monitors.ping import check_ping
from src.monitors.tcp import check_tcp

# The three check functions take genuinely different parameters (http wants
# url/verify_ssl/contact_email, tcp wants host/port, ping wants host/count),
# so an unannotated dict infers a join of the three signatures that mypy
# then refuses to call. They are always invoked as `check_fn(**kwargs)` with
# kwargs built per type by main.py's _build_kwargs(), so "callable with
# unspecified arguments, returning an awaitable CheckResult" is the accurate
# contract -- not a widening to Any.
CheckFn = Callable[..., Awaitable[CheckResult]]

MONITOR_TYPES: dict[str, CheckFn] = {
    "http": check_http,
    "tcp": check_tcp,
    "ping": check_ping,
}
