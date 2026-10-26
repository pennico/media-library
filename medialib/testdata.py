"""Made-up books, movies and series in the Test section (0.8.0, D50), to try the
console with (selection, quick actions, sections) without touching the real
library. Every title starts with "Test ·"; the Test section is kept out of the
statistics, and the whole set goes again with --remove.

    python -m medialib test-data            # add (again: only what is missing)
    python -m medialib test-data --remove   # delete every item in the Test section for good
"""
import datetime

from . import db

PREFIX = "Test · "
BOOKS = [  # title, author, pages, language, status, finished
    ("The Paper Lighthouse", "Ada Fictitious", 212, "eng", "completed", "2026-03-14"),
    ("Il giardino di prova", "Bruno Inventato", 340, "ita", "completed", "2025-11-02"),
    ("Prøvebogen", "Carla Opdigtet", 158, "dan", "planned", None),
    ("A Map of Nowhere", "Ada Fictitious", 480, "eng", "in_progress", None),
    ("Notes on Sample Data", "Dario Fittizio", 96, "eng", "dropped", "2026-01-20"),
    ("The Placeholder Saga", "Eva Imaginary", 620, "eng", "planned", None),
]
MOVIES = [  # title, director, year, runtime, status, watched
    ("Midnight in Testville", "Fabio Finto", 2019, 104, "completed", "2026-02-11"),
    ("The Dummy Heist", "Greta Pretend", 2021, 118, "completed", "2025-12-30"),
    ("Lorem Ipsum Nights", "Fabio Finto", 2016, 95, "planned", None),
    ("Return of the Mock", "Hugo Sample", 2023, 131, "planned", None),
]
SERIES = [  # title, creator, year, [(season, episodes, watched episodes, state, ended)]
    ("Fixture Street", "Iris Example", 2018, [(1, 8, 8, "completed", "2025-10-05"), (2, 8, 3, "dropped", "2026-01-12"),
                                               (3, 10, None, None, None)]),
    ("The Sandbox Files", "Jonas Demo", 2022, [(1, 6, 6, "completed", "2026-04-01")]),
]


def _person(conn, name):
    row = conn.execute("SELECT id FROM catalog.person WHERE catalog.name_key(name) = catalog.name_key(%s)"
                       " AND disambiguation IS NULL AND deleted_at IS NULL", (name,)).fetchone()
    return row["id"] if row else conn.execute("INSERT INTO catalog.person (name) VALUES (%s) RETURNING id",
                                              (name,)).fetchone()["id"]


def _exists(conn, kind, title):
    return conn.execute("SELECT 1 AS x FROM catalog.media_item WHERE kind = %s AND title = %s AND deleted_at IS NULL",
                        (kind, PREFIX + title)).fetchone()


def _item(conn, kind, title, year, maker, role, section, status, lang=None):
    it = conn.execute("INSERT INTO catalog.media_item (kind, title, title_language, original_language, first_published,"
                      " first_published_precision) VALUES (%s, %s, %s, %s, %s, 'year') RETURNING id",
                      (kind, PREFIX + title, lang, lang, datetime.date(year, 1, 1))).fetchone()["id"]
    conn.execute("INSERT INTO catalog.contribution (person_id, role, item_id, position, source) VALUES (%s, %s, %s, 1, 'system')",
                 (_person(conn, maker), role, it))
    conn.execute("INSERT INTO library.entry (item_id, section_id) VALUES (%s, %s)", (it, section))
    return it


def _finish(conn, item, status, ended, **cols):
    """A reading/watch in the given state, then the entry's status (checked together at commit)."""
    if status == "planned":
        return
    d = datetime.date.fromisoformat(ended) if ended else None
    names = ["item_id", "state", "ended_on", "ended_precision", "started_on", "started_precision", "source"] + list(cols)
    vals = [item, status, d if status in ("completed", "dropped") else None, "day" if d and status in ("completed", "dropped") else None,
            (d - datetime.timedelta(days=9)) if d else datetime.date.today(), "day", "system"] + list(cols.values())
    conn.execute("INSERT INTO library.consumption ({}) VALUES ({})".format(", ".join(names), ", ".join(["%s"] * len(vals))), vals)


def add(conn, log=print):
    with db.write_tx(conn, "system", "test-data", reason="made-up items for trying the console"):
        section = conn.execute("SELECT id FROM library.section WHERE kind = 'test'").fetchone()["id"]
        n = 0
        for title, author, pages, lang, status, finished in BOOKS:
            if _exists(conn, "book", title):
                continue
            it = _item(conn, "book", title, 2020, author, "author", section, status, lang)
            conn.execute("INSERT INTO catalog.book (item_id) VALUES (%s)", (it,))
            ed = conn.execute("INSERT INTO catalog.edition (work_id, title, language, pages, format, is_primary)"
                              " VALUES (%s, %s, %s, %s, 'print', true) RETURNING id", (it, PREFIX + title, lang, pages)).fetchone()["id"]
            _finish(conn, it, status, finished, edition_id=ed, medium="print")
            conn.execute("UPDATE library.entry SET status = %s WHERE item_id = %s", (status, it))
            n += 1
        for title, director, year, runtime, status, watched in MOVIES:
            if _exists(conn, "movie", title):
                continue
            it = _item(conn, "movie", title, year, director, "director", section, status, "eng")
            conn.execute("INSERT INTO catalog.movie (item_id, release_date, runtime_min) VALUES (%s, %s, %s)",
                         (it, datetime.date(year, 6, 1), runtime))
            _finish(conn, it, status, watched, platform="netflix" if status == "completed" else None)
            conn.execute("UPDATE library.entry SET status = %s WHERE item_id = %s", (status, it))
            n += 1
        for title, creator, year, seasons in SERIES:
            if _exists(conn, "series", title):
                continue
            it = _item(conn, "series", title, year, creator, "creator", section, "planned", "eng")
            conn.execute("INSERT INTO catalog.series (item_id, first_air_date, season_count, episode_count, episode_runtime_min,"
                         " status) VALUES (%s, %s, %s, %s, 45, 'ended')",
                         (it, datetime.date(year, 3, 1), len(seasons), sum(s[1] for s in seasons)))
            last = "planned"
            for number, episodes, seen, state, ended in seasons:
                sid = conn.execute("INSERT INTO catalog.media_item (kind, title) VALUES ('season', %s) RETURNING id",
                                   ("Season {}".format(number),)).fetchone()["id"]
                conn.execute("INSERT INTO catalog.season (item_id, series_id, season_number, episode_count) VALUES (%s, %s, %s, %s)",
                             (sid, it, number, episodes))
                if state:
                    _finish(conn, it, state, ended, season_id=sid, episodes=seen, platform="prime_video")
                    last = state
            conn.execute("UPDATE library.entry SET status = %s WHERE item_id = %s", (last, it))
            n += 1
    log("added {} test item{} to the Test section".format(n, "" if n == 1 else "s") if n else "the Test section already has them")
    return n


def remove(conn, media_dir, log=print):
    """Every item in the Test section (the made-up ones, and anything moved there) goes for good."""
    from .web import writes
    rows = conn.execute("""SELECT i.id, i.title FROM library.entry e JOIN catalog.media_item i ON i.id = e.item_id
                            JOIN library.section s ON s.id = e.section_id AND s.kind = 'test'""").fetchall()
    for r in rows:
        with db.write_tx(conn, "owner", "test-data", reason="test data removed"):
            conn.execute("UPDATE library.entry SET deleted_at = now() WHERE item_id = %s", (r["id"],))
            conn.execute("UPDATE catalog.media_item SET deleted_at = now() WHERE id = %s", (r["id"],))
        writes.purge_item(conn, "test-data", r["id"], media_dir)
    log("removed {} item{} from the Test section".format(len(rows), "" if len(rows) == 1 else "s"))
    return len(rows)
