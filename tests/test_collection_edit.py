"""Editing what a collection holds (D70): add, remove, and its order in one go, with what an Undo needs."""
import uuid

from conftest import add_entry, new_book, tx
from medialib import db
from test_web import Client, site  # noqa: F401 - the running console


def _books(cfg, n, prefix="Coll edit"):
    with db.connect(cfg) as conn:
        with tx(conn, source="yamtrack", client="test"):
            ids = []
            for k in range(n):
                item, _ = new_book(conn, "{} {} {}".format(prefix, k, uuid.uuid4().hex[:6]))
                add_entry(conn, item)
                ids.append(item)
        return [str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (i,)).fetchone()["public_id"])
                for i in ids]


def _movie(cfg):
    with db.connect(cfg) as conn:
        with tx(conn, source="owner", client="test"):
            it = conn.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('movie', %s) RETURNING id",
                              ("Coll edit film " + uuid.uuid4().hex[:6],)).fetchone()["id"]
            conn.execute("INSERT INTO catalog.movie (item_id) VALUES (%s)", (it,))
            add_entry(conn, it)
        return str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (it,)).fetchone()["public_id"])


def _shelf(c, kind="book"):
    s, r = c.call("POST", "api/shelves", {"name": "Edit " + uuid.uuid4().hex[:6], "mediaKind": kind})
    assert s == 200
    return r["id"]


def test_add_remove_and_order_answer_the_new_content(site, cfg):  # noqa: F811
    b = _books(cfg, 4)
    c = Client(site).unlock()
    sid = _shelf(c)
    s, r = c.call("POST", "api/shelves/" + sid + "/items", {"add": b[:3]})
    assert s == 200 and r["items"] == b[:3]
    s, r = c.call("POST", "api/shelves/" + sid + "/items", {"add": [b[1]]})       # already in it: nothing moves
    assert r["items"] == b[:3]
    s, r = c.call("POST", "api/shelves/" + sid + "/items", {"remove": [b[1]]})
    assert s == 200 and r["items"] == [b[0], b[2]]
    # the Undo of that remove: the order from before puts it back where it was
    s, r = c.call("POST", "api/shelves/" + sid + "/order", {"items": b[:3]})
    assert s == 200 and r["items"] == b[:3]
    # a reorder, and one that also adds (the item joins there)
    s, r = c.call("POST", "api/shelves/" + sid + "/order", {"items": [b[2], b[0], b[1]]})
    assert r["items"] == [b[2], b[0], b[1]]
    s, r = c.call("POST", "api/shelves/" + sid + "/order", {"items": [b[3], b[2]]})
    assert r["items"] == [b[3], b[2], b[0], b[1]], "named first, the others keep their order after them"
    shelf = next(x for x in c.call("GET", "api/library")[1]["shelves"] if x["id"] == sid)
    assert shelf["items"] == r["items"]
    with db.connect(cfg) as conn:
        pos = [x["position"] for x in conn.execute(
            "SELECT si.position FROM library.shelf_item si JOIN library.shelf s ON s.id = si.shelf_id"
            " WHERE s.public_id = %s ORDER BY si.position", (sid,)).fetchall()]
        assert pos == [1, 2, 3, 4]
        who = {x["client"] for x in conn.execute(
            "SELECT DISTINCT client FROM audit.change WHERE table_name = 'library.shelf_item' AND id > "
            "(SELECT max(id) - 30 FROM audit.change)").fetchall()}
        assert any(w.startswith("console:") for w in who), "audited as the owner's console"


def test_order_keeps_members_in_the_trash(site, cfg):  # noqa: F811
    b = _books(cfg, 3, "Coll trash")
    c = Client(site).unlock()
    sid = _shelf(c)
    c.call("POST", "api/shelves/" + sid + "/items", {"add": b})
    assert c.call("POST", "api/items/" + b[1] + "/trash")[0] == 200
    s, r = c.call("POST", "api/shelves/" + sid + "/order", {"items": [b[2], b[0]]})
    assert s == 200 and r["items"] == [b[2], b[0]], "the console never sees the trashed one"
    assert c.call("POST", "api/items/" + b[1] + "/restore")[0] == 200
    shelf = next(x for x in c.call("GET", "api/library")[1]["shelves"] if x["id"] == sid)
    assert shelf["items"] == [b[2], b[0], b[1]], "restored: still in the collection, after the ones ordered"


def test_refusals(site, cfg):  # noqa: F811
    b = _books(cfg, 2, "Coll refuse")
    film = _movie(cfg)
    c = Client(site).unlock()
    sid = _shelf(c)
    s, e = c.call("POST", "api/shelves/" + sid + "/items", {"add": [film]})
    assert s == 400 and "can't go in this collection" in e["error"]
    s, e = c.call("POST", "api/shelves/" + sid + "/order", {"items": [b[0], film]})
    assert s == 400 and "can't go in this collection" in e["error"]
    assert c.call("POST", "api/shelves/" + sid + "/order", {"items": [b[0], b[0]]})[0] == 400
    assert c.call("POST", "api/shelves/" + sid + "/order", {"items": []})[0] == 400
    assert c.call("POST", "api/shelves/" + sid + "/order", {"items": "x"})[0] == 400
    assert c.call("POST", "api/shelves/" + sid + "/items", {"add": "x"})[0] == 400
    assert c.call("POST", "api/shelves/" + sid + "/order", {"items": [str(uuid.uuid4())]})[0] == 404
    shelf = next(x for x in c.call("GET", "api/library")[1]["shelves"] if x["id"] == sid)
    assert shelf["items"] == [], "a refused order changes nothing"
    locked = Client(site)
    assert locked.call("POST", "api/shelves/" + sid + "/order", {"items": b})[0] == 401
    assert Client(site, login="someone@else").call("POST", "api/shelves/" + sid + "/order", {"items": b})[0] == 403
