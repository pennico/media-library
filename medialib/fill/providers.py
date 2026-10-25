"""Metadata providers for adding books (DESIGN.md §7).

The easiest path: Apple Books (iTunes Search, no key) for the cover and
basics, Open Library (by ISBN, or a title/author search) for the rest, and
Google Books for categories and editions the others miss. Wikidata and DBC
(Danish libraries) are stubs behind the same interface, so they can be added
later without touching the callers.

Every provider takes a `fetch(url) -> parsed JSON` function, so tests run
offline on recorded answers and the service can cache every response.
"""
import dataclasses
import difflib
import html
import re
import urllib.parse

from .. import text

APPLE_STORES = ("dk", "it", "us")
APPLE_SIZE = "1400x1400bb"
JUNK = re.compile(r"\b(summary|summari[sz]ed|study guide|analysis of|workbook|guide to|sparknotes|cliffsnotes"
                  r"|key takeaways|conversation starters|quick read|book notes)\b")
# summary mills that publish under a brand ("Speedy Reads", "Turbo-Learning", "Moscow Press" summaries)
JUNK_AUTHOR = re.compile(r"\b(reads|summar(y|ies)|learning|study guides?|book notes)$")
GOOGLE_ID = re.compile(r"[A-Za-z0-9_-]{12}")          # catalog.provider 'google_books'
GOOGLE_COVER = "w1200-h1800"                           # fife= size; never larger than the original
# ISO 639-1 (Google Books, EPUB dc:language) -> the ISO 639-3 codes of catalog.language
ISO_639_1 = {"it": "ita", "da": "dan", "en": "eng", "fr": "fra", "de": "deu", "pt": "por", "es": "spa",
             "ca": "cat", "pl": "pol", "cs": "ces", "hu": "hun", "ru": "rus", "el": "ell", "la": "lat",
             "he": "heb", "iw": "heb", "ar": "ara", "tr": "tur", "ja": "jpn", "zh": "zho", "ko": "kor",
             "hi": "hin", "no": "nor", "nb": "nob", "nn": "nor", "sv": "swe", "fi": "fin", "is": "isl",
             "nl": "nld"}
# Open Library subjects that describe the copy or a list, not the book
OL_NOISE = {"accessible book", "protected daisy", "in library", "lending library", "large type books",
            "internet archive wishlist", "open library staff picks", "overdrive", "fiction, general"}
OL_TAG = re.compile(r"[a-z_]+:\S")                     # 'nyt:hardcover-fiction=2021-03-21', 'award:...'


@dataclasses.dataclass
class Candidate:
    provider: str                       # 'apple', 'openlibrary', ...
    title: str
    authors: list
    ids: list                           # [(provider_code, value)], e.g. ('isbn13', '978...')
    language: str = None                # ISO 639-3
    published: str = None               # YYYY / YYYY-MM / YYYY-MM-DD
    pages: int = None
    publisher: str = None
    description: str = None
    cover_url: str = None
    subjects: list = dataclasses.field(default_factory=list)
    work_id: str = None                 # Open Library work key (OL..W)
    score: float = 0.0                  # match confidence 0..1 against the query
    raw: dict = dataclasses.field(default_factory=dict)

    def as_dict(self):
        d = dataclasses.asdict(self)
        d.pop("raw")
        return d


# ------------------------------------------------------------------ matching

def head_title(title):
    """'The Silk Roads: A New History' -> 'The Silk Roads' (as written)."""
    return re.split(r"\s*[:(–—]|\s+-\s+", str(title or ""))[0].strip()


def main_title(title):
    """'The Silk Roads: A New History' -> 'the silk roads' (apple_covers.py)."""
    return text.norm(head_title(title))


def language3(code):
    """'it' / 'it-IT' / 'pt_BR' / 'ita' / 'Italian' -> ISO 639-3; 'und' and unknown -> None."""
    key = re.split(r"[-_]", str(code or "").strip().lower(), maxsplit=1)[0]
    if key in ISO_639_1:
        return ISO_639_1[key]
    if key in ("und", "mul", "zxx", "mis"):
        return None
    return text.language_code(key if len(key) == 3 else str(code or ""))


def _numbers(t):
    return {w for w in t.split() if re.fullmatch(r"\d+|[ivxlc]+", w)}


def title_score(want, got):
    a, b = main_title(want), main_title(got)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 8 and longer.startswith(shorter + " "):
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


def last_name(name):
    parts = text.norm(name).split()
    return parts[-1] if parts else ""


def authors_agree(wanted, got):
    """True when no author was given, or one wanted last name matches one of `got`."""
    if not wanted:
        return True
    for w in wanted:
        lw = last_name(w)
        for g in got:
            lg = last_name(g)
            if lw and lg and (lw == lg or difflib.SequenceMatcher(None, lw, lg).ratio() >= 0.85):
                return True
    return False


def score(query_title, query_authors, cand):
    """0..1; 0 when the title or the author clearly disagree (never a guess)."""
    if JUNK.search(text.norm(cand.title)) or (cand.authors and all(JUNK_AUTHOR.search(text.norm(a)) for a in cand.authors)):
        return 0.0
    s = title_score(query_title, cand.title) if query_title else 0.8
    if s < 1.0 and _numbers(main_title(query_title or "")) != _numbers(main_title(cand.title)):
        return 0.0
    if not authors_agree(query_authors, cand.authors):
        return 0.0
    return round(s, 3)


# ----------------------------------------------------------------- providers

class Provider:
    name = "base"
    available = True

    def __init__(self, fetch):
        self.fetch = fetch

    def search(self, title, author=None, limit=5):
        return []

    def by_isbn(self, isbn13):
        return None


class AppleBooks(Provider):
    name = "apple"

    def _cand(self, r, store):
        art = r.get("artworkUrl100") or ""
        desc = r.get("description") or ""
        return Candidate(
            provider="apple", title=r.get("trackName") or "",
            authors=[a for a in re.split(r"\s*(?:,|&| and | og | e )\s*", r.get("artistName") or "") if a],
            ids=[("apple_books", str(r["trackId"]))] if r.get("trackId") else [],
            published=(r.get("releaseDate") or "")[:10] or None,
            description=text.html_to_text(html.unescape(desc)) if desc else None,
            cover_url=art.replace("100x100bb", APPLE_SIZE) if art else None,
            subjects=[g for g in r.get("genres") or [] if g.lower() not in ("books", "ebooks")],
            raw=dict(r, _store=store))

    def search(self, title, author=None, limit=5):
        out, seen = [], set()
        term = "{} {}".format(main_title(title), author or "").strip()
        for store in APPLE_STORES:
            q = urllib.parse.urlencode({"term": term, "media": "ebook", "country": store, "limit": 10})
            res = self.fetch("https://itunes.apple.com/search?" + q) or {}
            for r in res.get("results") or []:
                if r.get("trackId") in seen:
                    continue
                seen.add(r.get("trackId"))
                out.append(self._cand(r, store))
            if len(out) >= limit:
                break
        return out

    def by_isbn(self, isbn13):
        for store in APPLE_STORES:
            res = self.fetch("https://itunes.apple.com/lookup?" + urllib.parse.urlencode({"isbn": isbn13, "country": store})) or {}
            for r in res.get("results") or []:
                if r.get("trackId"):
                    c = self._cand(r, store)
                    c.ids.append(("isbn13", isbn13))
                    return c
        return None


class OpenLibrary(Provider):
    name = "openlibrary"
    BASE = "https://openlibrary.org"

    def _lang(self, entries):
        for e in entries or []:
            key = (e.get("key") if isinstance(e, dict) else e) or ""
            code = text.language_code(key.rsplit("/", 1)[-1])
            if code:
                return code
        return None

    def by_isbn(self, isbn13):
        return self._edition(self.fetch("{}/isbn/{}.json".format(self.BASE, isbn13)))

    def by_edition(self, olid):
        return self._edition(self.fetch("{}/books/{}.json".format(self.BASE, olid)))

    def _edition(self, ed):
        if not ed or not ed.get("title"):
            return None
        authors = []
        for a in ed.get("authors") or []:
            key = a.get("key") if isinstance(a, dict) else None
            if key:
                au = self.fetch(self.BASE + key + ".json") or {}
                if au.get("name"):
                    authors.append(text.first_last(au["name"]))
        work = (ed.get("works") or [{}])[0].get("key")
        wk = self.fetch(self.BASE + work + ".json") if work else {}
        wk = wk or {}
        if not authors:
            for a in wk.get("authors") or []:
                key = (a.get("author") or {}).get("key")
                au = self.fetch(self.BASE + key + ".json") if key else {}
                if (au or {}).get("name"):
                    authors.append(text.first_last(au["name"]))
        desc = ed.get("description") or wk.get("description")
        if isinstance(desc, dict):
            desc = desc.get("value")
        pd = text.parse_partial_date(ed.get("publish_date"))
        isbns = [text.normalize_isbn(i) for i in (ed.get("isbn_13") or []) + (ed.get("isbn_10") or [])]
        ids = [("openlibrary_edition", ed["key"].rsplit("/", 1)[-1])] if ed.get("key") else []
        ids += [("isbn13", i) for i in dict.fromkeys(i for i in isbns if i)]
        covers = [c for c in ed.get("covers") or [] if isinstance(c, int) and c > 0]
        return Candidate(
            provider="openlibrary", title=ed["title"] + (": " + ed["subtitle"] if ed.get("subtitle") else ""),
            authors=authors, ids=ids, language=self._lang(ed.get("languages")),
            published=_partial_str(pd), pages=ed.get("number_of_pages"),
            publisher=(ed.get("publishers") or [None])[0], description=desc,
            cover_url="https://covers.openlibrary.org/b/id/{}-L.jpg".format(covers[0]) if covers else None,
            subjects=_ol_subjects(wk.get("subjects") or ed.get("subjects")),
            work_id=work.rsplit("/", 1)[-1] if work else None, raw={"edition": ed, "work": wk})

    def search(self, title, author=None, limit=5):
        q = {"title": main_title(title), "limit": limit,
             "fields": "key,title,author_name,first_publish_year,isbn,cover_i,language,edition_key,"
                       "number_of_pages_median,subject,edition_count,readinglog_count"}
        if author:
            q["author"] = author
        res = self.fetch("{}/search.json?{}".format(self.BASE, urllib.parse.urlencode(q))) or {}
        out = []
        for d in res.get("docs") or []:
            isbn = next((text.normalize_isbn(i) for i in d.get("isbn") or [] if text.normalize_isbn(i)), None)
            ids = [("openlibrary_edition", (d.get("edition_key") or [None])[0])] if d.get("edition_key") else []
            if isbn:
                ids.append(("isbn13", isbn))
            out.append(Candidate(
                provider="openlibrary", title=d.get("title") or "", authors=[text.first_last(a) for a in d.get("author_name") or []],
                ids=ids, language=None,      # the search lists every language of the work: no edition language
                published=str(d["first_publish_year"]) if d.get("first_publish_year") else None,
                pages=d.get("number_of_pages_median"),
                cover_url="https://covers.openlibrary.org/b/id/{}-L.jpg".format(d["cover_i"]) if d.get("cover_i") else None,
                subjects=_ol_subjects(d.get("subject")),
                work_id=(d.get("key") or "").rsplit("/", 1)[-1] or None, raw=d))
        return out


def _ol_subjects(values, limit=15):
    """Open Library subjects without the library and list tags, at most `limit`."""
    out = []
    for s in values or []:
        s = s.strip() if isinstance(s, str) else ""
        if s and len(s) <= 100 and not OL_TAG.match(s) and s.lower() not in OL_NOISE \
                and s.lower() not in (o.lower() for o in out):
            out.append(s)
        if len(out) >= limit:
            break
    return out


class GoogleBooks(Provider):
    """Books API v1, one request per search or ISBN (no per-result calls).

    Without a key Google answers 429 to everything: keyless calls share one
    Google project whose daily quota is 0 (checked 2026-10-25). With
    `api_key` the key is part of the URL, so it also lands in the fetch
    cache. Subjects are Google's categories as given ('Fiction / Literary')."""
    name = "google"
    BASE = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self, fetch, api_key=None):
        super().__init__(fetch)
        self.api_key = api_key

    def _volumes(self, q, limit):
        params = {"q": q, "printType": "books", "maxResults": max(1, min(int(limit), 40))}
        if self.api_key:
            params["key"] = self.api_key
        res = self.fetch(self.BASE + "?" + urllib.parse.urlencode(params)) or {}
        return [it for it in res.get("items") or []
                if isinstance(it, dict) and isinstance((it.get("volumeInfo") or {}).get("title"), str)]

    def _cand(self, it):
        v = it["volumeInfo"]
        ids = [("google_books", it["id"])] if GOOGLE_ID.fullmatch(str(it.get("id") or "")) else []
        isbns = [text.normalize_isbn(x.get("identifier")) for x in v.get("industryIdentifiers") or []
                 if isinstance(x, dict) and x.get("type") in ("ISBN_13", "ISBN_10")]
        ids += [("isbn13", i) for i in dict.fromkeys(i for i in isbns if i)]
        pages = v.get("pageCount")
        strings = lambda key: [s.strip() for s in v.get(key) or [] if isinstance(s, str) and s.strip()]  # noqa: E731
        return Candidate(
            provider="google", title=v["title"].strip(),        # the subtitle stays in raw
            authors=list(dict.fromkeys(strings("authors"))), ids=ids,
            language=language3(v.get("language")), published=_google_date(v.get("publishedDate")),
            pages=pages if isinstance(pages, int) and pages > 0 else None,
            publisher=str(v.get("publisher") or "").strip("\"' ") or None,
            description=text.html_to_text(v.get("description")) if isinstance(v.get("description"), str) else None,
            cover_url=_google_cover(v.get("imageLinks")),
            subjects=list(dict.fromkeys(strings("categories"))), raw=it)

    def search(self, title, author=None, limit=5):
        head = head_title(title).replace('"', "")
        if not head:
            return []
        q = 'intitle:"{}"'.format(head)
        last = re.sub(r"[\"()]", "", text.first_last(author or "")).split()
        if last:
            q += " inauthor:" + last[-1]
        return [self._cand(it) for it in self._volumes(q, limit)]

    def by_isbn(self, isbn13):
        cands = [self._cand(it) for it in self._volumes("isbn:" + isbn13, 5)]
        for c in cands:                     # the volume that lists this ISBN
            if ("isbn13", isbn13) in c.ids:
                return c
        for c in cands:                     # or one that lists no ISBN at all
            if not any(p == "isbn13" for p, _ in c.ids):
                c.ids.append(("isbn13", isbn13))
                return c
        return None


def _google_date(value):
    """'2021' / '2021-03' / '2021-03-02' (as given) -> the same, validated."""
    m = re.match(r"[12][0-9]{3}(?:-[0-9]{2}(?:-[0-9]{2})?)?", str(value or ""))
    return _partial_str(text.parse_partial_date(m.group(0))) if m else None


def _google_cover(links):
    """The largest image link over https, without the page-curl effect. For
    the search's thumbnails, fife= asks for a bigger image: zoom=0/2/3 answer
    an 'image not available' placeholder for volumes without a preview."""
    links = links if isinstance(links, dict) else {}
    key = next((k for k in ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail")
                if isinstance(links.get(k), str) and links[k].startswith("http")), None)
    if not key:
        return None
    url = re.sub(r"^http://", "https://", links[key]).replace("&edge=curl", "")
    if key in ("thumbnail", "smallThumbnail") and "/books/content?" in url and "fife=" not in url:
        url = url.replace("zoom=5", "zoom=1") + "&fife=" + GOOGLE_COVER
    return url


class Wikidata(Provider):
    """Stub: original title, original language, first publication for works."""
    name = "wikidata"
    available = False


class DBC(Provider):
    """Stub: Danish library data (DBC / bibliotek.dk), needs credentials."""
    name = "dbc"
    available = False


def _partial_str(pd):
    if not pd:
        return None
    d, p = pd
    return d.isoformat()[:{"year": 4, "month": 7, "day": 10}[p]]


ALL = (AppleBooks, OpenLibrary, GoogleBooks, Wikidata, DBC)
