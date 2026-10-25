-- 0013: the trash (a book deleted in the console waits 30 days, then goes for
-- good), a two-level category tree for genres, and statistics about the
-- library itself (not only what was read). DESIGN.md §14.

-- ------------------------------------------------------------------ trash
-- Moving to the trash is the soft delete every table already has
-- (media_item.deleted_at + entry.deleted_at). Purging removes the item and
-- everything that hangs off it; audit.change keeps the history, and
-- ingest.id_map keeps the source ids, so a re-import doesn't bring it back.

ALTER TABLE sync.joplin_note DROP CONSTRAINT joplin_note_synced_has_item;
ALTER TABLE sync.joplin_note DROP CONSTRAINT joplin_note_state_check;
ALTER TABLE sync.joplin_note ADD CONSTRAINT joplin_note_state_check
    CHECK (state IN ('synced', 'unclear', 'deleted'));       -- deleted: its book was deleted for good
ALTER TABLE sync.joplin_note ADD CONSTRAINT joplin_note_synced_has_item
    CHECK ((state = 'synced') = (item_id IS NOT NULL));

-- Deletes one trashed item for good. Returns the media files (relative paths)
-- nothing else uses, for the caller to remove from disk. Runs as the owner of
-- the tables (the app role has no hard DELETE); the caller's write_tx names
-- the source and client for the audit.
CREATE FUNCTION library.purge_item(p_item bigint) RETURNS TABLE (path text)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    eds bigint[];
    docs text[];
    s record;
BEGIN
    PERFORM 1 FROM catalog.media_item WHERE id = p_item AND deleted_at IS NOT NULL
       AND kind IN ('book', 'movie', 'series');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'item % is not in the trash', p_item USING ERRCODE = 'ML002';
    END IF;
    IF EXISTS (SELECT 1 FROM catalog.season WHERE series_id = p_item) THEN
        RAISE EXCEPTION 'series % has seasons; purging those is not supported yet', p_item USING ERRCODE = 'ML002';
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
         WHERE (c.item_id = p_item OR c.edition_id = ANY (eds)) AND c.local_path IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM catalog.cover o
                            WHERE o.local_path = c.local_path
                              AND NOT (coalesce(o.item_id = p_item, false) OR coalesce(o.edition_id = ANY (eds), false)));

    UPDATE sync.joplin_note SET state = 'deleted', item_id = NULL, consumption_id = NULL,
           problem = 'its book was deleted for good in the console; change the note to bring it back'
     WHERE item_id = p_item OR consumption_id IN (SELECT id FROM library.consumption WHERE item_id = p_item);
    DELETE FROM sync.kosync_position WHERE document = ANY (docs);
    DELETE FROM library.consumption WHERE item_id = p_item;
    FOR s IN SELECT shelf_id, position FROM library.shelf_item WHERE item_id = p_item LOOP
        DELETE FROM library.shelf_item WHERE shelf_id = s.shelf_id AND item_id = p_item;
        UPDATE library.shelf_item SET position = position - 1 WHERE shelf_id = s.shelf_id AND position > s.position;
    END LOOP;
    DELETE FROM library.item_tag WHERE item_id = p_item;
    DELETE FROM library.entry WHERE item_id = p_item;
    DELETE FROM catalog.cover WHERE item_id = p_item OR edition_id = ANY (eds);
    DELETE FROM catalog.document_alias WHERE edition_id = ANY (eds);
    DELETE FROM catalog.edition_file WHERE edition_id = ANY (eds);
    DELETE FROM catalog.contribution WHERE item_id = p_item OR edition_id = ANY (eds);
    DELETE FROM catalog.external_id WHERE item_id = p_item OR edition_id = ANY (eds);
    DELETE FROM catalog.edition WHERE work_id = p_item;
    DELETE FROM catalog.media_item WHERE id = p_item;        -- the book/movie/series row cascades
END;
$$;
REVOKE ALL ON FUNCTION library.purge_item(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION library.purge_item(bigint) TO ml_app;

-- -------------------------------------------------------------- categories
-- Genres get one optional level above them: a group (Fiction, Non-fiction)
-- holds categories (Novel, Crime & mystery, History…). A book carries
-- categories, or a group alone when nothing finer is known.

ALTER TABLE library.tag ADD COLUMN parent_id bigint REFERENCES library.tag,
                        ADD COLUMN position smallint CHECK (position BETWEEN 1 AND 1000);
ALTER TABLE library.tag ADD CONSTRAINT tag_parent_is_genre CHECK (parent_id IS NULL OR kind = 'genre');
ALTER TABLE library.tag ADD CONSTRAINT tag_not_own_parent CHECK (parent_id <> id);
CREATE INDEX tag_parent ON library.tag (parent_id);

CREATE FUNCTION library.tag_two_levels() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.parent_id IS NOT NULL THEN
        PERFORM 1 FROM library.tag p
         WHERE p.id = NEW.parent_id AND p.kind = 'genre' AND p.parent_id IS NULL AND p.deleted_at IS NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'a category''s group must be a top-level genre' USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM library.tag c WHERE c.parent_id = NEW.id AND c.deleted_at IS NULL) THEN
            RAISE EXCEPTION 'genre % is a group; it cannot sit inside another group', NEW.name
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER a10_two_levels BEFORE INSERT OR UPDATE OF parent_id ON library.tag
    FOR EACH ROW EXECUTE FUNCTION library.tag_two_levels();

INSERT INTO catalog.data_source (code, label) VALUES ('categorize', 'Category rules: your labels and provider subjects');

-- The vocabulary. Existing genres with the same name are reused (History,
-- Philosophy, Fantasy…); `medialib categorize` moves books onto it.
CREATE FUNCTION pg_temp.genre(p_name text, p_parent text, p_pos int) RETURNS void
    LANGUAGE plpgsql
AS $$
DECLARE
    pid bigint;
    tid bigint;
BEGIN
    IF p_parent IS NOT NULL THEN
        SELECT id INTO pid FROM library.tag WHERE kind = 'genre' AND deleted_at IS NULL
           AND lower(catalog.f_unaccent(name)) = lower(catalog.f_unaccent(p_parent));
    END IF;
    SELECT id INTO tid FROM library.tag WHERE kind = 'genre' AND deleted_at IS NULL
       AND lower(catalog.f_unaccent(name)) = lower(catalog.f_unaccent(p_name));
    IF tid IS NULL THEN
        INSERT INTO library.tag (kind, name, parent_id, position) VALUES ('genre', p_name, pid, p_pos);
    ELSE
        UPDATE library.tag SET name = p_name, parent_id = pid, position = p_pos WHERE id = tid;
    END IF;
END;
$$;
SELECT pg_temp.genre(n, NULL, p) FROM (VALUES ('Fiction', 1), ('Non-fiction', 2)) v(n, p);
SELECT pg_temp.genre(n, 'Fiction', p) FROM (VALUES
    ('Novel', 1), ('Historical fiction', 2), ('Crime & mystery', 3), ('Science fiction', 4), ('Fantasy', 5),
    ('Horror', 6), ('Short stories', 7), ('Drama & poetry', 8), ('Graphic novels', 9), ('Satire & humour', 10)) v(n, p);
SELECT pg_temp.genre(n, 'Non-fiction', p) FROM (VALUES
    ('History', 1), ('Biography & memoir', 2), ('Philosophy', 3), ('Psychology & self-help', 4),
    ('Science & nature', 5), ('Economics & business', 6), ('Politics & society', 7), ('Essays & criticism', 8),
    ('Religion & spirituality', 9), ('Art & culture', 10), ('Travel', 11)) v(n, p);

-- ------------------------------------------------------------ statistics
-- The calendar counted readings of deleted books; not any more.
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
  JOIN library.entry e ON e.item_id = d.item_id AND e.deleted_at IS NULL
  JOIN catalog.media_item i ON i.id = d.item_id AND i.deleted_at IS NULL
 GROUP BY day;

-- One row per book in the library (any status): what Visuals → Library counts.
CREATE VIEW stats.library_book AS
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
 WHERE e.deleted_at IS NULL;
