"""Titles in other languages (1.1.0, D54): catalog.item_title.

TMDB's translations give a movie's or series' title in English, Italian and
Danish; the owner can type them too. The owner's titles are never replaced.
`backfill` asks TMDB for the movies and series already in the library
(`python -m medialib titles [--apply]`).
"""
from . import db

# the languages kept, with TMDB's language code and the country whose translation comes first
LANGS = {"eng": ("en", ("US", "GB")), "ita": ("it", ("IT",)), "dan": ("da", ("DK",))}
MAX = 1000


def from_translations(kind, d):
    """{lang3: title} from TMDB's `translations` (a details answer with them appended, or the
    /translations answer itself). An empty translation (TMDB leaves the original's language empty) is skipped."""
    rows = (d or {}).get("translations") or []
    if isinstance(rows, dict):                  # appended to the details: {translations: {translations: [...]}}
        rows = rows.get("translations") or []
    if not isinstance(rows, list):
        return {}
    key = "title" if kind == "movie" else "name"
    out = {}
    for code3, (code2, countries) in LANGS.items():
        cands = [r for r in rows if isinstance(r, dict) and r.get("iso_639_1") == code2
                 and ((r.get("data") or {}).get(key) or "").strip()]
        cands.sort(key=lambda r: countries.index(r.get("iso_3166_1")) if r.get("iso_3166_1") in countries else len(countries))
        if cands:
            out[code3] = cands[0]["data"][key].strip()[:MAX]
    return out


def save(conn, item, titles, source):
    """Set the titles {lang3: title} inside an open write transaction: a new language is added,
    one from another source is replaced, the owner's stay unless `source` is the owner.
    An empty title (the owner's) removes that language. -> [changed languages]."""
    changed = []
    for lang, title in titles.items():
        if lang not in LANGS:
            continue
        title = (title or "").strip()[:MAX]
        row = conn.execute("SELECT title, source FROM catalog.item_title WHERE item_id = %s AND language = %s",
                           (item, lang)).fetchone()
        if row and row["source"] == "owner" and source != "owner":
            continue
        if not title:
            if row and source == "owner":
                conn.execute("DELETE FROM catalog.item_title WHERE item_id = %s AND language = %s", (item, lang))
                changed.append(lang)
            continue
        if row and row["title"] == title and row["source"] == source:
            continue
        db.set_source(conn, source)
        conn.execute("""INSERT INTO catalog.item_title (item_id, language, title, source) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (item_id, language) DO UPDATE SET title = EXCLUDED.title, source = EXCLUDED.source""",
                     (item, lang, title, source))
        changed.append(lang)
    return changed


def backfill(conn, tmdb, apply=False, log=print):
    """Ask TMDB for the titles of every movie and series with a TMDB id that has none of
    LANGS yet from TMDB. -> {items, changed, failed}."""
    from .fill import tmdb as T
    rows = conn.execute("""SELECT i.id, i.kind, i.title, x.value AS tmdb FROM catalog.media_item i
                             JOIN catalog.external_id x ON x.item_id = i.id AND x.provider IN ('tmdb_movie', 'tmdb_tv')
                            WHERE i.kind IN ('movie', 'series') AND i.deleted_at IS NULL
                              AND (SELECT count(*) FROM catalog.item_title t WHERE t.item_id = i.id) < %s
                            ORDER BY i.id""", (len(LANGS),)).fetchall()
    done, changed, failed = 0, 0, []
    for r in rows:
        try:
            got = from_translations(r["kind"], tmdb.translations(r["kind"], r["tmdb"]))
        except Exception as e:          # one lookup failing leaves the rest to do
            failed.append((r["title"], str(e)))
            continue
        done += 1
        if not got:
            continue
        if apply:
            with db.write_tx(conn, "tmdb", "titles", reason="titles from TMDB"):
                n = save(conn, r["id"], got, "tmdb")
        else:
            n = list(got)
        if n:
            changed += 1
            log("{}: {}".format(r["title"], ", ".join("{} {}".format(k, got[k]) for k in sorted(got))))
    log("{} {} of {} looked up ({} failed){}".format(changed, "updated" if apply else "would change", done + len(failed),
                                                   len(failed), "" if apply else "; dry run, nothing written"))
    for t, e in failed[:20]:
        log("failed: {}: {}".format(t, e))
    return {"items": len(rows), "changed": changed, "failed": failed}
