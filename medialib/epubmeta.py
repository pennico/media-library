"""Read an EPUB's own metadata (the OPF package document) without unpacking
it: title, authors, language, ISBNs, series, cover image ... for adding a
book from a file, or finding the book a file belongs to.

Safe on untrusted files: everything stays in memory (zipfile +
ElementTree, which never fetches external entities; an OPF that declares
entities is not given to the XML parser at all), the OPF is capped at 2 MB
and the cover at 15 MB. Broken XML gets a repair pass and then a loose,
regex-based pass, so a damaged EPUB still yields what it can; only a file
that is not an EPUB at all raises EpubError.
"""
import datetime
import html
import json
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

from . import text
from .fill.providers import language3

MAX_OPF = 2 * 1024 * 1024
MAX_COVER = 15 * 1024 * 1024
DC = "{http://purl.org/dc/elements/"                # 1.1 (and the old 1.0)
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
NO_NAMES = {"unknown", "ukendt", "sconosciuto", "inconnu", "unbekannt", "desconocido"}
TOOL = re.compile(r"(?i)calibre-ebook\.com|^calibre \(\d")          # dc:contributor 'calibre (9.1.0) [https://...]'
JUNK_TITLE = re.compile(r"(?i)^(unknown|untitled|sconosciuto|senza titolo|ukendt|ingen titel|no title|title|titolo|"
                        r"ebook|book|document\d*)$|\.(epub|docx?|pdf|html?|txt|rtf)$|^microsoft word - ")
IMAGES = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF87a", "image/gif"),
          (b"GIF89a", "image/gif"))
# loose pass for OPFs ElementTree cannot read
LOOSE_DC = re.compile(r"<dc:(\w+)\b([^>]*?)(?:/>|>(.*?)</dc:\1\s*>)", re.S | re.I)
LOOSE_META = re.compile(r"<(?:opf:)?meta\b([^>]*?)(?:/>|>(.*?)</(?:opf:)?meta\s*>)", re.S | re.I)
LOOSE_ITEM = re.compile(r"<(?:opf:)?item\b([^>]*?)/?>", re.I)
ATTR = re.compile(r"([\w:.-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
# file-name noise: '[retail]', calibre's '(123)', '(Italian Edition)', '(z-lib.org)'
FILE_TAG = re.compile(r"(?i)\s*(\[[^\[\]]*\]|\(\d+\)|\([^()]*\b(edition|ed\.|z-lib|retail|epub|ebook|v\d)[^()]*\))\s*$")
ARTICLES = {"the", "a", "an", "il", "lo", "gli", "un", "una", "uno", "den", "det", "die", "das", "el", "los", "las", "les"}
PARTICLES = {"de", "di", "da", "del", "della", "van", "von", "der", "den", "du", "dos", "le", "la", "ibn", "bin"}


class EpubError(ValueError):
    pass


def read(path):
    """An EPUB (path or file object) -> dict of what its OPF says; None or []
    for what it does not say. Raises EpubError when it is not an EPUB."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            opf = _rootfile(z, names)
            if not opf:
                raise EpubError("not an EPUB (no OPF package document)")
            data = _member(z, opf, MAX_OPF)
            if data is None:
                raise EpubError("the EPUB's OPF is unreadable or larger than {} bytes".format(MAX_OPF))
            entries, items = _parse(data)
            out = _metadata(entries)
            out["cover"] = _cover(z, names, opf, items, entries)
            return out
    except zipfile.BadZipFile:
        raise EpubError("not an EPUB (not a zip file)") from None


def junk_title(title):
    """True for OPF titles that say nothing ('Unknown', a file name, ...):
    then title_from_filename() is the better guess."""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    return not t or bool(JUNK_TITLE.search(t))


def title_from_filename(name):
    """File name -> (title, author or None), a guess:
    "Il viaggio dell'elefante - Jose Saramago.epub" -> ("Il viaggio dell'elefante", 'Jose Saramago');
    'Saramago, Jose - Il viaggio.epub' -> ('Il viaggio', 'Jose Saramago').
    Calibre's 'Title - Author' order wins when both halves could be a name."""
    base = posixpath.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"(?i)(\.kepub)?\.(epub|pdf|mobi|azw3?|fb2|txt)$", "", base).replace("_", " ")
    while FILE_TAG.search(base) and FILE_TAG.sub("", base).strip():
        base = FILE_TAG.sub("", base)
    base = re.sub(r"\s+", " ", base).strip(" .-–—")
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", base) if p.strip()]
    if len(parts) < 2:
        return base or None, None
    if _name_like(parts[0]) and not _name_like(parts[-1]):
        author, title = parts[0], " - ".join(parts[1:])
    else:
        author, title = parts[-1], " - ".join(parts[:-1])
    author = text.first_last(author)
    return title, (None if author.lower() in NO_NAMES else author)


def _name_like(s):
    """'Jose Saramago', 'Saramago, Jose', 'J. R. R. Tolkien': a few capitalised words."""
    if re.search(r"\d", s):
        return False
    if s.count(",") == 1:
        return all(p.strip() for p in s.split(","))
    words = s.split()
    return 2 <= len(words) <= 5 and words[0].lower() not in ARTICLES \
        and all(w[0].isupper() or w.lower() in PARTICLES for w in words)


# ------------------------------------------------------------------ zip parts

def _member(z, name, cap):
    """Bytes of one zip entry; None when missing, broken or bigger than `cap`."""
    try:
        info = z.getinfo(name)
        if info.file_size > cap:
            return None
        with z.open(info) as fh:
            data = fh.read(cap + 1)
        return data if len(data) <= cap else None
    except Exception:  # noqa: BLE001 - zipfile/zlib raise many kinds for damaged or encrypted entries
        return None


def _find(names, path):
    """A zip entry by path, exact or ignoring case (EPUBs made on Windows)."""
    if path in names:
        return path
    low = path.lower()
    return next((n for n in names if n.lower() == low), None)


def _rootfile(z, names):
    """META-INF/container.xml -> the OPF's path; any .opf when that is broken."""
    cont = _find(names, "META-INF/container.xml")
    raw = (_member(z, cont, 64 * 1024) or b"") if cont else b""
    for m in re.finditer(rb"<(?:\w+:)?rootfile\b[^>]*?full-path\s*=\s*[\"']([^\"']+)[\"']", raw):
        found = _find(names, html.unescape(m.group(1).decode("utf-8", "replace")))
        if found:
            return found
    opfs = sorted((n for n in names if n.lower().endswith(".opf")), key=lambda n: (n.count("/"), n))
    return opfs[0] if opfs else None


# ------------------------------------------------------------------- parsing

def _parse(data):
    """OPF bytes -> (entries, items): every Dublin Core and meta element as
    (local name, {attribute local name: value}, text), and the manifest items'
    attributes. Strict XML first, then repaired XML, then the loose pass."""
    if b"<!ENTITY" not in data:
        for attempt in (lambda: data, lambda: _repair(data)):
            try:
                return _from_tree(ET.fromstring(attempt()))
            except (ET.ParseError, ValueError):
                continue
    return _loose(_decode(data))


def _decode(data):
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def _repair(data):
    """The usual damage: HTML entities (&nbsp;), bare '&', control characters."""
    s = re.sub(r"^\s*<\?xml[^>]*\?>", "", _decode(data))
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)", "&amp;", s)


def _local(name):
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _from_tree(root):
    entries, items = [], []
    for el in root.iter():
        if not isinstance(el.tag, str):              # comments, processing instructions
            continue
        attrs = {_local(k): v for k, v in el.attrib.items()}
        name = _local(el.tag)
        if el.tag.startswith(DC) or name == "meta":
            body = "".join(el.itertext())
            entries.append((name, attrs, body.strip() if name == "description" else _clean(body)))
        elif name == "item":
            items.append(attrs)
    return entries, items


def _loose(s):
    def attrs(raw):
        return {_local(k): html.unescape(a if a or not b else b) for k, a, b in ATTR.findall(raw or "")}

    def body(raw):
        return html.unescape(re.sub(r"<[^>]+>", "", raw or ""))
    entries = [(m.group(1).lower(), attrs(m.group(2)), body(m.group(3)).strip()) for m in LOOSE_DC.finditer(s)]
    entries = [(n, a, t if n == "description" else _clean(t)) for n, a, t in entries]
    entries += [("meta", attrs(m.group(1)), _clean(body(m.group(2)))) for m in LOOSE_META.finditer(s)]
    return entries, [attrs(m.group(1)) for m in LOOSE_ITEM.finditer(s)]


# ------------------------------------------------------------------ metadata

def _metadata(entries):
    refs = {}                                        # EPUB3: '#id' -> {property: [values]}
    metas = []                                       # (name or property, value, attrs)
    for name, a, t in entries:
        if name != "meta":
            continue
        if a.get("refines") and a.get("property"):
            refs.setdefault(a["refines"].lstrip("#"), {}).setdefault(a["property"].lower(), []).append(t)
        elif a.get("name"):
            metas.append((a["name"].lower(), _clean(a.get("content")), a))
        elif a.get("property"):
            metas.append((a["property"].lower(), t, a))

    def ref(a, prop):
        return (refs.get(a.get("id") or "", {}).get(prop) or [None])[0]

    def dc(name):
        return [(a, t) for n, a, t in entries if n == name and t]

    out = {"title": None, "subtitle": None}
    titles = {}
    for a, t in dc("title"):
        titles.setdefault((ref(a, "title-type") or "").lower(), t)
    title = titles.get("main") or titles.get("") or next(iter(titles.values()), None)
    m = re.search(r"\s*\((\w+) edition\)$", title or "", re.I)        # Amazon's '(Italian Edition)'
    out["title"] = title[:m.start()] if m and m.start() and m.group(1).lower() in text.LANGUAGE_NAMES else title
    out["subtitle"] = titles.get("subtitle")
    out["authors"], out["contributors"] = _people(dc("creator"), dc("contributor"), refs)
    out["language"] = next((language3(t) for _, t in dc("language") if language3(t)), None)
    out.update(_identifiers(dc("identifier"), dc("source"), ref))
    out["publisher"] = next((t for _, t in dc("publisher")), None)
    out["published"] = _published(dc("date"))
    out["description"] = next((text.html_to_text(t) for _, t in dc("description")), None)
    out["subjects"] = _unique(t for _, t in dc("subject"))
    out["series"], out["series_index"] = _series(metas, ref)
    out["page_count"] = _pages(metas)
    return out


def _unique(values):
    out = []
    for v in values:
        if v.lower() not in (o.lower() for o in out):
            out.append(v)
    return out


def _people(creators, contributors, refs):
    """dc:creator with role 'aut' or no role -> authors; everyone else with a
    role (opf:role or an EPUB3 refines) -> contributors. Tools are dropped;
    'A and B' / 'A & B' in one element are two people."""
    authors, others = [], []
    for kind, people in (("creator", creators), ("contributor", contributors)):
        for a, t in people:
            roles = [r.strip().lower() for r in ([a["role"]] if a.get("role") else
                                                 refs.get(a.get("id") or "", {}).get("role", [])) if r.strip()]
            parts = re.split(r"\s+(?:and|&)\s+|\s*;\s*", t)
            for name in parts if all(len(p.split()) >= 2 for p in parts) else [t]:
                name = text.first_last(name)
                if not name or name.lower() in NO_NAMES or TOOL.search(name):
                    continue
                if kind == "creator" and (not roles or "aut" in roles):
                    if name not in authors:
                        authors.append(name)
                elif "bkp" not in roles:
                    others.append({"name": name, "role": roles[0] if roles else "ctb"})
    return authors, others


def _identifiers(idents, sources, ref):
    """ISBNs (any spelling, as ISBN-13; invalid ones skipped), the print ISBNs
    named in dc:source, and the uuid of the calibre library the file came from."""
    isbns, source_isbns, calibre, uuids, calibre_id = [], [], None, [], False
    for where, values in ((isbns, idents), (source_isbns, sources)):
        for a, t in values:
            scheme, value = _scheme(a, t)
            if scheme.startswith("isbn") or ref(a, "identifier-type") in ("02", "15") or \
                    (not scheme and re.fullmatch(r"[0-9Xx -]{10,17}", value)):
                isbn = text.normalize_isbn(value)
                if isbn and isbn not in where:
                    where.append(isbn)
            elif where is isbns and scheme == "calibre":
                if UUID.fullmatch(value):
                    calibre = calibre or value.lower()
                calibre_id = calibre_id or value.isdigit()
            elif where is isbns and scheme == "uuid" and UUID.fullmatch(value):
                uuids.append((bool(a.get("id")) and len(values) > 1, value.lower()))
    # calibre writes 'calibre:<library uuid>', or 'calibre:<book id>' next to 'uuid:<library uuid>'
    # (EPUB3: without an id; an id'd 'urn:uuid:' is usually the EPUB's own identifier)
    if not calibre and calibre_id and uuids:
        calibre = next((v for own, v in uuids if not own), uuids[0][1])
    return {"isbns": isbns, "source_isbns": [i for i in source_isbns if i not in isbns], "calibre_uuid": calibre}


def _scheme(a, t):
    """(scheme, value): opf:scheme="ISBN", 'urn:isbn:…', 'isbn:…', 'calibre:…', 'urn:uuid:…'."""
    m = re.match(r"(?:urn:)?([A-Za-z][A-Za-z0-9_-]*):(?!//)\s*(.+)$", t)
    if m and m.group(1).lower() not in ("http", "https"):
        return (a.get("scheme") or m.group(1)).lower(), m.group(2).strip()
    return (a.get("scheme") or "").lower(), t


def _published(dates):
    """dc:date of the publication (never the 'modification' event)."""
    for wanted in ("publication", ""):
        for a, t in dates:
            if (a.get("event") or "").lower() == wanted:
                d = _date(t)
                if d:
                    return d
    return None


def _date(value):
    """'2015' / '2015-03' / '2015-03-12' / calibre's '2015-03-12T00:00:00+00:00' ->
    YYYY / YYYY-MM / YYYY-MM-DD. A UTC time on the hour after noon is a local
    midnight east of UTC (calibre stores '2014-12-31T23:00:00+00:00' for 2015):
    the next day. Calibre's 'no date' (year 101) is dropped."""
    m = re.match(r"\s*([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2})(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?"
                 r"(?:\.[0-9]+)?(Z|[+-]00:?00)?)?)?)?", value or "")
    if not m or not 1000 <= int(m.group(1)) <= 2999:
        return None
    y, mo, d, hh, mi, ss, utc = m.groups()
    try:
        if d:
            day = datetime.date(int(y), int(mo), int(d))
            if utc and hh and int(hh) >= 13 and mi == "00" and (ss or "00") == "00":
                day += datetime.timedelta(days=1)
            return day.isoformat()
        if mo and 1 <= int(mo) <= 12:
            return "{}-{}".format(y, mo)
    except (ValueError, OverflowError):
        pass
    return y


def _series(metas, ref):
    """calibre:series / calibre:series_index, or an EPUB3 belongs-to-collection
    of type 'series' (or untyped; a 'set' is not a series)."""
    get = lambda key: next((v for k, v, _ in metas if k == key and v), None)  # noqa: E731
    if get("calibre:series"):
        return get("calibre:series"), _number(get("calibre:series_index"))
    colls = [(a, v) for k, v, a in metas if k == "belongs-to-collection" and v]
    for wanted in ("series", None):
        for a, v in colls:
            if ((ref(a, "collection-type") or "").lower() or None) == wanted:
                return v, _number(ref(a, "group-position"))
    return None, None


def _number(value):
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not 0 <= n < 100000:
        return None
    return int(n) if n.is_integer() else n


def _pages(metas):
    """schema:numberOfPages, or calibre's '#pages' column (Count Pages plugin)."""
    for key, value, _ in metas:
        n = None
        if key == "schema:numberofpages":
            n = value
        elif key in ("calibre:user_metadata:#pages", "calibre:user_metadata"):
            try:
                col = json.loads(value)
                n = (col.get("#pages") or {} if key == "calibre:user_metadata" else col).get("#value#")
            except (ValueError, AttributeError):
                continue
        n = _number(n)
        if isinstance(n, int) and 0 < n <= 20000:
            return n
    return None


# --------------------------------------------------------------------- cover

def _cover(z, names, opf, items, entries):
    """{'data', 'media_type', 'path'} of the cover image, or None: the EPUB3
    cover-image item, the EPUB2 <meta name="cover">, else an image item whose
    id or href says 'cover' (a back cover last). Only real JPEG/PNG/GIF/WebP."""
    by_id = {a.get("id"): a for a in items if a.get("id")}
    picks = [a for a in items if "cover-image" in (a.get("properties") or "").split()]
    for name, a, _ in entries:
        if name == "meta" and (a.get("name") or "").lower() == "cover" and a.get("content"):
            picks.append(by_id.get(a["content"]) or {"href": a["content"]})   # some write the href there
    named = [a for a in items if (a.get("media-type") or "").startswith("image/")
             and "cover" in ((a.get("id") or "") + (a.get("href") or "")).lower()]
    picks += sorted(named, key=lambda a: "back" in ((a.get("id") or "") + (a.get("href") or "")).lower())
    base, tried = posixpath.dirname(opf), set()
    for a in picks:
        href = urllib.parse.unquote((a.get("href") or "").split("#")[0]).lstrip("/")
        path = href and _find(names, posixpath.normpath(posixpath.join(base, href)))
        if not path or path in tried:
            continue
        tried.add(path)
        data = _member(z, path, MAX_COVER)
        kind = _image_type(data)
        if kind:
            return {"data": data, "media_type": kind, "path": path}
    return None


def _image_type(data):
    if not data:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return next((t for magic, t in IMAGES if data.startswith(magic)), None)
