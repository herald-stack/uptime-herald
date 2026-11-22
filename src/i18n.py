# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
i18n.py - Translation and locale-aware formatting.

Uses Python gettext (stdlib) for string translation and Babel for
locale-aware date/time/number formatting. Translation files live in
locales/<lang>/LC_MESSAGES/uptimeherald.{po,mo}.

The extraction workflow uses pybabel:
    pybabel extract -F babel.cfg -o locales/uptimeherald.pot -k gettext_string src/
    pybabel update -i locales/uptimeherald.pot -d locales
    pybabel compile -d locales
"""

import gettext
import logging
from datetime import datetime
from pathlib import Path

from babel.core import Locale, UnknownLocaleError
from babel.dates import format_datetime as babel_format_datetime
from babel.numbers import format_decimal as babel_format_number

logger = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).parent.parent / "locales"
DOMAIN = "uptimeherald"

# Cache translation objects by language code
_translations: dict[str, gettext.NullTranslations] = {}

# Babel uses locale names like "en_US", but config uses "en".
_LOCALE_MAP = {"en": "en_US"}
_FALLBACK_LOCALE = "en_US"

# Resolved (and validated) Babel locale name per config language, cached
# the same way _translations is -- format_datetime() runs once per row of
# a check-log page, so this shouldn't re-validate on every call.
_babel_locales: dict[str, str] = {}


# --------------------------------------------------------------------------------------
def _babel_locale(lang: str) -> str:
    """Map a config language code to a Babel locale name.

    Unlike get_translations() -- which quietly degrades to
    NullTranslations for a language it can't load -- Babel raises on a
    locale it doesn't recognize. Left unguarded, a typo'd `language:` in
    config.yaml renders every page fine (gettext falls back) while
    500ing the check-log API, which is the only caller that formats
    dates through Babel. Resolve the name once here so both paths share
    the same graceful fallback.
    """
    if lang in _babel_locales:
        return _babel_locales[lang]
    locale = _LOCALE_MAP.get(lang, lang)
    try:
        Locale.parse(locale)
    except (UnknownLocaleError, ValueError, TypeError) as e:
        logger.warning(
            "Locale '%s' is not recognized by Babel (%s), falling back to %s",
            locale, e, _FALLBACK_LOCALE,
        )
        locale = _FALLBACK_LOCALE
    _babel_locales[lang] = locale
    return locale


# --------------------------------------------------------------------------------------
def get_translations(lang: str) -> gettext.NullTranslations:
    """Return a gettext translations object for the given language.

    Falls back to NullTranslations (English, no-op) if the language
    is not found or no .mo file exists.

    Args:
        lang: ISO 639-1 language code (e.g. "en", "fr", "de")

    Returns:
        A gettext.NullTranslations (or GNUTranslations if .mo exists).
    """
    if lang in _translations:
        return _translations[lang]

    mo_path = LOCALES_DIR / lang / "LC_MESSAGES" / f"{DOMAIN}.mo"
    if mo_path.exists():
        try:
            t = gettext.translation(
                DOMAIN, localedir=str(LOCALES_DIR), languages=[lang]
            )
            _translations[lang] = t
            return t
        except OSError as e:
            logger.warning("Failed to load translations for '%s': %s", lang, e)

    # Annotated as the base class: gettext.translation() returns a
    # GNUTranslations while this fallback is a plain NullTranslations, and
    # callers only ever use the common gettext()/ngettext() surface.
    fallback: gettext.NullTranslations = gettext.NullTranslations()
    _translations[lang] = fallback
    return fallback


# --------------------------------------------------------------------------------------
def gettext_string(lang: str, msgid: str, **vars) -> str:
    """Translate a string in the given language.

    Falls back to the original English string if no translation exists.

    Args:
        lang: ISO 639-1 language code
        msgid: The string to translate (English source text)
        **vars: Optional variable substitutions (e.g. name="foo")

    Returns:
        Translated string, or English original if no translation.
    """
    t = get_translations(lang)
    return t.gettext(msgid) if not vars else t.gettext(msgid).format(**vars)


# --------------------------------------------------------------------------------------
def ngettext_string(
    lang: str, singular: str, plural: str, n: int, **vars
) -> str:
    """Translate a pluralized string in the given language.

    Args:
        lang: ISO 639-1 language code
        singular: English singular form
        plural: English plural form
        n: The count determining singular vs plural
        **vars: Optional variable substitutions

    Returns:
        Translated string with correct plural form.
    """
    t = get_translations(lang)
    result = t.ngettext(singular, plural, n)
    return result.format(**vars) if vars else result


# --------------------------------------------------------------------------------------
def format_datetime(
    lang: str,
    dt: datetime,
    format: str = "medium",
    tzinfo=None,
) -> str:
    """Format a datetime using Babel locale-aware formatting.

    Args:
        lang: ISO 639-1 language code (used as Babel locale)
        dt: The datetime to format
        format: Babel format name ("short", "medium", "long", "full")
                or a custom pattern string
        tzinfo: Optional timezone to convert to before formatting

    Returns:
        Locale-formatted date/time string.
    """
    if tzinfo is not None:
        dt = dt.astimezone(tzinfo)
    return babel_format_datetime(dt, format=format, locale=_babel_locale(lang))


# --------------------------------------------------------------------------------------
def format_number(lang: str, num: float | int) -> str:
    """Format a number using Babel locale-aware formatting.

    Args:
        lang: ISO 639-1 language code
        num: The number to format

    Returns:
        Locale-formatted number string.
    """
    return babel_format_number(num, locale=_babel_locale(lang))


# --------------------------------------------------------------------------------------
def get_js_translations(lang: str) -> dict:
    """Return a dict of translated strings for JavaScript consumption.

    Inline <script> blocks can't use Jinja2 {% trans %} tags, so we
    inject these as a JSON object that JS references as T.key.

    Args:
        lang: ISO 639-1 language code

    Returns:
        Dict of translation key -> translated string.
    """
    t = get_translations(lang)
    return {
        "loading": t.gettext("Loading…"),
        "no_monitors": t.gettext("No monitors configured."),
        "no_checks": t.gettext("No checks recorded"),
        "failed_load": t.gettext("Failed to load checks"),
        "failed_load_checks": t.gettext("Failed to load checks"),
        "failed_monitor": t.gettext("Failed to load:"),
        "prev": t.gettext("‹ PREV"),
        "next": t.gettext("NEXT ›"),
        "page_of": t.gettext("PAGE %(page)d OF %(total)d (%(count)d CHECKS)"),
        "inactive": t.gettext("INACTIVE"),
        "maint": t.gettext("MAINT"),
        "maintenance": t.gettext("Maintenance:"),
        "verifying": t.gettext("Verifying Status"),
        "verification_confirmed": t.gettext("Status Change Verified"),
        "no_latency": t.gettext("No latency data"),
        "go_to_page": t.gettext("Go to page"),
        "latency": t.gettext("Latency"),
        "details": t.gettext("Details"),
    }
