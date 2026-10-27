"""Console web side: the security checks of every write (owner login, unlock,
X-Console-Action, same Origin), the write paths, and that statistics come
from the read-only role. The server runs for real on a free local port."""
import http.cookiejar
import json
import socket
import threading
import urllib.request
import uuid

import pytest

from conftest import add_entry, new_book, tx
from medialib import db
from medialib.web import auth as auth_mod
from medialib.web import server

OWNER = "owner@example.test"
PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def site(cfg, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("web")
    (tmp / "password").write_text(auth_mod.hash_password(PASSWORD) + "\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    origin = "http://127.0.0.1:{}".format(port)
    import dataclasses
    cfg = dataclasses.replace(cfg, media_dir=str(tmp / "media"))
    app = server.App(cfg, owner_login=OWNER, origin=origin, password_file=str(tmp / "password"),
                     passkeys_file=str(tmp / "passkeys.json"), state_dir=str(tmp / "state"), cookie_secure=False)
    httpd = server.Server(("127.0.0.1", port), server.make_handler(app))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    with db.connect(cfg) as conn:
        with tx(conn, source="yamtrack", client="test"):
            item, _ = new_book(conn, "Web test book " + uuid.uuid4().hex[:6])
            add_entry(conn, item, "planned")
        pid = str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["public_id"])
    yield {"base": origin + "/", "origin": origin, "item": pid, "item_id": item, "state": str(tmp / "state")}
    httpd.shutdown()
    app.close()


class Client:
    def __init__(self, site, login=OWNER):
        self.site, self.login = site, login
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, **headers):
        h = {"Tailscale-User-Login": self.login, "X-Console-Action": "1", "Content-Type": "application/json",
             "Origin": self.site["origin"]}
        h.update({k.replace("_", "-"): v for k, v in headers.items()})
        h = {k: v for k, v in h.items() if v is not None}
        data = json.dumps(body or {}).encode() if method != "GET" else None
        req = urllib.request.Request(self.site["base"] + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def unlock(self):
        assert self.call("POST", "api/unlock", {"password": PASSWORD})[0] == 200
        return self


def test_reads_are_open(site):
    c = Client(site, login=None)
    s, lib = c.call("GET", "api/library")
    assert s == 200 and any(i["id"] == site["item"] for i in lib["items"])
    s, d = c.call("GET", "api/items/" + site["item"])
    assert s == 200 and d["status"] == "planned"
    assert c.call("GET", "api/items/" + str(uuid.uuid4()))[0] == 404


def test_stats_use_the_readonly_role(site):
    s, st = Client(site).call("GET", "api/stats")
    assert s == 200 and "tiles" in st and "byYear" in st


def test_health(site):
    s, h = Client(site).call("GET", "healthz")
    assert s == 200 and h["ok"] is True
    s, h = Client(site).call("GET", "api/health")
    names = {c["name"]: c["ok"] for c in h["checks"]}
    assert names["page checksums on"] and names["audit chain intact"] and names["schema version"]


@pytest.mark.parametrize("login,headers,code", [
    ("someone@else.test", {}, 403),                       # not the owner
    (None, {}, 403),                                      # no Tailscale login (not through Serve)
    (OWNER, {"X_Console_Action": None}, 403),             # a plain cross-site form could not add the header
    (OWNER, {"Origin": "https://evil.example"}, 403),     # wrong origin
])
def test_writes_need_owner_header_origin(site, login, headers, code):
    c = Client(site, login=login)
    assert c.call("POST", "api/unlock", {"password": PASSWORD}, **headers)[0] == code


def test_writes_need_a_session(site):
    c = Client(site)
    assert c.call("PATCH", "api/items/" + site["item"], {"entry": {"rating": 5}})[0] == 401
    assert c.call("POST", "api/unlock", {"password": "wrong"})[0] == 401
    c.unlock()
    assert c.call("PATCH", "api/items/" + site["item"], {"entry": {"rating": 5}})[0] == 200
    assert c.call("POST", "api/lock")[0] == 200
    assert c.call("PATCH", "api/items/" + site["item"], {"entry": {"rating": 6}})[0] == 401


def test_edits_are_owner_labelled_and_audited(site, cfg):
    c = Client(site).unlock()
    s, d = c.call("PATCH", "api/items/" + site["item"], {"entry": {"rating": 8.5, "review": "Good."},
                                                         "edition": {"pages": "321", "published": "2019-05"},
                                                         "authors": ["Ada Example", "Bo Example"], "genres": ["Fiction"]})
    assert s == 200, d
    assert d["rating"] == 8.5 and d["review"] == "Good." and d["edition"]["pages"] == 321
    assert d["edition"]["publishedPrecision"] == "month"
    assert d["prov"]["entry"]["rating"] == "owner" and d["prov"]["edition"]["pages"] == "owner"
    assert [a["name"] for a in d["authorsList"]] == ["Ada Example", "Bo Example"]
    with db.connect(cfg) as conn:
        row = conn.execute("SELECT client, source FROM audit.change WHERE table_name = 'library.entry'"
                           " AND row_key = jsonb_build_object('item_id', %s::bigint) ORDER BY id DESC LIMIT 1",
                           (site["item_id"],)).fetchone()
    assert row["client"] == "console:" + OWNER and row["source"] == "owner"


@pytest.mark.parametrize("body,fragment", [
    ({"entry": {"rating": 11}}, "0 to 10"),
    ({"entry": {"score": 3}}, "unknown entry field"),
    ({"edition": {"published": "2019-13"}}, "not a real date"),
    ({"edition": {"pages": 0}}, "between 1 and 20000"),
    ({"item": {"title": "  "}}, "cannot be empty"),
    ({"item": {"originalLang": "xxx"}}, "unknown language"),
    ({"surprise": 1}, "unknown fields"),
])
def test_bad_input_is_refused(site, body, fragment):
    c = Client(site).unlock()
    s, r = c.call("PATCH", "api/items/" + site["item"], body)
    assert s == 400 and fragment in r["error"]


def test_status_and_readings(site):
    c = Client(site).unlock()
    s, d = c.call("POST", "api/items/" + site["item"] + "/status", {"status": "in_progress", "date": "2026-10-03"})
    assert s == 200 and d["status"] == "in_progress" and d["readings"][0]["state"] == "in_progress"
    s, d = c.call("POST", "api/items/" + site["item"] + "/status", {"status": "completed", "date": "2026-10"})
    assert s == 200 and d["status"] == "completed"
    r = d["readings"][0]
    assert (r["started"], r["ended"], r["endedPrecision"]) == ("2026-10-03", "2026-10-01", "month")
    s, e = c.call("PATCH", "api/readings/" + r["id"], {"state": "completed", "started": "2026-10-20", "ended": "2026-10-12"})
    assert s == 409 and "finish before it started" in e["error"]
    assert c.call("DELETE", "api/readings/" + r["id"])[0] == 200
    s, d = c.call("GET", "api/items/" + site["item"])
    assert d["status"] == "planned" and not d["readings"]          # status follows the readings


def test_collections_and_drag_move(site, cfg):
    with db.connect(cfg) as conn:
        with tx(conn, source="yamtrack", client="test"):
            for n in range(4):
                item, _ = new_book(conn, "Shelf test {} {}".format(n, uuid.uuid4().hex[:6]))
                add_entry(conn, item)
    c = Client(site).unlock()
    s, lib = c.call("GET", "api/library")
    books = [i["id"] for i in lib["items"] if i["kind"] == "book"][:4]
    s, r = c.call("POST", "api/shelves", {"name": "Test " + uuid.uuid4().hex[:6], "mediaKind": "book"})
    sid = r["id"]
    assert c.call("POST", "api/shelves/" + sid + "/items", {"add": books})[0] == 200
    assert c.call("POST", "api/shelves/" + sid + "/move", {"item": books[3], "to": 1})[0] == 200
    shelf = next(x for x in c.call("GET", "api/library")[1]["shelves"] if x["id"] == sid)
    assert shelf["items"] == [books[3]] + books[:3]
    assert c.call("PATCH", "api/shelves/" + sid, {"layout": "list", "sortKey": "pages"})[0] == 200
    assert c.call("PATCH", "api/shelves/" + sid, {"sortKey": "colour"})[0] == 400
    all_books = next(x for x in lib["shelves"] if x["kind"] == "all" and x["media_kind"] == "book")
    assert c.call("DELETE", "api/shelves/" + all_books["id"])[0] == 400
    assert c.call("DELETE", "api/shelves/" + sid)[0] == 200


def test_lockout_after_five_failures(site, cfg, tmp_path):
    (tmp_path / "pw").write_text(auth_mod.hash_password("x" * 12))
    a = auth_mod.Auth(OWNER, site["origin"], str(tmp_path / "pw"), None, None)
    for _ in range(5):
        assert not a.check_password("wrong")
        a.fail()
    assert a.locked_out()


def test_page_and_security_headers(site):
    req = urllib.request.Request(site["base"])
    with urllib.request.urlopen(req) as r:
        assert r.status == 200
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Frame-Options"] == "DENY"
        assert b"Media library" in r.read()


def test_find_link_address_is_the_owners_to_change(site):
    """The "find this book" address: none from the environment here; the owner sets one, it survives a restart
    (state folder), bad addresses are refused, and reset goes back to the environment's."""
    assert Client(site).call("POST", "api/settings/find", {"url": "https://a.example/s?q={q}"})[0] == 401
    assert Client(site, login="someone@example.test").call("POST", "api/settings/find", {"reset": True})[0] == 403
    c = Client(site).unlock()
    for bad in ["http://a.example/?q={q}", "https://a.example/search", "https://a.example/?q={q}&x={q}",
                "https://{q}.example/", "https://a.example/?q={q} x", "javascript:alert(1)//{q}", "https://localhost/{q}"]:
        s, r = c.call("POST", "api/settings/find", {"url": bad, "label": "X"})
        assert s == 400, bad
    s, r = c.call("POST", "api/settings/find", {"url": " https://books.example/search?q={q}&check=1 ", "label": "Books"})
    assert s == 200 and r["find"] == {"url": "https://books.example/search?q={q}&check=1", "label": "Books"} and r["findCustom"]
    lib = c.call("GET", "api/library")[1]
    assert lib["find"]["url"].startswith("https://books.example/") and lib["findCustom"] and lib["findDefault"] is None
    saved = json.loads(open(site["state"] + "/find-link.json").read())
    assert saved["by"] == OWNER and saved["url"] == "https://books.example/search?q={q}&check=1"
    s, r = c.call("POST", "api/settings/find", {"reset": True})
    assert s == 200 and r["find"] is None and not r["findCustom"]


def test_find_link_survives_a_restart(cfg, tmp_path):
    state = tmp_path / "state"
    kw = dict(owner_login=OWNER, origin="http://127.0.0.1", password_file=str(tmp_path / "pw"),
              passkeys_file=str(tmp_path / "pk.json"), state_dir=str(state), cookie_secure=False,
              find_url="https://default.example/?q={q}", find_label="Default")
    app = server.App(cfg, **kw)
    try:
        assert app.find == {"url": "https://default.example/?q={q}", "label": "Default"}
        app.set_find({"url": "https://moved.example/search?q={q}", "label": ""}, OWNER)
    finally:
        app.close()
    app = server.App(cfg, **kw)
    try:
        assert app.find == {"url": "https://moved.example/search?q={q}", "label": "Search"}
        assert app.find_payload()["findCustom"] and app.find_payload()["findDefault"]["label"] == "Default"
        app.set_find({"reset": True}, OWNER)
        assert app.find["url"] == "https://default.example/?q={q}" and not (state / "find-link.json").exists()
    finally:
        app.close()


def raw_post(site, client, path, data, ctype):
    req = urllib.request.Request(site["base"] + path, data=data, method="POST", headers={
        "Tailscale-User-Login": OWNER, "X-Console-Action": "1", "Content-Type": ctype, "Origin": site["origin"]})
    try:
        with client.opener.open(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def make_epub(title):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf", "<package><metadata><title>{}</title></metadata></package>".format(title) + "x" * 5000)
    return buf.getvalue()


def test_add_by_hand(site):
    c = Client(site).unlock()
    import random
    core = "979" + "".join(random.choice("0123456789") for _ in range(9))
    isbn = core + str((10 - sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(core)) % 10) % 10)
    s, r = c.call("POST", "api/add-manual", {"title": "Hand made " + uuid.uuid4().hex[:6], "authors": "Ada Hand; Bo Hand",
                                             "language": "ita", "published": "2001-04", "pages": "120", "isbn": isbn,
                                             "status": "completed"})
    assert s == 200 and r["existing"] is False
    s, d = c.call("GET", "api/items/" + r["id"])
    assert [a["name"] for a in d["authorsList"]] == ["Ada Hand", "Bo Hand"]
    assert d["status"] == "completed" and d["readings"][0]["state"] == "completed"
    assert d["edition"]["publishedPrecision"] == "month" and d["prov"]["item"]["title"] == "owner"
    s, again = c.call("POST", "api/add-manual", {"title": "Other title", "isbn": "-".join([isbn[:3], isbn[3:]])})
    assert again == {"id": r["id"], "existing": True}
    assert c.call("POST", "api/add-manual", {"title": " "})[0] == 400
    assert c.call("POST", "api/add-manual", {"title": "x", "isbn": "123"})[0] == 400


def test_cover_upload(site, cfg):
    import io
    from PIL import Image
    c = Client(site).unlock()
    buf = io.BytesIO()
    Image.new("RGB", (300, 450), (200, 30, 30)).save(buf, "PNG")
    s, d = raw_post(site, c, "api/items/" + site["item"] + "/cover-upload", buf.getvalue(), "image/png")
    assert s == 200, d
    chosen = [x for x in d["covers"] if x["chosen"]]
    assert len(chosen) == 1 and chosen[0]["source"] == "owner" and chosen[0]["thumb"]
    s, e = raw_post(site, c, "api/items/" + site["item"] + "/cover-upload", b"<svg>not an image</svg>", "image/png")
    assert s == 400 and "not a usable image" in e["error"]
    assert raw_post(site, Client(site), "api/items/" + site["item"] + "/cover-upload", buf.getvalue(), "image/png")[0] == 401


def test_epub_upload_and_download(site):
    c = Client(site).unlock()
    data = make_epub("Upload test " + uuid.uuid4().hex)
    s, d = raw_post(site, c, "api/items/" + site["item"] + "/file", data, "application/epub+zip")
    assert s == 200, d
    f = [x for x in d["files"] if x["storage"] == "store"][0]
    assert f["kosync"] and f["download"]
    with c.opener.open(site["base"] + f["download"]) as r:
        assert r.read() == data and "attachment" in r.headers["Content-Disposition"]
    s, e = raw_post(site, c, "api/items/" + site["item"] + "/file", data, "application/epub+zip")
    assert s == 400 and "already in the library" in e["error"]
    s, e = raw_post(site, c, "api/items/" + site["item"] + "/file", b"PK\x03\x04 not really a zip", "application/epub+zip")
    assert s == 400 and "not an EPUB" in e["error"]


# ---------------------------------------------------------------- transfer: gzip, ETag, keep-alive

def _conn(site):
    import http.client
    return http.client.HTTPConnection("127.0.0.1", int(site["origin"].rsplit(":", 1)[1]), timeout=10)


def test_json_and_page_are_gzipped_and_revalidated(site):
    import gzip
    h = {"Tailscale-User-Login": OWNER, "Accept-Encoding": "gzip"}
    c = _conn(site)
    c.request("GET", "/api/library", headers=h)
    r = c.getresponse()
    raw, etag = r.read(), r.getheader("ETag")
    assert r.getheader("Content-Encoding") == "gzip" and etag and r.getheader("Cache-Control") == "no-cache"
    assert any(i["id"] == site["item"] for i in json.loads(gzip.decompress(raw))["items"])
    c.request("GET", "/api/library", headers=dict(h, **{"If-None-Match": etag}))   # same connection
    r = c.getresponse()
    assert r.status == 304 and r.read() == b""
    c.request("GET", "/api/library", headers={"Tailscale-User-Login": OWNER})       # no gzip asked
    r = c.getresponse()
    assert r.getheader("Content-Encoding") is None and json.loads(r.read())["items"]
    c.request("GET", "/", headers=h)
    r = c.getresponse()
    page_etag = r.getheader("ETag")
    assert r.getheader("Content-Encoding") == "gzip" and b"<html" in gzip.decompress(r.read())[:500].lower()
    c.request("GET", "/", headers=dict(h, **{"If-None-Match": page_etag}))
    assert c.getresponse().status == 304


def test_library_etag_changes_with_the_data(site):
    c = _conn(site)
    c.request("GET", "/api/library", headers={"Tailscale-User-Login": OWNER})
    r = c.getresponse(); r.read()
    first = r.getheader("ETag")
    client = Client(site).unlock()
    s, _ = client.call("PATCH", "api/items/" + site["item"], {"entry": {"rating": 7}})
    assert s == 200
    c.request("GET", "/api/library", headers={"Tailscale-User-Login": OWNER, "If-None-Match": first})
    r = c.getresponse(); r.read()
    assert r.status == 200 and r.getheader("ETag") != first


def test_keep_alive_survives_a_refused_write(site):
    """A refused POST leaves its body unread; the server must close rather than misread it."""
    c = _conn(site)
    body = json.dumps({"x": "y" * 5000}).encode()
    c.request("POST", "/api/items/" + site["item"], body=body, headers={
        "Tailscale-User-Login": "someone@else.test", "X-Console-Action": "1", "Content-Type": "application/json"})
    r = c.getresponse(); r.read()
    assert r.status == 403
    c2 = _conn(site)
    for _ in range(3):    # an ordinary connection is reused
        c2.request("GET", "/healthz")
        r = c2.getresponse(); r.read()
        assert r.status == 200 and r.version == 11
    c.close()
    c.request("GET", "/healthz")     # http.client reconnects if the server closed
    assert c.getresponse().status == 200


# ---------------------------------------------------------------- batch save (the list's Edit mode)

def _books(cfg, n):
    with db.connect(cfg) as conn:
        with tx(conn, source="yamtrack", client="test"):
            for k in range(n):
                item, _ = new_book(conn, "Batch test {} {}".format(k, uuid.uuid4().hex[:6]))
                add_entry(conn, item)
    return None


def test_batch_edit_saves_rows_in_one_request(site, cfg):
    _books(cfg, 4)
    c = Client(site).unlock()
    ids = [i["id"] for i in c.call("GET", "api/library")[1]["items"] if i["title"].startswith("Batch test")][:4]
    rows = [{"id": ids[0], "patch": {"entry": {"rating": 8}, "edition": {"pages": "321"}}},
            {"id": ids[1], "status": "completed", "ended": "2026-05-04"},
            {"id": ids[2], "patch": {"item": {"title": "Batch renamed"}, "genres": ["Fiction"]}},
            {"id": ids[3], "patch": {"entry": {"rating": 99}}, "status": "completed"}]      # the bad one
    s, r = c.call("POST", "api/items/batch", {"rows": rows})
    assert s == 200 and r["done"] == 3 and len(r["errors"]) == 1 and r["errors"][0]["id"] == ids[3]
    assert "rating" in r["errors"][0]["error"]
    by = {i["id"]: i for i in r["items"]}
    assert by[ids[0]]["rating"] == 8 and by[ids[0]]["pages"] == 321
    assert by[ids[1]]["status"] == "completed" and by[ids[1]]["reads"][0][0] == "2026-05-04"
    assert by[ids[2]]["title"] == "Batch renamed" and by[ids[2]]["genres"] == ["Fiction"]
    assert by[ids[3]]["status"] == "planned"                       # a failed row leaves nothing half done
    assert any(cat["name"] == "Fiction" for cat in r["categories"])
    # the library read agrees with what the batch answered
    lib = {i["id"]: i for i in c.call("GET", "api/library")[1]["items"]}
    assert all(lib[k] == by[k] for k in by)


def test_batch_edit_is_guarded_and_validated(site):
    anon = Client(site)                                            # locked
    assert anon.call("POST", "api/items/batch", {"rows": [{"id": site["item"], "status": "planned"}]})[0] == 401
    c = Client(site).unlock()
    for rows in (None, [], [{"id": site["item"]}], [{"id": site["item"], "status": "x"}, {"id": site["item"], "status": "planned"}],
                 "text", [{"id": 5, "status": "planned"}], [{"id": site["item"], "patch": "no"}]):
        assert c.call("POST", "api/items/batch", {"rows": rows})[0] == 400, rows
    s, r = c.call("POST", "api/items/batch", {"rows": [{"id": str(uuid.uuid4()), "status": "planned"}]})
    assert s == 200 and r["done"] == 0 and r["errors"][0]["error"]


def test_rows_endpoint_returns_just_those_items(site):
    c = Client(site, login=None)
    s, r = c.call("GET", "api/rows?ids=" + site["item"])
    assert s == 200 and [i["id"] for i in r["items"]] == [site["item"]] and "categories" in r
    assert c.call("GET", "api/rows?ids=nope")[0] == 400
    assert c.call("GET", "api/rows")[0] == 400


def test_batch_row_is_all_or_nothing(site, cfg):
    """A row is one transaction: if its status is refused, its rating is not saved either."""
    _books(cfg, 1)
    c = Client(site).unlock()
    pid = next(i["id"] for i in c.call("GET", "api/library")[1]["items"] if i["title"].startswith("Batch test"))
    s, r = c.call("POST", "api/items/batch", {"rows": [
        {"id": pid, "patch": {"entry": {"rating": 4}}, "ended": "2026-01-02", "status": "nonsense"}]})
    assert s == 200 and r["done"] == 0 and len(r["errors"]) == 1
    item = r["items"][0]
    assert item["rating"] != 4 and not item["reads"] and item["status"] == "planned"


# ---------------------------------------------------------------- 1.0.0: history by index, bulk restore, cached library, thumbs

def test_history_finds_edition_and_reading_changes(site, cfg):
    """The audit history looks up editions and readings by their whole row key (the index), and still finds them."""
    _books(cfg, 1)
    c = Client(site).unlock()
    pid = next(i["id"] for i in c.call("GET", "api/library")[1]["items"] if i["title"].startswith("Batch test"))
    assert c.call("PATCH", "api/items/" + pid, {"edition": {"pages": "404"}})[0] == 200
    assert c.call("POST", "api/items/" + pid + "/readings", {"state": "completed", "ended": "2025-02"})[0] == 200
    s, d = c.call("GET", "api/items/" + pid)
    whats = {h["what"] for h in d["history"]}
    assert s == 200 and {"edition", "reading"} <= whats


def test_bulk_restore_undoes_a_bulk_trash(site, cfg):
    _books(cfg, 2)
    c = Client(site).unlock()
    ids = [i["id"] for i in c.call("GET", "api/library")[1]["items"] if i["title"].startswith("Batch test")][-2:]
    assert c.call("POST", "api/bulk", {"items": ids, "action": "trash"})[1]["done"] == 2
    assert not {i["id"] for i in c.call("GET", "api/library")[1]["items"]} & set(ids)
    s, r = c.call("POST", "api/bulk", {"items": ids, "action": "restore"})
    assert s == 200 and r["done"] == 2 and set(ids) <= {i["id"] for i in c.call("GET", "api/library")[1]["items"]}


def test_library_answer_is_rebuilt_after_a_write_from_elsewhere(site, cfg):
    """The built /api/library is reused while nothing changed, and rebuilt when another process (the importer,
    a feed) writes: every table it reads is audited, and the newest audit id is its token."""
    c = Client(site)
    first = c.call("GET", "api/library")[1]
    assert c.call("GET", "api/library")[1] == first                     # reused
    title = "Written elsewhere " + uuid.uuid4().hex[:6]
    with db.connect(cfg) as conn:
        with tx(conn, source="yamtrack", client="test"):
            item, _ = new_book(conn, title)
            add_entry(conn, item)
    assert any(i["title"] == title for i in c.call("GET", "api/library")[1]["items"])


def test_thumbnails_never_enlarge_and_480_is_served(tmp_path):
    from PIL import Image
    from medialib.covers import make_thumb
    src = tmp_path / "small.jpg"
    Image.new("RGB", (300, 450), (120, 80, 40)).save(src, "JPEG")
    make_thumb(str(src), str(tmp_path / "t-640.jpg"), 640)
    make_thumb(str(src), str(tmp_path / "t-160.jpg"), 160)
    assert Image.open(tmp_path / "t-640.jpg").size == (300, 450)        # not enlarged
    assert Image.open(tmp_path / "t-160.jpg").size == (160, 240)
    assert not list(tmp_path.glob("*.tmp"))
    assert 480 in server.THUMB_WIDTHS
