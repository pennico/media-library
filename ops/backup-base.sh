#!/usr/bin/env bash
# Physical base backup for point-in-time recovery (DESIGN.md §5.5), nightly
# at 02:50 (cron: 50 2 * * *). With the WAL archive it restores to any second
# within the kept window. Keeps 7 base backups and prunes WAL older than the
# oldest one. Runs as the install user (docker access); the work happens inside medialib-db as postgres.
set -euo pipefail
ts=$(date -u +%Y%m%d-%H%M%S)
docker exec -u postgres medialib-db sh -ec "
  umask 027
  d=/backups/base/$ts
  pg_basebackup -D \$d.tmp -Ft -z -X fetch --checkpoint=fast --manifest-checksums=SHA256
  pg_verifybackup -n \$d.tmp
  mv \$d.tmp \$d
  ls -1d /backups/base/2* | head -n -7 | xargs -r rm -rf
  oldest=\$(ls -1d /backups/base/2* | head -n 1)
  lsn=\$(sed -n 's/.*\"Start-LSN\": *\"\\([0-9A-F/]*\\)\".*/\\1/p' \$oldest/backup_manifest | head -n 1)
  wal=\$(psql -Atc \"SELECT pg_walfile_name('\$lsn')\")
  pg_archivecleanup -x .gz /archive \$wal
"
echo "base backup ok: $ts"
