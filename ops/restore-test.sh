#!/usr/bin/env bash
# Weekly restore test (DESIGN.md §5.6; cron: 0 4 * * 0, after the backups and
# before Watchtower at 04:30). In a throwaway container with no network:
#   1. PITR path: newest base backup + archived WAL replayed to the end,
#      then pg_amcheck --heapallindexed, audit.verify(), row counts;
#   2. dump path: newest pg_dump restored into a fresh database, audit.verify(),
#      row counts.
# Writes backups/restore-test.json (read by the console's health check).
# Runs as the install user (docker access). Nothing touches the live database.
set -euo pipefail
BASE=${MEDIALIB_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}   # ops/ lives in <install dir>/app/ops
IMG=${MEDIALIB_PG_IMAGE:-postgres:18.6-bookworm}
NAME=medialib-restore-test
OUT=$BASE/backups/restore-test.json
started=$(date -u +%FT%TZ)
work=$(mktemp -d /tmp/medialib-restore.XXXXXX)
cleanup() {
  docker rm -f $NAME >/dev/null 2>&1 || true
  docker run --rm -v "$work":/w "$IMG" sh -c 'rm -rf /w/*' >/dev/null 2>&1 || true
  rmdir "$work" 2>/dev/null || true
}
fail() {
  printf '{"ok": false, "started": "%s", "finished": "%s", "error": "%s"}\n' "$started" "$(date -u +%FT%TZ)" "$1" > "$OUT"
  echo "restore test FAILED: $1" >&2
  exit 1
}
trap cleanup EXIT
base=$(docker exec medialib-db sh -c 'ls -1d /backups/base/2* 2>/dev/null | tail -n 1') || true
dump=$(docker exec medialib-db sh -c 'ls -1 /backups/dumps/medialib-*.dump 2>/dev/null | tail -n 1') || true
[ -n "$base" ] || fail "no base backup yet"
[ -n "$dump" ] || fail "no dump yet"

docker run -d --name $NAME --network none -v "$BASE/backups":/backups:ro -v "$BASE/wal-archive":/archive:ro \
  -v "$work":/restore -e PGDATA=/restore/data "$IMG" sleep infinity >/dev/null
docker exec $NAME sh -ec "
  mkdir -p \$PGDATA
  tar -xzf $base/base.tar.gz -C \$PGDATA          # -X fetch: the WAL is inside base.tar
  if [ -f $base/pg_wal.tar.gz ]; then mkdir -p \$PGDATA/pg_wal; tar -xzf $base/pg_wal.tar.gz -C \$PGDATA/pg_wal; fi
  printf '%s\n' \"restore_command = 'gzip -dc /archive/%f.gz > %p'\" \"recovery_target_action = 'promote'\" \
    \"archive_mode = off\" \"listen_addresses = ''\" \"hba_file = '/restore/hba'\" >> \$PGDATA/postgresql.auto.conf
  printf 'local all all trust\n' > /restore/hba
  touch \$PGDATA/recovery.signal
  chown -R postgres:postgres /restore; chmod 700 \$PGDATA
" || fail "could not unpack $base"
t0=$(date +%s)
docker exec -u postgres $NAME pg_ctl -D /restore/data -w -t 600 -l /restore/log start >/dev/null || fail "PITR start failed"
pitr_s=$(( $(date +%s) - t0 ))
q() { docker exec -u postgres $NAME psql -X -At -d "$1" -c "$2"; }
COUNTS="SELECT string_agg(format('\"%s\": %s', t, n), ', ') FROM (SELECT n.nspname || '.' || c.relname AS t, (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text::int AS n FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname IN ('catalog', 'library', 'audit', 'ingest', 'meta') ORDER BY 1) x"
pitr_chain=$(q medialib "SELECT coalesce(first_bad::text, 'ok') || ' ' || checked FROM audit.verify()") || fail "PITR audit.verify failed"
pitr_counts=$(q medialib "$COUNTS") || fail "PITR counts failed"
docker exec -u postgres $NAME pg_amcheck --install-missing --heapallindexed -d medialib >/dev/null || fail "pg_amcheck found corruption"
checksums=$(q medialib "SHOW data_checksums")

t0=$(date +%s)
q postgres "CREATE DATABASE dumpcheck TEMPLATE template0" >/dev/null
docker exec -u postgres $NAME pg_restore --exit-on-error -d dumpcheck "$dump" || fail "pg_restore of $dump failed"
dump_s=$(( $(date +%s) - t0 ))
dump_chain=$(q dumpcheck "SELECT coalesce(first_bad::text, 'ok') || ' ' || checked FROM audit.verify()") || fail "dump audit.verify failed"
dump_counts=$(q dumpcheck "$COUNTS")

ok=true
case "$pitr_chain" in ok\ *) ;; *) ok=false ;; esac
case "$dump_chain" in ok\ *) ;; *) ok=false ;; esac
[ "$checksums" = on ] || ok=false
cat > "$OUT" <<JSON
{"ok": $ok, "started": "$started", "finished": "$(date -u +%FT%TZ)",
 "pitr": {"base": "$base", "seconds": $pitr_s, "audit": "$pitr_chain", "amcheck": "ok", "data_checksums": "$checksums", "rows": {$pitr_counts}},
 "dump": {"file": "$dump", "seconds": $dump_s, "audit": "$dump_chain", "rows": {$dump_counts}}}
JSON
echo "restore test: ok=$ok (PITR ${pitr_s}s, dump ${dump_s}s) -> $OUT"
$ok
