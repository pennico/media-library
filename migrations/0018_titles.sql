-- 0018 titles in other languages and a random order (1.1.0).
-- A movie or series is known by other names: "Life Is Beautiful" is "La vita è bella" in
-- Italian. catalog.item_title keeps one title per language (English, Italian,
-- Danish from TMDB's translations, or typed by the owner); the console searches
-- them all and can show the titles in one language. The item's own title stays
-- what it was. A collection can also be shown in a random order. D54.

SELECT set_config('ml.source', 'system', true), set_config('ml.client', 'migration 0018', true);

CREATE TABLE catalog.item_title (
    item_id     bigint NOT NULL REFERENCES catalog.media_item ON DELETE CASCADE,
    language    catalog.lang_code NOT NULL REFERENCES catalog.language,
    title       catalog.label NOT NULL CHECK (length(title) <= 1000),
    source      catalog.code NOT NULL REFERENCES catalog.data_source,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, language)
);
COMMENT ON TABLE catalog.item_title IS 'An item''s title in another language (TMDB translations or the owner''s); the owner''s are never replaced';
CREATE INDEX item_title_trgm ON catalog.item_title USING gin (lower(catalog.f_unaccent(title)) ext.gin_trgm_ops);
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.item_title
    FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.item_title
    FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id', 'language');
GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.item_title TO ml_app;

ALTER TABLE library.shelf DROP CONSTRAINT shelf_sort_key_check;
ALTER TABLE library.shelf ADD CONSTRAINT shelf_sort_key_check CHECK (sort_key IN (
    'manual', 'title', 'author', 'added', 'finished', 'rating', 'published', 'pages', 'language', 'status', 'genre',
    'random'));
