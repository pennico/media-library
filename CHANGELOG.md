# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

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
