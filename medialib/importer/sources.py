"""Read the snapshot (DESIGN.md §0): Yamtrack, CWA, library-sync state,
owner covers. Everything is opened read-only; nothing here writes."""
import collections
import dataclasses
import hashlib
import json
import os
import sqlite3

YAMTRACK = "yamtrack.sqlite3"
CWA_APP = "cwa-app.db"
CALIBRE = os.path.join("calibre-library", "metadata.db")
STATE = "library-sync-state"
OWNER_COVERS = "owner-covers"


def _ro(path):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    db = sqlite3.connect("file:{}?mode=ro&immutable=1".format(path), uri=True)
    db.row_factory = sqlite3.Row
    return db


def _json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclasses.dataclass
class Snapshot:
    root: str
    yt_items: dict          # app_item.id -> row
    yt_books: dict          # app_book.id -> row
    yt_history: list        # app_historicalbook rows, by history_id
    yt_user_count: int
    cwa: dict               # books.id -> joined record
    cwa_app_counts: dict    # table -> rows (all dropped, counted)
    links: dict             # cwa id (str) -> link
    apple_applied: dict
    apple_keep: list
    apple_cache: dict
    search_cache: dict
    cwa_migration: dict
    sync_state: dict
    owner_covers: dict      # file name -> {path, sha256, bytes}
    files: dict             # input file -> {sha256, bytes, mtime}

    def ol_details(self, media_id):
        """Open Library data Yamtrack fetched for this edition (search-cache)."""
        return self._ol.get(media_id)

    def __post_init__(self):
        self._ol = {}
        for _query, cands in (self.search_cache or {}).items():
            if not isinstance(cands, list):
                continue
            for c in cands:
                if c.get("source") == "openlibrary" and c.get("media_id"):
                    self._ol.setdefault(c["media_id"], c)


def read_calibre(path):
    db = _ro(path)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    recs = {}
    for r in q("SELECT * FROM books ORDER BY id"):
        recs[r["id"]] = dict(r, authors=[], tags=[], languages=[], publishers=[], series=[],
                             identifiers={}, rating=None, comments=None, custom={}, data=[],
                             checksums=[], pages_link=None)
    for r in q("SELECT l.book, a.name, a.sort FROM books_authors_link l JOIN authors a ON a.id = l.author ORDER BY l.id"):
        recs[r["book"]]["authors"].append({"name": r["name"], "sort": r["sort"]})
    for r in q("SELECT l.book, t.name FROM books_tags_link l JOIN tags t ON t.id = l.tag ORDER BY l.id"):
        recs[r["book"]]["tags"].append(r["name"])
    for r in q("SELECT l.book, x.lang_code FROM books_languages_link l JOIN languages x ON x.id = l.lang_code"
               " ORDER BY l.item_order"):
        recs[r["book"]]["languages"].append(r["lang_code"])
    for r in q("SELECT l.book, p.name FROM books_publishers_link l JOIN publishers p ON p.id = l.publisher"):
        recs[r["book"]]["publishers"].append(r["name"])
    for r in q("SELECT l.book, s.name FROM books_series_link l JOIN series s ON s.id = l.series"):
        recs[r["book"]]["series"].append(r["name"])
    for r in q("SELECT book, type, val FROM identifiers ORDER BY id"):
        recs[r["book"]]["identifiers"][r["type"]] = r["val"]
    for r in q("SELECT l.book, r.rating FROM books_ratings_link l JOIN ratings r ON r.id = l.rating"):
        recs[r["book"]]["rating"] = r["rating"]
    for r in q("SELECT book, text FROM comments"):
        recs[r["book"]]["comments"] = r["text"]
    for r in q("SELECT book, format, name, uncompressed_size FROM data ORDER BY id"):
        recs[r["book"]]["data"].append(dict(r))
    for r in q("SELECT id, book, format, checksum, version, created FROM book_format_checksums ORDER BY id"):
        if r["book"] in recs:
            recs[r["book"]]["checksums"].append(dict(r))
    for r in q("SELECT book, pages, algorithm FROM books_pages_link"):
        if r["book"] in recs:
            recs[r["book"]]["pages_link"] = {"pages": r["pages"], "algorithm": r["algorithm"]}
    for col in q("SELECT id, label, datatype, is_multiple FROM custom_columns"):
        cid, label = col["id"], col["label"]
        if col["datatype"] in ("text", "series", "enumeration") or col["is_multiple"]:
            rows = q("SELECT l.book, v.value FROM books_custom_column_{0}_link l"
                     " JOIN custom_column_{0} v ON v.id = l.value".format(cid))
        else:
            rows = q("SELECT book, value FROM custom_column_{}".format(cid))
        for r in rows:
            if r["book"] in recs:
                recs[r["book"]]["custom"][label] = r["value"]
    db.close()
    return recs


def read(root):
    root = os.path.abspath(root)
    yt = _ro(os.path.join(root, YAMTRACK))
    items = {r["id"]: dict(r) for r in yt.execute("SELECT * FROM app_item ORDER BY id")}
    books = {r["id"]: dict(r) for r in yt.execute("SELECT * FROM app_book ORDER BY id")}
    history = [dict(r) for r in yt.execute("SELECT * FROM app_historicalbook ORDER BY history_id")]
    users = yt.execute("SELECT count(*) FROM users_user").fetchone()[0]
    yt.close()

    app = _ro(os.path.join(root, CWA_APP))
    counts = collections.OrderedDict()
    for (name,) in app.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        counts[name] = app.execute('SELECT count(*) FROM "{}"'.format(name)).fetchone()[0]
    app.close()

    state = os.path.join(root, STATE)
    owner_dir = os.path.join(root, OWNER_COVERS)
    owner = {}
    if os.path.isdir(owner_dir):
        for name in sorted(os.listdir(owner_dir)):
            p = os.path.join(owner_dir, name)
            if os.path.isfile(p):
                owner[name] = {"path": p, "sha256": _sha256(p), "bytes": os.path.getsize(p)}

    files = {}
    for rel in (YAMTRACK, CWA_APP, CALIBRE) + tuple(
            os.path.join(STATE, n) for n in sorted(os.listdir(state)) if n.endswith(".json")):
        p = os.path.join(root, rel)
        files[rel] = {"sha256": _sha256(p), "bytes": os.path.getsize(p), "mtime": int(os.path.getmtime(p))}

    return Snapshot(
        root=root, yt_items=items, yt_books=books, yt_history=history, yt_user_count=users,
        cwa=read_calibre(os.path.join(root, CALIBRE)), cwa_app_counts=counts,
        links=(_json(os.path.join(state, "cwa-links.json"), {}) or {}).get("books", {}),
        apple_applied=_json(os.path.join(state, "apple-covers-applied-20261021.json"), {}) or {},
        apple_keep=_json(os.path.join(state, "apple-keep.json"), []) or [],
        apple_cache=_json(os.path.join(state, "apple-cache.json"), {}) or {},
        search_cache=_json(os.path.join(state, "search-cache.json"), {}) or {},
        cwa_migration=_json(os.path.join(state, "cwa-migration.json"), {}) or {},
        sync_state=_json(os.path.join(state, "state.json"), {}) or {},
        owner_covers=owner, files=files)
