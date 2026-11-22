# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
test_i18n.py - Tests for translation and locale formatting.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo

import src.i18n as i18n_module
from src.i18n import (
    format_datetime,
    format_number,
    get_js_translations,
    get_translations,
    gettext_string,
    ngettext_string,
)


@pytest.fixture
def fake_locale(tmp_path, monkeypatch):
    """Build a real compiled .mo catalog for a non-English locale and
    point the module at it, so tests can assert that translated output
    actually differs from the English source — not just that nothing
    crashed. Also clears the module-level translation cache so the fake
    locale isn't shadowed by (or doesn't leak into) other tests.
    """
    lang = "fr"
    msgid = "Loading…"
    msgstr = "Chargement…"

    catalog = Catalog(locale=lang)
    catalog.add(msgid, msgstr)
    mo_dir = tmp_path / lang / "LC_MESSAGES"
    mo_dir.mkdir(parents=True)
    with open(mo_dir / f"{i18n_module.DOMAIN}.mo", "wb") as f:
        write_mo(f, catalog)

    monkeypatch.setattr(i18n_module, "LOCALES_DIR", tmp_path)
    monkeypatch.setattr(i18n_module, "_translations", {})

    return lang, msgid, msgstr


# --- Translation loading --------------------------------------------------------------

def test_get_translations_unknown_lang_returns_null():
    """Unknown language falls back to NullTranslations (English)."""
    t = get_translations("xx")
    # NullTranslations returns the original string unchanged
    assert t.gettext("Hello") == "Hello"


def test_get_translations_english():
    """English translations object loads correctly."""
    t = get_translations("en")
    assert t.gettext("Loading…") == "Loading…"


def test_get_translations_loads_real_mo_translation(fake_locale):
    """A locale backed by a real .mo file returns the translated string,
    not the English source. This is the actual regression check for bugs
    like a wrong LOCALES_DIR silently falling back to NullTranslations:
    a test that only ever exercises "en" (source == target) can't catch
    that, because the output looks identical either way.
    """
    lang, msgid, msgstr = fake_locale
    t = get_translations(lang)
    result = t.gettext(msgid)
    assert result == msgstr
    assert result != msgid


def test_get_translations_caches():
    """Repeated calls return the same cached object."""
    t1 = get_translations("en")
    t2 = get_translations("en")
    assert t1 is t2


# --- gettext_string -------------------------------------------------------------------

def test_gettext_string_fallback():
    """Unknown language returns the English original."""
    assert gettext_string("xx", "Hello") == "Hello"


def test_gettext_string_translates(fake_locale):
    """gettext_string returns the translated string for a known locale
    with a real .mo catalog, and it differs from the English source.
    """
    lang, msgid, msgstr = fake_locale
    result = gettext_string(lang, msgid)
    assert result == msgstr
    assert result != msgid


def test_gettext_string_with_vars():
    """Variable substitution works."""
    result = gettext_string("en", "Monitor {name} is UP", name="drd01")
    assert "drd01" in result


# --- ngettext_string ------------------------------------------------------------------

def test_ngettext_singular():
    """Singular form used when n=1."""
    result = ngettext_string("en", "{count} check", "{count} checks", 1, count=1)
    assert "1" in result


def test_ngettext_plural():
    """Plural form used when n>1."""
    result = ngettext_string("en", "{count} check", "{count} checks", 5, count=5)
    assert "5" in result


# --- format_datetime ------------------------------------------------------------------

def test_format_datetime_english():
    """English datetime formatting works."""
    dt = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
    result = format_datetime("en", dt, format="medium")
    # Should contain the year and some readable date
    assert "2026" in result


def test_format_datetime_with_tzinfo():
    """Datetime formatting with timezone conversion."""
    dt = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
    tz = ZoneInfo("America/Chicago")
    result = format_datetime("en", dt, format="medium", tzinfo=tz)
    # Should be 09:30 Chicago time (UTC-5 in July)
    assert "09" in result or "9:" in result or "9 " in result


# --- format_number --------------------------------------------------------------------

def test_format_number_english():
    """English number formatting works."""
    assert format_number("en", 1234.5) == "1,234.5"


# --- get_js_translations --------------------------------------------------------------

def test_js_translations_returns_dict():
    """JS translations returns a dict with expected keys."""
    t = get_js_translations("en")
    assert isinstance(t, dict)
    assert "loading" in t
    assert "prev" in t
    assert "next" in t
    assert "page_of" in t


def test_js_translations_fallback():
    """JS translations for unknown lang returns English."""
    t = get_js_translations("xx")
    assert t["loading"] == "Loading…"
    assert t["prev"] == "‹ PREV"


# --- Corrupted translation file -------------------------------------------------------


def test_get_translations_corrupted_mo_file_falls_back_gracefully(
    tmp_path, monkeypatch,
):
    """A .mo file that exists on disk but is malformed (bad magic number,
    truncated during a bad deploy, etc.) makes gettext.translation() raise
    OSError -- get_translations() must catch that and fall back to
    NullTranslations rather than crashing every page render for that
    language."""
    lang = "xx"
    mo_dir = tmp_path / lang / "LC_MESSAGES"
    mo_dir.mkdir(parents=True)
    (mo_dir / f"{i18n_module.DOMAIN}.mo").write_bytes(b"not a real mo file")

    monkeypatch.setattr(i18n_module, "LOCALES_DIR", tmp_path)
    monkeypatch.setattr(i18n_module, "_translations", {})

    t = get_translations(lang)  # must not raise

    assert t.gettext("Hello") == "Hello"  # NullTranslations passthrough
    # The fallback is cached too, same as a real translation would be --
    # a second call shouldn't try (and fail) to re-parse the bad file.
    assert get_translations(lang) is t
