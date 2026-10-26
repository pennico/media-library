# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

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
