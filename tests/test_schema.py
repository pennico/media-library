"""Database rules: every constraint, trigger and privilege in DESIGN.md §5.1
has a test that a bad write fails (and a good one passes)."""
import shutil
import uuid

import psycopg
import pytest
from psycopg import errors

from conftest import add_entry, new_book, tx
from medialib import db, migrate


class sqlstate:
    """pytest.raises for our own SQLSTATEs (ML001 missing source, ML002 protected)."""

    def __init__(self, code, match=None):
        self.code, self.match = code, match

    def __enter__(self):
        return self

    def __exit__(self, typ, exc, tb):
        if exc is None:
            pytest.fail("DID NOT RAISE {}".format(self.code))
        if not isinstance(exc, psycopg.Error) or exc.sqlstate != self.code:
            return False
        if self.match and self.match not in str(exc):
            pytest.fail("{} raised without {!r}: {}".format(self.code, self.match, exc))
        return True


def uniq(prefix="x"):
    return "{} {}".format(prefix, uuid.uuid4().hex[:8])


# ------------------------------------------------------------ migrations

def test_migrate_again_is_a_no_op(cfg):
    assert migrate.migrate(cfg, log=lambda *_: None) == []


def test_changed_migration_is_refused(cfg, tmp_path):
    for f in migrate.files():
        shutil.copy("{}/{:04d}_{}.sql".format(migrate.MIGRATIONS_DIR, f["version"], f["name"]), tmp_path)
    first = sorted(tmp_path.iterdir())[0]
    first.write_text(first.read_text() + "\n-- edited\n")
    with pytest.raises(migrate.MigrationError, match="checksum changed"):
        migrate.migrate(cfg, directory=str(tmp_path), log=lambda *_: None)


def test_failed_migration_leaves_nothing(cfg, tmp_path):
    for f in migrate.files():
        shutil.copy("{}/{:04d}_{}.sql".format(migrate.MIGRATIONS_DIR, f["version"], f["name"]), tmp_path)
    (tmp_path / "9999_broken.sql").write_text(
        "CREATE TABLE catalog.half_done (id int);\nSELECT * FROM no_such_table;\n")
    with pytest.raises(migrate.MigrationError, match="nothing of it was applied"):
        migrate.migrate(cfg, directory=str(tmp_path), log=lambda *_: None)
    with db.connect(cfg, db.ROLE_APP) as conn:
        assert conn.execute("SELECT to_regclass('catalog.half_done') AS t").fetchone()["t"] is None


def test_database_settings(su):
    assert su.execute("SHOW data_checksums").fetchone()["data_checksums"] == "on"
    row = su.execute("SELECT datlocprovider, datlocale FROM pg_database WHERE datname = current_database()").fetchone()
    assert (row["datlocprovider"], row["datlocale"]) == ("b", "C.UTF-8")


# ------------------------------------------------------- source and client

def test_write_without_source_is_refused(app):
    with sqlstate("ML001"):
        with app.transaction():
            app.execute("SELECT set_config('ml.client', 'test', true)")
            new_book(app)


def test_write_without_client_is_refused(app):
    with sqlstate("ML001"):
        with app.transaction():
            app.execute("SELECT set_config('ml.source', 'owner', true)")
            new_book(app)


def test_unknown_source_is_refused(app):
    with sqlstate("ML001"):
        with tx(app, source="goodreads_scraper"):
            new_book(app)


# --------------------------------------------------------------- catalog

def test_book_needs_exactly_one_primary_edition(app):
    with pytest.raises(errors.IntegrityConstraintViolation, match="primary edition"):
        with tx(app):
            i = app.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('book', 'No edition')"
                            " RETURNING id").fetchone()["id"]
            app.execute("INSERT INTO catalog.book (item_id) VALUES (%s)", (i,))
    with pytest.raises(errors.UniqueViolation):
        with tx(app):
            item, _ = new_book(app)
            app.execute("INSERT INTO catalog.edition (work_id, title, is_primary) VALUES (%s, 'second', true)", (item,))
    with tx(app):
        item, ed = new_book(app)
        app.execute("INSERT INTO catalog.edition (work_id, title) VALUES (%s, 'another edition')", (item,))


def test_item_needs_its_subtype_row(app):
    with pytest.raises(errors.IntegrityConstraintViolation, match="has no movie row"):
        with tx(app):
            app.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('movie', 'No subtype')")


def test_subtype_must_match_kind(app):
    with pytest.raises(errors.ForeignKeyViolation):
        with tx(app):
            item, _ = new_book(app)
            app.execute("INSERT INTO catalog.movie (item_id) VALUES (%s)", (item,))


def test_kind_cannot_change(app):
    with tx(app):
        item, _ = new_book(app)
    with pytest.raises(errors.CheckViolation, match="cannot change kind"):
        with tx(app):
            app.execute("UPDATE catalog.media_item SET kind = 'movie' WHERE id = %s", (item,))


def test_movie_and_series(app):
    with tx(app):
        m = app.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('movie', 'Perfect Days') RETURNING id").fetchone()["id"]
        app.execute("INSERT INTO catalog.movie (item_id, runtime_min) VALUES (%s, 124)", (m,))
        s = app.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('series', 'Severance') RETURNING id").fetchone()["id"]
        app.execute("INSERT INTO catalog.series (item_id, status) VALUES (%s, 'returning')", (s,))
        add_entry(app, m)
        add_entry(app, s)
        se = app.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('season', 'Season 1') RETURNING id").fetchone()["id"]
        app.execute("INSERT INTO catalog.season (item_id, series_id, season_number) VALUES (%s, %s, 1)", (se, s))
    with pytest.raises(errors.CheckViolation, match="cannot be a library entry"):
        with tx(app):
            add_entry(app, se)


@pytest.mark.parametrize("d,p,ok", [
    ("2019-01-01", "year", True), ("2019-12-31", "year", False),
    ("2019-03-01", "month", True), ("2019-03-02", "month", False),
    ("2019-03-02", "day", True), ("2019-03-02", None, False), ("0385-01-01 BC", "year", True),
])
def test_partial_dates(app, d, p, ok):
    def run():
        with tx(app):
            item, _ = new_book(app)
            app.execute("UPDATE catalog.media_item SET first_published = %s, first_published_precision = %s"
                        " WHERE id = %s", (d, p, item))
    if ok:
        run()
    else:
        with pytest.raises(errors.CheckViolation):
            run()


@pytest.mark.parametrize("provider,value,level,ok", [
    ("isbn13", "9780330544078", "edition", True),
    ("isbn13", "9780330544079", "edition", False),     # check digit
    ("isbn13", "0330544071", "edition", False),        # ISBN-10 is converted before storing
    ("openlibrary_edition", "OL37746960M", "edition", True),
    ("openlibrary_edition", "OL37746960W", "edition", False),
    ("openlibrary_work", "OL123W", "edition", False),  # works hang off the item
    ("cwa_book", "100", "edition", True),
    ("tmdb_movie", "976893", "edition", False),
])
def test_external_ids(app, provider, value, level, ok):
    def run():
        with tx(app):
            item, ed = new_book(app)
            app.execute("DELETE FROM catalog.external_id WHERE provider = %s AND value = %s", (provider, value))
            app.execute("INSERT INTO catalog.external_id (provider, value, {}) VALUES (%s, %s, %s)".format(
                "edition_id" if level == "edition" else "item_id"), (provider, value, ed if level == "edition" else item))
    if ok:
        run()
    else:
        with pytest.raises(errors.CheckViolation):
            run()


def test_external_id_is_unique(app):
    value = "OL{}M".format(uuid.uuid4().int % 10**9)
    with pytest.raises(errors.UniqueViolation):
        with tx(app):
            _, e1 = new_book(app)
            _, e2 = new_book(app)
            app.execute("INSERT INTO catalog.external_id (provider, value, edition_id) VALUES ('openlibrary_edition', %s, %s)", (value, e1))
            app.execute("INSERT INTO catalog.external_id (provider, value, edition_id) VALUES ('openlibrary_edition', %s, %s)", (value, e2))


def test_person_names_dedupe(app):
    name = uniq("José Saramägo")
    with pytest.raises(errors.UniqueViolation):
        with tx(app):
            app.execute("INSERT INTO catalog.person (name) VALUES (%s)", (name,))
            app.execute("INSERT INTO catalog.person (name) VALUES (%s)", (name.replace("é", "e").replace("ä", "a"),))
    first = "James" + uuid.uuid4().hex[:6]
    with tx(app):
        p = app.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING sort_name",
                        (first + " Tiptree Jr.",)).fetchone()
    assert p["sort_name"] == "Tiptree, {} Jr.".format(first)


def test_contribution_positions_reorder(app):
    with tx(app):
        item, _ = new_book(app)
        a = app.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id", (uniq("Author A"),)).fetchone()["id"]
        b = app.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id", (uniq("Author B"),)).fetchone()["id"]
        app.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position) VALUES (%s, 'author', %s, 1), (%s, 'author', %s, 2)",
                    (a, item, b, item))
    with tx(app):   # swap: valid at commit thanks to the deferrable constraint
        app.execute("UPDATE catalog.contribution SET position = 3 - position WHERE item_id = %s", (item,))
    with pytest.raises(errors.CheckViolation):
        with tx(app):
            app.execute("INSERT INTO catalog.contribution (person_id, role, item_id, edition_id) VALUES (%s, 'author', NULL, NULL)", (a,))


def test_cover_rules(app):
    with pytest.raises(errors.CheckViolation, match="cover_chosen_is_local"):
        with tx(app):
            _, ed = new_book(app)
            app.execute("INSERT INTO catalog.cover (edition_id, source, source_url, is_chosen) VALUES (%s, 'apple', 'https://x.test/a.jpg', true)", (ed,))
    sha = "{:064x}".format(uuid.uuid4().int)
    with pytest.raises(errors.UniqueViolation):
        with tx(app):
            _, ed = new_book(app)
            for n in (1, 2):
                app.execute("INSERT INTO catalog.cover (edition_id, source, local_path, sha256, mime, is_chosen)"
                            " VALUES (%s, 'owner', %s, %s, 'image/jpeg', true)",
                            (ed, "covers/{}.jpg".format(n), sha[:-1] + str(n)))


@pytest.mark.parametrize("path", ["/etc/passwd", "a/../../b", "..", ""])
def test_file_paths_are_relative(app, path):
    with pytest.raises(errors.CheckViolation):
        with tx(app):
            _, ed = new_book(app)
            app.execute("INSERT INTO catalog.edition_file (edition_id, format, storage, rel_path, size_bytes, sha256)"
                        " VALUES (%s, 'epub', 'cwa', %s, 1, repeat('a', 64))", (ed, path))


# --------------------------------------------------------------- library

def test_rating_range(app):
    with pytest.raises(errors.CheckViolation):
        with tx(app):
            item, _ = new_book(app)
            app.execute("INSERT INTO library.entry (item_id, rating, rated_at) VALUES (%s, 10.5, now())", (item,))
    with tx(app):
        item, _ = new_book(app)
        app.execute("INSERT INTO library.entry (item_id, rating, rated_at) VALUES (%s, 7.5, now())", (item,))
    assert app.execute("SELECT status FROM library.entry WHERE item_id = %s", (item,)).fetchone()["status"] == "planned"


def test_status_and_readings_agree(app):
    # completed needs a completed reading
    with pytest.raises(errors.IntegrityConstraintViolation, match="needs a completed reading"):
        with tx(app):
            item, _ = new_book(app)
            add_entry(app, item, "completed")
    # reading needs an open reading
    with pytest.raises(errors.IntegrityConstraintViolation, match="exactly one open reading"):
        with tx(app):
            item, _ = new_book(app)
            add_entry(app, item, "in_progress")
    # planned with an open reading
    with pytest.raises(errors.IntegrityConstraintViolation, match="still open"):
        with tx(app):
            item, _ = new_book(app)
            add_entry(app, item, "planned")
            app.execute("INSERT INTO library.consumption (item_id, state, source) VALUES (%s, 'in_progress', 'owner')", (item,))
    # a correct life cycle: start, finish, re-read later
    with tx(app):
        item, ed = new_book(app)
        add_entry(app, item, "in_progress")
        app.execute("INSERT INTO library.consumption (item_id, edition_id, state, started_on, started_precision, source)"
                    " VALUES (%s, %s, 'in_progress', '2026-10-01', 'day', 'owner')", (item, ed))
    with tx(app):
        app.execute("UPDATE library.consumption SET state = 'completed', ended_on = '2026-10-20', ended_precision = 'day'"
                    " WHERE item_id = %s", (item,))
        app.execute("UPDATE library.entry SET status = 'completed' WHERE item_id = %s", (item,))
    with tx(app):
        app.execute("UPDATE library.entry SET status = 'planned' WHERE item_id = %s", (item,))  # want to read again


def test_reading_rules(app):
    with pytest.raises(errors.CheckViolation, match="open_has_no_end"):
        with tx(app):
            item, _ = new_book(app)
            add_entry(app, item, "in_progress")
            app.execute("INSERT INTO library.consumption (item_id, state, ended_on, ended_precision, source)"
                        " VALUES (%s, 'in_progress', '2026-01-01', 'day', 'owner')", (item,))
    with pytest.raises(errors.UniqueViolation):
        with tx(app):
            item, _ = new_book(app)
            add_entry(app, item, "in_progress")
            for _ in range(2):
                app.execute("INSERT INTO library.consumption (item_id, state, source) VALUES (%s, 'in_progress', 'owner')", (item,))
    with pytest.raises(errors.CheckViolation, match="dates_ordered"):
        with tx(app):
            item, _ = new_book(app)
            add_entry(app, item, "completed")
            app.execute("INSERT INTO library.consumption (item_id, state, started_on, started_precision, ended_on, ended_precision, source)"
                        " VALUES (%s, 'completed', '2026-02-01', 'day', '2026-01-01', 'day', 'owner')", (item,))
    with pytest.raises(errors.ForeignKeyViolation):
        with tx(app):   # the edition of another book
            item, _ = new_book(app)
            _, other_ed = new_book(app)
            add_entry(app, item, "completed")
            app.execute("INSERT INTO library.consumption (item_id, edition_id, state, source) VALUES (%s, %s, 'completed', 'owner')",
                        (item, other_ed))


def test_shelf_drag_reorder(app):
    with tx(app):
        shelf = app.execute("INSERT INTO library.shelf (name, kind, media_kind, position)"
                            " VALUES (%s, 'manual', 'book', (SELECT max(position) + 1 FROM library.shelf WHERE media_kind = 'book'))"
                            " RETURNING id", (uniq("Shelf"),)).fetchone()["id"]
        items = []
        for n in range(1, 6):
            item, _ = new_book(app, "Book {}".format(n))
            add_entry(app, item)
            app.execute("INSERT INTO library.shelf_item (shelf_id, item_id, position) VALUES (%s, %s, %s)", (shelf, item, n))
            items.append(item)
    # move the 5th to position 2: shift 2..4 down by one, place the moved one
    with tx(app):
        app.execute("UPDATE library.shelf_item SET position = position + 1 WHERE shelf_id = %s AND position BETWEEN 2 AND 4", (shelf,))
        app.execute("UPDATE library.shelf_item SET position = 2 WHERE shelf_id = %s AND item_id = %s", (shelf, items[4]))
    order = [r["item_id"] for r in app.execute(
        "SELECT item_id FROM library.shelf_item WHERE shelf_id = %s ORDER BY position", (shelf,))]
    assert order == [items[0], items[4], items[1], items[2], items[3]]
    with pytest.raises(errors.UniqueViolation):
        with tx(app):
            app.execute("UPDATE library.shelf_item SET position = 1 WHERE shelf_id = %s", (shelf,))
    # every entry is also on the built-in Books shelf
    n = app.execute("SELECT count(*) AS n FROM library.shelf_item si JOIN library.shelf s ON s.id = si.shelf_id"
                    " WHERE s.kind = 'all' AND si.item_id = ANY(%s)", (items,)).fetchone()["n"]
    assert n == 5


def test_shelf_holds_one_kind(app):
    with pytest.raises(errors.CheckViolation, match="wrong media kind"):
        with tx(app):
            item, _ = new_book(app)
            add_entry(app, item)
            movies = app.execute("SELECT id FROM library.shelf WHERE kind = 'all' AND media_kind = 'movie'").fetchone()["id"]
            app.execute("INSERT INTO library.shelf_item (shelf_id, item_id, position) VALUES (%s, %s, 99999)", (movies, item))


# ------------------------------------------------- provenance protection

def test_owner_values_are_protected(app):
    with tx(app, source="yamtrack"):
        item, ed = new_book(app, "Imported title")
    with tx(app, source="owner"):
        app.execute("UPDATE catalog.media_item SET title = 'My title' WHERE id = %s", (item,))
    prov = app.execute("SELECT provenance FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["provenance"]
    assert prov["title"]["src"] == "owner" and prov["_row"]["src"] == "yamtrack"
    assert prov["sort_title"]["src"] == "derived"
    with sqlstate("ML002", "set by the owner"):
        with tx(app, source="apple"):
            app.execute("UPDATE catalog.media_item SET title = 'Apple title' WHERE id = %s", (item,))
    with tx(app, source="apple"):   # other fields can still be filled
        app.execute("UPDATE catalog.media_item SET description = 'From Apple' WHERE id = %s", (item,))
    prov = app.execute("SELECT provenance FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["provenance"]
    assert prov["description"]["src"] == "apple"


def test_owner_cleared_value_stays_cleared(app):
    with tx(app, source="openlibrary"):
        item, ed = new_book(app)
        app.execute("UPDATE catalog.edition SET pages = 300 WHERE id = %s", (ed,))
    with tx(app, source="owner"):
        app.execute("UPDATE catalog.edition SET pages = NULL WHERE id = %s", (ed,))
    with sqlstate("ML002"):
        with tx(app, source="openlibrary"):
            app.execute("UPDATE catalog.edition SET pages = 320 WHERE id = %s", (ed,))


def test_owner_label_cannot_be_forged(app):
    with sqlstate("ML002", "only the owner can label"):
        with tx(app, source="apple"):
            app.execute("""INSERT INTO catalog.media_item (kind, title, provenance)
                           VALUES ('book', 'x', '{"title": {"src": "owner"}}')""")


def test_owner_rows_cannot_be_deleted_by_automation(app):
    with tx(app, source="owner"):
        item, ed = new_book(app)
        app.execute("INSERT INTO catalog.external_id (provider, value, edition_id) VALUES ('cwa_book', %s, %s)",
                    (str(uuid.uuid4().int % 10**9), ed))
    with sqlstate("ML002"):
        with tx(app, source="cwa"):
            app.execute("UPDATE catalog.media_item SET deleted_at = now() WHERE id = %s", (item,))
    with sqlstate("ML002"):
        with tx(app, source="cwa"):
            app.execute("DELETE FROM catalog.external_id WHERE edition_id = %s", (ed,))


def test_locked_items(app):
    with tx(app, source="cwa"):
        item, ed = new_book(app, "Storia dell'assedio di Lisbona")
        app.execute("UPDATE catalog.media_item SET locked = true WHERE id = %s", (item,))
    for stmt in ("UPDATE catalog.media_item SET title = 'x' WHERE id = %(i)s",
                 "UPDATE catalog.edition SET pages = 10 WHERE id = %(e)s",
                 "INSERT INTO catalog.cover (edition_id, source, source_url) VALUES (%(e)s, 'apple', 'https://x.test/c.jpg')"):
        with sqlstate("ML002", "locked"):
            with tx(app, source="apple"):
                app.execute(stmt, {"i": item, "e": ed})
    with tx(app, source="owner"):
        app.execute("UPDATE catalog.edition SET pages = 338 WHERE id = %s", (ed,))


def test_unknown_provenance_key_is_refused(app):
    with pytest.raises(errors.CheckViolation, match="not a tracked column"):
        with tx(app):
            app.execute("""INSERT INTO catalog.media_item (kind, title, provenance)
                           VALUES ('book', 'x', '{"colour": {"src": "owner"}}')""")


# ------------------------------------------------------ sorting and search

def test_sort_title_strips_articles(app):
    cases = [("The Silk Roads", "eng", "Silk Roads"), ("L'amico ritrovato", "ita", "amico ritrovato"),
             ("Il Castello", "ita", "Castello"), ("Den grønne enke", "dan", "grønne enke"),
             ("I corpi", "ita", "corpi"), ("1984", "eng", "1984"), ("The", "eng", "The")]
    with tx(app):
        for title, lang, want in cases:
            item, _ = new_book(app, title, lang)
            got = app.execute("SELECT sort_title FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["sort_title"]
            assert got == want, title
        app.execute("UPDATE catalog.media_item SET sort_title = 'Corpi, I' WHERE id = %s", (item,))
        app.execute("UPDATE catalog.media_item SET title = 'I corpi (2)' WHERE id = %s", (item,))
        # the owner's sort title survives a title change
        assert app.execute("SELECT sort_title FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["sort_title"] == "Corpi, I"


def test_collations(app):
    words = ["Lys", "Løgner", "Lukas", "Volume 10", "Volume 2", "Æble", "Zebra"]
    q = "SELECT w FROM unnest(%s::text[]) AS w ORDER BY w COLLATE catalog.{}"
    da = [r["w"] for r in app.execute(q.format("sort_da"), (words,))]
    und = [r["w"] for r in app.execute(q.format("sort_und"), (words,))]
    assert da.index("Løgner") > da.index("Lys") and da.index("Æble") > da.index("Zebra")   # Danish: ø, æ after z
    assert und.index("Løgner") < und.index("Lukas") and und.index("Æble") < und.index("Lys")
    assert da.index("Volume 2") < da.index("Volume 10")                                       # numeric


def test_search_is_accent_insensitive(app):
    with tx(app):
        new_book(app, "Løgner", "dan")
        p = uniq("Camilla Läckberg")
    found = app.execute("SELECT title FROM catalog.media_item WHERE lower(catalog.f_unaccent(title)) LIKE '%logner%'").fetchall()
    assert any(r["title"] == "Løgner" for r in found)
    sim = app.execute("SELECT ext.similarity(lower(catalog.f_unaccent(%s)), 'camilla lackberg') AS s", (p,)).fetchone()["s"]
    assert sim > 0.5
    n = app.execute("SELECT count(*) AS n FROM library.entry WHERE to_tsvector('catalog.ml_search', coalesce(review, ''))"
                    " @@ plainto_tsquery('catalog.ml_search', 'citta')").fetchone()["n"]
    assert n >= 0


# ------------------------------------------------------------------ audit

def test_audit_records_changes(app):
    with tx(app, source="owner", client="console:test"):
        item, _ = new_book(app, "Audited")
        add_entry(app, item)
    with tx(app, source="owner", client="console:test"):
        app.execute("UPDATE library.entry SET rating = 8, rated_at = now() WHERE item_id = %s", (item,))
    rows = app.execute("SELECT op, new_values, old_row, client, source FROM audit.change"
                       " WHERE table_name = 'library.entry' AND row_key = jsonb_build_object('item_id', %s::bigint)"
                       " ORDER BY id", (item,)).fetchall()
    assert [r["op"] for r in rows] == ["I", "U"]
    assert rows[1]["new_values"]["rating"] == 8 and rows[1]["old_row"]["rating"] is None
    assert rows[1]["client"] == "console:test" and rows[1]["source"] == "owner"
    assert "updated_at" not in rows[1]["new_values"]


def test_audit_is_append_only(app, cfg):
    for stmt in ("UPDATE audit.change SET client = 'x'", "DELETE FROM audit.change", "TRUNCATE audit.change",
                 "INSERT INTO audit.change (at, tx, table_name, row_key, op, client, db_role, hash)"
                 " VALUES (now(), 1, 't', '{}', 'I', 'x', 'x', '\\x00')"):
        with pytest.raises(errors.InsufficientPrivilege):
            with app.transaction():
                app.execute(stmt)
    with db.connect(cfg, db.ROLE_MIGRATOR) as owner:   # even the table owner
        owner.execute("SET ROLE ml_owner")
        with sqlstate("ML002"):
            with owner.transaction():
                owner.execute("DELETE FROM audit.change")


def test_audit_chain_detects_tampering(app, su):
    with tx(app):
        new_book(app, "Chain")
    assert app.execute("SELECT * FROM audit.verify()").fetchone()["first_bad"] is None
    victim = su.execute("SELECT max(id) - 1 AS id FROM audit.change").fetchone()["id"]
    su.execute("ALTER TABLE audit.change DISABLE TRIGGER append_only")
    try:
        su.execute("UPDATE audit.change SET client = 'forged' WHERE id = %s", (victim,))
        assert app.execute("SELECT * FROM audit.verify()").fetchone()["first_bad"] == victim
    finally:
        su.execute("UPDATE audit.change SET client = (SELECT client FROM audit.change WHERE id = %s + 1) WHERE id = %s",
                   (victim, victim))
        su.execute("ALTER TABLE audit.change ENABLE TRIGGER append_only")


def test_record_external_history(app):
    with tx(app, source="yamtrack", client="importer"):
        item, _ = new_book(app)
        app.execute("SELECT audit.record_external('library.entry', jsonb_build_object('item_id', %s::bigint),"
                    " '{\"rating\": 8}', '{\"rating\": 9}', '2026-10-19 17:11:24+00')", (item,))
    with pytest.raises(errors.CheckViolation):
        with tx(app, source="yamtrack", client="importer"):
            app.execute("SELECT audit.record_external('library.entry', '{}', '{}', '{}', now() + interval '1 day')")


# ------------------------------------------------------------------ roles

def test_app_role_limits(app):
    for stmt in ("INSERT INTO catalog.language (code, bcp47, name_en) VALUES ('xxx', 'xx', 'X')",
                 "DELETE FROM catalog.media_item",
                 "CREATE TABLE catalog.evil (id int)",
                 "TRUNCATE library.entry"):
        with pytest.raises(errors.InsufficientPrivilege):
            with tx(app):
                app.execute(stmt)


def test_readonly_role(ro):
    assert ro.execute("SELECT count(*) AS n FROM stats.library_counts").fetchone()["n"] >= 0
    for stmt in ("SELECT * FROM library.entry", "SELECT * FROM catalog.media_item", "SELECT * FROM audit.change"):
        with pytest.raises(errors.InsufficientPrivilege):
            with ro.transaction():
                ro.execute(stmt)
    with pytest.raises(errors.ReadOnlySqlTransaction):
        with ro.transaction():
            ro.execute("CREATE TEMP TABLE t (id int)")


def test_migrator_has_no_rights_without_set_role(cfg):
    with db.connect(cfg, db.ROLE_MIGRATOR) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM library.entry")
