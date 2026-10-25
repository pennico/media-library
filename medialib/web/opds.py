"""OPDS 1.2 catalog (Atom) of the books that have a stored ebook file, for
Readest and other reading apps.

    /opds                    navigation: Reading, To read, Read, All, Recently added
    /opds/books?shelf=…      acquisition feed (50 per page, rel="next")
    /opds/search.xml         OpenSearch description; /opds/search?q=… results
    /opds/files/<uuid>       the file (served by the web handler)

Covers point at the console's own /covers and /thumbs. Links are relative
to the feed, except the OpenSearch template, which is absolute when the
public origin is configured.
"""
import datetime
import unicodedata
import urllib.parse
from xml.sax.saxutils import escape, quoteattr

PAGE = 50
NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
MIME = {"epub": "application/epub+zip", "pdf": "application/pdf"}
SHELVES = {
    "reading": ("Reading", "en.status IN ('in_progress', 'paused')", "progress_at DESC NULLS LAST, i.sort_title"),
    "to-read": ("To read", "en.status = 'planned'", "i.sort_title"),
    "read": ("Read", "en.status = 'completed'", "last_read DESC NULLS LAST, i.sort_title"),
    "all": ("All books", "true", "i.sort_title"),
    "recent": ("Recently added", "true", "file_added DESC, i.sort_title"),
}
ORDER = ("reading", "to-read", "recent", "read", "all")

BOOKS = """
SELECT i.public_id::text AS id, i.title, i.sort_title, coalesce(e.description, i.description) AS description,
       e.language, e.publisher, e.published_on, e.published_precision, en.status,
       greatest(i.updated_at, e.updated_at, max(f.updated_at), en.updated_at) AS updated,
       max(coalesce(f.added_at, f.created_at)) AS file_added,
       jsonb_agg(jsonb_build_object('id', f.public_id, 'format', f.format, 'size', f.size_bytes)
                 ORDER BY f.id) AS files,
       (SELECT array_agg(p.name::text ORDER BY c.position) FROM catalog.contribution c
          JOIN catalog.person p ON p.id = c.person_id WHERE c.item_id = i.id AND c.role = 'author') AS authors,
       (SELECT array_agg(t.name::text ORDER BY it.position) FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
         WHERE it.item_id = i.id AND t.kind = 'genre' AND t.deleted_at IS NULL) AS genres,
       (SELECT cv.local_path FROM catalog.cover cv WHERE cv.is_chosen AND cv.deleted_at IS NULL AND cv.local_path IS NOT NULL
           AND (cv.edition_id = e.id OR cv.item_id = i.id) LIMIT 1) AS cover,
       (SELECT cv.sha256 FROM catalog.cover cv WHERE cv.is_chosen AND cv.deleted_at IS NULL AND cv.local_path IS NOT NULL
           AND (cv.edition_id = e.id OR cv.item_id = i.id) LIMIT 1) AS cover_sha,
       (SELECT max(c.progress_at) FROM library.consumption c WHERE c.item_id = i.id AND c.deleted_at IS NULL) AS progress_at,
       (SELECT max(c.ended_on) FROM library.consumption c WHERE c.item_id = i.id AND c.state = 'completed'
           AND c.deleted_at IS NULL) AS last_read,
       (SELECT l.bcp47 FROM catalog.language l WHERE l.code = e.language) AS bcp47
  FROM catalog.media_item i
  JOIN library.entry en ON en.item_id = i.id AND en.deleted_at IS NULL
  JOIN catalog.edition e ON e.work_id = i.id AND e.deleted_at IS NULL
  JOIN catalog.edition_file f ON f.edition_id = e.id AND f.storage = 'store' AND f.deleted_at IS NULL
 WHERE i.kind = 'book' AND i.deleted_at IS NULL AND {where}
 GROUP BY i.id, e.id, en.item_id
 ORDER BY {order}
 LIMIT %(limit)s OFFSET %(offset)s
"""

SEARCH = """(lower(catalog.f_unaccent(i.title)) LIKE %(q)s OR lower(catalog.f_unaccent(e.title)) LIKE %(q)s
             OR EXISTS (SELECT 1 FROM catalog.contribution c JOIN catalog.person p ON p.id = c.person_id
                         WHERE c.item_id = i.id AND lower(catalog.f_unaccent(p.name)) LIKE %(q)s))"""


def stamp(v=None):
    v = v or datetime.datetime.now(datetime.timezone.utc)
    if isinstance(v, datetime.date) and not isinstance(v, datetime.datetime):
        v = datetime.datetime(v.year, v.month, v.day, tzinfo=datetime.timezone.utc)
    return v.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def link(rel, href, type_, **extra):
    attrs = "".join(" {}={}".format(k, quoteattr(str(v))) for k, v in extra.items() if v is not None)
    return "<link rel={} href={} type={}{}/>".format(quoteattr(rel), quoteattr(href), quoteattr(type_), attrs)


def feed(fid, title, self_href, kind, entries, links=()):
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<feed xmlns=\"http://www.w3.org/2005/Atom\" xmlns:dc=\"http://purl.org/dc/terms/\""
            " xmlns:opds=\"http://opds-spec.org/2010/catalog\">\n"
            "<id>{}</id>\n<title>{}</title>\n<updated>{}</updated>\n<author><name>media-library</name></author>\n"
            "{}\n{}\n{}\n{}\n{}\n</feed>\n").format(
        escape(fid), escape(title), stamp(), link("self", self_href, kind), link("start", "/opds", NAV),
        link("search", "/opds/search.xml", "application/opensearchdescription+xml"),
        "\n".join(links), "\n".join(entries)).encode("utf-8")


def root(conn):
    counts = {}
    for key in ORDER:
        _, where, _ = SHELVES[key]
        counts[key] = conn.execute(
            "SELECT count(DISTINCT e.id) AS n FROM catalog.media_item i JOIN library.entry en ON en.item_id = i.id"
            " AND en.deleted_at IS NULL JOIN catalog.edition e ON e.work_id = i.id AND e.deleted_at IS NULL"
            " JOIN catalog.edition_file f ON f.edition_id = e.id AND f.storage = 'store' AND f.deleted_at IS NULL"
            " WHERE i.kind = 'book' AND i.deleted_at IS NULL AND " + where).fetchone()["n"]
    entries = []
    for key in ORDER:
        title = SHELVES[key][0]
        entries.append("<entry><title>{}</title><id>urn:medialib:shelf:{}</id><updated>{}</updated>"
                       "<content type=\"text\">{} book{}</content>{}</entry>".format(
                           escape(title), key, stamp(), counts[key], "" if counts[key] == 1 else "s",
                           link("subsection", "/opds/books?shelf=" + key, ACQ)))
    return feed("urn:medialib:root", "Media library", "/opds", NAV, entries)


def entry(r):
    parts = ["<entry>", "<title>{}</title>".format(escape(r["title"])), "<id>urn:uuid:{}</id>".format(r["id"]),
             "<updated>{}</updated>".format(stamp(r["updated"]))]
    for name in r["authors"] or []:
        parts.append("<author><name>{}</name></author>".format(escape(name)))
    if r["bcp47"]:
        parts.append("<dc:language>{}</dc:language>".format(r["bcp47"]))
    if r["publisher"]:
        parts.append("<dc:publisher>{}</dc:publisher>".format(escape(r["publisher"])))
    if r["published_on"]:
        n = {"year": 4, "month": 7}.get(r["published_precision"], 10)
        parts.append("<dc:issued>{}</dc:issued>".format(r["published_on"].isoformat()[:n]))
    for g in r["genres"] or []:
        parts.append("<category term={0} label={0}/>".format(quoteattr(g)))
    if r["description"]:
        parts.append("<summary type=\"text\">{}</summary>".format(escape(r["description"][:2000])))
    if r["cover"]:
        ext = r["cover"].rsplit(".", 1)[-1]
        parts.append(link("http://opds-spec.org/image", "/" + r["cover"],
                          {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")))
        parts.append(link("http://opds-spec.org/image/thumbnail", "/thumbs/{}-320.jpg".format(r["cover_sha"]),
                          "image/jpeg"))
    for f in r["files"]:
        parts.append(link("http://opds-spec.org/acquisition", "/opds/files/{}".format(f["id"]),
                          MIME.get(f["format"], "application/octet-stream"), length=f["size"]))
    parts.append("</entry>")
    return "".join(parts)


def books(conn, shelf, page=1):
    if shelf not in SHELVES:
        return None
    title, where, order = SHELVES[shelf]
    return _acquisition(conn, "urn:medialib:shelf:" + shelf, title, where, order, {},
                        "/opds/books?shelf=" + shelf, page)


def search(conn, q, page=1):
    q = " ".join((q or "").split())[:200]
    if not q:
        return _acquisition(conn, "urn:medialib:search", "Search", "false", "i.sort_title", {}, "/opds/search?q=", page)
    plain = "".join(c for c in unicodedata.normalize("NFKD", q.lower()) if not unicodedata.combining(c))
    like = "%" + plain.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return _acquisition(conn, "urn:medialib:search:" + urllib.parse.quote(q), "Search: " + q, SEARCH,
                        "i.sort_title", {"q": like}, "/opds/search?q=" + urllib.parse.quote(q), page)


def _acquisition(conn, fid, title, where, order, params, base, page):
    page = max(1, page)
    rows = conn.execute(BOOKS.format(where=where, order=order),
                        dict(params, limit=PAGE + 1, offset=(page - 1) * PAGE)).fetchall()
    links = []
    sep = "&" if "?" in base else "?"
    if len(rows) > PAGE:
        links.append(link("next", "{}{}page={}".format(base, sep, page + 1), ACQ))
    if page > 1:
        links.append(link("previous", "{}{}page={}".format(base, sep, page - 1), ACQ))
    self_href = base if page == 1 else "{}{}page={}".format(base, sep, page)
    return feed(fid, title, self_href, ACQ, [entry(r) for r in rows[:PAGE]], links)


def opensearch(origin):
    base = (origin or "").rstrip("/")
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<OpenSearchDescription xmlns=\"http://a9.com/-/spec/opensearch/1.1/\">\n"
            "<ShortName>Media library</ShortName>\n<Description>Search the books with an ebook file</Description>\n"
            "<InputEncoding>UTF-8</InputEncoding>\n<OutputEncoding>UTF-8</OutputEncoding>\n"
            "<Url type={} template={}/>\n</OpenSearchDescription>\n").format(
        quoteattr(ACQ), quoteattr(base + "/opds/search?q={searchTerms}")).encode("utf-8")
