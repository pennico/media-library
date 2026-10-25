# media-library: design (phase 0)

Status: the design behind 0.1.0, written before any app code and kept up to
date with it. It was checked against a real Yamtrack + Calibre-Web Automated
library of about 150 books (§0). "The owner" is the one person whose library
this is; the console has no other users.

Short version:

- **PostgreSQL 18** (18.6 today), official Debian image pinned to the exact
  minor version. It meets every need in §1 with built-in features (domains,
  transactional DDL, deferrable constraints, partial indexes, ICU, PITR).
  MySQL 8.4 would need workarounds for five of them. §2.
- One database `medialib` with six schemas: `catalog` (what things are),
  `library` (the owner's side), `audit` (append-only, hash-chained), `ingest`
  (raw source rows and import runs), `stats` (read-only views for Visuals),
  `meta` (migrations). §3–§4.
- Every owner-facing value records **where it came from** (yamtrack / cwa /
  apple / openlibrary / owner / …). The database itself refuses to let any
  automated source overwrite a value the owner entered. §4.4.
- The import is **lossless**: every source row is kept raw in
  `ingest.source_record`. Every field is mapped to a column or listed as
  dropped, with the reason. §6.
- App: Python, the standard-library HTTP server (same as agent-console), plus
  three pinned dependencies: `psycopg`, `psycopg-pool`, `Pillow`. §8.
- In the snapshot, every completed reading has a **year only** (the Excel
  "Reading Year"); no read date has a month or day. The schema keeps the
  precision explicitly, so stats never invent a month. §4.3, §9.

---

## 0. What a real library looked like

The importer was built against a snapshot of a Yamtrack log (about 140
entries, a quarter of them rated) and a Calibre-Web Automated library (about
150 records, most of them empty records from an earlier spreadsheet, a few
dozen with an EPUB). What that data taught, and what the design answers:

- **Yamtrack `notes` are often structured** (`By: … · Read in: … · Genre: … ·
  Pages: … — <review>`), written by an earlier migration. The importer parses
  them field by field and keeps the review text as is.
- **Every completed reading had a year only**, no month or day. The schema
  stores the date precision explicitly, so statistics never invent a month
  (§4.3).
- **The two sources disagree**, and each is right about something: CWA's
  authors were the corrected ones, Yamtrack's ratings were the current ones
  (score edits made after the last sync). Hence the per-field conflict rules
  in §6 instead of "one source wins".
- **Yamtrack keeps old editions** as separate items after an entry was moved
  to another edition. They become extra editions of the same work, not
  duplicate books.
- **Box sets and test files** exist in CWA and are not books: a link file can
  mark them `ignore`.
- **Readest/KOReader ids** (the partial MD5) change with each version of a
  file, so the importer keeps the whole checksum history (§11).

---

## 1. Needs

Functional (from the brief and the data):

- **N1** One catalogue for books now, movies and series later: book work →
  edition → file; movie; series → season → episode.
- **N2** Contributors with roles (author, translator, narrator, director, …),
  at work or edition level (Open Library lists translators as authors).
- **N3** External IDs (Open Library edition/work, ISBN-13, Apple, Google,
  TMDB, IMDb, Wikidata, plus legacy `yamtrack_item`, `cwa_book`): unique, validated.
- **N4** Covers: several candidates per edition (owner, Apple, Open Library,
  CWA copy), each with source, URL, local file, sha256; exactly one chosen.
- **N5** Files: path, size, sha256, KOReader partial MD5 (kosync), so OPDS +
  kosync can be added later with no migration pain.
- **N6** Owner side: status (to read / reading / read / paused / abandoned),
  readings as separate events (a re-read is a new reading), each with start,
  end, progress, the language it was read in, and a source. Also rating (lossless
  for Yamtrack's 0–10 with one decimal), review, genres and tags.
- **N7** Shelves (Apple Books "collections") with manual drag order and a
  display preference per shelf (sort, cover size, layout).
- **N8** Field-level provenance; values the owner entered are never
  overwritten by automated fills.
- **N9** Mixed-language sorting and search (Italian, Danish, English; also
  French, German, Portuguese, Polish, Japanese and Hebrew originals): "Løgner"
  sorts as Danish where wanted, "L'amico ritrovato" files under A, and
  searching "logner" or "lackberg" finds the book.
- **N10** Visuals: per year/month counts and pages, rating distribution,
  authors/genres/languages, reading calendar, current progress, from a
  read-only role.
- **N11** Room for open-data fill (Apple, Open Library now; Google Books,
  Wikidata, Danish library data later), for an LLM "fix issues" assistant
  that proposes diffs, and for TMDB.

Integrity and operations ("gold standard"):

- **G1** NOT NULL / FK / CHECK on every column where it makes sense; domains for
  recurring value types; invariants that span rows enforced in the database,
  not only in the app.
- **G2** Append-only audit (old/new, when, which client), tamper-evident.
- **G3** Soft delete for owner-facing entities.
- **G4** Plain versioned SQL migrations, atomic.
- **G5** Least-privilege roles: migrator, app, read-only (Visuals).
- **G6** Page checksums from initdb; no host port for the DB.
- **G7** Nightly logical dump plus WAL archiving for point-in-time recovery,
  fitting the host's file backup schedule, with a **tested** restore.
- **G8** Health check covering the DB, backups, archiver, checksums and
  collation drift.
- **G9** Small footprint on a small home server (16 GB RAM shared with other
  services): one small Postgres, one small app container.

Scale: hundreds of items now, a few thousand in ten years; one writer (the
owner), a handful of reads per minute. Performance needs nothing special;
correctness, recoverability and clarity matter most.

---

## 2. PostgreSQL vs MySQL

Compared for **these needs**, current releases: PostgreSQL 18 (18.6) vs
MySQL 8.4 LTS (MariaDB 11.x noted where it differs).

| Need | PostgreSQL 18 | MySQL 8.4 | Verdict |
|---|---|---|---|
| CHECK constraints (G1) | Enforced, including on domains | Enforced since 8.0.16; no domains, so every rule is repeated per column | PG |
| Domains (G1) | `CREATE DOMAIN isbn13 AS text CHECK (…check digit…)`, reused everywhere | None | PG |
| Rules spanning rows (G1) | Partial unique indexes ("one chosen cover per edition", "one open reading per book"), exclusion constraints, **deferrable** unique constraints and constraint triggers | No partial indexes (needs generated-column tricks); unique checks run row by row and nothing is deferrable, so renumbering positions in place can collide | **PG, decisive** for drag reorder (N7) and the status↔reading invariant |
| Transactional DDL (G4) | Yes: a migration commits fully or not at all | DDL commits implicitly; 8.0's "atomic DDL" covers single statements only, so a failed multi-step migration leaves the schema half-changed | **PG, decisive** |
| JSONB (provider payloads, provenance) | `jsonb`, GIN indexes, jsonpath, `to_jsonb(NEW)` in triggers | `JSON` type (binary), multi-valued indexes, no generic row→JSON in triggers | PG |
| Audit triggers (G2) | One generic trigger function for all tables (`to_jsonb(OLD/NEW)`, `TG_TABLE_NAME`), `SECURITY DEFINER`, per-transaction client tag via `SET LOCAL` | One trigger per table and event, columns spelled out by hand | PG |
| Collations (N9) | ICU collations per column/expression (`da-x-icu`, `it-x-icu`, numeric ordering `-u-kn`), nondeterministic (accent/case-insensitive) collations, plus the stable **builtin** `C.UTF-8` provider (PG 17+) for keys | UCA 9.0 collations incl. `utf8mb4_da_0900_ai_ci`; per-query `COLLATE` works; fewer options | PG ahead, MySQL adequate |
| Full-text search (N9) | `tsvector` with Snowball stemmers for Italian, Danish and English, `unaccent`, `pg_trgm` fuzzy matching ("Lackberg" ≈ "Läckberg"); all in contrib and *trusted* (no superuser needed) | InnoDB FULLTEXT without stemming; no trigram similarity | **PG** |
| Point-in-time recovery (G7) | Built in: WAL archiving, `pg_basebackup`, `pg_verifybackup`, PG 17 incremental backups; mature tools (pgBackRest, WAL-G) | Binlog replay; physical hot backup needs Percona XtraBackup (third party) or Enterprise Backup (paid) | **PG** |
| Page checksums (G6) | On by default from PG 18 (`initdb --data-checksums` before) | InnoDB page checksums on by default | tie |
| Roles (G5) | Default privileges, column privileges, `SECURITY DEFINER`, PG 16 `GRANT … WITH INHERIT FALSE, SET TRUE`, `pg_read_all_data` | Roles and column privileges exist; no `SECURITY DEFINER` equivalent for trigger isolation (DEFINER stored programs are close) | PG |
| Python driver | psycopg 3 (binary wheels include libpq, server-side binding, COPY) | mysqlclient (C build) / PyMySQL (pure Python) | tie |
| Familiar on home servers | the usual self-hosted apps (Nextcloud, Immich, …) already ship PostgreSQL | Fewer | PG |
| Licence | PostgreSQL licence (permissive) | GPLv2, Oracle-owned | PG |

**Where the expectation does not hold, or Postgres costs something:**

- **Major upgrades are manual.** A new major version (18 → 19) needs
  `pg_upgrade` or dump-and-restore. MySQL upgrades LTS lines in place. For a
  database this size, dump-and-restore takes seconds and the restore test
  (§5.6) exercises it every week, so the cost is low. It is still a step the
  owner has to remember.
- **ICU collations depend on the ICU library version.** A base-image change
  (e.g. Debian 12 → 13: ICU 72 → 76) can reorder strings and corrupt indexes
  that use those collations until they are reindexed. Mitigation: the
  database default is the *builtin* `C.UTF-8` provider, which does not use
  ICU, so primary and unique keys never depend on ICU. ICU is used only in
  explicit sort indexes. The health check compares `pg_collation_actual_version`
  with the recorded versions and flags drift. MySQL has the same class of
  problem, more hidden.
- **MVCC needs VACUUM.** Autovacuum handles it at this scale; nothing to tune.
- **Process per connection.** Irrelevant at ≤ 10 connections; the app pools.
- **MariaDB's system-versioned tables** (automatic row history) are a real
  alternative for G2. They do not fix transactional DDL, partial indexes or
  stemming, and the audit trigger here does the same job and is tamper-evident.

**Recommendation: PostgreSQL 18.** Pin the image to the exact minor version,
`postgres:18.6-bookworm`. Bookworm, not the default trixie variant, so the
ICU version (72) matches the development cluster used to test this design;
§5.7 covers moving later. Do not use 19.0 yet: it is new, and a
"gold-standard" database waits for the first minor releases. Watchtower is
disabled for the DB container; minor upgrades are a one-line tag change (§5.7).

The development cluster used for phase 1 is the same PostgreSQL 18.6 from the
official PGDG Debian packages, unpacked without root in this container
(`--with-icu`, ICU 72, checksums on). Tests run against the same engine,
version and ICU as production.

---

## 3. Architecture

```
 tailnet browser ──https──► Tailscale Serve :<port> ► 127.0.0.1:8099 ─┐
                                                                     │ (host)
 ┌──────────────────────── compose project "media-library" ──────────┼──────┐
 │  medialib-app  (python:3.13-slim, read-only rootfs, uid 1000)     ▼      │
 │    • console page + JSON API + covers                                    │
 │    • pool "app"      → role ml_app       (reads + writes, audited)       │
 │    • pool "visuals"  → role ml_readonly  (stats.* views only)            │
 │            │ internal network "medialib" (no host port)                  │
 │  medialib-db   (postgres:18.6-bookworm, checksums, WAL archive)          │
 └──────────────────────────────────────────────────────────────────────────┘
   <install dir>/{pgdata, wal-archive, backups, media/covers}
       └── the host's file backup (e.g. restic/Backrest) snapshots it
```

The importer and the metadata fill run as one-off commands in the app
container (`docker compose run --rm app medialib import …`).

Schemas:

| Schema | Holds | Written by |
|---|---|---|
| `catalog` | media items and subtypes, editions, files, people, contributions, external IDs, covers, lookups | app, importer, fill |
| `library` | entries (status, rating, review), readings, tags, shelves | app, importer |
| `audit` | `audit.change` (append-only, hash chain) | triggers only |
| `ingest` | import runs, raw source records, source→target id map | importer |
| `stats` | views for Visuals | nobody (views) |
| `meta` | `schema_migration` | migrator |

---

## 4. Schema

The authoritative definition will be `migrations/0001_*.sql` (phase 1). This
section is the design those migrations implement.

### 4.1 Conventions

- **Keys:** `bigint GENERATED ALWAYS AS IDENTITY`. Owner-facing rows also have
  `public_id uuid NOT NULL DEFAULT uuidv7() UNIQUE` (PG 18 built-in, time-ordered),
  used in URLs, exports and future OPDS entry ids (`urn:uuid:…`); internal ids
  are never exposed.
- **Timestamps:** `created_at` / `updated_at timestamptz NOT NULL`
  (`updated_at` by trigger). The server time zone is UTC; the app renders
  local time (in 0.1.0 the stats views bucket days in one fixed zone, set in
  migration 0007; a setting is planned).
- **Soft delete:** `deleted_at timestamptz NULL` on owner-facing entities
  (media items, editions, files, covers, entries, readings, shelves, tags). Unique
  indexes that must allow re-adding are partial (`WHERE deleted_at IS NULL`).
  Link tables (shelf membership, tags on items, contributions) are hard-deleted;
  the audit log keeps them. Purging soft-deleted rows is a migrator-only
  maintenance command.
- **Vocabularies:** lookup tables with text codes (`'planned'`, `'isbn13'`),
  referenced by FK, rather than enums (codes stay readable in raw data and dumps,
  and values can be retired). Codes use the domain `catalog.code`
  (`^[a-z][a-z0-9_]{0,39}$`).
- **Domains** (all in `catalog`):

  | Domain | Base | Rule |
  |---|---|---|
  | `label` | text | trimmed, not empty, ≤ 1000 chars (titles, names) |
  | `code` | text | `^[a-z][a-z0-9_]{0,39}$` |
  | `isbn13` | text | `^97[89][0-9]{10}$` and valid check digit (`catalog.isbn13_ok()`, IMMUTABLE) |
  | `sha256_hex` | text | `^[0-9a-f]{64}$` |
  | `md5_hex` | text | `^[0-9a-f]{32}$` |
  | `precision` | text | `day` / `month` / `year` |
  | `rating` | numeric(3,1) | 0.0–10.0 (Yamtrack's scale and decimal places: lossless) |
  | `percent` | numeric(5,2) | 0–100 |
  | `rel_path` | text | relative, no leading `/`, no `..` segment, no NUL |
  | `http_url` | text | `^https?://` and ≤ 2000 chars |

- **Partial dates:** a `date` column plus a `precision` column, both null or
  both set. CHECK `catalog.date_ok(d, p)`: `year` ⇒ 1 January, `month` ⇒ day 1.
  "Read in 2019" is stored as `2019-01-01` / `year` and shown as "2019".
  Ancient works use BC dates (`Il Simposio`: about 385 BC).
- **Text:** the database default collation is the builtin `C.UTF-8` (stable
  across ICU upgrades). Sorting uses explicit ICU collations created by the
  migration, with numeric ordering:
  `catalog.sort_und` (`und-u-kn-true`, CLDR root, the default for a mixed
  shelf), `catalog.sort_da`, `catalog.sort_it`, `catalog.sort_en`. A shelf can pick
  one (§4.3). Accent-insensitive matching uses
  `catalog.f_unaccent()`, an IMMUTABLE wrapper over `unaccent` with the dictionary
  named explicitly (the standard pattern, so it can be indexed), plus `pg_trgm`.
- **Search path:** all objects are schema-qualified in code; `SECURITY DEFINER`
  functions pin `SET search_path = pg_catalog, pg_temp`.

### 4.2 Catalog: what things are

```
media_kind ◄── media_item ──┬── book (work) ──< edition ──< edition_file
                            │                    │
                            ├── movie            ├──< cover (edition)
                            ├── series ──< season ──< episode
                            │
                            ├──< contribution >── person      (work or edition level)
                            ├──< external_id                  (item, edition or person)
                            └──< cover (posters for movies/series)
```

**`catalog.media_kind`** (lookup): `book`, `movie`, `series`, `season`, `episode`.

**`catalog.media_item`**: the supertype for everything the owner can track.

| Column | Type | Rule |
|---|---|---|
| id, public_id | | §4.1 |
| kind | code | FK `media_kind`; `UNIQUE (id, kind)` so subtypes can pin their kind |
| title | label | NOT NULL. For books: the chosen edition's title (Yamtrack's) |
| title_language | char(3) | FK `language`; language of `title`, drives article stripping |
| sort_title | label | NOT NULL; trigger strips the leading article for `title_language` ("The ", "Il ", "L'", "Den "…) unless the owner set it |
| original_title | label | null |
| original_language | char(3) | FK `language` |
| first_published, first_published_precision | date, precision | partial date |
| description | text | null or non-empty |
| locked | boolean | NOT NULL DEFAULT false. Automated sources may not change this item or its editions (tts-ereader working copies) |
| provenance | jsonb | §4.4 |
| created_at, updated_at, deleted_at | | §4.1 |

**Subtypes** (one row per item of that kind; `item_id` is PK and FK
`(item_id, kind) → media_item (id, kind)` with `kind` fixed by CHECK, so a
movie row can never point at a book item):

- `catalog.book (item_id, kind='book')`: the **work**. It carries no extra
  columns yet; it exists so editions can reference only books.
- `catalog.movie (item_id, kind='movie', release_date, runtime_min > 0)`.
- `catalog.series (item_id, kind='series', first_air_date, last_air_date,
  status ∈ returning/ended/canceled/unknown)`.
- `catalog.season (item_id, kind='season', series_id → series, season_number ≥ 0,
  air_date; UNIQUE (series_id, season_number))`.
- `catalog.episode (item_id, kind='episode', season_id → season,
  episode_number ≥ 0, air_date, runtime_min; UNIQUE (season_id, episode_number))`.

Movies/series are schema only for now (no rows in Yamtrack). TMDB fills them later.

**`catalog.edition`**: a published form of a book work.

| Column | Type | Rule |
|---|---|---|
| id, public_id | | |
| work_id | bigint | FK `book(item_id)`; `UNIQUE (id, work_id)` (target of a composite FK, §4.3) |
| title, subtitle | label | title NOT NULL |
| language | char(3) | FK `language` |
| publisher | label | |
| published_on, published_precision | date, precision | partial date |
| pages | integer | 1–20000 |
| format | code | FK `edition_format`: `ebook`, `paperback`, `hardcover`, `audiobook`, `unknown` |
| description | text | publisher blurb (CWA comments, Open Library) |
| is_primary | boolean | partial UNIQUE `(work_id) WHERE is_primary AND deleted_at IS NULL`: the edition whose title, cover and metadata represent the work ("Yamtrack's chosen edition") |
| provenance, timestamps, deleted_at | | |

A deferred constraint trigger checks that every live book has **exactly one**
live primary edition at commit time.

**`catalog.edition_file`**: an actual file of an edition.

| Column | Type | Rule |
|---|---|---|
| edition_id | bigint | FK `edition` |
| format | code | FK `file_format`: `epub`, `pdf`, `mobi`, `azw3`, `cbz`, `m4b`, `mp3` |
| storage | code | `cwa` (still lives in CWA's library) or `store` (our own file store, for OPDS later) |
| rel_path | rel_path | relative to the storage root; partial UNIQUE `(storage, rel_path)` |
| size_bytes | bigint | > 0 |
| sha256 | sha256_hex | NOT NULL; partial UNIQUE |
| kosync_md5 | md5_hex | KOReader partial MD5 (what Readest sends to `/kosync`); partial UNIQUE |
| added_at | timestamptz | |
| missing_since | timestamptz | set when a check finds the file gone |

The importer computes the partial MD5 itself (KOReader algorithm: 1 KiB samples
at offsets 256 B and 1 KiB·4ⁱ for i = 0…10) and checks it against CWA's
`book_format_checksums`. A mismatch is reported.

**`catalog.person`**: `name` (label), `sort_name` (label, from Calibre's
`author_sort` or derived "Last, First"), `disambiguation` (label, null).
UNIQUE `(f_unaccent(lower(name)), coalesce(disambiguation,''))` among live rows,
so "Jose Saramago" and "José Saramago" cannot become two people.

**`catalog.contributor_role`** (lookup): `author`, `translator`, `editor`,
`illustrator`, `narrator`, `foreword`, `director`, `writer`, `creator`, `cast`.

**`catalog.contribution`**: `person_id`, `role`, exactly one of `item_id`
(work level: authors, directors) or `edition_id` (edition level: translators,
narrators), `position smallint ≥ 1`, `credited_as label` (the name as printed).
CHECK `num_nonnulls(item_id, edition_id) = 1`. UNIQUE `(item_id, edition_id,
role, person_id) NULLS NOT DISTINCT`. UNIQUE `(item_id, edition_id, role,
position) NULLS NOT DISTINCT DEFERRABLE` (so authors can be reordered in one
statement).

**`catalog.provider`** (lookup): `code`, `label`, `applies_to` (item / edition
/ person), `pattern` (regex), `url_template`, `legacy` (bool). Seed:

| code | applies to | pattern |
|---|---|---|
| `openlibrary_edition` | edition | `^OL[0-9]+M$` |
| `openlibrary_work` | item | `^OL[0-9]+W$` |
| `openlibrary_author` | person | `^OL[0-9]+A$` |
| `isbn13` | edition | domain `isbn13` (ISBN-10 converted on input) |
| `apple_books` | edition | `^[0-9]+$` (iTunes trackId) |
| `google_books` | edition | `^[A-Za-z0-9_-]{12}$` |
| `amazon_asin` | edition | `^[A-Z0-9]{10}$` |
| `ibdb` | edition | uuid |
| `calibre_uuid` | edition | uuid |
| `tmdb_movie`, `tmdb_tv` | item | `^[0-9]+$` |
| `imdb` | item | `^tt[0-9]{7,}$` |
| `wikidata` | item, person | `^Q[0-9]+$` |
| `yamtrack_item` (legacy) | edition | `^[0-9]+$` (Yamtrack `app_item.id`) |
| `yamtrack_manual` (legacy) | edition | uuid (Yamtrack manual `media_id`) |
| `cwa_book` (legacy) | edition | `^[0-9]+$` (CWA `books.id`) |

**`catalog.external_id`**: `provider`, `value`, exactly one of `item_id` /
`edition_id` / `person_id`. **UNIQUE (provider, value)**: one Open Library
edition, ISBN or CWA id identifies exactly one row, including soft-deleted ones,
so re-adding a deleted book restores it instead of duplicating it. A trigger
validates `value` against `provider.pattern` (plus the ISBN check digit) and
checks that the owning column matches `applies_to`. One edition may have several
ISBNs and several `cwa_book` ids (CWA has two empty records for *Meditations*).

**`catalog.cover`**: exactly one of `edition_id` (books) / `item_id` (posters).
`source` (FK `data_source`: owner, apple, openlibrary, google, tmdb, cwa),
`source_url http_url`, `local_path rel_path` (content-addressed:
`covers/ab/<sha256>.jpg`), `sha256`, `mime ∈ jpeg/png/webp`, `width`, `height`,
`bytes`, `fetched_at`, `is_chosen`, `note`. CHECKs: at least a URL or a local
file; `local_path` and `sha256` both set or both null; **a chosen cover must be
stored locally** (`NOT is_chosen OR local_path IS NOT NULL`). Partial UNIQUE
`(edition_id) WHERE is_chosen AND deleted_at IS NULL` (same for `item_id`);
UNIQUE `(edition_id, source_url)`. Alternatives (the Open Library cover that
Apple replaced, the Apple cover the owner rejected) are kept as non-chosen rows
with a URL only, so the console can offer them later.

**Other lookups:** `catalog.language` (`code char(3)` ISO 639-3 PK, `bcp47`,
`name_en`, `sort_collation` e.g. `catalog.sort_da`; seeded with the languages in the
data plus the obvious neighbours), `catalog.data_source` (§4.4),
`catalog.edition_format`, `catalog.file_format`.

### 4.3 Library: the owner's side

**`library.status`** (lookup): `code`, `label_book`, `label_screen`, `sort`.

| code | book label | movie/series label | Yamtrack |
|---|---|---|---|
| `planned` | To read | To watch | Planning |
| `in_progress` | Reading | Watching | In progress |
| `completed` | Read | Watched | Completed |
| `paused` | Paused | Paused | Paused |
| `dropped` | Abandoned | Abandoned | Dropped |

**`library.entry`**: one per top-level item in the owner's library (book,
movie, series), `item_id` PK/FK.

| Column | Type | Rule |
|---|---|---|
| item_id | bigint | PK, FK `media_item`; trigger: item kind ∈ book/movie/series |
| status | code | NOT NULL DEFAULT `planned` (owner, 2026-10-24: "default is to read"), FK `status` |
| status_changed_at | timestamptz | |
| rating | rating | null = not rated |
| rated_at | timestamptz | |
| review | text | null or non-empty |
| reviewed_at | timestamptz | |
| added_at | timestamptz | NOT NULL |
| provenance, timestamps, deleted_at | | |

Rating and review belong to the **book**, not to a reading. That is what the
detail sheet shows and what Visuals counts. A reading has its own `note`.
Yamtrack keeps a score per entry; with no re-reads in the data, both models
hold the same values (decision D5). Earlier ratings remain visible through the
audit history (the 8 Yamtrack score edits are imported into it, §6).

**`library.consumption`**: one row per time through an item: a reading of a
book (a re-read is a new row), and later a viewing of a movie. The name is
neutral because the table serves both; the UI says "reading" or "watch".

| Column | Type | Rule |
|---|---|---|
| id, public_id | | |
| item_id | bigint | FK `library.entry(item_id)`: only items in the library have readings |
| edition_id | bigint | null; composite FK `(edition_id, item_id) → edition (id, work_id)`, so the edition must belong to that work |
| state | code | `in_progress`, `paused`, `completed`, `dropped` |
| started_on, started_precision | date, precision | partial date, null = unknown |
| ended_on, ended_precision | date, precision | null while in progress/paused; may be null (unknown) when completed/dropped |
| progress_pct | percent | |
| progress_pages | integer ≥ 0 | |
| progress_at | timestamptz | |
| language | char(3) | FK `language`: the language it was read in ("Read in") |
| medium | code | FK `edition_format`, null: how it was read (ebook, paper, audiobook) |
| note | text | |
| source | code | FK `data_source`: where the event came from (yamtrack, owner, kosync…) |
| provenance, timestamps, deleted_at | | |

CHECKs: `state IN ('in_progress','paused') ⇒ ended_on IS NULL`;
`started_on ≤ ended_on`; partial dates valid. Partial UNIQUE `(item_id) WHERE
state IN ('in_progress','paused') AND deleted_at IS NULL`: at most one open
reading per book.

**Status and readings stay consistent** (deferred constraint trigger on both
tables, checked at commit):

- `in_progress` ⇔ exactly one open reading in state `in_progress`;
- `paused` ⇔ exactly one open reading in state `paused`;
- `completed` ⇒ no open reading and at least one `completed` reading;
- `dropped` ⇒ no open reading and the latest reading is `dropped`;
- `planned` ⇒ no open reading (past readings allowed: "read before, want to
  read again").

The console changes status through one API call that also opens or closes
the reading in the same transaction ("Mark as reading" opens one today; "Read"
closes it today).

**`library.tag`**: `kind ∈ genre/tag`, `name` (label), UNIQUE
`(kind, f_unaccent(lower(name)))`. **`library.item_tag`**: `(item_id, tag_id)`
PK, `position` (genre order: "Fiction, Sci-Fi" keeps Fiction first), `source`.
Imported genres are the owner's Excel/CWA genres split on `,` and `;`.

**`library.shelf`** (Apple Books "collections"):

| Column | Type | Rule |
|---|---|---|
| id, public_id | | |
| name | label | partial UNIQUE `(f_unaccent(lower(name)))` |
| kind | code | `all` (built in, one per media kind: "Books", "Movies", "Series") or `manual` |
| media_kind | code | FK `media_kind` (book/movie/series) |
| position | integer | order in the shelf list |
| sort_key | code | `manual`, `title`, `author`, `added`, `finished`, `rating`, `published` |
| sort_desc | boolean | |
| cover_size | code | `s`, `m`, `l` |
| layout | code | `grid`, `list` |
| sort_locale | code | `und`, `da`, `it`, `en` (which `catalog.sort_*` the title/author sort uses) |
| timestamps, deleted_at | | |

**`library.shelf_item`**: `(shelf_id, item_id)` PK, `position integer > 0`,
`added_at`. **UNIQUE (shelf_id, position) DEFERRABLE INITIALLY DEFERRED**:
dragging a book from 40 to 3 is one `UPDATE … SET position = position + 1 WHERE
position BETWEEN 3 AND 39` plus one `UPDATE` of the moved book, valid at
commit. A trigger appends every new entry to its kind's `all` shelf, so the
main library also has a manual order. Smart (rule-based) shelves like CWA's
"magic shelves" can be added later as `kind = 'smart'` with a rule column; not
built now. The status filters (Read / To read / Reading) are filters, not
shelves.

### 4.4 Provenance and owner protection

`catalog.data_source` (lookup): `owner`, `yamtrack`, `cwa`, `apple`,
`openlibrary`, `google`, `wikidata`, `tmdb`, `kosync`, `derived` (computed by
the database, e.g. `sort_title`). Later: `llm` (for approved assistant
proposals), `dbc` (Danish libraries).

Every table with owner-visible values has `provenance jsonb NOT NULL DEFAULT
'{}'`. It maps column name → `{"src": <data_source>, "at": <timestamptz>,
"ref": <optional source reference>}`, for example:

```json
{"title": {"src": "yamtrack", "at": "2026-10-24T18:00:00Z", "ref": "app_item:6"},
 "pages": {"src": "openlibrary", "at": "…", "ref": "OL37746960M"},
 "rating": {"src": "owner", "at": "2026-11-02T20:11:04Z"}}
```

Every write declares its source in the transaction: `SET LOCAL ml.source =
'owner'` from the console, `'yamtrack'` / `'cwa'` / … from the importer,
`'apple'` / `'openlibrary'` from the fill, together with `ml.client` (who: `console:<tailscale
login>`, `importer`, `fill`). The trigger `catalog.provenance_guard()`, attached
to each such table with the list of tracked columns, then:

1. **refuses the write when `ml.source` is missing**, so nothing reaches the
   database without a declared source;
2. on INSERT, records the source for every non-null tracked column that the
   writer did not label explicitly (the importer labels per column);
3. on UPDATE, records the source for every changed column;
4. **raises an error when a non-owner source changes a column whose current
   provenance is `owner`**, or changes a `locked` item. "Never overwrite
   owner-entered values" is a database rule, so no future job can break it.
   The fill checks provenance first and skips such fields; reaching the error
   means a bug.
5. validates the provenance JSON itself (known columns, known sources).

Provenance lives in the row, not in a side table, so it changes atomically
with the value, needs no polymorphic FK, and the console reads it with the row
(a small "from Apple" hint next to a field).

### 4.5 Audit

**`audit.change`**: `id` (identity), `at timestamptz DEFAULT clock_timestamp()`,
`tx bigint` (transaction id), `table_name`, `row_key jsonb` (primary key),
`op ∈ I/U/D`, `old_row jsonb` (full row before), `new_values jsonb` (for
INSERT the full row; for UPDATE only the changed columns), `client text NOT
NULL` (`ml.client`), `source` (`ml.source`), `db_role name DEFAULT
session_user`, `reason text` (optional `ml.reason`, e.g. "importer run 3"),
`prev_hash bytea`, `hash bytea NOT NULL`.

- One generic AFTER trigger `audit.capture()` (`SECURITY DEFINER`, owned by
  `ml_owner`) on every `catalog.*` and `library.*` table. Application roles have
  **no** INSERT/UPDATE/DELETE on `audit.change`; only the trigger writes.
- **Append-only:** no role has UPDATE/DELETE/TRUNCATE; a BEFORE UPDATE/DELETE
  and a BEFORE TRUNCATE trigger raise errors even for the table owner. Only a
  superuser disabling triggers could get around this, and the hash chain would
  then show it.
- **Tamper-evident:** `hash = sha256(prev_hash || canonical row text)`, where
  the canonical text is the row's JSON without the hash columns. The chain is
  serialised with a transaction-level advisory lock, which is fine for one
  writer. `audit.verify(from_id)` recomputes the chain and returns the first
  broken id; the health check runs it incrementally.
- The console's detail sheet shows a book's history ("rating 8 → 9, 2026-10-19,
  yamtrack") from `audit.change` (the app role has SELECT).
- Imported Yamtrack history (`app_historicalbook` `~` rows) is inserted as
  audit rows with their original timestamps, client `import:yamtrack-history`.

### 4.6 Ingest

- **`ingest.run`**: `id`, `started_at`, `finished_at`, `tool`, `mode ∈
  dry_run/apply`, `sources jsonb` (each input file with path, sha256, mtime),
  `counts jsonb`, `report_path`, `status ∈ running/ok/failed`.
- **`ingest.source_record`**: `source` (e.g. `yamtrack.app_book`,
  `cwa.books`, `owner-covers`), `source_key`, `payload jsonb` (the full source
  row, including the custom columns and joined values), `payload_sha256`,
  `first_run_id`, `last_run_id`; UNIQUE `(source, source_key)`. **Nothing from
  a source is lost**, even fields not mapped to a column. Credentials never
  appear (the snapshot has them blanked, and `users_user` is not read).
- **`ingest.id_map`**: `(source, source_key, target_table)` → `target_id`. This
  makes the importer idempotent: a second run finds every target and changes
  only what differs.

### 4.7 Stats (Visuals)

Views in `stats`, owned by `ml_owner`. `ml_readonly` can read only these views,
not the base tables. Reviews are not exposed (Visuals does not need them).

- `stats.book_reading`: one row per live book reading: item public id, title,
  authors, state, year, month (null unless precision ≥ month), day (null unless
  precision = day), pages (the edition read, else the primary edition), rating,
  language read in, original language, genres.
- `stats.books_by_year`, `stats.books_by_month` (known months only, plus an
  "unknown month" count per year), `stats.pages_by_year`,
  `stats.rating_histogram` (bins of 1 point, 0–10), `stats.top_authors`,
  `stats.genres`, `stats.languages` (read in × original), `stats.activity_days`
  (days with a dated start, finish or progress, for the calendar),
  `stats.current` (open readings with progress), `stats.library_counts`
  (per status).

---

## 5. Integrity and operations

### 5.1 Constraint inventory

Within one row: NOT NULL on every required column; FKs on every reference; CHECKs
for ranges, formats, partial dates, exactly-one-owner (`num_nonnulls`), and
state rules. Domains for repeated types.

Across rows (database-enforced):

| Rule | Mechanism |
|---|---|
| subtype matches kind | composite FK `(item_id, kind)` + CHECK |
| one primary edition per book | partial unique index + deferred constraint trigger |
| one chosen cover per edition/item; chosen ⇒ stored locally | partial unique index + CHECK |
| one open reading per book | partial unique index |
| reading's edition belongs to the book | composite FK `(edition_id, item_id) → edition (id, work_id)` |
| status ⇔ readings | deferred constraint trigger (§4.3) |
| external id unique, valid, at the right level | UNIQUE + validation trigger |
| shelf order positions unique | deferrable UNIQUE |
| owner values not overwritten; locked items untouched | provenance trigger (§4.4) |
| every write declares a source | provenance trigger |
| audit append-only and chained | triggers + privileges + hash chain |

Every one of these gets a negative test in phase 1 (the bad write must fail).

### 5.2 Roles and privileges

| Role | Login | Rights |
|---|---|---|
| `ml_owner` | no | owns all schemas and objects |
| `ml_migrator` | yes | `GRANT ml_owner TO ml_migrator WITH INHERIT FALSE, SET TRUE` (PG 16+): no owner rights until `SET ROLE ml_owner`, which `migrate` does explicitly. Runs migrations and maintenance (purge) |
| `ml_app` | yes | USAGE on catalog/library/ingest/stats; SELECT/INSERT/UPDATE on catalog.*, library.*, ingest.*; DELETE only on link tables; SELECT on `audit.change`; no DDL, no TRUNCATE. `statement_timeout 10s`, `idle_in_transaction_session_timeout 60s`, `CONNECTION LIMIT 10` |
| `ml_readonly` | yes | USAGE + SELECT on `stats` only; `default_transaction_read_only = on`, `statement_timeout 5s`, `CONNECTION LIMIT 5` |
| `postgres` | yes (socket) | superuser for initdb, extensions, backups and restores inside the DB container only; never used by the app |

`REVOKE ALL ON DATABASE medialib FROM PUBLIC`; CONNECT granted to the three
login roles. `ALTER DEFAULT PRIVILEGES` so future tables get the same grants.
Passwords: SCRAM-SHA-256, generated on the server by `setup.sh` into
`<install dir>/secrets/` (0600) and passed as Docker secrets
(files, not environment variables). No secret value is ever in this repo.

`pg_hba.conf` (shipped): `local all postgres peer`; `host medialib
ml_app,ml_readonly,ml_migrator samenet scram-sha-256`; everything else `reject`.
No TLS between containers: the traffic stays on the internal Docker network on
one host. TLS is terminated by Tailscale Serve for the browser.

### 5.3 Database container

- `postgres:18.6-bookworm`, `initdb` args: `--locale-provider=builtin
  --builtin-locale=C.UTF-8 --encoding=UTF8 --data-checksums --wal-segsize=1`
  (1 MB WAL segments keep the archive small for a database this quiet).
- No `ports:`. Only the internal network `medialib` (the app reaches it by
  name).
- `security_opt: no-new-privileges`, `cap_drop: ALL` plus only what the official
  entrypoint needs (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`),
  `mem_limit: 512m`, `shm_size: 128m`, `pids_limit`, healthcheck `pg_isready`,
  Watchtower label off.
- Settings (command flags): `shared_buffers=128MB`, `work_mem=8MB`,
  `wal_level=replica`, `archive_mode=on`, `archive_command` = a small script
  that gzips each segment into `/archive` and refuses to overwrite a different
  file, `archive_timeout=3600`, `summarize_wal=on`, `wal_compression=zstd`,
  `password_encryption=scram-sha-256`, `log_min_duration_statement=500ms`,
  `log_lock_waits=on`, `timezone=UTC`.
- Data: bind mounts under the install directory (`pgdata`,
  `wal-archive`, `backups`), inside the host's backup paths.

### 5.4 Migrations

`migrations/NNNN_name.sql`, forward-only, applied in order by `medialib
migrate` as `ml_migrator` → `SET ROLE ml_owner`. Each file runs in **one
transaction** together with its row in `meta.schema_migration (version, name,
sha256, applied_at, applied_by, duration_ms)`. A changed checksum of an
already-applied file stops the run. `migrate --check` lists pending and drifted
files without applying anything. Extensions (`unaccent`, `pg_trgm`) are trusted
since PG 13, so the database owner creates them in 0001 with no superuser step.
Undoing a release = restore (§5.6) or a new migration; the staging deployment
itself rolls back by stopping its containers.

### 5.5 Backups (fits a file-level backup)

Four layers, cheapest first:

1. **Row-level undo**: `audit.change` has every old value; the console can
   restore a field or a deleted book without touching backups.
2. **Logical dump** 4× a day (cron), a few minutes before each run of the
   host's file backup:
   `pg_dump -Fc medialib` + `pg_dumpall --globals-only --no-role-passwords`
   → `backups/dumps/`, keep 14 days. Portable across major versions.
3. **PITR**: WAL archive (continuous, gzip, `archive_timeout` 1 h) plus a nightly
   base backup at 02:50 (`pg_basebackup -Ft -z -X fetch --checkpoint=fast
   --manifest-checksums=SHA256`, then `pg_verifybackup`), keep 7; WAL older than
   the oldest kept base backup is pruned with `pg_archivecleanup`. Restores to
   any second within 7 days, as long as the disk is intact.
4. **The host's file backup** (restic, Backrest, …) snapshots the install
   directory (dumps, base backups, WAL archive), ideally to a second disk and
   off site. If the server's disk dies, the worst case is losing the time since
   the last snapshot.

At this size the dump is a few MB and a base backup tens of MB. PG 17
incremental backups are not worth the complexity here.

### 5.6 Tested restore

`ops/restore-test.sh` (weekly cron, e.g. Sunday 04:00 after the backups; also run
on demand):

1. starts a throwaway `postgres:18.6-bookworm` container with no network on a
   temp directory;
2. **PITR path**: unpacks the latest base backup, replays the archived WAL to
   the end, starts, then runs `pg_amcheck --heapallindexed`, `audit.verify()`
   and row counts per table;
3. **dump path**: restores the latest `pg_dump` into a fresh database and
   compares row counts and the audit chain head with the PITR result;
4. writes `backups/restore-test.json` (ok/failed, timings, counts), which the
   health check reads; failure → ntfy.

Both paths are also run in this container during phase 1 against the
development cluster, and the result goes in the phase 1 report.

### 5.7 Upgrades

- **Minor** (18.6 → 18.7): change the tag, `docker compose up -d db`, done
  (same on-disk format).
- **Major** (18 → 19, later): `ops/major-upgrade.sh`: final dump, new cluster
  on the new image, restore, verify, swap. This is the same procedure as the
  weekly restore test, so it is already rehearsed.
- **Base OS / ICU** (bookworm → trixie, ICU 72 → 76): after the switch,
  `REINDEX` the indexes on `catalog.sort_*` collations and run `ALTER COLLATION …
  REFRESH VERSION`. The health check reports the drift until this is done.
  Key and unique indexes use the builtin provider and are unaffected.

### 5.8 Health check

- Docker: DB `pg_isready`; app `GET /healthz` (DB ping + migration version
  matches the code).
- `GET /api/health` (shown in the console footer; can feed Uptime Kuma):
  `data_checksums = on`, checksum failures (`pg_stat_database`), archiver
  (`pg_stat_archiver`: last archived < 2 h, no failure since), last dump < 7 h,
  last base backup < 26 h, last restore test ok and < 8 days, collation version
  drift, `audit.verify` since the last verified id, pending migrations.

---

## 6. Import mapping

Rules (from the brief and the book-library decisions):

1. **Owner covers beat everything.**
2. **Yamtrack wins** for title, cover, edition, status, rating, review and dates.
3. **CWA fills** authors, IDs, pages, genre and description where Yamtrack has
   none. Since Yamtrack stores no authors (only the notes string, written from
   CWA on 2026-10-18), **CWA's current authors win** over the notes "By:", which
   is older and has typos. Differences are listed in the report.
4. Open Library data for **Yamtrack's chosen edition** (from `search-cache.json`,
   by `media_id`) is Yamtrack's metadata: publish date, ISBNs, and pages where
   CWA has none (the owner's CWA/Excel page counts win, D26). Other candidates
   in the cache are ignored.
5. Nothing is guessed: an unclear value is left empty and listed in the report.
6. The tts-ereader working copy *Storia dell'assedio di Lisbona* is imported
   with `locked = true`, as it is in CWA.

### 6.1 What becomes what

| Source | Becomes | Count (snapshot) |
|---|---|---|
| Yamtrack `app_book` + its `app_item` | `media_item` (book) + `book` + primary `edition` + `library.entry` | 143 |
| …Completed | + one `consumption` (state completed, end date year precision) | 91 |
| CWA records linked to a Yamtrack item that has no `app_book` (I corpi, Summary of Sapiens), or to an item missing from Yamtrack (Wealth of Nations, Workbook for Dopamine Nation, Storia dell'assedio di Lisbona) | item + edition + file + `library.entry`, status **planned** (owner) | 5 |
| Yamtrack items never logged, not linked to CWA: *The Dream Within*, *Kitchen*, *Leonardo Del Vecchio*, *La democrazia*, *On the Calculation of Volume II*, *… III*, the movie *Perfect Days*, the series *Severance* | item (+ edition for books; `movie` / `series` subtype) + `library.entry`, status **planned** (owner) | 8 |
| Yamtrack items the entry was moved off (older editions: *1984*, *Sophie's world*, *Harry Potter og de vises stein*, *La Metamorfosis*, *Così Parlò Zarathustra*, *Kyacchā in za rai*, *Il Vangelo…*, *What Do You Care…*, *India's Founding Moment*) | a **non-primary edition** of the book that holds the entry (its Open Library id, `yamtrack_item` id and cover kept as an alternative); no second entry | 9 |
| CWA EPUBs of imported books | `edition_file` (storage `cwa`) | 18 |
| CWA `ignore` records (two box sets, test file) | not imported; listed (box sets: to be moved in later, owner) | 3 |

A superseded edition is recognised by Yamtrack's ids: on 2026-10-18 every
`app_book` row was created together with an `app_item` of the same id; where
`app_book N` now points at a newer item and item `N` has no entry, item `N` is
the edition the entry was moved off (the titles confirm all 9).

Expected totals: 154 books + 1 movie + 1 series; 163 editions (154 primary);
156 entries, all with a status; 91 readings; 18 files; 156 chosen covers.

### 6.2 Field by field

**Yamtrack `app_item`** (logged items):

| Field | Target | Rule / note |
|---|---|---|
| `id` | `external_id` `yamtrack_item` (edition) | legacy key |
| `title` | `media_item.title`, `edition.title` | Yamtrack wins |
| `image` | chosen `cover` | `/covers/<f>` → owner file; `mzstatic.com` → Apple (download 1400 px once); `covers.openlibrary.org` → Open Library (download `-L`); placeholders (TMDB glyph, `/id/-1-`) → none, fall back to CWA's `cover.jpg` (source `cwa`) |
| `source` + `media_id` | `external_id` `openlibrary_edition` or `yamtrack_manual` | |
| `media_type` | `media_item.kind` | book only in this snapshot |
| `season_number`, `episode_number` | — | always null for books |

**Yamtrack `app_book`**:

| Field | Target | Rule / note |
|---|---|---|
| `status` | `entry.status` | Planning → planned, In progress → in_progress, Completed → completed, Paused → paused, Dropped → dropped |
| `score` | `entry.rating` | exact (0–10, one decimal). CWA rating dropped (stale, 8 differ) |
| `notes` → review (after ` — `) | `entry.review` | Calibre HTML (`<p>`) converted to plain paragraphs; raw kept in `source_record` |
| `notes` → `By:` | contributions (author) only when CWA has no authors | CWA otherwise (rule 3); 21 differences reported |
| `notes` → `Read in:` | `consumption.language` of the reading; `edition.language` (and `title_language`) if CWA has none | names → ISO 639-3 (typos "Portoguese"/"Portouguese" → por) |
| `notes` → `Original language:` | `media_item.original_language` | same mapping; "Greek" → `ell` (reported for the Plato titles, likely `grc`) |
| `notes` → `Genre:` | `library.item_tag` (genre), split on `,` / `;`, order kept | identical to CWA `#genre` for all 141 |
| `notes` → `Pages:` | `edition.pages` when CWA has no `#pages` (the owner's numbers win, D26) | = CWA `#pages` (Excel) |
| `notes` → `Ebook:` | dropped | informational ("2-book set in CWA") |
| `start_date` | `consumption.started_on` | all null in the snapshot |
| `end_date` | `consumption.ended_on` + precision `year` | `YYYY-12-31 12:00` and the migration date `2026-10-18` both mean "read in YYYY" (= CWA `#read_year`, 91/91) |
| `progress` | completed: `edition.pages` fallback (it is the edition's page count); in progress: `progress_pages` | |
| `created_at` | `entry.added_at` candidate | `added_at = min(Yamtrack created_at, CWA timestamp)`; both are migration dates, approximate |
| `progressed_at` | dropped | equals `created_at` (migration) |
| `user_id` | dropped | single owner |

**Yamtrack `app_historicalbook`**: `+` rows equal the created row (nothing
new). The 8 `~` rows (score edits) become `audit.change` rows with their
original time, so the history shows "8 → 9". Status transitions would become
start/end dates; there are none in the snapshot, but the importer handles them.

**Yamtrack, other tables**: `users_user` not read (credentials; single owner);
`events_event` (release calendar, regenerated by Yamtrack) dropped; lists empty;
Django/Celery tables dropped.

**CWA `metadata.db`** (linked records):

| Field | Target | Rule / note |
|---|---|---|
| `id` | `external_id` `cwa_book` (edition) | legacy key; both *Meditations* records map to one edition |
| `uuid` | `external_id` `calibre_uuid` | |
| `title` | only for the 3 items absent from Yamtrack | Yamtrack wins otherwise |
| authors (`books_authors_link`, `authors.name`, `authors.sort`) | `contribution` (author, work level, link order) + `person.sort_name` | names joined with `\|` in one record are split ("Ken Blanchard\| Spencer Johnson") |
| `languages` | `edition.language` | |
| `publishers` | `edition.publisher` | 20 records |
| `pubdate` | `0101-01-01` → none; `from-excel` records (`YYYY-01-02`) → `media_item.first_published` (year: the Excel "Released" year); records with a file → `edition.published_on` if Open Library has none | |
| `comments` | `edition.description` | HTML → text; 18 records |
| `identifiers` | `isbn`/`isbn10`/`isbn13` → `isbn13` (converted, checked); `openlibrary` → checked against Yamtrack's (it is the mirror's copy); `google` → `google_books`; `amazon`, `mobi-asin` → `amazon_asin`; `ibdb` → `ibdb`; `comicvine`, `url` → dropped (only on the locked/test records) | invalid check digits reported, not stored |
| `tags` | `library.tag` (tag), source cwa | `from-excel` dropped (import marker; kept in `source_record`) |
| `ratings` | dropped | stale (rule 2) |
| `#read_status`, `#read_year`, `#notes`, `#genre`, `#pages` | validation only | equal to Yamtrack's values (§0); for records without a Yamtrack entry they would be imported, and none of the five have them |
| `series`, `series_index` | dropped | only on the locked and test records |
| `timestamp` | `edition_file.added_at`, `entry.added_at` candidate | |
| `path` + `data` (`name`, `format`, `uncompressed_size`) | `edition_file` (rel_path, format, size; sha256 + partial MD5 computed) | |
| `cover.jpg` | fallback chosen cover (source `cwa`) | it is CWA's copy of the Yamtrack cover |
| `book_format_checksums` | validation of `kosync_md5`; the full history stays in `source_record` | first import: 1 file matches CWA's latest checksum, 17 match an older one (CWA recorded a newer checksum than the file in the snapshot); kosync later must accept the older ids too |
| `books_pages_link`, `last_modified`, `sort`, annotations | dropped | Calibre's own scan (0/−2), bookkeeping, empty |

**CWA `app.db`**: `book_read_link` (derived from `#read_status`),
`kosync_progress` (0 rows), shelves (0), CWA's default `magic_shelf` rules,
`downloads`, `kobo_*` (empty values), `thumbnail` cache: dropped and counted in
the report.

**`library-sync-state/`**: `cwa-links.json` is the join and the `ignore` list
(`mirrored_cover` is used as a check; `pct` is 0 everywhere).
`apple-covers-applied-20261021.json`: each `old` cover becomes a non-chosen
alternative. `apple-keep.json`: those items keep their non-Apple cover (already
true in Yamtrack); the Apple hit from `apple-cache.json` becomes a non-chosen
alternative with note "owner preferred the previous cover". `apple-cache.json`
also seeds the phase 2 cache. `search-cache.json`: rule 4.
`cwa-migration.json`, `state.json`: dropped (bookkeeping).

**`owner-covers/`**: chosen cover, source `owner`, matched by the file name in
the Yamtrack image URL (all 11).

### 6.3 Importer behaviour

- `medialib import --snapshot DIR` is **dry-run by default**: everything runs in
  one transaction that is rolled back at the end, so the dry run exercises every
  constraint. `--apply` commits. Covers are downloaded into the
  content-addressed store in both modes (idempotent by sha256); the dry run only
  never references them.
- **Idempotent** through `ingest.id_map`: a second `--apply` reports
  "0 created, 0 changed".
- **Completeness check** (fails the run if violated): every Yamtrack
  `app_book` → an entry; every linked CWA record → an edition with that
  `cwa_book` id; every non-empty source field → imported, or on the dropped list
  above (the report shows a count per field and source, with any field that is
  neither).
- **Report** `out/import-<run>.md` + `.json`: counts source vs target, unmatched
  lists (§6.1), conflicts (authors, ratings, pages, ISBNs), files whose partial
  MD5 differs from CWA's, covers that could not be downloaded, and suggested
  owner checks (e.g. a translator listed as author: "José Saramago & R. Desti").

---

## 7. Metadata fill for new books (phase 2)

The easiest path, behind a small interface:

```python
class Provider:
    name: str                       # 'apple', 'openlibrary', …
    def search(self, title: str, author: str | None) -> list[Candidate]: ...
    def by_isbn(self, isbn13: str) -> Candidate | None: ...
# Candidate: provider, external ids, title, authors, language, published,
#            pages, publisher, description, cover_urls, raw payload
```

- **Apple** (iTunes Search API, no key, stores dk/it/us, `media=ebook`): cover
  (1400 px), title, author, release date, description, trackId. The matching
  rules of `apple_covers.py` are reused (main title + one author must agree; no
  summaries/study guides; numbers must match).
- **Open Library** by ISBN (`/isbn/{isbn}.json` → edition, then
  `/works/{id}.json`): pages, publisher, publish date, languages, work id,
  first publish date, description.
- Stubs only: Google Books, Wikidata, DBC (Danish libraries).
- The flow in the console: type a title/author or ISBN → candidates from both →
  pick one → one transaction creates the work, edition, IDs, cover and an entry
  (status of your choice). Each value carries its provider in provenance.
  Later "refresh" fills only empty, non-owner fields; the database rejects
  anything else (§4.4).
- Provider answers are cached on disk for 30 days (`media/cache/http`), so a
  lookup is never repeated unnecessarily; the candidates of every book that
  is added are kept in `ingest.source_record` (`fill.apple`,
  `fill.openlibrary`). Requests are spaced (Apple allows ~20/min).
- Implemented (phase 2): `medialib/fill/` with Apple Books and Open Library
  (search, ISBN, edition id), stubs for Google Books, Wikidata and DBC; the
  console's "+ Add book" and "Fill missing" use it.

---

## 8. Stack

- **Python 3.13** in the image (`python:3.13-slim-bookworm`); the code also runs
  on 3.11 (this container).
- **The stdlib-only rule is book-library's** (jobs that run on the host and in
  other projects' containers). This project runs in its own container and
  needs one exception: **a PostgreSQL driver**, since the standard library has
  none. Runtime dependencies, pinned with hashes (`requirements.lock`, `pip
  install --require-hashes`):

  | Package | Version | Why |
  |---|---|---|
  | `psycopg[binary]` | 3.3.6 | driver (binary wheel bundles libpq) |
  | `psycopg-pool` | 3.3.3 | connection pools (app, visuals) |
  | `Pillow` | 12.3.0 | validate uploaded/downloaded images (real JPEG/PNG, size), make shelf thumbnails (1400 px Apple covers are too heavy for a phone grid). 13.0.0 is eight days old, so not yet |

  Development only: `pytest 9.1.1`, `playwright 1.63.0` (headless UI checks, if
  Chromium runs in this container).
- **No web framework.** `http.server.ThreadingHTTPServer` plus a small router,
  like agent-console: JSON API, static page, cover files. It sits behind Tailscale
  Serve on the tailnet only, with a single user. Requests are size-limited,
  JSON-only writes, with Origin and `Tailscale-User-Login` checks (writes only
  from the owner's login, `OWNER_LOGIN`, as in agent-console).
- **Front end:** one `index.html`, vanilla JS, no build step, the agent-console
  tokens (`--bg`, `--panel`, `--accent: #2f6f5b`…, dark/light, phone layout),
  charts as inline SVG.

---

## 9. Console (phase 3), as designed

- Left pane: segmented switch **Database | Visuals**; under it **Books**,
  **Movies**, **Series** (the last two show an empty state).
- **Database → Books**: shelf list ("Books" = all, then the owner's
  collections, drag to reorder); toolbar with sort (manual, title, author, date
  added, date read, rating, published), cover size (S/M/L), grid/list, search,
  and filter chips **All · Read · To read · Reading**. Cover grid in Apple Books
  proportions; drag to reorder when the sort is manual (pointer events, works on
  touch). A "+" adds books to a shelf. Clicking a cover opens the **detail
  sheet**: large cover (and alternatives), title, authors, edition (language,
  publisher, year, pages, ISBN), status control, **rating** (0–10, as in
  Yamtrack; owner 2026-10-24), **review** (textarea, saved explicitly), readings list (add/edit,
  dates with precision), shelves, genres/tags, IDs as links, provenance hints, and
  the change history. All writes go through `ml_app` and are audited.
- **Visuals → Books** (role `ml_readonly`): tiles (read this year, pages this
  year, average rating, in progress), books per year (bars, stacked by
  language read in), pages per year, per month for the months known (a note
  says the imported readings have years only), rating distribution, top
  authors, genres, languages (read in vs original), reading calendar (dated
  activity only, which fills as new readings are logged), current progress.

---

## 10. Deploy (phase 4), as designed

- An install directory with `docker-compose.yml` (db + app + one-off `tools`),
  secrets as files, `.env` settings, the `ops/` scripts (dump, base backup,
  WAL archive, restore test) on cron, and Tailscale Serve publishing
  `127.0.0.1:8099` on a tailnet-only HTTPS port. Steps: `docs/INSTALL.md`.
- It reads a copy of the Yamtrack/CWA data and writes nothing to them, so it
  can run next to them and be removed with `docker compose down`,
  `tailscale serve --https=<port> off` and deleting the folder.

---

## 11. Later, and why nothing here blocks it

- **OPDS + kosync (replacing CWA):** done in 0.2.0, §13.1. CWA's checksum
  history became `catalog.document_alias`; files move to the store by copy +
  update (`adopt-files`).
- **LLM "fix issues" assistant:** a new `ingest.proposal` table (target row,
  column diffs as JSON, rationale, status pending/approved/rejected). Applying
  an approved proposal is a normal write with `ml.source = 'llm'`, so the owner
  protection and the audit apply as they do to any other source.
- **Movies/series via TMDB:** the subtypes, `tmdb_movie`/`tmdb_tv` providers,
  roles (director, creator, cast) and screen status labels exist; per-episode
  watches add an `episode_id` to `consumption` when needed.
- **Box sets / omnibus files** (owner: to be moved in later): an omnibus
  edition plus `catalog.edition_content (edition_id, work_id, position)`
  listing the works it contains; the box-set EPUB then hangs off that edition.
- **Feeding new data in** (Joplin Reviews, Readest progress): done in 0.2.0,
  §13. Switching off the Yamtrack feeds and CWA's sync is the owner's call.
---

## 12. Choices made with the owner

1. Never-logged Yamtrack items become **to read / to watch** (older editions
   are kept as extra editions of the logged book, not as duplicate books).
2. Ebooks without a Yamtrack status: **to read**, the default status.
3. Read dates: when no source has a month or day, the console makes reading
   dates easy to edit by hand.
4. Rating: **0–10** in the console (Yamtrack's scale, lossless).
5. **Tailnet only**; writes only from the owner's Tailscale login.
6. Box sets: not imported yet; they will become an omnibus edition that
   contains several works (§11).

---

## 13. Reading apps and feeds (0.2.0, 2026-10-25)

The owner reads in Readest (iPhone, Mac) and writes reviews as Joplin notes.
Both now reach this database directly; the console's **Sync** section shows
them.

### 13.1 Readest: OPDS catalog + KOReader sync

| Path | What | Login |
|---|---|---|
| `/opds` | OPDS 1.2 (Atom) navigation: Reading, To read, Recently added, Read, All books; OpenSearch at `/opds/search.xml` | HTTP Basic, reader login |
| `/opds/books?shelf=…&page=N` | acquisition feeds, 50 per page (`rel="next"`): title, authors, language, publisher, issued, genres, description, cover + thumbnail, one acquisition link per stored file | same |
| `/opds/files/<file uuid>` | the EPUB (`HEAD` answered; `Content-Disposition` names it) | same |
| `/kosync/users/auth`, `/users/create`, `/syncs/progress[/<doc>]` | the KOReader sync protocol as Readest speaks it | `x-auth-user` + `x-auth-key` (md5 of the password), or Basic |

- **Login (D30).** Reading apps can't do the console's unlock, so they get a
  user name and a generated password (`secrets/reader.pw`), never the console
  password: KOReader sync sends md5(password) on every request. On top, the
  request must carry the owner's Tailscale login, like console writes. Ten
  minutes' lockout after 20 wrong logins. `/users/create` answers 201 only to
  the configured login (Readest "registers" when a first login fails), 402
  otherwise. These paths skip the console's Origin and `X-Console-Action`
  checks: Readest sends `Origin: tauri://localhost` through its native HTTP
  client, and there is no cookie to protect.
- **Which book (D31).** A document id is the KOReader partial MD5 of the
  reader's copy. It names a book through `edition_file.kosync_md5` or
  `catalog.document_alias`: CWA's whole checksum history per file (Readest's
  copies came from CWA, which rewrote metadata inside the EPUBs, so they
  differ from the stored bytes), and links the owner makes in the console for
  copies from elsewhere. Every position is kept in `sync.kosync_position`, named
  or not, so devices sync either way; Readest's optional metadata (title,
  authors) is stored to show unnamed ones.
- **What a position does (D32).** Same rules as the earlier CWA → Yamtrack job:
  To read (or Abandoned) and a move of 1 % or more → Reading, a new reading
  started today; 99 % or more → Read, finished today; after the end was seen
  here, starting over below 25 % → a re-read; in between, the open reading's
  `progress_pct` (and pages, from the edition's page count) follows in steps of
  one point. A book read before, with a mid-way position, stays Read. These
  are the owner's own reading, so they are written as the owner (client
  `kosync:<device>`) and audited; the position table itself is device state
  and not audited.
- **Files (D35).** The catalog lists books with a file in the store.
  `medialib adopt-files SNAPSHOT` copies the imported CWA files there
  (verified by sha256; locked books skipped), and a re-import keeps them there.

### 13.2 Joplin review notes

`python -m medialib joplin --tar -` reads the Joplin sync folder as a tar
stream on stdin (`ops/joplin-sync.sh` pipes it from wherever Joplin syncs to
into the console container; D34), takes the live notes of the **Reviews**
notebook and its sub-notebooks (Books, Audiobooks, Movies, TV-Series;
templates skipped), and writes them as the owner, client `joplin:<note id>`
(D33). The note format is the earlier Joplin → Yamtrack job's
(`medialib/feeds/notes.py`), plus an `isbn:` line; dates keep their precision.

- **Which item:** the one the note matched before (same title and kind); an
  explicit `isbn:` / `openlibrary:` / `tmdb:` line; an item in the library
  whose title fits and whose author (or, for films, year) agrees, or the very
  same title when the note names nobody; else, for books, Apple Books + Open
  Library through the metadata fill, where title and author must agree, and
  the book is added. Films and series the library lacks are added from the
  note itself (title, year, director, original language). Anything else is
  **unclear**: listed in the console with the closest candidates, never
  guessed, and tried again (library only) on every run, so adding the book in
  the console resolves it.
- **What it writes:** rating (x/5, x/10 → 0–10), review (the comment),
  details the library lacks (original language, pages, author, genre),
  collections (`lists:`; one the note no longer names is left again), and the
  reading: an edited note updates the reading it wrote; a "finished" note
  closes the open reading (e.g. one Readest started) instead of adding one,
  and adopts the reading it describes (same period, or days less than a month
  apart: Readest's "finished today" gets the note's date). A note without a
  date describes the latest reading already there.
- **Runs** are `ingest.run` rows (tool `joplin`, a `summary`); the health check
  wants the last one ok and under 3 hours old once the feed exists.

---

## 14. Trash, categories, metadata fill v2, reading in the browser (0.3.0, 2026-10-25)

### 14.1 Trash

Deleting a book in the console moves it to the **Trash** (the soft delete
every table has: `media_item.deleted_at` + `entry.deleted_at`): hidden from
the shelf, the statistics, OPDS, kosync and the Joplin feed, restorable for
**30 days** (D36). Then `medialib purge-trash` (daily cron) deletes it for good
through `library.purge_item()`, a `SECURITY DEFINER` function, because the app
role has no hard `DELETE`. It removes the readings, shelf places (renumbered),
tags, covers, document aliases, files, contributions, ids, editions and the
item, plus Readest positions of its documents. It returns the media files no
other row uses, which the caller deletes from disk with their thumbnails.
"Delete now" in the Trash does the same at once. `audit.change` keeps the
history (op `D` rows with the old values), and `ingest.id_map` keeps the
source ids, so **a re-import does not bring a deleted book back** (reported
as "deleted by you"). A Joplin note whose book was deleted for good is set
aside (`state = 'deleted'`) until the note changes. Adding a book whose ISBN
is in the Trash says so instead of creating a twin.

### 14.2 Categories

Genres get a group above them (migration 0013, `library.tag.parent_id`, two
levels enforced by a trigger). **Fiction**: Novel, Historical fiction, Crime &
mystery, Science fiction, Fantasy, Horror, Short stories, Drama & poetry,
Graphic novels, Satire & humour. **Non-fiction**: History, Biography & memoir,
Philosophy, Psychology & self-help, Science & nature, Economics & business,
Politics & society, Essays & criticism, Religion & spirituality, Art &
culture, Travel. A book carries one or two categories, or a group alone.

`medialib categorize` (once, from `setup.sh`; `medialib/categories.py`, D37)
moves every book onto the tree:
1. **Your labels decide** when they can ("Novel | Sci-Fi" → Science fiction;
   "Fiction | Philosophy" → Novel, philosophy being a theme in a novel).
2. Where they say only "Fiction"/"Non-fiction", nothing, or something broad
   ("Social Science", "Strategy"), **provider subjects** decide: Apple Books
   genres (Italian and Danish store names included), Open Library subjects,
   and Google Books categories when a key is set. Each source's first genre
   weighs most, and a specific genre beats "Novel" only with real support.
3. Labels that are no category (Open Library's "Brides", "Suicide victims")
   become plain tags. Labels no book uses any more leave the list.
Genres you set in the console are never touched. The run writes a report
(`out/categorize-*.md`: before, after and why, per book). New books get
their category the same way when added (search or EPUB) or through **Fill
missing**. The console shows the tree in the left pane and as a picker on the
toolbar, and the editor offers it grouped.

### 14.3 Metadata fill v2

- **Google Books** provider (D38). The keyless API answers 429 (shared quota
  0), so it stays off unless `secrets/google-books.key` holds a key; a
  failing provider never breaks a lookup. The cache never stores the key.
- **Title-only search**: the same edition from several providers or stores
  is shown once ("Apple + Open Library"), with at most three editions per
  book. Summary products are dropped by title ("Summarized for Busy People")
  and by publisher-like author ("Speedy Reads", "Turbo-Learning"). With no
  author, the book most people mean comes first (Open Library reading logs
  and editions, Apple ratings).
- **Add from an EPUB** (D39): `medialib/epubmeta.py` reads the OPF safely
  (in memory, entity-free, size caps, a repair pass for broken XML): title,
  authors and roles, language, ISBNs (also the print ISBN in `dc:source`),
  publisher, date, description, subjects, series and the cover. The book is
  looked up by ISBN, then by title and author. You pick a match, or add it
  "as the file says"; the file is attached, the EPUB's cover is used when the
  sources have none, and its subjects give the category.

### 14.4 Reading and finding books

- **Read** (D40): `/read?file=<id>` is an in-browser EPUB reader
  (`web/reader.html`, `web/reader.js`, foliate-js 78914ae vendored with
  zip.js; MIT/BSD). It has pages, a table of contents and font size, keeps the
  position per file in the browser, and works in light and dark. Its own
  CSP keeps a book's scripts from running: `script-src 'self'` only, with
  sections in blob: iframes that inherit it, and `frame-ancestors 'self'`,
  which Safari needs. **Download** still serves the file; on iPhone and Mac it
  opens in Books.
- **Find** (D41, D43): a book with no file shows a search link, set by
  `BOOK_FIND_URL` (`{q}` = title + first author, sent as a search form sends
  it, spaces as `+`) and `BOOK_FIND_LABEL`. It is empty by default, so the
  public code links nowhere until you configure it. Search sites move
  domains, so the owner can change the address from the book's panel
  (**Change address**, unlocked): it is kept in `state/find-link.json` and
  wins over `.env` until **Back to the server's**. Only `https://` addresses
  with `{q}` once, outside the host name, are accepted.

### 14.5 Visuals: My reading | The library

Visuals gets a switch. **My reading** is what was there: per year and month,
pages, ratings, the calendar. **The library** (`stats.library_book`, D42)
counts the books themselves, all of them, the read ones or the unread ones:
categories, original language, authors and release decade (first
published, else the edition's year).

### 14.6 The bookcase (0.3.1, whole page 0.3.2)

After the old iBooks, lighter (D44). The whole page is wood (light wood by
day, walnut in dark mode; the grain is SVG noise in a `data:` image, nothing
downloaded): the header is a wooden top rail, the left pane sits on the wood,
and panels, cards, the list and the forms are paper on it. The rest of the
palette is warm to match (paper and ink instead of grey-green; the green
accent stays). Each row of covers stands on a board that runs from the left
pane to the window edge (to the info panel when it is open);
`layoutShelves()` places the boards under the rows it measures, plus empty
ones down to the bottom of the case, and redoes it when the grid changes
size. The covers keep their spacing (closer than in iBooks), with a spine
shadow on the left edge. The left pane can be hidden with the button at the
top left (remembered per browser).

Pointing at a cover lifts it off the board, a little larger, with a glint.
Clicking one opens it: a copy of the cover flies to the info panel (a
transform, so the compositor runs it), swings open on its spine, and the
panel shows through as the page fades, about 1.6 s in all (`OPEN_MS`); the
book's place on the shelf stays empty until the copy is gone. With "Reduce
motion" the panel just opens.

## 15. Movies and series (0.6.0, 2026-10-25)

### 15.1 Schema (migration 0014)

- A **watch** is a `library.consumption` row, as a reading is (D22). A watch of
  a series can be of one **season**: `season_id`, with the series as `item_id`;
  the composite FK `(season_id, item_id) → catalog.season (item_id, series_id)`
  makes a season of another series impossible. `episodes` (only with a season)
  counts the episodes seen in that watch; `platform` (FK `library.platform`:
  Netflix, Prime Video, HBO Max, Disney+, Apple TV+, YouTube / Google Play,
  Viaplay, DR TV, TV 2 Play, cinema, TV, disc, other) says where. The
  provenance guard tracks the three new columns.
- One card per series (D45). The series' status follows its latest season
  watch: the status rules of §4.3 hold as they are (Watched needs a finished
  watch, Abandoned an abandoned one).
- `catalog.movie` gains `origin_countries`; `catalog.series` gains
  `origin_countries`, `season_count`, `episode_count`, `episode_runtime_min`;
  `catalog.season` gains `episode_count`. All three tables get a `provenance`
  column with the guard, so every value records where it came from and TMDB
  cannot change what the owner set (§4.4). Countries are ISO 3166-1 codes
  (`catalog.countries_ok`).
- New data sources: `netflix`, `prime_video`, `google_play`.

### 15.2 TMDB (D46)

`medialib/fill/tmdb.py` searches and reads TMDB's details (with credits and
external ids) through the same cached fetcher as the book providers; the key
is `secrets/tmdb.key` (empty = TMDB off: "+ Add movie / series" and Fill
missing for movies and series are hidden). `medialib/screen.py` turns the
details into values and writes them, labelled `tmdb`:

| TMDB | becomes |
|---|---|
| title / name, original title, original language, overview | `media_item` title, original_title, original_language, description |
| release date / first air date | `movie.release_date` / `series.first_air_date`, and `media_item.first_published` (day) unless the owner set it |
| runtime; episode run time, number of seasons and episodes, last air date, status | `movie.runtime_min`; `series.*` |
| production countries / origin country | `origin_countries` |
| directors (movies), creators (series), the first 6 of the cast | contributions `director`, `creator`, `cast` |
| genres | genre tags: the book tree's category of that name, else one under "Film & TV" |
| seasons (number, name, air date, episodes, poster) | `catalog.season` rows (their media items titled with the season's name) |
| poster | the chosen cover when there is none; season posters for watched seasons |
| TMDB id, IMDb id, Wikidata id | `external_id` `tmdb_movie` / `tmdb_tv`, `imdb`, `wikidata` |

On an existing item only empty values are filled, never one labelled
`owner`. Finding the item: its TMDB id, else the same title (or original
title) of that kind, confirmed by the year when both know one (§13.2 makes
movies from Joplin notes this way, so an import finds them).

### 15.3 The watch import file (D47)

`python -m medialib import-watch FILE.json [--apply] [--offline] [--out DIR]`
reads a file made outside the app (from viewing-history exports and
watchlists; the TMDB details are in it, so the import needs no key):

```json
{"format": "medialib-watch", "version": 1,
 "items": [{"kind": "movie | series",
            "tmdb": { TMDB details with credits and external_ids, or absent },
            "title": "only when there are no TMDB details", "year": 1990,
            "status": "planned | in_progress | completed | paused | dropped",
            "rating": 0-10, "review": "text", "added": "YYYY-MM-DD",
            "watches": [{"season": 1, "state": "completed | dropped | in_progress | paused",
                         "started": "YYYY-MM-DD", "ended": "YYYY-MM-DD", "precision": "day | month | year",
                         "episodes": 10, "platform": "netflix", "source": "netflix | prime_video | google_play | owner"}],
            "tags": [{"name": "Netflix: liked", "source": "netflix"}],
            "collections": ["Prime Video watchlist"]}]}
```

Per item, in one transaction: find or create the movie/series (§15.2), create
the entry (or fill its empty rating and review; a status the owner set
stays), add each watch unless the same one is there (same item and season,
ending the same day; an undated one when a watch in that state exists), add
tags and collections. The ratings and reviews in the file are the owner's
(source `owner`); watches carry their own source. Nothing is written in a dry
run; the report goes to `<out>/import-watch-<time>.json`. A second run
changes nothing.

### 15.4 Console

- **Database → Movies / Series**: the same shelf, list, filters and panel as
  books. The list's Pages and Version columns become Runtime (Seasons) and
  Where; the Filter pane adds Where you watched, Country and Tags (Netflix
  thumbs, D48), and Released replaces Published. The panel shows the
  director or creators, cast, runtime or seasons and episodes, countries, the
  watches (season, episodes, where) and, for a series, its seasons with their
  posters and what you did with each. "+ Add movie / series" searches TMDB.
- **Visuals → Movies / Series**: the same page and page filter. My watching:
  watched this year (movies, or seasons), hours this year, average rating,
  movies/seasons or hours over time, ratings, most watched directors /
  creators, genres, where you watched, country, original language, the
  watching calendar. The library: categories, original language, country,
  directors / creators, release decade.
