"""Metadata fill: matching rules, adding a book with per-provider provenance,
duplicates, and that filling never touches owner values. Offline: the
providers get recorded answers through a fake fetch."""
import uuid

import pytest

from conftest import add_entry, new_book, tx
from medialib import db
from medialib.fill import providers as P
from medialib.fill import service


def test_matching_rules():
    c = P.Candidate("apple", "Never Let Me Go", ["Kazuo Ishiguro"], [])
    assert P.score("Never let me go", ["Ishiguro"], c) == 1.0
    assert P.score("Never Let Me Go", ["Tolstoy"], c) == 0.0                     # author disagrees: never a guess
    summary = P.Candidate("apple", "Summary of Never Let Me Go", ["Kazuo Ishiguro"], [])
    assert P.score("Never Let Me Go", ["Ishiguro"], summary) == 0.0
    vol2 = P.Candidate("openlibrary", "On the Calculation of Volume II", ["Solvej Balle"], [])
    assert P.score("On the Calculation of Volume I", ["Balle"], vol2) == 0.0     # a different volume
    assert P.main_title("The Silk Roads: A New History") == "the silk roads"


def random_isbn():
    import random
    core = "978" + "".join(random.choice("0123456789") for _ in range(9))
    return core + str((10 - sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(core)) % 10) % 10)


def fake_fetch(store):
    uid = uuid.uuid4().hex[:6]
    isbn = random_isbn()
    tid = str(int(uuid.uuid4().int % 10**9))
    olid = "OL{}M".format(int(uuid.uuid4().int % 10**8))
    title = "Testbook " + uid

    def fetch(url):
        if url.startswith("https://itunes.apple.com/search"):
            return {"results": [
                {"trackId": int(tid), "trackName": title, "artistName": "Ada Tester",
                 "releaseDate": "2020-05-01T07:00:00Z", "description": "<p>A <b>test</b> book.</p>",
                 "artworkUrl100": "https://example.invalid/100x100bb.jpg", "genres": ["Books", "Fiction"]},
                {"trackId": 1, "trackName": "Summary of " + title, "artistName": "Ada Tester"}]}
        if url.startswith("https://openlibrary.org/search.json"):
            return {"docs": [{"key": "/works/OL1W", "title": title, "author_name": ["Tester, Ada"],
                              "first_publish_year": 2019, "isbn": [isbn], "edition_key": [olid],
                              "number_of_pages_median": 287}]}
        return None
    return fetch, title, olid


class NoCovers:
    def fetch(self, url):
        from medialib.covers import CoverError
        raise CoverError("offline test")


def test_lookup_and_add(cfg):
    fetch, title, olid = fake_fetch(None)
    f = service.Fill(NoCovers(), fetch)
    r = f.lookup(title, "Ada Tester")
    assert [c["provider"] for c in r["candidates"]] == ["apple", "openlibrary"]      # the summary was dropped
    with db.connect(cfg) as conn:
        out = f.add(conn, "owner@test", r["token"], 0, "planned")
        assert out["existing"] is False
        row = conn.execute("""SELECT i.title, i.provenance AS ip, e.pages, e.published_on, e.published_precision,
                                     e.description, e.provenance AS ep, en.status, en.provenance AS np
                                FROM catalog.media_item i JOIN catalog.edition e ON e.work_id = i.id AND e.is_primary
                                JOIN library.entry en ON en.item_id = i.id WHERE i.public_id = %s""", (out["id"],)).fetchone()
        assert row["title"] == title and row["ip"]["title"]["src"] == "apple"
        assert row["pages"] == 287 and row["ep"]["pages"]["src"] == "openlibrary"        # filled from the other provider
        assert str(row["published_on"]) == "2020-05-01" and row["ep"]["published_on"]["src"] == "apple"
        assert row["description"] == "A test book."
        assert row["status"] == "planned" and row["np"]["status"]["src"] == "owner"
        ids = {r_["provider"]: r_["value"] for r_ in conn.execute(
            "SELECT provider, value FROM catalog.external_id x JOIN catalog.edition e ON e.id = x.edition_id"
            " JOIN catalog.media_item i ON i.id = e.work_id WHERE i.public_id = %s", (out["id"],))}
        assert ids["openlibrary_edition"] == olid and "apple_books" in ids
        authors = [r_["name"] for r_ in conn.execute(
            "SELECT p.name FROM catalog.contribution ct JOIN catalog.person p ON p.id = ct.person_id"
            " JOIN catalog.media_item i ON i.id = ct.item_id WHERE i.public_id = %s", (out["id"],))]
        assert authors == ["Ada Tester"]
        r2 = f.lookup(title, "Ada Tester")
        assert f.add(conn, "owner@test", r2["token"], 1, "planned") == {"id": out["id"], "existing": True}


def test_fill_missing_respects_owner(cfg):
    fetch, title, olid = fake_fetch(None)
    f = service.Fill(NoCovers(), fetch)
    with db.connect(cfg) as conn:
        with tx(conn, source="yamtrack", client="test"):
            item, ed = new_book(conn, title)
            add_entry(conn, item)
            row = conn.execute("SELECT id FROM catalog.person WHERE name = 'Ada Tester'").fetchone()
            p = row["id"] if row else conn.execute("INSERT INTO catalog.person (name) VALUES ('Ada Tester') RETURNING id").fetchone()["id"]
            conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, source) VALUES (%s, 'author', %s, 'yamtrack')",
                         (p, item))
        with tx(conn, source="owner", client="test"):
            conn.execute("UPDATE catalog.edition SET publisher = NULL WHERE id = %s", (ed,))     # owner cleared it
        pid = str(conn.execute("SELECT public_id FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["public_id"])
        res = f.fill_missing(conn, "owner@test", pid)
        fields = {c["field"] for c in res["changes"]}
        assert "pages" in fields and "publisher" not in fields
        row = conn.execute("SELECT pages, provenance FROM catalog.edition WHERE id = %s", (ed,)).fetchone()
        assert row["pages"] == 287 and row["provenance"]["pages"]["src"] == "openlibrary"
        again = {c["field"] for c in f.fill_missing(conn, "owner@test", pid)["changes"]}
        assert "pages" not in again and "publisher" not in again


def test_bad_lookups():
    f = service.Fill(NoCovers(), lambda url: None)
    with pytest.raises(service.FillError):
        f.lookup(isbn="123")
    with pytest.raises(service.FillError):
        f.lookup()
    assert f.lookup("Nothing", "Nobody")["candidates"] == []
