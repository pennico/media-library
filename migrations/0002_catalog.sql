-- 0002 catalog: media items and subtypes, editions, files, people,
-- contributions, external ids, covers. DESIGN.md §4.2.

CREATE TABLE catalog.media_item (
    id                         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id                  uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    kind                       catalog.code NOT NULL REFERENCES catalog.media_kind,
    title                      catalog.label NOT NULL,
    title_language             catalog.lang_code REFERENCES catalog.language,
    sort_title                 catalog.label NOT NULL,          -- set by trigger unless the owner set it
    original_title             catalog.label,
    original_language          catalog.lang_code REFERENCES catalog.language,
    first_published            date,
    first_published_precision  catalog.precision,
    description                catalog.long_text,
    locked                     boolean NOT NULL DEFAULT false,  -- automated sources may not change it
    provenance                 jsonb NOT NULL DEFAULT '{}',
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    deleted_at                 timestamptz,
    UNIQUE (id, kind),
    CONSTRAINT media_item_first_published_ok CHECK (catalog.date_ok(first_published, first_published_precision)),
    CONSTRAINT media_item_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
);
COMMENT ON TABLE catalog.media_item IS 'Supertype of everything the owner can track: book (work), movie, series, season, episode';
CREATE INDEX media_item_kind ON catalog.media_item (kind) WHERE deleted_at IS NULL;
CREATE INDEX media_item_sort_title ON catalog.media_item (sort_title COLLATE catalog.sort_und);
CREATE INDEX media_item_title_trgm ON catalog.media_item
    USING gin (lower(catalog.f_unaccent(title)) ext.gin_trgm_ops);

-- Subtypes: item_id is PK and FK (item_id, kind) with the kind pinned, so a
-- movie row can never point at a book item.
CREATE TABLE catalog.book (
    item_id  bigint PRIMARY KEY,
    kind     catalog.code NOT NULL DEFAULT 'book' CHECK (kind = 'book'),
    FOREIGN KEY (item_id, kind) REFERENCES catalog.media_item (id, kind) ON DELETE CASCADE
);
COMMENT ON TABLE catalog.book IS 'A book work; its editions hang off it';

CREATE TABLE catalog.movie (
    item_id       bigint PRIMARY KEY,
    kind          catalog.code NOT NULL DEFAULT 'movie' CHECK (kind = 'movie'),
    release_date  date,
    runtime_min   integer CHECK (runtime_min > 0 AND runtime_min < 2000),
    FOREIGN KEY (item_id, kind) REFERENCES catalog.media_item (id, kind) ON DELETE CASCADE
);

CREATE TABLE catalog.series (
    item_id         bigint PRIMARY KEY,
    kind            catalog.code NOT NULL DEFAULT 'series' CHECK (kind = 'series'),
    first_air_date  date,
    last_air_date   date,
    status          text NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ('returning', 'ended', 'canceled', 'unknown')),
    FOREIGN KEY (item_id, kind) REFERENCES catalog.media_item (id, kind) ON DELETE CASCADE,
    CHECK (first_air_date <= last_air_date)
);

CREATE TABLE catalog.season (
    item_id        bigint PRIMARY KEY,
    kind           catalog.code NOT NULL DEFAULT 'season' CHECK (kind = 'season'),
    series_id      bigint NOT NULL REFERENCES catalog.series (item_id),
    season_number  integer NOT NULL CHECK (season_number >= 0 AND season_number < 1000),
    air_date       date,
    FOREIGN KEY (item_id, kind) REFERENCES catalog.media_item (id, kind) ON DELETE CASCADE,
    UNIQUE (series_id, season_number)
);

CREATE TABLE catalog.episode (
    item_id         bigint PRIMARY KEY,
    kind            catalog.code NOT NULL DEFAULT 'episode' CHECK (kind = 'episode'),
    season_id       bigint NOT NULL REFERENCES catalog.season (item_id),
    episode_number  integer NOT NULL CHECK (episode_number >= 0 AND episode_number < 100000),
    air_date        date,
    runtime_min     integer CHECK (runtime_min > 0 AND runtime_min < 2000),
    FOREIGN KEY (item_id, kind) REFERENCES catalog.media_item (id, kind) ON DELETE CASCADE,
    UNIQUE (season_id, episode_number)
);

CREATE TABLE catalog.edition (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id            uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    work_id              bigint NOT NULL REFERENCES catalog.book (item_id),
    title                catalog.label NOT NULL,
    subtitle             catalog.label,
    language             catalog.lang_code REFERENCES catalog.language,
    publisher            catalog.label,
    published_on         date,
    published_precision  catalog.precision,
    pages                integer CHECK (pages >= 1 AND pages <= 20000),
    format               catalog.code REFERENCES catalog.edition_format,
    description          catalog.long_text,
    is_primary           boolean NOT NULL DEFAULT false,
    provenance           jsonb NOT NULL DEFAULT '{}',
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    deleted_at           timestamptz,
    UNIQUE (id, work_id),
    CONSTRAINT edition_published_ok CHECK (catalog.date_ok(published_on, published_precision)),
    CONSTRAINT edition_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
);
COMMENT ON COLUMN catalog.edition.is_primary IS 'The edition that represents the work (Yamtrack''s chosen edition)';
CREATE UNIQUE INDEX edition_one_primary ON catalog.edition (work_id)
    WHERE is_primary AND deleted_at IS NULL;
CREATE INDEX edition_work ON catalog.edition (work_id);

CREATE TABLE catalog.edition_file (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id      uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    edition_id     bigint NOT NULL REFERENCES catalog.edition,
    format         catalog.code NOT NULL REFERENCES catalog.file_format,
    storage        text NOT NULL CHECK (storage IN ('cwa', 'store')),
    rel_path       catalog.rel_path NOT NULL,
    size_bytes     bigint NOT NULL CHECK (size_bytes > 0),
    sha256         catalog.sha256_hex NOT NULL,
    kosync_md5     catalog.md5_hex,
    added_at       timestamptz,
    missing_since  timestamptz,
    provenance     jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz,
    CONSTRAINT edition_file_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
);
COMMENT ON COLUMN catalog.edition_file.kosync_md5 IS 'KOReader partial MD5, the document id Readest sends to /kosync';
CREATE UNIQUE INDEX edition_file_path ON catalog.edition_file (storage, rel_path) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX edition_file_sha256 ON catalog.edition_file (sha256) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX edition_file_kosync ON catalog.edition_file (kosync_md5) WHERE deleted_at IS NULL;
CREATE INDEX edition_file_edition ON catalog.edition_file (edition_id);

CREATE TABLE catalog.person (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id       uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    name            catalog.label NOT NULL,
    sort_name       catalog.label NOT NULL,      -- set by trigger unless given
    disambiguation  catalog.label,
    provenance      jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    CONSTRAINT person_provenance_object CHECK (jsonb_typeof(provenance) = 'object'),
    CONSTRAINT person_name_has_key CHECK (catalog.name_key(name) <> '')
);
-- "Jose Saramago" and "José Saramago" are one person (unless disambiguated).
CREATE UNIQUE INDEX person_name_key ON catalog.person (catalog.name_key(name), coalesce(disambiguation, ''))
    WHERE deleted_at IS NULL;
CREATE INDEX person_name_trgm ON catalog.person USING gin (lower(catalog.f_unaccent(name)) ext.gin_trgm_ops);

CREATE TABLE catalog.contribution (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id    bigint NOT NULL REFERENCES catalog.person,
    role         catalog.code NOT NULL REFERENCES catalog.contributor_role,
    item_id      bigint REFERENCES catalog.media_item ON DELETE CASCADE,
    edition_id   bigint REFERENCES catalog.edition ON DELETE CASCADE,
    position     smallint NOT NULL DEFAULT 1 CHECK (position >= 1),
    credited_as  catalog.label,
    source       catalog.code NOT NULL REFERENCES catalog.data_source,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contribution_one_owner CHECK (num_nonnulls(item_id, edition_id) = 1),
    CONSTRAINT contribution_unique UNIQUE NULLS NOT DISTINCT (item_id, edition_id, role, person_id),
    CONSTRAINT contribution_position UNIQUE NULLS NOT DISTINCT (item_id, edition_id, role, position)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX contribution_person ON catalog.contribution (person_id);
CREATE INDEX contribution_edition ON catalog.contribution (edition_id) WHERE edition_id IS NOT NULL;

CREATE TABLE catalog.external_id (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider    catalog.code NOT NULL REFERENCES catalog.provider,
    value       text NOT NULL CHECK (value = btrim(value) AND value <> '' AND length(value) <= 200),
    item_id     bigint REFERENCES catalog.media_item ON DELETE CASCADE,
    edition_id  bigint REFERENCES catalog.edition ON DELETE CASCADE,
    person_id   bigint REFERENCES catalog.person ON DELETE CASCADE,
    source      catalog.code NOT NULL REFERENCES catalog.data_source,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT external_id_one_owner CHECK (num_nonnulls(item_id, edition_id, person_id) = 1),
    CONSTRAINT external_id_unique UNIQUE (provider, value)
);
CREATE INDEX external_id_item ON catalog.external_id (item_id) WHERE item_id IS NOT NULL;
CREATE INDEX external_id_edition ON catalog.external_id (edition_id) WHERE edition_id IS NOT NULL;

-- Validates the value against the provider's pattern (and the ISBN check
-- digit) and that it hangs off an allowed level.
CREATE FUNCTION catalog.external_id_check() RETURNS trigger
    LANGUAGE plpgsql
AS $$
DECLARE
    p catalog.provider;
    level text := CASE WHEN NEW.item_id IS NOT NULL THEN 'item'
                       WHEN NEW.edition_id IS NOT NULL THEN 'edition'
                       ELSE 'person' END;
BEGIN
    SELECT * INTO p FROM catalog.provider WHERE code = NEW.provider;
    IF NEW.value !~ p.pattern THEN
        RAISE EXCEPTION 'external id % "%" does not match %', NEW.provider, NEW.value, p.pattern
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.provider = 'isbn13' AND NOT catalog.isbn13_ok(NEW.value) THEN
        RAISE EXCEPTION 'ISBN-13 % has a wrong check digit', NEW.value
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT level = ANY (p.applies_to) THEN
        RAISE EXCEPTION 'external id % cannot be attached to a %', NEW.provider, level
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER external_id_check BEFORE INSERT OR UPDATE ON catalog.external_id
    FOR EACH ROW EXECUTE FUNCTION catalog.external_id_check();

CREATE TABLE catalog.cover (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id  bigint REFERENCES catalog.edition ON DELETE CASCADE,
    item_id     bigint REFERENCES catalog.media_item ON DELETE CASCADE,
    source      catalog.code NOT NULL REFERENCES catalog.data_source,
    source_url  catalog.http_url,
    local_path  catalog.rel_path,                 -- covers/ab/<sha256>.<ext>
    sha256      catalog.sha256_hex,
    mime        text CHECK (mime IN ('image/jpeg', 'image/png', 'image/webp')),
    width       integer CHECK (width > 0 AND width <= 20000),
    height      integer CHECK (height > 0 AND height <= 20000),
    bytes       integer CHECK (bytes > 0),
    fetched_at  timestamptz,
    is_chosen   boolean NOT NULL DEFAULT false,
    note        catalog.label,
    provenance  jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz,
    CONSTRAINT cover_one_owner CHECK (num_nonnulls(edition_id, item_id) = 1),
    CONSTRAINT cover_has_image CHECK (source_url IS NOT NULL OR local_path IS NOT NULL),
    CONSTRAINT cover_local_complete CHECK ((local_path IS NULL) = (sha256 IS NULL)
                                          AND (local_path IS NULL) = (mime IS NULL)),
    CONSTRAINT cover_chosen_is_local CHECK (NOT is_chosen OR local_path IS NOT NULL),
    CONSTRAINT cover_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
);
-- the same URL or the same image only once per edition/item
CREATE UNIQUE INDEX cover_url_unique ON catalog.cover (edition_id, item_id, source_url) NULLS NOT DISTINCT
    WHERE source_url IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX cover_sha_unique ON catalog.cover (edition_id, item_id, sha256) NULLS NOT DISTINCT
    WHERE sha256 IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX cover_one_chosen_edition ON catalog.cover (edition_id)
    WHERE is_chosen AND deleted_at IS NULL AND edition_id IS NOT NULL;
CREATE UNIQUE INDEX cover_one_chosen_item ON catalog.cover (item_id)
    WHERE is_chosen AND deleted_at IS NULL AND item_id IS NOT NULL;
CREATE INDEX cover_edition ON catalog.cover (edition_id) WHERE edition_id IS NOT NULL;
CREATE INDEX cover_item ON catalog.cover (item_id) WHERE item_id IS NOT NULL;

-- ------------------------------------------------- derived-value triggers

-- sort_title follows the title unless the owner set it explicitly.
CREATE FUNCTION catalog.media_item_sort_title() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.sort_title IS NULL THEN
            NEW.sort_title := catalog.make_sort_title(NEW.title, NEW.title_language);
        END IF;
    ELSIF NEW.sort_title IS NOT DISTINCT FROM OLD.sort_title
          AND (NEW.title IS DISTINCT FROM OLD.title
               OR NEW.title_language IS DISTINCT FROM OLD.title_language)
          AND coalesce(OLD.provenance #>> '{sort_title,src}', '') <> 'owner' THEN
        NEW.sort_title := catalog.make_sort_title(NEW.title, NEW.title_language);
    END IF;
    RETURN NEW;
END;
$$;
-- Named so it runs before the provenance guard (triggers fire in name order).
CREATE TRIGGER a10_sort_title BEFORE INSERT OR UPDATE ON catalog.media_item
    FOR EACH ROW EXECUTE FUNCTION catalog.media_item_sort_title();

CREATE FUNCTION catalog.person_sort_name() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.sort_name IS NULL THEN
            NEW.sort_name := catalog.make_sort_name(NEW.name);
        END IF;
    ELSIF NEW.sort_name IS NOT DISTINCT FROM OLD.sort_name
          AND NEW.name IS DISTINCT FROM OLD.name
          AND coalesce(OLD.provenance #>> '{sort_name,src}', '') <> 'owner' THEN
        NEW.sort_name := catalog.make_sort_name(NEW.name);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER a10_sort_name BEFORE INSERT OR UPDATE ON catalog.person
    FOR EACH ROW EXECUTE FUNCTION catalog.person_sort_name();

-- A media item's kind never changes (subtype rows depend on it).
CREATE FUNCTION catalog.media_item_kind_fixed() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'media item % cannot change kind (% -> %)', OLD.id, OLD.kind, NEW.kind
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER a05_kind_fixed BEFORE UPDATE OF kind ON catalog.media_item
    FOR EACH ROW EXECUTE FUNCTION catalog.media_item_kind_fixed();

-- ---------------------------------------- consistency checked at commit

-- Every live item has its subtype row; every live book has exactly one live
-- primary edition.
CREATE FUNCTION catalog.check_item(p_item bigint) RETURNS void
    LANGUAGE plpgsql
AS $$
DECLARE
    it catalog.media_item;
    n int;
BEGIN
    SELECT * INTO it FROM catalog.media_item WHERE id = p_item;
    IF NOT FOUND OR it.deleted_at IS NOT NULL THEN
        RETURN;
    END IF;
    PERFORM 1 FROM (
        SELECT item_id FROM catalog.book WHERE item_id = p_item AND it.kind = 'book'
        UNION ALL SELECT item_id FROM catalog.movie WHERE item_id = p_item AND it.kind = 'movie'
        UNION ALL SELECT item_id FROM catalog.series WHERE item_id = p_item AND it.kind = 'series'
        UNION ALL SELECT item_id FROM catalog.season WHERE item_id = p_item AND it.kind = 'season'
        UNION ALL SELECT item_id FROM catalog.episode WHERE item_id = p_item AND it.kind = 'episode') s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'media item % (%) has no % row', p_item, it.title, it.kind
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF it.kind = 'book' THEN
        SELECT count(*) INTO n FROM catalog.edition
         WHERE work_id = p_item AND is_primary AND deleted_at IS NULL;
        IF n <> 1 THEN
            RAISE EXCEPTION 'book % (%) must have exactly one primary edition, has %', p_item, it.title, n
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
END;
$$;

CREATE FUNCTION catalog.check_item_trigger() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'media_item' THEN
        PERFORM catalog.check_item(NEW.id);
    ELSIF TG_TABLE_NAME = 'edition' THEN
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            PERFORM catalog.check_item(OLD.work_id);
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            PERFORM catalog.check_item(NEW.work_id);
        END IF;
    ELSE  -- subtype tables
        PERFORM catalog.check_item(CASE WHEN TG_OP = 'DELETE' THEN OLD.item_id ELSE NEW.item_id END);
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER z_check_item AFTER INSERT OR UPDATE ON catalog.media_item
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION catalog.check_item_trigger();
CREATE CONSTRAINT TRIGGER z_check_item AFTER INSERT OR UPDATE OR DELETE ON catalog.edition
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION catalog.check_item_trigger();
CREATE CONSTRAINT TRIGGER z_check_item AFTER DELETE ON catalog.book
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION catalog.check_item_trigger();
CREATE CONSTRAINT TRIGGER z_check_item AFTER DELETE ON catalog.movie
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION catalog.check_item_trigger();
CREATE CONSTRAINT TRIGGER z_check_item AFTER DELETE ON catalog.series
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION catalog.check_item_trigger();
CREATE CONSTRAINT TRIGGER z_check_item AFTER DELETE ON catalog.season
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION catalog.check_item_trigger();
CREATE CONSTRAINT TRIGGER z_check_item AFTER DELETE ON catalog.episode
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION catalog.check_item_trigger();

-- updated_at on every mutable table
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.media_item FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.edition FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.edition_file FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.person FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.contribution FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.external_id FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
CREATE TRIGGER a00_updated_at BEFORE UPDATE ON catalog.cover FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
