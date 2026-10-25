"""TMDB (The Movie Database) for movies and series (DESIGN.md §15.2, D46).

Search and details through the same `fetch(url) -> parsed JSON` as the book
providers (disk cache, tests on recorded answers). An API key is needed: a
secrets file, like the Google Books key; without one TMDB stays off and the
console hides "Add movie" and "Fill missing" for movies and series.

Posters come from TMDB's image server, which needs no key.
"""
import os
import urllib.parse

from . import providers as P

API = "https://api.themoviedb.org/3"
IMAGES = "https://image.tmdb.org/t/p/"
POSTER_SIZE = "w780"
SEASON_POSTER_SIZE = "w500"
PATH = {"movie": "movie", "series": "tv"}
PROVIDER = {"movie": "tmdb_movie", "series": "tmdb_tv"}


def api_key(path=None):
    """The TMDB API key (v3) from a secrets file; empty or missing = TMDB off."""
    path = path or os.environ.get("MEDIALIB_TMDB_KEY_FILE", "/run/secrets/tmdb.key")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def image_url(path, size=POSTER_SIZE):
    return IMAGES + size + path if path else None


def year_of(d):
    v = (d.get("release_date") or d.get("first_air_date") or "")[:4]
    return int(v) if v.isdigit() else None


class Tmdb:
    def __init__(self, fetch, key=None):
        self.fetch = fetch
        self.key = key
        self.available = bool(key)

    def _get(self, path, **params):
        params = dict(params, api_key=self.key)
        return self.fetch(API + path + "?" + urllib.parse.urlencode(sorted(params.items()))) or {}

    def search(self, kind, query, year=None, limit=8):
        """Candidates for the console: [{kind, tmdb, title, original_title, year, overview, poster_url, score}]."""
        query = (query or "").strip()
        if not query:
            return []
        params = {"query": query, "language": "en-US", "include_adult": "false"}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        out = []
        for r in (self._get("/search/" + PATH[kind], **params).get("results") or [])[:limit]:
            title = r.get("title") or r.get("name") or ""
            orig = r.get("original_title") or r.get("original_name") or ""
            out.append({"kind": kind, "tmdb": r["id"], "title": title, "original_title": orig if orig != title else None,
                        "year": year_of(r), "overview": (r.get("overview") or "")[:400] or None,
                        "language": P.language3(r.get("original_language")),
                        "poster_url": image_url(r.get("poster_path"), "w185"),
                        "score": round(max(P.title_score(query, title), P.title_score(query, orig) if orig else 0), 3),
                        "popularity": r.get("popularity") or 0})
        return out

    def details(self, kind, tmdb_id):
        """Details with credits and external ids (the importer's `tmdb` object has the same shape)."""
        d = self._get("/{}/{}".format(PATH[kind], int(tmdb_id)), language="en-US",
                      append_to_response="credits,external_ids")
        return d if d.get("id") else None

    def confident(self, kind, title, year):
        """The one TMDB entry this title (and year) can only mean, else None (never a guess)."""
        cands = [c for c in self.search(kind, title, year) if c["score"] >= 0.97
                 and (year is None or c["year"] is None or abs(c["year"] - year) <= 1)]
        if len(cands) == 1 or (cands and cands[0]["popularity"] > 3 * cands[1]["popularity"]):
            return cands[0]
        return None
