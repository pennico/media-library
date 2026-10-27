# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-27

The console is complete for its one user: fast on a big library, kind to
mistakes, at home on the phone. From here on, versions follow semantic
versioning strictly (a breaking change to the data or the API is 2.0).

- **Covers in the size the screen needs**: a shelf cover offers 160, 320, 480
  and 640 px and the browser takes what the column width and the screen's
  density need (L covers on a Retina screen are sharp, S covers on a plain
  screen are light); small places (list rows, search, Trash, seasons) take
  160 px. The server makes the four sizes of the chosen covers in the
  background (about 110 KB more per cover), never enlarges a small original,
  and no longer makes thumbnails one at a time behind a single lock.
- **The flying cover** lands as a book (2:3) on a phone too, centred above
  the sheet, and turns sharp (640 px) as soon as that picture is decoded.
- **Opens at once**: the console keeps the last library in the browser and
  draws it before the server answers (read-only until it does: "Connecting…");
  without the server it stays, marked "Offline · the last copy", with Try
  again. Coming back to the tab after a minute away checks for changes and
  redraws only if the library changed and nothing is being edited. The server
  builds `/api/library` again only when something changed (the newest audit
  entry, or a write through it), at most every 30 s otherwise.
- **The book panel is a place**: an open book is in the address
  (`#item=…`): Back (or the iPhone's swipe) closes the panel, Forward opens it
  again on its own shelf, a reload or a link opens the same book (closing it
  then stays in the console). Opened from the keyboard, focus goes to its title.
- **Phone**: the panel is a sheet over a dimmed backdrop (a tap on it closes
  the sheet, after asking if a form is open), its ✕ stays at the top while scrolling, scrolling it doesn't
  scroll the page behind, the selection bar waits behind it; the "Filter this
  list" box takes the room it needs.
- **Undo**: Move to trash, Archive, Unarchive and Move to section offer Undo
  in their message (each item goes back where it was; only what that action
  changed); the bar's "Move to trash" is one click now, and focus goes to
  its Undo. Messages stack above the selection bar (three at most), can be
  dismissed, stay while pointed at or focused, are read by screen readers with
  their action, and errors stay longer.
- **The lock**: three minutes before it, a message and a "Keep unlocked"
  button (Touch ID / Face ID, or the password); renewing keeps open forms
  and typed rows as they are, and the password field keeps what is typed.
- **Fit a column** of a long list measures every row, not only those drawn.
- **Server**: opening a book reads its history through the audit index
  (it read every edition and reading change before); bulk `restore`.
- **Browser checks** in the repository: `tools/uicheck/` runs the console in
  headless Chromium against a copy of the dev database (shelf, keyboard,
  spreadsheet, the review regressions, this release, timings).

## [0.10.0] - 2026-10-27

- **A big library stays quick**: measured at 1,680 items on a slow CPU (4x
  throttle), every click took 1.5 to 2 s because each one rebuilt the whole
  shelf and laid it out again. Now:
  - selecting a cover costs about 0.1 s (was 1.8 s); Select on/off, All,
    None, a row toggle or a list check change only what changed;
  - cover size, a status chip, the filter box, opening and closing a book
    0.25 to 0.35 s (were 0.9 to 1.6 s): the shelf keeps its covers, rebuilds a
    card only when its item changed, and cards out of view cost nothing
    (`content-visibility`), while the hover lift, the shadows and the
    selection ring look as before;
  - grid to list 0.25 s (was 2 to 3 s): a long list draws only the rows on
    and near the screen, in a fixed table layout, with small thumbnails;
  - the shelf boards are placed from the grid's row sizes in one read, and
    only rewritten when they move;
  - pointing at covers no longer tests thousands of nested positioned boxes.
- **Spreadsheet mode**: Escape leaves a cell without throwing the table away;
  Enter goes down a row and Ctrl/Cmd+Enter saves; Cancel asks before
  discarding typed changes (and so do Select and Grid); what was typed
  survives a sort, a filter that hides the row for a while, a redraw, a save
  that failed for some rows (those rows are marked) and the console locking;
  undo works after a redraw;
  "Saved" is only said when it saved; language lists are filled when opened.
- **Locks and double clicks**: an action stopped by the lock runs right after
  unlocking, if the page is still where it was (not after moving to another
  book, cancelling, or 10 minutes); saving a reading, adding a book, a bulk
  action or the table save can't run twice at once (a double click adds one
  reading); a busy cursor shows while something is being saved.
- **Keyboard and focus**: Escape closes the top layer first and never clears
  a selection or a draft from a text field; arrow keys move between covers;
  closing the panel puts focus back on its book; the filter box keeps its
  focus and caret while the shelf follows the typing; Tab reaches every row
  of a long list, which tells screen readers how many rows it has.
- **Smaller things**: a live "12 of 154" count when the shelf is narrowed;
  "Clear the filters" also clears the typed filter and the status chip; the
  typed filter belongs to one shelf; covers that fail to load show their
  title; going from book to book with the panel open no longer replays the
  flight; Visuals opens without waiting for an unused request; "Can't reach
  the library server. Is Tailscale on?" instead of "Load failed", with a
  Try again button; an "Opening the library…" placeholder; toasts are
  announced to screen readers and readable in dark mode; on a phone, form
  fields are 16 px so Safari doesn't zoom into them; the Sort menu saves once.
- **Server**: 160 px thumbnails are made in the background too (list rows);
  thumbnails are decoded at a reduced scale and written as baseline JPEG;
  JSON without spaces.

## [0.9.0] - 2026-10-27

- **Spreadsheet mode in the list's Edit**: select cells by clicking, shift-clicking
  or dragging; Ctrl/Cmd+C copies them as tab-separated text and Ctrl/Cmd+V pastes
  what Excel or Sheets copied (one value fills the whole selection; a block lands
  from the selected cell down and right); Ctrl/Cmd+D fills down, Delete clears,
  Ctrl/Cmd+Z undoes the last grid action, and a Paste button reads the clipboard
  for browsers where the shortcut is not at hand. Pasted text is checked per
  column (status and language by name or code, rating 0-10, dates as
  yyyy / yyyy-mm / yyyy-mm-dd, pages, version, categories by name); a value that
  doesn't fit is marked red and left out. Changed cells are tinted until Save.
- **Faster saves**: a row of the batch is now one transaction (status, date and
  details together, all or nothing): 154 rows with status, rating and date
  changes save in about 1.9 s instead of 3.2 s.

## [0.8.5] - 2026-10-27

- **Saving many rows is fast**: the list's Edit mode sends all its changed rows
  in one request (`POST /api/items/batch`) instead of one to three per row, and
  gets the saved rows back; 150 rows went from about 10 s to half a second.
  Each row is still saved on its own with the same provenance, audit and
  database rules, and one that fails (a rating of 99) doesn't stop the others.
- **No full reload after an edit**: saving in the detail panel (status, rating,
  version, review, readings, cover, EPUB) refreshes just that item's row
  (`GET /api/rows`) instead of loading the whole library again; collections
  still reload it, as they change the shelves too.
- A browser closing an idle connection no longer prints a traceback in the log.

## [0.8.4] - 2026-10-27

- **Faster loading**: the page and the JSON are gzip-compressed (the page
  267 KB to 74 KB, the library 144 KB to 25 KB); `index.html` and the library
  are revalidated with an ETag, so an unchanged one costs a 304 and no body;
  connections are kept alive (HTTP/1.1, `TCP_NODELAY`), so a shelf's covers no
  longer open a connection each; the shelf thumbnails are made in the
  background at start; covers decode off the main thread.

## [0.8.3] - 2026-10-26

- **Filter**: the date and range sections (Read / Watched, Published /
  Released, Rating, Pages / Runtime) have a "Not set" choice: only the items
  without that value, e.g. watched with no date. Moving the slider again
  takes it off.

## [0.8.2] - 2026-10-26

- **List columns**: drag the edge of a column header to change its width,
  double-click it to fit the content; the widths are kept per kind in this
  browser.
- **Filter**: past five values a facet shows the five largest and puts the
  rest in a "+ N more…" dropdown (what you pick shows as a toggle); every
  facet can pick the items without a value ("Not recorded", "No tags", "No
  category").
- **List**: the 🗑 is only on the Edit rows now (it asks, then takes that row
  out and keeps what you typed in the others); the checks are a little
  larger, on the covers too.

## [0.8.1] - 2026-10-26

- **List view**:
  - unlocked, every row has its check (and one at the top for all shown)
    without pressing Select first; the quick-action bar appears as soon as
    something is checked;
  - a 🗑 on each row that asks once more before moving it to the trash;
  - the title opens the details (again: closes them); a click elsewhere on
    a row closes them;
  - **Edit** in the toolbar turns the whole list into fields (it replaces the
    ✎ on each row); **Save** stores only the rows that changed, Cancel or
    Escape drops it all;
  - the Read / Watched date can be edited: it moves the last finished
    reading or watch (and its start, when that was later), or adds one when
    there is none.
- **Edit all…** has the Read / Watched date too (`ended` in `POST /api/bulk`).
- **Places to watch**: SSD and Streaming join the list, and "+ Your own…" in a
  watch's Where adds one of your own for good (`POST /api/platforms`; Other
  stays last). Migration 0017.
- **Grid**: no delete button on the covers any more (deleting one is in the
  list and the details); Select still picks covers to act on together.

## [0.8.0] - 2026-10-26

- **Sections** in the left pane: folders for your books, movies and series.
  An item lives in one section or in the main library; the main shelves show
  what is in none. Two are built in: **Archive** (out of sight, nothing
  deleted) and **Test**; both stay out of the Visuals and search, your own
  sections count everywhere. Migration 0016 (the statistics views leave
  Archive and Test out).
- **Select** (unlocked): a check on every cover (and a column in the list), a
  toggle at the start of each shelf row, All / None.
- **Quick actions** on the selection: Mark as read / watched, Move to a
  section, Archive / Unarchive, Add to a collection, Delete, and **Edit all**:
  one blank row over the shelf where only what you fill in (status, rating,
  category, tags to add, version, language, added date, collection) changes;
  everything else stays as each item has it.
- **Delete from the shelf**: unlocked, a button at the bottom right of each
  cover (it asks once more) moves it to the trash.
- `medialib test-data` fills the Test section with made-up books, movies and
  series (titles start "Test ·"); `--remove` deletes them all again.
- Fixed: a series with seasons in the trash could not be deleted for good (the
  30-day purge stopped on it).
- Fixed: the nightly base backup folder was readable by its owner only, so the
  health check saw an older one ("base backup < 26 h" failed).

## [0.7.0] - 2026-10-26

- **Film collections**: films that belong together (a trilogy, a saga) show
  as one card on the Movies shelf, the films stacked behind the first poster
  with their number on it, the way a series shows its seasons. The card
  opens a panel listing the films in release order, each opening its own
  panel, which links back to its collection. Filters, sorting and Visuals
  still count each film; a collection with one film in view stays a plain
  card; dragging the card moves all its films. Collections come from TMDB
  (filled on import, "+ Add movie" and Fill missing) or from a group the
  owner names in a watch import file (`film_collection`), which replaces
  TMDB's. Migration 0015.

## [0.6.2] - 2026-10-26

- Fixed: `import-watch` could not move a title from a watchlist (To watch) to
  Watched when a later viewing history had it: the correction of a status it
  had written itself as the owner's is now written under that same label.

## [0.6.1] - 2026-10-25

- Fixed: `import-watch` left a title **To watch** when its only watch came
  from the owner's own list (an undated watch of source `owner`), though the
  file said Watched. New entries now get the file's status; a rerun corrects
  a status this importer wrote itself, never one set in the console or by a
  Joplin note.

## [0.6.0] - 2026-10-25

- **Movies and series**: the Database and Visuals pages work for them as they
  do for books. Shelf, list, filters and the detail panel show the director
  or creators, cast, runtime or seasons, countries and posters; a series is
  one card with its seasons listed (each with its poster, episodes and what
  you did with it). Visuals: watched over time (movies or seasons, or
  hours), ratings, directors / creators, genres, where you watched, country,
  original language, the watching calendar, and the library view.
- **Watches per season**: a watch of a series can name its season, the
  episodes seen and where it was watched (Netflix, Prime Video, HBO Max,
  cinema…). Migration 0014.
- **TMDB**: "+ Add movie / series" searches TMDB, and Fill missing fills a
  movie's or series' empty fields from it. Needs a key in
  `secrets/tmdb.key` (empty = off).
- **Import a watch history**: `medialib import-watch FILE.json` (dry run
  unless `--apply`) adds movies and series with their watches, tags and
  collections from a file made from viewing-history exports (DESIGN.md
  §15.3); a second run changes nothing.
- The Filter pane's facets follow the kind: Where you watched, Country and
  Tags for movies and series; Released instead of Published.

## [0.5.1] - 2026-10-25

- List view row editing also covers **categories** (a checklist by group,
  from the button in the Genre cell) and the **Added** date.
- API: `entry.added` (YYYY-MM-DD, not in the future) changes when a book
  entered the library; owner-labelled like every console edit.
- Fixed: adding a finished reading to a book still To read left it To read;
  it now becomes Read.

## [0.5.0] - 2026-10-25

- **Page filter on Visuals**: one filter for every chart and tile on the page
  (status, version, language, category, read and added dates, published,
  rating, pages, EPUB, review), kept apart from the Database filter. The
  per-chart pickers (the time chart's year/month, the library's All / Read /
  Not read chips) are gone. A Read date in the filter also limits which
  readings count. All charts are now counted in the browser from the library
  list, so they follow the filter.
- **Books read over time**: an X-axis range slider at each level (year,
  month, day keep their own range), and Per period / Cumulative.
- **Pies**: authors, genres, languages (and the library's categories,
  languages, versions, authors) as pies by default, 8 colours + Other, with a
  Pie / Bars switch per chart (remembered) and the table view. A value keeps
  its colour while you filter. Slices and legend rows pick their books.
- **Reading calendar**: last 12 months, any single year or all years stacked;
  follows the page filter; click a day for its books.
- The library list carries each book's activity days (dated starts,
  finishes, progress).
- **List view**: only the cover opens a book's panel (a click elsewhere on
  the row does nothing); with the console unlocked, ✎ next to the cover edits
  title, authors, status, rating, pages, language and version in the row
  (Enter saves, Escape cancels; only changed values are written).

## [0.4.0] - 2026-10-25

- **Version of a book**: Paper, Ebook or Audiobook, one click in the book's
  panel (the editor keeps paperback and hardcover), chosen when adding a book
  (an EPUB defaults to Ebook), a small mark on ebook and audiobook covers, a
  Version column and sort in the list. Each reading has a "Read as" (new
  readings take the book's version; Joplin audiobook notes already set it).
- **Opening a book is smoother**: the panel opens at once with the cover and
  title while the details load, the covers glide aside to make room instead
  of jumping, and the book stays on its shelf while its copy flies open.
  Edits no longer redraw the whole shelf (no cover blink), and the panel
  keeps its scroll position.
- **Filter pane**: a Filter button opens a small pane with toggles (version,
  language of the book or original, categories, EPUB, review) showing how
  many books each leaves, and sliders for the read and added dates (by year,
  month or day, narrowed to a year or month), published year, rating and
  pages. Active filters show as removable chips; the choice is remembered.
- **Interactive visuals**: click a bar (a language, category, author, genre,
  rating, decade, version or period) to see its books under the chart, then
  open one or show them all in the Database with the matching filters.
  "Books read over time" replaces the per-year and per-month charts: books
  or pages, per year, month or day, narrowed to a year (and month), scrolling
  sideways when long. The library view gains a Version chart.
- The library list now carries each book's format, latest reading medium and
  finished readings (date, precision, language, pages, medium).

## [0.3.2] - 2026-10-25

- The wood covers the whole page: a wooden top rail, the left pane and the
  shelves on the wood, panels and cards as paper, and a warm palette to
  match. The shelves run from the left pane to the window edge.
- A button at the top left hides the left pane (remembered per browser).
- The cover opening is slower (about 1.6 s).

## [0.3.1] - 2026-10-25

- **Bookcase**: the book area is a wooden case (light wood, walnut in dark
  mode) with a board under every row across the case; pointing at a cover
  lifts and enlarges it; clicking flies the cover to the info panel and opens
  it on its spine (not with Reduce motion).
- **Find link**: the address can be changed from a book's panel (Change
  address), kept over `.env`; the search words go in form-style (`+` for
  spaces).
- Deploy script: on a tagged Tailscale node the owner login is taken from
  the untagged devices, not the node itself (which made the console
  read-only for the owner).

## [0.3.0] - 2026-10-25

- **Trash**: deleting a book moves it to the Trash for 30 days (restore or
  delete now); a daily `purge-trash` then removes it and its files for good.
  A re-import doesn't bring deleted books back.
- **Categories**: two levels, Fiction and Non-fiction with their categories,
  in the left pane, a toolbar picker and the editor. `medialib categorize`
  moves existing books onto the tree from your labels and provider subjects
  (a report says why for each book); new books get theirs on arrival.
- **Metadata fill**: Google Books (with an API key), Apple genres and Open
  Library subjects for categories, title-only search with one row per edition
  and summary products dropped, and **add from an EPUB** (its own metadata,
  cover and file).
- **Read in the browser**: a built-in EPUB reader (foliate-js), plus a
  configurable "find this book" link for books without a file.
- **Visuals**: My reading / The library (categories, original language,
  authors, release decades).
- A light wooden shelf under the covers.
- Migration 0013. Fixed: the reading calendar counted deleted books.

## [0.2.0] - 2026-10-25

Readest and Joplin reach the library directly.

- **Readest**: an OPDS 1.2 catalog (`/opds`: Reading, To read, Recently added,
  Read, All books, search) of the books with an ebook file, and the KOReader
  sync protocol (`/kosync`) for reading positions. Moving into a book marks it
  Reading, the last page marks it Read, starting over after the end is a
  re-read. Copies Readest got from Calibre-Web are recognised by their old
  checksums; others can be linked once in the console. Separate generated
  login for reading apps (DESIGN.md §13.1).
- **Joplin review notes**: `medialib joplin` reads a Joplin sync folder
  (read-only) and writes each note's status, rating, review, dated reading and
  collections, matching the book in the library or through Apple Books + Open
  Library, never by guess (§13.2). `ops/joplin-sync.sh` runs it from cron.
- **Console**: a **Sync** section (Readest login and synced books, Joplin
  feed's last run and notes that need a look); history names Readest and
  Joplin as the source of a change.
- `medialib adopt-files` copies imported Calibre-Web EPUBs into the store.
- Migration 0012 (schema `sync`, `catalog.document_alias`, `ingest.run.summary`).
  Health: the Joplin feed's last run.

## [0.1.0] - 2026-10-24

First release.

- **Database**: PostgreSQL 18.6 schema in 11 forward-only migrations. Media items
  with subtypes (book work → edition → file; movie; series → season →
  episode), contributors with roles, external ids with unique constraints,
  covers, files with sha256 and the KOReader partial MD5.
- **Your side**: status, readings with date precision (re-reads are separate
  readings), rating 0–10, review, tags and genres, shelves with manual order
  and a display preference each.
- **Integrity**: field-level provenance with owner protection in the
  database, a hash-chained append-only audit log, soft delete, least-privilege
  roles (migrator, app, read-only), data checksums.
- **Import** from Yamtrack and Calibre-Web Automated: dry run by default,
  idempotent, a completeness report, and values changed in the console are kept.
- **Metadata fill** for new books: Apple Books (iTunes Search) for the cover
  and basics, Open Library by ISBN for the rest, behind a provider interface.
- **Console**: Database (Apple Books-style shelf with drag reorder,
  collections, sort, Read / To read / Reading filters, detail panel with
  editing, cover and EPUB upload, add a book) and Visuals (per year and
  month, pages, ratings, authors, genres, languages, reading calendar).
  Movies and Series are visible and empty.
- **Operations**: Docker Compose (db + app + tools), WAL archive, dumps,
  nightly base backup, weekly restore test, `/api/health`.

Known limits: the statistics bucket days in one fixed time zone (migration
0007; a setting is planned); a single owner.
