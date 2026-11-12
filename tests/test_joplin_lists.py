"""Joplin "To watch" / "To read" lists -> the library (D73): the line format, matching (library first, then the
sources, never a guess), what is added and how, the plan of lines for the client to delete, what stays in
the note, and that a second run adds nothing twice. No network: fakes stand in for TMDB and the book sources."""
import datetime
import io
import json
import uuid

import pytest

from conftest import add_entry, new_book, tx
from medialib import db
from medialib.feeds import joplin_lists as L
from test_joplin import FakeFill, Notebook, book_with_author, hexid
from test_screen import movie_details, series_details
from test_web import Client, site  # noqa: F401 - the running console

NOW = datetime.datetime(2026, 11, 12, 18, 0, tzinfo=datetime.timezone.utc)


def first(line, lst="watch", kind=None):
    return L.parse_line(line, lst, kind, this_year=2026)


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize("line,lst,want", [
    ("Dune (2021)", "watch", ("Dune", None, 2021)),
    ("Dune [2021]", "watch", ("Dune", None, 2021)),
    ("Dune, 2021", "watch", ("Dune", None, 2021)),
    ("- [ ] Perfect Days", "watch", ("Perfect Days", None, None)),
    ("* **Il nome della rosa**", "read", ("Il nome della rosa", None, None)),
    ("1. [Arrival](https://example.org/x)", "watch", ("Arrival", None, None)),
    ("Pachinko - Min Jin Lee", "read", ("Pachinko", "Min Jin Lee", None)),
    ("Stoner by John Williams", "read", ("Stoner", "John Williams", None)),
    ("Stoner (John Williams)", "read", ("Stoner", "John Williams", None)),
    ("1984", "read", ("1984", None, None)),
    ("Blade Runner 2049", "watch", ("Blade Runner 2049", None, None)),       # not a year: too far ahead
    ("Wonder Woman 1984", "watch", ("Wonder Woman", None, 1984)),
])
def test_line_format(line, lst, want):
    p = first(line, lst)
    assert p["tries"][0] == want
    assert p["line"] == line.strip()


def test_line_kinds_and_fallbacks():
    assert first("Severance (series)")["kind"] == "series"
    assert first("Severance (tv)")["kind"] == "series"
    assert first("Arrival (film)")["kind"] == "movie"
    assert first("Arrival")["kind"] is None
    assert first("Stoner (series)", "read")["kind"] == "book"       # a To read line is always a book
    p = first("Wonder Woman 1984")
    assert p["tries"][-1] == ("Wonder Woman 1984", None, None), "the plain line is always tried last"
    p = first("Stand by Me (1986)")
    assert [t[0] for t in p["tries"]] == ["Stand", "Stand by Me", "Stand by Me (1986)"]
    t = first("https://www.themoviedb.org/movie/329865-arrival")
    assert t["tmdb"] == ("movie", 329865) and t["kind"] == "movie"
    assert first("see https://www.themoviedb.org/tv/95396")["tmdb"] == ("series", 95396)
    for skip in ["", "   ", "---", "> a quote", "https://example.org/list", "# Films", "```", "| a | b |", "<!-- x -->"]:
        assert first(skip) is None, skip


def test_headings_set_the_kind_on_the_watch_list():
    body = "Arrival\n\n# Series\nSeverance\n- Shogun 2024\n## Films\nDune (2021)\n# Someday\nThe Wire\n"
    got = [(p["tries"][0][0], p["kind"]) for p in L.list_lines(body, "watch", 2026)]
    assert got == [("Arrival", None), ("Severance", "series"), ("Shogun", "series"), ("Dune", "movie"), ("The Wire", None)]
    assert [p["kind"] for p in L.list_lines("# Series\nStoner\n", "read", 2026)] == ["book"]


# ----------------------------------------------------------------- fakes

class FakeTmdb:
    available = True

    def __init__(self, entries):
        self.entries, self.searches = entries, []      # [(kind, tmdb id, title, year, popularity)]

    def search(self, kind, query, year=None, limit=8):
        from medialib.fill import providers as P
        self.searches.append((kind, query, year))
        out = []
        for k, tid, title, y, pop in self.entries:
            if k != kind or (year and abs(y - year) > 1):
                continue
            s = P.title_score(query, title)
            if s >= 0.5:
                out.append({"kind": k, "tmdb": tid, "title": title, "original_title": None, "year": y, "overview": None,
                            "language": "eng", "poster_url": None, "score": round(s, 3), "popularity": pop})
        return out[:limit]

    def details(self, kind, tid):
        for k, i, title, y, _ in self.entries:
            if k == kind and i == tid:
                return movie_details(title, tid, y) if k == "movie" else dict(series_details(title, tid),
                                                                               first_air_date="{}-01-10".format(y))
        return None


def u(s):
    return "{} {}".format(s, uuid.uuid4().hex[:6])


def done(conn, item):
    """Read (or watched): a finished reading and the status that goes with it."""
    with tx(conn, source="owner"):
        conn.execute("INSERT INTO library.consumption (item_id, state, ended_on, ended_precision, source)"
                     " VALUES (%s, 'completed', '2020-01-01', 'year', 'owner')", (item,))
        conn.execute("UPDATE library.entry SET status = 'completed' WHERE item_id = %s", (item,))


def lines_of(conn, nid):
    return {r["line"]: r for r in conn.execute("SELECT * FROM sync.joplin_list_line WHERE note_id = %s", (nid,)).fetchall()}


def entry_of(conn, title):
    return conn.execute("""SELECT i.id, i.kind, e.status, i.deleted_at FROM catalog.media_item i
                            LEFT JOIN library.entry e ON e.item_id = i.id WHERE i.title = %s""", (title,)).fetchall()


# ------------------------------------------------------------------- the run

def test_watch_list_adds_what_is_sure_and_leaves_the_rest(app):
    film, twin, known = u("Lists Film"), u("Lists Twin"), u("Lists Known")
    tm = FakeTmdb([("movie", 910001, film, 2001, 20.0),
                   ("movie", 910002, twin, 2010, 10.0), ("series", 910003, twin, 2012, 12.0),
                   ("movie", 910004, known, 1999, 5.0)])
    with tx(app, source="owner"):                                     # already in the library, watched
        it = app.execute("INSERT INTO catalog.media_item (kind, title, first_published, first_published_precision)"
                         " VALUES ('movie', %s, '1999-01-01', 'year') RETURNING id", (known,)).fetchone()["id"]
        app.execute("INSERT INTO catalog.movie (item_id) VALUES (%s)", (it,))
        add_entry(app, it, "planned")
    nid = hexid()
    body = "{}\n# Films\n{} (2001)\n\n{} (series)\n{}\nsome text that names nothing at all here\n".format(twin, film, twin, known)
    s, plan = L.run(app, {"notes": [{"id": nid, "title": "To watch", "body": body}]}, tmdb=tm, apply=True, now=NOW)
    assert [x["remove"] for x in plan["notes"]] == [["{} (2001)".format(film), "{} (series)".format(twin), known]]
    added = entry_of(app, film)
    assert len(added) == 1 and added[0]["status"] == "planned"
    tw = entry_of(app, twin)
    assert [r["kind"] for r in tw] == ["series"], "(series) picks the series; the bare line is unclear"
    assert entry_of(app, known)[0]["status"] == "planned" and len(entry_of(app, known)) == 1
    client = app.execute("SELECT DISTINCT a.client FROM audit.change a WHERE a.table_name = 'library.entry'"
                         " AND (a.row_key->>'item_id')::bigint = %s", (added[0]["id"],)).fetchone()["client"]
    assert client == "joplin-list:" + nid, "written as the list's own client"
    rows = lines_of(app, nid)
    assert rows[twin]["state"] == "unclear" and "several fit" in rows[twin]["problem"]
    assert {c["kind"] for c in rows[twin]["candidates"]} == {"movie", "series"}
    assert rows["some text that names nothing at all here"]["state"] == "unclear"
    assert rows[known]["state"] == "taken" and "already in the library" in rows[known]["how"]
    assert len(s["added"]) == 2 and len(s["taken"]) == 1 and len(s["unclear"]) == 2 and not s["errors"]
    run = app.execute("SELECT status, summary FROM ingest.run WHERE tool = 'joplin_lists' ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "ok" and len(run["summary"]["added"]) == 2

    # the client could not delete the lines: the next run takes them again and adds nothing twice; the bare line now
    # names the series on your list; the unclear one is tried against the library only (TMDB again after a day)
    n0 = len(tm.searches)
    s2, plan2 = L.run(app, {"notes": [{"id": nid, "title": "To watch", "body": body}]}, tmdb=tm, apply=True,
                      now=NOW + datetime.timedelta(hours=1))
    assert not s2["added"] and len(s2["taken"]) == 4 and plan2["notes"][0]["remove"][0] == twin
    assert len(entry_of(app, film)) == 1 and len(entry_of(app, twin)) == 1
    assert len(tm.searches) == n0
    # the lines are gone from the note: their rows go; the unclear one stays, and a day later TMDB is asked again
    left = "some text that names nothing at all here"
    L.run(app, {"notes": [{"id": nid, "title": "To watch", "body": left}]}, tmdb=tm, apply=True,
          now=NOW + datetime.timedelta(hours=26))
    assert set(lines_of(app, nid)) == {left} and len(tm.searches) > n0


def test_seen_already_is_taken_only_when_the_line_confirms_it(app):
    seen = u("Lists Seen")
    with tx(app, source="owner"):
        it = app.execute("INSERT INTO catalog.media_item (kind, title, first_published, first_published_precision)"
                         " VALUES ('movie', %s, '1984-01-01', 'year') RETURNING id", (seen,)).fetchone()["id"]
        app.execute("INSERT INTO catalog.movie (item_id) VALUES (%s)", (it,))
        add_entry(app, it, "planned")
    done(app, it)
    nid = hexid()
    s, plan = L.run(app, {"notes": [{"id": nid, "title": "To watch", "body": "{}\n{} (1984)\n".format(seen, seen)}]},
                    tmdb=FakeTmdb([]), apply=True, now=NOW)
    assert plan["notes"][0]["remove"] == ["{} (1984)".format(seen)]
    assert "watched it already ({}, 1984): add the year".format(seen) in lines_of(app, nid)[seen]["problem"]
    assert entry_of(app, seen)[0]["status"] == "completed", "nothing changes on the item"


def test_tmdb_address_trash_and_entryless_items(app):
    film = u("Lists Addressed")
    tm = FakeTmdb([("movie", 920001, film, 2005, 1.0)])
    nid = hexid()
    line = "https://www.themoviedb.org/movie/920001-x"
    s, plan = L.run(app, {"notes": [{"id": nid, "title": "To watch", "body": line}]}, tmdb=tm, apply=True, now=NOW)
    item = entry_of(app, film)[0]
    assert item["status"] == "planned" and plan["notes"][0]["remove"] == [line]
    pid = app.execute("SELECT public_id::text AS p FROM catalog.media_item WHERE id = %s", (item["id"],)).fetchone()["p"]
    from medialib.web import writes
    writes.trash_item(app, "test", pid)
    s, plan = L.run(app, {"notes": [{"id": nid, "title": "To watch", "body": line}]}, tmdb=tm, apply=True, now=NOW)
    assert not plan["notes"] and "Trash" in lines_of(app, nid)[line]["problem"]
    # a book in the catalogue but not in the library: taken, and put in the library
    title = u("Lists Catalogue Only")
    with tx(app, source="yamtrack"):
        new_book(app, title)
    s, plan = L.run(app, {"notes": [{"id": hexid(), "title": "To read", "body": title}]}, apply=True, now=NOW)
    assert entry_of(app, title)[0]["status"] == "planned" and "catalogue" in s["taken"][0]


def test_read_list_books(app):
    known, new, amb = u("Lists Known Book"), u("Lists New Book"), u("Lists Twin Book")
    done(app, book_with_author(app, known, "Ann Author")[0])
    fill = FakeFill(app, [{"provider": "apple", "title": new, "authors": ["Bea Writer"]},
                          {"provider": "apple", "title": amb, "authors": ["Ola Smith"]},
                          {"provider": "openlibrary", "title": amb, "authors": ["Per Jones"]}])
    nid = hexid()
    body = "{} - Ann Author\n{} by Bea Writer\n{}\n".format(known, new, amb)
    s, plan = L.run(app, {"notes": [{"id": nid, "title": "To read", "body": body}]}, fill=fill, apply=True, now=NOW)
    assert plan["notes"][0]["remove"] == ["{} - Ann Author".format(known), "{} by Bea Writer".format(new)]
    assert entry_of(app, known)[0]["status"] == "completed", "a book you read stays read"
    assert entry_of(app, new)[0]["status"] == "planned" and fill.added == [new]
    row = lines_of(app, nid)[amb]
    assert row["state"] == "unclear" and "different authors" in row["problem"] and len(row["candidates"]) == 2


def test_dry_run_and_the_sync_folder(app, cfg, capsys):
    title = u("Lists Dry Film")
    tm = FakeTmdb([("movie", 930001, title, 2011, 3.0)])
    nid = hexid()
    s, plan = L.run(app, {"notes": [{"id": nid, "title": "To watch", "body": title}]}, tmdb=tm, apply=False, now=NOW)
    assert "would add" in s["added"][0] and not entry_of(app, title) and not lines_of(app, nid)
    # the sync folder, read-only: To watch / To read anywhere (not a conflict copy, not in the trash), or in one notebook
    nb = Notebook()
    w = nb.add(nb.other, "To watch", "Arrival\n")
    nb.add(nb.reviews, "to read", "Stoner\n")
    c = nb.add(nb.other, "To watch", "Conflict copy\n")
    nb.files[c] = nb.files[c].replace("is_conflict: 0", "is_conflict: 1")
    nb.add(nb.other, "To watch", "Deleted\n", deleted_time="1794507108197")
    nb.add(nb.books, "Some review", "x\n")
    got = L.notes_from_sync(nb.tar())["notes"]
    assert sorted(n["title"] for n in got) == ["To watch", "to read"] and next(n for n in got if n["id"] == w)["notebook"] == "Groceries"
    assert [n["title"] for n in L.notes_from_sync(nb.tar(), "reviews")["notes"]] == ["to read"]
    # main: --apply needs the client's notes; with them, the plan is JSON on stdout
    with pytest.raises(SystemExit):
        L.main(cfg, tar=io.BytesIO(b""), apply=True)
    assert L.main(cfg, notes=io.StringIO(json.dumps({"notes": [{"id": hexid(), "title": "To read", "body": u("Nothing")}]})),
                  apply=True, offline=True) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"notes": []} and "left in the note (1)" in out.err and "offline" in out.err


def test_a_conflict_copy_gives_its_own_lines_and_is_never_changed(app):
    a, b = u("Lists Copy Film"), u("Lists Copy Late")
    tm = FakeTmdb([("movie", 940001, a, 2003, 2.0), ("movie", 940002, b, 2004, 2.0)])
    note, copy = hexid(), hexid()
    data = {"notes": [{"id": note, "title": "To watch", "body": "# Films\n" + a},
                      {"id": copy, "title": "To watch", "body": "# Films\n{}\n{}".format(a, b), "conflict_of": note}]}
    s, plan = L.run(app, data, tmdb=tm, apply=True, now=NOW)
    assert entry_of(app, b) and len(entry_of(app, a)) == 1, "the late line from the copy is taken, the shared one once"
    assert [n["id"] for n in plan["notes"]] == [note], "the copy is never changed"
    assert [n.get("conflictCopy", False) for n in s["notes"]] == [False, True] and s["notes"][1]["lines"] == 1


def test_the_console_sees_the_lists_and_the_health_check_follows(cfg, app):
    from medialib.web import queries
    title = u("Lists Console Film")
    tm = FakeTmdb([("movie", 950001, title, 2015, 2.0)])
    nid = hexid()
    L.run(app, {"notes": [{"id": nid, "title": "To watch", "notebook": "Lists", "body": "{}\nNothing called this {}".format(
        title, uuid.uuid4().hex[:4])}]}, tmdb=tm, apply=True, now=NOW)
    r = queries.reading_apps(app)["lists"]
    assert r["lastRun"]["status"] == "ok" and r["lastRun"]["summary"]["notes"][0]["notebook"] == "Lists"
    assert any(a["title"] == title and a["list"] == "watch" and a["id"] for a in r["added"])
    got = {ln["line"].split(" ")[0]: (ln["state"], ln["itemTitle"]) for ln in r["lines"] if ln["note"] == nid}
    assert got == {"Lists": ("taken", title), "Nothing": ("unclear", None)}, "taken: until the client deletes the line"
    # the client failed: recorded as a failed run that says why
    assert L.main(cfg, client_error=io.StringIO("joplin-lists: syncing would raise the apps' minimum version")) == 0
    r = queries.reading_apps(app)["lists"]
    assert r["lastRun"]["status"] == "failed" and "minimum version" in r["lastRun"]["summary"]["client_error"]


def test_health_has_the_lists_once_they_run(site, app):  # noqa: F811
    check = lambda: [x for x in Client(site).call("GET", "api/health")[1]["checks"]  # noqa: E731
                     if x["name"] == "Joplin lists ran ok < 3 h ago"]
    L.record_client_error(app, "joplin-lists: the sync did not complete")
    assert check() and not check()[0]["ok"], "the last run failed: red"
    L.run(app, {"notes": []}, apply=True, now=NOW)
    assert check()[0]["ok"], "a run that worked: green"
