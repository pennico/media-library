"""Book series and film collections as one kind of group (D76-D78): the database rules of
catalog.item_group / catalog.group_member, the placing in medialib/groups.py, and the console's API.
Every title here is made up."""
import datetime
import uuid

import psycopg
import pytest

from conftest import add_entry, new_book, tx
from test_schema import sqlstate
from medialib import db, groups, screen
from medialib.web import writes

OWNER = "owner@example.test"


def uniq(t):
    return "{} {}".format(t, uuid.uuid4().hex[:6])


def book(conn, title, first=None, edition=None, status="planned"):
    """A book in the library; `first` its first published year, `edition` its edition's year."""
    item, ed = new_book(conn, title)
    if first:
        conn.execute("UPDATE catalog.media_item SET first_published = %s, first_published_precision = 'year' WHERE id = %s",
                     (datetime.date(first, 1, 1), item))
    if edition:
        conn.execute("UPDATE catalog.edition SET published_on = %s, published_precision = 'year' WHERE id = %s",
                     (datetime.date(edition, 1, 1), ed))
    add_entry(conn, item, status)
    return item


def pid(conn, item):
    return str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["public_id"])


def order(conn, gid):
    return [r["id"] for r in groups.members(conn, gid)]


def movie(conn, title, year):
    item = conn.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('movie', %s) RETURNING id", (title,)).fetchone()["id"]
    conn.execute("INSERT INTO catalog.movie (item_id, release_date) VALUES (%s, %s)", (item, datetime.date(year, 5, 1)))
    add_entry(conn, item)
    return item


# ------------------------------------------------------------------ database rules

def test_a_group_holds_items_of_its_own_kind_once(app):
    with tx(app):
        b1, b2 = book(app, uniq("Dragon Egg")), book(app, uniq("Dragon Rider"))
        f = movie(app, uniq("Dragon Film"), 2006)
        s = groups.create(app, "book", uniq("Dragon Cycle"))["id"]
        groups.place(app, s, [b1, b2])
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="group_member_kind"):
        with tx(app):
            app.execute("INSERT INTO catalog.group_member (item_id, item_kind, group_id, position) VALUES (%s, 'movie', %s, 3)",
                        (f, s))
    with pytest.raises(psycopg.errors.UniqueViolation):           # one group per item
        with tx(app):
            app.execute("INSERT INTO catalog.group_member (item_id, item_kind, group_id, position) VALUES (%s, 'book', %s, 3)",
                        (b1, s))
    with pytest.raises(psycopg.errors.UniqueViolation, match="group_member_position"):   # one item per place, at commit
        with tx(app):
            app.execute("UPDATE catalog.group_member SET position = 1 WHERE item_id = %s", (b2,))
    with pytest.raises(psycopg.errors.CheckViolation):            # TMDB's collections are films
        with tx(app, source="tmdb"):
            app.execute("INSERT INTO catalog.item_group (name, media_kind, tmdb_id) VALUES ('X', 'book', '42')")
    with pytest.raises(psycopg.errors.CheckViolation, match="cannot change kind"):
        with tx(app):
            app.execute("UPDATE catalog.item_group SET media_kind = 'movie' WHERE id = %s", (s,))


def test_tmdb_never_moves_or_takes_out_what_the_owner_grouped(app):
    with tx(app, source="tmdb"):
        a, b = movie(app, uniq("Heist One"), 2001), movie(app, uniq("Heist Two"), 2004)
        tm = groups.create(app, "movie", uniq("Heist Collection"), str(900_000 + uuid.uuid4().int % 99_999))["id"]
        groups.place(app, tm, [a])
    with tx(app):
        mine = groups.create(app, "movie", uniq("My heists"))["id"]
        groups.place(app, mine, [b])
        groups.leave(app, [a])                                     # the owner: not in a collection
    with sqlstate("ML002", "set by the owner"):
        with tx(app, source="tmdb"):
            app.execute("UPDATE catalog.group_member SET group_id = %s, position = 9 WHERE item_id = %s", (tm, b))
    with tx(app, source="tmdb"):                                   # the writer skips both before the database would refuse
        assert not screen.join_collection(app, a, {"name": "Heist", "tmdb": groups.group(app, tm)["tmdb_id"]})
        assert not screen.join_collection(app, b, {"name": "Heist", "tmdb": groups.group(app, tm)["tmdb_id"]})
    assert groups.membership(app, a) == (None, "owner") and groups.membership(app, b) == (mine, "owner")


def test_a_locked_item_is_grouped_by_the_owner_only(app):
    with tx(app):
        item, _ = new_book(app, uniq("Locked volume"), locked=True)
        add_entry(app, item)
        s = groups.create(app, "book", uniq("Locked cycle"))["id"]
    with sqlstate("ML002", "locked"):
        with tx(app, source="openlibrary"):
            groups.place(app, s, [item])
    with tx(app):
        groups.place(app, s, [item])
    assert groups.membership(app, item) == (s, "owner")


def test_the_app_cannot_delete_a_membership_and_a_purge_takes_it(app, cfg, tmp_path):
    with tx(app):
        a, b = book(app, uniq("Purged volume"), 1990), book(app, uniq("Kept volume"), 1991)
        s = groups.create(app, "book", uniq("Purge cycle"))["id"]
        groups.place(app, s, [a, b])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tx(app):
            app.execute("DELETE FROM catalog.group_member WHERE item_id = %s", (a,))
    writes.trash_item(app, OWNER, pid(app, a))
    assert order(app, s) == [a, b]                                 # the trash keeps its place
    writes.purge_item(app, "test", a, str(tmp_path))
    assert order(app, s) == [b]


# ------------------------------------------------------------------- placing

def test_items_join_in_order_of_first_publication(app):
    with tx(app):
        third = book(app, uniq("Third"), first=2008)
        first = book(app, uniq("First"), edition=2002)             # no first-published date: the edition's
        second = book(app, uniq("Second"), first=2005, edition=2011)
        undated = book(app, uniq("Undated"))
        s = groups.create(app, "book", uniq("Ordered cycle"))["id"]
        assert set(groups.place(app, s, [undated, third, first, second])) == {undated, third, first, second}
    assert order(app, s) == [first, second, third, undated]
    with tx(app):                                                  # a later one goes where its date belongs
        between = book(app, uniq("Between"), first=2006)
        assert groups.place(app, s, [between, first]) == [between]    # one already in it stays put
    assert order(app, s) == [first, second, between, third, undated]


def test_the_order_keeps_a_trashed_member_in_its_place(app):
    with tx(app):
        a, b, c, d = (book(app, uniq(t), y) for t, y in (("A", 2001), ("B", 2002), ("C", 2003), ("D", 2004)))
        s = groups.create(app, "book", uniq("Trash cycle"))["id"]
        groups.place(app, s, [a, b, c, d])
    writes.trash_item(app, OWNER, pid(app, b))
    with tx(app):
        groups.set_order(app, s, [d, a, c])                        # the console knows the three in the library
    assert order(app, s) == [d, b, a, c]                           # B keeps the second place
    writes.restore_item(app, OWNER, pid(app, b))
    assert groups.live_order(app, s) == [pid(app, x) for x in (d, b, a, c)]
    with tx(app):
        groups.leave(app, [a])
        groups.set_order(app, s, [d, b, a, c])                     # the Undo of the remove: back in its place
    assert order(app, s) == [d, b, a, c]


# ------------------------------------------------------------------ console API

@pytest.fixture(scope="module")
def web(cfg, tmp_path_factory):
    import test_web
    gen = test_web.site.__wrapped__(cfg, tmp_path_factory)
    s = next(gen)
    yield s
    next(gen, None)


def client(web):
    from test_web import Client
    return Client(web).unlock()


def test_create_a_book_series_from_a_selection(web, cfg, app):
    with tx(app):
        e3, e1, e2 = book(app, uniq("Dragon Three"), 2005), book(app, uniq("Dragon One"), 2002), book(app, uniq("Dragon Two"), 2003)
        f = movie(app, uniq("Not a book"), 2006)
    p3, p1, p2, pf = (pid(app, x) for x in (e3, e1, e2, f))
    c = client(web)
    name = uniq("Dragon Cycle")
    s, r = c.call("POST", "api/groups", {"kind": "book", "name": name, "items": [p3, p1, pf]})
    assert s == 400 and "is not a book" in r["error"]
    s, r = c.call("POST", "api/groups", {"kind": "book", "name": name, "items": [p3, p1]})
    assert s == 200 and r["items"] == [p1, p3] and r["kind"] == "book" and r["moved"] == []
    gid = r["id"]
    s, lib = c.call("GET", "api/library")
    rows = {i["id"]: i for i in lib["items"]}
    assert rows[p1]["coll"]["name"] == name and rows[p1]["coll"]["pos"] < rows[p3]["coll"]["pos"]
    assert rows[p2]["coll"] is None
    assert next(g for g in lib["groups"] if g["id"] == gid) == {"id": gid, "name": name, "kind": "book", "tmdb": False, "n": 2}
    # the same name again: refused, with what to do instead
    assert "Add to series" in c.call("POST", "api/groups", {"kind": "book", "name": name.upper(), "items": [p2]})[1]["error"]
    # Add to series: where its date belongs
    s, r = c.call("POST", "api/groups/" + gid + "/items", {"add": [p2]})
    assert s == 200 and r["items"] == [p1, p2, p3]
    # reorder, Undo
    s, r = c.call("POST", "api/groups/" + gid + "/order", {"items": [p3, p1, p2]})
    assert r["items"] == [p3, p1, p2]
    assert c.call("POST", "api/groups/" + gid + "/order", {"items": [p1, p2, p3]})[1]["items"] == [p1, p2, p3]
    # the history says it in words
    s, d = c.call("GET", "api/items/" + p2)
    assert any(h["changes"].get("series") == [None, name] for h in d["history"])
    assert d["prov"]["group"]["group_id"] == "owner"


def test_a_book_in_another_series_moves_only_when_asked(web, cfg, app):
    with tx(app):
        a, b, c_ = book(app, uniq("Shared A"), 2001), book(app, uniq("Shared B"), 2002), book(app, uniq("Shared C"), 2003)
    pa, pb, pc = (pid(app, x) for x in (a, b, c_))
    c = client(web)
    first = c.call("POST", "api/groups", {"kind": "book", "name": uniq("First cycle"), "items": [pa, pb]})[1]
    s, r = c.call("POST", "api/groups", {"kind": "book", "name": uniq("Second cycle"), "items": [pb, pc]})
    assert s == 400 and "Shared B" in r["error"] and first["name"] in r["error"]
    s, r = c.call("POST", "api/groups", {"kind": "book", "name": uniq("Second cycle"), "items": [pb, pc], "move": True})
    assert s == 200 and r["items"] == [pb, pc]
    assert r["moved"] == [{"id": pb, "title": app.execute("SELECT title FROM catalog.media_item WHERE id = %s", (b,)).fetchone()["title"],
                           "from": {"id": first["id"], "name": first["name"]}}]
    # Undo: the new series goes, and the moved book goes back to its place in the first
    assert c.call("DELETE", "api/groups/" + r["id"])[0] == 200
    assert c.call("POST", "api/groups/" + first["id"] + "/order", {"items": [pa, pb]})[1]["items"] == [pa, pb]
    s, lib = c.call("GET", "api/library")
    rows = {i["id"]: i for i in lib["items"]}
    assert rows[pc]["coll"] is None and rows[pb]["coll"]["id"] == first["id"]
    assert all(g["id"] != r["id"] for g in lib["groups"])


def test_remove_rename_delete_and_undo(web, cfg, app):
    with tx(app):
        items = [book(app, uniq("Volume {}".format(n)), 2000 + n) for n in range(3)]
    p = [pid(app, x) for x in items]
    c = client(web)
    g = c.call("POST", "api/groups", {"kind": "book", "name": uniq("Three volumes"), "items": p})[1]
    # Remove from series on a selection, Undo by the order from before
    s, r = c.call("POST", "api/groups/leave", {"items": [p[1]]})
    assert s == 200 and r["left"] == [p[1]]
    s, lib = c.call("GET", "api/library")
    assert next(i for i in lib["items"] if i["id"] == p[1])["coll"] is None
    assert c.call("POST", "api/groups/" + g["id"] + "/order", {"items": p})[1]["items"] == p
    # remove through the series' own panel
    assert c.call("POST", "api/groups/" + g["id"] + "/items", {"remove": [p[2]]})[1]["items"] == p[:2]
    # rename; a name another series has is refused
    other = c.call("POST", "api/groups", {"kind": "book", "name": uniq("Other"), "items": [p[2]]})[1]
    assert c.call("PATCH", "api/groups/" + g["id"], {"name": "  " + other["name"].lower() + " "})[0] == 409
    s, r = c.call("PATCH", "api/groups/" + g["id"], {"name": g["name"] + " (renamed)"})
    assert s == 200 and r["name"] == g["name"] + " (renamed)"
    # delete: the books stay in the library, in no series; Undo brings it back as it was
    s, r = c.call("DELETE", "api/groups/" + g["id"])
    assert s == 200 and r["items"] == p[:2]
    s, lib = c.call("GET", "api/library")
    assert all(i["coll"] is None for i in lib["items"] if i["id"] in p[:2]) and all(x["id"] != g["id"] for x in lib["groups"])
    assert c.call("POST", "api/groups/" + g["id"] + "/items", {"add": [p[0]]})[0] == 404
    s, r = c.call("POST", "api/groups/" + g["id"] + "/restore")
    assert s == 200 and r["items"] == p[:2]
    assert c.call("POST", "api/groups/" + g["id"] + "/restore")[0] == 404     # only a deleted one


def test_a_film_collection_is_made_the_same_way(web, cfg, app):
    with tx(app):
        a, b = movie(app, uniq("Caper Two"), 2004), movie(app, uniq("Caper One"), 2001)
        bk = book(app, uniq("Caper novel"), 2000)
    pa, pb, pk = (pid(app, x) for x in (a, b, bk))
    c = client(web)
    s, r = c.call("POST", "api/groups", {"kind": "movie", "name": uniq("Capers"), "items": [pa, pb, pk]})
    assert s == 400 and "can't go in a film collection" in r["error"]
    s, r = c.call("POST", "api/groups", {"kind": "movie", "name": uniq("Capers"), "items": [pa, pb]})
    assert s == 200 and r["items"] == [pb, pa] and r["kind"] == "movie"
    # TMDB filling the film later keeps the owner's collection
    with tx(app, source="tmdb"):
        assert not screen.join_collection(app, a, {"name": "Caper Collection", "tmdb": "424242"})
    s, d = c.call("GET", "api/items/" + pa)
    assert d["coll"]["id"] == r["id"]


def test_group_writes_need_the_unlock(web):
    from test_web import Client
    c = Client(web)
    assert c.call("POST", "api/groups", {"kind": "book", "name": "Nope", "items": [web["item"]]})[0] == 401
    assert c.call("POST", "api/groups/leave", {"items": [web["item"]]})[0] == 401
