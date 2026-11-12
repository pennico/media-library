#!/usr/bin/env bash
# Joplin "To watch" / "To read" lists -> the library, once (D73). Cron runs it hourly (docs/INSTALL.md §6d).
#
#   ops/joplin-lists.sh [--dry-run]      run from the install folder (docker-compose.yml, .env)
#
# 1. the joplin container (deploy/joplin) syncs with the target your Joplin apps use and passes the list notes on;
# 2. the console container matches each line, adds what it is sure of as To watch / To read, and says which lines
#    it took;
# 3. the joplin container syncs again, deletes those lines from the notes as they are now (not a note edited in the
#    last 20 minutes: next time) and syncs at once.
# A dry run stops after 2 with nothing added and nothing deleted. Only the Joplin client ever writes to the sync
# target. When the client fails, what it said is recorded for the console's Sync page (and the health check).
set -uo pipefail
cd "$(dirname "$0")/../.."                      # the install folder (app/ops/ -> ./)
[ -f docker-compose.yml ] && [ -f .env ] || { echo "run from an install folder (app/ops/joplin-lists.sh)"; exit 1; }
files=(-f docker-compose.yml -f app/deploy/joplin/compose.yml)
[ -f compose.joplin.site.yml ] && files+=(-f compose.joplin.site.yml)
dc() { docker compose "${files[@]}" "$@"; }
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
record() {      # what the client said, for the Sync page
  tail -c 3000 "$1" | dc exec -T app python -m medialib joplin-lists --client-error - || true
}
echo "joplin-lists $(date -u +%FT%TZ)"

if ! dc run --rm -T joplin pull > "$work/notes.json" 2> "$work/pull.err"; then
  cat "$work/pull.err"; echo "the Joplin client could not read the notes"
  [ "${1:-}" = --dry-run ] || record "$work/pull.err"
  exit 1
fi
grep -v -e '^Downloading resources' "$work/pull.err" | tail -5
if [ "${1:-}" = --dry-run ]; then
  dc exec -T app python -m medialib joplin-lists --notes - < "$work/notes.json"
  exit $?
fi
rc=0
dc exec -T app python -m medialib joplin-lists --notes - --apply < "$work/notes.json" > "$work/plan.json" || rc=$?
if [ -s "$work/plan.json" ]; then
  if ! dc run --rm -T joplin push < "$work/plan.json" 2> "$work/push.err"; then
    cat "$work/push.err"; echo "the Joplin client could not delete the lines it took (they stay; next run)"
    record "$work/push.err"; rc=1
  else
    grep -e 'removed' "$work/push.err" || true
  fi
fi
exit $rc
