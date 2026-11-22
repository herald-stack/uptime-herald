# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
hostnames.py - Converts host names to the form DNS is asked for.
"""

import idna


# --------------------------------------------------------------------------------------
def to_dns_name(host: str) -> str:
    """Return the name DNS is asked for: a host with non-ASCII letters in its
    "xn--" form, anything else unchanged.

    This is the same conversion httpx makes for an http monitor (IDNA 2008, the
    standard registries and browsers follow) and the one `ping` makes, so all
    three monitor types look up the same name. Python's own connection code
    would convert with the older IDNA 2003 rules instead, which turn "ß" into
    "ss" and so look up a different domain than the other two.

    Raises idna.IDNAError (a UnicodeError) when the name cannot be converted,
    for example one with a symbol, an emoji or an underscore among the
    non-ASCII letters.
    """
    if host.isascii():
        return host
    return idna.encode(host.lower()).decode("ascii")
