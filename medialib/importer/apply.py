"""Write an import plan to the database (inside the caller's transaction).

Idempotent: every target is found again through ``ingest.id_map`` (or its
natural key), only differences are written, values the owner set are never
touched (checked here and enforced by the database), locked items are left
alone. Counts per table and action go to ``self.counts``.
"""
import collections
import datetime
import hashlib
import json
import os
import re

from psycopg.types.json import Jsonb

from .. import db, files
from ..covers import CoverError

TRACKED_ITEM = ("title", "title_language", "original_title", "original_language", "first_published",
                "first_published_precision", "description")
TRACKED_EDITION = ("title", "subtitle", "language", "publisher", "published_on", "published_precision",
                   "pages", "format", "description")


def _jsonable(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return str(v)


class Writer:
    def __init__(self, conn, run_id, store, report, log=print):
        self.conn = conn
        self.run_id = run_id
        self.store = store
        self.report = report
        self.log = log
        self.counts = collections.Counter()
        self.skipped = []          # works the owner deleted (trash or for good): not brought back
        self._source = None
        self.stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # ---------------------------------------------------------- basics

    def q(self, sql, *args):
        return self.conn.execute(sql, args)

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def source(self, src):
        if src != self._source:
            db.set_source(self.conn, src)
            self._source = src

    def label(self, p):
        out = {"src": p["src"], "at": self.stamp}
        if p.get("ref"):
            out["ref"] = p["ref"]
        return out

    def map_get(self, key, table):
        row = self.one("SELECT target_id FROM ingest.id_map WHERE source = %s AND source_key = %s AND target_table = %s",
                       key[0], key[1], table)
        return row["target_id"] if row else None

    def map_put(self, key, table, target_id):
        self.q("INSERT INTO ingest.id_map (source, source_key, target_table, target_id, run_id)"
               " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (source, source_key, target_table)"
               " DO UPDATE SET target_id = EXCLUDED.target_id", key[0], key[1], table, target_id, self.run_id)

    def upsert(self, table, key, cols, prov, default_src, extra=None, pk="id", locked=False, title=""):
        """Insert, or update only the columns that differ (never owner-set
        ones, never locked rows). Returns the row id."""
        target = self.map_get(key, table) if key else None
        if target is None and extra and pk in extra:
            target = extra[pk] if self.one("SELECT 1 AS x FROM {} WHERE {} = %s".format(table, pk), extra[pk]) else None
        if target is None:
            values = dict(extra or {}, **cols)
            labels = {c: self.label(prov[c]) for c in cols if c in prov}
            self.source(default_src)
            names = list(values) + ["provenance"]
            row = self.one("INSERT INTO {} ({}) VALUES ({}) RETURNING {} AS id".format(
                table, ", ".join(names), ", ".join(["%s"] * len(names)), pk),
                *values.values(), Jsonb(labels))
            target = row["id"]
            self.counts[(table, "created")] += 1
            if key:
                self.map_put(key, table, target)
            return target
        if key:
            self.map_put(key, table, target)
        current = self.one("SELECT * FROM {} WHERE {} = %s".format(table, pk), target)
        changes = {}
        for c, v in cols.items():
            if current[c] == v:
                continue
            if (current["provenance"].get(c) or {}).get("src") == "owner":
                self.counts[(table, "kept_owner_value")] += 1
                self.report.add("kept_owner_value", table=table, id=target, column=c, title=title)
                continue
            changes[c] = v
        if not changes:
            self.counts[(table, "unchanged")] += 1
            return target
        if locked:
            self.counts[(table, "skipped_locked")] += 1
            self.report.add("skipped_locked", table=table, id=target, columns=sorted(changes), title=title)
            return target
        self.source(default_src)
        sets = ", ".join("{} = %s".format(c) for c in changes)
        labels = {c: self.label(prov.get(c) or {"src": default_src}) for c in changes}
        self.q("UPDATE {} SET {}, provenance = provenance || %s WHERE {} = %s".format(table, sets, pk),
               *changes.values(), Jsonb(labels), target)
        self.counts[(table, "updated")] += 1
        self.report.add("updated", table=table, id=target, columns=sorted(changes), title=title)
        return target

    # ----------------------------------------------------------- pieces

    def person(self, name, sort, prov):
        row = self.one("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)"
                       " AND disambiguation IS NULL AND deleted_at IS NULL", name)
        if row:
            self.counts[("catalog.person", "unchanged")] += 1
            return row["id"]
        self.source(prov["src"])
        labels = {"name": self.label(prov)}
        if sort:
            labels["sort_name"] = self.label(prov)
        row = self.one("INSERT INTO catalog.person (name, sort_name, provenance) VALUES (%s, %s, %s) RETURNING id",
                       name, sort, Jsonb(labels))
        self.counts[("catalog.person", "created")] += 1
        return row["id"]

    def contributions(self, item_id, authors, locked):
        for pos, a in enumerate(authors, 1):
            pid = self.person(a["name"], a.get("sort"), a["prov"])
            row = self.one("SELECT id, position, source FROM catalog.contribution WHERE item_id = %s"
                           " AND role = 'author' AND person_id = %s", item_id, pid)
            if row:
                self.counts[("catalog.contribution", "unchanged")] += 1
                continue
            if locked:
                self.counts[("catalog.contribution", "skipped_locked")] += 1
                continue
            self.source(a["prov"]["src"])
            taken = self.one("SELECT coalesce(max(position), 0) AS m FROM catalog.contribution"
                             " WHERE item_id = %s AND role = 'author'", item_id)["m"]
            self.q("INSERT INTO catalog.contribution (person_id, role, item_id, position, source)"
                   " VALUES (%s, 'author', %s, %s, %s)", pid, item_id, max(pos, taken + 1), a["prov"]["src"])
            self.counts[("catalog.contribution", "created")] += 1

    def external_ids(self, ids, title, edition_id=None, item_id=None, locked=False):
        for provider, value, src in ids:
            row = self.one("SELECT item_id, edition_id FROM catalog.external_id WHERE provider = %s AND value = %s",
                           provider, value)
            if row:
                if (row["edition_id"], row["item_id"]) == (edition_id, item_id):
                    self.counts[("catalog.external_id", "unchanged")] += 1
                else:
                    self.counts[("catalog.external_id", "collision")] += 1
                    self.report.add("id_collision", provider=provider, value=value, title=title,
                                    held_by_edition=row["edition_id"], held_by_item=row["item_id"])
                continue
            if locked:
                self.counts[("catalog.external_id", "skipped_locked")] += 1
                continue
            self.source(src)
            self.q("INSERT INTO catalog.external_id (provider, value, edition_id, item_id, source)"
                   " VALUES (%s, %s, %s, %s, %s)", provider, value, edition_id, item_id, src)
            self.counts[("catalog.external_id", "created")] += 1

    def covers(self, planned, title, edition_id=None, item_id=None, locked=False):
        owner_col = "edition_id" if edition_id else "item_id"
        owner_id = edition_id or item_id
        chosen_plan = [c for c in planned if c["chosen"]]
        fallbacks = [c for c in planned if c.get("fallback")]
        alternatives = [c for c in planned if not c["chosen"] and not c.get("fallback")]
        # the chosen cover: its image (downloaded once, or a local file)
        chosen = None
        for c in chosen_plan + fallbacks:
            if "info" in c:
                chosen = c
                break
        for c in chosen_plan:
            if "info" not in c:
                self.report.add("cover_download_failed", title=title, url=c.get("url"), error=c.get("error"),
                                used=chosen["source"] if chosen else None)
                if c.get("url"):
                    alternatives.append(dict(c, chosen=False, note="download failed: {}".format(c.get("error"))))
        if chosen is not None and chosen.get("fallback"):
            chosen = dict(chosen, note=chosen["note"] + " (Yamtrack's cover could not be downloaded)")
        current = self.one("SELECT id, provenance FROM catalog.cover WHERE {} = %s AND is_chosen AND deleted_at IS NULL"
                           .format(owner_col), owner_id)
        if chosen is not None:
            info = chosen["info"]
            row = self.one("SELECT id, is_chosen FROM catalog.cover WHERE {} = %s AND deleted_at IS NULL AND"
                           " (sha256 = %s OR source_url = %s)".format(owner_col), owner_id, info["sha256"], chosen.get("url"))
            keep_owner_choice = current and (current["provenance"].get("is_chosen") or {}).get("src") == "owner"
            make_chosen = not keep_owner_choice
            if keep_owner_choice and (not row or row["id"] != current["id"]):
                self.report.add("kept_owner_value", table="catalog.cover", id=current["id"], column="is_chosen", title=title)
            if row:
                cid = row["id"]
                if row["is_chosen"] or not make_chosen:
                    self.counts[("catalog.cover", "unchanged")] += 1
                elif not locked:
                    self.source(chosen["source"])
                    if current:
                        self.q("UPDATE catalog.cover SET is_chosen = false WHERE id = %s", current["id"])
                    self.q("UPDATE catalog.cover SET is_chosen = true WHERE id = %s", cid)
                    self.counts[("catalog.cover", "updated")] += 1
            elif locked and current:
                self.counts[("catalog.cover", "skipped_locked")] += 1
            else:
                self.source(chosen["source"])
                if current and make_chosen:
                    self.q("UPDATE catalog.cover SET is_chosen = false WHERE id = %s", current["id"])
                self.q("INSERT INTO catalog.cover ({}, source, source_url, local_path, sha256, mime, width, height,"
                       " bytes, fetched_at, is_chosen, note) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                       .format(owner_col), owner_id, chosen["source"], chosen.get("url"), info["local_path"],
                       info["sha256"], info["mime"], info["width"], info["height"], info["bytes"],
                       info.get("fetched_at"), make_chosen, chosen.get("note"))
                self.counts[("catalog.cover", "created")] += 1
        for c in alternatives:
            if not c.get("url"):
                continue
            if self.one("SELECT 1 AS x FROM catalog.cover WHERE {} = %s AND source_url = %s AND deleted_at IS NULL"
                        .format(owner_col), owner_id, c["url"]):
                self.counts[("catalog.cover", "unchanged")] += 1
                continue
            if locked:
                continue
            self.source(c["source"])
            self.q("INSERT INTO catalog.cover ({}, source, source_url, is_chosen, note) VALUES (%s, %s, %s, false, %s)"
                   .format(owner_col), owner_id, c["source"], c["url"], c.get("note"))
            self.counts[("catalog.cover", "created")] += 1

    def edition_files(self, edition_id, planned, locked):
        for f in planned:
            sha = files.sha256_file(f["path"])
            md5 = files.kosync_partial_md5(f["path"])
            if f["cwa_checksums"]:
                if md5 == f["cwa_checksums"][-1]:
                    self.counts[("kosync", "matches CWA latest")] += 1
                elif md5 in f["cwa_checksums"]:
                    self.counts[("kosync", "matches an older CWA checksum")] += 1
                    self.report.add("kosync_older", title=f["title"], file=f["rel_path"], computed=md5,
                                    cwa_latest=f["cwa_checksums"][-1], cwa_history=len(f["cwa_checksums"]))
                else:
                    self.counts[("kosync", "not in CWA history")] += 1
                    self.report.add("kosync_unknown", title=f["title"], file=f["rel_path"], computed=md5)
            cols = {"edition_id": edition_id, "format": f["format"], "storage": "cwa", "rel_path": f["rel_path"],
                    "size_bytes": os.path.getsize(f["path"]), "sha256": sha, "kosync_md5": md5,
                    "added_at": f["added_at"]}
            known = self.map_get(f["key"], "catalog.edition_file")
            if known and self.one("SELECT 1 AS x FROM catalog.edition_file WHERE id = %s AND storage = 'store'"
                                  " AND sha256 = %s", known, sha):
                del cols["storage"], cols["rel_path"]          # already copied into the store (adopt-files)
            prov = {c: {"src": "cwa", "ref": "data + file in the snapshot"} for c in cols}
            fid = self.upsert("catalog.edition_file", f["key"], cols, prov, "cwa", locked=locked, title=f["title"])
            for doc in dict.fromkeys(f["cwa_checksums"]):
                if doc == md5 or not re.fullmatch(r"[0-9a-f]{32}", doc or "") or self.one("SELECT 1 AS x FROM catalog.document_alias WHERE document = %s", doc) \
                        or self.one("SELECT 1 AS x FROM catalog.edition_file WHERE kosync_md5 = %s", doc):
                    continue
                self.source("cwa")
                self.q("INSERT INTO catalog.document_alias (document, edition_id, edition_file_id, source)"
                       " VALUES (%s, %s, %s, 'cwa')", doc, edition_id, fid)
                self.counts[("catalog.document_alias", "created")] += 1

    def tags(self, item_id, tags, locked):
        for t in tags:
            row = self.one("SELECT id FROM library.tag WHERE kind = %s AND lower(catalog.f_unaccent(name)) ="
                           " lower(catalog.f_unaccent(%s)) AND deleted_at IS NULL", t["kind"], t["name"])
            if row:
                tid = row["id"]
            else:
                self.source(t["source"])
                tid = self.one("INSERT INTO library.tag (kind, name) VALUES (%s, %s) RETURNING id", t["kind"], t["name"])["id"]
                self.counts[("library.tag", "created")] += 1
            if self.one("SELECT 1 AS x FROM library.item_tag WHERE item_id = %s AND tag_id = %s", item_id, tid):
                self.counts[("library.item_tag", "unchanged")] += 1
                continue
            if locked:
                continue
            self.source(t["source"])
            self.q("INSERT INTO library.item_tag (item_id, tag_id, position, source) VALUES (%s, %s, %s, %s)",
                   item_id, tid, t["position"], t["source"])
            self.counts[("library.item_tag", "created")] += 1

    # ------------------------------------------------------------- works

    def work(self, w):
        title = w["title"]
        item_cols = dict(w["item"], kind=w["kind"])
        item_prov = dict(w["item_prov"])
        existing = self.map_get(w["key"], "catalog.media_item")
        if existing is not None:
            row = self.one("SELECT deleted_at FROM catalog.media_item WHERE id = %s", existing)
            if row is None or row["deleted_at"] is not None:
                self.skipped.append(w)
                self.counts[("catalog.media_item", "deleted_by_owner")] += 1
                self.report.add("deleted_by_owner", title=title,
                                state="deleted for good" if row is None else "in the trash")
                return
        locked = bool(existing and self.one("SELECT locked FROM catalog.media_item WHERE id = %s", existing)["locked"])
        default_src = w["item_prov"]["title"]["src"]
        item_id = self.upsert("catalog.media_item", w["key"], {k: v for k, v in item_cols.items() if k != "kind"},
                              item_prov, default_src, extra={"kind": w["kind"]}, locked=locked, title=title)
        if existing is None:
            table = {"book": "catalog.book", "movie": "catalog.movie", "series": "catalog.series"}[w["kind"]]
            self.q("INSERT INTO {} (item_id) VALUES (%s)".format(table), item_id)
        for ed in w["editions"]:
            cols = dict(ed["cols"])
            extra = {"work_id": item_id, "is_primary": ed["is_primary"]}
            eid = self.upsert("catalog.edition", ed["key"], cols, ed["prov"], default_src, extra=extra,
                              locked=locked, title=title)
            self.external_ids(ed["ext_ids"], title, edition_id=eid, locked=locked)
            self.covers(ed["covers"], title, edition_id=eid, locked=locked)
            self.edition_files(eid, ed["files"], locked)
            ed["id"] = eid
        self.external_ids(w["item_ext_ids"], title, item_id=item_id, locked=locked)
        if w["item_covers"]:
            self.covers(w["item_covers"], title, item_id=item_id, locked=locked)
        self.contributions(item_id, w["authors"], locked)
        self.tags(item_id, w["tags"], locked)
        # the owner's side
        entry_src = w["entry_prov"]["status"]["src"]
        self.upsert("library.entry", w["key"], w["entry"], w["entry_prov"], entry_src,
                    extra={"item_id": item_id}, pk="item_id", title=title)
        primary = next((e["id"] for e in w["editions"] if e["is_primary"]), None)
        for r in w["readings"]:
            cols = {k: r[k] for k in ("state", "started_on", "started_precision", "ended_on", "ended_precision",
                                      "progress_pages", "progress_at", "language") if r.get(k) is not None}
            cols["edition_id"] = primary
            prov = dict(r["prov"], edition_id={"src": "yamtrack", "ref": "the book's primary edition"})
            self.upsert("library.consumption", r["key"], cols, prov, r["source"],
                        extra={"item_id": item_id, "source": r["source"]}, title=title)
        for h in w["history"]:
            if self.one("SELECT 1 AS x FROM audit.change WHERE occurred_at IS NOT NULL AND reason = %s", h["ref"]):
                self.counts[("audit.history", "unchanged")] += 1
                continue
            self.source("yamtrack")
            self.q("SELECT set_config('ml.reason', %s, true)", h["ref"])
            self.q("SELECT audit.record_external('library.entry', jsonb_build_object('item_id', %s::bigint), %s, %s, %s)",
                   item_id, Jsonb(h["old"]), Jsonb(h["new"]), h["occurred_at"])
            self.q("SELECT set_config('ml.reason', %s, true)", "import run {}".format(self.run_id))
            self.counts[("audit.history", "created")] += 1
        if w["locked"] and not locked:
            self.source("cwa")
            self.q("UPDATE catalog.media_item SET locked = true, provenance = provenance || %s WHERE id = %s",
                   Jsonb({"locked": self.label({"src": "cwa", "ref": "tts-ereader working copy (book-library PROTECTED_TITLES)"})}),
                   item_id)
            self.counts[("catalog.media_item", "locked")] += 1
        w["item_id"] = item_id
        return item_id

    # ---------------------------------------------------- source records

    def source_record(self, source, key, payload):
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_jsonable)
        sha = hashlib.sha256(body.encode()).hexdigest()
        row = self.one("SELECT id, payload_sha256 FROM ingest.source_record WHERE source = %s AND source_key = %s",
                       source, key)
        if row is None:
            self.q("INSERT INTO ingest.source_record (source, source_key, payload, payload_sha256, first_run_id, last_run_id)"
                   " VALUES (%s, %s, %s::jsonb, %s, %s, %s)", source, key, body, sha, self.run_id, self.run_id)
            self.counts[("ingest.source_record", "created")] += 1
        elif row["payload_sha256"] != sha:
            self.q("UPDATE ingest.source_record SET payload = %s::jsonb, payload_sha256 = %s, last_run_id = %s WHERE id = %s",
                   body, sha, self.run_id, row["id"])
            self.counts[("ingest.source_record", "updated")] += 1
        else:
            self.q("UPDATE ingest.source_record SET last_run_id = %s WHERE id = %s", self.run_id, row["id"])
            self.counts[("ingest.source_record", "unchanged")] += 1

    def all_source_records(self, snap):
        for i, r in snap.yt_items.items():
            self.source_record("yamtrack.app_item", str(i), r)
        for i, r in snap.yt_books.items():
            self.source_record("yamtrack.app_book", str(i), r)
        for r in snap.yt_history:
            self.source_record("yamtrack.app_historicalbook", str(r["history_id"]), r)
        for i, r in snap.cwa.items():
            self.source_record("cwa.books", str(i), r)
        for i, r in snap.links.items():
            self.source_record("sync.cwa_links", str(i), r)
        for c in snap.apple_applied.get("covers", []):
            self.source_record("sync.apple_covers_applied", c["media_id"], c)
        self.source_record("sync.apple_covers_applied", "_lists", {k: v for k, v in snap.apple_applied.items()
                                                                   if k != "covers"})
        self.source_record("sync.apple_keep", "_list", {"ids": snap.apple_keep})
        for k, v in snap.apple_cache.items():
            self.source_record("sync.apple_cache", k[:500], {"key": k, "hit": v})
        for k, v in snap.search_cache.items():
            self.source_record("sync.search_cache", k[:500], {"query": k, "results": v})
        for k, v in snap.cwa_migration.get("notes", {}).items():
            self.source_record("sync.cwa_migration", k, v)
        for name, oc in snap.owner_covers.items():
            self.source_record("owner.covers", name, {"file": name, "sha256": oc["sha256"], "bytes": oc["bytes"]})
        self.source_record("cwa_app.tables", "_counts", snap.cwa_app_counts)
