-- 0001 base: schemas, extensions, domains, helper functions, collations,
-- lookup tables. DESIGN.md §4.1.
-- Runs as ml_owner (the migrator does SET ROLE ml_owner), in one transaction.

REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA ext;      -- extensions
CREATE SCHEMA catalog;  -- what things are
CREATE SCHEMA library;  -- the owner's side
CREATE SCHEMA audit;    -- append-only change log
CREATE SCHEMA ingest;   -- import runs, raw source records
CREATE SCHEMA stats;    -- read-only views for Visuals

-- Trusted extensions (PG 13+): the database owner may create them.
CREATE EXTENSION unaccent WITH SCHEMA ext;
CREATE EXTENSION pg_trgm WITH SCHEMA ext;

-- ---------------------------------------------------------------- functions

CREATE FUNCTION catalog.isbn13_ok(v text) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN v ~ '^97[89][0-9]{10}$'
   AND (SELECT sum(substr(v, i, 1)::int * CASE WHEN i % 2 = 0 THEN 3 ELSE 1 END)
          FROM generate_series(1, 13) AS i) % 10 = 0;
COMMENT ON FUNCTION catalog.isbn13_ok(text) IS 'ISBN-13: 13 digits, prefix 978/979, valid check digit';

-- A partial date: both null, or a date that is normalised for its precision
-- (year: 1 January, month: day 1).
CREATE FUNCTION catalog.date_ok(d date, p text) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (d IS NULL) = (p IS NULL)
   AND (d IS NULL
        OR p = 'day'
        OR (p = 'month' AND extract(day FROM d) = 1)
        OR (p = 'year' AND extract(month FROM d) = 1 AND extract(day FROM d) = 1));

-- unaccent with the dictionary named explicitly, so it is safe to mark
-- IMMUTABLE and use in indexes (if unaccent.rules ever changes, reindex).
CREATE FUNCTION catalog.f_unaccent(text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN ext.unaccent('ext.unaccent'::regdictionary, $1);

-- Matching key for names: no accents, lower case, letters and digits only.
-- "José Saramago" = "Jose  Saramago"; "J.K. Rowling" = "J. K. Rowling".
CREATE FUNCTION catalog.name_key(text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN regexp_replace(lower(catalog.f_unaccent($1)), '[^[:alnum:]]+', '', 'g');

-- Title without its leading article, by the title's language (ISO 639-3).
-- Unknown language: only English "The". Never returns an empty string.
CREATE FUNCTION catalog.make_sort_title(t text, lang text) RETURNS text
    LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
AS $$
DECLARE
    pattern text := CASE lang
        WHEN 'eng' THEN '^(the|a|an)\s+'
        WHEN 'ita' THEN '^((il|lo|la|i|gli|le|un|uno|una)\s+|(l|un)[''’]\s*)'
        WHEN 'dan' THEN '^(den|det|de|en|et)\s+'
        WHEN 'nor' THEN '^(den|det|de|en|ei|et)\s+'
        WHEN 'nob' THEN '^(den|det|de|en|ei|et)\s+'
        WHEN 'swe' THEN '^(den|det|de|en|ett)\s+'
        WHEN 'fra' THEN '^((le|la|les|un|une)\s+|l[''’]\s*)'
        WHEN 'deu' THEN '^(der|die|das|ein|eine)\s+'
        WHEN 'spa' THEN '^(el|la|los|las|un|una)\s+'
        WHEN 'por' THEN '^(o|a|os|as|um|uma)\s+'
        ELSE '^the\s+'
    END;
    s text;
BEGIN
    IF t IS NULL THEN
        RETURN NULL;
    END IF;
    -- leading quotes and punctuation do not count ("'Salem's Lot" -> S)
    s := regexp_replace(btrim(t), '^[[:punct:][:space:]«»“”‘’¿¡]+', '');
    s := btrim(regexp_replace(s, pattern, '', 'i'));
    RETURN CASE WHEN s = '' THEN btrim(t) ELSE s END;
END;
$$;

-- "José Saramago" -> "Saramago, José"; "James Tiptree Jr." -> "Tiptree, James Jr.".
CREATE FUNCTION catalog.make_sort_name(n text) RETURNS text
    LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
AS $$
DECLARE
    parts text[] := regexp_split_to_array(btrim(n), '\s+');
    k int;
    suffix text := '';
BEGIN
    IF n IS NULL THEN
        RETURN NULL;
    END IF;
    IF position(',' IN n) > 0 OR array_length(parts, 1) < 2 THEN
        RETURN btrim(n);
    END IF;
    k := array_length(parts, 1);
    IF lower(parts[k]) IN ('jr', 'jr.', 'sr', 'sr.', 'phd', 'ii', 'iii', 'iv') AND k > 2 THEN
        suffix := ' ' || parts[k];
        k := k - 1;
    END IF;
    RETURN parts[k] || ', ' || array_to_string(parts[1:k - 1], ' ') || suffix;
END;
$$;

-- ------------------------------------------------------------------ domains

CREATE DOMAIN catalog.label AS text
    CHECK (VALUE = btrim(VALUE) AND VALUE <> '' AND length(VALUE) <= 1000
          );
CREATE DOMAIN catalog.code AS text CHECK (VALUE ~ '^[a-z][a-z0-9_]{0,39}$');
CREATE DOMAIN catalog.lang_code AS text CHECK (VALUE ~ '^[a-z]{3}$');
CREATE DOMAIN catalog.isbn13 AS text CHECK (catalog.isbn13_ok(VALUE));
CREATE DOMAIN catalog.sha256_hex AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');
CREATE DOMAIN catalog.md5_hex AS text CHECK (VALUE ~ '^[0-9a-f]{32}$');
CREATE DOMAIN catalog.precision AS text CHECK (VALUE IN ('day', 'month', 'year'));
CREATE DOMAIN catalog.rating AS numeric(3, 1) CHECK (VALUE >= 0 AND VALUE <= 10);
CREATE DOMAIN catalog.percent AS numeric(5, 2) CHECK (VALUE >= 0 AND VALUE <= 100);
CREATE DOMAIN catalog.rel_path AS text
    CHECK (VALUE <> '' AND length(VALUE) <= 1000
           AND VALUE !~ '^/' AND VALUE !~ '(^|/)\.\.(/|$)'
          );
CREATE DOMAIN catalog.http_url AS text
    CHECK (VALUE ~ '^https?://[^[:space:]]+$' AND length(VALUE) <= 2000);
CREATE DOMAIN catalog.long_text AS text
    CHECK (btrim(VALUE) <> '' AND length(VALUE) <= 100000);

-- --------------------------------------------------------------- collations
-- Sorting only; the database default is the builtin C.UTF-8 (stable keys).
-- "-u-kn-true": numbers sort by value ("Volume 2" before "Volume 10").

CREATE COLLATION catalog.sort_und (provider = icu, locale = 'und-u-kn-true');
CREATE COLLATION catalog.sort_da (provider = icu, locale = 'da-u-kn-true');
CREATE COLLATION catalog.sort_it (provider = icu, locale = 'it-u-kn-true');
CREATE COLLATION catalog.sort_en (provider = icu, locale = 'en-u-kn-true');

-- Text search configuration for mixed-language text: no stemming,
-- accent-insensitive (Italian, Danish and English in one library).
CREATE TEXT SEARCH CONFIGURATION catalog.ml_search (COPY = pg_catalog.simple);
ALTER TEXT SEARCH CONFIGURATION catalog.ml_search
    ALTER MAPPING FOR hword, hword_part, word WITH ext.unaccent, simple;

-- ------------------------------------------------------------------ lookups

CREATE TABLE catalog.data_source (
    code   catalog.code PRIMARY KEY,
    label  catalog.label NOT NULL
);
INSERT INTO catalog.data_source (code, label) VALUES
    ('owner',       'Entered by the owner'),
    ('yamtrack',    'Yamtrack'),
    ('cwa',         'Calibre-Web Automated'),
    ('apple',       'Apple Books (iTunes Search)'),
    ('openlibrary', 'Open Library'),
    ('google',      'Google Books'),
    ('wikidata',    'Wikidata'),
    ('tmdb',        'TMDB'),
    ('kosync',      'Reading position sync (kosync)'),
    ('derived',     'Computed by the database'),
    ('system',      'Created by a migration or the system');

CREATE TABLE catalog.language (
    code            catalog.lang_code PRIMARY KEY,   -- ISO 639-3
    bcp47           text NOT NULL CHECK (bcp47 ~ '^[a-z]{2,3}$'),
    name_en         catalog.label NOT NULL UNIQUE,
    sort_collation  text CHECK (sort_collation IN ('und', 'da', 'it', 'en'))
);
INSERT INTO catalog.language (code, bcp47, name_en, sort_collation) VALUES
    ('ita', 'it', 'Italian', 'it'),
    ('dan', 'da', 'Danish', 'da'),
    ('eng', 'en', 'English', 'en'),
    ('fra', 'fr', 'French', NULL),
    ('deu', 'de', 'German', NULL),
    ('por', 'pt', 'Portuguese', NULL),
    ('spa', 'es', 'Spanish', NULL),
    ('cat', 'ca', 'Catalan', NULL),
    ('pol', 'pl', 'Polish', NULL),
    ('ces', 'cs', 'Czech', NULL),
    ('hun', 'hu', 'Hungarian', NULL),
    ('rus', 'ru', 'Russian', NULL),
    ('ell', 'el', 'Greek', NULL),
    ('grc', 'grc', 'Ancient Greek', NULL),
    ('lat', 'la', 'Latin', NULL),
    ('heb', 'he', 'Hebrew', NULL),
    ('ara', 'ar', 'Arabic', NULL),
    ('tur', 'tr', 'Turkish', NULL),
    ('jpn', 'ja', 'Japanese', NULL),
    ('zho', 'zh', 'Chinese', NULL),
    ('kor', 'ko', 'Korean', NULL),
    ('hin', 'hi', 'Hindi', NULL),
    ('nor', 'no', 'Norwegian', 'da'),
    ('nob', 'nb', 'Norwegian Bokmål', 'da'),
    ('swe', 'sv', 'Swedish', NULL),
    ('fin', 'fi', 'Finnish', NULL),
    ('isl', 'is', 'Icelandic', NULL),
    ('nld', 'nl', 'Dutch', NULL);

CREATE TABLE catalog.media_kind (
    code       catalog.code PRIMARY KEY,
    label      catalog.label NOT NULL,
    top_level  boolean NOT NULL    -- can be in the library on its own
);
INSERT INTO catalog.media_kind (code, label, top_level) VALUES
    ('book', 'Book', true),
    ('movie', 'Movie', true),
    ('series', 'Series', true),
    ('season', 'Season', false),
    ('episode', 'Episode', false);

CREATE TABLE catalog.edition_format (
    code   catalog.code PRIMARY KEY,
    label  catalog.label NOT NULL
);
INSERT INTO catalog.edition_format (code, label) VALUES
    ('ebook', 'Ebook'),
    ('print', 'Print'),
    ('paperback', 'Paperback'),
    ('hardcover', 'Hardcover'),
    ('audiobook', 'Audiobook');

CREATE TABLE catalog.file_format (
    code   catalog.code PRIMARY KEY,
    label  catalog.label NOT NULL,
    mime   text NOT NULL
);
INSERT INTO catalog.file_format (code, label, mime) VALUES
    ('epub', 'EPUB', 'application/epub+zip'),
    ('pdf', 'PDF', 'application/pdf'),
    ('mobi', 'MOBI', 'application/x-mobipocket-ebook'),
    ('azw3', 'AZW3', 'application/vnd.amazon.ebook'),
    ('cbz', 'CBZ', 'application/vnd.comicbook+zip'),
    ('m4b', 'M4B audiobook', 'audio/mp4'),
    ('mp3', 'MP3', 'audio/mpeg');

CREATE TABLE catalog.contributor_role (
    code        catalog.code PRIMARY KEY,
    label       catalog.label NOT NULL,
    applies_to  text[] NOT NULL CHECK (applies_to <@ ARRAY['item', 'edition'] AND applies_to <> '{}')
);
INSERT INTO catalog.contributor_role (code, label, applies_to) VALUES
    ('author', 'Author', '{item,edition}'),
    ('translator', 'Translator', '{edition}'),
    ('editor', 'Editor', '{item,edition}'),
    ('illustrator', 'Illustrator', '{item,edition}'),
    ('narrator', 'Narrator', '{edition}'),
    ('foreword', 'Foreword', '{edition}'),
    ('director', 'Director', '{item}'),
    ('writer', 'Writer', '{item}'),
    ('creator', 'Creator', '{item}'),
    ('cast', 'Cast', '{item}');

CREATE TABLE catalog.provider (
    code          catalog.code PRIMARY KEY,
    label         catalog.label NOT NULL,
    applies_to    text[] NOT NULL CHECK (applies_to <@ ARRAY['item', 'edition', 'person'] AND applies_to <> '{}'),
    pattern       text NOT NULL,          -- regular expression for values
    url_template  text CHECK (url_template LIKE 'https://%{}%'),
    legacy        boolean NOT NULL DEFAULT false
);
INSERT INTO catalog.provider (code, label, applies_to, pattern, url_template, legacy) VALUES
    ('openlibrary_edition', 'Open Library edition', '{edition}', '^OL[0-9]+M$', 'https://openlibrary.org/books/{}', false),
    ('openlibrary_work', 'Open Library work', '{item}', '^OL[0-9]+W$', 'https://openlibrary.org/works/{}', false),
    ('openlibrary_author', 'Open Library author', '{person}', '^OL[0-9]+A$', 'https://openlibrary.org/authors/{}', false),
    ('isbn13', 'ISBN-13', '{edition}', '^97[89][0-9]{10}$', NULL, false),
    ('apple_books', 'Apple Books', '{edition}', '^[0-9]{1,20}$', 'https://books.apple.com/book/id{}', false),
    ('google_books', 'Google Books', '{edition}', '^[A-Za-z0-9_-]{12}$', 'https://books.google.com/books?id={}', false),
    ('amazon_asin', 'Amazon ASIN', '{edition}', '^[A-Z0-9]{10}$', NULL, false),
    ('goodreads', 'Goodreads', '{edition,item}', '^[0-9]{1,20}$', NULL, false),
    ('ibdb', 'IBDB', '{edition}', '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', NULL, false),
    ('calibre_uuid', 'Calibre book uuid', '{edition}', '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', NULL, false),
    ('tmdb_movie', 'TMDB movie', '{item}', '^[0-9]{1,12}$', 'https://www.themoviedb.org/movie/{}', false),
    ('tmdb_tv', 'TMDB series', '{item}', '^[0-9]{1,12}$', 'https://www.themoviedb.org/tv/{}', false),
    ('imdb', 'IMDb', '{item}', '^tt[0-9]{7,}$', 'https://www.imdb.com/title/{}/', false),
    ('wikidata', 'Wikidata', '{item,person}', '^Q[0-9]+$', 'https://www.wikidata.org/wiki/{}', false),
    ('yamtrack_item', 'Yamtrack item (legacy)', '{edition,item}', '^[0-9]{1,12}$', NULL, true),
    ('yamtrack_manual', 'Yamtrack manual item (legacy)', '{edition}', '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', NULL, true),
    ('cwa_book', 'CWA book id (legacy)', '{edition}', '^[0-9]{1,12}$', NULL, true);

-- ---------------------------------------------------------- common triggers

CREATE FUNCTION catalog.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;
