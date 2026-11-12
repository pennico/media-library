#!/bin/sh
# media-library's headless Joplin client (D73): the entry point of the joplin container. ops/joplin-lists.sh runs
#   pull     settings, guard, sync, guard; the To watch / To read notes as JSON on stdout
#   push     stdin: the plan from `medialib joplin-lists --apply`; sync first (the owner may have edited the note),
#            delete the taken lines from the notes as they are now, read them back, sync at once
#   status   the CLI's version and the target's shared settings (no sync)
# Everything the CLI says goes to stderr; stdout carries only the JSON. A STOPPED file in /data (the guard writes it
# when a sync changed what the owner's devices share, or a note could not be put back) stops every run until a
# person has looked and deleted it.
set -eu
H=${JOPLIN_LISTS_HOME:-/opt/medialib-joplin}
DATA=${JOPLIN_DATA:-/data}
PROFILE=${JOPLIN_PROFILE:-$DATA/profile}
J() { joplin --profile "$PROFILE" "$@"; }
if [ -e "$DATA/STOPPED" ]; then echo "joplin-lists: stopped: $(cat "$DATA/STOPPED")" >&2; exit 9; fi
mkdir -p "$PROFILE"
# the CLI's own log grows with every sync: keep the last megabyte
if [ -f "$PROFILE/log.txt" ] && [ "$(wc -c < "$PROFILE/log.txt")" -gt 5000000 ]; then
  tail -c 1000000 "$PROFILE/log.txt" > "$PROFILE/log.txt.new" && mv "$PROFILE/log.txt.new" "$PROFILE/log.txt"
fi
# one sync between the two guards; the CLI exits 0 even when items failed ("Last error: …"), so its words decide
sync_once() {
  node --no-warnings "$H/lists.mjs" guard before
  out=$(mktemp)
  if ! J sync > "$out" 2>&1; then cat "$out" >&2; rm -f "$out"; echo "joplin-lists: the sync failed: nothing more this time" >&2; exit 6; fi
  cat "$out" >&2
  if grep -q -e 'Last error:' -e 'must be upgraded' "$out" || ! grep -q 'Completed:' "$out"; then
    rm -f "$out"; echo "joplin-lists: the sync did not complete (above): nothing more this time" >&2; exit 6
  fi
  rm -f "$out"
  node --no-warnings "$H/lists.mjs" guard after
}
case "${1:-}" in
  pull)
    node --no-warnings "$H/lists.mjs" setup
    sync_once
    node --no-warnings "$H/lists.mjs" notes ;;
  push)
    plan=$(mktemp); trap 'rm -f "$plan"' EXIT
    cat > "$plan"
    node --no-warnings -e 'const p = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8")); process.exit((p.notes || []).length ? 0 : 1)' "$plan" \
      || { echo "joplin-lists: nothing to delete from the notes" >&2; exit 0; }
    node --no-warnings "$H/lists.mjs" setup
    sync_once
    node --no-warnings "$H/lists.mjs" strip < "$plan"
    sync_once ;;
  status)
    node --no-warnings "$H/lists.mjs" setup
    J version >&2
    node --no-warnings "$H/lists.mjs" guard show ;;
  *) echo "usage: client.sh pull | push | status" >&2; exit 2 ;;
esac
