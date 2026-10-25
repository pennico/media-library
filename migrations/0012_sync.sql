-- 0012 sync: reading apps and feeds. Readest's reading positions (KOReader
-- sync), the other document ids that name a book, and the state of
-- the Joplin Reviews feed. DESIGN.md §11.

CREATE SCHEMA sync;     -- device and feed state (not audited: what it causes in library.* is)

INSERT INTO catalog.data_source (code, label) VALUES ('joplin', 'Joplin Reviews notes');

-- A KOReader document id is the partial MD5 of the copy the reader has. A copy
-- downloaded from CWA can differ from the stored bytes (CWA rewrote its
-- metadata), so CWA's checksum history names the book too; and the owner can
-- link a document no stored file answers to (a copy from elsewhere).
ALTER TABLE catalog.edition_file ADD CONSTRAINT edition_file_id_edition UNIQUE (id, edition_id);
CREATE TABLE catalog.document_alias (
    document         catalog.md5_hex PRIMARY KEY,
    edition_id       bigint NOT NULL REFERENCES catalog.edition,
    edition_file_id  bigint,
    source           catalog.code NOT NULL REFERENCES catalog.data_source,
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (edition_file_id, edition_id) REFERENCES catalog.edition_file (id, edition_id)
);
COMMENT ON TABLE catalog.document_alias IS 'Other KOReader document ids of a book: CWA checksum history (with the file), or linked by the owner';
CREATE INDEX document_alias_edition ON catalog.document_alias (edition_id);
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.document_alias
    FOR EACH ROW EXECUTE FUNCTION audit.capture('document');

-- The ids already imported: every CWA checksum of a file, except the file's own.
INSERT INTO catalog.document_alias (document, edition_id, edition_file_id, source)
SELECT DISTINCT ON (c ->> 'checksum') c ->> 'checksum', f.edition_id, f.id, 'cwa'
  FROM ingest.source_record r
  CROSS JOIN LATERAL jsonb_array_elements(r.payload -> 'checksums') c
  JOIN ingest.id_map m ON m.source = 'cwa.data' AND m.target_table = 'catalog.edition_file'
                      AND m.source_key = r.source_key || ':' || (c ->> 'format')
  JOIN catalog.edition_file f ON f.id = m.target_id AND f.deleted_at IS NULL
 WHERE r.source = 'cwa.books'
   AND jsonb_typeof(r.payload -> 'checksums') = 'array'
   AND c ->> 'checksum' ~ '^[0-9a-f]{32}$'
   AND NOT EXISTS (SELECT 1 FROM catalog.edition_file o WHERE o.kosync_md5 = c ->> 'checksum')
 ORDER BY c ->> 'checksum', f.id;

-- One row per document Readest syncs (one owner, so no user column). A
-- document no book answers to is kept too: devices still sync it, and the
-- console offers to link it.
CREATE TABLE sync.kosync_position (
    document       catalog.md5_hex PRIMARY KEY,
    progress       text NOT NULL CHECK (length(progress) <= 2000),
    percentage     double precision NOT NULL CHECK (percentage >= 0 AND percentage <= 1),
    device         text NOT NULL CHECK (length(device) BETWEEN 1 AND 200),
    device_id      text CHECK (length(device_id) <= 200),
    metadata       jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),  -- title/authors, if the app sends them
    updated_at     timestamptz NOT NULL DEFAULT now(),
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz           -- last time it reached the end; starting over after it is a re-read
);
COMMENT ON TABLE sync.kosync_position IS 'Latest KOReader-sync position per document (Readest); status changes it causes are audited in library.*';

-- What the Joplin feed did with each note, so an edited note updates what it
-- wrote before instead of adding to it.
CREATE TABLE sync.joplin_note (
    note_id         text PRIMARY KEY CHECK (note_id ~ '^[0-9a-f]{32}$'),
    updated_time    text NOT NULL CHECK (length(updated_time) <= 40),
    title           text NOT NULL CHECK (length(title) <= 1000),
    kind            text CHECK (kind IN ('book', 'movie', 'series')),
    state           text NOT NULL CHECK (state IN ('synced', 'unclear')),
    item_id         bigint REFERENCES catalog.media_item,
    consumption_id  bigint REFERENCES library.consumption,
    shelves         text[] NOT NULL DEFAULT '{}',     -- collections this note put the item in
    problem         text CHECK (length(problem) <= 2000),
    candidates      jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(candidates) = 'array'),
    seen_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT joplin_note_synced_has_item CHECK ((state = 'synced') = (item_id IS NOT NULL))
);
CREATE INDEX joplin_note_item ON sync.joplin_note (item_id);

-- Feed runs reuse ingest.run (tool 'joplin'); what they did, for the console.
ALTER TABLE ingest.run ADD COLUMN summary jsonb NOT NULL DEFAULT '{}'
    CHECK (jsonb_typeof(summary) = 'object');

GRANT USAGE ON SCHEMA sync TO ml_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA sync TO ml_app;
GRANT DELETE ON sync.joplin_note, catalog.document_alias TO ml_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ml_owner IN SCHEMA sync
    GRANT SELECT, INSERT, UPDATE ON TABLES TO ml_app;
