"""Small text helpers shared by the importer and the metadata fill."""
import datetime
import html
import re
import unicodedata

# Language names as written in the owner's notes -> ISO 639-3. Includes the
# typos found in the snapshot ("Portoguese", "Portouguese").
LANGUAGE_NAMES = {
    "english": "eng", "italian": "ita", "danish": "dan", "french": "fra", "german": "deu",
    "portuguese": "por", "portoguese": "por", "portouguese": "por", "portugese": "por",
    "spanish": "spa", "polish": "pol", "greek": "ell", "ancient greek": "grc", "latin": "lat",
    "chinese": "zho", "japanese": "jpn", "korean": "kor", "norwegian": "nor", "swedish": "swe",
    "hebrew": "heb", "russian": "rus", "dutch": "nld", "czech": "ces", "hungarian": "hun",
    "finnish": "fin", "icelandic": "isl", "arabic": "ara", "turkish": "tur", "hindi": "hin",
    "catalan": "cat",
    "italiano": "ita", "dansk": "dan", "inglese": "eng",
}
# Calibre / MARC codes that differ from ISO 639-3
LANGUAGE_CODES = {"fre": "fra", "ger": "deu", "dut": "nld", "gre": "ell", "chi": "zho", "cze": "ces",
                  "ice": "isl", "nob": "nob", "nno": "nor"}

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})


def language_code(name):
    """'Italian' -> 'ita'; 'ita'/'fre' -> ISO 639-3; unknown -> None."""
    if not name:
        return None
    key = name.strip().lower()
    if key in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[key]
    if re.fullmatch(r"[a-z]{3}", key):
        return LANGUAGE_CODES.get(key, key)
    return None


def norm(text):
    """Matching key: no accents, lower case, letters/digits/spaces only."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("ø", "o").replace("Ø", "o").replace("æ", "ae").replace("Æ", "ae") \
               .replace("ß", "ss").replace("ł", "l").replace("Ł", "l")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", text.lower())).strip()


def html_to_text(value):
    """Calibre 'comments' HTML -> plain text with paragraphs. Plain text is
    returned unchanged (stripped)."""
    if value is None:
        return None
    s = str(value)
    if not re.search(r"<[a-zA-Z/][^>]*>", s) and "&" not in s:
        return s.strip() or None
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|li|blockquote)>", "\n\n", s)
    s = re.sub(r"(?i)<li\b[^>]*>", "• ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = "\n".join(line.strip() for line in s.splitlines())
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s or None


def parse_notes(notes):
    """Yamtrack notes written by cwa_migrate.py:
    'By: A & B · Read in: Italian · Original language: Portuguese · Genre: Novel · Pages: 201 — review'.
    Returns {'fields': {key: value}, 'review': text or None}. Text without the
    prefix is all review."""
    notes = notes or ""
    head, sep, tail = notes.partition(" — ")
    fields = {}
    parts = head.split(" · ")
    if parts and re.match(r"^[A-Z][A-Za-z ]{1,30}: ", parts[0]):
        for part in parts:
            key, colon, value = part.partition(": ")
            if not colon:
                return {"fields": {}, "review": notes.strip() or None}
            fields[key.strip()] = value.strip()
        review = tail if sep else ""
    else:
        review = notes
    return {"fields": fields, "review": html_to_text(review) if review.strip() else None}


def first_last(name):
    """'Lewis, Thomas' -> 'Thomas Lewis' (Open Library style); others unchanged."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if name.count(",") == 1:
        last, first = (p.strip() for p in name.split(","))
        if first and last and " " not in last and not re.fullmatch(r"(?i)(jr|sr|phd)\.?", first):
            return "{} {}".format(first, last)
    return name


def split_people(value):
    """'A & B', 'A, B', 'A og B', 'A and B', 'A| B' -> ['A', 'B']; drops 'Unknown'."""
    if not value:
        return []
    parts = re.split(r"\s*(?:\||&|;|,| and | og | e )\s*", value)
    out = []
    for p in parts:
        p = p.strip()
        if p and p.lower() not in ("unknown", "ukendt", "sconosciuto") and p not in out:
            out.append(p)
    return out


def split_genres(value):
    """'Non-fiction, Psychology' / 'Novel; Various' -> ['Non-fiction', 'Psychology']."""
    out = []
    for g in re.split(r"\s*[,;]\s*", value or ""):
        g = g.strip()
        if g and g.lower() not in (x.lower() for x in out):
            out.append(g)
    return out


def isbn10_to_13(isbn10):
    core = "978" + isbn10[:9]
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(core))
    return core + str((10 - total % 10) % 10)


def isbn10_ok(v):
    if not re.fullmatch(r"[0-9]{9}[0-9X]", v):
        return False
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(v))
    return total % 11 == 0


def isbn13_ok(v):
    if not re.fullmatch(r"97[89][0-9]{10}", v):
        return False
    return sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(v)) % 10 == 0


def normalize_isbn(value):
    """Any ISBN-10/13 spelling -> valid ISBN-13, or None if invalid."""
    v = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    if len(v) == 10 and isbn10_ok(v):
        return isbn10_to_13(v)
    if len(v) == 13 and isbn13_ok(v):
        return v
    return None


def parse_partial_date(value):
    """Open Library / free-form dates -> (date, precision) or None.
    '2019' -> year; '2019-05' / 'May 2019' / 'December, 2019' -> month;
    '2019-05-07' / 'May 7, 2019' -> day; '07/05/2019' (ambiguous) -> year."""
    if not value:
        return None
    s = str(value).strip()
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return datetime.date(int(m.group(1)), 1, 1), "year"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return datetime.date(*map(int, m.groups())), "day"
        except ValueError:
            return datetime.date(int(m.group(1)), 1, 1), "year"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return datetime.date(int(m.group(1)), int(m.group(2)), 1), "month"
    m = re.fullmatch(r"([A-Za-z]+)\.?,?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m.group(1).lower() in MONTHS:
        try:
            return datetime.date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))), "day"
        except ValueError:
            pass
    m = re.fullmatch(r"([A-Za-z]+)\.?,?\s+(\d{4})", s)
    if m and m.group(1).lower() in MONTHS:
        return datetime.date(int(m.group(2)), MONTHS[m.group(1).lower()], 1), "month"
    m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", s)
    if m:
        return datetime.date(int(m.group(1)), 1, 1), "year"
    return None


def title_similarity(a, b):
    """Token overlap of two titles, 0..1."""
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
