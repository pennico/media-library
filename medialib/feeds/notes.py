"""Parse review notes (one note per book, audiobook, movie or series).

The note title is the item's title. The kind comes from the sub-notebook
("Books", "Audiobooks", "Movies", "TV-Series") or a ``type:`` line. The
template has two headings::

    # Rating
    4/5
    # Comment
    My comment...

Optional ``key: value`` lines may come first (or under their own heading,
e.g. ``# Date``); a ``---`` line also starts the comment. Everything is
optional except the title.

    type: book            # book | audiobook | movie | tv
    status: finished      # finished | to-read | to-watch | reading | paused | dropped
    by: Kazuo Ishiguro    # author (book), director (movie), creator (tv)
    date: 2026-10-18      # when finished; YYYY-MM-DD, DD/MM/YYYY, YYYY-MM or YYYY
    in: Italian           # language I read/watched it in
    original: Japanese    # original language
    rating: 4/5           # x/5, x/10 or a bare number (<= 5 means /5); stored 0-10
    year: 1982            # release year (helps matching)
    genre: Novel
    pages: 180
    lists: Favourites, Italian classics   # collections (created if new)
    isbn: 9788806...      # or openlibrary: OL123M / tmdb: 603 (an explicit match)
    ---
    My comment...

Same format as the earlier Joplin -> Yamtrack job, so existing notes keep
working. Differences: dates keep their precision (a ``2024`` date is "some
time in 2024", not 31 December), and an audiobook is a reading in the
audiobook medium instead of a list.
"""
import datetime
import re

from .. import text

_STATUS = {
    "finished": "completed", "read": "completed", "watched": "completed", "done": "completed",
    "completed": "completed", "seen": "completed",
    "to-read": "planned", "to read": "planned", "to-watch": "planned", "to watch": "planned",
    "want": "planned", "want-to-read": "planned", "want to read": "planned", "planning": "planned",
    "planned": "planned", "wishlist": "planned",
    "reading": "in_progress", "watching": "in_progress", "in progress": "in_progress",
    "started": "in_progress",
    "dropped": "dropped", "abandoned": "dropped",
    "paused": "paused",
}

_KEYS = {
    "type": "type", "kind": "type",
    "status": "status", "state": "status",
    "by": "by", "author": "by", "director": "by", "authors": "by",
    "date": "date", "finished": "date", "watched on": "date", "read on": "date", "when": "date",
    "in": "read_in", "read in": "read_in", "watched in": "read_in", "language": "read_in", "lang": "read_in",
    "original": "original", "original language": "original",
    "rating": "rating", "score": "rating", "stars": "rating",
    "year": "year", "released": "year", "release year": "year",
    "genre": "genre", "pages": "pages",
    "lists": "lists", "list": "lists",
    "tmdb": "tmdb", "openlibrary": "openlibrary", "hardcover": "hardcover", "isbn": "isbn",
}

_AUDIO = {"audiobook", "audiobooks", "audio book", "audio books", "audiolibro"}
_COMMENT_HEADINGS = {"comment", "comments", "review", "my review", "notes", "commento", "kommentar"}
_TYPES = {"book": "book", "books": "book", "libro": "book",
          "movie": "movie", "movies": "movie", "film": "movie", "films": "movie",
          "tv": "series", "tv show": "series", "tv shows": "series", "show": "series",
          "series": "series", "serie": "series", "tv series": "series", "tv-series": "series",
          "tv-shows": "series"}
_TYPES.update({a: "book" for a in _AUDIO})

_KV = re.compile(r"^\s*([A-Za-z][A-Za-z _-]*?)\s*:\s*(.*?)\s*$")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*?)\s*#*\s*$")


def parse_rating(value):
    """'4/5' -> 8.0, '8/10' -> 8.0, '4' -> 8.0, '7.5' -> 7.5 (0-10 scale)."""
    value = (value or "").strip().replace(",", ".")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(?:/\s*(\d+))?\s*(?:stars?)?$", value)
    if not m:
        return None
    num = float(m.group(1))
    scale = float(m.group(2)) if m.group(2) else (5.0 if num <= 5 else 10.0)
    if scale <= 0 or num > scale:
        return None
    return round(num / scale * 10, 1)


def parse_date(value):
    """'2026-10-18' / '18/10/2026' -> day; '2026-10' -> month; '2026' -> year.
    Returns (date, precision) or None."""
    value = (value or "").strip()
    try:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
        if m:
            d = datetime.date(*(int(x) for x in m.groups()))
            return (d, "day") if 1900 <= d.year <= 2100 else None
        m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", value)
        if m:
            d = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            return (d, "day") if 1900 <= d.year <= 2100 else None
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", value)
        if m:
            d = datetime.date(int(m.group(1)), int(m.group(2)), 1)
            return (d, "month") if 1900 <= d.year <= 2100 else None
        m = re.fullmatch(r"(\d{4})", value)
        if m and 1900 <= int(value) <= 2100:
            return datetime.date(int(value), 1, 1), "year"
    except ValueError:
        return None
    return None


def parse_note(title, body, notebook_path=(), created=None):
    """One note -> dict. ``notebook_path``: notebook titles from the reviews
    notebook down to the note's own (kind hint). ``created``: the note's
    creation time (ISO), the finish date when the note gives none."""
    entry = dict(title=" ".join((title or "").split()), kind=None, status="completed", by="",
                 date=None, date_given=False, read_in="", read_in_code=None, original="",
                 original_code=None, rating=None, year=None, genre="", pages=None, lists=[],
                 comment="", ids={}, audiobook=False, problems=[], warnings=[])
    for name in reversed(list(notebook_path)):
        t = _TYPES.get(name.strip().lower())
        if t:
            entry["kind"] = t
            entry["audiobook"] = name.strip().lower() in _AUDIO
            break

    # HTML comments are the templates' usage notes, never part of the comment.
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    comment, section = [], None        # section: None (top), a key, or "comment"
    for line in body.split("\n"):
        h = _HEADING.match(line)
        if h:
            name = h.group(1).strip().rstrip(":").lower()
            if name in _COMMENT_HEADINGS:
                section = "comment"
                continue
            if _KEYS.get(name):
                section = _KEYS[name]
                continue
        if section == "comment":
            comment.append(line)
            continue
        if line.strip() == "":
            continue
        if line.strip() == "---":
            section = "comment"
            continue
        m = _KV.match(line)
        key = _KEYS.get(m.group(1).strip().lower()) if m else None
        if key:
            _set(entry, key, m.group(2))
        elif section not in (None, "comment"):
            _set(entry, section, line)  # value under its own heading
            section = None
        else:
            comment.append(line)       # free text: part of the comment
            section = "comment"
    entry["comment"] = "\n".join(comment).strip()

    if not entry["title"]:
        entry["problems"].append("the note has no title")
    if not entry["kind"]:
        entry["problems"].append("type unknown: add 'type: book', 'type: movie' or 'type: tv' "
                                 "(or put the note in a Books/Audiobooks/Movies/TV-Series notebook)")
    if entry["status"] == "completed" and not entry["date"] and created:
        d = parse_date(str(created)[:10])
        if d:
            entry["date"] = d
    return entry


def _set(entry, key, raw):
    value = raw.strip()
    if key == "type":
        t = _TYPES.get(value.lower())
        if t:
            entry["kind"] = t
            entry["audiobook"] = value.lower() in _AUDIO
        else:
            entry["problems"].append("unknown type '{}'".format(value))
    elif key == "status":
        s = _STATUS.get(value.lower())
        if s:
            entry["status"] = s
        else:
            entry["problems"].append("unknown status '{}'".format(value))
    elif key == "date":
        d = parse_date(value)
        if d:
            entry["date"], entry["date_given"] = d, True
        elif value:
            entry["problems"].append("unreadable date '{}'".format(value))
    elif key == "rating":
        r = parse_rating(value)
        if r is not None:
            entry["rating"] = r
        elif value:
            entry["problems"].append("unreadable rating '{}'".format(value))
    elif key in ("year", "pages"):
        if re.fullmatch(r"\d{1,5}", value):
            entry[key] = int(value)
        elif value:
            entry["problems"].append("unreadable {} '{}'".format(key, value))
    elif key == "lists":
        seen = {n.lower() for n in entry["lists"]}
        for name in value.split(","):
            name = " ".join(name.split())[:200]
            if name and name.lower() not in seen:
                seen.add(name.lower())
                entry["lists"].append(name)
    elif key in ("read_in", "original"):
        entry[key] = value
        entry[key + "_code"] = text.language_code(value)
        if value and not entry[key + "_code"]:
            entry["warnings"].append("unknown language '{}' (left empty)".format(value))
    elif key == "isbn":
        isbn = text.normalize_isbn(value)
        if isbn:
            entry["ids"]["isbn13"] = isbn
        elif value:
            entry["problems"].append("invalid ISBN '{}'".format(value))
    elif key in ("tmdb", "openlibrary", "hardcover"):
        if value:
            entry["ids"][key] = value.strip()
    else:
        entry[key] = value


def people(by):
    """'A & B, C and D' -> ['A', 'B', 'C', 'D']."""
    return [n.strip() for n in re.split(r"\s*(?:&|,|;|\band\b)\s*", by or "") if n.strip()]
