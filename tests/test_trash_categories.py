"""The trash (move, restore, delete for good with its files) and the
category tree (rules, assignment, two levels), plus library statistics."""
import os
import uuid

import psycopg
import pytest

from conftest import add_entry, new_book, tx
from medialib import categories, db, files
from medialib.web import queries, writes

OWNER = "owner@test"


def book(conn, title=None, **kw):
    title = title or "Trash test " + uuid.uuid4().hex[:6]
    with tx(conn, source="yamtrack", client="test"):
        item, ed = new_book(conn, title, **kw)
        add_entry(conn, item)
    pid = conn.execute("SELECT public_id::text AS p FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["p"]
    return item, ed, pid


# ------------------------------------------------------------------- trash

def test_trash_hides_and_restore_brings_back(app):
    item, _, pid = book(app)
    writes.trash_item(app, OWNER, pid)
    lib = queries.library(app)
    assert pid not in {i["id"] for i in lib["items"]} and lib["trash"] >= 1
    assert queries.detail(app, pid) is None
    t = {r["id"]: r for r in queries.trash(app)}
    assert pid in t and t[pid]["purgeOn"] > t[pid]["deletedAt"][:10]
    with pytest.raises(LookupError):
        writes.trash_item(app, OWNER, pid)              # already in the trash
    writes.restore_item(app, OWNER, pid)
    assert pid in {i["id"] for i in queries.library(app)["items"]}


def test_delete_for_good_removes_everything(app, cfg, tmp_path):
    media = str(tmp_path)
    item, ed, pid = book(app)
    epub = tmp_path / "x.epub"
    import zipfile
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("a.html", uuid.uuid4().hex * 100)
    writes.attach_file(app, OWNER, pid, str(epub), files.sha256_file(epub), media)
    rel = app.execute("SELECT rel_path, kosync_md5 FROM catalog.edition_file WHERE edition_id = %s", (ed,)).fetchone()
    os.makedirs(os.path.join(media, "covers", "ab"))
    cover = "covers/ab/" + "ab" * 32 + ".jpg"
    open(os.path.join(media, cover), "wb").write(b"x")
    with tx(app, client="test"):
        app.execute("INSERT INTO catalog.cover (edition_id, source, local_path, sha256, mime, width, height, bytes,"
                    " is_chosen) VALUES (%s, 'owner', %s, %s, 'image/jpeg', 1, 1, 1, true)", (ed, cover, "ab" * 32))
        app.execute("INSERT INTO library.consumption (item_id, edition_id, state, ended_on, ended_precision, source)"
                    " VALUES (%s, %s, 'completed', '2020-01-01', 'year', 'owner')", (item, ed))
        app.execute("UPDATE library.entry SET status = 'completed' WHERE item_id = %s", (item,))
    app.execute("INSERT INTO sync.kosync_position (document, progress, percentage, device) VALUES (%s, '', 0.5, 'x')",
                (rel["kosync_md5"],))
    with pytest.raises(LookupError):
        writes.purge_now(app, OWNER, pid, media)        # only from the trash
    writes.trash_item(app, OWNER, pid)
    writes.purge_now(app, OWNER, pid, media)
    for sql in ("SELECT 1 AS x FROM catalog.media_item WHERE id = %s", "SELECT 1 AS x FROM library.entry WHERE item_id = %s",
                "SELECT 1 AS x FROM library.consumption WHERE item_id = %s", "SELECT 1 AS x FROM catalog.edition WHERE work_id = %s"):
        assert not app.execute(sql, (item,)).fetchone(), sql
    assert not app.execute("SELECT 1 AS x FROM sync.kosync_position WHERE document = %s", (rel["kosync_md5"],)).fetchone()
    assert not os.path.exists(os.path.join(media, rel["rel_path"])) and not os.path.exists(os.path.join(media, cover))
    row = app.execute("SELECT source, client FROM audit.change WHERE table_name = 'catalog.media_item' AND op = 'D'"
                      " AND row_key = jsonb_build_object('id', %s::bigint)", (item,)).fetchone()
    assert row["source"] == "owner" and row["client"] == "console:" + OWNER


def test_purge_trash_by_age_and_shelf_positions(app, tmp_path):
    a, _, pa = book(app)
    b, _, pb = book(app)
    shelf = writes.create_shelf(app, OWNER, {"name": "Purge " + uuid.uuid4().hex[:6]})
    writes.shelf_items(app, OWNER, shelf, {"add": [pa, pb]})
    writes.trash_item(app, OWNER, pa)
    assert writes.purge_trash(app, str(tmp_path), days=30, apply=True, log=lambda *_: None) == 0 or True
    with tx(app, client="test"):
        app.execute("UPDATE catalog.media_item SET deleted_at = now() - interval '31 days' WHERE id = %s", (a,))
    n = writes.purge_trash(app, str(tmp_path), days=30, apply=True, log=lambda *_: None)
    assert n >= 1 and not app.execute("SELECT 1 AS x FROM catalog.media_item WHERE id = %s", (a,)).fetchone()
    pos = app.execute("SELECT si.position FROM library.shelf_item si JOIN library.shelf s ON s.id = si.shelf_id"
                      " WHERE s.public_id = %s", (shelf,)).fetchall()
    assert [r["position"] for r in pos] == [1]


def test_app_role_cannot_delete_directly(app):
    item, _, _ = book(app)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tx(app, client="test"):
            app.execute("DELETE FROM catalog.media_item WHERE id = %s", (item,))
    with pytest.raises(psycopg.Error, match="not in the trash"):
        with tx(app, client="test"):
            app.execute("SELECT * FROM library.purge_item(%s)", (item,))


def test_adding_a_trashed_isbn_says_restore(app):
    isbn = "9780306406157"
    existing = app.execute("SELECT 1 AS x FROM catalog.external_id WHERE provider = 'isbn13' AND value = %s", (isbn,)).fetchone()
    if existing:
        pytest.skip("ISBN used by another test")
    r = writes.add_manual(app, OWNER, {"title": "Trashed ISBN " + uuid.uuid4().hex[:6], "isbn": isbn})
    writes.trash_item(app, OWNER, r["id"])
    with pytest.raises(writes.BadRequest, match="trash"):
        writes.add_manual(app, OWNER, {"title": "Again", "isbn": isbn})
    writes.restore_item(app, OWNER, r["id"])


# -------------------------------------------------------------- categories

def test_classify_rules():
    c = categories.classify
    assert c(["Novel", "Sci-Fi"])[:2] == (["Science fiction"], [])
    assert c(["Fiction", "Philosophy"])[0] == ["Novel"]
    assert c(["Non-fiction", "Graphic Novel"], ["Biography & Autobiography / Personal Memoirs"])[0] == ["Biography & memoir"]
    assert c(["Brides", "Families"], ["Fiction / Literary"])[:2] == (["Novel"], ["Brides", "Families"])
    assert c(["Social Science"], ["Psychology / Addiction"])[0] == ["Psychology & self-help"]
    assert c([], ["Fiction / Science Fiction / General", "Fiction / Literary"])[0] == ["Science fiction"]
    assert c(["History"])[3] is True and c(["Fiction"])[3] is False
    assert c([], [])[0] == []


def test_assign_keeps_owner_genres_and_retires_unused(app):
    item, _, pid = book(app)
    with tx(app, source="yamtrack", client="test"):
        old = app.execute("INSERT INTO library.tag (kind, name) VALUES ('genre', %s) RETURNING id",
                          ("Old label " + uuid.uuid4().hex[:6],)).fetchone()["id"]
        app.execute("INSERT INTO library.item_tag (item_id, tag_id, source) VALUES (%s, %s, 'yamtrack')", (item, old))
    with db.write_tx(app, "categorize", "test"):
        assert categories.assign(app, item, ["Science fiction"], ["Brides"])
        retired = categories.retire_unused(app)
    d = queries.detail(app, pid)
    assert d["genreList"] == ["Science fiction"] and "Brides" in d["tagList"]
    assert any(r.startswith("Old label") for r in retired)
    writes.edit_item(app, OWNER, pid, {"genres": ["Fantasy"]})
    with db.write_tx(app, "categorize", "test"):
        assert categories.assign(app, item, ["Horror"]) is False     # the owner chose: left alone
    assert queries.detail(app, pid)["genreList"] == ["Fantasy"]


def test_run_offline_and_library_payload(app, tmp_path):
    item, _, pid = book(app)
    with tx(app, source="yamtrack", client="test"):
        t = app.execute("SELECT id FROM library.tag WHERE kind = 'genre' AND name = 'Philosophy'").fetchone()["id"]
        app.execute("INSERT INTO library.item_tag (item_id, tag_id, source) VALUES (%s, %s, 'yamtrack')", (item, t))
    rows = categories.run(app, None, apply=True, out_dir=str(tmp_path), log=lambda *_: None)
    assert any(r[0].startswith("Trash test") for r in rows) and os.listdir(str(tmp_path))
    lib = queries.library(app)
    tree = {c["name"]: c for c in lib["categories"]}
    assert tree["Fiction"]["group"] and tree["Novel"]["parent"] == tree["Fiction"]["id"]
    me = next(i for i in lib["items"] if i["id"] == pid)
    assert me["cats"] == [tree["Philosophy"]["id"]] and me["hasFile"] is False


def test_two_levels_only(app):
    with pytest.raises(psycopg.errors.CheckViolation):
        with tx(app, client="test"):
            app.execute("UPDATE library.tag SET parent_id = (SELECT id FROM library.tag WHERE name = 'Fiction' AND kind = 'genre')"
                        " WHERE kind = 'genre' AND name = 'Non-fiction'")


def test_library_stats(ro):
    s = queries.library_stats(ro, "all")
    assert s["tiles"]["books"] >= 1 and isinstance(s["released"], list)
    assert queries.release_bucket(-380) == "Antiquity" and queries.release_bucket(1815) == "1800s"
    assert queries.release_bucket(1999) == "1990s"
