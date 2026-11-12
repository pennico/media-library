-- 0030 Joplin "To watch" and "To read" lists (D73).
-- One note "To watch" and one "To read" in Joplin, a title per line. Every hour the feed matches each
-- line, adds it to the library as to watch / to read, and has the headless Joplin client delete the
-- lines it took from the note. A line it can't match stays in the note and is kept here, with why
-- and the closest candidates, for the console's Sync page; a line taken but not yet deleted from the
-- note (the client could not sync) is kept too. Feed state, not audited: what it adds is.

SELECT set_config('ml.source', 'system', true), set_config('ml.client', 'migration 0030', true);

CREATE TABLE sync.joplin_list_line (
    note_id      text NOT NULL CHECK (note_id ~ '^[0-9a-f]{32}$'),
    line         text NOT NULL CHECK (length(line) BETWEEN 1 AND 1000),   -- the line as written, trimmed
    list         text NOT NULL CHECK (list IN ('watch', 'read')),
    state        text NOT NULL CHECK (state IN ('unclear', 'taken')),
    item_id      bigint REFERENCES catalog.media_item ON DELETE CASCADE,
    how          text CHECK (length(how) <= 500),
    problem      text CHECK (length(problem) <= 2000),
    candidates   jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(candidates) = 'array'),
    searched_at  timestamptz,                         -- the last time TMDB / the book sources were asked
    first_seen   timestamptz NOT NULL DEFAULT now(),
    seen_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (note_id, line),
    CONSTRAINT joplin_list_line_taken_has_item CHECK ((state = 'taken') = (item_id IS NOT NULL))
);
COMMENT ON TABLE sync.joplin_list_line IS 'Lines of the Joplin To watch / To read notes the feed could not take, or took and the client has not deleted yet';
CREATE INDEX joplin_list_line_item ON sync.joplin_list_line (item_id);
GRANT SELECT, INSERT, UPDATE, DELETE ON sync.joplin_list_line TO ml_app;
