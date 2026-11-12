"""Joplin "To watch" and "To read" lists -> the library (D73).

    python -m medialib joplin-lists --notes FILE|- [--apply] [--offline]    # the notes from the Joplin client
    python -m medialib joplin-lists (--tar FILE|- | --dir DIR) [--offline]   # the sync folder, read-only: a look only

The notes come from the headless Joplin client (deploy/joplin, `client.sh pull`: JSON) or, to look
before the client is set up, from the sync folder itself (read-only, never written). The notes are
the live ones titled "To watch" and "To read" (any notebook, or the one JOPLIN_LISTS_NOTEBOOK
names). Each line is one title, with an optional year, author or kind:

    Dune (2021)                       a year picks between films (and series) with the same title
    Severance (series)                (film), (movie), (series), (tv) say the kind on the To watch list
    # Series                          a heading on the To watch list: the lines under it are series
    Pachinko - Min Jin Lee            " - ", " – " or " by ": the author (or the director)
    Stoner (John Williams)            on the To read list a name in brackets is the author
    https://www.themoviedb.org/movie/693134      a TMDB address: that one
    - [ ] Perfect Days                list marks, checkboxes, **bold** and links are fine

A line is *taken* when it is an item already in the library (left as it is: a book you read stays
read) or one the sources are sure of: added as To watch / To read, written as the owner (client
``joplin-list:<note id>``), with the rules of the review feed and of Fill missing (D33, D46): the
title must be the same, an author or a year must agree when given, and among several only one far
more popular than the rest counts; never a guess. The run prints the plan for the client on stdout:
``{"notes": [{"id", "remove": [lines]}]}``; the client deletes those lines from the note (D73).
A line it can't take stays in the note and shows on the Sync page with why and the closest
candidates; it is tried against the library every run and against the sources again after a day.
Headings, blank lines and other text are left alone.

A *conflict copy* of a list note (Joplin makes one when a device that had not synced yet changed the note
the client had just changed; the client passes them on with ``conflict_of``) is read for the lines that
are only in it, the owner's late additions: those are taken like the others, but the copy is never
changed; the Sync page asks the owner to delete it in Joplin.
"""
import datetime
import json
import re
import sys

from psycopg.types.json import Jsonb

from .. import db
from ..fill.service import FillError
from . import joplin, joplin_items

LISTS = {"to watch": "watch", "to read": "read"}
TMDB_PROVIDER = {"movie": "tmdb_movie", "series": "tmdb_tv"}
RETRY = datetime.timedelta(hours=24)
MAX_LINES = 500                                         # per note; a longer note is not a list
KIND_WORDS = {
    "film": "movie", "films": "movie", "movie": "movie", "movies": "movie",
    "series": "series", "serie": "series", "tv": "series", "tv series": "series", "tv-series": "series",
    "serie tv": "series", "tv show": "series", "show": "series", "shows": "series", "miniseries": "series",
}
_MARK = re.compile(r"^(?:[-*+•]\s+|\d{1,3}[.)]\s+)?(?:\[[ xX]?\]\s*)?")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EMPH = re.compile(r"(\*\*|__|\*|~~)(.+?)\1")
_TMDB = re.compile(r"themoviedb\.org/(movie|tv)/(\d{1,9})")
_TAG = re.compile(r"\s*[(\[]([^()\[\]]{1,60})[)\]]\s*$")
_BARE_YEAR = re.compile(r"(?:\s+|,\s*)(1[89]\d\d|20\d\d)\s*$")
_BY = re.compile(r"\s+(?:[-–—]|by)\s+", re.I)
_SKIP = ("#", ">", "```", "---", "***", "___", "|", "<!--")


class Unclear(joplin.Unclear):
    pass


# ------------------------------------------------------------------ parsing

def heading_kind(line):
    """'# Series' -> 'series', '## Films' -> 'movie', another heading -> None."""
    words = line.lstrip("#").strip().strip(":").lower()
    return KIND_WORDS.get(words)


def parse_line(raw, lst, default_kind=None, this_year=None):
    """One line of a list note -> what to look for, or None (not a title: blank, heading, rule, text).

    {line, kind, tmdb: (kind, id) | None, tries: [(title, by, year)]}; the tries go from the most
    precise reading of the line to the plain text of it."""
    s = raw.strip()
    if not s or s.startswith(_SKIP) or len(s) > 300:
        return None
    s = _MARK.sub("", s, count=1).strip()
    m = _TMDB.search(s)
    if m:
        kind = "movie" if m.group(1) == "movie" else "series"
        return {"line": raw.strip(), "kind": kind, "tmdb": (kind, int(m.group(2))), "tries": []}
    s = _EMPH.sub(r"\2", _LINK.sub(r"\1", s)).strip()
    if not s or re.match(r"^[a-z]+://", s, re.I) or not re.search(r"\w", s):
        return None
    plain, kind, year, by = s, default_kind if lst == "watch" else "book", None, None
    this_year = this_year or datetime.date.today().year
    for _ in range(3):                                   # trailing (…) / […]: a kind, a year, an author
        m = _TAG.search(s)
        if not m or not s[:m.start()].strip():
            break
        tag = m.group(1).strip()
        if lst == "watch" and tag.lower() in KIND_WORDS:
            kind = KIND_WORDS[tag.lower()]
        elif re.fullmatch(r"1[89]\d\d|20\d\d", tag) and int(tag) <= this_year + 3:
            year = int(tag)
        elif lst == "read" and by is None and not re.search(r"\d", tag):
            by = tag
        else:
            break
        s = s[:m.start()].rstrip(" ,;")
    m = _BARE_YEAR.search(s)
    if year is None and m and s[:m.start()].strip() and int(m.group(1)) <= this_year + 3:
        year, s = int(m.group(1)), s[:m.start()].rstrip(" ,;")
    title = s
    parts = _BY.split(s, maxsplit=1)
    if by is None and len(parts) == 2 and parts[0].strip() and parts[1].strip():
        title, by = parts[0].strip(), parts[1].strip()
    tries = []
    for t in [(title, by, year), (s, None, year), (plain, None, None)]:
        if t[0] and t not in tries:
            tries.append(t)
    return {"line": raw.strip(), "kind": kind, "tmdb": None, "tries": tries}


def list_lines(body, lst, this_year=None):
    """The lines of a list note that are titles, in order (a heading sets the kind on To watch)."""
    out, kind = [], None
    for raw in (body or "").replace("\r\n", "\n").split("\n")[:MAX_LINES]:
        if raw.strip().startswith("#"):
            kind = heading_kind(raw.strip()) if lst == "watch" else None
            continue
        p = parse_line(raw, lst, kind, this_year)
        if p:
            out.append(p)
    return out


def notes_from_sync(items, notebook=None):
    """The list notes in a Joplin sync folder (read-only), in the client's JSON shape."""
    folders = {i["id"]: i for i in items if i.get("type_") == joplin_items.TYPE_FOLDER and joplin_items.is_live(i)}
    out = []
    for n in items:
        if n.get("type_") != joplin_items.TYPE_NOTE or not joplin_items.is_live(n):
            continue
        if n["title"].strip().lower() not in LISTS:
            continue
        path, f, seen = [], n.get("parent_id", ""), set()
        while f in folders and f not in seen:
            seen.add(f)
            path.insert(0, folders[f]["title"])
            f = folders[f].get("parent_id", "")
        if notebook and notebook.strip().lower() not in [p.strip().lower() for p in path]:
            continue
        out.append({"id": n["id"], "title": n["title"], "body": n["body"], "notebook": " / ".join(path),
                    "updated_time": n.get("updated_time", "")})
    return {"notes": out}


# ----------------------------------------------------------------- matching

def _in_library(conn, item):
    row = conn.execute("""SELECT i.deleted_at IS NOT NULL AS trashed, e.status, e.item_id IS NOT NULL AS entry
                            FROM catalog.media_item i LEFT JOIN library.entry e ON e.item_id = i.id
                           WHERE i.id = %s""", (item,)).fetchone()
    return row


DONE = {"book": {"completed": "read", "dropped": "abandoned"}, "screen": {"completed": "watched", "dropped": "abandoned"}}


def _found(conn, item, how, confirmed, lst):
    """An item the line names that is already there. One you read or watched (or abandoned) is taken only when the
    line confirms it (an author, a year, a TMDB address): a bare "Dune" may mean another Dune than the one seen."""
    row = _in_library(conn, item)
    if row and row["trashed"]:
        raise Unclear("it is in the Trash: restore it there, or delete the line")
    done = DONE["book" if lst == "read" else "screen"].get(row["status"] if row and row["entry"] else None)
    if done and not confirmed:
        t = conn.execute("SELECT title, extract(year FROM first_published)::int AS y FROM catalog.media_item WHERE id = %s",
                         (item,)).fetchone()
        raise Unclear("you have {} it already ({}{}): add the {} to take the line as it is, or delete the line".format(
            done, t["title"], ", {}".format(t["y"]) if t["y"] and lst == "watch" else "", "author" if lst == "read" else "year"))
    labels = {"planned": "on your list", "in_progress": "in progress", "completed": "done", "paused": "paused",
              "dropped": "abandoned"}
    return ("found", item, "{} ({})".format(how, labels.get(row["status"], "in the library")) if row and row["entry"]
            else how)


def match_book(conn, fill, library, p, search):
    """-> ('found', item id, how) or ('add', (lookup answer, index), how). Raises Unclear."""
    for title, by, year in p["tries"]:
        item = library.find("book", title, by, year)
        if item:
            return _found(conn, item, "already in the library", bool(by), "read")
    if not search:
        raise Unclear(None)
    if fill is None:
        raise Unclear("not in the library (offline run: no Apple Books / Open Library search)")
    last = None
    for title, by, _year in p["tries"]:
        try:
            res = fill.lookup(title, (joplin.notes.people(by) or [None])[0])
        except FillError as e:
            raise Unclear("search failed: {}".format(e)) from None
        try:
            index = joplin._choose({"by": by}, res["candidates"])
        except joplin.Unclear as u:
            last = last or u
            continue
        pick = next(c for c in res["candidates"] if c["index"] == index)
        return "add", (res, index), "from {}: {}".format(pick["provider"], pick["title"])
    if last and "different authors" in str(last):
        raise Unclear("the title fits books by different authors: add the author (Title - Author)", last.candidates)
    raise Unclear("no confident match at Apple Books / Open Library: {}or add the book in the console (the line then finds"
                  " it)".format("" if any(t[1] for t in p["tries"]) else "add the author (Title - Author), "),
                  last.candidates if last else ())


def _screen_kinds(p):
    return [p["kind"]] if p["kind"] in ("movie", "series") else ["movie", "series"]


def match_screen(conn, tmdb, library, p, search):
    """-> ('found', item id, how) or ('add', (kind, tmdb id), how). Raises Unclear."""
    if p["tmdb"]:
        kind, tid = p["tmdb"]
        item = joplin._item_by_id(conn, TMDB_PROVIDER[kind], str(tid))
        if item:
            return _found(conn, item, "already in the library", True, "watch")
        if not tmdb or not tmdb.available:
            raise Unclear("TMDB is off (no key in secrets/tmdb.key)")
        return "add", (kind, tid), "from its TMDB address"
    for title, by, year in p["tries"]:
        hits = []
        for k in _screen_kinds(p):
            item = library.find(k, title, by, year)
            if item:
                hits.append(item)
        if len(hits) > 1:
            raise Unclear("a film and a series in your library are both called that: add (film) or (series)")
        if hits:
            return _found(conn, hits[0], "already in the library", bool(year or by), "watch")
    if not search:
        raise Unclear(None)
    if not tmdb or not tmdb.available:
        raise Unclear("not in the library, and TMDB is off (no key in secrets/tmdb.key)")
    for title, _by, year in p["tries"]:
        cands = []
        try:
            for k in _screen_kinds(p):
                cands += [c for c in tmdb.search(k, title, year) if c["score"] >= 0.97
                          and (year is None or c["year"] is None or abs(c["year"] - year) <= 1)]
        except FillError as e:
            raise Unclear("search failed: {}".format(e)) from None
        if not cands:
            continue
        cands.sort(key=lambda c: -c["popularity"])
        short = [{"title": c["title"], "year": c["year"], "kind": c["kind"], "tmdb": c["tmdb"]} for c in cands[:3]]
        if len(cands) == 1 or cands[0]["popularity"] > 3 * cands[1]["popularity"]:
            c = cands[0]
            how = "from TMDB: {} ({}{})".format(c["title"], "film" if c["kind"] == "movie" else "series",
                                               ", {}".format(c["year"]) if c["year"] else "")
            if len(cands) > 1:
                how += ", far more popular than {} other{}".format(len(cands) - 1, "s" if len(cands) > 2 else "")
            return "add", (c["kind"], c["tmdb"]), how
        raise Unclear("several fit: {}; add the year{}".format(
            ", ".join("{} ({}, {})".format(c["title"], "film" if c["kind"] == "movie" else "series", c["year"] or "?")
                      for c in cands[:3]), "" if p["kind"] else " or (film) / (series)"), short)
    raise Unclear("TMDB has nothing called “{}”{}".format(p["tries"][0][0], " from {}".format(p["tries"][0][2])
                                                         if p["tries"][0][2] else ""))


# ------------------------------------------------------------------ writing

def add_book(conn, fill, nid, found):
    res, index = found
    added = fill.add(conn, None, res["token"], index, "planned", client="joplin-list:" + nid)
    return joplin._pid_to_id(conn, added["id"])


def add_screen(conn, tmdb, store, nid, found):
    from .. import screen
    from ..web import writes
    kind, tid = found
    d = tmdb.details(kind, tid)
    if not d:
        raise Unclear("TMDB doesn't know that one any more")
    covers = writes.screen_posters(store, kind, screen.parse(kind, d)) if store else {}
    try:
        added = writes.add_screen(conn, None, kind, d, covers, "planned", client="joplin-list:" + nid)
    except writes.BadRequest as e:
        raise Unclear(str(e)) from None
    return joplin._pid_to_id(conn, added["id"])


def save_line(conn, nid, line, lst, state, item=None, how=None, problem=None, candidates=(), searched=False):
    conn.execute("""INSERT INTO sync.joplin_list_line (note_id, line, list, state, item_id, how, problem, candidates,
                                                       searched_at, seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, now())
                    ON CONFLICT (note_id, line) DO UPDATE SET list = EXCLUDED.list, state = EXCLUDED.state,
                        item_id = EXCLUDED.item_id, how = EXCLUDED.how,
                        problem = coalesce(EXCLUDED.problem, sync.joplin_list_line.problem),
                        candidates = CASE WHEN EXCLUDED.problem IS NULL AND EXCLUDED.state = 'unclear'
                                          THEN sync.joplin_list_line.candidates ELSE EXCLUDED.candidates END,
                        searched_at = coalesce(EXCLUDED.searched_at, sync.joplin_list_line.searched_at),
                        seen_at = now()""",
                 (nid, line[:1000], lst, state, item, (how or "")[:500] or None, (problem or "")[:2000] or None,
                  Jsonb(list(candidates)), searched))


# ----------------------------------------------------------------------- run

def run(conn, data, fill=None, tmdb=None, store=None, apply=False, now=None, log=print):
    """One pass over the list notes. -> (summary, plan); the summary is stored with the run."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    summary = {"notes": [], "added": [], "taken": [], "unclear": [], "errors": [], "lines": 0, "addedItems": []}
    plan = {"notes": []}
    status = "failed"
    run_id = conn.execute("INSERT INTO ingest.run (tool, mode, sources) VALUES ('joplin_lists', %s, %s) RETURNING id",
                          ("apply" if apply else "dry_run",
                           Jsonb({"client": (data.get("client") or "")[:200]}))).fetchone()["id"]
    started = conn.execute("SELECT now() AS t").fetchone()["t"]
    try:
        prev = {(r["note_id"], r["line"]): r for r in conn.execute("SELECT * FROM sync.joplin_list_line").fetchall()}
        library = joplin.Library(conn)
        by_id = {n.get("id"): n for n in data.get("notes") or []}
        for note in data.get("notes") or []:
            nid, lst = note.get("id", ""), LISTS.get((note.get("title") or "").strip().lower())
            if not lst or not re.fullmatch(r"[0-9a-f]{32}", nid):
                continue
            lines, copy_of = list_lines(note.get("body"), lst), note.get("conflict_of")
            if copy_of is not None:                       # a conflict copy: only the lines that are not in the note
                orig = by_id.get(copy_of)
                have = {p["line"] for p in list_lines(orig.get("body"), lst)} if orig else set()
                lines = [p for p in lines if p["line"] not in have]
            summary["notes"].append(dict({"id": nid, "title": note["title"].strip(), "notebook": note.get("notebook") or "",
                                          "lines": len(lines)}, **({"conflictCopy": True} if copy_of is not None else {})))
            summary["lines"] += len(lines)
            remove = []
            for p in lines:
                key = (nid, p["line"][:1000])
                old = prev.get(key)
                search = old is None or old["state"] == "taken" or old["searched_at"] is None \
                    or now - old["searched_at"] > RETRY
                label = "{}: {}".format(note["title"].strip(), p["line"])
                try:
                    if lst == "read":
                        what, found, how = match_book(conn, fill, library, p, search)
                    else:
                        what, found, how = match_screen(conn, tmdb, library, p, search)
                    if what == "add":
                        if not apply:
                            summary["added"].append("{} → would add {}".format(label, how))
                            remove.append(p["line"])
                            continue
                        item = add_book(conn, fill, nid, found) if lst == "read" else add_screen(conn, tmdb, store, nid, found)
                        library.refresh()
                        summary["added"].append("{} → {}".format(label, how))
                        it = conn.execute("SELECT public_id::text AS id, title, kind FROM catalog.media_item WHERE id = %s",
                                          (item,)).fetchone()
                        summary["addedItems"].append({"line": p["line"], "list": lst, "id": it["id"], "title": it["title"],
                                                      "kind": it["kind"]})
                    else:
                        item = found
                        if apply and not _in_library(conn, item)["entry"]:      # in the catalogue only: into the library
                            with db.write_tx(conn, "owner", "joplin-list:" + nid, reason="Joplin list"):
                                conn.execute("INSERT INTO library.entry (item_id) VALUES (%s)", (item,))
                            how = "added to the library from the catalogue"
                        summary["taken"].append("{} → {}".format(label, how))
                    remove.append(p["line"])
                    if apply:
                        save_line(conn, nid, p["line"], lst, "taken", item=item, how=how)
                except joplin.Unclear as u:
                    problem = str(u) if u.args and u.args[0] else (old["problem"] if old else None) or "not found"
                    summary["unclear"].append("{}: {}".format(label, problem))
                    if apply:
                        save_line(conn, nid, p["line"], lst, "unclear", problem=problem if u.args[0] else None,
                                  candidates=joplin._short(u.candidates) if u.candidates and "provider" in u.candidates[0]
                                  else list(u.candidates), searched=search)
                except Exception as e:      # noqa: BLE001 - one bad line must not stop the others
                    msg = getattr(getattr(e, "diag", None), "message_primary", None) or str(e) or type(e).__name__
                    summary["errors"].append("{}: {}".format(label, msg))
            if remove and copy_of is None:
                plan["notes"].append({"id": nid, "title": note["title"].strip(), "remove": remove})
        if apply:
            conn.execute("DELETE FROM sync.joplin_list_line WHERE seen_at < %s", (started,))
        status = "ok"
    finally:
        conn.execute("UPDATE ingest.run SET finished_at = now(), status = %s, summary = %s, counts = %s WHERE id = %s",
                     (status, Jsonb(summary),
                      Jsonb({k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()}), run_id))
    return summary, plan


def format_summary(summary):
    parts = ["{} note{}, {} line{}".format(len(summary["notes"]), "" if len(summary["notes"]) == 1 else "s",
                                           summary["lines"], "" if summary["lines"] == 1 else "s")]
    for key, head in (("added", "added"), ("taken", "already in the library"), ("unclear", "left in the note"),
                      ("errors", "errors")):
        if summary[key]:
            parts.append("{} ({}):\n- {}".format(head, len(summary[key]), "\n- ".join(summary[key])))
    return "\n".join(parts)


def record_client_error(conn, text):
    """The Joplin client could not do its part (sync, the guard, deleting lines): a failed run that says why, so the
    Sync page shows it and the health check turns red."""
    msg = " ".join((text or "").split())[-2000:] or "the Joplin client failed without a message"
    conn.execute("INSERT INTO ingest.run (tool, mode, sources, finished_at, status, summary, counts)"
                 " VALUES ('joplin_lists', 'apply', %s, now(), 'failed', %s, %s)",
                 (Jsonb({"client": "error"}), Jsonb({"notes": [], "added": [], "taken": [], "unclear": [], "lines": 0,
                                                       "errors": [], "client_error": msg}), Jsonb({"client_error": 1})))
    return msg


def main(cfg, notes=None, source_dir=None, tar=None, apply=False, offline=False, notebook=None, client_error=None):
    if client_error is not None:
        with db.connect(cfg, db.ROLE_APP) as conn:
            print("recorded: " + record_client_error(conn, client_error.read()), file=sys.stderr)
        return 0
    if notes is not None:
        data = json.load(notes)
        if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
            raise SystemExit("--notes: a JSON object with a notes list (deploy/joplin client.sh pull)")
    else:
        if apply:
            raise SystemExit("--apply needs the notes from the Joplin client (--notes): only the client may delete"
                             " lines from a note (ops/joplin-lists.sh)")
        data = notes_from_sync(joplin.load(source_dir, tar), notebook)
    fill = tmdb = store = None
    if not offline:
        import os
        from ..covers import CoverStore
        from ..fill import service
        from ..fill import tmdb as tmdb_mod
        store = CoverStore(cfg.media_dir, log=lambda *_: None)
        fill = service.make(cfg.media_dir, store)
        tmdb = tmdb_mod.Tmdb(service.http_fetcher(os.path.join(cfg.media_dir, "cache", "http")), tmdb_mod.api_key())
    with db.connect(cfg, db.ROLE_APP) as conn:
        summary, plan = run(conn, data, fill, tmdb, store, apply=apply)
    print(format_summary(summary), file=sys.stderr)
    if not apply:
        print("dry run: nothing added, nothing to delete from the notes", file=sys.stderr)
        plan = {"notes": [], "dry_run": True}
    json.dump(plan, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 1 if summary["errors"] else 0
