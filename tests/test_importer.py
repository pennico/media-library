"""Importer: text helpers, the plan built from the real snapshot, and the
end-to-end import (dry run, apply, idempotent re-run, owner edits kept).

The snapshot tests need data/ (the 2026-10-24 snapshot, not in git) and the
cover cache from a previous run in media/ (copied, so the tests never touch
the network)."""
import datetime
import decimal
import os
import shutil

import pytest

from conftest import ROOT, tx
from medialib import db, files, text
from medialib.importer import plan, run, sources

SNAPSHOT = os.path.join(ROOT, "data")
MEDIA_CACHE = os.path.join(ROOT, "media")
needs_snapshot = pytest.mark.skipif(not os.path.exists(os.path.join(SNAPSHOT, "yamtrack.sqlite3")),
                                    reason="snapshot not present")


# ----------------------------------------------------------------- text

def test_parse_notes():
    n = text.parse_notes("By: José Saramago & R. Desti · Read in: Italian · Original language: Portouguese"
                         " · Genre: Novel · Pages: 201 — Slow start. Worth it in the end.")
    assert n["fields"] == {"By": "José Saramago & R. Desti", "Read in": "Italian",
                           "Original language": "Portouguese", "Genre": "Novel", "Pages": "201"}
    assert n["review"] == "Slow start. Worth it in the end."
    assert text.parse_notes("By: Susan Cain · Genre: Social Science · Pages: 300")["review"] is None
    assert text.parse_notes("just a review")["review"] == "just a review"
    assert text.parse_notes("<p>An amazing concept</p><p>Second</p>")["review"] == "An amazing concept\n\nSecond"


def test_language_codes():
    assert [text.language_code(x) for x in ("Italian", "Portouguese", "Portoguese", "dan", "fre", "Klingon")] == \
        ["ita", "por", "por", "dan", "fra", None]


def test_split_people_and_genres():
    assert text.split_people("Ken Blanchard| Spencer Johnson") == ["Ken Blanchard", "Spencer Johnson"]
    assert text.split_people("Nan Silver og John Gottman PhD") == ["Nan Silver", "John Gottman PhD"]
    assert text.split_people("Unknown") == []
    assert text.split_genres("Non-fiction, Psychology") == ["Non-fiction", "Psychology"]
    assert text.split_genres("Novel; Various") == ["Novel", "Various"]
    assert text.first_last("Lewis, Thomas") == "Thomas Lewis"


def test_isbn():
    assert text.normalize_isbn("0330544071") == "9780330544078"
    assert text.normalize_isbn("978-0-330-54407-8") == "9780330544078"
    assert text.normalize_isbn("9780330544079") is None
    assert text.normalize_isbn("b9472387-b001") is None


@pytest.mark.parametrize("raw,want", [
    ("2019", (datetime.date(2019, 1, 1), "year")),
    ("2019-05-07", (datetime.date(2019, 5, 7), "day")),
    ("2019-05", (datetime.date(2019, 5, 1), "month")),
    ("December, 1971", (datetime.date(1971, 12, 1), "month")),
    ("May 7, 2019", (datetime.date(2019, 5, 7), "day")),
    ("15/07/2022", (datetime.date(2022, 1, 1), "year")),
    ("", None),
])
def test_partial_dates(raw, want):
    assert text.parse_partial_date(raw) == want


def test_kosync_md5_offsets(tmp_path):
    """KOReader samples 1 KiB at 0, 1K, 4K, 16K, ... (LuaJIT: lshift(1024, -2) = 0)."""
    import hashlib
    data = bytes(range(256)) * 300            # 76.8 KB
    f = tmp_path / "book.epub"
    f.write_bytes(data)
    want = hashlib.md5()
    for off in (0, 1024, 4096, 16384, 65536):
        want.update(data[off:off + 1024])
    assert files.kosync_partial_md5(str(f)) == want.hexdigest()


@needs_snapshot
def test_kosync_md5_matches_cwa():
    """Real data: book 25's file matches CWA's latest checksum."""
    snap = sources.read(SNAPSHOT)
    r = snap.cwa[25]
    d = r["data"][0]
    path = os.path.join(SNAPSHOT, "calibre-library", r["path"], "{}.{}".format(d["name"], d["format"].lower()))
    assert files.kosync_partial_md5(path) == r["checksums"][-1]["checksum"]


# ----------------------------------------------------------------- plan

@pytest.fixture(scope="module")
def snapshot_plan():
    if not os.path.exists(os.path.join(SNAPSHOT, "yamtrack.sqlite3")):
        pytest.skip("snapshot not present")
    snap = sources.read(SNAPSHOT)
    return snap, plan.build(snap)


def test_plan_counts(snapshot_plan):
    snap, p = snapshot_plan
    works = p["works"]
    assert len(works) == 156
    assert sum(w["kind"] == "book" for w in works) == 154
    assert sum(len(w["editions"]) for w in works) == 163
    assert sum(len(w["readings"]) for w in works) == 91
    assert all(r["ended_precision"] == "year" for w in works for r in w["readings"])
    assert sum(len(e["files"]) for w in works for e in w["editions"]) == 18
    assert sum(w["entry"]["status"] == "planned" for w in works) == 65
    owner = [c for w in works for e in w["editions"] for c in e["covers"] if c["source"] == "owner"]
    assert len(owner) == len(snap.owner_covers) == 11


def test_plan_superseded_editions(snapshot_plan):
    _, p = snapshot_plan
    pairs = {r["item"]: r["now"] for r in p["report"].items["superseded"]}
    assert pairs[94] == "1984" and pairs[125] == "Che t'importa di quel che pensa la gente"
    assert len(pairs) == 9
    assert 5 not in pairs          # Severance (tv) is not an edition of Attached


def test_plan_authors_prefer_cwa(snapshot_plan):
    _, p = snapshot_plan
    by_title = {w["title"]: w for w in p["works"]}
    assert [a["name"] for a in by_title["Biondo Era e Bello"]["authors"]] == ["Mario Tobino"]
    assert [a["name"] for a in by_title["Il viaggio dell'elefante"]["authors"]] == ["José Saramago"]
    assert any(r["not_in_cwa"] == "R. Desti" for r in p["report"].items["authors_differ"])


def test_plan_locked_copy(snapshot_plan):
    _, p = snapshot_plan
    locked = [w for w in p["works"] if w["locked"]]
    assert [w["title"] for w in locked] == ["Storia dell'assedio di Lisbona (Italian Edition)"]
    assert locked[0]["entry"]["status"] == "planned"


# --------------------------------------------------------- end to end

@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not os.path.exists(os.path.join(MEDIA_CACHE, "cache", "fetch-index.json")):
        pytest.skip("no cover cache: run a dry run once (python -m medialib import data)")
    dest = tmp_path_factory.mktemp("media")
    shutil.copytree(MEDIA_CACHE, dest, dirs_exist_ok=True)
    return str(dest)


@pytest.fixture(scope="module")
def imported(cfg, media, tmp_path_factory):
    import dataclasses
    c = dataclasses.replace(cfg, media_dir=media)
    out = str(tmp_path_factory.mktemp("out"))
    with db.connect(c) as conn:
        before = conn.execute("SELECT count(*) AS n FROM catalog.media_item").fetchone()["n"]
    dry = run.run(c, SNAPSHOT, apply=False, offline=True, out_dir=out, log=lambda *_: None)
    with db.connect(c) as conn:
        after_dry = conn.execute("SELECT count(*) AS n FROM catalog.media_item").fetchone()["n"]
    first = run.run(c, SNAPSHOT, apply=True, offline=True, out_dir=out, log=lambda *_: None)
    return {"cfg": c, "out": out, "dry": dry, "first": first, "before": before, "after_dry": after_dry}


@needs_snapshot
def test_dry_run_writes_nothing(imported):
    assert imported["dry"]["committed"] is False and not imported["dry"].get("failed")
    assert imported["after_dry"] == imported["before"]
    assert os.path.exists(imported["dry"]["report_path"])


@needs_snapshot
def test_apply_passes_all_checks(imported):
    first = imported["first"]
    assert first["committed"] is True
    failed = [c for c in first["checks"] if not c["ok"]]
    assert not failed, failed
    t = first["totals"]
    assert t["entries"] >= 156 and t["readings"] >= 91 and t["files"] >= 18


@needs_snapshot
def test_second_apply_changes_nothing(imported):
    c = imported["cfg"]
    with db.connect(c) as conn:
        audit_before = conn.execute("SELECT count(*) AS n FROM audit.change").fetchone()["n"]
    again = run.run(c, SNAPSHOT, apply=True, offline=True, out_dir=imported["out"], log=lambda *_: None)
    changed = {k: v for k, v in again["counts"].items() if k.endswith((" created", " updated"))}
    assert changed == {}
    with db.connect(c) as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit.change").fetchone()["n"] == audit_before


@needs_snapshot
def test_reimport_keeps_owner_edits(imported):
    c = imported["cfg"]
    with db.connect(c) as conn:
        item = conn.execute("SELECT i.id FROM catalog.media_item i JOIN catalog.external_id x ON x.edition_id IN"
                            " (SELECT id FROM catalog.edition WHERE work_id = i.id) WHERE x.provider = 'cwa_book'"
                            " AND x.value = '3'").fetchone()["id"]          # Attached
        with tx(conn, source="owner", client="console:test"):
            conn.execute("UPDATE library.entry SET rating = 7.5, rated_at = now() WHERE item_id = %s", (item,))
            conn.execute("UPDATE catalog.media_item SET title = 'Attached (owner title)' WHERE id = %s", (item,))
    again = run.run(c, SNAPSHOT, apply=True, offline=True, out_dir=imported["out"], log=lambda *_: None)
    assert all(ch["ok"] for ch in again["checks"])
    kept = {(k["table"], k["column"]) for k in again["report"]["kept_owner_value"]}
    assert ("library.entry", "rating") in kept and ("catalog.media_item", "title") in kept
    with db.connect(c) as conn:
        row = conn.execute("SELECT e.rating, i.title FROM library.entry e JOIN catalog.media_item i ON i.id = e.item_id"
                           " WHERE e.item_id = %s", (item,)).fetchone()
    assert row["rating"] == decimal.Decimal("7.5") and row["title"] == "Attached (owner title)"


@needs_snapshot
def test_locked_item_is_untouched(imported):
    c = imported["cfg"]
    with db.connect(c) as conn:
        item = conn.execute("SELECT id FROM catalog.media_item WHERE locked").fetchone()["id"]
        with pytest.raises(Exception, match="locked"):
            with tx(conn, source="openlibrary", client="test"):
                conn.execute("UPDATE catalog.edition SET pages = 1 WHERE work_id = %s", (item,))


@needs_snapshot
def test_reimport_leaves_deleted_books_deleted(imported):
    from medialib.web import writes
    c = imported["cfg"]
    with db.connect(c) as conn:
        def by_cwa(cid):
            return conn.execute("SELECT i.id, i.public_id::text AS pid FROM catalog.media_item i JOIN catalog.external_id x"
                                " ON x.edition_id IN (SELECT id FROM catalog.edition WHERE work_id = i.id)"
                                " WHERE x.provider = 'cwa_book' AND x.value = %s", (cid,)).fetchone()
        trashed, purged = by_cwa("13"), by_cwa("17")        # Summary of Sapiens, Your Erroneous Zones
        writes.trash_item(conn, "owner@test", trashed["pid"])
        writes.trash_item(conn, "owner@test", purged["pid"])
        writes.purge_item(conn, "test", purged["id"], c.media_dir)
    again = run.run(c, SNAPSHOT, apply=True, offline=True, out_dir=imported["out"], log=lambda *_: None)
    assert all(ch["ok"] for ch in again["checks"]), [ch for ch in again["checks"] if not ch["ok"]]
    states = sorted(r["state"] for r in again["report"]["deleted_by_owner"])
    assert states == ["deleted for good", "in the trash"]
    with db.connect(c) as conn:
        assert conn.execute("SELECT deleted_at FROM catalog.media_item WHERE id = %s", (trashed["id"],)).fetchone()["deleted_at"]
        assert not conn.execute("SELECT 1 AS x FROM catalog.media_item WHERE title LIKE 'Your Erroneous Zones%%'"
                                " AND deleted_at IS NULL").fetchone()
        writes.restore_item(conn, "owner@test", trashed["pid"])
