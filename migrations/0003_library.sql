-- 0003 library: the owner's side. Entries (status, rating, review), readings
-- (consumption), tags, shelves. DESIGN.md §4.3.

CREATE TABLE library.status (
    code          catalog.code PRIMARY KEY,
    label_book    catalog.label NOT NULL,
    label_screen  catalog.label NOT NULL,
    sort          smallint NOT NULL UNIQUE
);
INSERT INTO library.status (code, label_book, label_screen, sort) VALUES
    ('planned', 'To read', 'To watch', 1),
    ('in_progress', 'Reading', 'Watching', 2),
    ('completed', 'Read', 'Watched', 3),
    ('paused', 'Paused', 'Paused', 4),
    ('dropped', 'Abandoned', 'Abandoned', 5);

CREATE TABLE library.entry (
    item_id            bigint PRIMARY KEY REFERENCES catalog.media_item,
    status             catalog.code NOT NULL DEFAULT 'planned' REFERENCES library.status,
    status_changed_at  timestamptz NOT NULL DEFAULT now(),
    rating             catalog.rating,
    rated_at           timestamptz,
    review             catalog.long_text,
    reviewed_at        timestamptz,
    added_at           timestamptz NOT NULL DEFAULT now(),
    provenance         jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    deleted_at         timestamptz,
    CONSTRAINT entry_rated_at CHECK (rating IS NULL OR rated_at IS NOT NULL),
    CONSTRAINT entry_reviewed_at CHECK (review IS NULL OR reviewed_at IS NOT NULL),
    CONSTRAINT entry_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
);
COMMENT ON TABLE library.entry IS 'One per top-level item in the owner''s library; rating and review belong to the item (D5)';
CREATE INDEX entry_status ON library.entry (status) WHERE deleted_at IS NULL;
CREATE INDEX entry_review_fts ON library.entry
    USING gin (to_tsvector('catalog.ml_search', coalesce(review, '')));

CREATE FUNCTION library.entry_top_level() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1 FROM catalog.media_item i JOIN catalog.media_kind k ON k.code = i.kind
     WHERE i.id = NEW.item_id AND k.top_level;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'item % cannot be a library entry (only books, movies and series)', NEW.item_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER a05_top_level BEFORE INSERT OR UPDATE OF item_id ON library.entry
    FOR EACH ROW EXECUTE FUNCTION library.entry_top_level();

-- One row per time through an item: a reading (a re-read is a new row), and
-- later a movie or episode watch (D22).
CREATE TABLE library.consumption (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id          uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    item_id            bigint NOT NULL REFERENCES library.entry (item_id),
    edition_id         bigint,
    state              catalog.code NOT NULL
                       CHECK (state IN ('in_progress', 'paused', 'completed', 'dropped'))
                       REFERENCES library.status,
    started_on         date,
    started_precision  catalog.precision,
    ended_on           date,
    ended_precision    catalog.precision,
    progress_pct       catalog.percent,
    progress_pages     integer CHECK (progress_pages >= 0 AND progress_pages <= 20000),
    progress_at        timestamptz,
    language           catalog.lang_code REFERENCES catalog.language,
    medium             catalog.code REFERENCES catalog.edition_format,
    note               catalog.long_text,
    source             catalog.code NOT NULL REFERENCES catalog.data_source,
    provenance         jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    deleted_at         timestamptz,
    -- the edition read must be an edition of this book
    FOREIGN KEY (edition_id, item_id) REFERENCES catalog.edition (id, work_id),
    CONSTRAINT consumption_started_ok CHECK (catalog.date_ok(started_on, started_precision)),
    CONSTRAINT consumption_ended_ok CHECK (catalog.date_ok(ended_on, ended_precision)),
    CONSTRAINT consumption_open_has_no_end CHECK (state NOT IN ('in_progress', 'paused') OR ended_on IS NULL),
    CONSTRAINT consumption_dates_ordered CHECK (started_on <= ended_on),
    CONSTRAINT consumption_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
);
-- at most one open reading per item
CREATE UNIQUE INDEX consumption_one_open ON library.consumption (item_id)
    WHERE state IN ('in_progress', 'paused') AND deleted_at IS NULL;
CREATE INDEX consumption_item ON library.consumption (item_id);
CREATE INDEX consumption_ended ON library.consumption (ended_on) WHERE deleted_at IS NULL;

-- Status and readings agree (checked at commit, DESIGN.md §4.3).
CREATE FUNCTION library.check_entry(p_item bigint) RETURNS void
    LANGUAGE plpgsql
AS $$
DECLARE
    e library.entry;
    open_states text[];
    n_completed int;
    n_dropped int;
BEGIN
    SELECT * INTO e FROM library.entry WHERE item_id = p_item;
    IF NOT FOUND OR e.deleted_at IS NOT NULL THEN
        RETURN;
    END IF;
    SELECT coalesce(array_agg(state::text) FILTER (WHERE state IN ('in_progress', 'paused')), '{}'),
           count(*) FILTER (WHERE state = 'completed'),
           count(*) FILTER (WHERE state = 'dropped')
      INTO open_states, n_completed, n_dropped
      FROM library.consumption
     WHERE item_id = p_item AND deleted_at IS NULL;
    IF e.status IN ('in_progress', 'paused') THEN
        IF open_states <> ARRAY[e.status::text] THEN
            RAISE EXCEPTION 'item %: status % needs exactly one open reading in state %, found %',
                p_item, e.status, e.status, open_states USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF open_states <> '{}' THEN
        RAISE EXCEPTION 'item %: status % but a reading is still open (%)', p_item, e.status, open_states
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF e.status = 'completed' AND n_completed = 0 THEN
        RAISE EXCEPTION 'item %: status completed needs a completed reading', p_item
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF e.status = 'dropped' AND n_dropped = 0 THEN
        RAISE EXCEPTION 'item %: status dropped needs a dropped reading', p_item
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
END;
$$;

CREATE FUNCTION library.check_entry_trigger() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM library.check_entry(OLD.item_id);
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND (TG_OP = 'INSERT' OR NEW.item_id <> OLD.item_id) THEN
        PERFORM library.check_entry(NEW.item_id);
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER z_check_entry AFTER INSERT OR UPDATE OR DELETE ON library.entry
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION library.check_entry_trigger();
CREATE CONSTRAINT TRIGGER z_check_entry AFTER INSERT OR UPDATE OR DELETE ON library.consumption
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION library.check_entry_trigger();

-- ------------------------------------------------------------------- tags

CREATE TABLE library.tag (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('genre', 'tag')),
    name        catalog.label NOT NULL CHECK (length(name) <= 100),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz,
    CONSTRAINT tag_name_has_key CHECK (catalog.name_key(name) <> '')
);
CREATE UNIQUE INDEX tag_name ON library.tag (kind, lower(catalog.f_unaccent(name))) WHERE deleted_at IS NULL;

CREATE TABLE library.item_tag (
    item_id     bigint NOT NULL REFERENCES catalog.media_item ON DELETE CASCADE,
    tag_id      bigint NOT NULL REFERENCES library.tag,
    position    smallint NOT NULL DEFAULT 1 CHECK (position >= 1),
    source      catalog.code NOT NULL REFERENCES catalog.data_source,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX item_tag_tag ON library.item_tag (tag_id);

-- ---------------------------------------------------------------- shelves

CREATE TABLE library.shelf (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id   uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    name        catalog.label NOT NULL CHECK (length(name) <= 100),
    kind        text NOT NULL CHECK (kind IN ('all', 'manual')),
    media_kind  catalog.code NOT NULL CHECK (media_kind IN ('book', 'movie', 'series'))
                REFERENCES catalog.media_kind,
    position    integer NOT NULL CHECK (position >= 1),
    sort_key    text NOT NULL DEFAULT 'manual'
                CHECK (sort_key IN ('manual', 'title', 'author', 'added', 'finished', 'rating', 'published')),
    sort_desc   boolean NOT NULL DEFAULT false,
    cover_size  text NOT NULL DEFAULT 'm' CHECK (cover_size IN ('s', 'm', 'l')),
    layout      text NOT NULL DEFAULT 'grid' CHECK (layout IN ('grid', 'list')),
    sort_locale text NOT NULL DEFAULT 'und' CHECK (sort_locale IN ('und', 'da', 'it', 'en')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz,
    CONSTRAINT shelf_all_not_deleted CHECK (kind <> 'all' OR deleted_at IS NULL),
    CONSTRAINT shelf_position UNIQUE (media_kind, position) DEFERRABLE INITIALLY DEFERRED
);
CREATE UNIQUE INDEX shelf_name ON library.shelf (media_kind, lower(catalog.f_unaccent(name))) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX shelf_one_all ON library.shelf (media_kind) WHERE kind = 'all';

CREATE TABLE library.shelf_item (
    shelf_id    bigint NOT NULL REFERENCES library.shelf ON DELETE CASCADE,
    item_id     bigint NOT NULL REFERENCES library.entry (item_id) ON DELETE CASCADE,
    position    integer NOT NULL CHECK (position >= 1),
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shelf_id, item_id),
    -- drag reorder: shift a range in one statement, unique again at commit
    CONSTRAINT shelf_item_position UNIQUE (shelf_id, position) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX shelf_item_item ON library.shelf_item (item_id);

-- The built-in "all" shelves, one per media kind.
INSERT INTO library.shelf (name, kind, media_kind, position, sort_key) VALUES
    ('Books', 'all', 'book', 1, 'manual'),
    ('Movies', 'all', 'movie', 1, 'manual'),
    ('Series', 'all', 'series', 1, 'manual');

-- A shelf only holds items of its media kind.
CREATE FUNCTION library.shelf_item_kind() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1 FROM library.shelf s JOIN catalog.media_item i ON i.kind = s.media_kind
     WHERE s.id = NEW.shelf_id AND i.id = NEW.item_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'item % does not belong on shelf % (wrong media kind)', NEW.item_id, NEW.shelf_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER a05_kind BEFORE INSERT OR UPDATE OF shelf_id, item_id ON library.shelf_item
    FOR EACH ROW EXECUTE FUNCTION library.shelf_item_kind();

-- Every new entry goes to the end of its kind's "all" shelf, so the main
-- library has a manual order too.
CREATE FUNCTION library.entry_to_all_shelf() RETURNS trigger
    LANGUAGE plpgsql
AS $$
DECLARE
    s bigint;
BEGIN
    SELECT sh.id INTO s FROM library.shelf sh JOIN catalog.media_item i ON i.kind = sh.media_kind
     WHERE i.id = NEW.item_id AND sh.kind = 'all';
    INSERT INTO library.shelf_item (shelf_id, item_id, position)
    SELECT s, NEW.item_id, coalesce(max(position), 0) + 1 FROM library.shelf_item WHERE shelf_id = s;
    RETURN NULL;
END;
$$;
CREATE TRIGGER z_all_shelf AFTER INSERT ON library.entry
    FOR EACH ROW EXECUTE FUNCTION library.entry_to_all_shelf();

CREATE TRIGGER a00_updated_at BEFORE UPDATE ON library.entry FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON library.consumption FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON library.tag FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON library.item_tag FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON library.shelf FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
