# media-library

A personal book, movie and series database with a small web console,
for one person's library on a home server. PostgreSQL holds the data with
the rules enforced by the database itself; the console is a single page that
looks like a bookshelf.

- **Database**: an Apple Books-style cover shelf you can arrange (drag to
  reorder, collections, sort, size), filters for Read / To read / Reading
  and a Filter pane (version, language, category, dates, rating, pages),
  and a detail panel for metadata, rating (0–10), review and readings,
  including re-reads and dates as precise as you know them (year, month or day).
- **Visuals**: statistics per year, month or day, pages, ratings, authors,
  genres, languages, versions and a reading calendar, under one page filter;
  pies or bars, cumulative or per period; click a bar or slice to see its books.
- **Movies and series**: the same shelf, panel and Visuals, with directors,
  creators, cast, runtime, countries and posters from TMDB (with a key);
  a series is one card with its seasons, and each watch can name its
  season, the episodes seen and where it was watched. Add by TMDB search, or
  import a whole watch history (`medialib import-watch`).
- **Versions**: paper, ebook or audiobook for each book, and for each reading.
- **Add a book** from Apple Books (cover and basics), Open Library (ISBN,
  pages, publisher) and Google Books (with a key), from an EPUB file (its own
  metadata and cover), or by hand. Upload a cover or an EPUB (stored by
  sha256, with its Readest/KOReader id).
- **Readest**: an OPDS catalog of your ebooks and KOReader-protocol position
  sync; reading in Readest moves a book to Reading and, at the end, to Read.
- **Joplin review notes** (one note per book or film: rating, review, date,
  collections) flow in hourly from a Joplin sync folder.
- **Categories** in two levels (Fiction / Non-fiction and their kinds), filled
  from your own labels and the providers' subjects.
- **Read** EPUBs in the browser, or follow a configurable search link for
  books you don't have a file of yet. Deleted books wait 30 days in a Trash.
- **Import** from Yamtrack and Calibre-Web Automated without losing data: every
  source field is imported or listed as dropped, with the reason, and a
  completeness report checks that nothing went missing.
- **Integrity**: every value records where it came from, and the database
  refuses to let an automated source overwrite a value you entered. There's
  a hash-chained append-only audit log and soft delete. Separate migrator, app and
  read-only roles. Data checksums are on, the database has no host port, and
  backups use dumps plus WAL archiving for point-in-time recovery, with a weekly
  restore test.
- **Access**: tailnet only through Tailscale Serve. Only the owner's
  Tailscale login can write, after unlocking with a password or a passkey
  (shared with [agent-console](https://github.com/pennico/agent-console) if
  you use it).

Status: 0.6.0, used daily by its author.

## Docs

| File | What |
|---|---|
| [`docs/INSTALL.md`](docs/INSTALL.md) | install, update, backups, rollback |
| [`DESIGN.md`](DESIGN.md) | needs, PostgreSQL vs MySQL, schema, integrity and operations, import mapping |
| [`DECISIONS.md`](DECISIONS.md) | design decisions (cited in the code as D1…D42) |
| [`SECURITY.md`](SECURITY.md) | security model and how to report a problem |
| [`CHANGELOG.md`](CHANGELOG.md) | releases |

## Layout

| Path | What |
|---|---|
| `migrations/NNNN_*.sql` | the schema, forward-only, one transaction each |
| `medialib/` | Python package: `config`, `db` (audited write transactions), `bootstrap`, `migrate`, `importer/`, `fill/` (metadata providers), `categories`, `epubmeta`, `feeds/` (Joplin notes, KOReader sync), `web/` (server, auth, queries, writes, OPDS, reading-app login), `adopt`, `covers`, `files`, `text` |
| `web/index.html` | the console: one file, no build step |
| `web/reader.html`, `web/reader.js`, `web/vendor/foliate-js/` | the EPUB reader (foliate-js vendored, see `VENDORED.md`) |
| `tests/` | one test per database rule, importer, metadata fill, web API |
| `tools/devdb.py` | development PostgreSQL 18.6 without root (PGDG packages in `.devdb/`) |
| `tools/restore_drill.py` | dump/restore + point-in-time recovery drill on the dev cluster |
| `ops/` | WAL archiver, dump, base backup, restore test and Joplin feed scripts |
| `deploy/` | Dockerfile, compose file, `pg_hba.conf`, `env.example`, `make-password.py` |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python3 tools/devdb.py fetch && python3 tools/devdb.py init && python3 tools/devdb.py start
eval "$(python3 tools/devdb.py env)"; export MEDIALIB_MEDIA_DIR=$PWD/media
.venv/bin/python -m medialib bootstrap                 # roles + database "medialib" (superuser)
.venv/bin/python -m medialib migrate
.venv/bin/python -m medialib import data               # dry run: report in out/, nothing written
.venv/bin/python -m medialib import data --apply       # commit; run it again: 0 changes
.venv/bin/python -m pytest -q tests/
.venv/bin/python tools/restore_drill.py --db medialib  # backup/restore drill
```

Every write goes through `medialib.db.write_tx(conn, source, client)`: the
database refuses writes that do not say where the values come from
(`ml.source`) and who writes (`ml.client`), labels every changed column with
its source, and never lets an automated source change a value the owner set.

Tests can also use any PostgreSQL 18 server: set `MEDIALIB_DB_HOST`,
`MEDIALIB_DB_PORT` and `MEDIALIB_SECRETS_DIR` (a folder with `postgres.pw`)
before running pytest, as CI does.

## License

MIT, see [LICENSE](LICENSE).
