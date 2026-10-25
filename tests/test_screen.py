"""Movies and series (0.6.0): the season-watch rules in the database, the
TMDB writer and the watch importer (no network: details are recorded below)."""
import datetime
import json
import uuid

import psycopg
import pytest

from conftest import tx
from test_schema import sqlstate
from medialib import screen
from medialib.importer import watch


def uniq(t):
    return "{} {}".format(t, uuid.uuid4().hex[:6])


def movie_details(title, tmdb_id, year=1999):
    return {"id": tmdb_id, "title": title, "original_title": title, "original_language": "en",
            "release_date": "{}-10-15".format(year), "runtime": 139, "overview": "A soap salesman.",
            "genres": [{"id": 18, "name": "Drama"}, {"id": 99, "name": uniq("Gloomy")}],
            "production_countries": [{"iso_3166_1": "US"}, {"iso_3166_1": "DE"}],
            "poster_path": None, "external_ids": {"imdb_id": "tt0{}".format(tmdb_id % 10**6 + 1000000)},
            "credits": {"crew": [{"job": "Director", "name": "David Fincher"}, {"job": "Writer", "name": "Jim Uhls"}],
                        "cast": [{"name": "Edward Norton"}, {"name": "Brad Pitt"}]}}


def series_details(title, tmdb_id):
    return {"id": tmdb_id, "name": title, "original_name": title, "original_language": "da",
            "first_air_date": "2010-09-26", "last_air_date": "2022-06-02", "status": "Ended",
            "number_of_seasons": 3, "number_of_episodes": 30, "episode_run_time": [58],
            "origin_country": ["DK"], "genres": [{"id": 18, "name": "Drama"}], "poster_path": None,
            "created_by": [{"name": "Adam Price"}], "credits": {"cast": [{"name": "Sidse Babett Knudsen"}]},
            "external_ids": {}, "seasons": [{"season_number": 0, "name": "Specials", "episode_count": 2}] + [
                {"season_number": n, "name": "Season {}".format(n), "episode_count": 10, "air_date": "201{}-01-01".format(n)}
                for n in (1, 2, 3)]}


class NoStore:
    def fetch(self, url):
        from medialib.covers import CoverError
        raise CoverError("no network in tests")


def run_import(cfg, app, items, tmp_path, apply=True):
    path = tmp_path / "watch.json"
    path.write_text(json.dumps({"format": "medialib-watch", "version": 1, "items": items}))
    return watch.run(cfg, str(path), apply=apply, out_dir=str(tmp_path), log=lambda *_: None, conn=app, store=NoStore())


# ------------------------------------------------------------------ database

def new_series(conn, title):
    item = conn.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('series', %s) RETURNING id", (title,)).fetchone()["id"]
    conn.execute("INSERT INTO catalog.series (item_id) VALUES (%s)", (item,))
    return item


def test_a_season_watch_must_be_of_that_series(app):
    with tx(app):
        a, b = new_series(app, uniq("A")), new_series(app, uniq("B"))
        sa = screen.season_id(app, a, 1)
        app.execute("INSERT INTO library.entry (item_id) VALUES (%s), (%s)", (a, b))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with tx(app):
            app.execute("INSERT INTO library.consumption (item_id, season_id, state, source) VALUES (%s, %s, 'dropped', 'owner')",
                        (b, sa))
    with pytest.raises(psycopg.errors.CheckViolation):
        with tx(app):
            app.execute("INSERT INTO library.consumption (item_id, state, episodes, source) VALUES (%s, 'completed', 3, 'owner')", (a,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with tx(app):
            app.execute("INSERT INTO library.consumption (item_id, season_id, state, platform, source)"
                        " VALUES (%s, %s, 'completed', 'nowhere', 'owner')", (a, sa))
    with tx(app):
        app.execute("INSERT INTO library.consumption (item_id, season_id, state, episodes, platform, source)"
                    " VALUES (%s, %s, 'completed', 10, 'netflix', 'owner')", (a, sa))
        app.execute("UPDATE library.entry SET status = 'completed' WHERE item_id = %s", (a,))


def test_screen_values_keep_their_provenance(app):
    with tx(app, source="tmdb"):
        item, _ = screen.write(app, "movie", screen.parse("movie", movie_details(uniq("Fight Club"), 550_000 + 1)))
    row = app.execute("SELECT provenance FROM catalog.movie WHERE item_id = %s", (item,)).fetchone()
    assert row["provenance"]["runtime_min"]["src"] == "tmdb"
    with tx(app):                                         # the owner corrects the runtime ...
        app.execute("UPDATE catalog.movie SET runtime_min = 151 WHERE item_id = %s", (item,))
    with sqlstate("ML002"):                               # ... and TMDB may not change it back
        with tx(app, source="tmdb"):
            app.execute("UPDATE catalog.movie SET runtime_min = 139 WHERE item_id = %s", (item,))
    with pytest.raises(psycopg.errors.CheckViolation):
        with tx(app, source="tmdb"):
            app.execute("UPDATE catalog.movie SET origin_countries = '{usa}' WHERE item_id = %s", (item,))


# -------------------------------------------------------------------- writer

def test_tmdb_writer_creates_and_then_only_fills(app):
    title = uniq("Fight Club")
    p = screen.parse("movie", movie_details(title, 550_000 + 2))
    assert p["makers"] == ["David Fincher"] and p["countries"] == ["US", "DE"] and p["original_language"] == "eng"
    with tx(app, source="tmdb"):
        item, changes = screen.write(app, "movie", p)
    assert "created" in changes
    people = app.execute("SELECT c.role, p.name FROM catalog.contribution c JOIN catalog.person p ON p.id = c.person_id"
                         " WHERE c.item_id = %s ORDER BY c.role, c.position", (item,)).fetchall()
    assert [(r["role"], r["name"]) for r in people] == [("cast", "Edward Norton"), ("cast", "Brad Pitt"), ("director", "David Fincher")]
    g = app.execute("SELECT t.name, pt.name AS grp FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id"
                    " LEFT JOIN library.tag pt ON pt.id = t.parent_id WHERE it.item_id = %s ORDER BY it.position", (item,)).fetchall()
    assert g[1]["grp"] == screen.GENRE_GROUP
    with tx(app):
        app.execute("UPDATE catalog.media_item SET description = 'My own words.' WHERE id = %s", (item,))
    p2 = dict(p, description="TMDB's words.", cast=["Someone Else"])
    with tx(app, source="tmdb"):
        _, changes = screen.write(app, "movie", p2, item=item)
    assert changes == []
    assert app.execute("SELECT description FROM catalog.media_item WHERE id = %s", (item,)).fetchone()["description"] == "My own words."


def test_series_get_their_seasons(app):
    with tx(app, source="tmdb"):
        item, _ = screen.write(app, "series", screen.parse("series", series_details(uniq("Borgen"), 660_000 + 1)))
    rows = app.execute("SELECT s.season_number, s.episode_count, i.title FROM catalog.season s JOIN catalog.media_item i"
                       " ON i.id = s.item_id WHERE s.series_id = %s ORDER BY 1", (item,)).fetchall()
    assert [(r["season_number"], r["episode_count"]) for r in rows] == [(0, 2), (1, 10), (2, 10), (3, 10)]
    s = app.execute("SELECT status, season_count, episode_runtime_min, origin_countries FROM catalog.series WHERE item_id = %s",
                    (item,)).fetchone()
    assert (s["status"], s["season_count"], s["episode_runtime_min"], s["origin_countries"]) == ("ended", 3, 58, ["DK"])


# ------------------------------------------------------------------ importer

def test_import_watch_adds_once_and_respects_the_owner(cfg, app, tmp_path):
    film, show = uniq("Fight Club"), uniq("Borgen")
    items = [
        {"kind": "movie", "tmdb": movie_details(film, 550_000 + 3), "status": "completed",
         "watches": [{"state": "completed", "started": "2018-01-25", "ended": "2018-01-25", "platform": "netflix", "source": "netflix"}],
         "tags": [{"name": "Netflix: liked", "source": "netflix"}], "added": "2018-01-25"},
        {"kind": "series", "tmdb": series_details(show, 660_000 + 2), "status": "dropped", "rating": 9,
         "watches": [{"season": 1, "state": "completed", "started": "2023-06-01", "ended": "2023-06-20", "episodes": 10,
                      "platform": "netflix", "source": "netflix"},
                     {"season": 2, "state": "dropped", "started": "2023-07-01", "ended": "2023-07-04", "episodes": 6,
                      "platform": "netflix", "source": "netflix"}]},
        {"kind": "movie", "title": uniq("Not on TMDB"), "year": 1990, "status": "completed",
         "watches": [{"state": "completed", "source": "owner"}]},
        {"kind": "movie", "tmdb": movie_details(uniq("Wanted"), 550_000 + 4, 2010), "status": "planned",
         "collections": ["Prime Video watchlist"]},
    ]
    dry = run_import(cfg, app, items, tmp_path, apply=False)
    assert dry["summary"] == {"added": 4}
    assert app.execute("SELECT count(*) AS n FROM catalog.media_item WHERE title = %s", (film,)).fetchone()["n"] == 0
    r = run_import(cfg, app, items, tmp_path)
    assert not r["failed"] and r["summary"] == {"added": 4}
    sid = app.execute("SELECT id FROM catalog.media_item WHERE title = %s", (show,)).fetchone()["id"]
    e = app.execute("SELECT status, rating, provenance FROM library.entry WHERE item_id = %s", (sid,)).fetchone()
    assert e["status"] == "dropped" and float(e["rating"]) == 9 and e["provenance"]["rating"]["src"] == "owner"
    ws = app.execute("SELECT s.season_number, c.state, c.episodes, c.platform, c.source FROM library.consumption c"
                     " JOIN catalog.season s ON s.item_id = c.season_id WHERE c.item_id = %s ORDER BY 1", (sid,)).fetchall()
    assert [(w["season_number"], w["state"], w["episodes"], w["source"]) for w in ws] == [(1, "completed", 10, "netflix"),
                                                                                      (2, "dropped", 6, "netflix")]
    fid = app.execute("SELECT id FROM catalog.media_item WHERE title = %s", (film,)).fetchone()["id"]
    assert app.execute("SELECT added_at::date AS d FROM library.entry WHERE item_id = %s", (fid,)).fetchone()["d"] == datetime.date(2018, 1, 25)
    again = run_import(cfg, app, items, tmp_path)         # a second run adds nothing
    assert again["summary"] == {"updated": 4} and all(not x["changes"] for x in again["results"])
    assert app.execute("SELECT count(*) AS n FROM library.consumption WHERE item_id = %s", (sid,)).fetchone()["n"] == 2


def test_import_watch_finds_a_note_made_movie_by_title(cfg, app, tmp_path):
    title = uniq("Il sorpasso")
    with tx(app):                                         # what the Joplin feed creates: title, year, a reading
        item = app.execute("INSERT INTO catalog.media_item (kind, title, first_published, first_published_precision)"
                           " VALUES ('movie', %s, '1962-01-01', 'year') RETURNING id", (title,)).fetchone()["id"]
        app.execute("INSERT INTO catalog.movie (item_id) VALUES (%s)", (item,))
        app.execute("INSERT INTO library.entry (item_id, status, review, reviewed_at) VALUES (%s, 'completed', 'Mine.', now())", (item,))
        app.execute("INSERT INTO library.consumption (item_id, state, ended_on, ended_precision, source)"
                    " VALUES (%s, 'completed', '2026-10-25', 'day', 'owner')", (item,))
    d = movie_details(title, 550_000 + 5, 1962)
    r = run_import(cfg, app, [{"kind": "movie", "tmdb": d, "status": "completed", "review": "Not this.",
                               "watches": [{"state": "completed", "source": "owner"}]}], tmp_path)
    assert r["summary"] == {"updated": 1}
    row = app.execute("SELECT i.first_published, m.release_date, e.review,"
                      " (SELECT count(*) FROM library.consumption c WHERE c.item_id = i.id) AS n,"
                      " (SELECT value FROM catalog.external_id x WHERE x.item_id = i.id AND x.provider = 'tmdb_movie') AS tmdb"
                      " FROM catalog.media_item i JOIN library.entry e ON e.item_id = i.id JOIN catalog.movie m ON m.item_id = i.id"
                      " WHERE i.id = %s", (item,)).fetchone()
    # the note's year is the owner's and stays; the release date goes on the movie; the owner's review stays
    assert row["first_published"] == datetime.date(1962, 1, 1) and row["release_date"] == datetime.date(1962, 10, 15)
    assert row["review"] == "Mine." and row["n"] == 1
    assert row["tmdb"] == str(550_000 + 5)


def test_import_watch_refuses_a_bad_file(cfg, app, tmp_path):
    r = run_import(cfg, app, [{"kind": "book", "title": "x", "status": "completed"}], tmp_path)
    assert r["failed"] and "kind must be movie or series" in r["problems"][0]


# ----------------------------------------------------------------- web API

@pytest.fixture(scope="module")
def web(cfg, tmp_path_factory):
    import test_web
    gen = test_web.site.__wrapped__(cfg, tmp_path_factory)
    s = next(gen)
    yield s
    next(gen, None)


def test_series_watches_through_the_api(web, cfg, app, tmp_path):
    from test_web import Client
    show = uniq("Borgen")
    run_import(cfg, app, [{"kind": "series", "tmdb": series_details(show, 660_000 + 9), "status": "completed",
                           "watches": [{"season": 1, "state": "completed", "ended": "2023-06-20", "episodes": 10,
                                        "platform": "netflix", "source": "netflix"}]}], tmp_path)
    c = Client(web)
    s, lib = c.call("GET", "api/library")
    it = next(i for i in lib["items"] if i["title"] == show)
    assert it["seasonWatches"] == {"done": 1, "dropped": 0} and it["seasons"] == 3 and it["platforms"] == ["netflix"]
    assert it["reads"][0][3] == 580 and it["reads"][0][4] == "netflix" and it["reads"][0][5] == 1    # 10 episodes x 58 min
    assert ["netflix", "Netflix"] in lib["platforms"] and lib["tmdb"] is False
    s, d = c.call("GET", "api/items/" + it["id"])
    assert d["authorsList"][0]["name"] == "Adam Price" and d["screen"]["episodeRuntime"] == 58
    assert [x["number"] for x in d["screen"]["seasons"]] == [1, 2, 3] and d["screen"]["seasons"][0]["watches"][0]["episodes"] == 10
    c.unlock()
    s, d = c.call("POST", "api/items/" + it["id"] + "/readings",
                  {"state": "dropped", "season": 2, "episodes": 4, "platform": "dr", "ended": "2023-08"})
    assert s == 200, d
    r = next(x for x in d["readings"] if x["season"] == 2)
    assert (r["state"], r["episodes"], r["platform"]) == ("dropped", 4, "dr")
    s, e = c.call("POST", "api/items/" + it["id"] + "/readings", {"state": "completed", "episodes": 3})
    assert s == 400 and "season" in e["error"]
    s, e = c.call("POST", "api/items/" + web["item"] + "/readings", {"state": "completed", "season": 1})
    assert s == 400 and "only a series" in e["error"]
    s, e = c.call("POST", "api/tmdb/search", {"kind": "movie", "query": "Heat"})
    assert s == 400 and "TMDB is off" in e["error"]
    s, d = c.call("PATCH", "api/items/" + it["id"], {"authors": ["Adam Price", "Jeppe Gjervig Gram"]})
    assert s == 200 and [a["name"] for a in d["authorsList"]] == ["Adam Price", "Jeppe Gjervig Gram"]


def test_import_watch_an_owner_seen_list_is_watched(cfg, app, tmp_path):
    """A title from the owner's own "seen" list (an undated watch, source owner) is Watched, not To watch."""
    title = uniq("Casablanca")
    item = {"kind": "movie", "tmdb": movie_details(title, 550_000 + 6, 1942), "status": "completed",
            "watches": [{"state": "completed", "source": "owner"}]}
    run_import(cfg, app, [item], tmp_path)
    row = app.execute("SELECT e.item_id, e.status FROM library.entry e JOIN catalog.media_item i ON i.id = e.item_id"
                      " WHERE i.title = %s", (title,)).fetchone()
    assert row["status"] == "completed"
    # an entry the 0.6.0 importer left To watch (its own owner-labelled status) is corrected by a rerun ...
    with tx(app, client="import-watch"):
        app.execute("UPDATE library.entry SET status = 'planned' WHERE item_id = %s", (row["item_id"],))
    again = run_import(cfg, app, [item], tmp_path)
    assert again["results"][0]["changes"] == ["status completed"]
    # ... but once the owner sets it in the console, a rerun leaves it
    with tx(app, client="console:owner"):
        app.execute("UPDATE library.entry SET status = 'planned' WHERE item_id = %s", (row["item_id"],))
    run_import(cfg, app, [item], tmp_path)
    assert app.execute("SELECT status FROM library.entry WHERE item_id = %s", (row["item_id"],)).fetchone()["status"] == "planned"
