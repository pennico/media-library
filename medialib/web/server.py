"""The console's web side: page, JSON API, covers.

Standard-library HTTP server (like agent-console) behind Tailscale Serve.
Reads are open to the tailnet; every write needs, as in agent-console:
  - the owner's Tailscale login (Tailscale-User-Login == OWNER_LOGIN),
  - a live unlock session (password or passkey, 30 minutes; cookie
    HttpOnly, SameSite=Strict, Secure + __Host- prefix behind https),
  - the X-Console-Action header (a cross-site page cannot send it without a
    CORS preflight, which is never answered) and a same-origin Origin.
Writes go through the app role in audited transactions (writes.py);
statistics through the read-only role.

Reading apps (Readest) use /opds and /kosync instead: the owner's Tailscale
login plus the reading-app login (reader.py), no cookie, no Origin check
(the app sends its own Origin).
"""
import glob
import gzip
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .. import db, migrate
from ..covers import CoverStore, make_thumb
from ..feeds import kosync
from ..fill import service as fill_service
from ..fill import tmdb as tmdb_mod
from . import auth as auth_mod
from . import opds, queries, reader, writes

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web")
MAX_BODY = 256 * 1024
MAX_COVER = 15 * 1024 * 1024
MAX_EBOOK = 500 * 1024 * 1024
THUMB_WIDTHS = (160, 320, 640)
SHA_RE = re.compile(r"^/thumbs/([0-9a-f]{64})-(\d+)\.jpg$")
COVER_RE = re.compile(r"^/covers/([0-9a-f]{2})/([0-9a-f]{64})\.(jpg|png|webp)$")
UUID = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';"
       " img-src 'self' data: https://*.mzstatic.com https://covers.openlibrary.org https://books.google.com"
       " https://image.tmdb.org;"
       " frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
# The EPUB reader (web/reader.js): book sections render in blob: iframes that inherit this policy, which is
# what keeps a book's own scripts from running. No 'unsafe-inline' scripts; frame-ancestors 'self' (Safari).
READER_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline' blob:; img-src blob: data:;"
              " font-src blob: data:; media-src blob:; connect-src 'self'; frame-src blob:; base-uri 'none';"
              " form-action 'none'; frame-ancestors 'self'")
READER_JS = re.compile(r"^/(reader\.js|vendor/foliate-js/(vendor/)?[a-z-]+\.js)$")
DOC = r"([0-9a-f]{32})"
FRIENDLY = writes.FRIENDLY


GZIP_MIN = 1024
GZIP_TYPES = {"application/json", "text/html", "text/javascript", "application/atom+xml",
              "application/opensearchdescription+xml", "application/xml", "text/xml"}
_gz_cache = {}


def gzip_cached(data, key=None):
    """gzip for a response; an entry with a key (ETag / file version) is compressed once."""
    if key is not None and key in _gz_cache:
        return _gz_cache[key]
    out = gzip.compress(data, compresslevel=6, mtime=0)
    if key is not None:
        if len(_gz_cache) >= 32:
            _gz_cache.clear()
        _gz_cache[key] = out
    return out


class CountingReader:
    """The request stream, counting the body bytes read (see Handler.handle_one_request)."""

    def __init__(self, raw):
        self.raw, self.count = raw, 0

    def read(self, n=-1):
        data = self.raw.read(n)
        self.count += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self.raw, name)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # with keep-alive a browser closing an idle connection is routine, not an error worth a traceback
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class App:
    def __init__(self, cfg, owner_login, origin, password_file, passkeys_file, state_dir, cookie_secure=True,
                 reader_user="", reader_password_file=None, find_url="", find_label=""):
        self.cfg = cfg
        kw = {"autocommit": True, "row_factory": dict_row}
        self.pool = ConnectionPool(cfg.dsn(db.ROLE_APP), min_size=1, max_size=4, kwargs=kw, open=True,
                                   name="app")
        self.ro_pool = ConnectionPool(cfg.dsn(db.ROLE_READONLY), min_size=1, max_size=2, kwargs=kw, open=True,
                                      name="readonly")
        self.auth = auth_mod.Auth(owner_login, origin, password_file, passkeys_file, state_dir)
        self.reader = reader.Reader(reader_user, reader_password_file)
        # "find this book" link for books without a file: a URL template with {q}, e.g. a search page.
        # .env gives the default; the owner can change it in the console, since search sites move domains.
        self.find_default = find_link(find_url, find_label)
        self.find_file = os.path.join(state_dir, "find-link.json") if state_dir else None
        self.find_lock = threading.Lock()
        self.find = self.saved_find() or self.find_default
        self.store = CoverStore(cfg.media_dir, log=lambda *_: None)
        self.fill = fill_service.make(cfg.media_dir, self.store)
        self.tmdb = tmdb_mod.Tmdb(fill_service.http_fetcher(os.path.join(cfg.media_dir, "cache", "http")), tmdb_mod.api_key())
        self.cookie_secure = cookie_secure
        self.cookie_name = "__Host-ml_session" if cookie_secure else "ml_session"
        self.thumb_lock = threading.Lock()
        self.expected_version = migrate.latest_version()
        self.backup_dir = os.environ.get("MEDIALIB_BACKUP_DIR", "")
        self.audit_verified = None
        self.health_lock = threading.Lock()

    def warm_thumbs(self, width=320):
        """Make the shelf thumbnails nobody has asked for yet, one at a time, so the first
        opening of a big shelf doesn't wait on image resizing. Runs in the background."""
        try:
            with self.pool.connection() as conn:
                rows = conn.execute("SELECT DISTINCT sha256, local_path FROM catalog.cover"
                                    " WHERE local_path IS NOT NULL AND deleted_at IS NULL").fetchall()
            for r in rows:
                dest = os.path.join(self.cfg.media_dir, "thumbs", "{}-{}.jpg".format(r["sha256"], width))
                if os.path.exists(dest):
                    continue
                with self.thumb_lock:
                    if not os.path.exists(dest):
                        make_thumb(os.path.join(self.cfg.media_dir, r["local_path"]), dest, width)
        except Exception:     # noqa: BLE001 - a bad image or a closing pool must not matter
            traceback.print_exc()

    def close(self):
        self.pool.close()
        self.ro_pool.close()

    def saved_find(self):
        try:
            with open(self.find_file, encoding="utf-8") as fh:
                d = json.load(fh)
            return find_link(d.get("url"), d.get("label"))
        except (OSError, TypeError, ValueError, AttributeError):
            return None

    def find_payload(self):
        return {"find": self.find, "findCustom": self.find != self.find_default, "findDefault": self.find_default}

    def set_find(self, req, login):
        """The owner's own "find this book" address ({"url", "label"}), or {"reset": true} for .env's."""
        if not self.find_file:
            raise writes.BadRequest("this console has no state folder to keep the setting in")
        with self.find_lock:
            if req.get("reset"):
                if os.path.exists(self.find_file):
                    os.remove(self.find_file)
                self.find = self.find_default
            else:
                link = check_find(req)
                os.makedirs(os.path.dirname(self.find_file), exist_ok=True)
                tmp = self.find_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(dict(link, by=login, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())), fh)
                os.replace(tmp, self.find_file)
                self.find = link
        return self.find_payload()


def find_link(url, label):
    url = (url or "").strip()
    return {"url": url, "label": (label or "").strip() or "Search"} if "{q}" in url else None


def check_find(req):
    """An https address with {q} once, where the search words go (outside the host name)."""
    url, label = str(req.get("url") or "").strip(), str(req.get("label") or "").strip()
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    if not url.startswith("https://") or len(url) > 500 or re.search(r"[\s\"'<>\\]", url) or "." not in host:
        raise writes.BadRequest("the address must be a web address starting with https://, without spaces")
    if url.count("{q}") != 1 or "{q}" in host:
        raise writes.BadRequest("put {q} once in the address, where the search words go")
    if len(label) > 40:
        raise writes.BadRequest("the name on the link is too long (40 characters at most)")
    return find_link(url, label)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "media-library"
        sys_version = ""
        protocol_version = "HTTP/1.1"     # keep-alive: one connection serves the page, the API and the covers
        timeout = 120                     # drops idle keep-alive connections (and stalled uploads)
        disable_nagle_algorithm = True    # headers and body are two writes; with Nagle + delayed ACK each reply waits ~40 ms

        def log_message(self, fmt, *args):
            pass

        # ------------------------------------------------------ responses

        def setup(self):
            super().setup()
            self.rfile = CountingReader(self.rfile)

        def handle_one_request(self):
            self.rfile.count = 0
            super().handle_one_request()
            # keep-alive is only safe when the whole request body was read; an early refusal
            # (403, 401, 413...) leaves it in the stream, so close instead of misreading it
            try:
                unread = int(self.headers.get("Content-Length", "0") or 0) > self.rfile.count
            except (AttributeError, ValueError):
                unread = False
            if unread:
                self.close_connection = True

        def send(self, code, body, ctype="application/json", cache=None, cookie=None, csp=CSP, etag=None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            if etag is None and ctype == "application/json" and code == 200 and self.command in ("GET", "HEAD") \
                    and self.path.split("?", 1)[0] == "/api/library":
                etag = '"{}"'.format(hashlib.sha1(data).hexdigest())
                cache = cache or "no-cache"
            if etag and etag in [t.strip() for t in self.headers.get("If-None-Match", "").split(",")] and code == 200:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache or "no-store")
                self.end_headers()
                return
            gz = len(data) >= GZIP_MIN and ctype.split(";")[0] in GZIP_TYPES and \
                "gzip" in self.headers.get("Accept-Encoding", "")
            if gz:
                data = gzip_cached(data, etag)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Cache-Control", cache or "no-store")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def error(self, code, msg):
            return self.send(code, {"error": msg})

        def login(self):
            return self.headers.get("Tailscale-User-Login", "")

        def token(self):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == app.cookie_name:
                    return v
            return ""

        def cookie(self, value, max_age):
            secure = "; Secure" if app.cookie_secure else ""
            return "{}={}; Path=/; Max-Age={}; HttpOnly; SameSite=Strict{}".format(app.cookie_name, value, max_age, secure)

        def body(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > MAX_BODY:
                raise writes.BadRequest("request too large")
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise writes.BadRequest("body must be a JSON object")
            return data

        # ---------------------------------------------------------- GET

        def do_HEAD(self):
            return self.do_GET()

        def do_GET(self):
            path, _, query = self.path.partition("?")
            try:
                if path == "/opds" or path.startswith("/opds/"):
                    return self.opds(path, query)
                if path.startswith("/kosync/"):
                    return self.kosync_get(path)
                if path in ("/", "/index.html"):
                    return self.static("index.html", "text/html; charset=utf-8")
                if path == "/read":
                    return self.static("reader.html", "text/html; charset=utf-8", csp=READER_CSP)
                if READER_JS.match(path):
                    return self.static(path.lstrip("/"), "text/javascript; charset=utf-8", cache="public, max-age=3600")
                if path == "/healthz":
                    return self.healthz()
                if path == "/api/health":
                    return self.send_health()
                if path == "/api/session":
                    return self.session()
                if path == "/api/library":
                    with app.pool.connection() as conn:
                        return self.send(200, dict(queries.library(conn), google=app.fill.google.available,
                                                   tmdb=app.tmdb.available, **app.find_payload()))
                if path == "/api/rows":
                    ids = [x for x in urllib.parse.parse_qs(query).get("ids", [""])[0].split(",") if x]
                    if not ids or len(ids) > 200 or not all(re.fullmatch(UUID, x) for x in ids):
                        return self.error(400, "ids: 1 to 200 item ids, separated by commas.")
                    with app.pool.connection() as conn:
                        return self.send(200, queries.library_rows(conn, ids))
                if path == "/api/trash":
                    with app.pool.connection() as conn:
                        return self.send(200, {"items": queries.trash(conn, writes.TRASH_DAYS), "days": writes.TRASH_DAYS})
                m = re.fullmatch(r"/api/items/" + UUID, path)
                if m:
                    with app.pool.connection() as conn:
                        d = queries.detail(conn, m.group(1))
                    return self.send(200, d) if d else self.error(404, "No such item.")
                if path == "/api/stats":
                    q = urllib.parse.parse_qs(query)
                    with app.ro_pool.connection() as ro:
                        if q.get("view", [""])[0] == "library":
                            scope = q.get("scope", ["all"])[0]
                            return self.send(200, queries.library_stats(ro, scope if scope in ("all", "read", "unread") else "all"))
                        return self.send(200, queries.stats(ro, "book"))
                if path == "/api/reading-apps":
                    with app.pool.connection() as conn:
                        return self.send(200, dict(queries.reading_apps(conn), user=app.reader.user,
                                                   ready=app.reader.available(),
                                                   opds=app.auth.origin.rstrip("/") + "/opds",
                                                   kosync=app.auth.origin.rstrip("/") + "/kosync"))
                m = re.fullmatch(r"/api/files/" + UUID, path)
                if m:
                    return self.download(m.group(1))
                m = SHA_RE.match(path)
                if m:
                    return self.thumb(m.group(1), int(m.group(2)))
                m = COVER_RE.match(path)
                if m:
                    return self.file(os.path.join(app.cfg.media_dir, path.lstrip("/")))
            except psycopg.Error as e:
                return self.error(503, "Database error: {}".format(e.diag.message_primary or type(e).__name__))
            return self.error(404, "Not found.")

        def static(self, name, ctype, csp=CSP, cache=None):
            path = os.path.join(WEB_DIR, name)
            try:
                st = os.stat(path)
                etag = '"{:x}-{:x}"'.format(st.st_mtime_ns, st.st_size)
                if etag in [t.strip() for t in self.headers.get("If-None-Match", "").split(",")]:
                    return self.send(200, b"", ctype, csp=csp, cache=cache or "no-cache", etag=etag)
                with open(path, "rb") as fh:
                    return self.send(200, fh.read(), ctype, csp=csp, cache=cache or "no-cache", etag=etag)
            except OSError:
                return self.error(404, name + " missing")

        def file(self, path):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                return self.error(404, "No such cover.")
            ctype = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[path.rsplit(".", 1)[1]]
            return self.send(200, data, ctype, cache="public, max-age=31536000, immutable")

        def download(self, fid):
            with app.pool.connection() as conn:
                row = conn.execute("""SELECT f.rel_path, f.storage, f.format, f.size_bytes, i.title
                                        FROM catalog.edition_file f JOIN catalog.edition e ON e.id = f.edition_id
                                        JOIN catalog.media_item i ON i.id = e.work_id
                                       WHERE f.public_id = %s AND f.deleted_at IS NULL""", (fid,)).fetchone()
            if not row:
                return self.error(404, "No such file.")
            if row["storage"] != "store":
                return self.error(404, "This file is still in CWA; download it there.")
            path = os.path.join(app.cfg.media_dir, row["rel_path"])
            name = re.sub(r"[^A-Za-z0-9 ._-]+", "", row["title"])[:80].strip() or "book"
            try:
                fh = open(path, "rb")
            except OSError:
                return self.error(404, "The file is missing on disk.")
            with fh:
                self.send_response(200)
                self.send_header("Content-Type", opds.MIME.get(row["format"], "application/octet-stream"))
                self.send_header("Content-Length", str(os.path.getsize(path)))
                self.send_header("Content-Disposition", "attachment; filename=\"{}.{}\"; filename*=UTF-8''{}.{}".format(
                    name, row["format"], urllib.parse.quote(row["title"][:80]), row["format"]))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while self.command != "HEAD":
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        def thumb(self, sha, width):
            if width not in THUMB_WIDTHS:
                return self.error(404, "Unknown size.")
            dest = os.path.join(app.cfg.media_dir, "thumbs", "{}-{}.jpg".format(sha, width))
            if not os.path.exists(dest):
                with app.pool.connection() as conn:
                    row = conn.execute("SELECT local_path FROM catalog.cover WHERE sha256 = %s AND local_path IS NOT NULL"
                                       " LIMIT 1", (sha,)).fetchone()
                if not row:
                    return self.error(404, "No such cover.")
                with app.thumb_lock:
                    if not os.path.exists(dest):
                        make_thumb(os.path.join(app.cfg.media_dir, row["local_path"]), dest, width)
            return self.file(dest)

        def healthz(self):
            try:
                with app.pool.connection() as conn:
                    v = conn.execute("SELECT max(version) AS v FROM meta.schema_migration").fetchone()["v"]
            except (psycopg.Error, psycopg.OperationalError) as e:
                return self.send(503, {"ok": False, "error": type(e).__name__})
            ok = v == app.expected_version
            return self.send(200 if ok else 503, {"ok": ok, "schema": v, "expected": app.expected_version})

        def send_health(self):
            res = health(app)
            return self.send(200 if res["ok"] else 503, res)

        # ------------------------------------------------- reading apps

        def reader_gate(self, kosync_api):
            """True when the request may go on; otherwise the answer is sent."""
            if not app.auth.is_owner(self.login()):
                self.send(403, {"message": "Only the owner's Tailscale login can use this."})
                return False
            if not app.reader.available():
                self.send(503, {"message": "No reading-app login is set up on the server."})
                return False
            if app.reader.locked_out():
                self.send(429, {"message": "Too many failed logins. Try again in 10 minutes."})
                return False
            ok = app.reader.check_kosync(self.headers) if kosync_api else \
                app.reader.check_basic(self.headers.get("Authorization"))
            if ok:
                return True
            if self.headers.get("Authorization") or self.headers.get("x-auth-user") is not None:
                app.reader.fail()
            self.send_response(401)
            body = json.dumps({"message": "Unauthorized"}).encode()
            if not kosync_api:
                self.send_header("WWW-Authenticate", 'Basic realm="media-library", charset="UTF-8"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return False

        def opds(self, path, query):
            if not self.reader_gate(False):
                return None
            q = urllib.parse.parse_qs(query)
            page = q.get("page", ["1"])[0]
            page = int(page) if page.isdigit() and 0 < len(page) < 6 else 1
            m = re.fullmatch(r"/opds/files/" + UUID, path)
            if m:
                return self.download(m.group(1))
            if path == "/opds/search.xml":
                return self.send(200, opds.opensearch(app.auth.origin), "application/opensearchdescription+xml")
            with app.pool.connection() as conn:
                if path in ("/opds", "/opds/"):
                    return self.send(200, opds.root(conn), opds.NAV)
                if path == "/opds/books":
                    body = opds.books(conn, q.get("shelf", ["all"])[0], page)
                    return self.send(200, body, opds.ACQ) if body else self.error(404, "No such shelf.")
                if path == "/opds/search":
                    return self.send(200, opds.search(conn, q.get("q", [""])[0], page), opds.ACQ)
            return self.error(404, "Not found.")

        def kosync_get(self, path):
            if path == "/kosync/healthcheck":
                return self.send(200, {"state": "OK"})
            if not self.reader_gate(True):
                return None
            if path == "/kosync/users/auth":
                return self.send(200, {"authorized": "OK"})
            m = re.fullmatch(r"/kosync/syncs/progress/" + DOC, path.lower())
            if m:
                with app.pool.connection() as conn:
                    return self.send(200, kosync.get(conn, m.group(1)) or {})
            return self.send(404, {"message": "Not found"})

        def do_PUT(self):
            path = self.path.split("?", 1)[0]
            if path != "/kosync/syncs/progress":
                return self.error(405, "Method not allowed.")
            if not self.reader_gate(True):
                return None
            try:
                req = self.body()
                with app.pool.connection() as conn:
                    doc, ts = kosync.put(conn, req)
            except (ValueError, writes.BadRequest) as e:      # BadDocument is a ValueError
                return self.send(400, {"message": str(e) or "Bad request"})
            except psycopg.Error as e:
                traceback.print_exc()
                return self.send(503, {"message": e.diag.message_primary or type(e).__name__})
            return self.send(200, {"document": doc, "timestamp": ts})

        def kosync_create(self):
            if not app.auth.is_owner(self.login()):
                return self.send(403, {"message": "Only the owner's Tailscale login can use this."})
            try:
                req = self.body()
            except (ValueError, writes.BadRequest):
                return self.send(400, {"message": "Bad request"})
            if app.reader.available() and not app.reader.locked_out() and \
                    app.reader.check_create(req.get("username"), req.get("password")):
                return self.send(201, {"username": app.reader.user})
            app.reader.fail()
            return self.send(402, {"message": "Registration is closed: use the reading-app login from the console."})

        def session(self):
            login = self.login()
            until = app.auth.session_until(self.token()) if app.auth.is_owner(login) else None
            keys = app.auth.load_passkeys()
            return self.send(200, {"login": login, "owner": app.auth.is_owner(login), "unlockedUntil": until,
                                   "password": app.auth.password_available(),
                                   "passkeys": {"available": app.auth.passkeys_available() and bool(keys),
                                                "count": len(keys)}})

        # --------------------------------------------------------- writes

        def do_POST(self):
            return self.write("POST")

        def do_PATCH(self):
            return self.write("PATCH")

        def do_DELETE(self):
            return self.write("DELETE")

        def write(self, method):
            path = self.path.split("?", 1)[0]
            if method == "POST" and path == "/kosync/users/create":
                return self.kosync_create()
            if not app.auth.is_owner(self.login()):
                return self.error(403, "Only the owner's Tailscale login can change things.")
            origin = self.headers.get("Origin")
            allowed = {self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", ""),
                       app.auth.origin.split("://", 1)[-1]} - {""}
            if origin and origin.split("://", 1)[-1] not in allowed:
                return self.error(403, "Cross-origin request refused.")
            if self.headers.get("X-Console-Action") != "1":
                return self.error(403, "Missing X-Console-Action header.")
            if method == "POST" and path == "/api/epub":
                if not app.auth.session_until(self.token()):
                    return self.error(401, "Locked: unlock first (sessions last 30 minutes).")
                return self.guarded(self.epub_upload)
            m = re.fullmatch(r"/api/items/" + UUID + r"/(cover-upload|file)", path)
            if m and method == "POST":
                if not app.auth.session_until(self.token()):
                    return self.error(401, "Locked: unlock first (sessions last 30 minutes).")
                return self.guarded(lambda: self.upload(m.group(1), m.group(2)))
            try:
                req = self.body()
            except (ValueError, writes.BadRequest) as e:
                return self.error(400, str(e) or "Body must be a JSON object.")
            if method == "POST" and path == "/api/unlock":
                return self.unlock(req)
            if method == "POST" and path == "/api/lock":
                app.auth.end_session(self.token())
                return self.send(200, {"ok": True}, cookie=self.cookie("", 0))
            if method == "POST" and path == "/api/passkey/options":
                if not app.auth.passkeys_available():
                    return self.error(503, "Touch ID / Face ID isn't set up (add a passkey in the agent console).")
                return self.send(200, app.auth.login_options())
            if method == "POST" and path == "/api/passkey/login":
                return self.passkey_login(req)
            if not app.auth.session_until(self.token()):
                return self.error(401, "Locked: unlock first (sessions last 30 minutes).")
            return self.guarded(lambda: self.route_write(method, path, req))

        def guarded(self, fn):
            try:
                return fn()
            except writes.BadRequest as e:
                return self.error(400, str(e))
            except LookupError as e:
                return self.error(404, str(e).capitalize() + ".")
            except (fill_service.FillError, kosync.BadDocument) as e:
                return self.error(400, str(e))
            except psycopg.errors.IntegrityError as e:
                return self.error(409, FRIENDLY.get(e.diag.constraint_name or "", None)
                                  or e.diag.message_primary or "That change conflicts with existing data.")
            except psycopg.Error as e:
                if e.sqlstate == "ML002":
                    return self.error(409, e.diag.message_primary)
                traceback.print_exc()
                return self.error(400, e.diag.message_primary or type(e).__name__)
            except Exception:     # noqa: BLE001 - answer instead of dropping the connection
                traceback.print_exc()
                return self.error(500, "Something went wrong on the server; the change was not saved.")

        def upload(self, pid, kind):
            n = int(self.headers.get("Content-Length", "0") or 0)
            limit = MAX_COVER if kind == "cover-upload" else MAX_EBOOK
            if n <= 0:
                return self.error(400, "Empty upload.")
            if n > limit:
                return self.error(413, "Too large: the limit is {} MB.".format(limit // (1024 * 1024)))
            login = self.login()
            if kind == "cover-upload":
                data = self.rfile.read(n)
                if len(data) != n:
                    return self.error(400, "The upload was cut short.")
                with app.pool.connection() as conn:
                    writes.upload_cover(conn, login, pid, data, app.store)
                    return self.send(200, queries.detail(conn, pid))
            tmpdir = os.path.join(app.cfg.media_dir, "tmp")
            os.makedirs(tmpdir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=tmpdir, prefix="upload-")
            h, left = hashlib.sha256(), n
            try:
                with os.fdopen(fd, "wb") as out:
                    while left:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            raise writes.BadRequest("the upload was cut short")
                        out.write(chunk)
                        h.update(chunk)
                        left -= len(chunk)
                with app.pool.connection() as conn:
                    writes.attach_file(conn, login, pid, tmp, h.hexdigest(), app.cfg.media_dir)
                    return self.send(200, queries.detail(conn, pid))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

        def receive(self, prefix, limit):
            """Stream the request body into media/tmp; returns (path, sha256)."""
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0:
                raise writes.BadRequest("empty upload")
            if n > limit:
                raise writes.BadRequest("too large: the limit is {} MB".format(limit // (1024 * 1024)))
            tmpdir = os.path.join(app.cfg.media_dir, "tmp")
            os.makedirs(tmpdir, exist_ok=True)
            for old in glob.glob(os.path.join(tmpdir, "epub-*")):           # unfinished adds
                if time.time() - os.path.getmtime(old) > 2 * 3600:
                    os.remove(old)
            fd, tmp = tempfile.mkstemp(dir=tmpdir, prefix=prefix)
            h, left = hashlib.sha256(), n
            try:
                with os.fdopen(fd, "wb") as out:
                    while left:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            raise writes.BadRequest("the upload was cut short")
                        out.write(chunk)
                        h.update(chunk)
                        left -= len(chunk)
            except BaseException:
                os.remove(tmp)
                raise
            return tmp, h.hexdigest()

        def epub_upload(self):
            """Step 1 of "add from an EPUB": keep the file for an hour or two,
            read what it says about itself, and look the book up."""
            from .. import epubmeta
            tmp, sha = self.receive("epub-up-", MAX_EBOOK)
            try:
                writes.check_epub(tmp)
                with app.pool.connection() as conn:
                    dup = conn.execute("""SELECT i.public_id::text AS id, i.title FROM catalog.edition_file f
                                           JOIN catalog.edition e ON e.id = f.edition_id JOIN catalog.media_item i ON i.id = e.work_id
                                          WHERE f.sha256 = %s AND f.deleted_at IS NULL AND i.deleted_at IS NULL""", (sha,)).fetchone()
                if dup:
                    return self.send(200, {"existing": {"id": dup["id"], "title": dup["title"]}})
                try:
                    meta = epubmeta.read(tmp)
                except epubmeta.EpubError as e:
                    raise writes.BadRequest(str(e)) from None
                name = urllib.parse.unquote(self.headers.get("X-File-Name", ""))[:300]
                summary = writes.epub_summary(meta, name)
                upload = secrets.token_hex(16)
                final = os.path.join(app.cfg.media_dir, "tmp", "epub-{}.epub".format(upload))
                os.replace(tmp, final)
                with open(final[:-5] + ".json", "w", encoding="utf-8") as fh:
                    json.dump({"summary": summary, "sha256": sha}, fh)
                look = app.fill.epub_lookup(summary) if summary.get("title") or summary.get("isbns") else \
                    {"token": None, "candidates": [], "how": "nothing to search with"}
                return self.send(200, {"upload": upload, "meta": summary, "token": look["token"],
                                       "candidates": look["candidates"], "how": look["how"]})
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

        def epub_add(self, conn, login, req):
            upload = str(req.get("upload") or "")
            if not re.fullmatch(r"[0-9a-f]{32}", upload):
                raise writes.BadRequest("no such upload")
            base = os.path.join(app.cfg.media_dir, "tmp", "epub-" + upload)
            try:
                with open(base + ".json", encoding="utf-8") as fh:
                    kept = json.load(fh)
            except OSError:
                raise writes.BadRequest("that upload has expired: choose the file again") from None
            status = req.get("status") or "planned"
            if status not in ("planned", "in_progress", "completed"):
                raise writes.BadRequest("status must be planned, in_progress or completed")
            index = req.get("index")
            r = writes.add_from_epub(conn, login, base + ".epub", kept["summary"], kept["sha256"], app.cfg.media_dir,
                                     app.store, app.fill, req.get("token"), index, status)
            for ext in (".epub", ".json"):
                if os.path.exists(base + ext):
                    os.remove(base + ext)
            return self.with_version(conn, login, r, req)

        def with_version(self, conn, login, r, req):
            """Adding a book: the version picked in the form (ebook, print, audiobook) goes on the new book."""
            if req.get("format") and not r.get("existing"):
                writes.set_version(conn, login, r["id"], req["format"])
            return r

        def route_write(self, method, path, req):
            login = self.login()
            if path == "/api/settings/find" and method == "POST":
                return self.send(200, app.set_find(req, login))
            with app.pool.connection() as conn:
                m = re.fullmatch(r"/api/items/" + UUID, path)
                if m and method == "PATCH":
                    writes.edit_item(conn, login, m.group(1), req)
                    return self.send(200, queries.detail(conn, m.group(1)))
                if path == "/api/lookup" and method == "POST":
                    return self.send(200, app.fill.lookup(req.get("title"), req.get("author"), req.get("isbn")))
                if path == "/api/epub-add" and method == "POST":
                    return self.send(200, self.epub_add(conn, login, req))
                if req.get("format") and path in ("/api/add-manual", "/api/add", "/api/epub-add"):
                    writes.edition_format(req["format"], "version")       # refused before anything is added
                if path == "/api/add-manual" and method == "POST":
                    return self.send(200, self.with_version(conn, login, writes.add_manual(conn, login, req), req))
                if path == "/api/add" and method == "POST":
                    return self.send(200, self.with_version(conn, login, app.fill.add(
                        conn, login, req.get("token"), req.get("index"), req.get("status") or "planned"), req))
                m = re.fullmatch(r"/api/items/" + UUID + r"/fill", path)
                if m and method == "POST":
                    kind = writes.ids_for(conn, m.group(1))["kind"]
                    if kind in ("movie", "series"):
                        if not app.tmdb.available:
                            raise writes.BadRequest("TMDB is off: put an API key in secrets/tmdb.key")
                        res = writes.fill_screen(conn, login, m.group(1), app.tmdb, app.store)
                    else:
                        res = app.fill.fill_missing(conn, login, m.group(1))
                    return self.send(200, dict(res, item=queries.detail(conn, m.group(1))))
                if path in ("/api/tmdb/search", "/api/tmdb/add") and method == "POST":
                    if not app.tmdb.available:
                        raise writes.BadRequest("TMDB is off: put an API key in secrets/tmdb.key")
                    kind = req.get("kind")
                    if kind not in ("movie", "series"):
                        raise writes.BadRequest("kind must be movie or series")
                    if path.endswith("/search"):
                        year = writes.whole(req.get("year"), "year", 1870, 2100)
                        return self.send(200, {"candidates": app.tmdb.search(kind, writes.text(req.get("query"), "title", required=True), year)})
                    d = app.tmdb.details(kind, writes.whole(req.get("tmdb"), "TMDB id", 1, 10**12))
                    if not d:
                        raise writes.BadRequest("TMDB doesn't know that one")
                    from .. import screen
                    covers = writes.screen_posters(app.store, kind, screen.parse(kind, d))
                    return self.send(200, writes.add_screen(conn, login, kind, d, covers, req.get("status") or "planned"))
                m = re.fullmatch(r"/api/items/" + UUID + r"/(status|readings|cover)", path)
                if m and method == "POST":
                    action = {"status": writes.set_status, "readings": writes.add_reading}.get(m.group(2))
                    if action:
                        action(conn, login, m.group(1), req)
                    else:
                        writes.choose_cover(conn, login, m.group(1), req, app.store)
                    return self.send(200, queries.detail(conn, m.group(1)))
                m = re.fullmatch(r"/api/items/" + UUID + r"/(trash|restore|purge)", path)
                if m and method == "POST":
                    if m.group(2) == "trash":
                        writes.trash_item(conn, login, m.group(1))
                    elif m.group(2) == "restore":
                        writes.restore_item(conn, login, m.group(1))
                    else:
                        writes.purge_now(conn, login, m.group(1), app.cfg.media_dir)
                    return self.send(200, {"ok": True})
                m = re.fullmatch(r"/api/readings/" + UUID, path)
                if m and method in ("PATCH", "DELETE"):
                    writes.edit_reading(conn, login, m.group(1), req, delete=(method == "DELETE"))
                    return self.send(200, {"ok": True})
                if path == "/api/reading-apps/password" and method == "POST":
                    return self.send(200, {"user": app.reader.user, "password": app.reader.password()})
                m = re.fullmatch(r"/api/kosync/" + DOC + r"/link", path)
                if m and method == "POST":
                    kosync.link(conn, login, m.group(1), req.get("item"))
                    return self.send(200, {"ok": True})
                if path == "/api/items/batch" and method == "POST":
                    return self.send(200, writes.batch_edit(conn, login, req.get("rows")))
                if path == "/api/bulk" and method == "POST":
                    return self.send(200, writes.bulk(conn, login, req))
                if path == "/api/platforms" and method == "POST":
                    return self.send(200, {"code": writes.create_platform(conn, login, req)})
                if path == "/api/sections" and method == "POST":
                    return self.send(200, {"id": writes.create_section(conn, login, req)})
                m = re.fullmatch(r"/api/sections/" + UUID, path)
                if m and method in ("PATCH", "DELETE"):
                    if method == "PATCH":
                        writes.edit_section(conn, login, m.group(1), req)
                    else:
                        writes.delete_section(conn, login, m.group(1))
                    return self.send(200, {"ok": True})
                if path == "/api/shelves" and method == "POST":
                    return self.send(200, {"id": writes.create_shelf(conn, login, req)})
                m = re.fullmatch(r"/api/shelves/" + UUID + r"(/items|/move)?", path)
                if m:
                    sid, sub = m.group(1), m.group(2)
                    if sub is None and method == "PATCH":
                        writes.edit_shelf(conn, login, sid, req)
                    elif sub is None and method == "DELETE":
                        writes.delete_shelf(conn, login, sid)
                    elif sub == "/items" and method == "POST":
                        writes.shelf_items(conn, login, sid, req)
                    elif sub == "/move" and method == "POST":
                        writes.move(conn, login, sid, req)
                    else:
                        return self.error(405, "Method not allowed.")
                    return self.send(200, {"ok": True})
            return self.error(404, "Not found.")

        def unlock(self, req):
            if app.auth.locked_out():
                return self.error(429, "Too many failed tries. Try again in 10 minutes.")
            if not app.auth.password_available():
                return self.error(503, "No password is set up for this console.")
            if not app.auth.check_password(req.get("password", "")):
                app.auth.fail()
                return self.error(401, "Wrong password.")
            return self.start()

        def passkey_login(self, req):
            if app.auth.locked_out():
                return self.error(429, "Too many failed tries. Try again in 10 minutes.")
            try:
                app.auth.login_passkey(req)
            except (ValueError, KeyError, TypeError) as e:
                app.auth.fail()
                return self.error(401, "Touch ID / Face ID didn't verify: {}.".format(e))
            return self.start()

        def start(self):
            token, until = app.auth.start_session()
            return self.send(200, {"ok": True, "expiresAt": until}, cookie=self.cookie(token, app.auth.session_s))

    return Handler


def _age_h(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 3600
    except OSError:
        return None


def _newest(pattern):
    files = glob.glob(pattern)
    return max(files, key=os.path.getmtime) if files else None


def health(app):
    """DESIGN.md §5.8: everything that has to be true for the data to be safe."""
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    try:
        with app.pool.connection() as conn:
            q = lambda sql, *a: conn.execute(sql, a).fetchone()  # noqa: E731
            v = q("SELECT max(version) AS v FROM meta.schema_migration")["v"]
            add("schema version", v == app.expected_version, "{} (code expects {})".format(v, app.expected_version))
            add("page checksums on", q("SHOW data_checksums")["data_checksums"] == "on", "data_checksums")
            fails = q("SELECT coalesce(sum(checksum_failures), 0) AS n FROM pg_stat_database")["n"]
            add("no checksum failures", fails == 0, "{} failures".format(fails))
            a = q("SELECT archived_count, last_archived_time, failed_count, last_failed_time FROM pg_stat_archiver")
            recent_fail = a["last_failed_time"] and (not a["last_archived_time"] or a["last_failed_time"] > a["last_archived_time"])
            add("WAL archiving", not recent_fail, "{} archived, last {}; {} failures".format(
                a["archived_count"], a["last_archived_time"], a["failed_count"]))
            drift = [r["collname"] for r in conn.execute(
                "SELECT c.collname FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace"
                " WHERE n.nspname = 'catalog' AND c.collversion IS DISTINCT FROM pg_collation_actual_version(c.oid)")]
            add("sort collations match the ICU library", not drift,
                "reindex + REFRESH VERSION needed for " + ", ".join(drift) if drift else "ok")
            with app.health_lock:
                start = app.audit_verified
            r = q("SELECT * FROM audit.verify(%s)", start)
            if r["first_bad"] is None:
                last = q("SELECT max(id) AS m FROM audit.change")["m"]
                with app.health_lock:
                    app.audit_verified = last
            add("audit chain intact", r["first_bad"] is None,
                "{} new rows checked".format(r["checked"]) if r["first_bad"] is None else "broken at row {}".format(r["first_bad"]))
            j = q("SELECT status, extract(epoch FROM now() - coalesce(finished_at, started_at)) / 3600 AS age"
                  " FROM ingest.run WHERE tool = 'joplin' AND mode = 'apply' ORDER BY id DESC LIMIT 1")
            if j:      # only once the feed is set up
                add("Joplin feed ran ok < 3 h ago", j["status"] == "ok" and j["age"] < 3,
                    "{} {:.1f} h ago".format(j["status"], j["age"]))
    except psycopg.Error as e:
        add("database reachable", False, type(e).__name__)
    bdir = app.backup_dir
    if bdir:
        dump = _newest(os.path.join(bdir, "dumps", "medialib-*.dump"))
        age = _age_h(dump) if dump else None
        add("logical dump < 7 h", age is not None and age < 7, "{:.1f} h ago".format(age) if age is not None else "none yet")
        base = _newest(os.path.join(bdir, "base", "*", "backup_manifest"))
        age = _age_h(base) if base else None
        add("base backup < 26 h", age is not None and age < 26, "{:.1f} h ago".format(age) if age is not None else "none yet")
        try:
            with open(os.path.join(bdir, "restore-test.json"), encoding="utf-8") as fh:
                rt = json.load(fh)
            age = _age_h(os.path.join(bdir, "restore-test.json"))
            add("restore test ok and < 8 days", rt.get("ok") and age < 8 * 24,
                "{} {:.0f} h ago".format("ok" if rt.get("ok") else "FAILED", age))
        except (OSError, ValueError):
            add("restore test ok and < 8 days", False, "not run yet")
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def serve(cfg, host="0.0.0.0", port=8099, log=print, **kw):
    app = App(cfg, **kw)
    httpd = Server((host, port), make_handler(app))
    threading.Thread(target=app.warm_thumbs, daemon=True, name="warm-thumbs").start()
    log("media-library console on http://{}:{}/ (schema {})".format(host, port, app.expected_version))
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        app.close()


def from_env(cfg):
    env = os.environ
    return dict(owner_login=env.get("OWNER_LOGIN", ""), origin=env.get("MEDIALIB_ORIGIN", ""),
                password_file=env.get("MEDIALIB_PASSWORD_FILE", "/secrets/console-password"),
                passkeys_file=env.get("MEDIALIB_PASSKEYS_FILE", "/agent-console-state/passkeys.json"),
                state_dir=env.get("MEDIALIB_STATE_DIR", "/state"),
                cookie_secure=env.get("MEDIALIB_COOKIE_SECURE", "1") != "0",
                reader_user=env.get("MEDIALIB_READER_USER", ""),
                reader_password_file=env.get("MEDIALIB_READER_PASSWORD_FILE", "/run/secrets/reader.pw"),
                find_url=env.get("MEDIALIB_FIND_URL", ""), find_label=env.get("MEDIALIB_FIND_LABEL", ""))
