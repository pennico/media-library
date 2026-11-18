"""Series suggestions (D83-D86): reading a series from a source's field or a title, Open Library's series and
its list of works, keeping them as suggestions (never applied), applying one in the numbers' order, matching a
series you have, dismissing. Offline: Open Library is a fake fetch with recorded-style answers. Every title here
is made up."""
import dataclasses
import datetime
import json
import socket
import threading
import urllib.parse
import uuid

import pytest

from conftest import add_entry, new_book, tx
from test_fill import NoCovers, random_isbn
from medialib import db, groups
from medialib.fill import providers as P
from medialib.fill import series as S
from medialib.fill import service
from medialib.web import queries, writes

OWNER = "owner@example.test"


def uniq(t):
    return "{} {}".format(t, uuid.uuid4().hex[:6])


# ------------------------------------------------------------------ reading a series

def test_series_in_titles():
    t = S.parse_title
    assert t("Moon Garden (Tide, Book 2)") == ("Tide", 2)
    assert t("Moon Garden (The Tide Saga #2)") == ("The Tide Saga", 2)
    assert t("Moon Garden: Tide, Book Two") == ("Tide", 2)
    assert t("Moon Garden: Book Two of the Tide Saga") == ("Tide Saga", 2)
    assert t("Glassworks Volume 3: The Long Night") == ("Glassworks", 3)
    assert t("Lantern Road #4") == ("Lantern Road", 4)
    assert t("A History of Small Towns, Volume II") == ("A History of Small Towns", 2)
    assert t("Il giardino di prova. Vol. 2") == ("Il giardino di prova", 2)
    # a number without a marker, a marker without a name, a name cut from a sentence: none
    for title in ("Station 11", "Room 404 (Grey Edition 2)", "Book 2", "Volume Three", "On the Measuring of Volume II",
                  "Catch-31", "Notes, 1970-80 — Volume 1 and Volume 2", "Moon Garden (Penguin Classics)"):
        assert t(title) is None, title


def test_series_fields_names_and_numbers():
    f = S.parse_field
    assert f("Tide saga -- bk. 2") == ("Tide saga", 2)
    assert f("Tide Saga ; 3") == ("Tide Saga", 3)
    assert f("Tide Saga, book 4") == ("Tide Saga", 4)
    assert f("Tide Saga (Book 5)") == ("Tide Saga", 5)
    assert f("Tide Saga #2.5") == ("Tide Saga", 2.5)
    assert f("Book Six of the Tide Saga") == ("Tide Saga", 6)
    assert f("Grey Imprint") == ("Grey Imprint", None)
    assert f("Grey Imprint", need_number=True) is None               # an edition's unnumbered series: a publisher's
    assert [S.number(x) for x in ("2", "2.5", "II", "xiv", "two", "Second", "1-2", "", "0")] == [2, 2.5, 2, 14, 2, 2, None, None, None]
    assert S.key("The Tide Saga") == S.key("tide") == S.key("Tíde Cycle") == "tide"
    assert S.key("The Series") == "series"
    assert S.plausible("Tide Saga", 3) and S.plausible("Tide Saga", None)
    for name, n in (("Penguin Classics", None), ("Everyman's Library", 12), ("Oscar Grey", 4), ("A Comet book", None),
                    ("Tide Saga", 120), ("Trilogy", 1), ("The Series", 2)):
        assert not S.plausible(name, n), name


def test_merged_keeps_one_per_series_with_the_strongest_name():
    got = S.merged([S.Found("Tide", 2, "owner", "title", raw="Moon Garden (Tide, Book 2)"),
                    S.Found("The Tide Saga", None, "openlibrary", "series", "OL1L", "The Tide Saga"),
                    S.Found("Tide saga", 2, "openlibrary", "series", raw="Tide saga ; 2"),
                    S.Found("Grey Classics", 3, "openlibrary", "series", raw="Grey Classics ; 3")])
    assert [(f.name, f.number, f.ref, f.source) for f in got] == [("The Tide Saga", 2, "OL1L", "openlibrary")]


# ------------------------------------------------------------------ a fake Open Library

class FakeOL:
    """Open Library's search, answering by query: isbn=, q=key:/works/…, q=series_key:…, title=&author=."""

    def __init__(self):
        self.works, self.urls = [], []

    def work(self, title, authors, isbn=None, series=None, pos=None, key=None):
        w = {"key": "/works/" + (key or "OL{}W".format(uuid.uuid4().int % 10**8)), "title": title, "author_name": authors,
             "_isbn": isbn}
        if series:
            w.update(series_key=[series[0]], series_name=[series[1]], series_position=[pos] if pos is not None else [])
        self.works.append(w)
        return w

    def __call__(self, url):
        self.urls.append(url)
        if url.startswith(S.SEARCH):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
            if q.get("fields") != S.FIELDS:                       # the fill's own search: nothing here
                return {"docs": []}
            docs = []
            for w in self.works:
                if "isbn" in q and w["_isbn"] == q["isbn"] or q.get("q") == "key:" + w["key"] \
                        or q.get("q", "").startswith("series_key:") and q["q"][11:] in (w.get("series_key") or []) \
                        or "title" in q and P.main_title(w["title"]) == q["title"]:
                    docs.append({k: v for k, v in w.items() if not k.startswith("_")})
            return {"numFound": len(docs), "docs": docs}
        return None


def book(conn, title, author, year=None, isbn=None):
    item, ed = new_book(conn, title)
    if year:
        conn.execute("UPDATE catalog.media_item SET first_published = %s, first_published_precision = 'year' WHERE id = %s",
                     (datetime.date(year, 1, 1), item))
    if author:
        row = conn.execute("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)", (author,)).fetchone() \
            or conn.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id", (author,)).fetchone()
        conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source) VALUES (%s, 'author', %s, 1, 'owner')",
                     (row["id"], item))
    if isbn:
        conn.execute("INSERT INTO catalog.external_id (provider, value, edition_id, source) VALUES ('isbn13', %s, %s, 'owner')",
                     (isbn, ed))
    add_entry(conn, item)
    return item


def pid(conn, item):
    return str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["public_id"])


def rows(conn, item):
    return {r["name_key"]: r for r in conn.execute("SELECT * FROM ingest.series_suggestion WHERE item_id = %s", (item,))}


def saga(conn, ol, n=4, author=None):
    """A made-up series of n books in the library and at Open Library (numbers 1..n, dated against them)."""
    author = author or uniq("Ada Tester")
    name, ref = uniq("The Tide Saga"), "OL{}L".format(uuid.uuid4().int % 10**7)
    years = [2010, 2005, 2012, 2001, 2003][:n]
    items = []
    for k in range(n):
        title, isbn = uniq("Tide Book {}".format(k + 1)), random_isbn()
        items.append(book(conn, title, author, years[k], isbn))
        ol.work(title, [author], isbn, (ref, name), str(k + 1))
    return name, ref, items, author


# ------------------------------------------------------------------ looking up and keeping

def test_a_lookup_keeps_suggestions_for_the_book_and_the_others_of_its_series(app):
    ol = FakeOL()
    with tx(app):
        name, ref, items, author = saga(app, ol)
        omnibus = ol.work("Tide Books 1-2", [author], None, (ref, name), "1-2")       # not in the library
        lookalike = book(app, ol.works[1]["title"], uniq("Other Writer"), 2005)   # the same title, another author
    got = S.suggest(app, S.Finder(ol), S.books(app, [pid(app, items[0])]), "test")
    assert got["looked"] == [pid(app, items[0])] and not got["error"]
    assert sum(u.startswith(S.SEARCH + "?isbn=") for u in ol.urls) == 1                 # one request for the book
    assert sum("series_key%3A" in u for u in ol.urls) == 1                              # one for the series' works
    k = S.key(name)
    for n, item in enumerate(items, 1):
        r = rows(app, item)[k]
        assert (r["name"], float(r["number"]), r["source"], r["how"], r["ref"]) == (name, n, "openlibrary", "series", ref)
    assert not rows(app, lookalike)                                                      # another author: not matched
    d = queries.detail(app, pid(app, items[1]))
    s = d["seriesSuggest"][0]
    assert (s["name"], s["number"], s["group"], s["key"]) == (name, 2, None, k)
    assert [(o["id"], o["number"]) for o in s["others"]] == [(pid(app, items[0]), 1), (pid(app, items[2]), 3), (pid(app, items[3]), 4)]
    listed = next(x for x in queries.series_suggestions(app) if x["key"] == k)
    assert listed["name"] == name and [b["id"] for b in listed["books"]] == [pid(app, i) for i in items]


def test_titles_and_edition_fields_and_no_publishers_series(app):
    ol = FakeOL()
    with tx(app):
        author = uniq("Bea Tester")
        item = book(app, "Moon Garden (Grey Tide, Book 3)", author, 2011, random_isbn())
    edition = {"series": ["Grey Classics ; 12", "Fare il Mondo", "Grey Tide ; 3"]}
    cands = [P.Candidate("openlibrary", "Moon Garden", [author], [], raw={"edition": edition, "work": {}}),
             P.Candidate("apple", "Moon Garden: Grey Tide, Book Three", [author], [])]
    S.suggest(app, S.Finder(ol), S.books(app, [pid(app, item)]), "test", extra={item: S.from_candidates(cands)})
    r = rows(app, item)
    assert list(r) == ["grey tide"]                                       # one series; no publisher's, no unnumbered one
    assert (r["grey tide"]["name"], float(r["grey tide"]["number"]), r["grey tide"]["how"], r["grey tide"]["source"]) == \
        ("Grey Tide", 3, "title", "owner")                                # its own title, labelled with the title's source


def test_a_book_in_a_series_is_not_looked_up_and_a_failing_source_stops(app):
    ol = FakeOL()
    with tx(app):
        a, b, c = (book(app, uniq("Quiet Book"), uniq("Cy Tester"), 2001, random_isbn()) for _ in range(3))
        g = groups.create(app, "book", uniq("Quiet Cycle"))["id"]
        groups.place(app, g, [a])
    S.suggest(app, S.Finder(ol), S.books(app, [pid(app, a)]), "test")
    assert ol.urls == []

    def down(url):
        raise service.FillError("lookup failed (openlibrary.org): offline")
    got = S.suggest(app, S.Finder(down), S.books(app, [pid(app, x) for x in (b, c)]), "test")
    assert got["looked"] == [] and got["error"].endswith("offline") and sorted(got["left"]) == sorted([pid(app, b), pid(app, c)])


def test_a_dismissed_suggestion_stays_dismissed(app):
    ol = FakeOL()
    with tx(app):
        name, ref, items, _ = saga(app, ol, 2)
    S.suggest(app, S.Finder(ol), S.books(app, [pid(app, items[0])]), "test")
    k = S.key(name)
    writes.series_dismiss(app, OWNER, {"items": [pid(app, items[0])], "key": k})
    assert queries.detail(app, pid(app, items[0]))["seriesSuggest"] == []
    assert [o["id"] for o in queries.detail(app, pid(app, items[1]))["seriesSuggest"][0]["others"]] == []
    S.suggest(app, S.Finder(ol), S.books(app, [pid(app, items[0])]), "test")             # found again: still hidden
    assert rows(app, items[0])[k]["dismissed_at"] is not None
    writes.series_dismiss(app, OWNER, {"items": [pid(app, items[0])], "key": k, "back": True})
    assert queries.detail(app, pid(app, items[0]))["seriesSuggest"][0]["name"] == name


def test_fill_missing_suggests_and_never_applies(app):
    ol = FakeOL()
    with tx(app):
        name, ref, items, _ = saga(app, ol, 3)
    f = service.Fill(NoCovers(), ol)
    f.fill_missing(app, OWNER, pid(app, items[2]))
    d = queries.detail(app, pid(app, items[2]))
    assert d["coll"] is None and d["seriesSuggest"][0]["name"] == name and d["seriesSuggest"][0]["number"] == 3
    assert all(groups.membership(app, i) == (None, None) for i in items)                   # nothing put in a series


def test_the_recorded_answers_and_nothing_else(tmp_path, monkeypatch):
    (tmp_path / "a.json").write_text(json.dumps([{"url": "https://openlibrary.org/x", "body": {"docs": []}}]))
    fetch = service.recorded_fetcher(str(tmp_path))
    assert fetch("https://openlibrary.org/x") == {"docs": []}
    with pytest.raises(service.FillError, match="not among the recorded answers"):
        fetch("https://openlibrary.org/y")
    monkeypatch.setenv("MEDIALIB_HTTP_RECORDED", str(tmp_path))
    with pytest.raises(service.FillError):
        service.fetcher("/nowhere")("https://itunes.apple.com/lookup?isbn=1")


def test_a_purge_takes_the_suggestions(app, cfg, tmp_path):
    ol = FakeOL()
    with tx(app):
        name, ref, items, _ = saga(app, ol, 2)
    S.suggest(app, S.Finder(ol), S.books(app, [pid(app, items[0])]), "test")
    with tx(app):
        app.execute("UPDATE library.entry SET deleted_at = now() WHERE item_id = %s", (items[1],))
        app.execute("UPDATE catalog.media_item SET deleted_at = now() WHERE id = %s", (items[1],))
    assert [o["id"] for o in queries.detail(app, pid(app, items[0]))["seriesSuggest"][0]["others"]] == []   # trashed: not offered
    writes.purge_item(app, "test", items[1], str(tmp_path))
    assert rows(app, items[1]) == {}


# ------------------------------------------------------------------ applying from the console

@pytest.fixture(scope="module")
def web(cfg, tmp_path_factory):
    """The console with a fake Open Library behind its fill (the same as test_web.site, keeping the app)."""
    import test_web
    from medialib.web import auth as auth_mod, server
    tmp = tmp_path_factory.mktemp("web-series")
    (tmp / "password").write_text(auth_mod.hash_password(test_web.PASSWORD) + "\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    origin = "http://127.0.0.1:{}".format(port)
    app_ = server.App(dataclasses.replace(cfg, media_dir=str(tmp / "media")), owner_login=test_web.OWNER, origin=origin,
                      password_file=str(tmp / "password"), passkeys_file=str(tmp / "passkeys.json"),
                      state_dir=str(tmp / "state"), cookie_secure=False)
    ol = FakeOL()
    app_.fill = service.Fill(NoCovers(), ol)
    httpd = server.Server(("127.0.0.1", port), server.make_handler(app_))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"base": origin + "/", "origin": origin, "ol": ol}
    httpd.shutdown()
    app_.close()


def client(web):
    from test_web import Client
    return Client(web).unlock()


def test_suggest_in_batches_then_create_in_the_numbers_order_and_undo(web, app):
    c, ol = client(web), web["ol"]
    with tx(app):
        name, ref, items, author = saga(app, ol, 4)                    # dated 2010, 2005, 2012, 2001 for numbers 1-4
        loose = book(app, uniq("Tide Interlude"), author, 2011)        # the same author, no number: by its date
    ps = [pid(app, i) for i in items]
    assert c.call("POST", "api/series/suggest", {"items": ps * 3})[0] == 400                 # at most 10 a request
    s, r = c.call("POST", "api/series/suggest", {"items": [ps[0]]})
    assert s == 200 and r["looked"] == [ps[0]] and r["found"][ps[3]] == [name]
    k = S.key(name)
    listed = next(x for x in r["series"] if x["key"] == k)
    assert [b["id"] for b in listed["books"]] == ps and listed["group"] is None
    # Create series from the suggestion: by the numbers (not the dates); a book without one by its date
    with tx(app):
        app.execute("INSERT INTO ingest.series_suggestion (item_id, name_key, name, source, how) VALUES (%s, %s, %s, 'owner', 'title')",
                    (loose, k, name))
    s, g = c.call("POST", "api/groups", {"kind": "book", "name": name, "items": ps + [pid(app, loose)], "suggestion": k})
    assert s == 200 and g["items"] == [ps[0], ps[1], pid(app, loose), ps[2], ps[3]]
    assert not any(x["key"] == k for x in c.call("GET", "api/series/suggestions")[1]["series"])   # applied: gone
    assert queries.detail(app, ps[1])["seriesSuggest"] == []
    # its Undo (the page deletes the new series): the suggestions are back
    assert c.call("DELETE", "api/groups/" + g["id"])[0] == 200
    assert next(x for x in c.call("GET", "api/series/suggestions")[1]["series"] if x["key"] == k)["group"] is None


def test_add_to_a_series_you_have_by_its_number(web, app):
    c, ol = client(web), web["ol"]
    with tx(app):
        name, ref, items, author = saga(app, ol, 3)
    ps = [pid(app, i) for i in items]
    c.call("POST", "api/series/suggest", {"items": [ps[1]]})
    mine = "the " + name.split(" ", 1)[1].upper()                      # yours, written differently: still a match
    s, g = c.call("POST", "api/groups", {"kind": "book", "name": mine, "items": [ps[2], ps[0]], "suggestion": S.key(name)})
    assert g["items"] == [ps[0], ps[2]]
    d = c.call("GET", "api/items/" + ps[1])[1]
    sug = d["seriesSuggest"][0]
    assert sug["group"] == {"id": g["id"], "name": mine} and sug["number"] == 2
    s, r = c.call("POST", "api/groups/" + g["id"] + "/items", {"add": [ps[1]], "suggestion": sug["key"]})
    assert s == 200 and r["items"] == ps                                # 1, 2, 3 by the numbers (2 is dated before 1)
    assert c.call("POST", "api/groups", {"kind": "book", "name": uniq("X"), "items": [ps[0]], "suggestion": 5})[0] == 400


def test_dismiss_from_the_console(web, app):
    c, ol = client(web), web["ol"]
    with tx(app):
        name, ref, items, _ = saga(app, ol, 2)
    ps = [pid(app, i) for i in items]
    c.call("POST", "api/series/suggest", {"items": ps})
    k = S.key(name)
    s, r = c.call("POST", "api/series/dismiss", {"items": ps, "key": k})
    assert s == 200 and r["changed"] == 2 and not any(x["key"] == k for x in r["series"])
    s, r = c.call("POST", "api/series/dismiss", {"items": ps, "key": k, "back": True})
    assert r["changed"] == 2 and any(x["key"] == k for x in r["series"])
    assert c.call("POST", "api/series/dismiss", {"items": ps, "key": ""})[0] == 400
    assert c.call("POST", "api/series/suggest", {"items": ps}, X_Console_Action=None)[0] in (400, 403)
