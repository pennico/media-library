"""A book's version (the edition's format: paper, ebook, audiobook) and the
medium of each reading: set from the console, when adding a book, and what
the library list gives the shelf, the filters and the charts."""
import uuid

from medialib import db
from test_web import Client, site  # noqa: F401  (the running console)


def library_item(c, pid):
    s, lib = c.call("GET", "api/library")
    assert s == 200
    return next(i for i in lib["items"] if i["id"] == pid)


def test_add_with_a_version(site):  # noqa: F811
    c = Client(site).unlock()
    s, r = c.call("POST", "api/add-manual", {"title": "Heard " + uuid.uuid4().hex[:6], "status": "completed",
                                             "format": "audiobook"})
    assert s == 200
    s, d = c.call("GET", "api/items/" + r["id"])
    assert d["edition"]["format"] == "audiobook" and d["prov"]["edition"]["format"] == "owner"
    assert d["readings"][0]["medium"] == "audiobook"
    i = library_item(c, r["id"])
    assert (i["format"], i["medium"]) == ("audiobook", "audiobook")
    assert len(i["reads"]) == 1 and i["reads"][0][1] == "day" and i["reads"][0][4] == "audiobook"


def test_a_bad_version_adds_nothing(site, cfg):  # noqa: F811
    c = Client(site).unlock()
    title = "Never added " + uuid.uuid4().hex[:6]
    s, e = c.call("POST", "api/add-manual", {"title": title, "format": "scroll"})
    assert s == 400 and "version" in e["error"]
    with db.connect(cfg) as conn:
        assert not conn.execute("SELECT 1 AS x FROM catalog.media_item WHERE title = %s", (title,)).fetchone()


def test_readings_have_a_medium(site):  # noqa: F811
    c = Client(site).unlock()
    s, r = c.call("POST", "api/add-manual", {"title": "Read twice " + uuid.uuid4().hex[:6]})
    pid = r["id"]
    assert c.call("PATCH", "api/items/" + pid, {"edition": {"format": "ebook"}})[0] == 200
    s, d = c.call("POST", "api/items/" + pid + "/status", {"status": "in_progress", "date": "2025-03-02"})
    assert s == 200 and d["readings"][0]["medium"] == "ebook"           # read in the book's version
    s, d = c.call("POST", "api/items/" + pid + "/readings", {"state": "completed", "ended": "2019", "medium": "paperback"})
    assert s == 200 and {x["medium"] for x in d["readings"]} == {"ebook", "paperback"}
    s, e = c.call("POST", "api/items/" + pid + "/readings", {"state": "completed", "medium": "vinyl"})
    assert s == 400 and "read as" in e["error"]
    rid = next(x["id"] for x in d["readings"] if x["medium"] == "paperback")
    assert c.call("PATCH", "api/readings/" + rid, {"medium": ""})[0] == 200
    i = library_item(c, pid)
    assert i["medium"] == "ebook" and i["reads"] == [["2019-01-01", "year", None, None, None]]
    assert i["days"] == ["2025-03-02"]                                # the start, for the calendar


def test_added_date_is_editable(site):  # noqa: F811
    c = Client(site).unlock()
    s, r = c.call("POST", "api/add-manual", {"title": "Added long ago " + uuid.uuid4().hex[:6]})
    s, d = c.call("PATCH", "api/items/" + r["id"], {"entry": {"added": "2019-06-30"}})
    assert s == 200 and d["added"] == "2019-06-30" and d["prov"]["entry"]["added_at"] == "owner"
    for bad in ("2019-06", "2999-01-01", "yesterday"):
        assert c.call("PATCH", "api/items/" + r["id"], {"entry": {"added": bad}})[0] == 400
