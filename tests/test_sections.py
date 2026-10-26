"""Sections, quick actions on many items, and the test data (0.8.0)."""
import uuid

import pytest

from conftest import add_entry, new_book, tx
from medialib import db, testdata


def uniq(t):
    return "{} {}".format(t, uuid.uuid4().hex[:6])


@pytest.fixture(scope="module")
def web(cfg, tmp_path_factory):
    import test_web
    gen = test_web.site.__wrapped__(cfg, tmp_path_factory)
    s = next(gen)
    yield s
    next(gen, None)


def books(conn, n, status="planned"):
    out = []
    with tx(conn, source="yamtrack"):
        for _ in range(n):
            item, _ = new_book(conn, uniq("Bulk book"))
            add_entry(conn, item, status)
            out.append(item)
    return [str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (i,)).fetchone()["public_id"]) for i in out]


def test_sections_and_moving(web, app):
    from test_web import Client
    c = Client(web).unlock()
    a, b = books(app, 2)
    s, r = c.call("POST", "api/sections", {"name": uniq("To sort")})
    assert s == 200
    sid = r["id"]
    s, r = c.call("POST", "api/bulk", {"items": [a, b], "action": "section", "section": sid})
    assert s == 200 and r["done"] == 2
    _, lib = c.call("GET", "api/library")
    assert {i["section"] for i in lib["items"] if i["id"] in (a, b)} == {sid}
    assert {x["kind"] for x in lib["sections"]} >= {"archive", "test", "manual"}
    s, r = c.call("POST", "api/bulk", {"items": [a], "action": "archive"})
    archive = next(x["id"] for x in lib["sections"] if x["kind"] == "archive")
    _, lib = c.call("GET", "api/library")
    assert next(i for i in lib["items"] if i["id"] == a)["section"] == archive
    assert c.call("PATCH", "api/sections/" + archive, {"name": "Old stuff"})[0] == 400       # built-ins keep their names
    assert c.call("DELETE", "api/sections/" + sid)[0] == 200                                # its items go back to the library
    _, lib = c.call("GET", "api/library")
    assert next(i for i in lib["items"] if i["id"] == b)["section"] is None
    assert sid not in [x["id"] for x in lib["sections"]]


def test_bulk_edit_changes_only_the_given_fields(web, app):
    from test_web import Client
    c = Client(web).unlock()
    a, b = books(app, 2)
    c.call("PATCH", "api/items/" + a, {"entry": {"rating": 7}, "tags": ["kept"]})
    s, r = c.call("POST", "api/bulk", {"items": [a, b], "action": "edit",
                                      "fields": {"status": "completed", "tags": ["bulk"], "format": "ebook"}})
    assert s == 200 and r == {"done": 2, "errors": []}
    _, da = c.call("GET", "api/items/" + a)
    assert da["status"] == "completed" and da["rating"] == 7.0 and da["tagList"] == ["kept", "bulk"]
    assert da["edition"]["format"] == "ebook"
    s, r = c.call("POST", "api/bulk", {"items": [a], "action": "edit", "fields": {"nonsense": 1}})
    assert s == 400


def test_bulk_trash_status_and_errors(web, app):
    from test_web import Client
    c = Client(web).unlock()
    a, b = books(app, 2)
    s, r = c.call("POST", "api/bulk", {"items": [a, b], "action": "status", "status": "completed"})
    assert r["done"] == 2
    s, r = c.call("POST", "api/bulk", {"items": [a, str(uuid.uuid4())], "action": "trash"})
    assert r["done"] == 1 and len(r["errors"]) == 1
    _, lib = c.call("GET", "api/library")
    assert a not in [i["id"] for i in lib["items"]]
    assert c.call("POST", "api/bulk", {"items": [b], "action": "explode"})[0] == 400


def test_archive_and_test_stay_out_of_the_statistics(app, ro):
    before = {r["status"]: r["items"] for r in ro.execute("SELECT status, items FROM stats.library_counts WHERE media_kind = 'book'")}
    a, = books(app, 1, "planned")
    after = {r["status"]: r["items"] for r in ro.execute("SELECT status, items FROM stats.library_counts WHERE media_kind = 'book'")}
    assert after.get("planned", 0) == before.get("planned", 0) + 1
    with tx(app):
        app.execute("UPDATE library.entry SET section_id = (SELECT id FROM library.section WHERE kind = 'archive')"
                    " WHERE item_id = (SELECT id FROM catalog.media_item WHERE public_id = %s)", (a,))
    again = {r["status"]: r["items"] for r in ro.execute("SELECT status, items FROM stats.library_counts WHERE media_kind = 'book'")}
    assert again.get("planned", 0) == before.get("planned", 0)


def test_test_data_comes_and_goes(cfg, app, tmp_path):
    testdata.remove(app, str(tmp_path), log=lambda *_: None)
    n = testdata.add(app, log=lambda *_: None)
    assert n == len(testdata.BOOKS) + len(testdata.MOVIES) + len(testdata.SERIES)
    assert testdata.add(app, log=lambda *_: None) == 0                  # a second run adds nothing
    rows = app.execute("""SELECT i.kind, e.status FROM library.entry e JOIN catalog.media_item i ON i.id = e.item_id
                           JOIN library.section s ON s.id = e.section_id WHERE s.kind = 'test'""").fetchall()
    assert len(rows) == n and {r["kind"] for r in rows} == {"book", "movie", "series"}
    fx = app.execute("SELECT e.status FROM library.entry e JOIN catalog.media_item i ON i.id = e.item_id"
                     " WHERE i.title = 'Test · Fixture Street'").fetchone()
    assert fx["status"] == "dropped"                                    # its last season watch was abandoned
    assert testdata.remove(app, str(tmp_path), log=lambda *_: None) == n   # series with seasons go too
    assert not app.execute("SELECT 1 AS x FROM catalog.media_item WHERE title LIKE 'Test · %'").fetchone()
    assert not app.execute("SELECT 1 AS x FROM catalog.media_item i WHERE i.kind = 'season'"
                           " AND NOT EXISTS (SELECT 1 FROM catalog.season s WHERE s.item_id = i.id)").fetchone()
