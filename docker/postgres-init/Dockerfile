# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
#
# Dockerfile - Builds the image that supplies the PostgreSQL init scripts.
#
# The uptimeherald-postgres-init image: a few files and nothing else. The
# bundled PostgreSQL stack runs it as a short-lived helper (the
# postgres-init service in deploy/postgres/docker-compose.yaml) that copies
# the files out to where the postgres service and the user need them.
# A separate image from the app so the app image carries nothing that only
# the PostgreSQL stack uses.
#
# Build from the repository root:
#   docker build -f docker/postgres-init/Dockerfile .
#
# Pinned to a minor release plus the registry's multi-arch index digest,
# the same way the app image is. Digest read from Docker Hub for
# alpine:3.24 (covers all published platforms) -- re-verify and update if
# you bump the tag.
FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6

# The PostgreSQL image has no equivalent of MARIADB_USER, so this script
# creates the app's own non-superuser role on the database's first init.
COPY docker/postgres-init/01-create-app-role.sh /app/postgres-init/01-create-app-role.sh
# The major-version upgrade script runs on the user's host, not in a
# container. It is copied into the user's ./scripts folder on every
# `docker compose up`, which keeps it matched to the running version.
COPY scripts/upgrade-postgres.sh /app/scripts/upgrade-postgres.sh
