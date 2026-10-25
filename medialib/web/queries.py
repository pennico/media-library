"""Read queries for the console. Library and detail use the app role;
statistics use the read-only role (stats.* views only)."""
import collections
import datetime

KINDS = ("book", "movie", "series")
PROVIDER_URL = {
    "openlibrary_edition": "https://openlibrary.org/books/{}", "openlibrary_work": "https://openlibrary.org/works/{}",
    "isbn13": "https://openlibrary.org/isbn/{}", "tmdb_movie": "https://www.themoviedb.org/movie/{}",
    "tmdb_tv": "https://www.themoviedb.org/tv/{}", "google_books": "https://books.google.com/books?id={}",
    "imdb": "https://www.imdb.com/title/{}/", "wikidata": "https://www.wikidata.org/wiki/{}",
    "apple_books": "https://books.apple.com/book/id{}",
}

LIBRARY = """
SELECT i.id, i.public_id::text AS id_pub, i.kind, i.title, i.sort_title, i.locked, i.original_language,
       e.status, e.rating, e.added_at, e.review IS NOT NULL AS has_review,
       ed.pages, ed.language, ed.format, extract(year FROM ed.published_on)::int AS published,
       extract(year FROM i.first_published)::int AS first_published,
       (SELECT string_agg(p.name, ', ' ORDER BY ct.position) FROM catalog.contribution ct
          JOIN catalog.person p ON p.id = ct.person_id WHERE ct.item_id = i.id AND ct.role = 'author') AS authors,
       (SELECT p.sort_name FROM catalog.contribution ct JOIN catalog.person p ON p.id = ct.person_id
         WHERE ct.item_id = i.id AND ct.role = 'author' ORDER BY ct.position LIMIT 1) AS author_sort,
       (SELECT array_agg(t.name::text ORDER BY it.position) FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
         WHERE it.item_id = i.id AND t.kind = 'genre' AND t.deleted_at IS NULL) AS genres,
       (SELECT array_agg(it.tag_id ORDER BY it.position) FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
         WHERE it.item_id = i.id AND t.kind = 'genre' AND t.deleted_at IS NULL) AS cat_ids,
       EXISTS (SELECT 1 FROM catalog.edition_file f JOIN catalog.edition fe ON fe.id = f.edition_id
                WHERE fe.work_id = i.id AND f.storage = 'store' AND f.deleted_at IS NULL) AS has_file,
       (SELECT max(c.ended_on) FROM library.consumption c
         WHERE c.item_id = i.id AND c.state = 'completed' AND c.deleted_at IS NULL) AS last_read,
       (SELECT count(*) FROM library.consumption c
         WHERE c.item_id = i.id AND c.state = 'completed' AND c.deleted_at IS NULL)::int AS times_read,
       (SELECT c.progress_pct FROM library.consumption c
         WHERE c.item_id = i.id AND c.state IN ('in_progress', 'paused') AND c.deleted_at IS NULL) AS progress,
       (SELECT c.medium FROM library.consumption c
         WHERE c.item_id = i.id AND c.medium IS NOT NULL AND c.deleted_at IS NULL
         ORDER BY coalesce(c.ended_on, c.started_on) DESC NULLS LAST, c.id DESC LIMIT 1) AS medium,
       -- the finished readings, for the date filter and the charts: [ended, precision, read in, pages, medium]
       (SELECT jsonb_agg(jsonb_build_array(c.ended_on, c.ended_precision, c.language, coalesce(ce.pages, ed.pages), c.medium)
                         ORDER BY c.ended_on, c.id)
          FROM library.consumption c LEFT JOIN catalog.edition ce ON ce.id = c.edition_id AND ce.deleted_at IS NULL
         WHERE c.item_id = i.id AND c.state = 'completed' AND c.ended_on IS NOT NULL AND c.deleted_at IS NULL) AS reads,
       -- days with dated activity (start, finish, progress), as stats.activity_days counts them, for the calendar
       (SELECT jsonb_agg(d.day ORDER BY d.day) FROM (
          SELECT c.started_on AS day FROM library.consumption c
           WHERE c.item_id = i.id AND c.started_precision = 'day' AND c.deleted_at IS NULL
          UNION ALL SELECT c.ended_on FROM library.consumption c
           WHERE c.item_id = i.id AND c.ended_precision = 'day' AND c.deleted_at IS NULL
          UNION ALL SELECT (c.progress_at AT TIME ZONE 'Europe/Copenhagen')::date FROM library.consumption c
           WHERE c.item_id = i.id AND c.progress_at IS NOT NULL AND c.deleted_at IS NULL) d) AS days,
       cv.sha256 AS cover_sha
  FROM catalog.media_item i
  JOIN library.entry e ON e.item_id = i.id AND e.deleted_at IS NULL
  LEFT JOIN catalog.edition ed ON ed.work_id = i.id AND ed.is_primary AND ed.deleted_at IS NULL
  LEFT JOIN catalog.cover cv ON cv.is_chosen AND cv.deleted_at IS NULL AND (cv.edition_id = ed.id OR cv.item_id = i.id)
 WHERE i.deleted_at IS NULL
"""

SHELVES = """
SELECT s.id, s.public_id::text AS id_pub, s.name, s.kind, s.media_kind, s.position, s.sort_key, s.sort_desc,
       s.cover_size, s.layout, s.sort_locale,
       coalesce((SELECT array_agg(i.public_id::text ORDER BY si.position) FROM library.shelf_item si
                   JOIN catalog.media_item i ON i.id = si.item_id AND i.deleted_at IS NULL
                   JOIN library.entry e ON e.item_id = i.id AND e.deleted_at IS NULL
                  WHERE si.shelf_id = s.id), '{}') AS items
  FROM library.shelf s WHERE s.deleted_at IS NULL ORDER BY s.media_kind, s.position
"""


def iso(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


def thumb_url(sha, w=320):
    return "thumbs/{}-{}.jpg".format(sha, w) if sha else None


def summary(r):
    return {
        "id": r["id_pub"], "kind": r["kind"], "title": r["title"], "sort": r["sort_title"], "locked": r["locked"],
        "status": r["status"], "rating": float(r["rating"]) if r["rating"] is not None else None,
        "authors": r["authors"] or "", "authorSort": r["author_sort"] or "", "genres": r["genres"] or [],
        "year": r["last_read"].year if r["last_read"] else None, "timesRead": r["times_read"],
        "progress": float(r["progress"]) if r["progress"] is not None else None,
        "pages": r["pages"], "lang": r["language"], "origLang": r["original_language"],
        "published": r["first_published"] or r["published"], "added": r["added_at"].date().isoformat(),
        "hasReview": r["has_review"], "cover": thumb_url(r["cover_sha"], 320),
        "cats": r["cat_ids"] or [], "hasFile": r["has_file"], "format": r["format"], "medium": r["medium"],
        "reads": r["reads"] or [], "days": r["days"] or [],
    }


def library(conn):
    items = [summary(r) for r in conn.execute(LIBRARY + " ORDER BY i.sort_title").fetchall()]
    shelves = [{k: (r[k] if k != "id_pub" else None) for k in r if k not in ("id", "id_pub")} | {"id": r["id_pub"]}
               for r in conn.execute(SHELVES).fetchall()]
    langs = {r["code"]: r["name_en"] for r in conn.execute("SELECT code, name_en FROM catalog.language")}
    return {"items": items, "shelves": shelves, "languages": langs, "categories": categories(conn),
            "trash": conn.execute("SELECT count(*) AS n FROM catalog.media_item WHERE deleted_at IS NOT NULL"
                                  " AND kind IN ('book', 'movie', 'series')").fetchone()["n"]}


def categories(conn):
    """The genre tree: groups (parent null, with children) and the categories in them;
    old flat genres with no group are listed as groups of their own."""
    rows = conn.execute("""SELECT t.id, t.name, t.parent_id, t.position,
                                  EXISTS (SELECT 1 FROM library.tag c WHERE c.parent_id = t.id AND c.deleted_at IS NULL) AS is_group
                             FROM library.tag t WHERE t.kind = 'genre' AND t.deleted_at IS NULL
                            ORDER BY t.parent_id NULLS FIRST, t.position NULLS LAST, t.name""").fetchall()
    return [{"id": r["id"], "name": r["name"], "parent": r["parent_id"], "group": r["is_group"]} for r in rows]


def trash(conn, days=30):
    rows = conn.execute("""
        SELECT i.public_id::text AS id, i.kind, i.title, i.deleted_at,
               (SELECT string_agg(p.name, ', ' ORDER BY ct.position) FROM catalog.contribution ct
                  JOIN catalog.person p ON p.id = ct.person_id
                 WHERE ct.item_id = i.id AND ct.role IN ('author', 'director', 'creator')) AS authors,
               (SELECT cv.sha256 FROM catalog.cover cv LEFT JOIN catalog.edition e ON e.id = cv.edition_id
                 WHERE cv.is_chosen AND cv.deleted_at IS NULL AND (cv.item_id = i.id OR e.work_id = i.id) LIMIT 1) AS cover_sha
          FROM catalog.media_item i
         WHERE i.deleted_at IS NOT NULL AND i.kind IN ('book', 'movie', 'series')
         ORDER BY i.deleted_at DESC""").fetchall()
    return [{"id": r["id"], "kind": r["kind"], "title": r["title"], "authors": r["authors"] or "",
             "deletedAt": iso(r["deleted_at"]), "purgeOn": iso((r["deleted_at"] + datetime.timedelta(days=days)).date()),
             "cover": thumb_url(r["cover_sha"], 320)} for r in rows]


def item_id(conn, pid):
    row = conn.execute("SELECT id FROM catalog.media_item WHERE public_id = %s AND deleted_at IS NULL", (pid,)).fetchone()
    return row["id"] if row else None


def labels(prov):
    return {k: v.get("src") for k, v in (prov or {}).items() if isinstance(v, dict) and k != "_row"}


def history(conn, iid, edition_ids, reading_ids, limit=60):
    rows = conn.execute("""
        SELECT at, occurred_at, table_name, op, old_row, new_values, client, source, reason
          FROM audit.change
         WHERE (table_name = 'catalog.media_item' AND row_key = jsonb_build_object('id', %(i)s::bigint))
            OR (table_name = 'library.entry' AND row_key = jsonb_build_object('item_id', %(i)s::bigint))
            OR (table_name = 'catalog.edition' AND (row_key ->> 'id')::bigint = ANY(%(e)s))
            OR (table_name = 'library.consumption' AND (row_key ->> 'id')::bigint = ANY(%(r)s))
         ORDER BY coalesce(occurred_at, at) DESC, id DESC LIMIT %(n)s""",
                        {"i": iid, "e": edition_ids or [0], "r": reading_ids or [0], "n": limit}).fetchall()
    out = []
    skip = {"updated_at", "provenance", "created_at", "id", "public_id", "item_id", "work_id"}
    for r in rows:
        what = {"catalog.media_item": "book", "library.entry": "your entry", "catalog.edition": "edition",
                "library.consumption": "reading"}[r["table_name"]]
        changes = {}
        if r["op"] == "U":
            for k, v in (r["new_values"] or {}).items():
                if k not in skip:
                    changes[k] = [(r["old_row"] or {}).get(k), v]
        out.append({"at": iso(r["occurred_at"] or r["at"]), "what": what, "op": r["op"], "changes": changes,
                    "client": r["client"], "source": r["source"], "imported": r["client"] == "importer",
                    "external": r["occurred_at"] is not None})
    return out


def detail(conn, pid):
    iid = item_id(conn, pid)
    if iid is None:
        return None
    row = conn.execute(LIBRARY + " AND i.id = %s", (iid,)).fetchone()
    it = conn.execute("SELECT * FROM catalog.media_item WHERE id = %s", (iid,)).fetchone()
    en = conn.execute("SELECT * FROM library.entry WHERE item_id = %s", (iid,)).fetchone()
    eds = conn.execute("SELECT * FROM catalog.edition WHERE work_id = %s AND deleted_at IS NULL"
                       " ORDER BY is_primary DESC, id", (iid,)).fetchall()
    primary = next((e for e in eds if e["is_primary"]), None)
    ed_ids = [e["id"] for e in eds]
    ids = conn.execute("""SELECT x.provider, x.value, x.source, x.edition_id FROM catalog.external_id x
                           WHERE x.item_id = %s OR x.edition_id = ANY(%s) ORDER BY x.provider, x.value""",
                       (iid, ed_ids or [0])).fetchall()
    covers = conn.execute("""SELECT id, source, source_url, sha256, is_chosen, note, width, height FROM catalog.cover
                              WHERE deleted_at IS NULL AND (item_id = %s OR edition_id = %s)
                              ORDER BY is_chosen DESC, id""", (iid, primary["id"] if primary else 0)).fetchall()
    files = conn.execute("""SELECT f.public_id::text AS id, f.format, f.size_bytes, f.kosync_md5, f.rel_path, f.storage, f.added_at
                              FROM catalog.edition_file f
                             WHERE f.edition_id = ANY(%s) AND f.deleted_at IS NULL ORDER BY f.id""", (ed_ids or [0],)).fetchall()
    reads = conn.execute("""SELECT id, public_id::text AS id_pub, state, started_on, started_precision, ended_on,
                                   ended_precision, language, medium, note, source, progress_pct, progress_pages
                              FROM library.consumption WHERE item_id = %s AND deleted_at IS NULL
                             ORDER BY coalesce(ended_on, started_on, 'infinity'::date) DESC, id DESC""", (iid,)).fetchall()
    people = conn.execute("""SELECT p.name, p.sort_name FROM catalog.contribution ct JOIN catalog.person p ON p.id = ct.person_id
                              WHERE ct.item_id = %s AND ct.role = 'author' ORDER BY ct.position""", (iid,)).fetchall()
    tags = conn.execute("""SELECT t.id, t.kind, t.name FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
                            WHERE it.item_id = %s AND t.deleted_at IS NULL ORDER BY t.kind, it.position""", (iid,)).fetchall()
    shelves = conn.execute("""SELECT s.public_id::text AS id, s.name, s.kind,
                                     EXISTS (SELECT 1 FROM library.shelf_item si WHERE si.shelf_id = s.id AND si.item_id = %s) AS member
                                FROM library.shelf s WHERE s.deleted_at IS NULL AND s.media_kind = %s ORDER BY s.position""",
                           (iid, it["kind"])).fetchall()
    d = summary(row)
    d.update({
        "review": en["review"],
        "item": {"title": it["title"], "sortTitle": it["sort_title"], "titleLang": it["title_language"],
                 "originalTitle": it["original_title"], "originalLang": it["original_language"],
                 "firstPublished": iso(it["first_published"]), "firstPublishedPrecision": it["first_published_precision"],
                 "description": it["description"]},
        "edition": None if not primary else {
            "id": str(primary["public_id"]),
            "title": primary["title"], "subtitle": primary["subtitle"], "lang": primary["language"],
            "publisher": primary["publisher"], "published": iso(primary["published_on"]),
            "publishedPrecision": primary["published_precision"], "pages": primary["pages"], "format": primary["format"],
            "description": primary["description"]},
        "otherEditions": [{"title": e["title"], "pages": e["pages"],
                           "published": e["published_on"].year if e["published_on"] else None,
                           "ids": [{"p": x["provider"], "v": x["value"]} for x in ids if x["edition_id"] == e["id"]]}
                          for e in eds if not e["is_primary"]],
        "authorsList": [{"name": p["name"], "sort": p["sort_name"]} for p in people],
        "genreList": [t["name"] for t in tags if t["kind"] == "genre"],
        "genreIds": [t["id"] for t in tags if t["kind"] == "genre"],
        "tagList": [t["name"] for t in tags if t["kind"] == "tag"],
        "readings": [{"id": r["id_pub"], "state": r["state"], "started": iso(r["started_on"]),
                      "startedPrecision": r["started_precision"], "ended": iso(r["ended_on"]),
                      "endedPrecision": r["ended_precision"], "lang": r["language"], "medium": r["medium"], "note": r["note"],
                      "source": r["source"], "progress": float(r["progress_pct"]) if r["progress_pct"] is not None else None}
                     for r in reads],
        "ids": [{"p": x["provider"], "v": x["value"], "url": PROVIDER_URL.get(x["provider"], "").format(x["value"]) or None,
                 "src": x["source"]} for x in ids if x["edition_id"] in (None, primary["id"] if primary else None)],
        "covers": [{"id": c["id"], "source": c["source"], "chosen": c["is_chosen"], "note": c["note"],
                    "thumb": thumb_url(c["sha256"], 320) if c["sha256"] else None, "url": c["source_url"],
                    "size": "{}×{}".format(c["width"], c["height"]) if c["width"] else None} for c in covers],
        "files": [{"id": f["id"], "format": f["format"], "mb": round(f["size_bytes"] / 1e6, 1), "kosync": f["kosync_md5"],
                   "storage": f["storage"], "download": "api/files/" + f["id"] if f["storage"] == "store" else None,
                   "added": iso(f["added_at"])} for f in files],
        "prov": {"item": labels(it["provenance"]), "entry": labels(en["provenance"]),
                 "edition": labels(primary["provenance"]) if primary else {}},
        "shelves": [dict(s) for s in shelves],
        "history": history(conn, iid, ed_ids, [r["id"] for r in reads]),
    })
    return d


def stats(ro, kind="book"):
    """Visuals for books, from the stats views (read-only role)."""
    q = lambda sql, *a: ro.execute(sql, a).fetchall()  # noqa: E731
    year = datetime.date.today().year
    by_year = [{"year": r["year"], "books": r["books"], "pages": r["pages"],
                "rating": float(r["avg_rating"]) if r["avg_rating"] is not None else None}
               for r in q("SELECT * FROM stats.books_by_year ORDER BY year")]
    this = next((y for y in by_year if y["year"] == year), {"books": 0, "pages": 0, "rating": None})
    counts = {r["status"]: r["items"] for r in q("SELECT status, items FROM stats.library_counts WHERE media_kind = %s", kind)}
    return {
        "kind": kind, "year": year,
        "tiles": {"readThisYear": this["books"], "pagesThisYear": this["pages"] or 0, "avgRatingThisYear": this["rating"],
                  "readTotal": sum(y["books"] for y in by_year), "toRead": counts.get("planned", 0),
                  "reading": counts.get("in_progress", 0) + counts.get("paused", 0)},
        "byYear": by_year,
        "byMonth": [{"year": r["year"], "month": r["month"], "books": r["books"]}
                    for r in q("SELECT * FROM stats.books_by_month ORDER BY year, month NULLS FIRST")],
        "ratings": [{"bucket": r["bucket"], "books": r["books"]} for r in q("SELECT * FROM stats.rating_histogram ORDER BY bucket")],
        "authors": [{"name": r["name"], "books": r["books_read"]}
                    for r in q("SELECT * FROM stats.top_authors ORDER BY books_read DESC, sort_name LIMIT 12")],
        "genres": [{"name": r["name"], "read": r["books_read"], "total": r["books"]}
                   for r in q("SELECT * FROM stats.genres ORDER BY books_read DESC, books DESC, name LIMIT 14")],
        "readIn": [{"lang": r["read_in"], "books": r["n"]}
                   for r in q("SELECT read_in, sum(books)::int AS n FROM stats.languages GROUP BY read_in ORDER BY n DESC")],
        "original": [{"lang": r["original_language"], "books": r["n"]}
                     for r in q("SELECT original_language, sum(books)::int AS n FROM stats.languages"
                                " GROUP BY original_language ORDER BY n DESC")],
        "activity": [{"day": iso(r["day"]), "events": r["events"]}
                     for r in q("SELECT * FROM stats.activity_days WHERE day > current_date - 371 ORDER BY day")],
        "current": [{"title": r["title"], "authors": r["authors"], "pct": float(r["progress_pct"]) if r["progress_pct"] else None,
                     "pages": r["pages"], "progressPages": r["progress_pages"], "started": iso(r["started_on"])}
                    for r in q("SELECT * FROM stats.current ORDER BY started_on DESC NULLS LAST")],
        "yearPrecisionOnly": sum(1 for m in q("SELECT 1 FROM stats.books_by_month WHERE month IS NOT NULL")) == 0,
    }


def reading_apps(conn):
    """Readest (synced documents, OPDS books) and the Joplin feed, for the console."""
    from ..feeds import kosync
    docs = [{"document": d["document"], "percentage": d["percentage"], "device": d["device"],
             "updated": iso(d["updated_at"]), "firstSeen": iso(d["first_seen_at"]), "item": d["item"],
             "title": d["title"], "how": d["how"], "appTitle": d["metadata"].get("title"),
             "appAuthors": d["metadata"].get("authors")} for d in kosync.documents(conn)]
    books = conn.execute("""SELECT count(DISTINCT e.work_id) AS n FROM catalog.edition_file f
                              JOIN catalog.edition e ON e.id = f.edition_id AND e.deleted_at IS NULL
                              JOIN catalog.media_item i ON i.id = e.work_id AND i.deleted_at IS NULL
                             WHERE f.storage = 'store' AND f.deleted_at IS NULL""").fetchone()["n"]
    run = conn.execute("""SELECT started_at, finished_at, status, summary FROM ingest.run
                           WHERE tool = 'joplin' AND mode = 'apply' ORDER BY id DESC LIMIT 1""").fetchone()
    unclear = conn.execute("""SELECT title, kind, problem, candidates, seen_at FROM sync.joplin_note
                               WHERE state = 'unclear' ORDER BY seen_at DESC LIMIT 100""").fetchall()
    synced = conn.execute("SELECT count(*) AS n FROM sync.joplin_note WHERE state = 'synced'").fetchone()["n"]
    return {
        "documents": docs, "opdsBooks": books,
        "joplin": {
            "lastRun": {"at": iso(run["finished_at"] or run["started_at"]), "status": run["status"],
                        "summary": run["summary"]} if run else None,
            "synced": synced,
            "unclear": [{"title": u["title"], "kind": u["kind"], "problem": u["problem"],
                         "candidates": u["candidates"], "seen": iso(u["seen_at"])} for u in unclear],
        },
    }


def library_stats(ro, scope="all"):
    """Visuals → Library: what the books are (any status), from stats.library_book.
    scope: all | read | unread."""
    where = {"all": "true", "read": "status = 'completed'", "unread": "status <> 'completed'"}[scope]
    rows = ro.execute("SELECT * FROM stats.library_book WHERE " + where).fetchall()
    langs, authors, groups, cats, buckets = (collections.Counter() for _ in range(5))
    years = []
    for r in rows:
        langs[r["original_language"]] += 1
        for a in dict.fromkeys(r["authors"] or []):
            authors[a] += 1
        for g in dict.fromkeys(r["groups"] or []):
            groups[g] += 1
        for c in dict.fromkeys(r["categories"] or []):
            cats[c] += 1
        if r["released"] is not None:
            years.append(r["released"])
            buckets[release_bucket(r["released"])] += 1
    order = sorted(buckets, key=lambda b: BUCKET_ORDER.get(b, int(b[:4]) if b[:4].isdigit() else 0))
    return {
        "scope": scope,
        "tiles": {"books": len(rows), "authors": len(authors), "languages": len([k for k in langs if k]),
                  "categories": len(cats), "oldest": min(years) if years else None, "newest": max(years) if years else None,
                  "noRelease": len(rows) - len(years)},
        "languages": [{"lang": k, "books": n} for k, n in langs.most_common()],
        "authors": [{"name": k, "books": n} for k, n in authors.most_common(15)],
        "groups": [{"name": k, "books": n} for k, n in groups.most_common()],
        "categories": [{"name": k, "books": n} for k, n in cats.most_common()],
        "released": [{"label": b, "books": buckets[b]} for b in order],
    }


BUCKET_ORDER = {"Antiquity": -1000, "Before 1700": 1000, "1700s": 1700, "1800s": 1800}


def release_bucket(year):
    if year < 500:
        return "Antiquity"
    if year < 1700:
        return "Before 1700"
    if year < 1900:
        return "{}00s".format(year // 100)
    return "{}s".format(year // 10 * 10)
