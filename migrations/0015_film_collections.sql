-- 0015 film collections (0.7.0): films that belong together (a trilogy, a
-- saga, the owner's own group such as "The Lord of the Rings + The Hobbit")
-- show as one card on the Movies shelf, the way a series shows its seasons.
-- A movie belongs to at most one collection. DESIGN.md §16; D49.

CREATE TABLE catalog.film_collection (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id   uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    name        catalog.label NOT NULL CHECK (length(name) <= 300),
    -- TMDB's collection id, when the collection is TMDB's; the owner's own groups have none
    tmdb_id     text UNIQUE CHECK (tmdb_id ~ '^[0-9]{1,12}$'),
    provenance  jsonb NOT NULL DEFAULT '{}'
                CONSTRAINT film_collection_provenance_object CHECK (jsonb_typeof(provenance) = 'object'),
    created_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE catalog.film_collection IS 'Films shown as one card (a TMDB collection, or a group the owner named)';
CREATE UNIQUE INDEX film_collection_owner_name ON catalog.film_collection (lower(catalog.f_unaccent(name)))
    WHERE tmdb_id IS NULL;
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.film_collection
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard('name', 'tmdb_id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.film_collection
    FOR EACH ROW EXECUTE FUNCTION audit.capture('id');

ALTER TABLE catalog.movie
    ADD COLUMN collection_id bigint REFERENCES catalog.film_collection;
CREATE INDEX movie_collection ON catalog.movie (collection_id) WHERE collection_id IS NOT NULL;

-- the provenance guard tracks the new column too: TMDB never moves a film out of a group the owner chose
DROP TRIGGER b10_provenance ON catalog.movie;
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.movie
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard('release_date', 'runtime_min', 'origin_countries',
                                                           'collection_id');
