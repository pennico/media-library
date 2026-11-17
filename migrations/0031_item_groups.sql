-- 0031 book series (D76, D77): books that belong together (a cycle, a
-- trilogy) show as one card on the Books shelf, as a film collection does on
-- the Movies shelf (0015, D49). A book series and a film collection are one
-- thing for two kinds of item, so catalog.film_collection becomes
-- catalog.item_group (with the kind of its items), and membership moves from
-- catalog.movie.collection_id to catalog.group_member, which also holds each
-- member's place in the group's order. DESIGN.md §23.
--
--  * An item is in at most one group (the member row's key is the item).
--  * A member row whose group is NULL is the owner's "in no group": TMDB never
--    puts a film back into a collection the owner took it out of, and never
--    moves it out of a group the owner chose (the provenance guard tracks
--    group_id, as it tracked movie.collection_id).
--  * The order is the position; the trash leaves the row alone, so a book
--    restored from the trash is back in its place.
--  * A group is deleted softly (deleted_at): its members leave it (group NULL,
--    by the owner), and an Undo brings it back with them.

SELECT set_config('ml.source', 'system', true), set_config('ml.client', 'migration 0031', true);

-- ------------------------------------------------------------- the groups
ALTER TABLE catalog.film_collection RENAME TO item_group;
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid = 'catalog.item_group'::regclass AND conname LIKE 'film\_collection\_%' LOOP
        EXECUTE format('ALTER TABLE catalog.item_group RENAME CONSTRAINT %I TO %I',
                       c.conname, 'item_group_' || substr(c.conname, length('film_collection_') + 1));
    END LOOP;
END;
$$;
ALTER TABLE catalog.item_group
    ADD COLUMN media_kind catalog.code NOT NULL DEFAULT 'movie'
        CONSTRAINT item_group_media_kind CHECK (media_kind IN ('book', 'movie')),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN deleted_at timestamptz,
    ADD CONSTRAINT item_group_of_kind UNIQUE (id, media_kind),
    -- TMDB's collections are films
    ADD CONSTRAINT item_group_tmdb_is_film CHECK (tmdb_id IS NULL OR media_kind = 'movie');
ALTER TABLE catalog.item_group ALTER COLUMN media_kind DROP DEFAULT;
COMMENT ON TABLE catalog.item_group IS 'Items shown as one card: a book series, or a film collection (TMDB''s or the owner''s)';
COMMENT ON COLUMN catalog.item_group.media_kind IS 'book (a book series) or movie (a film collection)';
-- one owner group per name and kind, among the live ones ("Dune" can be a book series and a film collection)
DROP INDEX catalog.film_collection_owner_name;
CREATE UNIQUE INDEX item_group_owner_name ON catalog.item_group (media_kind, lower(catalog.f_unaccent(name)))
    WHERE tmdb_id IS NULL AND deleted_at IS NULL;
DROP TRIGGER b10_provenance ON catalog.item_group;
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.item_group
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard('name', 'tmdb_id', 'deleted_at');
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.item_group
    FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
-- a group's kind never changes (its members are of that kind)
CREATE FUNCTION catalog.item_group_kind_fixed() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.media_kind IS DISTINCT FROM OLD.media_kind THEN
        RAISE EXCEPTION 'group % cannot change kind (% -> %)', OLD.id, OLD.media_kind, NEW.media_kind
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER a05_kind_fixed BEFORE UPDATE OF media_kind ON catalog.item_group
    FOR EACH ROW EXECUTE FUNCTION catalog.item_group_kind_fixed();

-- ------------------------------------------------------------ the members
CREATE TABLE catalog.group_member (
    item_id     bigint PRIMARY KEY,                    -- an item is in at most one group
    item_kind   catalog.code NOT NULL,
    group_id    bigint,                                -- NULL: in no group, by the owner's choice
    position    integer CHECK (position >= 1),         -- the place in the group's order
    provenance  jsonb NOT NULL DEFAULT '{}'
                CONSTRAINT group_member_provenance_object CHECK (jsonb_typeof(provenance) = 'object'),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (item_id, item_kind) REFERENCES catalog.media_item (id, kind) ON DELETE CASCADE,
    -- the group is of the item's kind: no film in a book series
    CONSTRAINT group_member_kind FOREIGN KEY (group_id, item_kind) REFERENCES catalog.item_group (id, media_kind),
    CONSTRAINT group_member_placed CHECK ((group_id IS NULL) = (position IS NULL)),
    CONSTRAINT group_member_position UNIQUE (group_id, position) DEFERRABLE INITIALLY DEFERRED
);
COMMENT ON TABLE catalog.group_member IS 'The group an item is in (a book series, a film collection) and its place in it';
CREATE INDEX group_member_group ON catalog.group_member (group_id) WHERE group_id IS NOT NULL;
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.group_member
    FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.group_member
    FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id');

-- the films of 0015's collections, in release order, keeping who put them there (TMDB or the owner)
INSERT INTO catalog.group_member (item_id, item_kind, group_id, position, provenance)
SELECT mv.item_id, 'movie', mv.collection_id,
       row_number() OVER (PARTITION BY mv.collection_id
                          ORDER BY coalesce(mv.release_date, i.first_published) NULLS LAST, i.sort_title, i.id),
       jsonb_build_object('group_id', coalesce(mv.provenance -> 'collection_id', catalog.provenance_stamp('system')),
                          '_row', catalog.provenance_stamp('system'))
  FROM catalog.movie mv JOIN catalog.media_item i ON i.id = mv.item_id
 WHERE mv.collection_id IS NOT NULL;

-- the guard comes after the copy (the copy keeps the owner's labels, which only the owner may write)
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.group_member
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard('group_id');

-- Only the owner removes a membership the owner made (a purge deletes as the owner).
CREATE FUNCTION catalog.group_member_delete_guard() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF catalog.current_source() <> 'owner' AND OLD.provenance #>> '{group_id,src}' = 'owner' THEN
        RAISE EXCEPTION 'the owner put item % in its group; only the owner can take it out', OLD.item_id
            USING ERRCODE = 'ML002';
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER b10_delete_guard BEFORE DELETE ON catalog.group_member
    FOR EACH ROW EXECUTE FUNCTION catalog.group_member_delete_guard();

-- a locked item's group is the owner's to change, like the rest of it
CREATE OR REPLACE FUNCTION catalog.row_locked(tbl text, r jsonb) RETURNS boolean
    LANGUAGE sql STABLE
RETURN CASE tbl
    WHEN 'media_item' THEN coalesce((r ->> 'locked')::boolean, false)
    WHEN 'edition' THEN catalog.item_locked((r ->> 'work_id')::bigint)
    WHEN 'edition_file' THEN catalog.edition_locked((r ->> 'edition_id')::bigint)
    WHEN 'cover' THEN coalesce(catalog.edition_locked((r ->> 'edition_id')::bigint), false)
                      OR catalog.item_locked((r ->> 'item_id')::bigint)
    WHEN 'contribution' THEN catalog.item_locked((r ->> 'item_id')::bigint)
                             OR catalog.edition_locked((r ->> 'edition_id')::bigint)
    WHEN 'external_id' THEN catalog.item_locked((r ->> 'item_id')::bigint)
                            OR catalog.edition_locked((r ->> 'edition_id')::bigint)
    WHEN 'item_tag' THEN catalog.item_locked((r ->> 'item_id')::bigint)
    WHEN 'group_member' THEN catalog.item_locked((r ->> 'item_id')::bigint)
    ELSE false
END;

-- ------------------------------------------- movie.collection_id goes
-- (its labels moved with it; the guard is made again without the column)
DROP TRIGGER b10_provenance ON catalog.movie;
UPDATE catalog.movie SET provenance = provenance - 'collection_id' WHERE provenance ? 'collection_id';
ALTER TABLE catalog.movie DROP COLUMN collection_id;
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.movie
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard('release_date', 'runtime_min', 'origin_countries');

-- the app writes members and groups; nothing is hard-deleted by it (a purge cascades from the item)
GRANT SELECT, INSERT, UPDATE ON catalog.group_member TO ml_app;
