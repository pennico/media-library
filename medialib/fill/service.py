"""Add a book from Apple Books / Open Library, or fill the empty fields of
one already in the library (DESIGN.md §7).

- `lookup` asks the providers (HTTP answers cached on disk for 30 days) and
  returns scored candidates; nothing is written.
- `add` creates the work, edition, authors, ids, cover and the entry in one
  transaction. Each value is labelled with the provider it came from; the
  entry's status is the owner's choice.
- `fill_missing` only fills empty fields. The database refuses any change
  to a value the owner set (provenance guard), and this code skips such
  fields before trying.
"""
import datetime
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

from psycopg.types.json import Jsonb

from .. import db, text
from ..covers import CoverError
from . import providers as P

USER_AGENT = "media-library/0.1 (personal library; contact: owner)"
CACHE_DAYS = 30
MIN_SCORE = 0.82


class FillError(ValueError):
    pass


def http_fetcher(cache_dir, pause=0.4, timeout=20):
    """fetch(url) -> parsed JSON or None; answers cached on disk."""
    lock = threading.Lock()

    def fetch(url):
        key = hashlib.sha256(url.encode()).hexdigest()
        path = os.path.join(cache_dir, key[:2], key + ".json")
        try:
            if time.time() - os.path.getmtime(path) < CACHE_DAYS * 86400:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)["body"]
        except (OSError, ValueError, KeyError):
            pass
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with lock:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = json.loads(r.read(5_000_000) or b"null")
            except urllib.error.HTTPError as e:
                body = None if e.code == 404 else _raise(e)
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise FillError("lookup failed ({}): {}".format(url.split("/")[2], e)) from e
            finally:
                time.sleep(pause)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"url": url, "at": time.time(), "body": body}, fh)
        os.replace(path + ".tmp", path)
        return body
    return fetch


def _raise(e):
    raise FillError("lookup failed: HTTP {}".format(e.code)) from e


class Fill:
    def __init__(self, store, fetch):
        self.store = store
        self.apple = P.AppleBooks(fetch)
        self.ol = P.OpenLibrary(fetch)
        self._lookups = {}
        self._lock = threading.Lock()

    # --------------------------------------------------------------- lookup

    def lookup(self, title=None, author=None, isbn=None):
        title = (title or "").strip()
        author = (author or "").strip() or None
        cands = []
        if isbn:
            isbn13 = text.normalize_isbn(isbn)
            if not isbn13:
                raise FillError("that is not a valid ISBN")
            for p in (self.ol, self.apple):
                c = p.by_isbn(isbn13)
                if c:
                    c.score = 1.0
                    cands.append(c)
        else:
            if not title:
                raise FillError("give a title (and an author if you know it) or an ISBN")
            wanted = [author] if author else []
            for p in (self.apple, self.ol):
                for c in p.search(title, author, limit=6):
                    c.score = P.score(title, wanted, c)
                    if c.score >= MIN_SCORE:
                        cands.append(c)
        cands.sort(key=lambda c: (-c.score, c.provider != "apple"))
        token = secrets.token_urlsafe(12)
        with self._lock:
            now = time.time()
            for k in [k for k, (t, _) in self._lookups.items() if now - t > 1800]:
                del self._lookups[k]
            self._lookups[token] = (now, cands[:12])
        return {"token": token, "candidates": [dict(c.as_dict(), index=i) for i, c in enumerate(cands[:12])]}

    def _picked(self, token, index):
        with self._lock:
            got = self._lookups.get(token)
        if not got:
            raise FillError("that search has expired: search again")
        try:
            return got[1][int(index)], got[1]
        except (ValueError, IndexError, TypeError):
            raise FillError("no such candidate") from None

    @staticmethod
    def _merge(chosen, others):
        """Chosen candidate's values; other providers fill what it lacks
        (only candidates whose title and author agree)."""
        merged = {"title": (chosen.title, chosen.provider), "authors": (chosen.authors, chosen.provider)}
        fields = ("language", "published", "pages", "publisher", "description", "work_id")
        for f in fields:
            if getattr(chosen, f):
                merged[f] = (getattr(chosen, f), chosen.provider)
        ids = list(chosen.ids)
        covers = [(chosen.cover_url, chosen.provider)] if chosen.cover_url else []
        best = {}       # the best agreeing candidate of each OTHER provider (never a second edition of the same one)
        for o in others:
            if o is chosen or o.provider == chosen.provider:
                continue
            sc = P.score(chosen.title, chosen.authors, o)
            if sc >= MIN_SCORE and sc > best.get(o.provider, (0, None))[0]:
                best[o.provider] = (sc, o)
        for _, o in best.values():
            for f in fields:
                if f not in merged and getattr(o, f):
                    merged[f] = (getattr(o, f), o.provider)
            if not merged["authors"][0] and o.authors:
                merged["authors"] = (o.authors, o.provider)
            ids += [i for i in o.ids if i not in ids]
            if o.cover_url:
                covers.append((o.cover_url, o.provider))
        # Apple first for the cover (D18 order: owner > apple > openlibrary)
        covers.sort(key=lambda c: c[1] != "apple")
        merged["ids"] = list(dict.fromkeys(ids))
        merged["covers"] = covers
        return merged

    # ------------------------------------------------------------------ add

    def add(self, conn, login, token, index, status="planned"):
        if status not in ("planned", "in_progress", "completed"):
            raise FillError("status must be planned, in_progress or completed")
        chosen, all_cands = self._picked(token, index)
        m = self._merge(chosen, all_cands)
        # already in the library? (same ISBN / Open Library edition / Apple id)
        for prov, val in m["ids"]:
            row = conn.execute("""SELECT i.public_id::text AS id FROM catalog.external_id x
                                  JOIN catalog.edition e ON e.id = x.edition_id JOIN catalog.media_item i ON i.id = e.work_id
                                 WHERE x.provider = %s AND x.value = %s AND i.deleted_at IS NULL""", (prov, val)).fetchone()
            if row:
                return {"id": row["id"], "existing": True}
        cover = None
        for url, src in m["covers"]:           # network before the transaction
            try:
                cover = dict(self.store.fetch(url), url=url, source=src)
                break
            except CoverError:
                continue
        now = datetime.datetime.now(datetime.timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        lab = lambda src, ref=None: dict({"src": src, "at": stamp}, **({"ref": ref} if ref else {}))  # noqa: E731
        title, tsrc = m["title"]
        lang = m.get("language", (None, None))
        with db.write_tx(conn, chosen.provider, "console:" + (login or "unknown"), reason="add from " + chosen.provider):
            item_prov = {"title": lab(tsrc)}
            if lang[0]:
                item_prov["title_language"] = lab(lang[1])
            iid = conn.execute("INSERT INTO catalog.media_item (kind, title, title_language, provenance)"
                               " VALUES ('book', %s, %s, %s) RETURNING id, public_id::text AS pid",
                               (title, lang[0], Jsonb(item_prov))).fetchone()
            conn.execute("INSERT INTO catalog.book (item_id) VALUES (%s)", (iid["id"],))
            cols = {"work_id": iid["id"], "title": title, "is_primary": True}
            prov = {"title": lab(tsrc)}
            for f, col in (("language", "language"), ("publisher", "publisher"), ("pages", "pages"),
                           ("description", "description")):
                if f in m and m[f][0]:
                    v = m[f][0]
                    if f == "pages" and not (isinstance(v, int) and 0 < v <= 20000):
                        continue
                    cols[col], prov[col] = (str(v)[:100000] if f == "description" else v), lab(m[f][1])
            if "published" in m:
                pd = _partial(m["published"][0])
                if pd:
                    cols["published_on"], cols["published_precision"] = pd
                    prov["published_on"] = prov["published_precision"] = lab(m["published"][1])
            names = list(cols) + ["provenance"]
            eid = conn.execute("INSERT INTO catalog.edition ({}) VALUES ({}) RETURNING id".format(
                ", ".join(names), ", ".join(["%s"] * len(names))), (*cols.values(), Jsonb(prov))).fetchone()["id"]
            for pos, name in enumerate(dict.fromkeys(a.strip() for a in m["authors"][0] if a and a.strip()), 1):
                db.set_source(conn, m["authors"][1])
                row = conn.execute("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)"
                                   " AND disambiguation IS NULL AND deleted_at IS NULL", (name,)).fetchone()
                pid = row["id"] if row else conn.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id",
                                                         (name,)).fetchone()["id"]
                conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source)"
                             " VALUES (%s, 'author', %s, %s, %s)", (pid, iid["id"], pos, m["authors"][1]))
            for prov_code, value in m["ids"]:
                if conn.execute("SELECT 1 AS x FROM catalog.external_id WHERE provider = %s AND value = %s",
                                (prov_code, value)).fetchone():
                    continue
                src = "apple" if prov_code == "apple_books" else "openlibrary"
                db.set_source(conn, src)
                conn.execute("INSERT INTO catalog.external_id (provider, value, edition_id, source) VALUES (%s, %s, %s, %s)",
                             (prov_code, value, eid, src))
            if m.get("work_id"):
                db.set_source(conn, "openlibrary")
                if not conn.execute("SELECT 1 AS x FROM catalog.external_id WHERE provider = 'openlibrary_work' AND value = %s",
                                    (m["work_id"][0],)).fetchone():
                    conn.execute("INSERT INTO catalog.external_id (provider, value, item_id, source) VALUES"
                                 " ('openlibrary_work', %s, %s, 'openlibrary')", (m["work_id"][0], iid["id"]))
            for url, src in m["covers"]:
                db.set_source(conn, src)
                if cover and url == cover["url"]:
                    conn.execute("INSERT INTO catalog.cover (edition_id, source, source_url, local_path, sha256, mime, width,"
                                 " height, bytes, fetched_at, is_chosen) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)",
                                 (eid, src, url, cover["local_path"], cover["sha256"], cover["mime"], cover["width"],
                                  cover["height"], cover["bytes"], cover.get("fetched_at")))
                else:
                    conn.execute("INSERT INTO catalog.cover (edition_id, source, source_url) VALUES (%s, %s, %s)"
                                 " ON CONFLICT DO NOTHING", (eid, src, url))
            db.set_source(conn, "owner")
            conn.execute("INSERT INTO library.entry (item_id, status) VALUES (%s, %s)", (iid["id"], status))
            if status in ("in_progress", "completed"):
                d = datetime.date.today()
                conn.execute("INSERT INTO library.consumption (item_id, edition_id, state, started_on, started_precision,"
                             " ended_on, ended_precision, source) VALUES (%s, %s, %s, %s, 'day', %s, %s, 'owner')",
                             (iid["id"], eid, status, d if status == "in_progress" else None,
                              d if status == "completed" else None, "day" if status == "completed" else None))
            run = conn.execute("INSERT INTO ingest.run (tool, mode, finished_at, status) VALUES ('fill', 'apply', now(), 'ok')"
                               " RETURNING id").fetchone()["id"]
            for c in all_cands:
                body = json.dumps(c.raw, sort_keys=True, default=str)
                key = "{}:{}".format(c.provider, (c.ids[0][1] if c.ids else hashlib.sha256(body.encode()).hexdigest()[:16]))
                conn.execute("INSERT INTO ingest.source_record (source, source_key, payload, payload_sha256, first_run_id, last_run_id)"
                             " VALUES (%s, %s, %s::jsonb, %s, %s, %s) ON CONFLICT (source, source_key) DO UPDATE"
                             " SET last_run_id = EXCLUDED.last_run_id",
                             ("fill." + c.provider, key[:500], body, hashlib.sha256(body.encode()).hexdigest(), run, run))
        return {"id": iid["pid"], "existing": False}

    # --------------------------------------------------------- fill missing

    def fill_missing(self, conn, login, pid):
        """Fill empty fields of an existing book; returns the changes."""
        it = conn.execute("""SELECT i.id, i.title, i.locked, e.id AS edition_id, e.language, e.publisher, e.pages,
                                    e.published_on, e.description, e.provenance
                               FROM catalog.media_item i JOIN catalog.edition e ON e.work_id = i.id AND e.is_primary
                              WHERE i.public_id = %s AND i.deleted_at IS NULL""", (pid,)).fetchone()
        if not it:
            raise LookupError("no such item")
        if it["locked"]:
            raise FillError("this book is locked (tts-ereader copy): only you can change it")
        ids = {r["provider"]: r["value"] for r in conn.execute(
            "SELECT provider, value FROM catalog.external_id WHERE edition_id = %s", (it["edition_id"],))}
        authors = [r["name"] for r in conn.execute(
            "SELECT p.name FROM catalog.contribution ct JOIN catalog.person p ON p.id = ct.person_id"
            " WHERE ct.item_id = %s AND ct.role = 'author' ORDER BY ct.position", (it["id"],))]
        cands = []
        if ids.get("isbn13"):
            cands += [c for c in (self.ol.by_isbn(ids["isbn13"]), self.apple.by_isbn(ids["isbn13"])) if c]
        elif ids.get("openlibrary_edition"):
            c = self.ol.by_edition(ids["openlibrary_edition"])
            if c:
                cands.append(c)
        for c in cands:
            c.score = 1.0
        if not cands:
            for p in (self.apple, self.ol):
                for c in p.search(it["title"], authors[0] if authors else None, limit=5):
                    c.score = P.score(it["title"], authors, c)
                    if c.score >= 0.9 and (authors or c.provider == "openlibrary"):
                        cands.append(c)
        if not cands:
            return {"changes": [], "note": "no confident match at Apple Books or Open Library"}
        cands.sort(key=lambda c: (-c.score, c.provider != "openlibrary"))
        m = self._merge(cands[0], cands)
        has_cover = conn.execute("SELECT 1 AS x FROM catalog.cover WHERE edition_id = %s AND is_chosen AND deleted_at IS NULL",
                                 (it["edition_id"],)).fetchone()
        cover = None
        if not has_cover:
            for url, src in m["covers"]:
                try:
                    cover = dict(self.store.fetch(url), url=url, source=src)
                    break
                except CoverError:
                    continue
        owner = lambda col: (it["provenance"].get(col) or {}).get("src") == "owner"  # noqa: E731
        changes = []
        with db.write_tx(conn, cands[0].provider, "console:" + (login or "unknown"), reason="fill missing"):
            for f, col in (("language", "language"), ("publisher", "publisher"), ("pages", "pages"),
                           ("description", "description")):
                if it[col] is None and not owner(col) and f in m and m[f][0]:
                    v = m[f][0]
                    if f == "pages" and not (isinstance(v, int) and 0 < v <= 20000):
                        continue
                    db.set_source(conn, m[f][1])
                    conn.execute("UPDATE catalog.edition SET {} = %s WHERE id = %s".format(col), (v, it["edition_id"]))
                    changes.append({"field": col, "value": v if f != "description" else str(v)[:80] + "…", "from": m[f][1]})
            if it["published_on"] is None and not owner("published_on") and "published" in m:
                pd = _partial(m["published"][0])
                if pd:
                    db.set_source(conn, m["published"][1])
                    conn.execute("UPDATE catalog.edition SET published_on = %s, published_precision = %s WHERE id = %s",
                                 (pd[0], pd[1], it["edition_id"]))
                    changes.append({"field": "published", "value": m["published"][0], "from": m["published"][1]})
            if not authors and m["authors"][0]:
                db.set_source(conn, m["authors"][1])
                for pos, name in enumerate(dict.fromkeys(m["authors"][0]), 1):
                    row = conn.execute("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)"
                                       " AND disambiguation IS NULL AND deleted_at IS NULL", (name,)).fetchone()
                    pid_ = row["id"] if row else conn.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id",
                                                              (name,)).fetchone()["id"]
                    conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source)"
                                 " VALUES (%s, 'author', %s, %s, %s)", (pid_, it["id"], pos, m["authors"][1]))
                changes.append({"field": "authors", "value": ", ".join(m["authors"][0]), "from": m["authors"][1]})
            for prov_code, value in m["ids"]:
                if prov_code in ids or conn.execute("SELECT 1 AS x FROM catalog.external_id WHERE provider = %s AND value = %s",
                                                    (prov_code, value)).fetchone():
                    continue
                src = "apple" if prov_code == "apple_books" else "openlibrary"
                db.set_source(conn, src)
                conn.execute("INSERT INTO catalog.external_id (provider, value, edition_id, source) VALUES (%s, %s, %s, %s)",
                             (prov_code, value, it["edition_id"], src))
                changes.append({"field": prov_code, "value": value, "from": src})
            if cover:
                db.set_source(conn, cover["source"])
                conn.execute("INSERT INTO catalog.cover (edition_id, source, source_url, local_path, sha256, mime, width, height,"
                             " bytes, fetched_at, is_chosen) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)",
                             (it["edition_id"], cover["source"], cover["url"], cover["local_path"], cover["sha256"],
                              cover["mime"], cover["width"], cover["height"], cover["bytes"], cover.get("fetched_at")))
                changes.append({"field": "cover", "value": cover["url"], "from": cover["source"]})
        return {"changes": changes, "matched": {"title": cands[0].title, "authors": cands[0].authors,
                                                "provider": cands[0].provider, "score": cands[0].score}}


def _partial(s):
    if not s:
        return None
    s = str(s)
    try:
        if len(s) >= 10:
            return datetime.date.fromisoformat(s[:10]), "day"
        if len(s) == 7:
            return datetime.date(int(s[:4]), int(s[5:7]), 1), "month"
        if len(s) == 4 and s.isdigit():
            return datetime.date(int(s), 1, 1), "year"
    except ValueError:
        return None
    return None
