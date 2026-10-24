"""Completeness rules: every field of every source row is either imported
(to a named target), used to check another value, or dropped for a stated
reason. A non-empty field with no rule fails the import (DESIGN.md §6.3)."""
import collections

IMPORTED, VALIDATED, DROPPED = "imported", "validated", "dropped"

RULES = {
    "yamtrack.app_item": {
        "id": (IMPORTED, "external_id yamtrack_item"),
        "media_type": (IMPORTED, "media_item.kind"),
        "title": (IMPORTED, "media_item.title, edition.title"),
        "image": (IMPORTED, "cover (chosen, or an alternative for a replaced edition)"),
        "season_number": (DROPPED, "always empty for books/movies/series"),
        "episode_number": (DROPPED, "always empty for books/movies/series"),
        "source": (IMPORTED, "which external id provider"),
        "media_id": (IMPORTED, "external_id openlibrary_edition / yamtrack_manual / tmdb_*"),
    },
    "yamtrack.app_book": {
        "id": (IMPORTED, "ingest.id_map key of the work, entry and reading"),
        "score": (IMPORTED, "library.entry.rating"),
        "progress": (IMPORTED, "edition.pages fallback (completed) / consumption.progress_pages (in progress)"),
        "status": (IMPORTED, "library.entry.status (+ consumption.state)"),
        "start_date": (IMPORTED, "consumption.started_on"),
        "end_date": (IMPORTED, "consumption.ended_on + precision"),
        "notes": (IMPORTED, "parsed: By/Read in/Original language/Genre/Pages -> columns, review -> entry.review"),
        "item_id": (IMPORTED, "join to app_item"),
        "user_id": (DROPPED, "single owner (D3)"),
        "progressed_at": (IMPORTED, "consumption.progress_at when in progress; else equals created_at"),
        "created_at": (IMPORTED, "entry.added_at / status_changed_at / rated_at"),
    },
    "yamtrack.app_historicalbook": {
        "id": (IMPORTED, "join to app_book"),
        "score": (IMPORTED, "audit history (rating changes)"),
        "progress": (IMPORTED, "audit history (if changed)"),
        "status": (IMPORTED, "audit history (if changed)"),
        "end_date": (IMPORTED, "audit history (if changed)"),
        "start_date": (IMPORTED, "audit history (if changed)"),
        "notes": (IMPORTED, "audit history (if changed)"),
        "history_id": (IMPORTED, "audit.change.reason reference"),
        "history_date": (IMPORTED, "audit.change.occurred_at"),
        "history_change_reason": (DROPPED, "always empty"),
        "history_type": (VALIDATED, "'+' = the created row (same values), '~' = a change"),
        "history_user_id": (DROPPED, "single owner (D3)"),
    },
    "cwa.books": {
        "id": (IMPORTED, "external_id cwa_book"),
        "title": (IMPORTED, "media_item/edition title only where Yamtrack has no item; else Yamtrack wins"),
        "sort": (DROPPED, "sort titles are computed per language (sort_title)"),
        "timestamp": (IMPORTED, "edition_file.added_at, entry.added_at"),
        "pubdate": (IMPORTED, "from-excel: media_item.first_published (year); with file: edition.published_on; 0101 = unknown"),
        "series_index": (DROPPED, "no book series (only the locked and test records have one)"),
        "author_sort": (DROPPED, "per-author sort taken from authors.sort"),
        "path": (IMPORTED, "edition_file.rel_path"),
        "uuid": (IMPORTED, "external_id calibre_uuid"),
        "has_cover": (IMPORTED, "cover.jpg as fallback cover"),
        "last_modified": (DROPPED, "Calibre bookkeeping"),
        "authors": (IMPORTED, "contribution (author) + person.sort_name"),
        "tags": (IMPORTED, "library.tag (tag); 'from-excel' dropped as an import marker"),
        "languages": (IMPORTED, "edition.language, media_item.title_language"),
        "publishers": (IMPORTED, "edition.publisher"),
        "series": (DROPPED, "only on the locked tts-ereader copy and the test file"),
        "identifiers": (IMPORTED, "external_id (isbn13, google_books, amazon_asin, ibdb); invalid or unknown types reported"),
        "rating": (DROPPED, "stale: Yamtrack's score wins (8 differ = the owner's 2026-10-19 edits)"),
        "comments": (IMPORTED, "edition.description (HTML -> text)"),
        "custom.read_status": (VALIDATED, "equals Yamtrack's status"),
        "custom.notes": (VALIDATED, "equals Yamtrack's review"),
        "custom.read_year": (VALIDATED, "equals Yamtrack's end year; decides year precision"),
        "custom.genre": (IMPORTED, "genre tags where Yamtrack's notes have none; else equal"),
        "custom.pages": (IMPORTED, "edition.pages fallback; else reported if different"),
        "data": (IMPORTED, "edition_file (format, size, sha256, kosync partial MD5)"),
        "checksums": (VALIDATED, "against the computed kosync MD5; full history kept in source_record"),
        "pages_link": (DROPPED, "Calibre's own page scan (0 / -2: not counted)"),
    },
    "sync.cwa_links": {
        "ignore": (IMPORTED, "records not imported (listed)"),
        "media": (IMPORTED, "the join CWA record -> Yamtrack item"),
        "mirrored_cover": (VALIDATED, "what CWA shows (the mirror's copy of Yamtrack's cover)"),
        "pct": (DROPPED, "CWA reading position, 0 for every record"),
        "status": (DROPPED, "CWA read flag, derived from #read_status"),
        "title": (VALIDATED, "CWA title at link time"),
        "unclear": (DROPPED, "flag of the ignore entries"),
        "cover_failed": (DROPPED, "mirror bookkeeping"),
    },
}

WHOLE_SOURCES = {
    "sync.apple_covers_applied": (IMPORTED, "'old' covers -> alternative covers; Apple covers the owner rejected -> alternatives"),
    "sync.apple_keep": (IMPORTED, "those books keep their non-Apple cover"),
    "sync.apple_cache": (DROPPED, "kept raw in ingest.source_record for the metadata fill"),
    "sync.search_cache": (IMPORTED, "Open Library data of Yamtrack's chosen editions (pages, dates, ISBNs, authors); the rest raw"),
    "sync.cwa_migration": (DROPPED, "bookkeeping of the 2026-10-18 migration (raw in source_record)"),
    "sync.state": (DROPPED, "empty"),
    "owner.covers": (IMPORTED, "chosen covers, source owner"),
}


def empty(v):
    return v is None or v == "" or v == [] or v == {}


def check(snap):
    """-> (table of rules with counts, list of unclassified fields)"""
    rows = {
        "yamtrack.app_item": list(snap.yt_items.values()),
        "yamtrack.app_book": list(snap.yt_books.values()),
        "yamtrack.app_historicalbook": snap.yt_history,
        "cwa.books": [], "sync.cwa_links": list(snap.links.values()),
    }
    for r in snap.cwa.values():
        flat = {k: v for k, v in r.items() if k != "custom"}
        flat.update({"custom." + k: v for k, v in r["custom"].items()})
        rows["cwa.books"].append(flat)
    counts = collections.Counter()
    unclassified = collections.Counter()
    for source, rs in rows.items():
        for r in rs:
            for field, value in r.items():
                if empty(value):
                    continue
                if field in RULES[source]:
                    counts[(source, field)] += 1
                else:
                    unclassified[(source, field)] += 1
    table = []
    for source, fields in RULES.items():
        for field, (kind, target) in fields.items():
            table.append({"source": source, "field": field, "rule": kind, "target": target,
                          "non_empty": counts.get((source, field), 0)})
    for source, (kind, target) in WHOLE_SOURCES.items():
        table.append({"source": source, "field": "*", "rule": kind, "target": target, "non_empty": None})
    for name, n in snap.cwa_app_counts.items():
        if n:
            table.append({"source": "cwa_app." + name, "field": "*", "rule": DROPPED,
                          "target": "CWA app state (read flags, Kobo sync, thumbnails, CWA default shelves): "
                                    "nothing the owner entered beyond what CWA metadata/Yamtrack hold", "non_empty": n})
    return table, [{"source": s, "field": f, "rows": n} for (s, f), n in sorted(unclassified.items())]
