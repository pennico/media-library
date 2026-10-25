"""Reading apps: KOReader sync (as Readest speaks it), the OPDS catalog, and
what reading positions do to the owner's status. The server runs for real on
a free local port, like test_web.py."""
import base64
import dataclasses
import hashlib
import json
import socket
import threading
import urllib.error
import urllib.request
import uuid
import zipfile
from http.server import ThreadingHTTPServer

import pytest

from conftest import add_entry, new_book, tx
from medialib import db, files
from medialib.web import auth as auth_mod
from medialib.web import server, writes

OWNER = "owner@example.test"
USER = "reader"
READER_PW = "abcd-efgh-ijkl-mnop-qrst"
KEY = hashlib.md5(READER_PW.encode()).hexdigest()
CONSOLE_PW = "correct horse battery staple"


def make_epub(path, text):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("OEBPS/content.html", "<p>{}</p>".format(text) * 400)
    return path


@pytest.fixture(scope="module")
def site(cfg, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("reader")
    (tmp / "password").write_text(auth_mod.hash_password(CONSOLE_PW) + "\n")
    (tmp / "reader.pw").write_text(READER_PW + "\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    origin = "http://127.0.0.1:{}".format(port)
    cfg = dataclasses.replace(cfg, media_dir=str(tmp / "media"))
    app = server.App(cfg, owner_login=OWNER, origin=origin, password_file=str(tmp / "password"),
                     passkeys_file=str(tmp / "passkeys.json"), state_dir=str(tmp / "state"), cookie_secure=False,
                     reader_user=USER, reader_password_file=str(tmp / "reader.pw"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    books = {}
    with db.connect(cfg) as conn:
        for key in ("planned", "finished", "reread"):
            title = "Readest {} {}".format(key, uuid.uuid4().hex[:6])
            with tx(conn, source="yamtrack", client="test"):
                item, ed = new_book(conn, title)
                conn.execute("UPDATE catalog.edition SET pages = 200 WHERE id = %s", (ed,))
                add_entry(conn, item, "planned")
            pid = str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["public_id"])
            epub = make_epub(tmp / (key + ".epub"), title)
            writes.attach_file(conn, OWNER, pid, str(epub), files.sha256_file(epub), cfg.media_dir)
            doc = conn.execute("SELECT kosync_md5, public_id::text AS fid FROM catalog.edition_file WHERE edition_id = %s",
                               (ed,)).fetchone()
            books[key] = {"item": item, "pid": pid, "edition": ed, "doc": doc["kosync_md5"], "file": doc["fid"],
                          "title": title}
    yield {"origin": origin, "books": books, "cfg": cfg}
    httpd.shutdown()
    app.close()


def call(site, method, path, body=None, login=OWNER, auth="kosync", raw=False, **headers):
    h = {"Tailscale-User-Login": login, "Origin": "tauri://localhost",
         "Accept": "application/vnd.koreader.v1+json", "Content-Type": "application/json"}
    if auth == "kosync":
        h.update({"x-auth-user": USER, "x-auth-key": KEY})
    elif auth == "basic":
        h["Authorization"] = "Basic " + base64.b64encode("{}:{}".format(USER, READER_PW).encode()).decode()
    elif isinstance(auth, tuple):
        h["Authorization"] = "Basic " + base64.b64encode("{}:{}".format(*auth).encode()).decode()
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    h = {k: v for k, v in h.items() if v is not None}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(site["origin"] + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            payload = r.read()
            return r.status, (payload if raw else json.loads(payload or b"{}")), dict(r.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            payload = payload if raw else json.loads(payload or b"{}")
        except ValueError:
            pass
        return e.code, payload, dict(e.headers)


def state(site, key):
    with db.connect(site["cfg"]) as conn:
        b = site["books"][key]
        e = conn.execute("SELECT status FROM library.entry WHERE item_id = %s", (b["item"],)).fetchone()
        rs = conn.execute("SELECT state, progress_pct, progress_pages, ended_on, source FROM library.consumption"
                          " WHERE item_id = %s AND deleted_at IS NULL ORDER BY id", (b["item"],)).fetchall()
        return e["status"], rs


def put(site, key, pct, device="iPhone", doc=None):
    return call(site, "PUT", "/kosync/syncs/progress",
                {"document": doc or site["books"][key]["doc"], "progress": "/body/DocFragment[3]/body/p[2]/text().0",
                 "percentage": pct, "device": device, "device_id": "dev-1"})


# ------------------------------------------------------------------ login

def test_auth_with_md5_key_and_basic_fallback(site):
    assert call(site, "GET", "/kosync/users/auth")[:2] == (200, {"authorized": "OK"})
    assert call(site, "GET", "/kosync/users/auth", auth="basic")[0] == 200
    s, body, _ = call(site, "GET", "/kosync/users/auth", auth=None, x_auth_user=USER, x_auth_key="0" * 32)
    assert s == 401 and body["message"]
    assert call(site, "GET", "/kosync/healthcheck", auth=None)[:2] == (200, {"state": "OK"})


def test_only_the_owners_tailscale_login(site):
    assert call(site, "GET", "/kosync/users/auth", login="someone@else.test")[0] == 403
    assert call(site, "GET", "/kosync/users/auth", login=None)[0] == 403
    assert call(site, "GET", "/opds", login="someone@else.test", auth="basic")[0] == 403


def test_register_says_yes_only_to_the_configured_login(site):
    s, body, _ = call(site, "POST", "/kosync/users/create", {"username": USER, "password": KEY}, auth=None)
    assert s == 201 and body["username"] == USER
    s, _, _ = call(site, "POST", "/kosync/users/create", {"username": "new", "password": KEY}, auth=None)
    assert s == 402


# ---------------------------------------------------------------- positions

def test_progress_round_trip_and_unknown_document(site):
    doc = uuid.uuid4().hex
    assert call(site, "GET", "/kosync/syncs/progress/" + doc)[:2] == (200, {})
    s, body, _ = put(site, None, 0.42, doc=doc)
    assert s == 200 and body["document"] == doc and isinstance(body["timestamp"], int)
    s, got, _ = call(site, "GET", "/kosync/syncs/progress/" + doc)
    assert got["percentage"] == pytest.approx(0.42) and got["device"] == "iPhone" and got["progress"].startswith("/body")
    assert put(site, None, 1.5, doc=doc)[0] == 400
    assert call(site, "PUT", "/kosync/syncs/progress", {"document": "nope", "percentage": 0.1})[0] == 400


def test_reading_starts_moves_and_finishes(site):
    assert state(site, "planned")[0] == "planned"
    put(site, "planned", 0.004)                       # opening the book is not reading it
    assert state(site, "planned")[0] == "planned"
    put(site, "planned", 0.05)
    status, rs = state(site, "planned")
    assert status == "in_progress" and rs[-1]["source"] == "kosync" and float(rs[-1]["progress_pct"]) == 5.0
    put(site, "planned", 0.055)                       # under 1 point: no write
    assert float(state(site, "planned")[1][-1]["progress_pct"]) == 5.0
    put(site, "planned", 0.5)
    status, rs = state(site, "planned")
    assert float(rs[-1]["progress_pct"]) == 50.0 and rs[-1]["progress_pages"] == 100
    put(site, "planned", 1.0)
    status, rs = state(site, "planned")
    assert status == "completed" and rs[-1]["state"] == "completed" and rs[-1]["ended_on"] is not None


def test_audited_as_the_owner_from_kosync(site):
    with db.connect(site["cfg"]) as conn:
        row = conn.execute("SELECT source, client FROM audit.change WHERE table_name LIKE '%consumption'"
                           " AND client LIKE 'kosync:%' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["source"] == "owner" and row["client"] == "kosync:iPhone"


def test_reread_only_after_the_end_was_seen_here(site):
    b = site["books"]["reread"]
    with db.connect(site["cfg"]) as conn:
        with tx(conn, client="test"):
            conn.execute("INSERT INTO library.consumption (item_id, state, ended_on, ended_precision, source)"
                         " VALUES (%s, 'completed', '2020-01-01', 'year', 'owner')", (b["item"],))
            conn.execute("UPDATE library.entry SET status = 'completed' WHERE item_id = %s", (b["item"],))
    put(site, "reread", 0.40)                         # read before; a position mid-way is not a re-read
    assert state(site, "reread")[0] == "completed"
    put(site, "reread", 1.0)                          # reaches the end here
    put(site, "reread", 0.95)                         # paging back near the end: still read
    assert state(site, "reread")[0] == "completed"
    put(site, "reread", 0.02)                         # starting over
    status, rs = state(site, "reread")
    assert status == "in_progress" and len(rs) == 2


def test_cwa_checksum_alias_and_owner_link(site):
    b = site["books"]["finished"]
    alias = uuid.uuid4().hex
    with db.connect(site["cfg"]) as conn:
        with tx(conn, source="cwa", client="test"):
            conn.execute("INSERT INTO catalog.document_alias (document, edition_id, source) VALUES (%s, %s, 'cwa')",
                         (alias, b["edition"]))
    put(site, "finished", 0.3, doc=alias)
    assert state(site, "finished")[0] == "in_progress"
    other = uuid.uuid4().hex
    put(site, None, 0.3, doc=other, device="Mac")
    with db.connect(site["cfg"]) as conn:
        docs = {d["document"]: d for d in __import__("medialib.feeds.kosync", fromlist=["x"]).documents(conn)}
    assert docs[alias]["item"] == b["pid"] and docs[other]["item"] is None


# -------------------------------------------------------------------- OPDS

def test_opds_needs_basic_and_challenges(site):
    s, _, h = call(site, "GET", "/opds", auth=None, raw=True)
    assert s == 401 and h["WWW-Authenticate"].startswith("Basic")
    assert call(site, "GET", "/opds", auth=(USER, "wrong"), raw=True)[0] == 401


def test_opds_feeds_and_download(site):
    s, body, h = call(site, "GET", "/opds", auth="basic", raw=True)
    xml = body.decode()
    assert s == 200 and "kind=navigation" in h["Content-Type"]
    assert 'type="application/opensearchdescription+xml"' in xml and "/opds/books?shelf=reading" in xml
    s, body, _ = call(site, "GET", "/opds/books?shelf=all", auth="basic", raw=True)
    xml = body.decode()
    b = site["books"]["planned"]
    assert s == 200 and b["title"] in xml and "/opds/files/" + b["file"] in xml
    assert 'rel="http://opds-spec.org/acquisition"' in xml and 'type="application/epub+zip"' in xml
    s, body, _ = call(site, "GET", "/opds/search?q=" + b["title"].split()[-1], auth="basic", raw=True)
    assert b["title"] in body.decode() and site["books"]["reread"]["title"] not in body.decode()
    s, body, _ = call(site, "GET", "/opds/search.xml", auth="basic", raw=True)
    assert "{searchTerms}" in body.decode()
    s, _, h = call(site, "HEAD", "/opds/files/" + b["file"], auth="basic", raw=True)
    assert s == 200 and h["Content-Type"] == "application/epub+zip"
    s, data, h = call(site, "GET", "/opds/files/" + b["file"], auth="basic", raw=True)
    assert data[:2] == b"PK" and "filename" in h["Content-Disposition"]
    assert call(site, "GET", "/opds/books?shelf=nope", auth="basic", raw=True)[0] == 404


def test_console_panel_and_password_needs_unlock(site):
    import http.cookiejar
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def console(method, path, body=None):
        req = urllib.request.Request(site["origin"] + path, data=json.dumps(body or {}).encode() if method != "GET"
                                     else None, method=method,
                                     headers={"Tailscale-User-Login": OWNER, "X-Console-Action": "1",
                                              "Origin": site["origin"], "Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    s, panel = console("GET", "/api/reading-apps")
    assert s == 200 and panel["user"] == USER and panel["ready"] and panel["opds"].endswith("/opds")
    assert panel["opdsBooks"] >= 3 and any(d["item"] is None for d in panel["documents"])
    assert console("POST", "/api/reading-apps/password")[0] == 401
    assert console("POST", "/api/unlock", {"password": CONSOLE_PW})[0] == 200
    s, pw = console("POST", "/api/reading-apps/password")
    assert s == 200 and pw["password"] == READER_PW
    unknown = next(d["document"] for d in panel["documents"] if d["item"] is None)
    target = site["books"]["planned"]["pid"]
    assert console("POST", "/api/kosync/{}/link".format(unknown), {"item": target})[0] == 200
    assert console("POST", "/api/kosync/{}/link".format(unknown), {"item": target})[0] == 400
