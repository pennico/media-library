# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

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
0007; a setting is planned); a single owner, no reader accounts.
