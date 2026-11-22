# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_hostnames.py - Tests for src/hostnames.py.
"""

import idna
import pytest

from src.hostnames import to_dns_name


@pytest.mark.parametrize("host, expected", [
    ("example.com", "example.com"),
    ("my_database", "my_database"),
    ("Example.COM", "Example.COM"),
    ("192.168.1.42", "192.168.1.42"),
    ("::1", "::1"),
    ("b\u00fccher.example", "xn--bcher-kva.example"),
    ("M\u00fcller.DE", "xn--mller-kva.de"),
    ("stra\u00dfe.de", "xn--strae-oqa.de"),
    ("fa\u00df.de", "xn--fa-hia.de"),
    ("\u03b2\u03cc\u03bb\u03bf\u03c2.com", "xn--nxasmm1c.com"),
    ("\u65e5\u672c\u8a9e.example", "xn--wgv71a119e.example"),
])
def test_to_dns_name(host, expected):
    assert to_dns_name(host) == expected


@pytest.mark.parametrize("host", [
    "\U0001F600.example",
    "ex\u2122ample.com",
    "my_s\u00ebrver.example",
    "-\u00fc.de",
])
def test_to_dns_name_refuses_what_idna_2008_refuses(host):
    with pytest.raises(idna.IDNAError):
        to_dns_name(host)
