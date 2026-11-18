-- 0032 series suggestions (D83-D86): Fill missing and Suggest series look a book up for the series it belongs
-- to (Open Library's series and its number, a series named in a title) and keep what they find here, as a
-- suggestion; nothing goes into a series until the owner clicks Add to "<series>" or Create series in the console
-- (catalog.group_member, 0031, audited there). Dismissing a suggestion hides it for that book. What a source said,
-- like ingest.source_record: not audited. DESIGN.md §24.

SELECT set_config('ml.source', 'system', true), set_config('ml.client', 'migration 0032', true);

CREATE TABLE ingest.series_suggestion (
    item_id      bigint NOT NULL REFERENCES catalog.media_item ON DELETE CASCADE,
    -- the name folded: no accents, lower case, no leading article, no trailing "series" / "cycle" / "saga" / "trilogy",
    -- so "Inheritance" (a title) and "The Inheritance Cycle" (Open Library) are one suggestion and match a series
    name_key     text NOT NULL CHECK (length(name_key) BETWEEN 1 AND 300),
    name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 300),       -- as the source writes it
    number       numeric(5,1) CHECK (number > 0 AND number < 1000),          -- the place in the series, when given
    source       catalog.code NOT NULL REFERENCES catalog.data_source (code), -- openlibrary, apple, or the title's source
    how          text NOT NULL CHECK (how IN ('series', 'title')),           -- a series field or list / read from a title
    ref          text CHECK (ref ~ '^OL[0-9]+L$'),                           -- Open Library's series, when it is one
    raw          text CHECK (length(raw) <= 1000),                           -- what it was read from
    found_at     timestamptz NOT NULL DEFAULT now(),
    dismissed_at timestamptz,                                                -- the owner said no, for this book
    PRIMARY KEY (item_id, name_key)
);
COMMENT ON TABLE ingest.series_suggestion IS 'Book series the sources suggest for a book; applied only by the owner';
CREATE INDEX series_suggestion_key ON ingest.series_suggestion (name_key) WHERE dismissed_at IS NULL;
GRANT SELECT, INSERT, UPDATE ON ingest.series_suggestion TO ml_app;
