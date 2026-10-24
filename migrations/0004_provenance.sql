-- 0004 provenance and owner protection. DESIGN.md §4.4.
--
-- Every write declares its source in the transaction:
--     SELECT set_config('ml.source', 'owner', true), set_config('ml.client', 'console:…', true);
-- The guards below then label changed columns with that source and refuse:
--   * writes without a declared (known) source;
--   * a non-owner change to a column whose current label is 'owner';
--   * a non-owner label 'owner' (labels cannot be forged);
--   * non-owner changes to locked items (and their editions, files, covers,
--     contributions, ids, tags);
--   * a non-owner soft delete of a row the owner created.
-- Errors use SQLSTATE ML001 (missing/unknown source) and ML002 (protected).

CREATE FUNCTION catalog.current_source() RETURNS text
    LANGUAGE plpgsql STABLE
AS $$
DECLARE
    s text := nullif(current_setting('ml.source', true), '');
BEGIN
    IF s IS NULL THEN
        RAISE EXCEPTION 'write without a source: set ml.source for the transaction'
            USING ERRCODE = 'ML001';
    END IF;
    PERFORM 1 FROM catalog.data_source WHERE code = s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown source "%"', s USING ERRCODE = 'ML001';
    END IF;
    RETURN s;
END;
$$;

CREATE FUNCTION catalog.item_locked(p_item bigint) RETURNS boolean
    LANGUAGE sql STABLE
RETURN coalesce((SELECT locked FROM catalog.media_item WHERE id = p_item), false);

CREATE FUNCTION catalog.edition_locked(p_edition bigint) RETURNS boolean
    LANGUAGE sql STABLE
RETURN coalesce((SELECT i.locked FROM catalog.edition e JOIN catalog.media_item i ON i.id = e.work_id
                  WHERE e.id = p_edition), false);

-- Is the catalog row behind this record locked? (r = to_jsonb of the row)
CREATE FUNCTION catalog.row_locked(tbl text, r jsonb) RETURNS boolean
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
    ELSE false
END;

CREATE FUNCTION catalog.provenance_stamp(src text) RETURNS jsonb
    LANGUAGE sql STABLE
RETURN jsonb_build_object('src', src, 'at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));

-- Tables with a provenance column. TG_ARGV = the tracked columns.
CREATE FUNCTION catalog.provenance_guard() RETURNS trigger
    LANGUAGE plpgsql
AS $$
DECLARE
    src text := catalog.current_source();
    tracked text[] := TG_ARGV;
    n jsonb := to_jsonb(NEW);
    o jsonb := CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(OLD) END;
    prov jsonb := coalesce(NEW.provenance, '{}'::jsonb);
    oldprov jsonb := CASE WHEN TG_OP = 'UPDATE' THEN coalesce(OLD.provenance, '{}'::jsonb) ELSE '{}'::jsonb END;
    col text;
    label jsonb;
    old_label jsonb;
    explicit boolean;
    changed boolean;
    k text;
    v jsonb;
BEGIN
    IF jsonb_typeof(prov) <> 'object' THEN
        RAISE EXCEPTION 'provenance must be a JSON object' USING ERRCODE = 'check_violation';
    END IF;
    IF src <> 'owner' THEN
        IF (TG_OP = 'UPDATE' AND catalog.row_locked(TG_TABLE_NAME, o))
           OR (TG_TABLE_NAME <> 'media_item' AND catalog.row_locked(TG_TABLE_NAME, n)) THEN
            RAISE EXCEPTION '%.% belongs to a locked item: only the owner can change it', TG_TABLE_SCHEMA, TG_TABLE_NAME
                USING ERRCODE = 'ML002';
        END IF;
    END IF;

    FOREACH col IN ARRAY tracked LOOP
        label := prov -> col;
        old_label := oldprov -> col;
        IF TG_OP = 'INSERT' THEN
            changed := coalesce(n -> col, 'null'::jsonb) <> 'null'::jsonb;
            explicit := label IS NOT NULL;
        ELSE
            changed := (n -> col) IS DISTINCT FROM (o -> col);
            explicit := label IS DISTINCT FROM old_label;
        END IF;
        IF explicit AND label ->> 'src' = 'owner' AND src <> 'owner' THEN
            RAISE EXCEPTION 'only the owner can label %.% as owner', TG_TABLE_NAME, col USING ERRCODE = 'ML002';
        END IF;
        IF TG_OP = 'UPDATE' AND (changed OR explicit)
           AND old_label ->> 'src' = 'owner' AND src <> 'owner' THEN
            RAISE EXCEPTION '%.% was set by the owner; source % may not change it', TG_TABLE_NAME, col, src
                USING ERRCODE = 'ML002';
        END IF;
        IF col = 'deleted_at' AND changed AND src <> 'owner'
           AND oldprov #>> '{_row,src}' = 'owner' THEN
            RAISE EXCEPTION '%: a row the owner created can only be deleted by the owner', TG_TABLE_NAME
                USING ERRCODE = 'ML002';
        END IF;
        IF changed AND NOT explicit THEN
            prov := jsonb_set(prov, ARRAY[col], catalog.provenance_stamp(src));
        ELSIF label ? 'by' THEN
            -- set by a database trigger in this statement ("by": "trigger" marks it as new)
            prov := jsonb_set(prov, ARRAY[col], label - 'by');
        END IF;
    END LOOP;

    -- who created the row; never changes afterwards
    IF TG_OP = 'INSERT' THEN
        prov := jsonb_set(prov, '{_row}', catalog.provenance_stamp(src));
    ELSE
        prov := CASE WHEN oldprov ? '_row' THEN jsonb_set(prov, '{_row}', oldprov -> '_row') ELSE prov - '_row' END;
    END IF;

    FOR k, v IN SELECT * FROM jsonb_each(prov) LOOP
        IF k <> '_row' AND NOT k = ANY (tracked) THEN
            RAISE EXCEPTION 'provenance key "%" is not a tracked column of %', k, TG_TABLE_NAME
                USING ERRCODE = 'check_violation';
        END IF;
        IF jsonb_typeof(v) <> 'object' OR v ->> 'src' IS NULL
           OR NOT EXISTS (SELECT 1 FROM catalog.data_source WHERE code = v ->> 'src') THEN
            RAISE EXCEPTION 'provenance of %.% must be {"src": <known source>, ...}, got %', TG_TABLE_NAME, k, v
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    NEW.provenance := prov;
    RETURN NEW;
END;
$$;

-- Link tables with a single "source" column (the row is the fact).
CREATE FUNCTION catalog.source_guard() RETURNS trigger
    LANGUAGE plpgsql
AS $$
DECLARE
    src text := catalog.current_source();
BEGIN
    IF TG_OP = 'INSERT' AND NEW.source IS NULL THEN
        NEW.source := src;
    END IF;
    IF src <> 'owner' THEN
        IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.source = 'owner' THEN
            RAISE EXCEPTION '% row was added by the owner; source % may not change it', TG_TABLE_NAME, src
                USING ERRCODE = 'ML002';
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.source = 'owner' THEN
            RAISE EXCEPTION 'only the owner can add % rows as owner', TG_TABLE_NAME USING ERRCODE = 'ML002';
        END IF;
        IF (TG_OP IN ('UPDATE', 'DELETE') AND catalog.row_locked(TG_TABLE_NAME, to_jsonb(OLD)))
           OR (TG_OP IN ('INSERT', 'UPDATE') AND catalog.row_locked(TG_TABLE_NAME, to_jsonb(NEW))) THEN
            RAISE EXCEPTION '% belongs to a locked item: only the owner can change it', TG_TABLE_NAME
                USING ERRCODE = 'ML002';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

-- The sort helpers label what they compute as 'derived'.
CREATE OR REPLACE FUNCTION catalog.media_item_sort_title() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF (TG_OP = 'INSERT' AND NEW.sort_title IS NULL)
       OR (TG_OP = 'UPDATE'
           AND NEW.sort_title IS NOT DISTINCT FROM OLD.sort_title
           AND (NEW.title IS DISTINCT FROM OLD.title OR NEW.title_language IS DISTINCT FROM OLD.title_language)
           AND coalesce(OLD.provenance #>> '{sort_title,src}', '') <> 'owner') THEN
        NEW.sort_title := catalog.make_sort_title(NEW.title, NEW.title_language);
        NEW.provenance := jsonb_set(coalesce(NEW.provenance, '{}'::jsonb), '{sort_title}',
                                    catalog.provenance_stamp('derived') || '{"by": "trigger"}');
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION catalog.person_sort_name() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF (TG_OP = 'INSERT' AND NEW.sort_name IS NULL)
       OR (TG_OP = 'UPDATE'
           AND NEW.sort_name IS NOT DISTINCT FROM OLD.sort_name
           AND NEW.name IS DISTINCT FROM OLD.name
           AND coalesce(OLD.provenance #>> '{sort_name,src}', '') <> 'owner') THEN
        NEW.sort_name := catalog.make_sort_name(NEW.name);
        NEW.provenance := jsonb_set(coalesce(NEW.provenance, '{}'::jsonb), '{sort_name}',
                                    catalog.provenance_stamp('derived') || '{"by": "trigger"}');
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.media_item
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'title', 'title_language', 'sort_title', 'original_title', 'original_language',
        'first_published', 'first_published_precision', 'description', 'locked', 'deleted_at');
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.edition
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'title', 'subtitle', 'language', 'publisher', 'published_on', 'published_precision',
        'pages', 'format', 'description', 'is_primary', 'deleted_at');
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.edition_file
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'edition_id', 'format', 'storage', 'rel_path', 'size_bytes', 'sha256', 'kosync_md5',
        'added_at', 'missing_since', 'deleted_at');
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.person
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'name', 'sort_name', 'disambiguation', 'deleted_at');
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.cover
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'source_url', 'local_path', 'sha256', 'mime', 'width', 'height', 'bytes', 'fetched_at',
        'is_chosen', 'note', 'deleted_at');
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON library.entry
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'status', 'status_changed_at', 'rating', 'rated_at', 'review', 'reviewed_at',
        'added_at', 'deleted_at');
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON library.consumption
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'edition_id', 'state', 'started_on', 'started_precision', 'ended_on', 'ended_precision',
        'progress_pct', 'progress_pages', 'progress_at', 'language', 'medium', 'note', 'deleted_at');

CREATE TRIGGER b10_source BEFORE INSERT OR UPDATE OR DELETE ON catalog.contribution
    FOR EACH ROW EXECUTE FUNCTION catalog.source_guard();
CREATE TRIGGER b10_source BEFORE INSERT OR UPDATE OR DELETE ON catalog.external_id
    FOR EACH ROW EXECUTE FUNCTION catalog.source_guard();
CREATE TRIGGER b10_source BEFORE INSERT OR UPDATE OR DELETE ON library.item_tag
    FOR EACH ROW EXECUTE FUNCTION catalog.source_guard();
