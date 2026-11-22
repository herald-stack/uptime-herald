#!/bin/bash
# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
#
# extract-i18n.sh - Extracts translatable text and compiles the catalogs.
#
# Extract translatable strings, update .po files, and compile .mo files.
# Run this after adding or changing user-facing strings in templates or Python.
#
# Usage: ./scripts/extract-i18n.sh

set -e
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' src/config.py)

echo "Extracting strings..."
pybabel extract -F babel.cfg -o locales/uptimeherald.pot -k gettext_string \
    --project="Uptime Herald" --version="$VERSION" \
    --copyright-holder="Uptime Herald Authors" \
    --msgid-bugs-address="admin@uptimeherald.org" src/

# Babel has no options for these header lines, so fill them in here.
sed -i \
    -e 's/^# FIRST AUTHOR <EMAIL@ADDRESS>, /# Uptime Herald Authors <admin@uptimeherald.org>, /' \
    -e 's/^"Last-Translator: .*\\n"$/"Last-Translator: Uptime Herald Authors <admin@uptimeherald.org>\\n"/' \
    -e 's/^"Language-Team: .*\\n"$/"Language-Team: Uptime Herald Authors <admin@uptimeherald.org>\\n"/' \
    locales/uptimeherald.pot

echo "Updating .po files..."
pybabel update -i locales/uptimeherald.pot -d locales --domain=uptimeherald

echo "Compiling .mo files..."
pybabel compile -d locales --domain=uptimeherald

echo "Done. Edit locales/<lang>/LC_MESSAGES/uptimeherald.po to add translations,"
echo "then re-run this script to compile."