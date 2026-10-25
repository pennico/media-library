"""Import a watch history: movies and series with their watches (0.6.0, D47).

    python -m medialib import-watch FILE.json [--apply] [--offline] [--out DIR]

The file (format "medialib-watch", version 1; DESIGN.md §15.3) is made
outside the app from viewing-history exports and watchlists, and carries
the TMDB details of each title, so the import needs no TMDB key. Per item:

- the movie/series is found (TMDB id, else title + year) or created; TMDB
  details only fill what it lacks (screen.write);
- the entry is created with the file's status, rating and review, or, when it
  exists, only empty values are filled; a value the owner set stays;
- watches are added once each (a second run adds nothing), each labelled with
  where it came from (netflix, prime_video, google_play, owner);
- tags (Netflix thumbs) and collections (a watchlist) are added.

Posters are downloaded before each item's transaction. A dry run rolls
every item back and reports what would happen. The report goes to
<out>/import-watch-<time>.json.
"""
import datetime
import json
import os

import psycopg

from .. import db, screen
from ..covers import CoverError, CoverStore

FORMAT = "medialib-watch"
SOURCES = ("owner", "netflix", "prime_video", "google_play", "tmdb")
STATES = ("completed", "dropped", "in_progress", "paused")


class Rollback(Exception):
    pass


def _date(v, field):
    if not v:
        return None
    try:
        return datetime.date.fromisoformat(v)
    except ValueError:
        raise ValueError("{}: not a date: {}".format(field, v)) from None


def check(data):
    """The file's shape, before anything is written; -> list of problems."""
    probs = []
    if data.get("format") != FORMAT or data.get("version") != 1:
        return ["not a {} version 1 file".format(FORMAT)]
    for n, it in enumerate(data.get("items") or [], 1):
        where = "item {} ({})".format(n, it.get("title") or (it.get("tmdb") or {}).get("id") or "?")
        if it.get("kind") not in ("movie", "series"):
            probs.append(where + ": kind must be movie or series")
        if not it.get("tmdb") and not (it.get("title") or "").strip():
            probs.append(where + ": needs tmdb details or a title")
        if it.get("status") not in ("planned", "in_progress", "completed", "paused", "dropped"):
            probs.append(where + ": unknown status {}".format(it.get("status")))
        if it.get("rating") is not None and not (isinstance(it["rating"], (int, float)) and 0 <= it["rating"] <= 10):
            probs.append(where + ": rating must be 0-10")
        for w in it.get("watches") or []:
            if w.get("state") not in STATES or w.get("source", "owner") not in SOURCES:
                probs.append(where + ": a watch with state {} / source {}".format(w.get("state"), w.get("source")))
            if w.get("season") is not None and it.get("kind") != "series":
                probs.append(where + ": a season watch on a movie")
            try:
                _date(w.get("started"), "started")
                _date(w.get("ended"), "ended")
            except ValueError as e:
                probs.append("{}: {}".format(where, e))
        for t in it.get("tags") or []:
            if not isinstance(t, dict) or not t.get("name") or t.get("source", "owner") not in SOURCES:
                probs.append(where + ": a tag needs a name and a known source")
    return probs


def posters(store, kind, p, it):
    """The poster, and the posters of the seasons watched; -> {url: info}."""
    urls = [p["poster"]] if p and p["poster"] else []
    if p and kind == "series":
        watched = {w.get("season") for w in it.get("watches") or []}
        urls += [s["poster"] for s in p["seasons"] if s["number"] in watched and s.get("poster")]
    got = {}
    for u in urls:
        try:
            got[u] = store.fetch(u)
        except CoverError:
            pass
    return got


def import_item(conn, it, store, client, apply):
    kind = it["kind"]
    p = screen.parse(kind, it["tmdb"]) if it.get("tmdb") else None
    title = p["title"] if p else it["title"].strip()
    year = p["released"].year if p and p["released"] else it.get("year")
    found, trashed = screen.find(conn, kind, p["tmdb"] if p else None, title, year)
    if trashed:
        return {"title": title, "result": "skipped: in the trash (restore it, or delete it for good)"}
    covers = posters(store, kind, p, it) if p else {}
    res = {"title": title, "kind": kind, "result": "updated" if found else "added", "changes": []}
    try:
        with db.write_tx(conn, "tmdb", client, reason="import-watch"):
            if p:
                item, res["changes"] = screen.write(conn, kind, p, covers, item=found)
            elif found:
                item = found
            else:
                db.set_source(conn, "owner")
                item = conn.execute("INSERT INTO catalog.media_item (kind, title, first_published, first_published_precision)"
                                    " VALUES (%s, %s, %s, %s) RETURNING id",
                                    (kind, title, datetime.date(year, 1, 1) if year else None,
                                     "year" if year else None)).fetchone()["id"]
                conn.execute("INSERT INTO catalog.{} (item_id) VALUES (%s)".format(kind), (item,))
                res["changes"].append("created (not on TMDB)")
            entry_and_watches(conn, item, it, res)
            if not apply:
                raise Rollback
    except Rollback:
        pass
    return res


def _status_source(it):
    srcs = [w.get("source", "owner") for w in it.get("watches") or []]
    return srcs[-1] if srcs else it.get("status_source", "owner")


def entry_and_watches(conn, item, it, res):
    entry = conn.execute("SELECT * FROM library.entry WHERE item_id = %s", (item,)).fetchone()
    added = it.get("added")
    created = entry is None
    if created:
        db.set_source(conn, _status_source(it))
        # status first as planned: the watches below make it what the file says (checked at commit)
        conn.execute("INSERT INTO library.entry (item_id, added_at) VALUES (%s, coalesce(%s::timestamptz, now()))",
                     (item, datetime.datetime.combine(_date(added, "added"), datetime.time(12)) if added else None))
        res["changes"].append("entry")
        entry = conn.execute("SELECT * FROM library.entry WHERE item_id = %s", (item,)).fetchone()
    labels = {k: (v or {}).get("src") for k, v in (entry["provenance"] or {}).items()}
    n = 0
    for w in it.get("watches") or []:
        rid = screen.add_watch(conn, item, w["state"], _date(w.get("started"), "started"), w.get("started_precision", "day"),
                               _date(w.get("ended"), "ended"), w.get("precision", "day"), season=w.get("season"),
                               episodes=w.get("episodes"), platform=w.get("platform"), source=w.get("source", "owner"),
                               note=w.get("note"))
        n += bool(rid)
    if n:
        res["changes"].append("{} watch{}".format(n, "es" if n > 1 else ""))
    status = it["status"]
    if status != entry["status"] and (created or labels.get("status") != "owner" or only_imported(conn, item)):
        states = {r["state"] for r in conn.execute("SELECT state FROM library.consumption WHERE item_id = %s"
                                                     " AND deleted_at IS NULL", (item,)).fetchall()}
        # only a status the watches allow (completed needs a completed watch…); never back to planned
        if (status in ("completed", "dropped") and status in states or status == "planned" and not states) \
                and not (entry["status"] in ("completed", "dropped") and status == "planned"):
            db.set_source(conn, _status_source(it))
            conn.execute("UPDATE library.entry SET status = %s, status_changed_at = now() WHERE item_id = %s", (status, item))
            res["changes"].append("status " + status)
    db.set_source(conn, "owner")
    cols = {}
    if it.get("rating") is not None and entry["rating"] is None and labels.get("rating") != "owner":
        cols.update(rating=it["rating"], rated_at=datetime.datetime.now(datetime.timezone.utc))
    if it.get("review") and not entry["review"] and labels.get("review") != "owner":
        cols.update(review=it["review"][:100000], reviewed_at=datetime.datetime.now(datetime.timezone.utc))
    if cols:
        conn.execute("UPDATE library.entry SET {} WHERE item_id = %s".format(", ".join(k + " = %s" for k in cols)),
                     (*cols.values(), item))
        res["changes"] += [k for k in cols if not k.endswith("_at")]
    for t in it.get("tags") or []:
        db.set_source(conn, t.get("source", "owner"))
        if screen.add_tag(conn, item, screen.plain_tag(conn, t["name"]), t.get("source", "owner")):
            res["changes"].append("tag " + t["name"])
    db.set_source(conn, "owner")
    for name in it.get("collections") or []:
        if add_to_collection(conn, item, it["kind"], name):
            res["changes"].append("collection " + name)


def only_imported(conn, item):
    """True when this importer is the only writer the entry ever had: an owner-labelled status it
    wrote itself (from the owner's own lists) may be corrected by a later run; one the owner set
    in the console, or a Joplin note set, stays."""
    row = conn.execute("""SELECT count(*) AS n, bool_and(client = 'import-watch') AS mine FROM audit.change
                           WHERE table_name = 'library.entry' AND row_key = jsonb_build_object('item_id', %s::bigint)""",
                       (item,)).fetchone()
    return bool(row["n"] and row["mine"])


def add_to_collection(conn, item, kind, name):
    s = conn.execute("SELECT id FROM library.shelf WHERE media_kind = %s AND kind = 'manual' AND deleted_at IS NULL"
                     " AND lower(catalog.f_unaccent(name)) = lower(catalog.f_unaccent(%s))", (kind, name)).fetchone()
    sid = s["id"] if s else conn.execute(
        "INSERT INTO library.shelf (name, kind, media_kind, position) VALUES (%s, 'manual', %s,"
        " (SELECT coalesce(max(position), 0) + 1 FROM library.shelf WHERE media_kind = %s)) RETURNING id",
        (name[:100], kind, kind)).fetchone()["id"]
    if conn.execute("SELECT 1 AS x FROM library.shelf_item WHERE shelf_id = %s AND item_id = %s", (sid, item)).fetchone():
        return False
    conn.execute("INSERT INTO library.shelf_item (shelf_id, item_id, position) SELECT %s, %s, coalesce(max(position), 0) + 1"
                 " FROM library.shelf_item WHERE shelf_id = %s", (sid, item, sid))
    return True


def run(cfg, path, apply=False, offline=False, out_dir="out", log=print, conn=None, store=None):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    probs = check(data)
    if probs:
        for p in probs[:30]:
            log("refused: " + p)
        return {"failed": True, "problems": probs}
    store = store or CoverStore(cfg.media_dir, offline=offline, log=lambda *_: None)
    own = conn is None
    conn = conn or db.connect(cfg, db.ROLE_APP)
    results = []
    try:
        for n, it in enumerate(data["items"], 1):
            try:
                r = import_item(conn, it, store, "import-watch", apply)
            except psycopg.Error as e:
                r = {"title": it.get("title") or (it.get("tmdb") or {}).get("title") or (it.get("tmdb") or {}).get("name"),
                     "result": "error: " + (e.diag.message_primary or type(e).__name__)}
            results.append(r)
            if n % 50 == 0:
                log("{} of {}".format(n, len(data["items"])))
    finally:
        if own:
            conn.close()
    summary = {}
    for r in results:
        key = r["result"].split(":")[0]
        summary[key] = summary.get(key, 0) + 1
    os.makedirs(out_dir, exist_ok=True)
    report = os.path.join(out_dir, "import-watch-{:%Y%m%d-%H%M%S}.json".format(datetime.datetime.now()))
    with open(report, "w", encoding="utf-8") as fh:
        json.dump({"file": os.path.basename(path), "apply": apply, "summary": summary, "items": results}, fh,
                  ensure_ascii=False, indent=1, default=str)
    log("{}: {}".format("applied" if apply else "dry run, nothing written", ", ".join(
        "{} {}".format(v, k) for k, v in sorted(summary.items()))))
    return {"failed": any(r["result"].startswith("error") for r in results), "summary": summary,
            "report_path": report, "results": results}
