"""Series suggestions for books (D83-D86, DESIGN.md §24).

Fill missing and Suggest series look a book up for the series it belongs to and keep what they find in
``ingest.series_suggestion``. Nothing goes into a series here: the owner applies a suggestion in the console
(Add to "<series>", Create series), through ``medialib/groups.py`` like every other change to a series.

Where a series comes from, strongest first:

1. **Open Library's series** (curated on its works, with a position). Its search answers ``series_name`` and
   ``series_position`` for the book found by ISBN, by its Open Library work, or by title and author (one
   request). One more search per series (``series_key:``) lists the series' works, which finds the other
   books of the library in it (the same title, an author agreeing) with their numbers.
2. **A title** with a series in it, the book's own or the sources' (Apple Books' answers have no series
   field, only titles like "Witchfinder Volume 1: …"): "Eldest (Inheritance, Book 2)", "Eragon: Inheritance,
   Book One", "Book Two of the Inheritance Cycle", "Shadow of the Golden Crane #4". Only with a marker
   (#, Book, Vol., …) and a number.
3. **An edition's free-text series** ("Inheritance cycle ; 2"), only with a number: most unnumbered ones are
   a publisher's series ("Fare l'Europa", "Lijsters").

Never a publisher's series: names with an imprint word ("Penguin Classics", "Everyman's Library", "Oscar
Mondadori", "A Tor book") and numbers above 99 are dropped.
"""
import dataclasses
import re
import urllib.parse

from .. import text
from . import providers as P

MAX_NUMBER = 99
MAX_NAME = 300
SEARCH = "https://openlibrary.org/search.json"
FIELDS = "key,title,subtitle,author_name,first_publish_year,series_key,series_name,series_position"

WORD_NUMBERS = {w: n for n, w in enumerate(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen"
    " eighteen nineteen twenty".split(), 1)}
WORD_NUMBERS.update({w: n for n, w in enumerate("first second third fourth fifth sixth seventh eighth ninth tenth".split(), 1)})
ROMAN = re.compile(r"(?=[ivxl])(xl|l?x{0,3})(ix|iv|v?i{0,3})", re.I)
NUM = r"(?:\d{1,3}(?:\.\d)?|[ivxl]{1,6}|" + "|".join(sorted(WORD_NUMBERS, key=len, reverse=True)) + r")"
# the words that mark a number as a place in a series
MARK = (r"(?:book|bk|volume|vol|v|no|nr|number|part|pt|tome|tomo|libro|bind|bog|band|teil|del|parte|episode|nummer)")
ARTICLES = {"the", "a", "an", "il", "lo", "la", "i", "gli", "le", "l", "den", "det", "de", "der", "die", "das", "el",
            "los", "las", "les", "un", "une", "ein", "eine"}
GENERIC = {"series", "serie", "serien", "saga", "cycle", "trilogy", "trilogia", "trilogie", "trilogien", "sequence"}
# a name ending like this was cut from a sentence ("On the Calculation of Volume II"), not a series
DANGLING = {"of", "the", "a", "an", "and", "in", "on", "to", "for", "with", "di", "del", "della", "dei", "e", "af", "og",
            "i", "til", "von", "und", "der", "de", "du", "et"}
IMPRINT = re.compile(
    r"\b(classics?|library|biblioteca|bibliotheque|bibliothek|bibliotek|collection|collana|collezione|editions?"
    r"|edizion[ei]|paperbacks?|pocket|pockets|tascabil[ei]|oscar|universale|einaudi|adelphi|feltrinelli|mondadori"
    r"|garzanti|penguin|pelican|vintage|everyman|everymans|bantam|signet|puffin|picador|folio|reclam|dtv|rororo"
    r"|suhrkamp|gyldendal|fischer|heyne|goldmann|bestsellers?|readers?|studies|lectures|monographs?|textbooks?"
    r"|handbooks?|large print|book club|graded)\b|^an? .+ books?$")


# ------------------------------------------------------------------ names and numbers

def key(name):
    """The name folded for matching: no accents, lower case, no leading article, no trailing "series", "cycle",
    "saga", "trilogy" ("The Inheritance Cycle" and "Inheritance" -> 'inheritance')."""
    words = text.norm(name).split()
    while len(words) > 1 and words[0] in ARTICLES:
        words.pop(0)
    while len(words) > 1 and words[-1] in GENERIC:
        words.pop()
    return " ".join(words)


def number(value):
    """'2' / '2.5' / 'II' / 'two' / 'Second' -> 2 / 2.5 / 2 / 2 / 2; '1-2' (an omnibus), '' and the rest -> None."""
    s = str(value or "").strip().lower().rstrip(".")
    if re.fullmatch(r"\d{1,3}(?:\.\d)?", s):
        n = float(s)
    elif s in WORD_NUMBERS:
        n = WORD_NUMBERS[s]
    elif ROMAN.fullmatch(s):
        m = ROMAN.fullmatch(s)
        tens = {"": 0, "x": 10, "xx": 20, "xxx": 30, "xl": 40, "l": 50, "lx": 60, "lxx": 70, "lxxx": 80}[m.group(1).lower()]
        ones = {"": 0, "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9}[m.group(2).lower()]
        n = tens + ones
    else:
        return None
    if not 0 < n < 1000:
        return None
    return int(n) if n == int(n) else n


def _name(s):
    """A series' name as written, without the punctuation around it; None when nothing like a name is left."""
    s = re.sub(r"\s+", " ", str(s or "")).strip(" \t\"'“”‘’«»[](){}")
    s = re.sub(r"[\s,;:.\-–—#/]+$", "", s).strip()
    s = re.sub(r"^[\s,;:.\-–—#/]+", "", s).strip()
    words = text.norm(s).split()
    if not words or words[-1] in DANGLING or len(re.sub(r"[^\w]", "", s)) < 2 or len(s) > MAX_NAME:
        return None
    return s


_FIELD = [
    # "Name #2", "Name, book 2", "Name -- bk. 2", "Name (Book 2)", "Name, v. 2", "Name ; no. 2"
    re.compile(r"^(?P<name>.+?)[\s,;:(\-–—]*(?:#\s*|\b" + MARK + r"\b\.?\s*)(?P<n>" + NUM + r")\)?\.?$", re.I),
    # "Name ; 2", "Name, 2", "Name -- 2"
    re.compile(r"^(?P<name>.+?)\s*(?:;|,|--|–|—)\s*(?P<n>" + NUM + r")\.?$", re.I),
    # "Book 2 of the Inheritance Cycle", "Book Two in the Name series"
    re.compile(r"^" + MARK + r"\.?\s*(?P<n>" + NUM + r")\s+(?:of|in)\s+(?:the\s+)?(?P<name>.+)$", re.I),
]


def parse_field(value, need_number=False):
    """A source's series field -> (name, number or None); None when it holds no name (or no number when needed)."""
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    if not s:
        return None
    for rx in _FIELD:
        m = rx.match(s)
        if m:
            n = number(m.group("n"))
            name = _name(m.group("name"))
            if n is not None and name:
                return name, n
    if need_number:
        return None
    name = _name(s)
    return (name, None) if name else None


def parse_title(title):
    """A series named in a title, with a marker and a number: (name, number) or None.
    "Eldest (Inheritance, Book 2)", "Eldest (The Inheritance Cycle #2)", "Eragon: Inheritance, Book One",
    "Eragon: Book One of the Inheritance Cycle", "Witchfinder Volume 1: In the Service of Angels",
    "Shadow of the Golden Crane #4"."""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if not t:
        return None
    parts = []
    m = re.search(r"[(\[]([^()\[\]]+)[)\]]\s*$", t)                 # a trailing (…) or […]
    if m:
        parts.append(m.group(1))
    m = re.search(r"\s*:\s+|\s+[-–—]\s+", t)                          # "Head: Sub", "Head - Sub"
    head, sub = (t[:m.start()], t[m.end():]) if m else (t, "")
    if sub:
        parts.append(re.sub(r"\s*[(\[][^()\[\]]*[)\]]\s*$", "", sub))   # "Eragon: Inheritance, Book One"
    parts.append(re.sub(r"\s*[(\[][^()\[\]]*[)\]]\s*$", "", head))      # "Witchfinder Volume 1", "Name #4"
    for p in parts:
        r = _marked(p)
        if r:
            return r
    return None


_MARKED = [
    re.compile(r"^(?P<name>.+?)[\s,;(\-–—]*(?:#\s*|\b" + MARK + r"\b\.?\s*)(?P<n>" + NUM + r")\.?$", re.I),
    re.compile(r"^" + MARK + r"\.?\s*(?P<n>" + NUM + r")\s+(?:of|in)\s+(?:the\s+)?(?P<name>.+)$", re.I),
]


def _marked(s):
    s = s.strip()
    for rx in _MARKED:
        m = rx.match(s)
        if m:
            n, name = number(m.group("n")), _name(m.group("name"))
            # "Book 2" alone, or "Book" as the name, says nothing
            if n is not None and name and not re.fullmatch(MARK, name, re.I):
                return name, n
    return None


def plausible(name, n):
    """A book series, not a publisher's: no imprint word, a number of at most 99."""
    k = key(name)
    return bool(k) and k not in GENERIC and not IMPRINT.search(text.norm(name)) and (n is None or 0 < n <= MAX_NUMBER)


# ------------------------------------------------------------------ what a lookup finds

@dataclasses.dataclass
class Found:
    name: str
    number: object                      # int / float / None
    source: str                         # catalog.data_source code: openlibrary, apple, or the title's own source
    how: str                            # 'series' (a series field or list) / 'title' (read from a title)
    ref: str = None                     # Open Library's series key (OL…L)
    raw: str = ""                       # what it was read from

    @property
    def strength(self):
        """3 Open Library's series, 2 a title, 1 an edition's free text."""
        return 3 if self.ref else 2 if self.how == "title" else 1


def merged(found):
    """One suggestion per name key, the strongest source's name, the first number any of them gives;
    the publisher's series dropped. Strongest first."""
    out = {}
    for f in sorted(found, key=lambda f: -f.strength):
        if not plausible(f.name, f.number):
            continue
        k = key(f.name)
        if k not in out:
            out[k] = dataclasses.replace(f)
        else:
            have = out[k]
            if have.number is None and f.number is not None:
                have.number = f.number
            if have.ref is None and f.ref:
                have.ref = f.ref
    return list(out.values())


def from_candidates(cands):
    """What Fill missing's candidates already say: an Apple Books or Open Library title with a series in it, an
    Open Library edition's numbered series field."""
    found = []
    for c in cands or []:
        t = parse_title(c.title)
        if t:
            found.append(Found(t[0], t[1], c.provider, "title", raw=c.title[:1000]))
        ed = (c.raw or {}).get("edition") if c.provider == "openlibrary" else None
        for s in (ed or {}).get("series") or []:
            r = parse_field(s, need_number=True) if isinstance(s, str) else None
            if r:
                found.append(Found(r[0], r[1], "openlibrary", "series", raw=s[:1000]))
    return found


class Finder:
    """Open Library's series for a book and the works of a series, through a `fetch(url) -> JSON` (the fill's
    cached, spaced fetcher; recorded answers in tests)."""

    def __init__(self, fetch):
        self.fetch = fetch

    def search(self, **q):
        res = self.fetch(SEARCH + "?" + urllib.parse.urlencode(dict(q, fields=FIELDS))) or {}
        return [d for d in res.get("docs") or [] if isinstance(d, dict)]

    @staticmethod
    def _series(doc):
        names, keys, pos = (doc.get(k) or [] for k in ("series_name", "series_key", "series_position"))
        for i, name in enumerate(names):
            if isinstance(name, str) and name.strip():
                ref = keys[i] if i < len(keys) and re.fullmatch(r"OL[0-9]+L", str(keys[i])) else None
                yield name.strip(), ref, str(pos[i]) if i < len(pos) and pos[i] is not None else ""

    def for_book(self, book):
        """book: {title, subtitle, title_src, isbn, ol_work, authors}. -> [Found], strongest first."""
        found = []
        own = parse_title(book["title"]) or (parse_title("{}: {}".format(book["title"], book["subtitle"]))
                                             if book.get("subtitle") else None)
        if own:
            found.append(Found(own[0], own[1], book.get("title_src") or "system", "title", raw=book["title"][:1000]))
        docs = []
        if book.get("isbn"):
            docs = self.search(isbn=book["isbn"], limit=3)
        if not docs and book.get("ol_work"):
            docs = self.search(q="key:/works/" + book["ol_work"], limit=1)
        if not docs and book.get("title") and book.get("authors"):          # by title only with an author to agree
            docs = [d for d in self.search(title=P.main_title(book["title"]), author=P.last_name(book["authors"][0]), limit=5)
                    if P.score(book["title"], book["authors"], P.Candidate(
                        "openlibrary", d.get("title") or "", [text.first_last(a) for a in d.get("author_name") or []], [])) >= 0.9]
        for d in docs[:1]:
            for name, ref, pos in self._series(d):
                found.append(Found(name, number(pos), "openlibrary", "series", ref,
                                   "{} #{}".format(name, pos) if pos else name))
            t = parse_title(d.get("title") or "")
            if t:
                found.append(Found(t[0], t[1], "openlibrary", "title", raw=str(d.get("title"))[:1000]))
        return found

    def works(self, ref):
        """The works Open Library has in a series: [{work, title, authors, number}]."""
        out = []
        for d in self.search(q="series_key:" + ref, limit=100):
            pos = next((p for _, r, p in self._series(d) if r == ref), "")
            out.append({"work": str(d.get("key") or "").rsplit("/", 1)[-1], "title": d.get("title") or "",
                        "authors": [text.first_last(a) for a in d.get("author_name") or []], "number": number(pos),
                        "pos": pos})
        return out


# ------------------------------------------------------------------ the library side

BOOKS = """
SELECT i.id, i.public_id::text AS pid, i.title, i.original_title, e.subtitle,
       (SELECT s.code FROM catalog.data_source s WHERE s.code = i.provenance #>> '{title,src}') AS title_src,
       (SELECT x.value FROM catalog.external_id x WHERE x.edition_id = e.id AND x.provider = 'isbn13' ORDER BY x.id LIMIT 1) AS isbn,
       (SELECT x.value FROM catalog.external_id x WHERE (x.item_id = i.id OR x.edition_id = e.id)
           AND x.provider = 'openlibrary_work' ORDER BY x.id LIMIT 1) AS ol_work,
       array(SELECT p.name::text FROM catalog.contribution ct JOIN catalog.person p ON p.id = ct.person_id
              WHERE ct.item_id = i.id AND ct.role = 'author' ORDER BY ct.position) AS authors,
       array(SELECT t.title::text FROM catalog.item_title t WHERE t.item_id = i.id) AS other_titles,
       EXISTS (SELECT 1 FROM catalog.group_member gm JOIN catalog.item_group g ON g.id = gm.group_id AND g.deleted_at IS NULL
                WHERE gm.item_id = i.id) AS grouped
  FROM catalog.media_item i
  JOIN library.entry en ON en.item_id = i.id AND en.deleted_at IS NULL
  LEFT JOIN catalog.edition e ON e.work_id = i.id AND e.is_primary AND e.deleted_at IS NULL
 WHERE i.kind = 'book' AND i.deleted_at IS NULL
"""


def books(conn, pids=None):
    """The library's books (or those named, in the order named), with what a lookup needs."""
    if pids is None:
        return conn.execute(BOOKS).fetchall()
    rows = {r["pid"]: r for r in conn.execute(BOOKS + " AND i.public_id = ANY(%s::uuid[])", (list(pids),)).fetchall()}
    return [rows[p] for p in pids if p in rows]


def members_of(lib, works):
    """The library's books among a series' works: [(item id, number, title)]. The same main title (any of the
    book's titles) and an author agreeing; a book without authors is never matched by title alone."""
    by_title = {}
    for b in lib:
        for t in [b["title"], b["original_title"]] + list(b["other_titles"] or []):
            if t:
                by_title.setdefault(P.main_title(t), []).append(b)
    out, seen = [], set()
    for w in works:
        hits = [b for b in lib if w["work"] and b["ol_work"] == w["work"]]
        hits += [b for b in by_title.get(P.main_title(w["title"]), [])
                 if b["authors"] and w["authors"] and P.authors_agree(b["authors"], w["authors"])]
        for b in hits:
            if b["id"] not in seen:
                seen.add(b["id"])
                out.append((b["id"], w["number"], w["title"]))
    return out


def store(conn, item, found):
    """Keep the suggestions (one per name key). A dismissed one stays dismissed; a known number is not lost."""
    for f in found:
        conn.execute("""INSERT INTO ingest.series_suggestion AS s (item_id, name_key, name, number, source, how, ref, raw)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (item_id, name_key) DO UPDATE
                           SET name = EXCLUDED.name, number = coalesce(EXCLUDED.number, s.number), source = EXCLUDED.source,
                               how = EXCLUDED.how, ref = coalesce(EXCLUDED.ref, s.ref), raw = EXCLUDED.raw, found_at = now()
                         WHERE (s.name, s.number, s.source, s.how, s.ref, s.raw)
                               IS DISTINCT FROM (EXCLUDED.name, coalesce(EXCLUDED.number, s.number), EXCLUDED.source,
                                                 EXCLUDED.how, coalesce(EXCLUDED.ref, s.ref), EXCLUDED.raw)""",
                     (item, key(f.name), f.name[:MAX_NAME], f.number, f.source, f.how, f.ref, (f.raw or "")[:1000] or None))


def suggest(conn, finder, items, client, extra=None):
    """Look the books up (the library's rows from books()) and keep what is found, for them and for the other
    books of the library Open Library puts in the same series. Books already in a series are not looked up.
    `extra`: {item id: [Found]} a caller already has (Fill missing's candidates). Network first, then one
    short transaction. A failing source stops the run (the rest are `left`, not waited on one by one).
    -> {"looked": [pids], "left": [pids], "error": text or None, "found": {pid: [names]}}"""
    from .service import FillError
    from .. import db
    lib, results, refs, looked, error, left = None, {}, {}, [], None, []
    for k, b in enumerate(items):
        if b["grouped"]:
            looked.append(b["pid"])
            continue
        try:
            got = finder.for_book(b) + list((extra or {}).get(b["id"], []))
        except FillError as e:
            error, left = str(e), [x["pid"] for x in items[k:]]
            break
        looked.append(b["pid"])
        results[b["id"]] = merged(got)
        for f in results[b["id"]]:
            if f.ref:
                refs.setdefault(f.ref, f)
    sibling = {}
    for ref, f in refs.items():
        try:
            works = finder.works(ref)
        except FillError:
            continue
        if lib is None:
            lib = books(conn)
        for item, n, title in members_of(lib, works):
            sibling.setdefault(item, []).append(Found(f.name, n, "openlibrary", "series", ref,
                                                      "{} #{}".format(f.name, n) if n is not None else f.name))
    pid = {b["id"]: b["pid"] for b in (lib or [])} | {b["id"]: b["pid"] for b in items}
    out = {}
    with db.write_tx(conn, "openlibrary", client, reason="series suggestions"):
        for item, found in list(results.items()) + list(sibling.items()):
            found = merged(found)
            store(conn, item, found)
            names = out.setdefault(pid[item], [])
            names += [f.name for f in found if f.name not in names]
    return {"looked": looked, "left": left, "error": error, "found": out}
