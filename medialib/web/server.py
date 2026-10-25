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
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from PIL import Image

from .. import db, migrate
from ..covers import CoverStore
from ..feeds import kosync
from ..fill import service as fill_service
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
DOC = r"([0-9a-f]{32})"
# database rules, in words (anything else shows the database's own message)
FRIENDLY = {
    "consumption_dates_ordered": "A reading can't finish before it started.",
    "consumption_open_has_no_end": "A reading in progress has no finish date.",
    "consumption_one_open": "This book already has a reading in progress.",
    "consumption_started_ok": "Start date: use YYYY, YYYY-MM or YYYY-MM-DD.",
    "consumption_ended_ok": "Finish date: use YYYY, YYYY-MM or YYYY-MM-DD.",
    "shelf_name": "There is already a collection with that name.",
    "person_name_key": "That person already exists.",
    "tag_name": "That tag already exists.",
}


class App:
    def __init__(self, cfg, owner_login, origin, password_file, passkeys_file, state_dir, cookie_secure=True,
                 reader_user="", reader_password_file=None):
        self.cfg = cfg
        kw = {"autocommit": True, "row_factory": dict_row}
        self.pool = ConnectionPool(cfg.dsn(db.ROLE_APP), min_size=1, max_size=4, kwargs=kw, open=True,
                                   name="app")
        self.ro_pool = ConnectionPool(cfg.dsn(db.ROLE_READONLY), min_size=1, max_size=2, kwargs=kw, open=True,
                                      name="readonly")
        self.auth = auth_mod.Auth(owner_login, origin, password_file, passkeys_file, state_dir)
        self.reader = reader.Reader(reader_user, reader_password_file)
        self.store = CoverStore(cfg.media_dir, log=lambda *_: None)
        self.fill = fill_service.Fill(self.store, fill_service.http_fetcher(os.path.join(cfg.media_dir, "cache", "http")))
        self.cookie_secure = cookie_secure
        self.cookie_name = "__Host-ml_session" if cookie_secure else "ml_session"
        self.thumb_lock = threading.Lock()
        self.expected_version = migrate.latest_version()
        self.backup_dir = os.environ.get("MEDIALIB_BACKUP_DIR", "")
        self.audit_verified = None
        self.health_lock = threading.Lock()

    def close(self):
        self.pool.close()
        self.ro_pool.close()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "media-library"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        # ------------------------------------------------------ responses

        def send(self, code, body, ctype="application/json", cache=None, cookie=None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';"
                             " img-src 'self' data: https://*.mzstatic.com https://covers.openlibrary.org;"
                             " frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
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
                if path == "/healthz":
                    return self.healthz()
                if path == "/api/health":
                    return self.send_health()
                if path == "/api/session":
                    return self.session()
                if path == "/api/library":
                    with app.pool.connection() as conn:
                        return self.send(200, queries.library(conn))
                m = re.fullmatch(r"/api/items/" + UUID, path)
                if m:
                    with app.pool.connection() as conn:
                        d = queries.detail(conn, m.group(1))
                    return self.send(200, d) if d else self.error(404, "No such item.")
                if path == "/api/stats":
                    with app.ro_pool.connection() as ro:
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

        def static(self, name, ctype):
            try:
                with open(os.path.join(WEB_DIR, name), "rb") as fh:
                    return self.send(200, fh.read(), ctype)
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
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with Image.open(os.path.join(app.cfg.media_dir, row["local_path"])) as im:
                            im = im.convert("RGB")
                            h = max(1, round(im.height * width / im.width))
                            im = im.resize((width, h), Image.LANCZOS)
                            tmp = dest + ".tmp"
                            im.save(tmp, "JPEG", quality=78, optimize=True, progressive=True)
                            os.replace(tmp, dest)
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

        def route_write(self, method, path, req):
            login = self.login()
            with app.pool.connection() as conn:
                m = re.fullmatch(r"/api/items/" + UUID, path)
                if m and method == "PATCH":
                    writes.edit_item(conn, login, m.group(1), req)
                    return self.send(200, queries.detail(conn, m.group(1)))
                if path == "/api/lookup" and method == "POST":
                    return self.send(200, app.fill.lookup(req.get("title"), req.get("author"), req.get("isbn")))
                if path == "/api/add-manual" and method == "POST":
                    return self.send(200, writes.add_manual(conn, login, req))
                if path == "/api/add" and method == "POST":
                    return self.send(200, app.fill.add(conn, login, req.get("token"), req.get("index"),
                                                       req.get("status") or "planned"))
                m = re.fullmatch(r"/api/items/" + UUID + r"/fill", path)
                if m and method == "POST":
                    res = app.fill.fill_missing(conn, login, m.group(1))
                    return self.send(200, dict(res, item=queries.detail(conn, m.group(1))))
                m = re.fullmatch(r"/api/items/" + UUID + r"/(status|readings|cover)", path)
                if m and method == "POST":
                    action = {"status": writes.set_status, "readings": writes.add_reading}.get(m.group(2))
                    if action:
                        action(conn, login, m.group(1), req)
                    else:
                        writes.choose_cover(conn, login, m.group(1), req, app.store)
                    return self.send(200, queries.detail(conn, m.group(1)))
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
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
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
                reader_password_file=env.get("MEDIALIB_READER_PASSWORD_FILE", "/run/secrets/reader.pw"))
