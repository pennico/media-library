#!/usr/bin/env bash
# Joplin review notes -> the library, once. Cron runs it hourly (docs/INSTALL.md).
#
#   ops/joplin-sync.sh [--dry-run]      run from the install folder (docker-compose.yml, .env)
#
# Reads the Joplin sync folder read-only, as a tar stream, from where .env says:
#   JOPLIN_SOURCE_CONTAINER + JOPLIN_SOURCE_PATH (+ JOPLIN_SOURCE_USER): a folder inside a
#     container (e.g. Nextcloud's data folder), read with docker exec ... tar;
#   or JOPLIN_SOURCE_DIR: a folder on this machine.
# and pipes it into the console container, which matches and writes the notes
# (python -m medialib joplin). Nothing is written to the sync folder.
set -euo pipefail
cd "$(dirname "$0")/../.."                      # the install folder (app/ops/ -> ./)
[ -f docker-compose.yml ] && [ -f .env ] || { echo "run from an install folder (app/ops/joplin-sync.sh)"; exit 1; }
val() {   # KEY=value or KEY="value with spaces", an inline " # comment" dropped
  local v; v=$(grep -E "^$1=" .env | tail -1 | cut -d= -f2-) || true
  v=${v%% #*}; v="${v%"${v##*[![:space:]]}"}"; v=${v#\"}; v=${v%\"}; printf '%s' "$v"
}
container=$(val JOPLIN_SOURCE_CONTAINER); path=$(val JOPLIN_SOURCE_PATH); user=$(val JOPLIN_SOURCE_USER)
dir=$(val JOPLIN_SOURCE_DIR); notebook=$(val JOPLIN_NOTEBOOK)
apply=--apply; [ "${1:-}" = --dry-run ] && apply=
excludes=(--exclude=./.resource --exclude=./locks --exclude=./temp --exclude=./.sync --exclude=./.lock)

if [ -n "$container" ] && [ -n "$path" ]; then
  source_cmd=(docker exec -u "${user:-www-data}" "$container" tar -C "$path" "${excludes[@]}" -cf - .)
elif [ -n "$dir" ]; then
  source_cmd=(tar -C "$dir" "${excludes[@]}" -cf - .)
else
  echo "set JOPLIN_SOURCE_CONTAINER + JOPLIN_SOURCE_PATH, or JOPLIN_SOURCE_DIR, in .env"; exit 1
fi

echo "joplin-sync $(date -u +%FT%TZ)"
"${source_cmd[@]}" | docker compose exec -T app python -m medialib joplin --tar - \
  --notebook "${notebook:-Reviews}" $apply
