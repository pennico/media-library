"""`medialib import`: snapshot -> database, dry-run by default.

1. read the snapshot and build the plan (pure);
2. fetch the covers into the content-addressed store (outside any
   transaction; each URL once);
3. one transaction: write, verify, check completeness, force the deferred
   constraint checks (SET CONSTRAINTS ALL IMMEDIATE), then COMMIT (--apply)
   or ROLLBACK (dry run);
4. write the reconciliation report to out/import-<time>-<mode>.{md,json}.
"""
import collections
import datetime
import decimal
import json
import os

import psycopg
from psycopg.types.json import Jsonb

from .. import db, text
from ..covers import CoverError, CoverStore
from . import apply as apply_mod
from . import fields, plan as plan_mod, sources


class ImportFailed(RuntimeError):
    pass


def prefetch(p, store, log):
    for w in p["works"]:
        for c in [c for e in w["editions"] for c in e["covers"]] + w["item_covers"]:
            if not (c["chosen"] or c.get("fallback")):
                continue
            try:
                c["info"] = store.put_file(c["file"]) if c.get("file") else store.fetch(c["url"])
            except (CoverError, OSError) as e:
                c["error"] = str(e)
    log("covers: {} downloaded now, the rest from the store/cache".format(store.fetched))


def verify(conn, snap, p, w):
    """Checks after writing (same transaction). Each -> {name, ok, detail}."""
    out = []
    q = lambda sql, *a: conn.execute(sql, a).fetchall()  # noqa: E731
    mapped = {r["source_key"]: r["target_id"] for r in q(
        "SELECT source_key, target_id FROM ingest.id_map WHERE source = 'yamtrack.app_book'"
        " AND target_table = 'catalog.media_item'")}
    bad = []
    for b in snap.yt_books.values():
        iid = mapped.get(str(b["id"]))
        e = conn.execute("SELECT * FROM library.entry WHERE item_id = %s", (iid,)).fetchone() if iid else None
        if not e:
            bad.append((b["id"], "no entry"))
            continue
        if e["status"] != plan_mod.STATUS[b["status"]]:
            bad.append((b["id"], "status {} != {}".format(e["status"], b["status"])))
        want = None if b["score"] is None else decimal.Decimal(str(b["score"]))
        if (e["rating"] is None) != (want is None) or (want is not None and e["rating"] != want):
            if not (e["provenance"].get("rating") or {}).get("src") == "owner":
                bad.append((b["id"], "rating {} != {}".format(e["rating"], b["score"])))
        review = text.parse_notes(b["notes"])["review"]
        if e["review"] != review and (e["provenance"].get("review") or {}).get("src") != "owner":
            bad.append((b["id"], "review differs"))
        if b["status"] == "Completed":
            n = conn.execute("SELECT count(*) AS n FROM library.consumption WHERE item_id = %s AND state = 'completed'"
                             " AND deleted_at IS NULL AND extract(year FROM ended_on) = %s",
                             (iid, plan_mod.ts(b["end_date"]).year)).fetchone()["n"]
            if n < 1:
                bad.append((b["id"], "no completed reading in {}".format(b["end_date"][:4])))
    out.append({"name": "every Yamtrack entry landed (status, rating, review, reading year)",
                "ok": not bad, "detail": "{} entries checked".format(len(snap.yt_books)), "problems": bad})
    linked = [cid for cid, link in snap.links.items() if link.get("media") and not link.get("ignore")]
    have = {r["value"] for r in q("SELECT value FROM catalog.external_id WHERE provider = 'cwa_book'")}
    missing = sorted((c for c in linked if c not in have), key=int)
    out.append({"name": "every linked CWA record landed (external id cwa_book)", "ok": not missing,
                "detail": "{} linked records".format(len(linked)), "problems": missing})
    planned_files = [f for wk in p["works"] for e in wk["editions"] for f in e["files"]]
    n_files = conn.execute("SELECT count(*) AS n FROM catalog.edition_file WHERE deleted_at IS NULL").fetchone()["n"]
    out.append({"name": "every CWA file of an imported book landed", "ok": n_files >= len(planned_files),
                "detail": "{} planned, {} in the database".format(len(planned_files), n_files), "problems": []})
    no_cover = [r["title"] for r in q(
        "SELECT i.title FROM catalog.media_item i JOIN library.entry e ON e.item_id = i.id"
        " LEFT JOIN catalog.edition ed ON ed.work_id = i.id AND ed.is_primary"
        " WHERE i.deleted_at IS NULL AND i.id IN (SELECT target_id FROM ingest.id_map"
        "   WHERE target_table = 'catalog.media_item')"                   # only what the import brought in
        " AND NOT EXISTS (SELECT 1 FROM catalog.cover c WHERE c.is_chosen"
        " AND c.deleted_at IS NULL AND (c.edition_id = ed.id OR c.item_id = i.id)) ORDER BY i.sort_title")]
    expected_missing = {r["title"] for r in p["report"].items.get("covers_missing", [])}
    out.append({"name": "every item has a chosen local cover (except those with none anywhere)",
                "ok": set(no_cover) <= expected_missing,
                "detail": "{} without a cover: {}".format(len(no_cover), ", ".join(no_cover) or "none"),
                "problems": sorted(set(no_cover) - expected_missing)})
    chain = conn.execute("SELECT * FROM audit.verify()").fetchone()
    out.append({"name": "audit chain intact", "ok": chain["first_bad"] is None,
                "detail": "{} rows checked".format(chain["checked"]), "problems": []})
    entries = conn.execute("SELECT count(*) AS n FROM library.entry WHERE deleted_at IS NULL").fetchone()["n"]
    out.append({"name": "one entry per planned work", "ok": entries >= len(p["works"]),
                "detail": "{} works planned, {} entries".format(len(p["works"]), entries), "problems": []})
    return out


def totals(conn):
    one = lambda sql: conn.execute(sql).fetchone()["n"]  # noqa: E731
    return collections.OrderedDict([
        ("media items", one("SELECT count(*) AS n FROM catalog.media_item WHERE deleted_at IS NULL")),
        ("  books", one("SELECT count(*) AS n FROM catalog.media_item WHERE kind = 'book' AND deleted_at IS NULL")),
        ("  movies", one("SELECT count(*) AS n FROM catalog.media_item WHERE kind = 'movie' AND deleted_at IS NULL")),
        ("  series", one("SELECT count(*) AS n FROM catalog.media_item WHERE kind = 'series' AND deleted_at IS NULL")),
        ("editions", one("SELECT count(*) AS n FROM catalog.edition WHERE deleted_at IS NULL")),
        ("  primary", one("SELECT count(*) AS n FROM catalog.edition WHERE is_primary AND deleted_at IS NULL")),
        ("files", one("SELECT count(*) AS n FROM catalog.edition_file WHERE deleted_at IS NULL")),
        ("people", one("SELECT count(*) AS n FROM catalog.person WHERE deleted_at IS NULL")),
        ("contributions", one("SELECT count(*) AS n FROM catalog.contribution")),
        ("external ids", one("SELECT count(*) AS n FROM catalog.external_id")),
        ("covers", one("SELECT count(*) AS n FROM catalog.cover WHERE deleted_at IS NULL")),
        ("  chosen (stored locally)", one("SELECT count(*) AS n FROM catalog.cover WHERE is_chosen AND deleted_at IS NULL")),
        ("entries", one("SELECT count(*) AS n FROM library.entry WHERE deleted_at IS NULL")),
        ("  to read", one("SELECT count(*) AS n FROM library.entry WHERE status = 'planned' AND deleted_at IS NULL")),
        ("  read", one("SELECT count(*) AS n FROM library.entry WHERE status = 'completed' AND deleted_at IS NULL")),
        ("  rated", one("SELECT count(*) AS n FROM library.entry WHERE rating IS NOT NULL AND deleted_at IS NULL")),
        ("  with review", one("SELECT count(*) AS n FROM library.entry WHERE review IS NOT NULL AND deleted_at IS NULL")),
        ("readings", one("SELECT count(*) AS n FROM library.consumption WHERE deleted_at IS NULL")),
        ("genres / tags", one("SELECT count(*) AS n FROM library.tag WHERE deleted_at IS NULL")),
        ("shelf places", one("SELECT count(*) AS n FROM library.shelf_item")),
        ("source records (raw)", one("SELECT count(*) AS n FROM ingest.source_record")),
        ("audit rows", one("SELECT count(*) AS n FROM audit.change")),
    ])


def run(cfg, snapshot_dir, apply=False, offline=False, out_dir="out", log=print):
    mode = "apply" if apply else "dry_run"
    started = datetime.datetime.now(datetime.timezone.utc)
    snap = sources.read(snapshot_dir)
    p = plan_mod.build(snap)
    store = CoverStore(cfg.media_dir, offline=offline, log=log)
    prefetch(p, store, log)
    field_table, unclassified = fields.check(snap)
    result = {"mode": mode, "started": started.isoformat(), "snapshot": snap.root, "sources": snap.files}
    with db.connect(cfg, db.ROLE_APP) as conn:
        conn.execute("SET statement_timeout = '120s'")
        try:
            with db.write_tx(conn, "yamtrack", "importer", reason="import"):
                run_id = conn.execute("INSERT INTO ingest.run (tool, mode, sources) VALUES ('import', %s, %s)"
                                      " RETURNING id", (mode, Jsonb(snap.files))).fetchone()["id"]
                conn.execute("SELECT set_config('ml.reason', %s, true)", ("import run {}".format(run_id),))
                w = apply_mod.Writer(conn, run_id, store, p["report"], log=log)
                for work in p["works"]:
                    w.work(work)
                w.all_source_records(snap)
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")     # run the deferred checks now
                checks = verify(conn, snap, p, w)
                checks.append({"name": "every non-empty source field is imported, validated or dropped",
                               "ok": not unclassified, "detail": "{} rules".format(len(field_table)),
                               "problems": unclassified})
                result.update(run_id=run_id, checks=checks, totals=totals(conn),
                              counts={"{} {}".format(*k): v for k, v in sorted(w.counts.items())})
                ok = all(c["ok"] for c in checks)
                conn.execute("UPDATE ingest.run SET finished_at = now(), status = %s, counts = %s WHERE id = %s",
                             ("ok" if ok else "failed", Jsonb(result["counts"]), run_id))
                if not ok:
                    raise ImportFailed("completeness/verification failed")
                if not apply:
                    raise psycopg.Rollback()
            result["committed"] = apply
        except ImportFailed:
            result["committed"] = False
            result["failed"] = True
    result["report"] = {k: v for k, v in p["report"].items.items()}
    result["fields"] = field_table
    path = write_report(result, out_dir)
    result["report_path"] = path
    return result


# ------------------------------------------------------------------ report

TITLES = [
    ("not_imported", "Not imported (CWA ignore list)"),
    ("unlogged_to_planned", "Yamtrack items with no log entry, imported as to read / to watch (owner answer)"),
    ("cwa_only_to_planned", "CWA books missing from Yamtrack, imported as to read (owner answer)"),
    ("superseded", "Older Yamtrack editions kept as extra editions of the logged book"),
    ("cwa_duplicates", "Several CWA records for one book"),
    ("authors_differ", "Yamtrack notes 'By:' differs from CWA's authors (CWA used; check for translators/typos)"),
    ("no_author", "No author anywhere (fill in the console or with the metadata fill)"),
    ("covers_missing", "No cover anywhere"),
    ("cover_download_failed", "Cover downloads that failed (fallback used)"),
    ("pages_differ", "Pages: your CWA/Excel number used; Open Library's edition has another (owner answer)"),
    ("date_parsed", "Publication dates parsed from free text"),
    ("invalid_ids", "Identifiers with an invalid format or check digit (not stored)"),
    ("ids_differ", "CWA's Open Library id differs from Yamtrack's (Yamtrack's kept)"),
    ("id_collision", "Identifiers already held by another edition (not stored twice)"),
    ("kosync_older", "Files whose kosync MD5 matches an older CWA checksum (CWA recorded a newer one)"),
    ("kosync_unknown", "Files whose kosync MD5 is not in CWA's history"),
    ("check", "Worth a look"),
    ("dropped_values", "Values dropped"),
    ("kept_owner_value", "Values the owner had changed: kept"),
    ("skipped_locked", "Locked items: changes skipped"),
    ("updated", "Updated in this run"),
    ("files_skipped", "Files skipped"),
]


def _cell(v):
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ")


def write_report(result, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = os.path.join(out_dir, "import-{}-{}".format(stamp, result["mode"]))
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False, default=str)
    L = []
    state = "COMMITTED" if result.get("committed") else ("FAILED, rolled back" if result.get("failed")
                                                         else "dry run, rolled back")
    L += ["# Import report: {}".format(state), "",
          "Run {} · mode {} · {} · snapshot `{}`".format(result.get("run_id"), result["mode"], result["started"],
                                                       result["snapshot"]), ""]
    L += ["## Checks", "", "| | Check | Detail |", "|---|---|---|"]
    for c in result.get("checks", []):
        L.append("| {} | {} | {} |".format("✅" if c["ok"] else "❌", c["name"], _cell(c["detail"])))
        for prob in c["problems"][:50]:
            L.append("| | ↳ | {} |".format(_cell(prob)))
    L += ["", "## Totals in the database", "", "| | count |", "|---|---|"]
    for k, v in (result.get("totals") or {}).items():
        L.append("| {} | {} |".format(k.replace("  ", "· "), v))
    L += ["", "## Sources", "", "| File | sha256 | bytes |", "|---|---|---|"]
    for k, v in result["sources"].items():
        L.append("| `{}` | `{}` | {} |".format(k, v["sha256"][:16], v["bytes"]))
    rep = result["report"]
    for key, title in TITLES:
        items = rep.get(key) or []
        if not items:
            continue
        L += ["", "## {} ({})".format(title, len(items)), ""]
        cols = list(dict.fromkeys(k for it in items for k in it))
        L += ["| " + " | ".join(c.rstrip("_") for c in cols) + " |", "|" + "---|" * len(cols)]
        for it in items:
            L.append("| " + " | ".join(_cell(it.get(c, "")) for c in cols) + " |")
    L += ["", "## Field rules (completeness)", "", "| Source | Field | Rule | Target / reason | Non-empty |",
          "|---|---|---|---|---|"]
    for f in result["fields"]:
        L.append("| {} | {} | {} | {} | {} |".format(f["source"], f["field"], f["rule"], _cell(f["target"]),
                                                     "" if f["non_empty"] is None else f["non_empty"]))
    L += ["", "## Write counts", "", "| table action | n |", "|---|---|"]
    for k, v in (result.get("counts") or {}).items():
        L.append("| {} | {} |".format(k, v))
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return base + ".md"
