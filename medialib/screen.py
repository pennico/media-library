"""Movies and series: one writer for TMDB details (D46), used by the watch
importer, the console's "Add movie / series" and "Fill missing".

- `parse` turns a TMDB details answer (with credits and external ids) into
  plain values; posters are URLs, downloaded by the caller before the
  transaction (network never runs inside one).
- `write` runs inside a write_tx with source 'tmdb': it creates the item, or
  fills what an existing one lacks. A value the owner set is never touched
  (the provenance guard refuses it; this code skips such columns first).
- `add_watch` adds a watch (of one season, for a series) unless the same one
  is already there, so an import can run again without doubling anything.
"""
import datetime
import re

from . import db
from .fill import providers as P
from .fill import tmdb as T

GENRE_GROUP = "Film & TV"          # TMDB genres the book tree doesn't have go under this group
ROLE = {"movie": "director", "series": "creator"}
SERIES_STATUS = {"Returning Series": "returning", "In Production": "returning", "Ended": "ended",
                 "Canceled": "canceled", "Cancelled": "canceled"}
CAST = 6
TMDB_LANG = {"cn": "yue", "et": "est"}      # TMDB's own codes (Cantonese) and ones the ISO table here lacks
IMDB = re.compile(r"tt[0-9]{7,}")
WIKIDATA = re.compile(r"Q[0-9]+")


def _date(v):
    try:
        return datetime.date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _minutes(v):
    return v if isinstance(v, int) and 0 < v < 2000 else None


def parse(kind, d):
    """TMDB details -> plain values (None where TMDB has nothing)."""
    title = d.get("title") or d.get("name") or ""
    orig = d.get("original_title") or d.get("original_name")
    credits = d.get("credits") or {}
    ext = d.get("external_ids") or {}
    if kind == "movie":
        makers = [c["name"] for c in credits.get("crew") or [] if c.get("job") == "Director"]
        countries = [c.get("iso_3166_1") for c in d.get("production_countries") or []] or list(d.get("origin_country") or [])
    else:
        makers = [c["name"] for c in d.get("created_by") or []]
        countries = list(d.get("origin_country") or []) or [c.get("iso_3166_1") for c in d.get("production_countries") or []]
    p = {
        "kind": kind, "tmdb": str(d["id"]), "title": title.strip(),
        "original_title": orig.strip() if orig and orig.strip() != title.strip() else None,
        "original_language": TMDB_LANG.get(d.get("original_language")) or P.language3(d.get("original_language")),
        "released": _date(d.get("release_date") or d.get("first_air_date")),
        "description": (d.get("overview") or "").strip() or None,
        "countries": [c for c in dict.fromkeys(countries) if c and re.fullmatch(r"[A-Z]{2}", c)][:30] or None,
        "genres": [g["name"] for g in d.get("genres") or [] if g.get("name")],
        "imdb": ext.get("imdb_id") if IMDB.fullmatch(ext.get("imdb_id") or "") else None,
        "wikidata": ext.get("wikidata_id") if WIKIDATA.fullmatch(ext.get("wikidata_id") or "") else None,
        "poster": T.image_url(d.get("poster_path")),
        "makers": list(dict.fromkeys(m.strip() for m in makers if m and m.strip()))[:4],
        "cast": list(dict.fromkeys(c["name"].strip() for c in (credits.get("cast") or [])[:CAST] if c.get("name"))),
    }
    if kind == "movie":
        p["runtime"] = _minutes(d.get("runtime"))
    else:
        runs = [r for r in d.get("episode_run_time") or [] if _minutes(r)]
        p.update({
            "last_air": _date(d.get("last_air_date")), "status": SERIES_STATUS.get(d.get("status"), "unknown"),
            "season_count": d.get("number_of_seasons") if isinstance(d.get("number_of_seasons"), int) else None,
            "episode_count": d.get("number_of_episodes") if isinstance(d.get("number_of_episodes"), int) else None,
            "episode_runtime": runs[0] if runs else _minutes((d.get("last_episode_to_air") or {}).get("runtime")),
            "seasons": [{"number": s["season_number"], "name": (s.get("name") or "Season {}".format(s["season_number"])).strip(),
                         "air_date": _date(s.get("air_date")),
                         "episodes": s.get("episode_count") if isinstance(s.get("episode_count"), int) else None,
                         "poster": T.image_url(s.get("poster_path"), T.SEASON_POSTER_SIZE)}
                        for s in d.get("seasons") or [] if isinstance(s.get("season_number"), int) and 0 <= s["season_number"] < 1000],
        })
        if p["released"] and p["last_air"] and p["last_air"] < p["released"]:
            p["last_air"] = None
    return p


# ------------------------------------------------------------------ finding

def find(conn, kind, tmdb_id=None, title=None, year=None):
    """-> (item id, trashed) of the movie/series this is, or (None, False).
    By TMDB id first; else the same title (or original title) of that kind,
    confirmed by the year when both know one. Never a loose guess."""
    if tmdb_id:
        row = conn.execute("""SELECT i.id, i.deleted_at IS NOT NULL AS trashed FROM catalog.external_id x
                               JOIN catalog.media_item i ON i.id = x.item_id
                              WHERE x.provider = %s AND x.value = %s""", (T.PROVIDER[kind], str(tmdb_id))).fetchone()
        if row:
            return row["id"], row["trashed"]
    if not title:
        return None, False
    rows = conn.execute("""SELECT i.id, i.title, i.original_title, i.deleted_at IS NOT NULL AS trashed,
                                  extract(year FROM coalesce(m.release_date, s.first_air_date, i.first_published))::int AS year,
                                  EXISTS (SELECT 1 FROM catalog.external_id x WHERE x.item_id = i.id
                                           AND x.provider IN ('tmdb_movie', 'tmdb_tv')) AS has_tmdb
                             FROM catalog.media_item i
                             LEFT JOIN catalog.movie m ON m.item_id = i.id LEFT JOIN catalog.series s ON s.item_id = i.id
                            WHERE i.kind = %s""", (kind,)).fetchall()
    hits = []
    for r in rows:
        if r["has_tmdb"] and tmdb_id:             # it is another TMDB entry with the same title
            continue
        ts = max(P.title_score(title, r["title"]), P.title_score(title, r["original_title"]) if r["original_title"] else 0)
        if ts < 0.97:
            continue
        if year and r["year"]:
            if abs(year - r["year"]) > 1:
                continue
        elif ts < 1.0:
            continue
        hits.append(r)
    if len(hits) == 1:
        return hits[0]["id"], hits[0]["trashed"]
    return None, False


# ------------------------------------------------------------------- writes

def _labels(conn, table, key_col, key):
    row = conn.execute("SELECT * FROM {} WHERE {} = %s".format(table, key_col), (key,)).fetchone()
    return row, {k: (v or {}).get("src") for k, v in (row["provenance"] or {}).items()} if row else {}


def _fill(conn, table, key_col, key, wanted, changes, label):
    """Set the wanted values that are empty and not the owner's; returns what changed."""
    row, src = _labels(conn, table, key_col, key)
    cols = {}
    for col, v in wanted.items():
        if v is None or src.get(col) == "owner" or row[col] not in (None, [], ""):
            continue
        cols[col] = v
    if cols:
        conn.execute("UPDATE {} SET {} WHERE {} = %s".format(table, ", ".join("{} = %s".format(c) for c in cols), key_col),
                     (*cols.values(), key))
        changes += [label(c) for c in cols]
    return cols


def _language(conn, code):
    if code and conn.execute("SELECT 1 AS x FROM catalog.language WHERE code = %s", (code,)).fetchone():
        return code
    return None


def _person(conn, name):
    row = conn.execute("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)"
                       " AND disambiguation IS NULL AND deleted_at IS NULL", (name,)).fetchone()
    return row["id"] if row else conn.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id",
                                              (name[:300],)).fetchone()["id"]


def genre_tag(conn, name):
    """The genre for a TMDB genre name: the book tree's category of that name when there is one
    (History, Fantasy…), else one under the 'Film & TV' group."""
    row = conn.execute("SELECT id FROM library.tag WHERE kind = 'genre' AND lower(catalog.f_unaccent(name)) ="
                       " lower(catalog.f_unaccent(%s)) AND deleted_at IS NULL", (name,)).fetchone()
    if row:
        return row["id"]
    group = conn.execute("SELECT id FROM library.tag WHERE kind = 'genre' AND parent_id IS NULL AND name = %s"
                         " AND deleted_at IS NULL", (GENRE_GROUP,)).fetchone()
    gid = group["id"] if group else conn.execute("INSERT INTO library.tag (kind, name) VALUES ('genre', %s) RETURNING id",
                                                 (GENRE_GROUP,)).fetchone()["id"]
    return conn.execute("INSERT INTO library.tag (kind, name, parent_id) VALUES ('genre', %s, %s) RETURNING id",
                        (name[:100], gid)).fetchone()["id"]


def plain_tag(conn, name):
    row = conn.execute("SELECT id FROM library.tag WHERE kind = 'tag' AND lower(catalog.f_unaccent(name)) ="
                       " lower(catalog.f_unaccent(%s)) AND deleted_at IS NULL", (name,)).fetchone()
    return row["id"] if row else conn.execute("INSERT INTO library.tag (kind, name) VALUES ('tag', %s) RETURNING id",
                                              (name[:100],)).fetchone()["id"]


def add_tag(conn, item, tag_id, source):
    if conn.execute("SELECT 1 AS x FROM library.item_tag WHERE item_id = %s AND tag_id = %s", (item, tag_id)).fetchone():
        return False
    n = conn.execute("SELECT count(*) AS n FROM library.item_tag WHERE item_id = %s", (item,)).fetchone()["n"]
    conn.execute("INSERT INTO library.item_tag (item_id, tag_id, position, source) VALUES (%s, %s, %s, %s)",
                 (item, tag_id, n + 1, source))
    return True


def _cover(conn, col, owner_id, url, info, changes, what):
    """A TMDB poster: chosen when there is no chosen cover yet, else kept as an alternative."""
    if not url or conn.execute("SELECT 1 AS x FROM catalog.cover WHERE {} = %s AND source_url = %s AND deleted_at IS NULL"
                               .format(col), (owner_id, url)).fetchone():
        return
    chosen = conn.execute("SELECT 1 AS x FROM catalog.cover WHERE {} = %s AND is_chosen AND deleted_at IS NULL".format(col),
                          (owner_id,)).fetchone()
    if info:
        conn.execute("INSERT INTO catalog.cover ({}, source, source_url, local_path, sha256, mime, width, height, bytes,"
                     " fetched_at, is_chosen) VALUES (%s, 'tmdb', %s, %s, %s, %s, %s, %s, %s, %s, %s)".format(col),
                     (owner_id, url, info["local_path"], info["sha256"], info["mime"], info["width"], info["height"],
                      info["bytes"], info.get("fetched_at"), not chosen))
        changes.append(what)
    elif col == "item_id" and what == "poster":
        conn.execute("INSERT INTO catalog.cover (item_id, source, source_url) VALUES (%s, 'tmdb', %s)", (owner_id, url))


def write(conn, kind, p, covers=None, item=None):
    """Create the movie/series from parsed TMDB values (item None) or fill what it
    lacks. Inside a write_tx (source 'tmdb'). -> (item id, [changed fields])."""
    covers = covers or {}
    changes = []
    db.set_source(conn, "tmdb")
    lang = _language(conn, p["original_language"])
    item_cols = {"title": p["title"], "original_title": p["original_title"], "original_language": lang,
                 "first_published": p["released"], "first_published_precision": "day" if p["released"] else None,
                 "description": p["description"]}
    if kind == "movie":
        sub = {"release_date": p["released"], "runtime_min": p["runtime"], "origin_countries": p["countries"]}
    else:
        sub = {"first_air_date": p["released"], "last_air_date": p["last_air"], "origin_countries": p["countries"],
               "season_count": p["season_count"], "episode_count": p["episode_count"],
               "episode_runtime_min": p["episode_runtime"]}
    if item is None:
        names = [c for c, v in item_cols.items() if v is not None]
        item = conn.execute("INSERT INTO catalog.media_item (kind, {}) VALUES (%s, {}) RETURNING id".format(
            ", ".join(names), ", ".join(["%s"] * len(names))), (kind, *[item_cols[c] for c in names])).fetchone()["id"]
        cols = {c: v for c, v in sub.items() if v is not None}
        if kind == "series":
            cols["status"] = p["status"]
        conn.execute("INSERT INTO catalog.{} (item_id{}) VALUES (%s{})".format(
            kind, "".join(", " + c for c in cols), ", %s" * len(cols)), (item, *cols.values()))
        changes.append("created")
    else:
        if item_cols["first_published"] is not None:
            row, src = _labels(conn, "catalog.media_item", "id", item)
            # a year typed by the owner stays; a year-only date from a note becomes the full release date
            if src.get("first_published") != "owner" and row["first_published_precision"] in (None, "year") \
                    and (row["first_published"] is None or row["first_published"].year == p["released"].year):
                conn.execute("UPDATE catalog.media_item SET first_published = %s, first_published_precision = 'day'"
                             " WHERE id = %s", (p["released"], item))
                if row["first_published"] != p["released"]:
                    changes.append("released")
            item_cols.pop("first_published")
            item_cols.pop("first_published_precision")
        _fill(conn, "catalog.media_item", "id", item, item_cols, changes, lambda c: c)
        if kind == "series" and p["status"] != "unknown":
            sub["status"] = p["status"]
        _fill(conn, "catalog." + kind, "item_id", item, sub, changes, lambda c: c)
        if kind == "series" and "status" in sub:
            row, src = _labels(conn, "catalog.series", "item_id", item)
            if row["status"] == "unknown" and src.get("status") != "owner":
                conn.execute("UPDATE catalog.series SET status = %s WHERE item_id = %s", (p["status"], item))
    # identifiers
    for prov, val in ((T.PROVIDER[kind], p["tmdb"]), ("imdb", p["imdb"]), ("wikidata", p["wikidata"])):
        if not val:
            continue
        taken = conn.execute("SELECT item_id FROM catalog.external_id WHERE provider = %s AND value = %s", (prov, val)).fetchone()
        mine = conn.execute("SELECT 1 AS x FROM catalog.external_id WHERE provider = %s AND item_id = %s", (prov, item)).fetchone()
        if not taken and not mine:
            conn.execute("INSERT INTO catalog.external_id (provider, value, item_id, source) VALUES (%s, %s, %s, 'tmdb')",
                         (prov, val, item))
            changes.append(prov)
    # people: directors / creators, then the main cast, when the item has none of that role yet
    for role, names in ((ROLE[kind], p["makers"]), ("cast", p["cast"])):
        if not names or conn.execute("SELECT 1 AS x FROM catalog.contribution WHERE item_id = %s AND role = %s",
                                     (item, role)).fetchone():
            continue
        for pos, name in enumerate(names, 1):
            conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source)"
                         " VALUES (%s, %s, %s, %s, 'tmdb')", (_person(conn, name), role, item, pos))
        changes.append(role)
    # genres, when the item has none yet (the owner's own choice stays as it is)
    if p["genres"] and not conn.execute("SELECT 1 AS x FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id"
                                        " WHERE it.item_id = %s AND t.kind = 'genre'", (item,)).fetchone():
        for name in p["genres"]:
            add_tag(conn, item, genre_tag(conn, name), "tmdb")
        changes.append("genres")
    _cover(conn, "item_id", item, p["poster"], covers.get(p["poster"]), changes, "poster")
    if kind == "series":
        for s in p["seasons"]:
            season_row(conn, item, s, covers, changes)
    return item, changes


def season_row(conn, series, s, covers=None, changes=None):
    """The season (created once; its empty values filled), with its poster when downloaded."""
    covers = covers or {}
    changes = changes if changes is not None else []
    row = conn.execute("SELECT item_id FROM catalog.season WHERE series_id = %s AND season_number = %s",
                       (series, s["number"])).fetchone()
    if row:
        sid = row["item_id"]
        _fill(conn, "catalog.season", "item_id", sid, {"air_date": s.get("air_date"), "episode_count": s.get("episodes")},
              changes, lambda c: "season " + c.replace("_", " "))
    else:
        sid = conn.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('season', %s) RETURNING id",
                           ((s.get("name") or "Season {}".format(s["number"]))[:1000],)).fetchone()["id"]
        conn.execute("INSERT INTO catalog.season (item_id, series_id, season_number, air_date, episode_count)"
                     " VALUES (%s, %s, %s, %s, %s)", (sid, series, s["number"], s.get("air_date"), s.get("episodes")))
    if s.get("poster") and s["poster"] in covers:
        _cover(conn, "item_id", sid, s["poster"], covers[s["poster"]], changes, "season poster")
    return sid


def season_id(conn, series, number, create=True):
    row = conn.execute("SELECT item_id FROM catalog.season WHERE series_id = %s AND season_number = %s",
                       (series, number)).fetchone()
    if row:
        return row["item_id"]
    return season_row(conn, series, {"number": number}) if create else None


def add_watch(conn, item, state, started=None, started_precision=None, ended=None, ended_precision=None,
              season=None, episodes=None, platform=None, source="owner", note=None):
    """A watch, unless the same one is there: one ending the same day (or, for an
    undated one, any watch in that state) of the same item and season."""
    sid = season_id(conn, item, season) if season is not None else None
    same = conn.execute("""SELECT id FROM library.consumption
                            WHERE item_id = %s AND season_id IS NOT DISTINCT FROM %s AND deleted_at IS NULL
                              AND (ended_on = %s OR (%s::date IS NULL AND state = %s))""",
                        (item, sid, ended, ended, state)).fetchone()
    if same:
        return None
    db.set_source(conn, source)
    return conn.execute("""INSERT INTO library.consumption (item_id, season_id, state, started_on, started_precision,
                                   ended_on, ended_precision, episodes, platform, note, source)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (item, sid, state, started, started_precision if started else None, ended,
                         ended_precision if ended else None, episodes if sid else None, platform, note, source)).fetchone()["id"]
