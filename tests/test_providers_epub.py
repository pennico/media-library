"""Google Books and Open Library subjects on recorded-style answers, and the
EPUB metadata reader on EPUBs built here. Offline, no database."""
import io
import urllib.parse
import zipfile

import pytest
from PIL import Image

from medialib import covers, epubmeta
from medialib.fill import providers as P

# ---------------------------------------------------------------- Google Books

KLARA = {
    "kind": "books#volume", "id": "P4yWzQEACAAJ", "etag": "pQ0rGnN1H5E",
    "selfLink": "https://www.googleapis.com/books/v1/volumes/P4yWzQEACAAJ",
    "volumeInfo": {
        "title": "Klara and the Sun", "subtitle": "A Novel", "authors": ["Kazuo Ishiguro"],
        "publisher": "Faber & Faber", "publishedDate": "2021-03-02",
        "description": "<p><b>From the bestselling author</b> of <i>Never Let Me Go</i>&nbsp;and more.</p><p>Second.</p>",
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780571364886"},
                                {"type": "OTHER", "identifier": "UOM:39015084542217"}],
        "readingModes": {"text": False, "image": False}, "pageCount": 307, "printType": "BOOK",
        "categories": ["Fiction / Literary", "Fiction / Science Fiction / General"],
        "maturityRating": "NOT_MATURE", "allowAnonLogging": False, "contentVersion": "preview-1.0.0",
        "imageLinks": {
            "smallThumbnail": "http://books.google.com/books/content?id=P4yWzQEACAAJ&printsec=frontcover&img=1&zoom=5&source=gbs_api",
            "thumbnail": "http://books.google.com/books/content?id=P4yWzQEACAAJ&printsec=frontcover&img=1&zoom=1&edge=curl&source=gbs_api"},
        "language": "en", "previewLink": "http://books.google.dk/books?id=P4yWzQEACAAJ&dq=x",
        "infoLink": "http://books.google.dk/books?id=P4yWzQEACAAJ&dq=x",
        "canonicalVolumeLink": "https://books.google.com/books/about/Klara_and_the_Sun.html?id=P4yWzQEACAAJ"},
    "saleInfo": {"country": "DK", "saleability": "NOT_FOR_SALE", "isEbook": False},
    "accessInfo": {"country": "DK", "viewability": "NO_PAGES", "embeddable": False},
    "searchInfo": {"textSnippet": "A thrilling book ..."}}

CORPI = {
    "kind": "books#volume", "id": "Arn9mQEACAAJ",
    "volumeInfo": {"title": "I corpi", "authors": ["Alessandro Baricco"], "publisher": "\"Feltrinelli\"",
                   "publishedDate": "2021-10", "industryIdentifiers": [{"type": "ISBN_10", "identifier": "8807895684"}],
                   "pageCount": 0, "categories": ["Fiction"], "language": "it"}}


def google_fetch(answer):
    urls = []

    def fetch(url):
        urls.append(url)
        return answer
    return fetch, urls


def query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_google_search_maps_volumes():
    fetch, urls = google_fetch({"kind": "books#volumes", "totalItems": 2, "items": [KLARA, CORPI]})
    g = P.GoogleBooks(fetch)
    assert g.available
    cands = g.search("Klara and the Sun: A Novel", "Ishiguro, Kazuo", limit=5)
    assert urls[0].startswith("https://www.googleapis.com/books/v1/volumes?")
    assert query(urls[0]) == {"q": 'intitle:"Klara and the Sun" inauthor:Ishiguro', "printType": "books", "maxResults": "5"}
    c = cands[0]
    assert (c.provider, c.title, c.authors) == ("google", "Klara and the Sun", ["Kazuo Ishiguro"])
    assert c.raw["volumeInfo"]["subtitle"] == "A Novel"                     # the title stays the main title
    assert c.ids == [("google_books", "P4yWzQEACAAJ"), ("isbn13", "9780571364886")]
    assert (c.language, c.published, c.pages, c.publisher) == ("eng", "2021-03-02", 307, "Faber & Faber")
    assert c.description == "From the bestselling author of Never Let Me Go and more.\n\nSecond."
    assert c.subjects == ["Fiction / Literary", "Fiction / Science Fiction / General"]
    assert c.cover_url.startswith("https://books.google.com/books/content?id=P4yWzQEACAAJ")
    assert "edge=curl" not in c.cover_url and "fife=" in c.cover_url
    assert P.score("Klara and the Sun", ["Ishiguro"], c) == 1.0
    it = cands[1]
    assert it.ids == [("google_books", "Arn9mQEACAAJ"), ("isbn13", "9788807895685")]     # ISBN-10 -> 13
    assert (it.language, it.published, it.pages, it.publisher) == ("ita", "2021-10", None, "Feltrinelli")


def test_google_by_isbn():
    other = {"id": "zyTCAlFPjgYC", "volumeInfo": {"title": "Klara and the Sun", "industryIdentifiers": [
        {"type": "ISBN_13", "identifier": "9780593318171"}]}}
    fetch, urls = google_fetch({"items": [other, KLARA]})
    c = P.GoogleBooks(fetch, api_key="test-key").by_isbn("9780571364886")
    assert query(urls[0])["q"] == "isbn:9780571364886" and query(urls[0])["key"] == "test-key"
    assert c.ids[0] == ("google_books", "P4yWzQEACAAJ")                     # the volume that lists the ISBN
    bare = {"id": "abcdefghijkl", "volumeInfo": {"title": "No identifiers"}}
    c = P.GoogleBooks(google_fetch({"items": [other, bare]})[0]).by_isbn("9780571364886")
    assert c.title == "No identifiers" and ("isbn13", "9780571364886") in c.ids
    assert P.GoogleBooks(google_fetch({"items": [other]})[0]).by_isbn("9780571364886") is None
    assert P.GoogleBooks(google_fetch(None)[0]).by_isbn("9780571364886") is None


def test_google_missing_fields():
    items = [{"id": "short", "volumeInfo": {"title": "Bare"}},
             {"id": "abcdefghijkl"},                                          # no volumeInfo: skipped
             {"id": "abcdefghijkm", "volumeInfo": {"title": None}},
             {"id": "abcdefghijkn", "volumeInfo": {"title": "Odd", "publishedDate": "2021-13", "language": "zh-CN",
                                                   "authors": ["", None, "A B"], "imageLinks": {"thumbnail": None},
                                                   "industryIdentifiers": [{"type": "ISBN_13", "identifier": "123"}],
                                                   "description": 5, "pageCount": "300"}}]
    bare, odd = P.GoogleBooks(google_fetch({"items": items})[0]).search("Bare")
    assert (bare.title, bare.authors, bare.ids, bare.language, bare.published, bare.cover_url, bare.subjects) == \
        ("Bare", [], [], None, None, None, [])
    assert (odd.published, odd.language, odd.authors, odd.ids) == ("2021", "zho", ["A B"], [("google_books", "abcdefghijkn")])
    assert (odd.description, odd.pages, odd.cover_url) == (None, None, None)
    assert P.GoogleBooks(google_fetch(None)[0]).search("Anything") == []
    assert P.GoogleBooks(google_fetch({"totalItems": 0})[0]).search("Anything", "Someone") == []


def test_language3():
    assert [P.language3(c) for c in ("it", "it-IT", "pt_BR", "ita", "fre", "Italian", "iw", "und", "xx", None)] == \
        ["ita", "ita", "por", "ita", "fra", "ita", "heb", None, None, None]


# ----------------------------------------------------- Open Library subjects

def test_openlibrary_subjects():
    noise = ["Fiction", "nyt:combined-print-and-e-book-fiction=2021-03-21", "Accessible book", "Protected DAISY",
             "Artificial intelligence, fiction", "fiction", "Robots, fiction"] + ["Topic {}".format(i) for i in range(30)]
    urls = []

    def fetch(url):
        urls.append(url)
        if "/search.json" in url:
            return {"docs": [{"key": "/works/OL1W", "title": "Klara and the Sun", "author_name": ["Kazuo Ishiguro"],
                              "subject": noise}]}
        if url.endswith("/isbn/9780571364886.json"):
            return {"key": "/books/OL2M", "title": "Klara and the Sun", "authors": [{"key": "/authors/OL3A"}],
                    "works": [{"key": "/works/OL1W"}], "subjects": ["Edition subject"]}
        if url.endswith("/authors/OL3A.json"):
            return {"name": "Kazuo Ishiguro"}
        if url.endswith("/works/OL1W.json"):
            return {"subjects": noise}
        return None
    ol = P.OpenLibrary(fetch)
    c = ol.search("Klara and the Sun", "Ishiguro")[0]
    assert "subject" in query(urls[0])["fields"].split(",")
    assert c.subjects[:4] == ["Fiction", "Artificial intelligence, fiction", "Robots, fiction", "Topic 0"]
    assert len(c.subjects) == 15
    n = len(urls)
    e = ol.by_isbn("9780571364886")
    assert e.subjects == c.subjects and len(urls) - n == 3                  # no extra request for subjects


# ------------------------------------------------------------------ EPUBs

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="{}" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def image(fmt):
    buf = io.BytesIO()
    Image.new("RGB", (60, 90), (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


def make_epub(tmp_path, opf, files=None, opf_path="OEBPS/content.opf", container=CONTAINER, name="book.epub"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        if container is not None:
            z.writestr("META-INF/container.xml", container.format(opf_path))
        z.writestr(opf_path, opf)
        for k, v in (files or {}).items():
            z.writestr(k, v)
    return str(path)


EPUB2 = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Il viaggio dell'elefante (Italian Edition)</dc:title>
    <dc:creator opf:role="aut" opf:file-as="Saramago, José">Saramago, José</dc:creator>
    <dc:creator opf:role="trl">Rita Desti</dc:creator>
    <dc:contributor opf:role="bkp">calibre (4.22.0) [http://calibre-ebook.com]</dc:contributor>
    <dc:language>it-IT</dc:language>
    <dc:identifier id="bookid" opf:scheme="ISBN">0-571-24935-3</dc:identifier>
    <dc:identifier>urn:isbn:978-88-07-89568-5</dc:identifier>
    <dc:identifier opf:scheme="ISBN13">9780571249350</dc:identifier>
    <dc:identifier opf:scheme="ISBN">12345</dc:identifier>
    <dc:identifier opf:scheme="calibre">78B5FE5C-B6D5-4162-B690-1AAE8E04BA1B</dc:identifier>
    <dc:identifier opf:scheme="AMAZON">880788710X</dc:identifier>
    <dc:publisher>Einaudi</dc:publisher>
    <dc:date opf:event="modification">2021-10-20</dc:date>
    <dc:date opf:event="publication">2014-12-31T23:00:00+00:00</dc:date>
    <dc:description>&lt;p&gt;Un &lt;b&gt;elefante&lt;/b&gt; in viaggio.&lt;/p&gt;</dc:description>
    <dc:subject>Narrativa</dc:subject>
    <dc:subject>narrativa</dc:subject>
    <dc:subject>Viaggi</dc:subject>
    <meta name="cover" content="cover-img"/>
    <meta name="calibre:series" content="Romanzi"/>
    <meta name="calibre:series_index" content="2.0"/>
    <meta name="calibre:user_metadata:#pages" content="{&quot;datatype&quot;: &quot;int&quot;, &quot;#value#&quot;: 201}"/>
  </metadata>
  <manifest>
    <item id="cover-page" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover-img" href="Images/copertina%20grande.jpg" media-type="image/jpeg"/>
  </manifest>
</package>"""

EPUB3 = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" prefix="calibre: https://calibre-ebook.com">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier>
    <dc:identifier>isbn:9781101911815</dc:identifier>
    <dc:identifier>calibre:25</dc:identifier>
    <dc:identifier>uuid:99B247CB-7D1C-4120-A550-DA7534DA6DDA</dc:identifier>
    <dc:identifier id="ebook">978-1-101-87061-7</dc:identifier>
    <dc:identifier>https://example.org/ebooks/memory-police</dc:identifier>
    <dc:source>urn:isbn:9781101870600</dc:source>
    <dc:title id="t1">The Memory Police</dc:title>
    <meta refines="#t1" property="title-type">main</meta>
    <dc:title id="t2">A Novel</dc:title>
    <meta refines="#t2" property="title-type">subtitle</meta>
    <dc:creator id="c1">Stephen Snyder</dc:creator>
    <meta refines="#c1" property="role" scheme="marc:relators">trl</meta>
    <dc:creator id="c2">Yoko Ogawa</dc:creator>
    <meta refines="#c2" property="role" scheme="marc:relators">aut</meta>
    <meta refines="#c2" property="file-as">Ogawa, Yoko</meta>
    <dc:creator>Unknown</dc:creator>
    <dc:contributor id="c3">calibre (9.1.0) [https://calibre-ebook.com]</dc:contributor>
    <meta refines="#c3" property="role" scheme="marc:relators">bkp</meta>
    <dc:language>en-US</dc:language>
    <dc:date>2019-08-13</dc:date>
    <meta property="dcterms:modified">2026-10-14T21:28:32Z</meta>
    <meta property="belongs-to-collection" id="set1">Great Books</meta>
    <meta refines="#set1" property="collection-type">set</meta>
    <meta property="belongs-to-collection" id="s1">Police Stories</meta>
    <meta refines="#s1" property="collection-type">series</meta>
    <meta refines="#s1" property="group-position">3</meta>
    <meta property="schema:numberOfPages">274</meta>
  </metadata>
  <manifest>
    <item id="img1" href="images/backcover.png" media-type="image/png"/>
    <item id="c" href="images/front.png" media-type="image/png" properties="cover-image"/>
  </manifest>
</package>"""


def test_epub2(tmp_path):
    jpeg = image("JPEG")
    r = epubmeta.read(make_epub(tmp_path, EPUB2, {"OEBPS/Images/copertina grande.jpg": jpeg,
                                                  "OEBPS/Text/cover.xhtml": "<html/>"}))
    assert (r["title"], r["subtitle"]) == ("Il viaggio dell'elefante", None)      # Amazon's '(Italian Edition)' dropped
    assert r["authors"] == ["José Saramago"]
    assert r["contributors"] == [{"name": "Rita Desti", "role": "trl"}]          # calibre itself is not a person
    assert r["language"] == "ita"
    assert r["isbns"] == ["9780571249350", "9788807895685"]                       # ISBN-10 -> 13, invalid skipped
    assert r["calibre_uuid"] == "78b5fe5c-b6d5-4162-b690-1aae8e04ba1b"
    assert r["published"] == "2015-01-01"                 # a local midnight east of UTC; never the modification date
    assert (r["publisher"], r["description"]) == ("Einaudi", "Un elefante in viaggio.")
    assert r["subjects"] == ["Narrativa", "Viaggi"]
    assert (r["series"], r["series_index"], r["page_count"]) == ("Romanzi", 2, 201)
    assert r["cover"]["media_type"] == "image/jpeg" and r["cover"]["path"] == "OEBPS/Images/copertina grande.jpg"
    assert r["cover"]["data"] == jpeg and covers.inspect(r["cover"]["data"])["width"] == 60


def test_epub3(tmp_path):
    png = image("PNG")
    r = epubmeta.read(make_epub(tmp_path, EPUB3, {"OEBPS/images/front.png": png, "OEBPS/images/backcover.png": image("PNG")}))
    assert (r["title"], r["subtitle"]) == ("The Memory Police", "A Novel")
    assert r["authors"] == ["Yoko Ogawa"]                                        # refines roles: aut vs trl
    assert r["contributors"] == [{"name": "Stephen Snyder", "role": "trl"}]
    assert r["language"] == "eng" and r["published"] == "2019-08-13"
    assert r["isbns"] == ["9781101911815", "9781101870617"] and r["source_isbns"] == ["9781101870600"]
    assert r["calibre_uuid"] == "99b247cb-7d1c-4120-a550-da7534da6dda"           # 'calibre:<id>' + 'uuid:<uuid>'
    assert (r["series"], r["series_index"], r["page_count"]) == ("Police Stories", 3, 274)   # a 'set' is not a series
    assert r["cover"] == {"data": png, "media_type": "image/png", "path": "OEBPS/images/front.png"}


def test_epub_without_much(tmp_path):
    opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:title>Only a title</dc:title><dc:date>0101-01-01T00:00:00+00:00</dc:date>
      <dc:identifier>urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier></metadata>
      <manifest><item id="x" href="cover.svg" media-type="image/svg+xml" properties="cover-image"/></manifest></package>"""
    r = epubmeta.read(make_epub(tmp_path, opf, {"OEBPS/cover.svg": "<svg/>"}))
    assert r["title"] == "Only a title" and r["authors"] == [] and r["language"] is None
    assert r["published"] is None                                                 # calibre's 'no date'
    assert r["calibre_uuid"] is None and r["isbns"] == [] and r["cover"] is None    # not calibre's uuid; SVG is no cover
    assert r["series"] is None and r["subjects"] == [] and r["page_count"] is None


def test_epub_broken_opf(tmp_path):
    # HTML entities and a bare '&': repaired, then read as XML
    opf = EPUB2.replace("<dc:publisher>Einaudi", "<dc:publisher>Faber & Faber").replace(
        "&lt;p&gt;Un", "Un&nbsp;bel")
    r = epubmeta.read(make_epub(tmp_path, opf, name="a.epub"))
    assert r["publisher"] == "Faber & Faber" and r["description"].startswith("Un bel")
    assert r["authors"] == ["José Saramago"] and r["series"] == "Romanzi"
    # undeclared prefix, unclosed elements: the loose pass still finds the basics and the cover
    jpeg = image("JPEG")
    opf = """<package><metadata><dc:title>Broken &amp; Book</dc:title>
      <dc:creator opf:role="aut">Writer, Ann</dc:creator><dc:language>ita</dc:language>
      <dc:identifier opf:scheme="ISBN">9780571249350</dc:identifier><meta name="cover" content="cv"/>
      <manifest><item id="cv" href="c.jpg" media-type="image/jpeg"/>"""
    r = epubmeta.read(make_epub(tmp_path, opf, {"OEBPS/c.jpg": jpeg}, name="b.epub"))
    assert (r["title"], r["authors"], r["language"], r["isbns"]) == ("Broken & Book", ["Ann Writer"], "ita", ["9780571249350"])
    assert r["cover"]["data"] == jpeg
    # entity declarations never reach the XML parser
    opf = """<?xml version="1.0"?><!DOCTYPE package [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>
      <package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:title>Entities &b;</dc:title></metadata></package>"""
    assert epubmeta.read(make_epub(tmp_path, opf, name="c.epub"))["title"] == "Entities &b;"


def test_epub_container_fallback(tmp_path):
    # no container.xml, or one without a rootfile: any .opf will do
    r = epubmeta.read(make_epub(tmp_path, EPUB3, opf_path="content.opf", container=None, name="a.epub"))
    assert r["title"] == "The Memory Police"
    r = epubmeta.read(make_epub(tmp_path, EPUB3, opf_path="x/package.opf", container="<container/>", name="b.epub"))
    assert r["title"] == "The Memory Police" and r["cover"] is None


def test_not_an_epub(tmp_path, monkeypatch):
    path = tmp_path / "notes.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(epubmeta.EpubError, match="no OPF"):
        epubmeta.read(str(path))
    (tmp_path / "plain.epub").write_bytes(b"not a zip at all")
    with pytest.raises(epubmeta.EpubError, match="not a zip"):
        epubmeta.read(str(tmp_path / "plain.epub"))
    big = make_epub(tmp_path, EPUB2, {"OEBPS/Images/copertina grande.jpg": image("JPEG")}, name="big.epub")
    monkeypatch.setattr(epubmeta, "MAX_COVER", 100)
    assert epubmeta.read(big)["cover"] is None                                   # cover over the cap: skipped
    monkeypatch.setattr(epubmeta, "MAX_OPF", 100)
    with pytest.raises(epubmeta.EpubError, match="larger than"):
        epubmeta.read(big)


def test_title_from_filename():
    assert epubmeta.title_from_filename("Il viaggio dell'elefante - Jose Saramago.epub") == \
        ("Il viaggio dell'elefante", "Jose Saramago")
    assert epubmeta.title_from_filename("Saramago, Jose - Il viaggio.epub") == ("Il viaggio", "Jose Saramago")
    assert epubmeta.title_from_filename("Jose Saramago - Il viaggio dell'elefante.epub") == \
        ("Il viaggio dell'elefante", "Jose Saramago")
    assert epubmeta.title_from_filename("lib/Klara and the Sun - Kazuo Ishiguro (123).epub") == \
        ("Klara and the Sun", "Kazuo Ishiguro")
    assert epubmeta.title_from_filename("Norwegian Wood - Haruki Murakami [retail].kepub.epub") == \
        ("Norwegian Wood", "Haruki Murakami")                                    # calibre order when both could be names
    assert epubmeta.title_from_filename("Candide - Voltaire.epub") == ("Candide", "Voltaire")
    assert epubmeta.title_from_filename("Some_book.epub") == ("Some book", None)
    assert epubmeta.title_from_filename("Il viaggio - Unknown.epub") == ("Il viaggio", None)
    assert [epubmeta.junk_title(t) for t in ("Unknown", "", "Microsoft Word - draft.doc", "book.epub", "1984", "Klara")] == \
        [True, True, True, True, False, False]
