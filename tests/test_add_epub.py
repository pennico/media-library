"""Add a book from an EPUB (read the file, look it up, add with the file and
its cover), the reader page's routes, and title-only lookups. Offline: a fake
fetch stands in for Apple Books and Open Library."""
import dataclasses
import http.cookiejar
import io
import json
import socket
import threading
import urllib.error
import urllib.request
import uuid
import zipfile
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

from medialib.fill import providers as P
from medialib.fill import service
from medialib.web import auth as auth_mod
from medialib.web import server

OWNER = "owner@example.test"
PASSWORD = "correct horse battery staple"


def epub_bytes(title, author, isbn=None, subjects=(), cover=True):
    opf = ['<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
           "<dc:title>{}</dc:title><dc:creator>{}</dc:creator><dc:language>it</dc:language>".format(title, author),
           '<dc:identifier id="id">urn:uuid:{}</dc:identifier>'.format(uuid.uuid4())]
    if isbn:
        opf.append("<dc:identifier>urn:isbn:{}</dc:identifier>".format(isbn))
    opf += ["<dc:subject>{}</dc:subject>".format(s) for s in subjects]
    opf.append('</metadata><manifest><item id="c" href="c.html" media-type="application/xhtml+xml"/>')
    if cover:
        opf.append('<item id="img" href="cover.png" media-type="image/png" properties="cover-image"/>')
    opf.append('</manifest><spine><itemref idref="c"/></spine></package>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", "".join(opf))
        z.writestr("OEBPS/c.html", "<html><body><p>{}</p></body></html>".format(uuid.uuid4().hex * 50))
        if cover:
            png = io.BytesIO()
            Image.new("RGB", (200, 300), (180, 60, 40)).save(png, "PNG")
            z.writestr("OEBPS/cover.png", png.getvalue())
    return buf.getvalue()


def fake_fetch(title):
    track = int(uuid.uuid4().int % 10**9)

    def fetch(url):
        if url.startswith("https://itunes.apple.com/search"):
            return {"results": [{"trackId": track, "trackName": title, "artistName": "Ada Tester",
                                 "releaseDate": "2020-05-01T07:00:00Z", "genres": ["Science Fiction", "Books", "Fiction & Literature"]},
                                {"trackId": 7, "trackName": title + " - Summarized for Busy People", "artistName": "Goldmine Reads"}]}
        return None
    return fetch


class NoCovers:
    def fetch(self, url):
        from medialib.covers import CoverError
        raise CoverError("offline test")


@pytest.fixture(scope="module")
def site(cfg, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("epub")
    (tmp / "password").write_text(auth_mod.hash_password(PASSWORD) + "\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    origin = "http://127.0.0.1:{}".format(port)
    cfg = dataclasses.replace(cfg, media_dir=str(tmp / "media"))
    app = server.App(cfg, owner_login=OWNER, origin=origin, password_file=str(tmp / "password"),
                     passkeys_file=str(tmp / "pk.json"), state_dir=str(tmp / "state"), cookie_secure=False,
                     find_url="https://search.example/?q={q}", find_label="Example search")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"origin": origin, "app": app}
    httpd.shutdown()
    app.close()


class Client:
    def __init__(self, site):
        self.site = site
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, raw=None, **headers):
        h = {"Tailscale-User-Login": OWNER, "X-Console-Action": "1", "Origin": self.site["origin"],
             "Content-Type": "application/json"}
        h.update(headers)
        data = raw if raw is not None else (json.dumps(body or {}).encode() if method != "GET" else None)
        req = urllib.request.Request(self.site["origin"] + "/" + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def json(self, *a, **k):
        s, body, _ = self.call(*a, **k)
        return s, json.loads(body or b"{}")


def test_add_from_epub_by_candidate_and_as_the_file_says(site):
    c = Client(site)
    assert c.json("POST", "api/unlock", {"password": PASSWORD})[0] == 200
    title = "Epubtest " + uuid.uuid4().hex[:6]
    site["app"].fill = service.Fill(NoCovers(), fake_fetch(title))
    data = epub_bytes(title, "Ada Tester", subjects=["Science fiction"])
    s, r = c.json("POST", "api/epub", raw=data, **{"Content-Type": "application/epub+zip", "X-File-Name": "x.epub"})
    assert s == 200 and r["meta"]["title"] == title and r["meta"]["authors"] == ["Ada Tester"] and r["meta"]["hasCover"]
    assert r["meta"]["language"] == "ita" and [x["provider"] for x in r["candidates"]] == ["apple"]   # the summary is dropped
    s, added = c.json("POST", "api/epub-add", {"upload": r["upload"], "token": r["token"], "index": r["candidates"][0]["index"]})
    assert s == 200 and added["existing"] is False
    s, d = c.json("GET", "api/items/" + added["id"])
    assert len(d["files"]) == 1 and d["genreList"] == ["Science fiction"]
    assert any(cv["chosen"] and cv["note"] == "from the EPUB" for cv in d["covers"])     # no provider cover: the file's
    s, again = c.json("POST", "api/epub", raw=data, **{"Content-Type": "application/epub+zip"})
    assert again["existing"]["id"] == added["id"]                                        # the same file twice

    own = "Owntitle " + uuid.uuid4().hex[:6]
    site["app"].fill = service.Fill(NoCovers(), lambda url: None)                       # no source knows it
    s, r = c.json("POST", "api/epub", raw=epub_bytes(own, "Bea Writer", subjects=["Detective and mystery stories"], cover=False),
                  **{"Content-Type": "application/epub+zip"})
    assert s == 200 and r["candidates"] == []
    s, added = c.json("POST", "api/epub-add", {"upload": r["upload"], "index": None, "status": "completed"})
    s, d = c.json("GET", "api/items/" + added["id"])
    assert d["title"] == own and d["status"] == "completed" and d["genreList"] == ["Crime & mystery"]
    assert d["edition"]["lang"] == "ita" and d["authorsList"][0]["name"] == "Bea Writer"
    assert c.json("POST", "api/epub-add", {"upload": r["upload"]})[0] == 400              # used up


def test_epub_upload_needs_unlock_and_a_real_epub(site):
    c = Client(site)
    s, _, _ = c.call("POST", "api/epub", raw=b"PK not really", **{"Content-Type": "application/epub+zip"})
    assert s == 401
    assert c.json("POST", "api/unlock", {"password": PASSWORD})[0] == 200
    s, r = c.json("POST", "api/epub", raw=b"not a zip at all", **{"Content-Type": "application/epub+zip"})
    assert s == 400 and "EPUB" in r["error"]


def test_reader_routes(site):
    c = Client(site)
    s, body, h = c.call("GET", "read?file=" + str(uuid.uuid4()))
    assert s == 200 and b"reader.js" in body
    assert "script-src 'self';" in h["Content-Security-Policy"] and "frame-ancestors 'self'" in h["Content-Security-Policy"]
    assert "unsafe-inline' blob:" not in h["Content-Security-Policy"].split("script-src")[1].split(";")[0]
    s, _, h = c.call("GET", "vendor/foliate-js/view.js")
    assert s == 200 and h["Content-Type"].startswith("text/javascript")
    assert c.call("GET", "vendor/foliate-js/../../medialib/db.py")[0] == 404
    assert c.call("GET", "vendor/foliate-js/LICENSE")[0] == 404


def test_library_payload_has_find_link(site):
    s, lib = Client(site).json("GET", "api/library")
    assert lib["find"] == {"url": "https://search.example/?q={q}", "label": "Example search"} and lib["google"] is False


def test_title_only_prefers_the_book_most_people_mean():
    def fetch(url):
        if url.startswith("https://itunes.apple.com/search"):
            return {"results": [{"trackId": 1, "trackName": "The Night Shelf", "artistName": "Sam Smallname"}]}
        if url.startswith("https://openlibrary.org/search.json"):
            return {"docs": [{"key": "/works/OL2W", "title": "The Night Shelf", "author_name": ["Bea Bigname"],
                              "edition_count": 40, "readinglog_count": 9000, "edition_key": ["OL9M"]},
                             {"key": "/works/OL3W", "title": "The Night Shelf", "author_name": ["Sam Smallname"],
                              "edition_count": 1, "edition_key": ["OL8M"]}]}
        return None
    r = service.Fill(NoCovers(), fetch).lookup("The Night Shelf")
    assert r["candidates"][0]["authors"] == ["Bea Bigname"]
    assert P.score("Educated", [], P.Candidate("apple", "Educated - Summarized for Busy People", ["Goldmine Reads"], [])) == 0
