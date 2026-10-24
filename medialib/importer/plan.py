"""Snapshot -> import plan (pure: no database, no network).

Implements the mapping of DESIGN.md §6 and the owner's answers of
2026-10-24 (D9, D10, D25). Every value carries its provenance
(source, reference). Anything that cannot be decided is put in the report,
never guessed.
"""
import collections
import datetime
import decimal
import os
import re

from .. import text

STATUS = {"Planning": "planned", "In progress": "in_progress", "Completed": "completed",
          "Paused": "paused", "Dropped": "dropped"}
OWNER_ANSWER = "owner answer 2026-10-24: default status is to read"
ASIN = re.compile(r"^[A-Z0-9]{10}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FILE_FORMATS = {"epub", "pdf", "mobi", "azw3", "cbz", "m4b", "mp3"}
SNAPSHOT_TIME = datetime.datetime(2026, 10, 24, 15, 42, tzinfo=datetime.timezone.utc)


def P(src, ref=None):
    """A provenance label."""
    return {"src": src, "ref": ref} if ref else {"src": src}


def cover_kind(url):
    url = url or ""
    if "/covers/" in url and "openlibrary" not in url:
        return "owner"
    if "mzstatic.com" in url:
        return "apple"
    if "covers.openlibrary.org" in url and "/id/-1-" not in url:
        return "openlibrary"
    if "image.tmdb.org" in url:
        return "tmdb"
    return None     # empty, TMDB glyph placeholder, Open Library "-1"


def ts(value):
    """SQLite datetime text (naive = UTC) -> aware datetime."""
    if not value:
        return None
    s = str(value).replace("T", " ")
    s = re.sub(r"\+00:00$", "", s)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


class Report:
    def __init__(self):
        self.items = collections.defaultdict(list)

    def add(self, section, **fields):
        self.items[section].append(fields)


class Planner:
    def __init__(self, snap):
        self.s = snap
        self.report = Report()
        self.works = []
        # Yamtrack: logged items, and items the entry was moved off (D25)
        self.logged = {b["item_id"]: b for b in snap.yt_books.values()}
        self.superseded = {}            # orphan item id -> app_book id holding the entry now
        for iid, it in snap.yt_items.items():
            b = snap.yt_books.get(iid)
            if iid not in self.logged and it["media_type"] == "book" and b and b["item_id"] > iid \
                    and snap.yt_items[b["item_id"]]["media_type"] == "book":
                self.superseded[iid] = b["id"]
        # CWA: link -> Yamtrack item id
        self.by_media = {(it["source"], it["media_id"]): iid for iid, it in snap.yt_items.items()}
        self.cwa_of_item = collections.defaultdict(list)   # yamtrack item id -> [cwa ids]
        self.cwa_only = []                                   # cwa ids linked to an item missing from Yamtrack
        self.ignored = []
        for cid, link in sorted(snap.links.items(), key=lambda kv: int(kv[0])):
            cid = int(cid)
            m = link.get("media")
            if link.get("ignore") or not m:
                self.ignored.append(cid)
                continue
            iid = self.by_media.get((m["source"], str(m["media_id"])))
            if iid is None:
                self.cwa_only.append(cid)
            else:
                self.cwa_of_item[iid].append(cid)

    # ------------------------------------------------------------ helpers

    def cwa(self, cid):
        return self.s.cwa.get(cid)

    def cwa_records(self, iid):
        return [self.s.cwa[c] for c in self.cwa_of_item.get(iid, []) if c in self.s.cwa]

    def cover_alternatives(self, media_id, chosen_url):
        out = []
        applied = {c["media_id"]: c for c in self.s.apple_applied.get("covers", [])}
        a = applied.get(media_id)
        if a:
            for url, src, note in ((a.get("old"), "openlibrary", "cover before the Apple run (2026-10-21)"),
                                   (a.get("image"), "apple", "Apple cover the owner rejected (kept the previous one)"
                                    if media_id in self.s.apple_keep else "Apple cover")):
                if url and url != chosen_url and cover_kind(url) in ("openlibrary", "apple"):
                    out.append({"source": src, "url": url, "chosen": False, "note": note})
        return out

    def chosen_cover(self, image, cwa_recs, work_title):
        """Owner > Apple > Open Library (Yamtrack's image), else CWA's cover.jpg."""
        kind = cover_kind(image)
        covers = []
        if kind == "owner":
            name = image.rsplit("/", 1)[-1]
            oc = self.s.owner_covers.get(name)
            if oc:
                covers.append({"source": "owner", "file": oc["path"], "chosen": True,
                               "note": "owner-supplied cover ({})".format(name)})
            else:
                self.report.add("covers_missing", title=work_title, reason="owner cover file {} not in snapshot".format(name))
        elif kind in ("apple", "openlibrary", "tmdb"):
            covers.append({"source": kind, "url": image, "chosen": True})
        fallback = None
        for r in cwa_recs:
            p = os.path.join(self.s.root, "calibre-library", r["path"], "cover.jpg")
            if r.get("has_cover") and os.path.exists(p):
                fallback = {"source": "cwa", "file": p, "chosen": not covers, "fallback": bool(covers),
                            "note": "CWA's cover.jpg (record {})".format(r["id"])}
                break
        if fallback:
            covers.append(fallback)     # used if the download fails (apply step)
        elif not covers:
            self.report.add("covers_missing", title=work_title, reason="no cover anywhere")
        return covers

    def authors(self, iid, notes_by, ol, title):
        """CWA's current authors > notes 'By:' > Open Library (D8)."""
        for r in self.cwa_records(iid) if iid else []:
            people = []
            for a in r["authors"]:
                names = text.split_people(a["name"]) if "|" in a["name"] else (
                    [a["name"]] if a["name"].strip().lower() != "unknown" else [])
                for n in names:
                    people.append({"name": n.strip(), "sort": a["sort"] if len(names) == 1 else None,
                                   "prov": P("cwa", "books_authors_link (record {})".format(r["id"]))})
            if people:
                self._compare_by(title, notes_by, [p["name"] for p in people])
                return people
        names = text.split_people(notes_by)
        if names:
            return [{"name": n, "sort": None, "prov": P("yamtrack", "notes By:")} for n in names]
        ol_names = [text.first_last(a) for a in ((ol or {}).get("details") or {}).get("author") or [] if a]
        if ol_names:
            return [{"name": n, "sort": None, "prov": P("openlibrary", (ol or {}).get("media_id"))}
                    for n in dict.fromkeys(ol_names)]
        self.report.add("no_author", title=title)
        return []

    def _compare_by(self, title, notes_by, cwa_names):
        last = {text.norm(n).split()[-1] for n in cwa_names if text.norm(n)}
        extra = [n for n in text.split_people(notes_by)
                 if text.norm(n) and text.norm(n).split()[-1] not in last]
        if extra:
            self.report.add("authors_differ", title=title, notes_by=notes_by, cwa=", ".join(cwa_names),
                            not_in_cwa=", ".join(extra))

    def edition_from_item(self, it, cwa_recs, notes_fields, app_book, primary):
        """An edition from a Yamtrack item (+ its CWA records, + Open Library cache)."""
        ol = self.s.ol_details(it["media_id"]) if it["source"] == "openlibrary" else None
        det = (ol or {}).get("details") or {}
        cols, prov = {"title": it["title"]}, {"title": P("yamtrack", "app_item:{}".format(it["id"]))}
        cwa0 = cwa_recs[0] if cwa_recs else None
        # language: CWA, else the language the owner reads it in
        lang = next((text.language_code(lg) for r in cwa_recs for lg in r["languages"]), None)
        if lang:
            cols["language"], prov["language"] = lang, P("cwa", "languages")
        elif text.language_code(notes_fields.get("Read in")):
            cols["language"], prov["language"] = text.language_code(notes_fields["Read in"]), P("yamtrack", "notes Read in:")
        # pages: the owner's numbers (CWA #pages = Excel, also in the notes) > Open Library
        # (Yamtrack's edition) > CWA links > Yamtrack progress. Owner answer 2026-10-24 (D26).
        link_pages = next((self.s.links[str(c)]["media"].get("max_progress") for c in self.cwa_of_item.get(it["id"], [])
                           if (self.s.links[str(c)].get("media") or {}).get("max_progress")), None)
        cwa_pages = next((r["custom"].get("pages") for r in cwa_recs if r["custom"].get("pages")), None)
        ol_pages = det.get("number_of_pages") or link_pages
        for value, label in ((cwa_pages, P("cwa", "#pages")),
                             (notes_fields.get("Pages"), P("yamtrack", "notes Pages:")),
                             (det.get("number_of_pages"), P("openlibrary", it["media_id"])),
                             (link_pages, P("openlibrary", "cwa-links max_progress")),
                             (app_book["progress"] if app_book and app_book["status"] == "Completed"
                              and app_book["progress"] else None, P("yamtrack", "app_book.progress"))):
            if value and str(value).isdigit() and 0 < int(value) <= 20000:
                cols["pages"], prov["pages"] = int(value), label
                break
        if ol_pages and cols.get("pages") and int(ol_pages) != cols["pages"] and primary:
            self.report.add("pages_differ", title=it["title"], used=cols["pages"], used_from=prov["pages"]["src"],
                            openlibrary=int(ol_pages))
        # published: Open Library > CWA pubdate of a record with a file
        pd = text.parse_partial_date(det.get("publish_date"))
        if pd:
            cols["published_on"], cols["published_precision"] = pd
            prov["published_on"] = prov["published_precision"] = P("openlibrary", it["media_id"])
            if pd[1] != "day" and not re.fullmatch(r"\d{4}(-\d{2})?", str(det.get("publish_date"))):
                self.report.add("date_parsed", title=it["title"], raw=det.get("publish_date"), as_=pd[1])
        else:
            for r in cwa_recs:
                if "from-excel" not in r["tags"] and r["pubdate"] and not str(r["pubdate"]).startswith("0101"):
                    d = ts(r["pubdate"])
                    if d:
                        cols["published_on"], cols["published_precision"] = d.date(), "day"
                        prov["published_on"] = prov["published_precision"] = P("cwa", "pubdate (record {})".format(r["id"]))
                        break
        pub = next((r["publishers"][0] for r in cwa_recs if r["publishers"]), None)
        if pub:
            cols["publisher"], prov["publisher"] = pub.strip(), P("cwa", "publishers")
        desc = next((text.html_to_text(r["comments"]) for r in cwa_recs if r.get("comments")), None)
        if desc:
            cols["description"], prov["description"] = desc, P("cwa", "comments")

        ext = [("yamtrack_item", str(it["id"]), "yamtrack")]
        if it["source"] == "openlibrary":
            ext.append(("openlibrary_edition", it["media_id"], "yamtrack"))
        elif it["source"] == "manual":
            ext.append(("yamtrack_manual", it["media_id"], "yamtrack"))
        isbns = []
        for raw in det.get("isbn") or []:
            v = text.normalize_isbn(raw)
            if v:
                isbns.append((v, "openlibrary"))
            else:
                self.report.add("invalid_ids", title=it["title"], provider="isbn", value=raw, source="openlibrary")
        for r in cwa_recs:
            ext.append(("cwa_book", str(r["id"]), "cwa"))
            if r.get("uuid") and UUID.match(r["uuid"]):
                ext.append(("calibre_uuid", r["uuid"], "cwa"))
            ext += self.cwa_identifiers(r, it, isbns)
        seen = set()
        for v, src in isbns:
            if v not in seen:
                seen.add(v)
                ext.append(("isbn13", v, src))
        return {"key": ("yamtrack.app_item", str(it["id"])), "cols": cols, "prov": prov, "is_primary": primary,
                "ext_ids": ext, "covers": [], "files": []}

    def cwa_identifiers(self, r, it, isbns):
        out = []
        for typ, val in r["identifiers"].items():
            typ = typ.lower()
            if typ in ("isbn", "isbn10", "isbn13"):
                v = text.normalize_isbn(val)
                if v:
                    isbns.append((v, "cwa"))
                else:
                    self.report.add("invalid_ids", title=r["title"], provider=typ, value=val, source="cwa")
            elif typ == "openlibrary":
                if it is None or it["source"] != "openlibrary":
                    out.append(("openlibrary_edition", val, "cwa"))
                elif val != it["media_id"]:
                    self.report.add("ids_differ", title=r["title"], provider="openlibrary", cwa=val,
                                    yamtrack=it["media_id"])
            elif typ == "google":
                out.append(("google_books", val, "cwa"))
            elif typ in ("amazon", "mobi-asin"):
                if ASIN.match(val):
                    out.append(("amazon_asin", val, "cwa"))
                else:
                    self.report.add("invalid_ids", title=r["title"], provider=typ, value=val, source="cwa")
            elif typ == "ibdb":
                out.append(("ibdb", val, "cwa"))
            else:
                self.report.add("dropped_values", source="cwa.identifiers", field=typ, value=val, title=r["title"],
                                reason="no provider for it ({})".format(typ))
        return out

    def files_for(self, cwa_recs, title):
        out = []
        for r in cwa_recs:
            for d in r["data"]:
                fmt = d["format"].lower()
                rel = "{}/{}.{}".format(r["path"], d["name"], fmt)
                path = os.path.join(self.s.root, "calibre-library", rel)
                if fmt not in FILE_FORMATS:
                    self.report.add("files_skipped", title=title, file=rel, reason="format {}".format(fmt))
                    continue
                if not os.path.exists(path):
                    self.report.add("files_skipped", title=title, file=rel, reason="missing in the snapshot")
                    continue
                out.append({"key": ("cwa.data", "{}:{}".format(r["id"], d["format"])), "path": path,
                            "rel_path": rel, "format": fmt, "added_at": ts(r["timestamp"]),
                            "cwa_checksums": [c["checksum"] for c in r["checksums"] if c["format"] == d["format"]],
                            "title": title})
        return out

    def tags_for(self, notes_fields, cwa_recs):
        tags = []
        genre_src = notes_fields.get("Genre")
        src = P("yamtrack", "notes Genre:")
        if not genre_src:
            genre_src = next((r["custom"].get("genre") for r in cwa_recs if r["custom"].get("genre")), None)
            src = P("cwa", "#genre")
        for pos, g in enumerate(text.split_genres(genre_src), 1):
            tags.append({"kind": "genre", "name": g, "position": pos, "source": src["src"]})
        names = []
        for r in cwa_recs:
            for t in r["tags"]:
                if t != "from-excel" and t not in names:
                    names.append(t)
        for pos, t in enumerate(names, 1):
            tags.append({"kind": "tag", "name": t, "position": pos, "source": "cwa"})
        return tags

    def first_published(self, cwa_recs):
        """from-excel records carry the Excel 'Released' year as pubdate YYYY-01-02."""
        for r in cwa_recs:
            if "from-excel" in r["tags"] and r["pubdate"] and not str(r["pubdate"]).startswith("0101"):
                d = ts(r["pubdate"])
                if d:
                    return datetime.date(d.year, 1, 1), P("cwa", "pubdate (Excel Released year, record {})".format(r["id"]))
        return None, None

    # ------------------------------------------------------------- works

    def book_work(self, key, it, app_book, cwa_recs, title, status_src):
        notes = text.parse_notes(app_book["notes"]) if app_book else {"fields": {}, "review": None}
        f = notes["fields"]
        known = {"By", "Read in", "Original language", "Genre", "Pages", "Ebook"}
        for k in set(f) - known:
            self.report.add("dropped_values", source="yamtrack.app_book.notes", field=k, value=f[k], title=title,
                            reason="unknown notes key")
        primary = self.edition_from_item(it, cwa_recs, f, app_book, True) if it else self.edition_from_cwa(cwa_recs[0])
        item_cols = {"title": title}
        item_prov = {"title": P(status_src if not it else "yamtrack",
                                "app_item:{}".format(it["id"]) if it else "books.title (record {})".format(cwa_recs[0]["id"]))}
        lang = primary["cols"].get("language")
        if lang:
            item_cols["title_language"], item_prov["title_language"] = lang, primary["prov"]["language"]
        orig = text.language_code(f.get("Original language"))
        if orig:
            item_cols["original_language"], item_prov["original_language"] = orig, P("yamtrack", "notes Original language:")
            if orig == "ell":
                self.report.add("check", title=title, what="original language 'Greek' stored as Modern Greek (ell); "
                                "Ancient Greek is grc")
        elif f.get("Original language"):
            self.report.add("dropped_values", source="yamtrack.app_book.notes", field="Original language",
                            value=f["Original language"], title=title, reason="unknown language name")
        fp, fp_prov = self.first_published(cwa_recs)
        if fp:
            item_cols["first_published"], item_cols["first_published_precision"] = fp, "year"
            item_prov["first_published"] = item_prov["first_published_precision"] = fp_prov
        ol = self.s.ol_details(it["media_id"]) if it and it["source"] == "openlibrary" else None
        work = {"key": key, "kind": "book", "item": item_cols, "item_prov": item_prov,
                "locked": False, "editions": [primary], "item_ext_ids": [], "item_covers": [],
                "authors": self.authors(it["id"] if it else None, f.get("By"), ol, title) if it else
                [{"name": n, "sort": a["sort"] if "|" not in a["name"] else None,
                  "prov": P("cwa", "books_authors_link (record {})".format(cwa_recs[0]["id"]))}
                 for a in cwa_recs[0]["authors"] for n in (text.split_people(a["name"]) if "|" in a["name"]
                                                          else [a["name"]]) if n.lower() != "unknown"],
                "tags": self.tags_for(f, cwa_recs), "readings": [], "history": [], "title": title}
        image = it["image"] if it else None
        primary["covers"] = self.chosen_cover(image, cwa_recs, title)
        if it:
            primary["covers"] += self.cover_alternatives(it["media_id"], image)
        primary["files"] = self.files_for(cwa_recs, title)
        return work, notes

    def edition_from_cwa(self, r):
        """Primary edition for a CWA record whose Yamtrack item does not exist."""
        link = self.s.links[str(r["id"])]
        m = link["media"]
        cols = {"title": r["title"].strip()}
        prov = {"title": P("cwa", "books.title (record {})".format(r["id"]))}
        lang = next((text.language_code(lg) for lg in r["languages"]), None)
        if lang:
            cols["language"], prov["language"] = lang, P("cwa", "languages")
        for value, label in ((r["custom"].get("pages"), P("cwa", "#pages")),
                             (m.get("max_progress"), P("openlibrary", "cwa-links max_progress"))):
            if value and 0 < int(value) <= 20000:
                cols["pages"], prov["pages"] = int(value), label
                break
        if r["publishers"]:
            cols["publisher"], prov["publisher"] = r["publishers"][0], P("cwa", "publishers")
        if r["pubdate"] and not str(r["pubdate"]).startswith("0101") and "from-excel" not in r["tags"]:
            d = ts(r["pubdate"])
            cols["published_on"], cols["published_precision"] = d.date(), "day"
            prov["published_on"] = prov["published_precision"] = P("cwa", "pubdate")
        desc = text.html_to_text(r.get("comments"))
        if desc:
            cols["description"], prov["description"] = desc, P("cwa", "comments")
        ext = [("cwa_book", str(r["id"]), "cwa")]
        if r.get("uuid") and UUID.match(r["uuid"]):
            ext.append(("calibre_uuid", r["uuid"], "cwa"))
        if m.get("source") == "openlibrary":
            ext.append(("openlibrary_edition", m["media_id"], "cwa"))
        isbns = []
        ext += self.cwa_identifiers(r, None, isbns)
        for v in dict.fromkeys(v for v, _ in isbns):
            ext.append(("isbn13", v, "cwa"))
        # openlibrary from identifiers duplicates the link's: keep one
        ext = list(dict.fromkeys(ext))
        return {"key": ("cwa.books", str(r["id"])), "cols": cols, "prov": prov, "is_primary": True,
                "ext_ids": ext, "covers": [], "files": []}

    def logged_book(self, b):
        it = self.s.yt_items[b["item_id"]]
        cwa_recs = self.cwa_records(it["id"])
        work, notes = self.book_work(("yamtrack.app_book", str(b["id"])), it, b, cwa_recs, it["title"], "yamtrack")
        f = notes["fields"]
        created = ts(b["created_at"])
        added = min([created] + [ts(r["timestamp"]) for r in cwa_recs if ts(r["timestamp"])])
        ref = "app_book:{}".format(b["id"])
        entry = {"status": STATUS[b["status"]], "status_changed_at": created, "added_at": added}
        prov = {k: P("yamtrack", ref) for k in entry}
        hist = [h for h in self.s.yt_history if h["id"] == b["id"]]
        if b["score"] is not None:
            entry["rating"] = decimal.Decimal(str(b["score"])).quantize(decimal.Decimal("0.1"))
            changes = [ts(h["history_date"]) for prev, h in zip(hist, hist[1:]) if prev["score"] != h["score"]]
            entry["rated_at"] = max(changes) if changes else created
            prov["rating"] = prov["rated_at"] = P("yamtrack", ref)
        if notes["review"]:
            entry["review"], entry["reviewed_at"] = notes["review"], created
            prov["review"] = prov["reviewed_at"] = P("yamtrack", ref + " notes")
        work["entry"], work["entry_prov"] = entry, prov
        # history: changes after creation (the 8 score edits)
        for prev, h in zip(hist, hist[1:]):
            old, new = {}, {}
            if prev["score"] != h["score"]:
                old["rating"], new["rating"] = prev["score"], h["score"]
            if prev["status"] != h["status"]:
                old["status"], new["status"] = STATUS.get(prev["status"]), STATUS.get(h["status"])
            if prev["notes"] != h["notes"]:
                old["review"] = text.parse_notes(prev["notes"])["review"]
                new["review"] = text.parse_notes(h["notes"])["review"]
            for col in ("start_date", "end_date", "progress"):
                if prev[col] != h[col]:
                    old[col], new[col] = prev[col], h[col]
            if new:
                work["history"].append({"occurred_at": ts(h["history_date"]), "old": old, "new": new,
                                        "ref": "app_historicalbook:{}".format(h["history_id"])})
        work["readings"] = self.readings_for(b, f, cwa_recs, it["title"])
        return work

    def readings_for(self, b, f, cwa_recs, title):
        status = STATUS[b["status"]]
        if status == "planned":
            return []
        lang = text.language_code(f.get("Read in"))
        r = {"key": ("yamtrack.app_book", str(b["id"])), "state": status, "language": lang, "source": "yamtrack",
             "prov": {}}
        ref = P("yamtrack", "app_book:{}".format(b["id"]))
        if lang:
            r["prov"]["language"] = P("yamtrack", "notes Read in:")
        start, end = ts(b["start_date"]), ts(b["end_date"])
        if start:
            r["started_on"], r["started_precision"] = start.date(), "day"
            r["prov"]["started_on"] = r["prov"]["started_precision"] = ref
        if end and status in ("completed", "dropped"):
            created = ts(b["created_at"])
            years = {r_["custom"].get("read_year") for r_ in cwa_recs if r_["custom"].get("read_year")}
            year_only = (end.month, end.day) == (12, 31) or (created and end.date() == created.date()) \
                or end.year in years and (end.month, end.day) == (12, 31)
            if year_only:
                r["ended_on"], r["ended_precision"] = datetime.date(end.year, 1, 1), "year"
                r["prov"]["ended_on"] = r["prov"]["ended_precision"] = P("yamtrack", "app_book:{} end_date (a reading year)".format(b["id"]))
                if years and end.year not in years:
                    self.report.add("check", title=title, what="Yamtrack year {} but CWA read_year {}".format(end.year, sorted(years)))
            else:
                r["ended_on"], r["ended_precision"] = end.date(), "day"
                r["prov"]["ended_on"] = r["prov"]["ended_precision"] = ref
        if status in ("in_progress", "paused") and b["progress"]:
            r["progress_pages"] = int(b["progress"])
            r["progress_at"] = ts(b["progressed_at"])
            r["prov"]["progress_pages"] = r["prov"]["progress_at"] = ref
        r["prov"]["state"] = ref
        return [r]

    def unlogged_item(self, it):
        """A Yamtrack item never logged: to read / to watch (owner answer, D9)."""
        title = it["title"]
        if it["media_type"] == "book":
            work, _ = self.book_work(("yamtrack.app_item", str(it["id"])), it, None, self.cwa_records(it["id"]),
                                     title, "yamtrack")
        else:
            kind = {"movie": "movie", "tv": "series"}[it["media_type"]]
            work = {"key": ("yamtrack.app_item", str(it["id"])), "kind": kind, "title": title,
                    "item": {"title": title}, "item_prov": {"title": P("yamtrack", "app_item:{}".format(it["id"]))},
                    "locked": False, "editions": [], "authors": [], "tags": [], "readings": [], "history": [],
                    "item_ext_ids": [("tmdb_movie" if kind == "movie" else "tmdb_tv", it["media_id"], "yamtrack"),
                                     ("yamtrack_item", str(it["id"]), "yamtrack")],
                    "item_covers": self.chosen_cover(it["image"], [], title)}
        added = min([ts(r["timestamp"]) for r in self.cwa_records(it["id"]) if ts(r["timestamp"])] or [SNAPSHOT_TIME])
        work["entry"] = {"status": "planned", "status_changed_at": SNAPSHOT_TIME, "added_at": added}
        work["entry_prov"] = {"status": P("owner", OWNER_ANSWER), "status_changed_at": P("owner", OWNER_ANSWER),
                              "added_at": P("cwa" if added != SNAPSHOT_TIME else "yamtrack",
                                            "CWA timestamp" if added != SNAPSHOT_TIME else "snapshot time (no date in Yamtrack)")}
        return work

    def cwa_only_book(self, cid):
        r = self.s.cwa[cid]
        work, _ = self.book_work(("cwa.books", str(cid)), None, None, [r], r["title"].strip(), "cwa")
        link = self.s.links[str(cid)]
        image = (link.get("media") or {}).get("image")
        locked = any(text.norm(r["title"]).startswith(p) for p in PROTECTED_TITLES)
        ed = work["editions"][0]
        if locked:
            # what CWA shows, untouched by the mirror; Open Library as an alternative
            ed["covers"] = [c for c in self.chosen_cover(None, [r], work["title"])]
            if cover_kind(image):
                ed["covers"].append({"source": cover_kind(image), "url": image, "chosen": False,
                                     "note": "Open Library cover of the linked edition"})
            work["locked"] = True
        else:
            ed["covers"] = self.chosen_cover(image, [r], work["title"])
        ed["files"] = self.files_for([r], work["title"])
        work["tags"] = self.tags_for({}, [r])
        added = ts(r["timestamp"]) or SNAPSHOT_TIME
        work["entry"] = {"status": "planned", "status_changed_at": SNAPSHOT_TIME, "added_at": added}
        work["entry_prov"] = {"status": P("owner", OWNER_ANSWER), "status_changed_at": P("owner", OWNER_ANSWER),
                              "added_at": P("cwa", "books.timestamp")}
        return work

    def superseded_edition(self, iid):
        it = self.s.yt_items[iid]
        ed = self.edition_from_item(it, [], {}, None, False)
        ed["cols"] = {k: v for k, v in ed["cols"].items() if k in ("title", "pages", "published_on", "published_precision")}
        ed["covers"] = [{"source": cover_kind(it["image"]), "url": it["image"], "chosen": False,
                         "note": "cover of the edition Yamtrack replaced"}] if cover_kind(it["image"]) in ("apple", "openlibrary") else []
        return ed

    # --------------------------------------------------------------- run

    def build(self):
        s = self.s
        for b in sorted(s.yt_books.values(), key=lambda b: b["id"]):
            self.works.append(self.logged_book(b))
        by_book = {w["key"][1]: w for w in self.works}
        for iid, bid in sorted(self.superseded.items()):
            w = by_book[str(bid)]
            ed = self.superseded_edition(iid)
            w["editions"].append(ed)
            self.report.add("superseded", item=iid, title=s.yt_items[iid]["title"], now=w["title"],
                            similarity=round(text.title_similarity(s.yt_items[iid]["title"], w["title"]), 2))
        for iid, it in sorted(s.yt_items.items()):
            if iid in self.logged or iid in self.superseded:
                continue
            w = self.unlogged_item(it)
            self.works.append(w)
            self.report.add("unlogged_to_planned", item=iid, kind=w["kind"], title=it["title"],
                            linked_cwa=self.cwa_of_item.get(iid, []))
        for cid in self.cwa_only:
            w = self.cwa_only_book(cid)
            self.works.append(w)
            self.report.add("cwa_only_to_planned", cwa=cid, title=w["title"], locked=w["locked"])
        for cid in self.ignored:
            link = s.links[str(cid)]
            self.report.add("not_imported", source="cwa", id=cid, title=link.get("title"),
                            reason=link.get("ignore") or "no Yamtrack link", files=len(s.cwa.get(cid, {}).get("data", [])))
        for iid, recs in self.cwa_of_item.items():
            if len(recs) > 1:
                self.report.add("cwa_duplicates", item=iid, title=s.yt_items[iid]["title"], cwa=recs)
        return {"works": self.works, "report": self.report}


# tts-ereader working copies (book-library PROTECTED_TITLES), normalised
PROTECTED_TITLES = ("storia dell assedio di lisbona",)


def build(snapshot):
    return Planner(snapshot).build()
