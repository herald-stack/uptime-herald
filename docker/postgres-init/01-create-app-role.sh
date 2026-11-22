#!/bin/sh
# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
#
# 01-create-app-role.sh - Creates the application's database role on first start.
#
# Runs once, on first initialization of an empty data volume (the
# postgres image's /docker-entrypoint-initdb.d mechanism -- same
# first-init-only behavior as MARIADB_USER on the MariaDB side).
#
# The postgres image always makes POSTGRES_USER a superuser, and has no
# equivalent of MARIADB_USER. To match the MariaDB stack -- where the app
# connects as a limited user that owns one database, and root is a
# separate account -- POSTGRES_USER is the separate superuser ("postgres")
# and this script creates the app's own role and hands it the database
# POSTGRES_DB already created.
#
# DB_USER and DB_PASSWORD come from the compose file's environment for
# the postgres service. They go in as psql variables (:"x" quotes an
# identifier, :'x' quotes a literal) rather than being pasted into the
# SQL, so unusual characters in either can't break out of the statement.
set -eu

: "${DB_USER:?DB_USER must be set}"
: "${DB_PASSWORD:?DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_user="$DB_USER" -v app_password="$DB_PASSWORD" \
    -v app_db="$POSTGRES_DB" <<'EOSQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password';
ALTER DATABASE :"app_db" OWNER TO :"app_user";
EOSQL
