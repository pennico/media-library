"""Owner edits from the console. Every function runs inside one write_tx
(source 'owner', client 'console:<login>'), so each change is labelled as
the owner's, protected from automated sources, and audited.

Input is checked here against explicit allow-lists; the database checks it
again (domains, CHECKs, triggers)."""
import datetime
import re
from zoneinfo import ZoneInfo

import psycopg

from .. import db
from ..covers import CoverError

STATUSES = ("planned", "in_progress", "completed", "paused", "dropped")
READING_STATES = ("in_progress", "paused", "completed", "dropped")
FORMATS = ("ebook", "print", "paperback", "hardcover", "audiobook")
SORT_KEYS = ("manual", "title", "author", "added", "finished", "rating", "published", "pages", "language", "status", "genre")


class BadRequest(ValueError):
    pass


def tx(conn, login):
    return db.write_tx(conn, "owner", "console:" + (login or "unknown"))


# ------------------------------------------------------------ validation

def text(v, field, max_len=1000, required=False):
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise BadRequest("{} cannot be empty".format(field))
        return None
    if not isinstance(v, str):
        raise BadRequest("{} must be text".format(field))
    v = re.sub(r"[ \t]+", " ", v.strip()) if max_len <= 1000 else v.strip()
    if len(v) > max_len:
        raise BadRequest("{} is longer than {} characters".format(field, max_len))
    return v


def partial_date(v, field):
    """'2019' / '2019-05' / '2019-05-07' -> (date, precision); '' -> (None, None)."""
    if v in (None, ""):
        return None, None
    m = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", str(v).strip())
    if not m:
        raise BadRequest("{}: use YYYY, YYYY-MM or YYYY-MM-DD".format(field))
    y, mo, d = m.group(1), m.group(2), m.group(3)
    try:
        if d:
            return datetime.date(int(y), int(mo), int(d)), "day"
        if mo:
            return datetime.date(int(y), int(mo), 1), "month"
        return datetime.date(int(y), 1, 1), "year"
    except ValueError:
        raise BadRequest("{}: {} is not a real date".format(field, v)) from None


def lang(conn, v, field):
    if v in (None, ""):
        return None
    row = conn.execute("SELECT code FROM catalog.language WHERE code = %s", (str(v),)).fetchone()
    if not row:
        raise BadRequest("{}: unknown language {}".format(field, v))
    return row["code"]


def rating(v):
    if v in (None, ""):
        return None
    try:
        r = round(float(v), 1)
    except (TypeError, ValueError):
        raise BadRequest("rating must be a number from 0 to 10") from None
    if not 0 <= r <= 10:
        raise BadRequest("rating must be from 0 to 10")
    return r


def ids_for(conn, pid):
    row = conn.execute("""SELECT i.id, i.kind, ed.id AS edition_id FROM catalog.media_item i
                           LEFT JOIN catalog.edition ed ON ed.work_id = i.id AND ed.is_primary AND ed.deleted_at IS NULL
                          WHERE i.public_id = %s AND i.deleted_at IS NULL""", (pid,)).fetchone()
    if not row:
        raise LookupError("no such item")
    return row


def update(conn, table, key_col, key, cols):
    if not cols:
        return
    sets = ", ".join("{} = %s".format(c) for c in cols)
    conn.execute("UPDATE {} SET {} WHERE {} = %s".format(table, sets, key_col), (*cols.values(), key))


# ------------------------------------------------------------ item edits

def edit_item(conn, login, pid, body):
    """PATCH: {item: {...}, edition: {...}, entry: {...}, authors: [...], genres: [...], tags: [...]}"""
    unknown = set(body) - {"item", "edition", "entry", "authors", "genres", "tags"}
    if unknown:
        raise BadRequest("unknown fields: " + ", ".join(sorted(unknown)))
    with tx(conn, login):
        ids = ids_for(conn, pid)
        it = body.get("item") or {}
        cols = {}
        for k, v in it.items():
            if k == "title":
                cols["title"] = text(v, "title", required=True)
            elif k == "sortTitle":
                cols["sort_title"] = text(v, "sort title", required=True)
            elif k == "originalTitle":
                cols["original_title"] = text(v, "original title")
            elif k == "originalLang":
                cols["original_language"] = lang(conn, v, "original language")
            elif k == "titleLang":
                cols["title_language"] = lang(conn, v, "title language")
            elif k == "firstPublished":
                cols["first_published"], cols["first_published_precision"] = partial_date(v, "first published")
            elif k == "description":
                cols["description"] = text(v, "description", max_len=100000)
            else:
                raise BadRequest("unknown item field " + k)
        update(conn, "catalog.media_item", "id", ids["id"], cols)
        ed = body.get("edition") or {}
        if ed and ids["edition_id"] is None:
            raise BadRequest("this item has no edition")
        cols = {}
        for k, v in ed.items():
            if k in ("title", "subtitle", "publisher"):
                cols[k] = text(v, k, required=(k == "title"))
            elif k == "lang":
                cols["language"] = lang(conn, v, "language")
            elif k == "published":
                cols["published_on"], cols["published_precision"] = partial_date(v, "published")
            elif k == "pages":
                if v in (None, ""):
                    cols["pages"] = None
                else:
                    try:
                        cols["pages"] = int(v)
                    except (TypeError, ValueError):
                        raise BadRequest("pages must be a whole number") from None
                    if not 1 <= cols["pages"] <= 20000:
                        raise BadRequest("pages must be between 1 and 20000")
            elif k == "format":
                cols["format"] = edition_format(v)
            elif k == "description":
                cols["description"] = text(v, "description", max_len=100000)
            else:
                raise BadRequest("unknown edition field " + k)
        update(conn, "catalog.edition", "id", ids["edition_id"], cols)
        en = body.get("entry") or {}
        cols = {}
        for k, v in en.items():
            if k == "rating":
                cols["rating"] = rating(v)
                cols["rated_at"] = datetime.datetime.now(datetime.timezone.utc) if cols["rating"] is not None else None
            elif k == "review":
                cols["review"] = text(v, "review", max_len=100000)
                cols["reviewed_at"] = datetime.datetime.now(datetime.timezone.utc) if cols["review"] else None
            elif k == "added":
                d, p = partial_date(v, "added")
                if d is None or p != "day":
                    raise BadRequest("added: use YYYY-MM-DD")
                if d > today():
                    raise BadRequest("added cannot be in the future")
                # midday in Copenhagen, so the date reads the same in any time zone the console shows it in
                cols["added_at"] = datetime.datetime.combine(d, datetime.time(12), tzinfo=ZoneInfo("Europe/Copenhagen"))
            else:
                raise BadRequest("unknown entry field " + k)
        update(conn, "library.entry", "item_id", ids["id"], cols)
        if "authors" in body:
            set_authors(conn, ids["id"], body["authors"], MAKER.get(ids["kind"], "author"))
        if "genres" in body or "tags" in body:
            for kind in ("genre", "tag"):
                key = kind + "s"
                if key in body:
                    set_tags(conn, ids["id"], kind, body[key])


def edition_format(v, field="format"):
    if v not in (None, "") and v not in FORMATS:
        raise BadRequest("{} must be one of {}".format(field, ", ".join(FORMATS)))
    return v or None


def set_version(conn, login, pid, fmt):
    """A new book's version (the edition's format), chosen when adding it; the
    reading opened by adding it as Reading or Read gets it as its medium."""
    fmt = edition_format(fmt, "version")
    if not fmt:
        return
    edit_item(conn, login, pid, {"edition": {"format": fmt}})
    with tx(conn, login):
        conn.execute("UPDATE library.consumption SET medium = %s WHERE item_id = %s AND medium IS NULL AND deleted_at IS NULL",
                     (fmt, ids_for(conn, pid)["id"]))


MAKER = {"movie": "director", "series": "creator"}     # who the "Authors" field means for movies and series


def set_authors(conn, item, names, role="author"):
    if not isinstance(names, list) or len(names) > 30:
        raise BadRequest("authors must be a list")
    clean = []
    for n in names:
        n = text(n, "author name", max_len=300)
        if n and n not in clean:
            clean.append(n)
    conn.execute("DELETE FROM catalog.contribution WHERE item_id = %s AND role = %s", (item, role))
    for pos, name in enumerate(clean, 1):
        row = conn.execute("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)"
                           " AND disambiguation IS NULL AND deleted_at IS NULL", (name,)).fetchone()
        pid = row["id"] if row else conn.execute(
            "INSERT INTO catalog.person (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
        conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source)"
                     " VALUES (%s, %s, %s, %s, 'owner')", (pid, role, item, pos))


def set_tags(conn, item, kind, names):
    if not isinstance(names, list) or len(names) > 50:
        raise BadRequest(kind + "s must be a list")
    clean = []
    for n in names:
        n = text(n, kind, max_len=100)
        if n and n.lower() not in (c.lower() for c in clean):
            clean.append(n)
    conn.execute("DELETE FROM library.item_tag it USING library.tag t WHERE t.id = it.tag_id AND it.item_id = %s"
                 " AND t.kind = %s", (item, kind))
    for pos, name in enumerate(clean, 1):
        row = conn.execute("SELECT id FROM library.tag WHERE kind = %s AND lower(catalog.f_unaccent(name)) ="
                           " lower(catalog.f_unaccent(%s)) AND deleted_at IS NULL", (kind, name)).fetchone()
        tid = row["id"] if row else conn.execute(
            "INSERT INTO library.tag (kind, name) VALUES (%s, %s) RETURNING id", (kind, name)).fetchone()["id"]
        conn.execute("INSERT INTO library.item_tag (item_id, tag_id, position, source) VALUES (%s, %s, %s, 'owner')",
                     (item, tid, pos))


# ------------------------------------------------------- status, readings

def period_end(d, p):
    """Last day of a partial date's period (mirrors catalog.period_end)."""
    if p == "year":
        return datetime.date(d.year, 12, 31)
    if p == "month":
        nxt = datetime.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return nxt - datetime.timedelta(days=1)
    return d


def today():
    return datetime.date.today()


def sync_status(conn, item):
    """After reading edits: make the entry's status agree with the readings
    (the database checks this at commit)."""
    e = conn.execute("SELECT status FROM library.entry WHERE item_id = %s", (item,)).fetchone()
    rs = conn.execute("SELECT state FROM library.consumption WHERE item_id = %s AND deleted_at IS NULL", (item,)).fetchall()
    states = [r["state"] for r in rs]
    open_state = next((s for s in states if s in ("in_progress", "paused")), None)
    status = e["status"]
    if open_state:
        status = open_state
    elif status in ("in_progress", "paused"):
        status = "completed" if "completed" in states else "planned"
    elif status == "completed" and "completed" not in states:
        status = "dropped" if "dropped" in states else "planned"
    elif status == "dropped" and "dropped" not in states:
        status = "planned"
    if status != e["status"]:
        conn.execute("UPDATE library.entry SET status = %s, status_changed_at = now() WHERE item_id = %s", (status, item))


def set_status(conn, login, pid, body):
    """Change status and open/close the reading in the same transaction.
    body: {status, date?: 'YYYY[-MM[-DD]]'} (date defaults to today)."""
    status = body.get("status")
    if status not in STATUSES:
        raise BadRequest("status must be one of " + ", ".join(STATUSES))
    when, prec = partial_date(body.get("date"), "date")
    if when is None:
        when, prec = today(), "day"
    with tx(conn, login):
        ids = ids_for(conn, pid)
        item = ids["id"]
        cur = conn.execute("SELECT status FROM library.entry WHERE item_id = %s", (item,)).fetchone()["status"]
        open_r = conn.execute("SELECT id, state, started_on FROM library.consumption WHERE item_id = %s AND"
                              " state IN ('in_progress', 'paused') AND deleted_at IS NULL", (item,)).fetchone()
        if status == cur:
            return
        medium = edition_medium(conn, ids["edition_id"])
        if status in ("in_progress", "paused"):
            if open_r:
                conn.execute("UPDATE library.consumption SET state = %s WHERE id = %s", (status, open_r["id"]))
            else:
                conn.execute("INSERT INTO library.consumption (item_id, edition_id, state, started_on, started_precision,"
                             " medium, source) VALUES (%s, %s, %s, %s, %s, %s, 'owner')",
                             (item, ids["edition_id"], status, when, prec, medium))
        elif status in ("completed", "dropped"):
            if open_r:
                if open_r["started_on"] and open_r["started_on"] > period_end(when, prec):
                    raise BadRequest("the reading started after that date")
                conn.execute("UPDATE library.consumption SET state = %s, ended_on = %s, ended_precision = %s WHERE id = %s",
                             (status, when, prec, open_r["id"]))
            else:
                conn.execute("INSERT INTO library.consumption (item_id, edition_id, state, ended_on, ended_precision,"
                             " medium, source) VALUES (%s, %s, %s, %s, %s, %s, 'owner')",
                             (item, ids["edition_id"], status, when, prec, medium))
        elif status == "planned" and open_r:
            # back to "to read": the open reading is set aside, not lost
            conn.execute("UPDATE library.consumption SET deleted_at = now() WHERE id = %s", (open_r["id"],))
        conn.execute("UPDATE library.entry SET status = %s, status_changed_at = now() WHERE item_id = %s", (status, item))


def edition_medium(conn, edition_id):
    """A new reading is read in the book's version, when one is set."""
    if edition_id is None:
        return None
    return conn.execute("SELECT format FROM catalog.edition WHERE id = %s", (edition_id,)).fetchone()["format"]


def whole(v, field, lo, hi):
    if v in (None, ""):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BadRequest("{} must be a whole number".format(field)) from None
    if not lo <= n <= hi:
        raise BadRequest("{} must be from {} to {}".format(field, lo, hi))
    return n


def reading_cols(conn, body, item=None):
    """A reading's / watch's columns. A watch of a series can name its season (by number: created when
    the series doesn't have it yet), the episodes seen, and the platform."""
    cols = {}
    for k, v in body.items():
        if k == "state":
            if v not in READING_STATES:
                raise BadRequest("state must be one of " + ", ".join(READING_STATES))
            cols["state"] = v
        elif k == "started":
            cols["started_on"], cols["started_precision"] = partial_date(v, "started")
        elif k == "ended":
            cols["ended_on"], cols["ended_precision"] = partial_date(v, "finished")
        elif k == "lang":
            cols["language"] = lang(conn, v, "language")
        elif k == "note":
            cols["note"] = text(v, "note", max_len=20000)
        elif k == "medium":
            cols["medium"] = edition_format(v, "read as")
        elif k == "season":
            n = whole(v, "season", 0, 999)
            kind = conn.execute("SELECT kind FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["kind"] if item else None
            if n is not None and kind != "series":
                raise BadRequest("only a series has seasons")
            from .. import screen
            cols["season_id"] = screen.season_id(conn, item, n) if n is not None else None
        elif k == "episodes":
            cols["episodes"] = whole(v, "episodes", 0, 99999)
        elif k == "platform":
            if v in (None, ""):
                cols["platform"] = None
            elif not conn.execute("SELECT 1 AS x FROM library.platform WHERE code = %s", (str(v),)).fetchone():
                raise BadRequest("unknown platform " + str(v))
            else:
                cols["platform"] = v
        else:
            raise BadRequest("unknown reading field " + k)
    if cols.get("state") in ("in_progress", "paused"):
        cols["ended_on"] = cols["ended_precision"] = None
    if cols.get("episodes") is not None and not cols.get("season_id") and "season_id" in cols:
        raise BadRequest("episodes go with a season")
    return cols


def add_reading(conn, login, pid, body):
    with tx(conn, login):
        ids = ids_for(conn, pid)
        cols = reading_cols(conn, dict({"state": "completed"}, **body), ids["id"])
        if cols.get("episodes") is not None and not cols.get("season_id"):
            raise BadRequest("episodes go with a season")
        if "medium" not in body:
            cols["medium"] = edition_medium(conn, ids["edition_id"])
        cols.update(item_id=ids["id"], edition_id=ids["edition_id"], source="owner")
        names = list(cols)
        conn.execute("INSERT INTO library.consumption ({}) VALUES ({})".format(", ".join(names), ", ".join(["%s"] * len(names))),
                     list(cols.values()))
        if cols["state"] == "completed":           # a finished reading added to a book still "to read": it has been read
            conn.execute("UPDATE library.entry SET status = 'completed', status_changed_at = now()"
                         " WHERE item_id = %s AND status = 'planned'", (ids["id"],))
        sync_status(conn, ids["id"])


def edit_reading(conn, login, rid, body, delete=False):
    with tx(conn, login):
        row = conn.execute("SELECT id, item_id FROM library.consumption WHERE public_id = %s AND deleted_at IS NULL",
                           (rid,)).fetchone()
        if not row:
            raise LookupError("no such reading")
        if delete:
            conn.execute("UPDATE library.consumption SET deleted_at = now() WHERE id = %s", (row["id"],))
        else:
            cols = reading_cols(conn, body, row["item_id"])
            if "season_id" in cols and cols["season_id"] is None:
                cols["episodes"] = None
            update(conn, "library.consumption", "id", row["id"], cols)
        sync_status(conn, row["item_id"])


# ------------------------------------------------------------------ covers

def choose_cover(conn, login, pid, body, store):
    try:
        cid = int(body.get("cover"))
    except (TypeError, ValueError):
        raise BadRequest("cover must be a cover id") from None
    ids = ids_for(conn, pid)
    row = conn.execute("SELECT id, source_url, local_path FROM catalog.cover WHERE id = %s AND deleted_at IS NULL"
                       " AND (edition_id = %s OR item_id = %s)", (cid, ids["edition_id"], ids["id"])).fetchone()
    if not row:
        raise LookupError("no such cover for this item")
    info = None
    if not row["local_path"]:
        try:
            info = store.fetch(row["source_url"])        # network outside the transaction
        except CoverError as e:
            raise BadRequest("could not download that cover: {}".format(e)) from None
    with tx(conn, login):
        if info:
            conn.execute("UPDATE catalog.cover SET local_path = %s, sha256 = %s, mime = %s, width = %s, height = %s,"
                         " bytes = %s, fetched_at = now() WHERE id = %s",
                         (info["local_path"], info["sha256"], info["mime"], info["width"], info["height"], info["bytes"], cid))
        conn.execute("UPDATE catalog.cover SET is_chosen = false WHERE is_chosen AND deleted_at IS NULL"
                     " AND (edition_id = %s OR item_id = %s) AND id <> %s", (ids["edition_id"], ids["id"], cid))
        conn.execute("UPDATE catalog.cover SET is_chosen = true WHERE id = %s", (cid,))


# ----------------------------------------------------------------- shelves

def shelf_row(conn, sid):
    row = conn.execute("SELECT * FROM library.shelf WHERE public_id = %s AND deleted_at IS NULL", (sid,)).fetchone()
    if not row:
        raise LookupError("no such collection")
    return row


def create_shelf(conn, login, body):
    name = text(body.get("name"), "name", required=True)
    kind = body.get("mediaKind", "book")
    if kind not in ("book", "movie", "series"):
        raise BadRequest("mediaKind must be book, movie or series")
    with tx(conn, login):
        row = conn.execute("INSERT INTO library.shelf (name, kind, media_kind, position) VALUES (%s, 'manual', %s,"
                           " (SELECT coalesce(max(position), 0) + 1 FROM library.shelf WHERE media_kind = %s))"
                           " RETURNING public_id::text AS id", (name, kind, kind)).fetchone()
    return row["id"]


def edit_shelf(conn, login, sid, body):
    cols = {}
    for k, v in body.items():
        if k == "name":
            cols["name"] = text(v, "name", required=True)
        elif k == "sortKey":
            if v not in SORT_KEYS:
                raise BadRequest("sortKey must be one of " + ", ".join(SORT_KEYS))
            cols["sort_key"] = v
        elif k == "sortDesc":
            cols["sort_desc"] = bool(v)
        elif k == "coverSize":
            if v not in ("s", "m", "l"):
                raise BadRequest("coverSize must be s, m or l")
            cols["cover_size"] = v
        elif k == "layout":
            if v not in ("grid", "list"):
                raise BadRequest("layout must be grid or list")
            cols["layout"] = v
        elif k == "sortLocale":
            if v not in ("und", "da", "it", "en"):
                raise BadRequest("sortLocale must be und, da, it or en")
            cols["sort_locale"] = v
        else:
            raise BadRequest("unknown collection field " + k)
    with tx(conn, login):
        s = shelf_row(conn, sid)
        if s["kind"] == "all" and "name" in cols:
            raise BadRequest("the built-in collection keeps its name")
        update(conn, "library.shelf", "id", s["id"], cols)


def delete_shelf(conn, login, sid):
    with tx(conn, login):
        s = shelf_row(conn, sid)
        if s["kind"] == "all":
            raise BadRequest("the built-in collection cannot be deleted")
        conn.execute("UPDATE library.shelf SET deleted_at = now() WHERE id = %s", (s["id"],))


def shelf_items(conn, login, sid, body):
    """{add: [item ids], remove: [item ids]}"""
    with tx(conn, login):
        s = shelf_row(conn, sid)
        for pid in body.get("add") or []:
            ids = ids_for(conn, pid)
            if conn.execute("SELECT 1 AS x FROM library.shelf_item WHERE shelf_id = %s AND item_id = %s",
                            (s["id"], ids["id"])).fetchone():
                continue
            conn.execute("INSERT INTO library.shelf_item (shelf_id, item_id, position) SELECT %s, %s,"
                         " coalesce(max(position), 0) + 1 FROM library.shelf_item WHERE shelf_id = %s",
                         (s["id"], ids["id"], s["id"]))
        for pid in body.get("remove") or []:
            if s["kind"] == "all":
                raise BadRequest("books stay in the built-in collection")
            ids = ids_for(conn, pid)
            row = conn.execute("DELETE FROM library.shelf_item WHERE shelf_id = %s AND item_id = %s RETURNING position",
                               (s["id"], ids["id"])).fetchone()
            if row:
                conn.execute("UPDATE library.shelf_item SET position = position - 1 WHERE shelf_id = %s AND position > %s",
                             (s["id"], row["position"]))


def move(conn, login, sid, body):
    """Drag reorder: {item, to} with `to` the new 1-based position."""
    try:
        to = int(body.get("to"))
    except (TypeError, ValueError):
        raise BadRequest("to must be a position") from None
    with tx(conn, login):
        s = shelf_row(conn, sid)
        ids = ids_for(conn, body.get("item"))
        cur = conn.execute("SELECT position FROM library.shelf_item WHERE shelf_id = %s AND item_id = %s",
                           (s["id"], ids["id"])).fetchone()
        if not cur:
            raise LookupError("that item is not in this collection")
        n = conn.execute("SELECT count(*) AS n FROM library.shelf_item WHERE shelf_id = %s", (s["id"],)).fetchone()["n"]
        to = max(1, min(n, to))
        frm = cur["position"]
        if to == frm:
            return
        if to < frm:
            conn.execute("UPDATE library.shelf_item SET position = position + 1 WHERE shelf_id = %s"
                         " AND position >= %s AND position < %s", (s["id"], to, frm))
        else:
            conn.execute("UPDATE library.shelf_item SET position = position - 1 WHERE shelf_id = %s"
                         " AND position > %s AND position <= %s", (s["id"], frm, to))
        conn.execute("UPDATE library.shelf_item SET position = %s WHERE shelf_id = %s AND item_id = %s",
                     (to, s["id"], ids["id"]))


# ---------------------------------------------------------- add by hand

def add_manual(conn, login, body):
    """A book the providers don't know: everything typed by the owner.
    body: {title, authors: [..] | "A; B", language, published, pages,
    publisher, isbn, status}"""
    title = text(body.get("title"), "title", required=True)
    authors = body.get("authors") or []
    if isinstance(authors, str):
        authors = [a for a in re.split(r"\s*[;\n]\s*", authors) if a.strip()]
    status = body.get("status") or "planned"
    if status not in ("planned", "in_progress", "completed"):
        raise BadRequest("status must be planned, in_progress or completed")
    isbn = None
    if body.get("isbn"):
        from ..text import normalize_isbn
        isbn = normalize_isbn(body["isbn"])
        if not isbn:
            raise BadRequest("that ISBN is not valid (check the digits)")
    language = lang(conn, body.get("language"), "language")
    published, precision = partial_date(body.get("published"), "published")
    pages = None
    if body.get("pages") not in (None, ""):
        try:
            pages = int(body["pages"])
        except (TypeError, ValueError):
            raise BadRequest("pages must be a whole number") from None
        if not 1 <= pages <= 20000:
            raise BadRequest("pages must be between 1 and 20000")
    publisher = text(body.get("publisher"), "publisher")
    if isbn:
        row = conn.execute("""SELECT i.public_id::text AS id, i.deleted_at IS NOT NULL AS trashed
                                FROM catalog.external_id x JOIN catalog.edition e ON e.id = x.edition_id
                               JOIN catalog.media_item i ON i.id = e.work_id
                              WHERE x.provider = 'isbn13' AND x.value = %s""", (isbn,)).fetchone()
        if row and row["trashed"]:
            raise BadRequest("that book is in the trash: restore it from Trash")
        if row:
            return {"id": row["id"], "existing": True}
    with tx(conn, login):
        it = conn.execute("INSERT INTO catalog.media_item (kind, title, title_language) VALUES ('book', %s, %s)"
                          " RETURNING id, public_id::text AS pid", (title, language)).fetchone()
        conn.execute("INSERT INTO catalog.book (item_id) VALUES (%s)", (it["id"],))
        eid = conn.execute("INSERT INTO catalog.edition (work_id, title, language, publisher, published_on, published_precision,"
                           " pages, is_primary) VALUES (%s, %s, %s, %s, %s, %s, %s, true) RETURNING id",
                           (it["id"], title, language, publisher, published, precision, pages)).fetchone()["id"]
        set_authors(conn, it["id"], authors)
        if isbn:
            conn.execute("INSERT INTO catalog.external_id (provider, value, edition_id, source) VALUES ('isbn13', %s, %s, 'owner')",
                         (isbn, eid))
        conn.execute("INSERT INTO library.entry (item_id, status) VALUES (%s, %s)", (it["id"], status))
        if status in ("in_progress", "completed"):
            d = today()
            conn.execute("INSERT INTO library.consumption (item_id, edition_id, state, started_on, started_precision,"
                         " ended_on, ended_precision, source) VALUES (%s, %s, %s, %s, %s, %s, %s, 'owner')",
                         (it["id"], eid, status, d if status == "in_progress" else None, "day" if status == "in_progress" else None,
                          d if status == "completed" else None, "day" if status == "completed" else None))
    return {"id": it["pid"], "existing": False}


# ------------------------------------------------------------ uploads

def upload_cover(conn, login, pid, data, store, note="uploaded by you"):
    """The owner's own cover image (JPEG/PNG/WebP, checked by Pillow)."""
    from ..covers import CoverError
    try:
        info = store.put(data)
    except CoverError as e:
        raise BadRequest("that is not a usable image: {}".format(e)) from None
    ids = ids_for(conn, pid)
    col, owner_id = ("edition_id", ids["edition_id"]) if ids["edition_id"] else ("item_id", ids["id"])
    with tx(conn, login):
        row = conn.execute("SELECT id FROM catalog.cover WHERE {} = %s AND sha256 = %s AND deleted_at IS NULL".format(col),
                           (owner_id, info["sha256"])).fetchone()
        conn.execute("UPDATE catalog.cover SET is_chosen = false WHERE {} = %s AND is_chosen AND deleted_at IS NULL".format(col),
                     (owner_id,))
        if row:
            conn.execute("UPDATE catalog.cover SET is_chosen = true WHERE id = %s", (row["id"],))
        else:
            conn.execute("INSERT INTO catalog.cover ({}, source, local_path, sha256, mime, width, height, bytes, fetched_at,"
                         " is_chosen, note) VALUES (%s, 'owner', %s, %s, %s, %s, %s, %s, now(), true, %s)".format(col),
                         (owner_id, info["local_path"], info["sha256"], info["mime"], info["width"], info["height"], info["bytes"], note))


def check_epub(path):
    """A real EPUB: a zip whose first entry 'mimetype' says application/epub+zip."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if not names or names[0] != "mimetype":
                raise BadRequest("not an EPUB (the first entry must be 'mimetype')")
            if z.read("mimetype").strip() != b"application/epub+zip":
                raise BadRequest("not an EPUB (wrong mimetype)")
            if "META-INF/container.xml" not in names:
                raise BadRequest("not an EPUB (META-INF/container.xml is missing)")
    except zipfile.BadZipFile:
        raise BadRequest("not an EPUB (not a zip file)") from None


def attach_file(conn, login, pid, tmp_path, sha, media_dir):
    """Store an uploaded EPUB (content-addressed, storage 'store') and attach
    it to the book's primary edition."""
    import os
    import shutil
    from .. import files
    check_epub(tmp_path)
    ids = ids_for(conn, pid)
    if not ids["edition_id"]:
        raise BadRequest("only books take ebook files")
    dup = conn.execute("""SELECT i.title FROM catalog.edition_file f JOIN catalog.edition e ON e.id = f.edition_id
                           JOIN catalog.media_item i ON i.id = e.work_id
                          WHERE f.sha256 = %s AND f.deleted_at IS NULL""", (sha,)).fetchone()
    if dup:
        raise BadRequest("this file is already in the library (\"{}\")".format(dup["title"]))
    rel = "files/{}/{}.epub".format(sha[:2], sha)
    dest = os.path.join(media_dir, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        shutil.move(tmp_path, dest)
        os.chmod(dest, 0o640)
    md5 = files.kosync_partial_md5(dest)
    with tx(conn, login):
        conn.execute("INSERT INTO catalog.edition_file (edition_id, format, storage, rel_path, size_bytes, sha256, kosync_md5, added_at)"
                     " VALUES (%s, 'epub', 'store', %s, %s, %s, %s, now())",
                     (ids["edition_id"], rel, os.path.getsize(dest), sha, md5))


# ------------------------------------------------------------------- trash

TRASH_DAYS = 30


def _trashed(conn, pid):
    row = conn.execute("SELECT id, title FROM catalog.media_item WHERE public_id = %s AND deleted_at IS NOT NULL"
                       " AND kind IN ('book', 'movie', 'series')", (pid,)).fetchone()
    if not row:
        raise LookupError("no such item in the trash")
    return row


def trash_item(conn, login, pid):
    """Move to the trash: hidden everywhere, back with restore_item for 30 days."""
    with tx(conn, login):
        ids = ids_for(conn, pid)
        if ids["kind"] not in ("book", "movie", "series"):
            raise BadRequest("only books, movies and series go to the trash")
        conn.execute("UPDATE library.entry SET deleted_at = now() WHERE item_id = %s", (ids["id"],))
        conn.execute("UPDATE catalog.media_item SET deleted_at = now() WHERE id = %s", (ids["id"],))


def restore_item(conn, login, pid):
    with tx(conn, login):
        row = _trashed(conn, pid)
        conn.execute("UPDATE catalog.media_item SET deleted_at = NULL WHERE id = %s", (row["id"],))
        conn.execute("UPDATE library.entry SET deleted_at = NULL WHERE item_id = %s", (row["id"],))


def remove_media(media_dir, paths):
    """Files nothing points at any more (EPUBs, covers and their thumbnails)."""
    import glob
    import os
    for rel in paths:
        full = os.path.normpath(os.path.join(media_dir, rel))
        if not full.startswith(os.path.normpath(media_dir) + os.sep):
            continue
        try:
            os.remove(full)
        except OSError:
            pass
        if rel.startswith("covers/"):
            sha = os.path.splitext(os.path.basename(rel))[0]
            for thumb in glob.glob(os.path.join(media_dir, "thumbs", sha + "-*.jpg")):
                try:
                    os.remove(thumb)
                except OSError:
                    pass


def purge_item(conn, client, item_id, media_dir):
    """Delete one trashed item for good (database function, then its files)."""
    with db.write_tx(conn, "owner", client, reason="deleted for good"):
        paths = [r["path"] for r in conn.execute("SELECT path FROM library.purge_item(%s)", (item_id,)).fetchall()]
    remove_media(media_dir, paths)
    return paths


def purge_now(conn, login, pid, media_dir):
    row = _trashed(conn, pid)
    purge_item(conn, "console:" + (login or "unknown"), row["id"], media_dir)
    return row["title"]


def purge_trash(conn, media_dir, days=TRASH_DAYS, apply=False, log=print):
    """Everything in the trash for longer than `days` goes for good (cron, daily)."""
    rows = conn.execute("SELECT id, title, deleted_at FROM catalog.media_item WHERE deleted_at < now() - make_interval(days => %s)"
                        " AND kind IN ('book', 'movie', 'series') ORDER BY deleted_at", (days,)).fetchall()
    for r in rows:
        if apply:
            purge_item(conn, "trash", r["id"], media_dir)
        log("{} {} (in the trash since {:%Y-%m-%d})".format("deleted" if apply else "would delete", r["title"],
                                                            r["deleted_at"]))
    if not rows:
        log("nothing in the trash older than {} days".format(days))
    return len(rows)


# --------------------------------------------------------- add from an EPUB

def epub_summary(meta, filename=None):
    """What the file says, cleaned for the console (and kept beside the upload)."""
    from .. import epubmeta
    title, authors = meta.get("title"), list(meta.get("authors") or [])
    if epubmeta.junk_title(title) and filename:
        guess_t, guess_a = epubmeta.title_from_filename(filename)
        title, authors = guess_t or title, authors or ([guess_a] if guess_a else [])
    return {"title": title, "subtitle": meta.get("subtitle"), "authors": authors, "language": meta.get("language"),
            "isbns": list(dict.fromkeys((meta.get("isbns") or []) + (meta.get("source_isbns") or []))),
            "publisher": meta.get("publisher"), "published": meta.get("published"), "series": meta.get("series"),
            "seriesIndex": meta.get("series_index"), "description": (meta.get("description") or "")[:600] or None,
            "subjects": (meta.get("subjects") or [])[:10], "hasCover": bool(meta.get("cover"))}


def add_from_epub(conn, login, path, summary, sha, media_dir, store, fill=None, token=None, index=None, status="planned"):
    """Create the book (a picked candidate, or what the file says), then attach
    the file and, if the book has no cover yet, the cover inside the EPUB."""
    from .. import epubmeta
    if index is not None:
        r = fill.add(conn, login, token, index, status)
    else:
        language = summary.get("language")
        if language and not conn.execute("SELECT 1 AS x FROM catalog.language WHERE code = %s", (language,)).fetchone():
            language = None
        r = add_manual(conn, login, {"title": summary.get("title"), "authors": summary.get("authors") or [],
                                     "language": language, "published": summary.get("published"),
                                     "publisher": summary.get("publisher"),
                                     "isbn": (summary.get("isbns") or [None])[0], "status": status})
    pid = r["id"]
    meta = epubmeta.read(path)                 # the cover bytes are not kept in the summary
    attach_file(conn, login, pid, path, sha, media_dir)
    ids = ids_for(conn, pid)
    if not conn.execute("SELECT 1 AS x FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id"
                        " WHERE it.item_id = %s AND t.kind = 'genre'", (ids["id"],)).fetchone():
        from .. import categories
        cats = categories.classify([], summary.get("subjects") or [])[0]
        if cats:
            with db.write_tx(conn, "categorize", "console:" + (login or "unknown"), reason="categories from the EPUB"):
                categories.assign(conn, ids["id"], cats)
    has_cover = conn.execute("SELECT 1 AS x FROM catalog.cover WHERE is_chosen AND deleted_at IS NULL"
                             " AND (edition_id = %s OR item_id = %s)", (ids["edition_id"], ids["id"])).fetchone()
    if not has_cover and meta.get("cover"):
        try:
            upload_cover(conn, login, pid, meta["cover"]["data"], store, note="from the EPUB")
        except BadRequest:
            pass                                # not a usable image: the book stays without a cover
    return dict(r, id=pid)


# ------------------------------------------------- movies and series (TMDB)

def add_screen(conn, login, kind, details, covers, status="planned"):
    """A movie or series from TMDB details (posters already downloaded): the
    catalogue values labelled tmdb, the entry and its watch the owner's."""
    from .. import screen
    if kind not in ("movie", "series"):
        raise BadRequest("kind must be movie or series")
    if status not in ("planned", "in_progress", "completed"):
        raise BadRequest("status must be planned, in_progress or completed")
    p = screen.parse(kind, details)
    found, trashed = screen.find(conn, kind, p["tmdb"])
    if trashed:
        raise BadRequest("that one is in the trash: restore it from Trash")
    if found:
        return {"id": str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (found,)).fetchone()["public_id"]),
                "existing": True}
    with db.write_tx(conn, "tmdb", "console:" + (login or "unknown"), reason="add from TMDB"):
        item, _ = screen.write(conn, kind, p, covers)
        db.set_source(conn, "owner")
        conn.execute("INSERT INTO library.entry (item_id, status) VALUES (%s, %s)", (item, status))
        if status != "planned":
            d = today()
            conn.execute("INSERT INTO library.consumption (item_id, state, started_on, started_precision, ended_on,"
                         " ended_precision, source) VALUES (%s, %s, %s, %s, %s, %s, 'owner')",
                         (item, status, d if status == "in_progress" else None, "day" if status == "in_progress" else None,
                          d if status == "completed" else None, "day" if status == "completed" else None))
        pid = conn.execute("SELECT public_id::text AS id FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["id"]
    return {"id": pid, "existing": False}


def screen_posters(store, kind, p, seasons=()):
    """Download the poster (and the given seasons' posters) before the transaction; -> {url: info}."""
    urls = [p["poster"]] + [s["poster"] for s in p.get("seasons") or [] if s["number"] in seasons]
    got = {}
    for u in [u for u in urls if u]:
        try:
            got[u] = store.fetch(u)
        except CoverError:
            pass
    return got


def fill_screen(conn, login, pid, tmdb, store):
    """Fill what a movie/series lacks from TMDB: by its TMDB id, else by a title
    (and year) TMDB can only mean one way. -> {changes, note}."""
    from .. import screen
    from ..fill import tmdb as T
    row = conn.execute("""SELECT i.id, i.kind, i.title, i.locked,
                                 extract(year FROM coalesce(m.release_date, s.first_air_date, i.first_published))::int AS year,
                                 (SELECT x.value FROM catalog.external_id x WHERE x.item_id = i.id
                                   AND x.provider IN ('tmdb_movie', 'tmdb_tv')) AS tmdb
                            FROM catalog.media_item i LEFT JOIN catalog.movie m ON m.item_id = i.id
                            LEFT JOIN catalog.series s ON s.item_id = i.id
                           WHERE i.public_id = %s AND i.deleted_at IS NULL""", (pid,)).fetchone()
    if not row:
        raise LookupError("no such item")
    if row["kind"] not in ("movie", "series"):
        raise BadRequest("only movies and series are filled from TMDB")
    if row["locked"]:
        raise BadRequest("this item is locked")
    tid = row["tmdb"]
    if not tid:
        hit = tmdb.confident(row["kind"], row["title"], row["year"])
        if not hit:
            return {"changes": [], "note": "TMDB has no single match for this title{}: add it with + Add, or set the year"
                    .format(" and year" if row["year"] else "")}
        tid = hit["tmdb"]
    d = tmdb.details(row["kind"], tid)
    if not d:
        return {"changes": [], "note": "TMDB doesn't know {} {} any more".format(T.PATH[row["kind"]], tid)}
    p = screen.parse(row["kind"], d)
    watched = {r["season_number"] for r in conn.execute(
        "SELECT s.season_number FROM library.consumption c JOIN catalog.season s ON s.item_id = c.season_id"
        " WHERE c.item_id = %s AND c.deleted_at IS NULL", (row["id"],)).fetchall()}
    covers = screen_posters(store, row["kind"], p, watched)
    with db.write_tx(conn, "tmdb", "console:" + (login or "unknown"), reason="fill from TMDB"):
        _, changes = screen.write(conn, row["kind"], p, covers, item=row["id"])
    return {"changes": [{"field": c} for c in changes]}


# ------------------------------------------------------------- sections (D50)

def section_row(conn, sid):
    row = conn.execute("SELECT * FROM library.section WHERE public_id = %s AND deleted_at IS NULL", (sid,)).fetchone()
    if not row:
        raise LookupError("no such section")
    return row


def create_section(conn, login, body):
    name = text(body.get("name"), "name", required=True)
    if len(name) > 100:
        raise BadRequest("name is longer than 100 characters")
    with tx(conn, login):
        row = conn.execute("INSERT INTO library.section (name, position) VALUES (%s,"
                           " (SELECT coalesce(max(position), 0) + 1 FROM library.section WHERE kind = 'manual'))"
                           " RETURNING public_id::text AS id", (name,)).fetchone()
    return row["id"]


def edit_section(conn, login, sid, body):
    cols = {}
    for k, v in body.items():
        if k == "name":
            cols["name"] = text(v, "name", required=True)
        elif k == "position":
            cols["position"] = whole(v, "position", 1, 10000)
        else:
            raise BadRequest("unknown section field " + k)
    with tx(conn, login):
        s = section_row(conn, sid)
        if s["kind"] != "manual" and "name" in cols:
            raise BadRequest("the built-in sections keep their names")
        update(conn, "library.section", "id", s["id"], cols)


def delete_section(conn, login, sid):
    """The section goes; what was in it goes back to the main library (nothing is deleted)."""
    with tx(conn, login):
        s = section_row(conn, sid)
        if s["kind"] != "manual":
            raise BadRequest("the built-in sections cannot be deleted")
        conn.execute("UPDATE library.entry SET section_id = NULL WHERE section_id = %s", (s["id"],))
        conn.execute("UPDATE library.section SET deleted_at = now() WHERE id = %s", (s["id"],))


def builtin_section(conn, kind):
    return conn.execute("SELECT public_id::text AS id FROM library.section WHERE kind = %s AND deleted_at IS NULL",
                        (kind,)).fetchone()["id"]


def set_section(conn, login, pids, sid):
    """Move items into a section (sid None: back to the main library)."""
    with tx(conn, login):
        target = section_row(conn, sid)["id"] if sid else None
        for pid in pids:
            conn.execute("UPDATE library.entry SET section_id = %s WHERE item_id = %s AND section_id IS DISTINCT FROM %s",
                         (target, ids_for(conn, pid)["id"], target))


# ---------------------------------------------------------- bulk (0.8.0)

BULK_ACTIONS = ("trash", "section", "archive", "unarchive", "status", "edit", "collection")
BULK_MAX = 2000


def bulk(conn, login, body):
    """One action on many items: {items: [ids], action, ...}. Each item goes through the same
    write as a single edit (provenance, owner label, audit). -> {done, errors: [{id, title, error}]}.
    edit: {fields: {status, rating, genres, tags (added), format, lang, added, ended, collection}}: only
    the fields given change; the rest of each item stays as it is."""
    items, action = body.get("items"), body.get("action")
    if not isinstance(items, list) or not items or len(items) > BULK_MAX or not all(isinstance(x, str) for x in items):
        raise BadRequest("items must be a list of item ids")
    if action not in BULK_ACTIONS:
        raise BadRequest("action must be one of " + ", ".join(BULK_ACTIONS))
    items = list(dict.fromkeys(items))
    if action in ("section", "archive", "unarchive"):
        sid = body.get("section") if action == "section" else builtin_section(conn, "archive") if action == "archive" else None
        set_section(conn, login, items, sid or None)
        return {"done": len(items), "errors": []}
    fields = body.get("fields") or {}
    if action == "edit":
        unknown = set(fields) - {"status", "rating", "genres", "tags", "format", "lang", "added", "ended", "collection"}
        if unknown:
            raise BadRequest("unknown fields: " + ", ".join(sorted(unknown)))
        if not fields:
            raise BadRequest("nothing to change")
    if action == "status" and body.get("status") not in STATUSES:
        raise BadRequest("status must be one of " + ", ".join(STATUSES))
    done, errors = 0, []
    for pid in items:
        try:
            if action == "trash":
                trash_item(conn, login, pid)
            elif action == "status":
                set_status(conn, login, pid, {"status": body["status"]})
            elif action == "collection":
                shelf_items(conn, login, body.get("shelf"), {"add": [pid]})
            else:
                edit_one(conn, login, pid, fields)
            done += 1
        except (BadRequest, LookupError) as e:
            errors.append({"id": pid, "title": title_of(conn, pid), "error": str(e)})
        except psycopg.Error as e:           # a database rule (a movie into a book collection…): that item only
            errors.append({"id": pid, "title": title_of(conn, pid), "error": e.diag.message_primary or type(e).__name__})
    return {"done": done, "errors": errors}


def title_of(conn, pid):
    row = conn.execute("SELECT title FROM catalog.media_item WHERE public_id = %s", (pid,)).fetchone()
    return row["title"] if row else pid


def edit_one(conn, login, pid, f):
    """The "apply to all" form for one item: only the given fields; book-only ones skip movies and series."""
    kind = ids_for(conn, pid)["kind"]
    body = {}
    if "rating" in f:
        body.setdefault("entry", {})["rating"] = f["rating"]
    if "added" in f:
        body.setdefault("entry", {})["added"] = f["added"]
    if "genres" in f:
        body["genres"] = f["genres"]
    if "tags" in f:
        have = [r["name"] for r in conn.execute(
            "SELECT t.name FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id WHERE it.item_id = %s"
            " AND t.kind = 'tag' AND t.deleted_at IS NULL ORDER BY it.position", (ids_for(conn, pid)["id"],))]
        body["tags"] = have + [t for t in f["tags"] if t.lower() not in (h.lower() for h in have)]
    if kind == "book" and ("format" in f or "lang" in f):
        ed = body.setdefault("edition", {})
        if "format" in f:
            ed["format"] = f["format"]
        if "lang" in f:
            ed["lang"] = f["lang"]
    if body:
        edit_item(conn, login, pid, body)
    if f.get("ended"):                         # moves the last finished reading / watch; with none, adds one
        last = conn.execute("SELECT public_id::text AS rid, started_on FROM library.consumption WHERE item_id = %s"
                            " AND state = 'completed' AND deleted_at IS NULL ORDER BY ended_on DESC NULLS LAST, id DESC LIMIT 1",
                            (ids_for(conn, pid)["id"],)).fetchone()
        if last:
            ended, _ = partial_date(f["ended"], "finished")
            change = {"ended": f["ended"]}
            if last["started_on"] and last["started_on"] > ended:   # moved before it started: the start moves with it
                change["started"] = f["ended"]
            edit_reading(conn, login, last["rid"], change)
        else:
            add_reading(conn, login, pid, {"state": "completed", "ended": f["ended"]})
    if f.get("status"):
        set_status(conn, login, pid, {"status": f["status"]})
    if f.get("collection"):
        shelf_items(conn, login, f["collection"], {"add": [pid]})
