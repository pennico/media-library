-- 0016 sections (0.8.0): folders in the left pane. An entry lives in at most
-- one section; none = the main library. Two built-in ones are kept out of
-- sight: Archive (moved away, nothing deleted) and Test (made-up items to try
-- the console with); the statistics leave both out. DESIGN.md §17; D50.

CREATE TABLE library.section (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id   uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    name        catalog.label NOT NULL CHECK (length(name) <= 100),
    kind        text NOT NULL DEFAULT 'manual' CHECK (kind IN ('manual', 'archive', 'test')),
    position    integer NOT NULL DEFAULT 1 CHECK (position >= 1),
    provenance  jsonb NOT NULL DEFAULT '{}'
                CONSTRAINT section_provenance_object CHECK (jsonb_typeof(provenance) = 'object'),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
COMMENT ON TABLE library.section IS 'A folder in the left pane: an entry lives in one section or in the main library';
CREATE UNIQUE INDEX section_name ON library.section (lower(catalog.f_unaccent(name))) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX section_one_builtin ON library.section (kind) WHERE kind <> 'manual';
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON library.section FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON library.section
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard('name', 'position', 'deleted_at');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.section
    FOR EACH ROW EXECUTE FUNCTION audit.capture('id');

SELECT set_config('ml.source', 'system', true), set_config('ml.client', 'migration 0016', true);
INSERT INTO library.section (name, kind, position) VALUES ('Archive', 'archive', 1), ('Test', 'test', 2);

ALTER TABLE library.entry ADD COLUMN section_id bigint REFERENCES library.section;
CREATE INDEX entry_section ON library.entry (section_id) WHERE section_id IS NOT NULL;

-- the provenance guard tracks the new column too
DROP TRIGGER b10_provenance ON library.entry;
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON library.entry
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'status', 'status_changed_at', 'rating', 'rated_at', 'review', 'reviewed_at',
        'added_at', 'deleted_at', 'section_id');

-- Does an entry in this section count in the statistics? Yes in the main
-- library and in the owner's own sections; not in Archive or Test.
-- (security definer: the read-only role reads the stats views, not library.section)
CREATE FUNCTION library.counts_in_stats(p_section bigint) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
RETURN p_section IS NULL OR coalesce((SELECT kind = 'manual' FROM library.section WHERE id = p_section), true);
REVOKE ALL ON FUNCTION library.counts_in_stats(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION library.counts_in_stats(bigint) TO ml_app, ml_readonly;

-- ------------------------------------------------------------ statistics
CREATE OR REPLACE VIEW stats.book_reading AS
SELECT c.public_id                                   AS reading_id,
       i.public_id                                   AS item_id,
       i.title,
       i.sort_title,
       (SELECT string_agg(p.name, ', ' ORDER BY ct.position)
          FROM catalog.contribution ct JOIN catalog.person p ON p.id = ct.person_id
         WHERE ct.item_id = i.id AND ct.role = 'author')                  AS authors,
       c.state,
       c.started_on, c.started_precision,
       c.ended_on, c.ended_precision,
       extract(year FROM c.ended_on)::int                                 AS year,
       CASE WHEN c.ended_precision IN ('day', 'month')
            THEN extract(month FROM c.ended_on)::int END                  AS month,
       CASE WHEN c.ended_precision = 'day' THEN c.ended_on END            AS day,
       coalesce(ed.pages, pe.pages)                                       AS pages,
       e.rating,
       c.language                                                         AS read_in,
       i.original_language,
       (SELECT array_agg(t.name::text ORDER BY it.position)
          FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
         WHERE it.item_id = i.id AND t.kind = 'genre' AND t.deleted_at IS NULL) AS genres,
       c.progress_pct,
       c.progress_pages
  FROM library.consumption c
  JOIN library.entry e ON e.item_id = c.item_id AND e.deleted_at IS NULL AND library.counts_in_stats(e.section_id)
  JOIN catalog.media_item i ON i.id = c.item_id AND i.deleted_at IS NULL AND i.kind = 'book'
  LEFT JOIN catalog.edition ed ON ed.id = c.edition_id AND ed.deleted_at IS NULL
  LEFT JOIN catalog.edition pe ON pe.work_id = i.id AND pe.is_primary AND pe.deleted_at IS NULL
 WHERE c.deleted_at IS NULL;

CREATE OR REPLACE VIEW stats.rating_histogram AS
SELECT floor(e.rating)::int AS bucket, count(*) AS books
  FROM library.entry e
  JOIN catalog.media_item i ON i.id = e.item_id AND i.deleted_at IS NULL AND i.kind = 'book'
 WHERE e.deleted_at IS NULL AND e.rating IS NOT NULL AND library.counts_in_stats(e.section_id)
 GROUP BY 1;

CREATE OR REPLACE VIEW stats.genres AS
SELECT t.name,
       count(*) FILTER (WHERE e.status = 'completed') AS books_read,
       count(*)                                       AS books
  FROM library.item_tag it
  JOIN library.tag t ON t.id = it.tag_id AND t.kind = 'genre' AND t.deleted_at IS NULL
  JOIN library.entry e ON e.item_id = it.item_id AND e.deleted_at IS NULL AND library.counts_in_stats(e.section_id)
  JOIN catalog.media_item i ON i.id = it.item_id AND i.deleted_at IS NULL AND i.kind = 'book'
 GROUP BY t.name;

CREATE OR REPLACE VIEW stats.activity_days AS
SELECT day, count(*) AS events
  FROM (SELECT c.started_on AS day, c.item_id FROM library.consumption c
         WHERE c.started_precision = 'day' AND c.deleted_at IS NULL
        UNION ALL
        SELECT c.ended_on, c.item_id FROM library.consumption c
         WHERE c.ended_precision = 'day' AND c.deleted_at IS NULL
        UNION ALL
        SELECT (c.progress_at AT TIME ZONE 'Europe/Copenhagen')::date, c.item_id FROM library.consumption c
         WHERE c.progress_at IS NOT NULL AND c.deleted_at IS NULL) d
  JOIN library.entry e ON e.item_id = d.item_id AND e.deleted_at IS NULL AND library.counts_in_stats(e.section_id)
  JOIN catalog.media_item i ON i.id = d.item_id AND i.deleted_at IS NULL
 GROUP BY day;

CREATE OR REPLACE VIEW stats.library_counts AS
SELECT i.kind AS media_kind, e.status, count(*) AS items
  FROM library.entry e
  JOIN catalog.media_item i ON i.id = e.item_id AND i.deleted_at IS NULL
 WHERE e.deleted_at IS NULL AND library.counts_in_stats(e.section_id)
 GROUP BY i.kind, e.status;

CREATE OR REPLACE VIEW stats.library_book AS
SELECT i.public_id                                                        AS item_id,
       e.status,
       i.original_language,
       coalesce(extract(year FROM i.first_published), extract(year FROM pe.published_on))::int AS released,
       (SELECT array_agg(p.name::text ORDER BY ct.position)
          FROM catalog.contribution ct JOIN catalog.person p ON p.id = ct.person_id
         WHERE ct.item_id = i.id AND ct.role = 'author')                  AS authors,
       (SELECT array_agg(coalesce(g.name, t.name)::text ORDER BY it.position)
          FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id AND t.kind = 'genre' AND t.deleted_at IS NULL
          LEFT JOIN library.tag g ON g.id = t.parent_id
         WHERE it.item_id = i.id)                                         AS groups,
       (SELECT array_agg(t.name::text ORDER BY it.position)
          FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id AND t.kind = 'genre' AND t.deleted_at IS NULL
         WHERE it.item_id = i.id AND t.parent_id IS NOT NULL)             AS categories
  FROM library.entry e
  JOIN catalog.media_item i ON i.id = e.item_id AND i.deleted_at IS NULL AND i.kind = 'book'
  LEFT JOIN catalog.edition pe ON pe.work_id = i.id AND pe.is_primary AND pe.deleted_at IS NULL
 WHERE e.deleted_at IS NULL AND library.counts_in_stats(e.section_id);

-- ------------------------------------------------------------------ purge
-- A series goes for good with its seasons (their watches, posters and rows); 0013 refused series that
-- had any, and since 0.6.0 most do. Otherwise as in 0013.
CREATE OR REPLACE FUNCTION library.purge_item(p_item bigint) RETURNS TABLE (path text)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    eds bigint[];
    seasons bigint[];
    docs text[];
    s record;
BEGIN
    PERFORM 1 FROM catalog.media_item WHERE id = p_item AND deleted_at IS NOT NULL
       AND kind IN ('book', 'movie', 'series');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'item % is not in the trash', p_item USING ERRCODE = 'ML002';
    END IF;
    SELECT coalesce(array_agg(item_id), '{}') INTO seasons FROM catalog.season WHERE series_id = p_item;
    IF EXISTS (SELECT 1 FROM catalog.episode WHERE season_id = ANY (seasons)) THEN
        RAISE EXCEPTION 'series % has episodes; purging those is not supported yet', p_item USING ERRCODE = 'ML002';
    END IF;
    SELECT coalesce(array_agg(id), '{}') INTO eds FROM catalog.edition WHERE work_id = p_item;
    SELECT coalesce(array_agg(d), '{}') INTO docs FROM (
        SELECT kosync_md5::text AS d FROM catalog.edition_file WHERE edition_id = ANY (eds) AND kosync_md5 IS NOT NULL
        UNION SELECT document::text FROM catalog.document_alias WHERE edition_id = ANY (eds)) x;

    RETURN QUERY
        SELECT f.rel_path::text FROM catalog.edition_file f
         WHERE f.edition_id = ANY (eds) AND f.storage = 'store'
           AND NOT EXISTS (SELECT 1 FROM catalog.edition_file o
                            WHERE o.storage = 'store' AND o.rel_path = f.rel_path AND NOT (o.edition_id = ANY (eds)))
        UNION
        SELECT c.local_path::text FROM catalog.cover c
         WHERE (c.item_id = p_item OR c.item_id = ANY (seasons) OR c.edition_id = ANY (eds)) AND c.local_path IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM catalog.cover o
                            WHERE o.local_path = c.local_path
                              AND NOT (coalesce(o.item_id = p_item OR o.item_id = ANY (seasons), false)
                                       OR coalesce(o.edition_id = ANY (eds), false)));

    UPDATE sync.joplin_note SET state = 'deleted', item_id = NULL, consumption_id = NULL,
           problem = 'its book was deleted for good in the console; change the note to bring it back'
     WHERE item_id = p_item OR consumption_id IN (SELECT id FROM library.consumption WHERE item_id = p_item);
    DELETE FROM sync.kosync_position WHERE document = ANY (docs);
    DELETE FROM library.consumption WHERE item_id = p_item;
    FOR s IN SELECT shelf_id, position FROM library.shelf_item WHERE item_id = p_item LOOP
        DELETE FROM library.shelf_item WHERE shelf_id = s.shelf_id AND item_id = p_item;
        UPDATE library.shelf_item SET position = position - 1 WHERE shelf_id = s.shelf_id AND position > s.position;
    END LOOP;
    DELETE FROM library.item_tag WHERE item_id = p_item OR item_id = ANY (seasons);
    DELETE FROM library.entry WHERE item_id = p_item;
    DELETE FROM catalog.cover WHERE item_id = p_item OR item_id = ANY (seasons) OR edition_id = ANY (eds);
    DELETE FROM catalog.document_alias WHERE edition_id = ANY (eds);
    DELETE FROM catalog.edition_file WHERE edition_id = ANY (eds);
    DELETE FROM catalog.contribution WHERE item_id = p_item OR item_id = ANY (seasons) OR edition_id = ANY (eds);
    DELETE FROM catalog.external_id WHERE item_id = p_item OR item_id = ANY (seasons) OR edition_id = ANY (eds);
    DELETE FROM catalog.edition WHERE work_id = p_item;
    DELETE FROM catalog.media_item WHERE id = ANY (seasons);  -- each season row cascades
    DELETE FROM catalog.media_item WHERE id = p_item;          -- the book/movie/series row cascades
END;
$$;
REVOKE ALL ON FUNCTION library.purge_item(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION library.purge_item(bigint) TO ml_app;
