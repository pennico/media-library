"""Titles in other languages and the random order (1.1.0, D54)."""
import uuid

import pytest

from conftest import tx
from medialib import screen, titles
from test_screen import movie_details, run_import, uniq


def tr(code, country, title, key="title"):
    return {"iso_639_1": code, "iso_3166_1": country, "data": {key: title}}


def test_translations_pick_the_country_and_skip_empty_ones():
    d = {"translations": {"translations": [
        tr("en", "GB", "Life Is Beautiful (UK)"), tr("en", "US", "Life Is Beautiful"), tr("it", "IT", ""), tr("da", "DK", "Livet er smukt"),
        tr("fr", "FR", "La vie est belle")]}}
    assert titles.from_translations("movie", d) == {"eng": "Life Is Beautiful", "dan": "Livet er smukt"}
    # a series' translation has a name; the /translations answer has the list at the top
    assert titles.from_translations("series", {"translations": [tr("it", "CH", "Il ponte", "name")]}) == {"ita": "Il ponte"}
    assert titles.from_translations("movie", {}) == {} and titles.from_translations("movie", None) == {}


def test_tmdb_titles_are_written_and_the_owners_stay(app):
    title = uniq("Life Is Beautiful")
    d = dict(movie_details(title, 990_001), translations={"translations": [
        tr("it", "IT", "La vita è bella"), tr("en", "US", title), tr("da", "DK", "Livet er smukt")]})
    p = screen.parse("movie", d)
    assert p["titles"] == {"ita": "La vita è bella", "eng": title, "dan": "Livet er smukt"}
    with tx(app, source="tmdb"):
        item, changes = screen.write(app, "movie", p)
    assert {"title ita", "title eng", "title dan"} <= set(changes)
    with tx(app):
        assert titles.save(app, item, {"ita": "La vita è bella!", "dan": ""}, "owner") == ["ita", "dan"]
    p2 = dict(p, titles={"ita": "La vita è bella", "dan": "Livet er smukt", "eng": title})
    with tx(app, source="tmdb"):
        _, changes = screen.write(app, "movie", p2, item=item)
    rows = {r["language"]: (r["title"], r["source"]) for r in app.execute(
        "SELECT language, title, source FROM catalog.item_title WHERE item_id = %s", (item,)).fetchall()}
    # the owner's Italian title stays; Danish, removed by the owner, comes back from TMDB (the owner keeps no "none")
    assert rows["ita"] == ("La vita è bella!", "owner") and rows["eng"] == (title, "tmdb") and rows["dan"] == ("Livet er smukt", "tmdb")
    assert changes == ["title dan"]
    audit = app.execute("SELECT count(*) AS n FROM audit.change WHERE table_name = 'catalog.item_title'"
                        " AND row_key = jsonb_build_object('item_id', %s::bigint, 'language', 'ita')", (item,)).fetchone()
    assert audit["n"] == 2


@pytest.fixture(scope="module")
def web(cfg, tmp_path_factory):
    import test_web
    gen = test_web.site.__wrapped__(cfg, tmp_path_factory)
    s = next(gen)
    yield s
    next(gen, None)


def test_titles_and_random_order_through_the_api(web, cfg, app, tmp_path):
    from test_web import Client
    title, orig = uniq("Cinema Paradiso"), uniq("Nuovo Cinema Paradiso")
    run_import(cfg, app, [{"kind": "movie", "tmdb": dict(movie_details(title, 990_002, 1980), original_title=orig,
                                                         original_language="it"),
                           "status": "completed", "watches": [{"state": "completed", "source": "owner"}]}], tmp_path)
    c = Client(web)
    it = next(i for i in c.call("GET", "api/library")[1]["items"] if i["title"] == title)
    assert it["origTitle"] == orig and it["origLang"] == "ita" and it["titles"] == {}
    c.unlock()
    s, d = c.call("PATCH", "api/items/" + it["id"], {"titles": {"ita": orig, "eng": "New Paradise Cinema"}})
    assert s == 200, d
    assert d["titles"] == {"ita": orig, "eng": "New Paradise Cinema"}
    assert sorted((t["lang"], t["src"]) for t in d["otherTitles"]) == [("eng", "owner"), ("ita", "owner")]
    s, d = c.call("PATCH", "api/items/" + it["id"], {"titles": {"eng": ""}})
    assert s == 200 and d["titles"] == {"ita": orig}
    s, e = c.call("PATCH", "api/items/" + it["id"], {"titles": {"fra": "Beau"}})
    assert s == 400 and "titles" in e["error"]
    s, e = c.call("PATCH", "api/items/" + it["id"], {"titles": {"ita": 5}})
    assert s == 400
    # a collection can keep the random order
    s, r = c.call("POST", "api/shelves", {"name": "Test " + uuid.uuid4().hex[:6], "mediaKind": "movie"})
    sid = r["id"]
    assert c.call("PATCH", "api/shelves/" + sid, {"sortKey": "random"})[0] == 200
    shelf = next(x for x in c.call("GET", "api/library")[1]["shelves"] if x["id"] == sid)
    assert shelf["sort_key"] == "random"
