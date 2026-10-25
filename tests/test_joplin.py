"""Joplin review notes -> the library: the note format, matching, and that an
edited note updates what it wrote before instead of adding to it. No
network: a fake stands in for Apple Books / Open Library."""
import datetime
import io
import tarfile
import uuid

import pytest

from conftest import add_entry, new_book, tx
from medialib import db
from medialib.feeds import joplin, joplin_items, notes

TODAY = datetime.date(2026, 10, 25)


def hexid():
    return uuid.uuid4().hex


def folder(fid, title, parent=""):
    return "{}\n\nid: {}\nparent_id: {}\ntype_: 2\nupdated_time: 2026-10-20T10:00:00.000Z\n".format(title, fid, parent)


def note(nid, parent, title, body, updated="2026-10-21T10:00:00.000Z", created="2026-10-21T09:00:00.000Z", **meta):
    extra = "".join("{}: {}\n".format(k, v) for k, v in meta.items())
    return ("{}\n\n{}\n\nid: {}\nparent_id: {}\ncreated_time: {}\nuser_created_time: {}\nupdated_time: {}\n"
            "is_conflict: 0\n{}type_: 1\n").format(title, body, nid, parent, created, created, updated, extra)


class Notebook:
    """A fake sync folder: Reviews/{Books, Movies, Templates} + an unrelated notebook."""

    def __init__(self):
        self.reviews, self.books, self.movies, self.tpl, self.other = (hexid() for _ in range(5))
        self.files = {
            self.reviews: folder(self.reviews, "Reviews"),
            self.books: folder(self.books, "Books", self.reviews),
            self.movies: folder(self.movies, "Movies", self.reviews),
            self.tpl: folder(self.tpl, "Templates", self.reviews),
            self.other: folder(self.other, "Groceries"),
        }

    def add(self, parent, title, body, nid=None, **kw):
        nid = nid or hexid()
        self.files[nid] = note(nid, parent, title, body, **kw)
        return nid

    def tar(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for fid, text in self.files.items():
                data = text.encode()
                info = tarfile.TarInfo("./{}.md".format(fid))
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo("./.resource/x.md")
            info.size = 3
            t.addfile(info, io.BytesIO(b"abc"))
        buf.seek(0)
        return joplin_items.load_tar(buf)


class FakeFill:
    """lookup/add like medialib.fill.service.Fill, from a fixed catalogue."""

    def __init__(self, conn, catalogue):
        self.conn, self.catalogue, self.added = conn, catalogue, []

    def lookup(self, title=None, author=None, isbn=None):
        from medialib.fill import providers as P
        cands = []
        for i, c in enumerate(self.catalogue):
            s = 1.0 if isbn and isbn == c.get("isbn") else P.score(title or "", [author] if author else [],
                                                                   P.Candidate(c["provider"], c["title"], c["authors"], []))
            if s >= 0.82:
                cands.append(dict(c, score=s, index=len(cands), raw=i))
        cands.sort(key=lambda c: -c["score"])
        for n, c in enumerate(cands):
            c["index"] = n
        self.last = cands
        return {"token": "t", "candidates": cands}

    def lookup_edition(self, olid):
        return {"token": "t", "candidates": []}

    def add(self, conn, login, token, index, status="planned", client=None):
        c = self.last[index]
        with db.write_tx(conn, c["provider"], client or "test"):
            item, _ = new_book(conn, c["title"])
            pid = person(conn, c["authors"][0])
            conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source)"
                         " VALUES (%s, 'author', %s, 1, %s)", (pid, item, c["provider"]))
            db.set_source(conn, "owner")
            add_entry(conn, item, status)
        self.added.append(c["title"])
        return {"id": str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (item,)).fetchone()[
            "public_id"]), "existing": False}


def person(conn, name):
    row = conn.execute("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)"
                       " AND disambiguation IS NULL", (name,)).fetchone()
    return row["id"] if row else conn.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id",
                                              (name,)).fetchone()["id"]


def book_with_author(conn, title, author, status="planned"):
    with tx(conn, source="yamtrack", client="test"):
        item, ed = new_book(conn, title)
        pid = person(conn, author)
        conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source)"
                     " VALUES (%s, 'author', %s, 1, 'yamtrack')", (pid, item))
        add_entry(conn, item, status)
    return item, ed


def look(conn, item):
    e = conn.execute("SELECT status, rating, review FROM library.entry WHERE item_id = %s", (item,)).fetchone()
    rs = conn.execute("SELECT state, started_on, ended_on, ended_precision, language, medium, source"
                      " FROM library.consumption WHERE item_id = %s AND deleted_at IS NULL ORDER BY id", (item,)).fetchall()
    return e, rs


def run(conn, nb, fill=None, apply=True):
    return joplin.run(conn, nb.tar(), fill=fill, apply=apply, today=TODAY, log=lambda *_: None)


def uniq(s):
    return "{} {}".format(s, uuid.uuid4().hex[:6])


# ------------------------------------------------------------------ format

def test_parse_note_template_and_lines():
    e = notes.parse_note("Klara and the Sun", "# Rating\n4/5\n# Comment\nLovely.\n\nSecond line.", ["Books"])
    assert e["kind"] == "book" and e["rating"] == 8.0 and e["comment"] == "Lovely.\n\nSecond line."
    e = notes.parse_note("X", "status: to-read\nby: A & B\nin: Italian\ndate: 2024\nrating: 7.5\n---\nhmm", ["Audiobooks"])
    assert e["status"] == "planned" and notes.people(e["by"]) == ["A", "B"] and e["read_in_code"] == "ita"
    assert e["date"] == (datetime.date(2024, 1, 1), "year") and e["rating"] == 7.5 and e["audiobook"]
    assert notes.parse_note("X", "date: 2024-05", ["Movies"])["date"] == (datetime.date(2024, 5, 1), "month")
    assert notes.parse_note("X", "date: 12/05/2024", ["TV-Series"])["kind"] == "series"
    assert notes.parse_note("X", "", [], created="2026-10-02T08:00:00Z")["problems"]       # no kind
    e = notes.parse_note("X", "rating: 11/10\ndate: someday\nin: Klingon", ["Books"], created="2026-10-02T08:00:00Z")
    assert len(e["problems"]) == 2 and e["warnings"] and e["date"] == (datetime.date(2026, 10, 2), "day")
    assert notes.parse_note("X", "isbn: 978-0-306-40615-7", ["Books"])["ids"]["isbn13"] == "9780306406157"


def test_tar_reader_and_notebook_scope():
    nb = Notebook()
    keep = nb.add(nb.books, "A book", "4/5")
    nb.add(nb.other, "Milk", "2 litres")
    nb.add(nb.books, "Gone", "x", deleted_time="1761000000000")
    found = {n["id"] for n, _ in joplin_items.notes_in_notebook(nb.tar(), "Reviews")}
    assert found == {keep}


# ------------------------------------------------------------------- feed

def test_note_for_a_book_in_the_library(app):
    title = uniq("Il viaggio dell'elefante")
    item, _ = book_with_author(app, title, "José Saramago")
    nb = Notebook()
    nid = nb.add(nb.books, title, "by: Jose Saramago\ndate: 2024-05\nin: Italian\n# Rating\n4/5\n# Comment\nBello.")
    nb.add(nb.tpl, "Template (copy me)", "# Rating\n\n# Comment\n")
    s = run(app, nb)
    assert not s["errors"] and not s["unclear"], s
    e, rs = look(app, item)
    assert e["status"] == "completed" and float(e["rating"]) == 8.0 and e["review"] == "Bello."
    assert [(r["state"], r["ended_on"], r["ended_precision"], r["language"], r["source"]) for r in rs] == \
        [("completed", datetime.date(2024, 5, 1), "month", "ita", "joplin")]
    row = app.execute("SELECT source, client FROM audit.change WHERE table_name = 'library.entry'"
                      " ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["source"], row["client"]) == ("owner", "joplin:" + nid)

    assert run(app, nb)["updated"] == []                           # unchanged note: nothing to do
    nb.add(nb.books, title, "by: Jose Saramago\ndate: 2024-05-12\nrating: 5/5\n---\nBellissimo.", nid=nid,
           updated="2026-10-22T10:00:00.000Z")
    s = run(app, nb)
    e, rs = look(app, item)
    assert float(e["rating"]) == 10.0 and e["review"] == "Bellissimo."
    assert len(rs) == 1 and (rs[0]["ended_on"], rs[0]["ended_precision"]) == (datetime.date(2024, 5, 12), "day")


def test_finished_note_closes_the_open_reading(app):
    title = uniq("Exhalation")
    item, ed = book_with_author(app, title, "Ted Chiang")
    with tx(app, client="test"):
        app.execute("INSERT INTO library.consumption (item_id, edition_id, state, started_on, started_precision, source)"
                    " VALUES (%s, %s, 'in_progress', '2026-10-01', 'day', 'kosync')", (item, ed))
        app.execute("UPDATE library.entry SET status = 'in_progress' WHERE item_id = %s", (item,))
    nb = Notebook()
    nb.add(nb.books, title, "by: Ted Chiang\n# Rating\n9/10", created="2026-09-15T10:00:00.000Z")
    run(app, nb)
    e, rs = look(app, item)
    # the note's default date (its creation) is before the start: finished today instead
    assert e["status"] == "completed" and len(rs) == 1 and rs[0]["state"] == "completed"
    assert rs[0]["ended_on"] == TODAY


def test_imported_reading_gets_the_notes_precise_date(app):
    title = uniq("The Remains of the Day")
    item, ed = book_with_author(app, title, "Kazuo Ishiguro")
    with tx(app, source="yamtrack", client="test"):
        app.execute("INSERT INTO library.consumption (item_id, edition_id, state, ended_on, ended_precision, source)"
                    " VALUES (%s, %s, 'completed', '2019-01-01', 'year', 'yamtrack')", (item, ed))
        app.execute("UPDATE library.entry SET status = 'completed' WHERE item_id = %s", (item,))
    nb = Notebook()
    nb.add(nb.books, title, "by: Kazuo Ishiguro\ndate: 2019-08-03\nrating: 4")
    run(app, nb)
    e, rs = look(app, item)
    assert len(rs) == 1 and (rs[0]["ended_on"], rs[0]["ended_precision"]) == (datetime.date(2019, 8, 3), "day")


def test_note_after_readest_finished_it_sets_the_date(app):
    title = uniq("Klara and the Sun")
    item, ed = book_with_author(app, title, "Kazuo Ishiguro")
    with tx(app, client="kosync:iPhone"):
        app.execute("INSERT INTO library.consumption (item_id, edition_id, state, ended_on, ended_precision, source)"
                    " VALUES (%s, %s, 'completed', '2026-10-25', 'day', 'kosync')", (item, ed))
        app.execute("UPDATE library.entry SET status = 'completed' WHERE item_id = %s", (item,))
    nb = Notebook()
    nb.add(nb.books, title, "by: Ishiguro\ndate: 2026-10-18\nrating: 4/5")
    run(app, nb)
    e, rs = look(app, item)
    assert len(rs) == 1 and rs[0]["ended_on"] == datetime.date(2026, 10, 18) and float(e["rating"]) == 8.0
    nb2 = Notebook()
    nb2.add(nb2.books, title, "by: Ishiguro\ndate: 2026\nrating: 4/5")     # a coarser date keeps the precise one
    run(app, nb2)
    assert [r["ended_on"] for r in look(app, item)[1]] == [datetime.date(2026, 10, 18)]


def test_movie_not_in_the_library_is_added_from_the_note(app):
    title = uniq("Perfect Days")
    nb = Notebook()
    nb.add(nb.movies, title, "by: Wim Wenders\nyear: 2023\noriginal: Japanese\nrating: 5/5\n---\nKomorebi.")
    s = run(app, nb)
    assert len(s["added"]) == 1 and "added from the note" in s["added"][0]
    row = app.execute("SELECT i.id, i.kind, i.original_language FROM catalog.media_item i WHERE i.title = %s",
                      (title,)).fetchone()
    assert row["kind"] == "movie" and row["original_language"] == "jpn"
    e, rs = look(app, row["id"])
    assert e["status"] == "completed" and float(e["rating"]) == 10.0 and rs[0]["state"] == "completed"
    assert app.execute("SELECT p.name FROM catalog.contribution c JOIN catalog.person p ON p.id = c.person_id"
                       " WHERE c.item_id = %s AND c.role = 'director'", (row["id"],)).fetchone()["name"] == "Wim Wenders"


def test_new_book_from_the_providers_or_unclear(app):
    good, vague = uniq("Il Gattopardo"), uniq("Something Vague")
    fill = FakeFill(app, [{"provider": "apple", "title": good, "authors": ["Giuseppe Tomasi di Lampedusa"]},
                          {"provider": "apple", "title": vague + " Part One", "authors": ["Someone"]}])
    nb = Notebook()
    nb.add(nb.books, good, "by: Giuseppe Tomasi di Lampedusa\nstatus: to-read\nlists: Italian classics")
    vid = nb.add(nb.books, vague, "status: finished\nrating: 3/5")
    s = run(app, nb, fill)
    assert fill.added == [good] and len(s["unclear"]) == 1 and vague in s["unclear"][0]
    item = app.execute("SELECT id FROM catalog.media_item WHERE title = %s", (good,)).fetchone()["id"]
    assert look(app, item)[0]["status"] == "planned"
    assert app.execute("SELECT 1 AS x FROM library.shelf_item si JOIN library.shelf s ON s.id = si.shelf_id"
                       " WHERE s.name = 'Italian classics' AND si.item_id = %s", (item,)).fetchone()
    st = app.execute("SELECT state, problem FROM sync.joplin_note WHERE note_id = %s", (vid,)).fetchone()
    assert st["state"] == "unclear" and "no confident match" in st["problem"]

    # the owner adds it in the console; the unchanged note finds it on the next run (no new search)
    v_item, _ = book_with_author(app, vague, "Someone")
    fill.catalogue = []
    s = run(app, nb, fill)
    assert not s["unclear"] and look(app, v_item)[0]["status"] == "completed"

    # the list is taken off the note: the book leaves that collection
    nb.add(nb.books, good, "by: Giuseppe Tomasi di Lampedusa\nstatus: to-read",
           nid=next(k for k, v in nb.files.items() if good in v), updated="2026-10-23T10:00:00.000Z")
    run(app, nb, fill)
    assert not app.execute("SELECT 1 AS x FROM library.shelf_item si JOIN library.shelf s ON s.id = si.shelf_id"
                           " WHERE s.name = 'Italian classics' AND si.item_id = %s", (item,)).fetchone()


def test_dry_run_writes_nothing(app):
    title = uniq("Finzioni")
    item, _ = book_with_author(app, title, "Jorge Luis Borges")
    nb = Notebook()
    nid = nb.add(nb.books, title, "by: Borges\nrating: 4/5")
    s = run(app, nb, apply=False)
    assert s["updated"] and "in the library" in s["updated"][0]
    assert look(app, item)[1] == [] and look(app, item)[0]["rating"] is None
    assert not app.execute("SELECT 1 AS x FROM sync.joplin_note WHERE note_id = %s", (nid,)).fetchone()


def test_ambiguous_title_is_unclear(app):
    title = uniq("Twin")
    book_with_author(app, title, "Anna One")
    book_with_author(app, title, "Bea Two")
    nb = Notebook()
    nb.add(nb.books, title, "rating: 3/5")
    s = run(app, nb)
    assert s["unclear"] and "several items" in s["unclear"][0]


@pytest.fixture
def app(cfg):
    conn = db.connect(cfg, db.ROLE_APP)
    yield conn
    conn.close()
