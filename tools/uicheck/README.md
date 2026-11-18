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
| `checks/reopen.js` | closing and reopening: the kind in the pane, the shelf and the items on the page agree |
| `checks/titles-random.js` | 1.1.0: the Random order (new per opening, steady while browsing, Shuffle), titles in other languages (shown, filtered, searched, listed and edited) |
| `checks/collection-edit.js` | D70: a collection's editor (add by search, Enter, up, down, to the top, remove, Undo of each), Remove from collection on a selection, rename, delete, 375 px |
| `checks/panel-nav.js` | D71: ‹ › and "3 of 12", ← →, the address replaced, the ends, the shelf's arrow keys taking the panel along, an item leaving a filtered list, the list layout, the phone sheet |
| `checks/full-page.js` | D72: expand and reduce, `#page=` and its history entry, Back / Forward / Esc, the shelf's scroll kept, ‹ › on the page, a link into the page, ✕ from it, two columns, the phone page |
| `checks/panel-bottom.js` | the panel's last line (Move to trash) can be scrolled into view: wide screen at the top and scrolled, a narrower window, the phone sheet |
| `checks/joplin-lists.js` | D73: the Sync page's Joplin lists (lines left with why and candidates, added items, a failed client run, a conflict copy, 375 px) |
| `checks/series.js` | D76-D81: Create series… on a selection (the name asked, the order shown, a move named), Undo; one card; the series' panel (order ⤒ ↑ ↓, remove, rename, delete, each with Undo); a book's "Book 2 of 3" and strip; Add to / Remove from series; ‹ › counting the card once; the page view; the list layout; a section; filter box and search; the trash keeping a place; Movies' film collection; an older kept library; 375 px; locked |
| `checks/series-suggest.js` | D83-D87: Fill missing suggests a series (never applies it); a book's panel: Create series at once and with a list to tick (kept through a redraw), Add to your series by its number, Not this series, each with Undo; the bar's Suggest series and its review list (yours first, by number, untick, Undo); a failing lookup stops the run; Movies; 375 px; locked. Its books are made up; Open Library answers from `recorded/` |
| `checks/perf.js` | click timings (not pass/fail): `run.py --perf 10 4` clones the library 10x and slows the CPU 4x; `SERIES=3` first makes book series of every three books by one author (each clone its own) and times opening one |

## Running

```bash
python3 tools/devdb.py start                  # the dev database, with a library in it (medialib)
cd tools/uicheck && npm install && npx playwright install chromium && cd -
.venv/bin/python tools/uicheck/run.py         # every check; or name some: run.py shelf keyboard
.venv/bin/python tools/uicheck/run.py --perf 10 4
```

Lookups never reach the network: `run.py` points the console at `recorded/`
(`MEDIALIB_HTTP_RECORDED`, lists of `{url, body}`), and a URL not recorded
is an error in the page. Nothing may be connected to the template database while the checks run (a
copy needs it alone): stop a dev console on it first. Each script gets its own copy of the dev database (`--template`, default
`medialib`) with the test items, its own console on a free port and a
throw-away password; the copy is dropped afterwards. The covers come from
`media/` (thumbnails missing there are made on the way).

In a container without Chromium's system libraries (no root), unpack them
instead: `apt-get download` the packages Playwright lists, `dpkg-deb -x` them
into a folder, and run with `LD_LIBRARY_PATH` pointing at its `usr/lib/x86_64-linux-gnu`
and `FONTCONFIG_FILE` at a fonts.conf that lists a fonts folder.
