#!/usr/bin/env bash
# Logical backup (DESIGN.md §5.5): pg_dump -Fc + roles, 4x a day at :05, ten
# minutes before each run of the host's file backup (e.g. cron 5 3,9,15,21 * * *). Keeps 14 days.
# Runs as the install user (docker access); everything happens inside medialib-db as postgres.
set -euo pipefail
ts=$(date -u +%Y%m%d-%H%M%S)
docker exec -u postgres medialib-db sh -ec "
  umask 027
  pg_dump -Fc -d medialib -f /backups/dumps/medialib-$ts.dump.tmp
  mv /backups/dumps/medialib-$ts.dump.tmp /backups/dumps/medialib-$ts.dump
  pg_dumpall --globals-only --no-role-passwords -f /backups/dumps/globals-$ts.sql
  find /backups/dumps -type f \( -name 'medialib-*.dump' -o -name 'globals-*.sql' \) -mtime +14 -delete
"
echo "dump ok: medialib-$ts.dump"
