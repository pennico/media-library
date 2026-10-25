"""Joplin review notes -> the library (in place of the Joplin -> Yamtrack job).

    python -m medialib joplin --tar - [--apply]     # tar stream of the sync folder on stdin
    python -m medialib joplin --dir PATH [--apply]

Each run:
1. reads the sync folder (read-only) and takes the live notes of the reviews
   notebook and its sub-notebooks (template notes are skipped);
2. parses notes that are new or changed since the last run, and tries the
   ones left unclear again against the library;
3. finds the item: the one the note matched before; an explicit ``isbn:`` /
   ``openlibrary:`` / ``tmdb:`` line; a book or film already in the library
   (title, confirmed by author or year); else, for books, Apple Books + Open
   Library, where title and author must agree (never a guess). Movies and
   series not in the library are added from the note itself (no TMDB yet);
4. writes as the owner (the notes are the owner's words), client
   ``joplin:<note id>``: status, rating, review, a reading with the note's
   date, collections. An edited note updates what it wrote before. Readings
   added in the console or by Readest stay; a "finished" note closes the open
   one instead of adding another;
5. stores the summary with the run (``ingest.run``, tool ``joplin``) for the
   console, and prints it.
"""
import datetime

from psycopg.types.json import Jsonb

from .. import db
from ..fill import providers as P
from ..fill.service import FillError
from ..web.writes import period_end, sync_status
from . import joplin_items, notes

NOTEBOOK = "Reviews"
SKIP_NOTEBOOKS = ("templates", "_templates")
CREATOR_ROLE = {"book": "author", "movie": "director", "series": "creator"}
TMDB = {"movie": "tmdb_movie", "series": "tmdb_tv"}
ACCEPT = 0.90          # title score needed when the author (or year) confirms it
ACCEPT_SOLO = 0.97     # ... when nothing confirms it: only the same title will do
PRECISION_RANK = {"day": 3, "month": 2, "year": 1, None: 0}


class Unclear(Exception):
    def __init__(self, problem, candidates=()):
        super().__init__(problem)
        self.candidates = list(candidates)


# ------------------------------------------------------------------ matching

class Library:
    """Titles and people of every item, for matching notes to what is there."""

    def __init__(self, conn):
        self.conn = conn
        self.refresh()

    def refresh(self):
        self.items = self.conn.execute("""
            SELECT i.id, i.kind, i.title, i.original_title,
                   coalesce(extract(year FROM m.release_date), extract(year FROM s.first_air_date),
                            extract(year FROM i.first_published))::int AS year,
                   array_remove(array_agg(DISTINCT e.title::text), NULL) AS edition_titles,
                   array_remove(array_agg(DISTINCT p.name::text), NULL) AS people
              FROM catalog.media_item i
              LEFT JOIN catalog.movie m ON m.item_id = i.id
              LEFT JOIN catalog.series s ON s.item_id = i.id
              LEFT JOIN catalog.edition e ON e.work_id = i.id AND e.deleted_at IS NULL
              LEFT JOIN catalog.contribution c ON c.item_id = i.id
                                              AND c.role IN ('author', 'director', 'creator', 'writer')
              LEFT JOIN catalog.person p ON p.id = c.person_id
             WHERE i.deleted_at IS NULL AND i.kind IN ('book', 'movie', 'series')
             GROUP BY i.id, m.release_date, s.first_air_date""").fetchall()

    def find(self, kind, title, by, year):
        """The item id, None, or Unclear when several fit equally well."""
        want = notes.people(by)
        scored = []
        for it in self.items:
            if it["kind"] != kind:
                continue
            titles = [t for t in [it["title"], it["original_title"]] + list(it["edition_titles"]) if t]
            ts = max((P.title_score(title, t) for t in titles), default=0.0)
            by_ok = P.authors_agree(want, it["people"]) if want and it["people"] else None
            year_ok = abs(year - it["year"]) <= 1 if year and it["year"] else None
            if by_ok is False or (year_ok is False and kind != "book"):
                continue
            confirmed = bool(by_ok or year_ok)
            if ts >= (ACCEPT if confirmed else ACCEPT_SOLO):
                scored.append((ts + (0.05 if confirmed else 0), it))
        if not scored:
            return None
        scored.sort(key=lambda s: -s[0])
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            raise Unclear("several items in the library fit: {}; add an 'isbn:' or 'openlibrary:' line".format(
                ", ".join(s[1]["title"] for s in scored[:3])))
        return scored[0][1]["id"]


def _item_by_id(conn, provider, value):
    row = conn.execute("""SELECT coalesce(x.item_id, e.work_id) AS id FROM catalog.external_id x
                           LEFT JOIN catalog.edition e ON e.id = x.edition_id
                           JOIN catalog.media_item i ON i.id = coalesce(x.item_id, e.work_id)
                          WHERE x.provider = %s AND x.value = %s AND i.deleted_at IS NULL""",
                       (provider, value)).fetchone()
    return row["id"] if row else None


def _pid_to_id(conn, pid):
    return conn.execute("SELECT id FROM catalog.media_item WHERE public_id = %s", (pid,)).fetchone()["id"]


def _choose(entry, cands):
    """The candidate index to add, or Unclear. Title and author must agree;
    with no author in the note only the very same title counts, and then all
    such candidates must agree on the author."""
    want = notes.people(entry["by"])
    need = ACCEPT if want else ACCEPT_SOLO
    good = [c for c in cands if c["score"] >= need]
    if not good:
        raise Unclear("no confident match; add an 'isbn:' or 'openlibrary: OL…M' line, or add the book in the"
                      " console (the note then finds it by title)", cands[:3])
    if not want and any(not P.authors_agree(good[0]["authors"], c["authors"]) for c in good[1:]):
        raise Unclear("the title fits books by different authors; add a 'by:' line", good[:3])
    return good[0]["index"]


def _short(cands):
    return [{"title": c["title"], "authors": c.get("authors") or [], "provider": c.get("provider")} for c in cands[:3]]


def resolve(conn, fill, nid, entry, prev, library, search, apply):
    """-> (item id or None to create from the note, how). Raises Unclear."""
    kind = entry["kind"]
    if prev and prev["item_id"] and prev["title"] == entry["title"] and prev["kind"] == kind:
        row = conn.execute("SELECT deleted_at IS NOT NULL AS trashed FROM catalog.media_item WHERE id = %s",
                           (prev["item_id"],)).fetchone()
        if row and row["trashed"]:
            raise Unclear("its book is in the trash: restore it, or delete it for good to let the note go")
        if row:
            return prev["item_id"], "as before"
    ids = entry["ids"]
    client = "joplin:" + nid
    if kind == "book" and ids.get("isbn13"):
        found = _item_by_id(conn, "isbn13", ids["isbn13"])
        if found:
            return found, "by ISBN"
    olid = ids.get("openlibrary", "")
    if kind == "book" and olid:
        if olid.endswith("W"):
            found = _item_by_id(conn, "openlibrary_work", olid)
            if found:
                return found, "by Open Library work"
            raise Unclear("an Open Library work id is not looked up; use the edition id (OL…M) or an ISBN")
        found = _item_by_id(conn, "openlibrary_edition", olid)
        if found:
            return found, "by Open Library edition"
    if kind in TMDB and ids.get("tmdb"):
        found = _item_by_id(conn, TMDB[kind], ids["tmdb"])
        if found:
            return found, "by TMDB id"
    found = library.find(kind, entry["title"], entry["by"], entry["year"])
    if found:
        return found, "in the library"
    if kind != "book":
        return None, "added from the note"
    if not search:
        raise Unclear(prev["problem"] if prev and prev["problem"] else "not found",
                      prev["candidates"] if prev else ())
    if fill is None:
        raise Unclear("not in the library (offline run: no Apple Books / Open Library search)")
    try:
        if olid:
            res = fill.lookup_edition(olid)
            if not res["candidates"]:
                raise Unclear("Open Library has no edition {}".format(olid))
            index = 0
        elif ids.get("isbn13"):
            res = fill.lookup(isbn=ids["isbn13"])
            if not res["candidates"]:
                raise Unclear("Apple Books and Open Library don't know ISBN {}".format(ids["isbn13"]))
            index = 0
        else:
            res = fill.lookup(entry["title"], (notes.people(entry["by"]) or [None])[0])
            index = _choose(entry, res["candidates"])
    except FillError as e:
        raise Unclear("search failed: {}".format(e)) from None
    pick = res["candidates"][index]
    if not apply:
        return None, "would add from {}: {}".format(pick["provider"], pick["title"])
    added = fill.add(conn, None, res["token"], index, "planned", client=client)
    library.refresh()
    return _pid_to_id(conn, added["id"]), ("already in the library" if added["existing"]
                                           else "added from {}".format(pick["provider"]))


# -------------------------------------------------------------------- writes

def create_from_note(conn, entry):
    """A movie or series the library doesn't have, from the note's own data."""
    kind = entry["kind"]
    year = entry["year"]
    it = conn.execute("INSERT INTO catalog.media_item (kind, title, original_language, first_published,"
                      " first_published_precision) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                      (kind, entry["title"], entry["original_code"], datetime.date(year, 1, 1) if year else None,
                       "year" if year else None)).fetchone()["id"]
    conn.execute("INSERT INTO catalog.{} (item_id) VALUES (%s)".format(kind), (it,))
    if entry["ids"].get("tmdb") and kind in TMDB:
        conn.execute("INSERT INTO catalog.external_id (provider, value, item_id, source) VALUES (%s, %s, %s, 'owner')",
                     (TMDB[kind], entry["ids"]["tmdb"], it))
    conn.execute("INSERT INTO library.entry (item_id) VALUES (%s)", (it,))
    return it


def _fill_empty(conn, item, entry):
    """Details from the note where the library has nothing yet."""
    row = conn.execute("""SELECT i.kind, i.original_language, e.id AS edition_id, e.pages,
                                 (SELECT count(*) FROM catalog.contribution c WHERE c.item_id = i.id
                                   AND c.role = %s) AS n_people,
                                 (SELECT count(*) FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
                                   WHERE it.item_id = i.id AND t.kind = 'genre') AS n_genres
                            FROM catalog.media_item i
                            LEFT JOIN catalog.edition e ON e.work_id = i.id AND e.is_primary AND e.deleted_at IS NULL
                           WHERE i.id = %s""", (CREATOR_ROLE[entry["kind"]], item)).fetchone()
    done = []
    if entry["original_code"] and not row["original_language"]:
        conn.execute("UPDATE catalog.media_item SET original_language = %s WHERE id = %s", (entry["original_code"], item))
        done.append("original language")
    if entry["pages"] and row["edition_id"] and not row["pages"] and 0 < entry["pages"] <= 20000:
        conn.execute("UPDATE catalog.edition SET pages = %s WHERE id = %s", (entry["pages"], row["edition_id"]))
        done.append("pages")
    if entry["by"] and not row["n_people"]:
        for pos, name in enumerate(list(dict.fromkeys(notes.people(entry["by"])))[:10], 1):
            p = conn.execute("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)"
                             " AND disambiguation IS NULL AND deleted_at IS NULL", (name,)).fetchone()
            pid = p["id"] if p else conn.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id",
                                                 (name,)).fetchone()["id"]
            conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source)"
                         " VALUES (%s, %s, %s, %s, 'owner')",
                         (pid, CREATOR_ROLE[entry["kind"]], item, pos))
        done.append(CREATOR_ROLE[entry["kind"]])
    if entry["genre"] and not row["n_genres"]:
        t = conn.execute("SELECT id FROM library.tag WHERE kind = 'genre' AND lower(catalog.f_unaccent(name)) ="
                         " lower(catalog.f_unaccent(%s)) AND deleted_at IS NULL", (entry["genre"],)).fetchone()
        tid = t["id"] if t else conn.execute("INSERT INTO library.tag (kind, name) VALUES ('genre', %s) RETURNING id",
                                             (entry["genre"][:100],)).fetchone()["id"]
        conn.execute("INSERT INTO library.item_tag (item_id, tag_id, position, source) VALUES (%s, %s, 1, 'owner')",
                     (item, tid))
        done.append("genre")
    return done


def _reading(conn, rid, item):
    if not rid:
        return None
    return conn.execute("SELECT * FROM library.consumption WHERE id = %s AND item_id = %s AND deleted_at IS NULL",
                        (rid, item)).fetchone()


def _set_reading(conn, rid, cols):
    sets = ", ".join("{} = %s".format(c) for c in cols)
    conn.execute("UPDATE library.consumption SET {} WHERE id = %s".format(sets), (*cols.values(), rid))


def apply_reading(conn, item, entry, prev_rid, today):
    """Make the readings say what the note says. Returns (reading id, what changed)."""
    status = entry["status"]
    mine = _reading(conn, prev_rid, item)
    readings = conn.execute("SELECT * FROM library.consumption WHERE item_id = %s AND deleted_at IS NULL"
                            " ORDER BY coalesce(ended_on, started_on, created_at::date) DESC, id DESC",
                            (item,)).fetchall()
    open_r = next((r for r in readings if r["state"] in ("in_progress", "paused")), None)
    edition = conn.execute("SELECT id FROM catalog.edition WHERE work_id = %s AND is_primary AND deleted_at IS NULL",
                           (item,)).fetchone()
    extra = {}
    if entry["read_in_code"]:
        extra["language"] = entry["read_in_code"]
    if entry["audiobook"]:
        extra["medium"] = "audiobook"
    date, prec = entry["date"] or (None, None)

    if status == "planned":
        if mine:
            conn.execute("UPDATE library.consumption SET deleted_at = now() WHERE id = %s", (mine["id"],))
            sync_status(conn, item)
            return None, "reading removed (the note says to read)"
        if readings:
            return None, "status kept: there are readings of it"
        return None, ""

    if status in ("in_progress", "paused"):
        target = mine if mine and mine["state"] in ("in_progress", "paused") else open_r
        if target:
            if target["state"] != status or any(target[k] != v for k, v in extra.items()):
                _set_reading(conn, target["id"], dict(extra, state=status))
                sync_status(conn, item)
                return target["id"], "reading " + status.replace("_", " ")
            return target["id"], ""
        start = (date, prec) if entry["date_given"] else (today, "day")
        rid = conn.execute("INSERT INTO library.consumption (item_id, edition_id, state, started_on, started_precision,"
                           " language, medium, source) VALUES (%s, %s, %s, %s, %s, %s, %s, 'joplin') RETURNING id",
                           (item, edition["id"] if edition else None, status, start[0], start[1],
                            extra.get("language"), extra.get("medium"))).fetchone()["id"]
        sync_status(conn, item)
        return rid, "reading started"

    # completed or dropped: the end of a reading
    end = (date, prec) if date else (today, "day")
    rid, what = None, ""
    if mine:
        cols = dict(extra, state=status)
        if entry["date_given"] or mine["source"] == "joplin":
            if mine["started_on"] and mine["started_on"] > period_end(*end):
                end = (today, "day")
            cols.update(ended_on=end[0], ended_precision=end[1])
        changed = {k: v for k, v in cols.items() if mine[k] != v}
        if changed:
            _set_reading(conn, mine["id"], changed)
            what = "reading updated"
        rid = mine["id"]
    elif open_r:
        if open_r["started_on"] and open_r["started_on"] > period_end(*end):
            end = (today, "day")
        _set_reading(conn, open_r["id"], dict(extra, state=status, ended_on=end[0], ended_precision=end[1]))
        rid, what = open_r["id"], "open reading closed as " + ("read" if status == "completed" else "abandoned")
    else:
        same = [r for r in readings if r["state"] == status]
        if same and not entry["date_given"]:
            rid = same[0]["id"]                 # no date in the note: it is about the reading already there
        for r in same if entry["date_given"] else ():
            how = _same_reading(r, end)
            if how == "refine":
                _set_reading(conn, r["id"], dict(extra, ended_on=end[0], ended_precision=end[1]))
                what = "reading date set to {}".format(end[0].isoformat()[:{"year": 4, "month": 7, "day": 10}[end[1]]])
            if how:
                rid = r["id"]
                break
        if rid is None:
            rid = conn.execute("INSERT INTO library.consumption (item_id, edition_id, state, ended_on, ended_precision,"
                               " language, medium, source) VALUES (%s, %s, %s, %s, %s, %s, %s, 'joplin') RETURNING id",
                               (item, edition["id"] if edition else None, status, end[0], end[1],
                                extra.get("language"), extra.get("medium"))).fetchone()["id"]
            done = ("read" if entry["kind"] == "book" else "watched") if status == "completed" else "abandoned"
            what = "reading added ({} {})".format(done,
                                                  end[0].isoformat()[:{"year": 4, "month": 7, "day": 10}[end[1]]])
    if _settle(conn, item, status) and not what:
        what = "marked " + ("read" if status == "completed" else "abandoned")
    return rid, what


def _same_reading(r, end):
    """Is reading r the one a note dated `end` (date, precision) is about?
    'keep' (r is as precise or more, inside the note's period), 'refine' (the
    note is more precise and inside r's period, or both are days at most a
    month apart, e.g. Readest finished it "today" and the note says when),
    or None."""
    if not r["ended_on"]:
        return None
    d, p = end
    rank, r_rank = PRECISION_RANK[p], PRECISION_RANK[r["ended_precision"]]
    if r_rank >= rank and d <= r["ended_on"] <= period_end(d, p):
        return "keep" if r_rank > rank or r["ended_on"] == d else "refine"
    if rank > r_rank and r["ended_on"] <= d <= period_end(r["ended_on"], r["ended_precision"]):
        return "refine"
    if p == "day" and r["ended_precision"] == "day" and abs((r["ended_on"] - d).days) <= 31:
        return "refine"
    return None


def _settle(conn, item, status):
    """After a finished/abandoned note: the entry says so too. True if it changed."""
    sync_status(conn, item)
    e = conn.execute("SELECT status FROM library.entry WHERE item_id = %s", (item,)).fetchone()
    if e["status"] in ("in_progress", "paused") or e["status"] == status:
        return False
    conn.execute("UPDATE library.entry SET status = %s, status_changed_at = now() WHERE item_id = %s", (status, item))
    return True


def apply_shelves(conn, item, kind, names, before):
    """Collections named in the note; ones this note added before and no longer names are left."""
    media_kind = {"book": "book", "movie": "movie", "series": "series"}[kind]
    added = []
    for name in names:
        s = conn.execute("SELECT id FROM library.shelf WHERE media_kind = %s AND lower(name) = lower(%s)"
                         " AND deleted_at IS NULL", (media_kind, name)).fetchone()
        sid = s["id"] if s else conn.execute(
            "INSERT INTO library.shelf (name, kind, media_kind, position) VALUES (%s, 'manual', %s,"
            " (SELECT coalesce(max(position), 0) + 1 FROM library.shelf WHERE media_kind = %s)) RETURNING id",
            (name, media_kind, media_kind)).fetchone()["id"]
        if not conn.execute("SELECT 1 AS x FROM library.shelf_item WHERE shelf_id = %s AND item_id = %s",
                            (sid, item)).fetchone():
            conn.execute("INSERT INTO library.shelf_item (shelf_id, item_id, position) SELECT %s, %s,"
                         " coalesce(max(position), 0) + 1 FROM library.shelf_item WHERE shelf_id = %s", (sid, item, sid))
            added.append(name)
    wanted = {n.lower() for n in names}
    removed = []
    for name in before or []:
        if name.lower() in wanted:
            continue
        row = conn.execute("""DELETE FROM library.shelf_item si USING library.shelf s
                               WHERE s.id = si.shelf_id AND si.item_id = %s AND lower(s.name) = lower(%s)
                                 AND s.media_kind = %s AND s.kind = 'manual'
                           RETURNING si.shelf_id, si.position""", (item, name, media_kind)).fetchone()
        if row:
            conn.execute("UPDATE library.shelf_item SET position = position - 1 WHERE shelf_id = %s AND position > %s",
                         (row["shelf_id"], row["position"]))
            removed.append(name)
    return added, removed


def apply_note(conn, nid, updated, entry, item, prev, today):
    """Write one note into the library (caller holds the transaction)."""
    changes = []
    if item is None:
        item = create_from_note(conn, entry)
    if not conn.execute("SELECT 1 AS x FROM library.entry WHERE item_id = %s", (item,)).fetchone():
        conn.execute("INSERT INTO library.entry (item_id) VALUES (%s)", (item,))
    en = conn.execute("SELECT rating, review FROM library.entry WHERE item_id = %s", (item,)).fetchone()
    if entry["rating"] is not None and en["rating"] != entry["rating"]:
        conn.execute("UPDATE library.entry SET rating = %s, rated_at = now() WHERE item_id = %s", (entry["rating"], item))
        changes.append("rating {:g}".format(entry["rating"]))
    if entry["comment"] and (en["review"] or "").strip() != entry["comment"]:
        conn.execute("UPDATE library.entry SET review = %s, reviewed_at = now() WHERE item_id = %s",
                     (entry["comment"][:100000], item))
        changes.append("review")
    changes += _fill_empty(conn, item, entry)
    same_item = prev and prev["item_id"] == item
    rid, what = apply_reading(conn, item, entry, prev["consumption_id"] if same_item else None, today)
    if what:
        changes.append(what)
    added, removed = apply_shelves(conn, item, entry["kind"], entry["lists"], prev["shelves"] if same_item else [])
    changes += ["in " + n for n in added] + ["out of " + n for n in removed]
    save_state(conn, nid, updated, entry, "synced", item=item, rid=rid, shelves=entry["lists"])
    return item, changes


def save_state(conn, nid, updated, entry, state, item=None, rid=None, shelves=(), problem=None, candidates=()):
    conn.execute("""INSERT INTO sync.joplin_note (note_id, updated_time, title, kind, state, item_id, consumption_id,
                                                  shelves, problem, candidates, seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (note_id) DO UPDATE SET updated_time = EXCLUDED.updated_time, title = EXCLUDED.title,
                        kind = EXCLUDED.kind, state = EXCLUDED.state, item_id = EXCLUDED.item_id,
                        consumption_id = coalesce(EXCLUDED.consumption_id, sync.joplin_note.consumption_id),
                        shelves = EXCLUDED.shelves, problem = EXCLUDED.problem, candidates = EXCLUDED.candidates,
                        seen_at = now()""",
                 (nid, updated[:40], entry["title"][:1000] or "(no title)", entry["kind"], state, item, rid,
                  list(shelves), (problem or "")[:2000] or None, Jsonb(list(candidates))))


# ----------------------------------------------------------------------- run

def changed_notes(items, notebook, state):
    """[(note id, updated_time, entry, prev, search)] for notes to look at."""
    out = []
    for note, path in joplin_items.notes_in_notebook(items, notebook):
        if any(p.strip().lower() in SKIP_NOTEBOOKS for p in path[1:]) or \
                note["title"].strip().lower().startswith("template"):
            continue
        prev = state.get(note["id"])
        updated = note.get("updated_time", "")
        same = prev is not None and prev["updated_time"] == updated
        if same and prev["state"] in ("synced", "deleted"):
            continue
        created = note.get("user_created_time") or note.get("created_time")
        entry = notes.parse_note(note["title"], note["body"], path[1:], created=created)
        out.append((note["id"], updated, entry, prev, not same))
    return out


def run(conn, items, fill=None, apply=False, notebook=NOTEBOOK, today=None, log=print):
    """One pass. Returns the summary dict (also stored with the run)."""
    today = today or datetime.date.today()
    summary = {"added": [], "updated": [], "unclear": [], "errors": [], "warnings": [], "notes": 0}
    status = "failed"
    run_id = conn.execute("INSERT INTO ingest.run (tool, mode, sources) VALUES ('joplin', %s, %s) RETURNING id",
                          ("apply" if apply else "dry_run", Jsonb({"notebook": notebook}))).fetchone()["id"]
    try:
        state = {r["note_id"]: r for r in conn.execute("SELECT * FROM sync.joplin_note").fetchall()}
        todo = changed_notes(items, notebook, state)
        summary["notes"] = len(joplin_items.notes_in_notebook(items, notebook))
        library = Library(conn)
        for nid, updated, entry, prev, search in todo:
            label = "{} ({})".format(entry["title"] or nid, entry["kind"] or "?")
            summary["warnings"] += ["{}: {}".format(label, w) for w in entry["warnings"]]
            try:
                if entry["problems"]:
                    raise Unclear("; ".join(entry["problems"]))
                item, how = resolve(conn, fill, nid, entry, prev, library, search, apply)
                if not apply:
                    summary["updated" if item else "added"].append("{}: {}".format(label, how))
                    continue
                with db.write_tx(conn, "owner", "joplin:" + nid, reason="Joplin note"):
                    created = item is None
                    item, changes = apply_note(conn, nid, updated, entry, item, prev, today)
                if created or how.startswith("added"):
                    library.refresh()
                    summary["added"].append("{}: {}{}".format(label, how, "; " + ", ".join(changes) if changes else ""))
                elif changes:
                    summary["updated"].append("{}: {}".format(label, ", ".join(changes)))
            except Unclear as u:
                summary["unclear"].append("{}: {}".format(label, u))
                if apply:
                    save_state(conn, nid, updated, entry, "unclear", problem=str(u), candidates=_short(u.candidates))
            except Exception as e:      # noqa: BLE001 - one bad note must not stop the others
                msg = getattr(getattr(e, "diag", None), "message_primary", None) or str(e) or type(e).__name__
                summary["errors"].append("{}: {}".format(label, msg))
        status = "ok"
    finally:
        conn.execute("UPDATE ingest.run SET finished_at = now(), status = %s, summary = %s,"
                     " counts = %s WHERE id = %s",
                     (status, Jsonb(summary),
                      Jsonb({k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()}), run_id))
    return summary


def format_summary(summary):
    parts = []
    for label in ("added", "updated"):
        if summary[label]:
            parts.append("{} {}:\n- {}".format(len(summary[label]), label, "\n- ".join(summary[label])))
    for label, head in (("unclear", "needs a look"), ("warnings", "warnings"), ("errors", "errors")):
        if summary[label]:
            parts.append("{}:\n- {}".format(head, "\n- ".join(summary[label])))
    return "\n".join(parts) or "nothing new ({} notes)".format(summary["notes"])


def load(source_dir=None, tar=None):
    if source_dir:
        return joplin_items.load_dir(source_dir)
    return joplin_items.load_tar(tar)


def main(cfg, source_dir=None, tar=None, apply=False, offline=False, notebook=NOTEBOOK):
    items = load(source_dir, tar)
    if not any(i.get("type_") == joplin_items.TYPE_FOLDER and i["title"].strip().lower() == notebook.lower()
               for i in items):
        raise SystemExit("no notebook named {!r} in the sync folder ({} items read)".format(notebook, len(items)))
    fill = None
    if not offline:
        from ..fill import service
        fill = service.make(cfg.media_dir)
    with db.connect(cfg, db.ROLE_APP) as conn:
        summary = run(conn, items, fill, apply=apply, notebook=notebook)
    print(format_summary(summary))
    if not apply:
        print("dry run: nothing written")
    return 1 if summary["errors"] else 0

