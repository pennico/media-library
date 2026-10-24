#!/bin/sh
# WAL archiver for PostgreSQL (archive_command).
#
#   archive_command = '/usr/local/bin/archive-wal.sh %p %f /archive'
#
# Writes <archive>/<segment>.gz atomically (temp file, fsync, rename, fsync
# dir). A segment that is already archived is accepted only if its content is
# identical; anything else is an error, so PostgreSQL keeps the segment and
# retries instead of losing it.
set -eu
src="$1"
name="$2"
dir="${3:-/archive}"
dst="$dir/$name.gz"

if [ -f "$dst" ]; then
    if gzip -dc "$dst" | cmp -s - "$src"; then
        exit 0
    fi
    echo "archive-wal: $dst exists with different content" >&2
    exit 1
fi
tmp="$dst.tmp.$$"
trap 'rm -f "$tmp"' EXIT
gzip -c "$src" > "$tmp"
sync "$tmp"
mv "$tmp" "$dst"
sync "$dir"
trap - EXIT
