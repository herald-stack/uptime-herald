#!/usr/bin/env bash
# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
#
# upgrade-postgres.sh - Upgrades the bundled PostgreSQL to a newer major version.
#
# Upgrade the bundled PostgreSQL container to a newer major version.
#
# Why this exists: PostgreSQL can't start a newer major version against a
# data volume written by an older one, so changing the image tag in
# docker-compose.yaml alone leaves the database container refusing to
# start. This script moves the data across the supported way: dump from
# the running old version, start a fresh newer version, restore into it.
#
# Usage (from the directory containing your docker-compose.yaml):
#
#   ./scripts/upgrade-postgres.sh <target-major> [--yes]
#
#   ./scripts/upgrade-postgres.sh 18
#
# What it does, in order:
#   1. Dumps the app database from the RUNNING container to ./backups/
#      and checks the dump completed.
#   2. Asks you to confirm (skip with --yes). Nothing is deleted before
#      this point.
#   3. Stops the stack, removes the old Postgres data volume, and points
#      the postgres service in docker-compose.yaml at the new major
#      (the original file is saved as docker-compose.yaml.pre-upgrade).
#      Moving to 18+ from an older major also moves the data mount from
#      /var/lib/postgresql/data to /var/lib/postgresql, as the 18+ images
#      require.
#   4. Starts the new Postgres on a fresh volume (the init script
#      recreates the non-superuser app role), restores the dump as that
#      role, and compares per-table row counts with what was there
#      before.
#   5. Starts the whole stack.
#
# The dump is never deleted. If anything fails after step 2, the script
# prints how to put the old version back from that dump.
#
# Requires: docker with the compose plugin, and the stack running.

set -euo pipefail

SERVICE="${PG_SERVICE:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
COMPOSE_FILE_NAME="${COMPOSE_FILE:-docker-compose.yaml}"

die() { echo "ERROR: $*" >&2; exit 1; }

TARGET=""
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) [ -z "$TARGET" ] && TARGET="$arg" || die "unexpected argument: $arg" ;;
    esac
done
[[ "$TARGET" =~ ^[0-9]+$ ]] || die "usage: $0 <target-major, e.g. 18> [--yes]"

command -v docker >/dev/null || die "docker not found"
docker compose version >/dev/null 2>&1 || die "docker compose plugin not found"
[ -f "$COMPOSE_FILE_NAME" ] || die "$COMPOSE_FILE_NAME not found -- run this from your stack directory"

# Exactly one `image: postgres:<tag>` line must exist, since that's what
# we rewrite. Checked up front so we never get halfway and then fail.
IMAGE_LINES=$(grep -cE '^[[:space:]]*image:[[:space:]]*postgres:' "$COMPOSE_FILE_NAME" || true)
[ "$IMAGE_LINES" = "1" ] || die "expected exactly one 'image: postgres:<tag>' line in $COMPOSE_FILE_NAME, found $IMAGE_LINES"

NOT_RUNNING_HELP="The dump has to come from the old version while it is running. If you already changed the image tag (or pulled a newer major) and the database won't start, put the previous tag back in $COMPOSE_FILE_NAME, run 'docker compose up -d', then re-run this script."

CID=$(docker compose ps -q "$SERVICE")
[ -n "$CID" ] || die "the '$SERVICE' service isn't running. $NOT_RUNNING_HELP"
STATE=$(docker inspect -f '{{.State.Status}}' "$CID")
[ "$STATE" = "running" ] || die "the '$SERVICE' container is '$STATE', not running. $NOT_RUNNING_HELP"

# stdin is closed for these so a docker exec can't swallow the terminal
# (or a piped script); the one command that needs input -- the restore --
# redirects its own.
pg() { docker compose exec -T "$SERVICE" "$@" </dev/null; }

CURRENT_NUM=$(pg psql -U postgres -tAc "SHOW server_version_num" | tr -d '[:space:]')
CURRENT=$(( CURRENT_NUM / 10000 ))
[ "$TARGET" -gt "$CURRENT" ] || die "target ($TARGET) must be newer than the running major ($CURRENT)"

DB=$(pg printenv POSTGRES_DB | tr -d '[:space:]')
APP_USER=$(pg printenv DB_USER | tr -d '[:space:]')
[ -n "$DB" ] && [ -n "$APP_USER" ] || die "couldn't read POSTGRES_DB / DB_USER from the $SERVICE container"

# Named volume holding the data, found from the container's mounts so we
# don't have to guess the compose project name.
VOLUME=""
DATA_DEST=""
for dest in /var/lib/postgresql /var/lib/postgresql/data; do
    VOLUME=$(docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$dest\"}}{{.Name}}{{end}}{{end}}" "$CID")
    if [ -n "$VOLUME" ]; then DATA_DEST="$dest"; break; fi
done
[ -n "$VOLUME" ] || die "couldn't find the data volume for $SERVICE (is it a named volume?)"

# PostgreSQL 18+ images want the volume mounted at /var/lib/postgresql;
# earlier ones used /var/lib/postgresql/data. The data volume is rebuilt
# from the dump anyway, so when the target is 18+ and the compose file
# still uses the old path, the mount is rewritten to the new one along with
# the image tag. (Checked now so nothing is touched if it can't be done.)
REWRITE_MOUNT=0
if [ "$TARGET" -ge 18 ] && [ "$DATA_DEST" = "/var/lib/postgresql/data" ]; then
    MOUNT_LINES=$(grep -cE ':/var/lib/postgresql/data' "$COMPOSE_FILE_NAME" || true)
    [ "$MOUNT_LINES" = "1" ] || die "expected exactly one ':/var/lib/postgresql/data' volume mount in $COMPOSE_FILE_NAME to update for PostgreSQL $TARGET, found $MOUNT_LINES -- change it to ':/var/lib/postgresql' by hand, then re-run. Nothing has been changed"
    REWRITE_MOUNT=1
fi

echo "PostgreSQL $CURRENT -> $TARGET"
echo "  database: $DB   app role: $APP_USER   data volume: $VOLUME"

# Per-table exact row counts, as "table=count" lines, sorted. Compared
# before and after to catch a restore that silently lost rows.
row_counts() {
    local tables t
    tables=$(pg psql -U postgres -d "$DB" -tAc \
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
    for t in $tables; do
        echo "$t=$(pg psql -U postgres -d "$DB" -tAc "SELECT count(*) FROM \"$t\"" | tr -d '[:space:]')"
    done
}

BEFORE=$(row_counts)
echo "Row counts before:"; echo "$BEFORE" | sed 's/^/  /'

mkdir -p "$BACKUP_DIR"
DUMP="$BACKUP_DIR/postgres-$CURRENT-to-$TARGET-$(date +%Y%m%d-%H%M%S).sql"
echo "Dumping to $DUMP ..."
pg pg_dump -U postgres -d "$DB" --no-owner --no-privileges > "$DUMP"
[ -s "$DUMP" ] || die "dump is empty -- nothing has been changed"
tail -n 5 "$DUMP" | grep -q "PostgreSQL database dump complete" \
    || die "dump looks incomplete -- nothing has been changed (see $DUMP)"
echo "Dump OK ($(wc -c < "$DUMP") bytes)."

if [ "$ASSUME_YES" -ne 1 ]; then
    echo
    echo "Next: stop the stack, DELETE volume '$VOLUME', start PostgreSQL $TARGET,"
    echo "and restore from $DUMP. The dump is kept either way."
    read -r -p "Type 'upgrade' to continue: " ANSWER
    [ "$ANSWER" = "upgrade" ] || die "cancelled -- nothing has been changed"
fi

rollback_help() {
    cat >&2 <<EOF

The upgrade did not finish. Your data is safe in:
  $DUMP

To go back to PostgreSQL $CURRENT:
  docker compose down
  docker volume rm $VOLUME
  cp $COMPOSE_FILE_NAME.pre-upgrade $COMPOSE_FILE_NAME
  docker compose up -d $SERVICE
  docker compose exec -T $SERVICE psql -U postgres -d $DB -v ON_ERROR_STOP=1 \\
      -c 'SET ROLE "$APP_USER"' -f - < $DUMP
  docker compose up -d
EOF
}
trap rollback_help ERR

docker compose down
docker volume rm "$VOLUME" >/dev/null

cp "$COMPOSE_FILE_NAME" "$COMPOSE_FILE_NAME.pre-upgrade"
sed -i -E "s|^([[:space:]]*image:[[:space:]]*)postgres:.*|\1postgres:$TARGET|" "$COMPOSE_FILE_NAME"
if [ "$REWRITE_MOUNT" -eq 1 ]; then
    sed -i -E 's|(:/var/lib/postgresql)/data|\1|' "$COMPOSE_FILE_NAME"
fi

docker compose up -d --quiet-pull "$SERVICE"
echo -n "Waiting for PostgreSQL $TARGET to become healthy "
NEW_CID=$(docker compose ps -q "$SERVICE")
for _ in $(seq 1 90); do
    STATUS=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$NEW_CID")
    [ "$STATUS" = "healthy" ] || [ "$STATUS" = "running" ] && break
    echo -n "."; sleep 2
done
echo " $STATUS"
[ "$STATUS" = "healthy" ] || [ "$STATUS" = "running" ] || { echo "PostgreSQL $TARGET did not come up" >&2; false; }

# SET ROLE makes every restored object owned by the app role, as if the
# app had created it. The dump has no owner/privilege statements.
docker compose exec -T "$SERVICE" psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 \
    -c "SET ROLE \"$APP_USER\"" -f - < "$DUMP" >/dev/null

AFTER=$(row_counts)
echo "Row counts after:"; echo "$AFTER" | sed 's/^/  /'
if [ "$BEFORE" != "$AFTER" ]; then
    echo "Row counts differ from before the upgrade -- not starting the app." >&2
    false
fi

docker compose up -d --quiet-pull
trap - ERR
echo
echo "Done. PostgreSQL is now $TARGET and the data matches."
echo "Dump kept at $DUMP; previous compose file at $COMPOSE_FILE_NAME.pre-upgrade."
