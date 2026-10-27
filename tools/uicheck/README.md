# Browser checks

The console's behaviour and speed, checked in headless Chromium against a copy
of the development database. `tests/` checks the server and the database; these
check what only a browser shows: the shelf, selection, the spreadsheet mode,
the keyboard, the phone sheet, the cached library, and how long clicks take.

| Script | What it checks |
|---|---|
| `checks/shelf.js` | grid, boards, sort, size, Select mode, bulk bar, detail, drag-to-reorder, chips, filter box, the windowed list, Edit drafts |
| `checks/keyboard.js` | Escape and Enter, arrow keys, counts, broken covers, focus, double submits, the lock |
| `checks/regressions.js` | row toggles after a sort, card reuse after a reload, kept actions after unlocking, list scroll and Tab, undo, drafts across filters |
| `checks/spreadsheet.js` | Edit mode as a spreadsheet: paste, fill, copy, undo, save |
| `checks/release-1.0.js` | toasts and Undo, srcset, history and reload, the flight's sharp face, the cached library, Keep unlocked, column fit, the phone sheet |
| `checks/perf.js` | click timings (not pass/fail): `run.py --perf 10 4` clones the library 10x and slows the CPU 4x |

## Running

```bash
python3 tools/devdb.py start                  # the dev database, with a library in it (medialib)
cd tools/uicheck && npm install && npx playwright install chromium && cd -
.venv/bin/python tools/uicheck/run.py         # every check; or name some: run.py shelf keyboard
.venv/bin/python tools/uicheck/run.py --perf 10 4
```

Nothing may be connected to the template database while the checks run (a
copy needs it alone): stop a dev console on it first. Each script gets its own copy of the dev database (`--template`, default
`medialib`) with the test items, its own console on a free port and a
throw-away password; the copy is dropped afterwards. The covers come from
`media/` (thumbnails missing there are made on the way).

In a container without Chromium's system libraries (no root), unpack them
instead: `apt-get download` the packages Playwright lists, `dpkg-deb -x` them
into a folder, and run with `LD_LIBRARY_PATH` pointing at its `usr/lib/x86_64-linux-gnu`
and `FONTCONFIG_FILE` at a fonts.conf that lists a fonts folder.
