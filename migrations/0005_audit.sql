-- 0005 audit: append-only, hash-chained change log. DESIGN.md §4.5.
--
-- One generic trigger (SECURITY DEFINER) on every catalog and library table.
-- Application roles cannot write audit.change directly; nobody can update,
-- delete or truncate it. hash = sha256(prev_hash || canonical row), so
-- audit.verify() detects any removed or altered row.

CREATE TABLE audit.change (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           timestamptz NOT NULL,
    occurred_at  timestamptz,                -- for history imported from elsewhere: when it happened there
    tx           bigint NOT NULL,
    table_name   text NOT NULL,
    row_key      jsonb NOT NULL,
    op           char(1) NOT NULL CHECK (op IN ('I', 'U', 'D')),
    old_row      jsonb,                      -- full row before (U, D)
    new_values   jsonb,                      -- full row (I) or changed columns (U)
    source       text,
    client       text NOT NULL,
    db_role      name NOT NULL,
    reason       text,
    prev_hash    bytea,
    hash         bytea NOT NULL,
    CONSTRAINT change_shape CHECK ((op = 'I') = (old_row IS NULL) AND (op = 'D') = (new_values IS NULL))
);
CREATE INDEX change_row ON audit.change (table_name, row_key);
CREATE INDEX change_at ON audit.change (at);

-- The canonical text that is hashed. Run with TimeZone = UTC so the text of
-- a row never depends on the session.
CREATE FUNCTION audit.canonical(c audit.change) RETURNS bytea
    LANGUAGE sql IMMUTABLE
    SET timezone = 'UTC'
RETURN convert_to(jsonb_build_object(
        'at', c.at, 'occurred_at', c.occurred_at, 'tx', c.tx, 'table', c.table_name,
        'key', c.row_key, 'op', c.op, 'old', c.old_row, 'new', c.new_values,
        'source', c.source, 'client', c.client, 'role', c.db_role, 'reason', c.reason)::text, 'UTF8');

CREATE FUNCTION audit.append(p_table text, p_key jsonb, p_op char, p_old jsonb, p_new jsonb,
                             p_occurred timestamptz DEFAULT NULL) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    SET timezone = 'UTC'
AS $$
DECLARE
    c audit.change;
    client text := nullif(current_setting('ml.client', true), '');
BEGIN
    IF client IS NULL THEN
        RAISE EXCEPTION 'write without a client: set ml.client for the transaction' USING ERRCODE = 'ML001';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        -- the chain reads the latest committed row; a frozen snapshot would fork it
        RAISE EXCEPTION 'audited writes must run in READ COMMITTED' USING ERRCODE = 'ML001';
    END IF;
    PERFORM pg_advisory_xact_lock(7420010001);   -- serialise the chain
    c.at := clock_timestamp();
    c.occurred_at := p_occurred;
    c.tx := pg_current_xact_id()::text::bigint;
    c.table_name := p_table;
    c.row_key := p_key;
    c.op := p_op;
    c.old_row := p_old;
    c.new_values := p_new;
    c.source := nullif(current_setting('ml.source', true), '');
    c.client := client;
    c.db_role := session_user;
    c.reason := nullif(current_setting('ml.reason', true), '');
    SELECT hash INTO c.prev_hash FROM audit.change ORDER BY id DESC LIMIT 1;
    c.hash := sha256(coalesce(c.prev_hash, '\x'::bytea) || audit.canonical(c));
    INSERT INTO audit.change (at, occurred_at, tx, table_name, row_key, op, old_row, new_values,
                              source, client, db_role, reason, prev_hash, hash)
    VALUES (c.at, c.occurred_at, c.tx, c.table_name, c.row_key, c.op, c.old_row, c.new_values,
            c.source, c.client, c.db_role, c.reason, c.prev_hash, c.hash);
END;
$$;
REVOKE ALL ON FUNCTION audit.append(text, jsonb, char, jsonb, jsonb, timestamptz) FROM PUBLIC;

-- AFTER trigger. TG_ARGV = primary key columns.
CREATE FUNCTION audit.capture() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    SET timezone = 'UTC'
AS $$
DECLARE
    o jsonb := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) END;
    n jsonb := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN to_jsonb(NEW) END;
    diff jsonb;
    key jsonb := '{}'::jsonb;
    col text;
BEGIN
    FOREACH col IN ARRAY TG_ARGV LOOP
        key := key || jsonb_build_object(col, coalesce(n, o) -> col);
    END LOOP;
    IF TG_OP = 'UPDATE' THEN
        SELECT jsonb_object_agg(e.key, e.value) INTO diff
          FROM jsonb_each(n) e
         WHERE (o -> e.key) IS DISTINCT FROM e.value AND e.key <> 'updated_at';
        IF diff IS NULL THEN
            RETURN NULL;        -- nothing but updated_at changed
        END IF;
        PERFORM audit.append(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, key, 'U', o, diff);
    ELSIF TG_OP = 'INSERT' THEN
        PERFORM audit.append(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, key, 'I', NULL, n);
    ELSE
        PERFORM audit.append(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, key, 'D', o, NULL);
    END IF;
    RETURN NULL;
END;
$$;

-- History that happened elsewhere (e.g. Yamtrack score edits), recorded with
-- its original time in occurred_at. Chained like any other row.
CREATE FUNCTION audit.record_external(p_table text, p_key jsonb, p_old jsonb, p_new jsonb,
                                      p_occurred timestamptz) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    SET timezone = 'UTC'
AS $$
BEGIN
    IF p_table NOT IN (SELECT n.nspname || '.' || c.relname FROM pg_class c
                         JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname IN ('catalog', 'library') AND c.relkind = 'r') THEN
        RAISE EXCEPTION 'record_external: unknown table %', p_table USING ERRCODE = 'check_violation';
    END IF;
    IF p_occurred IS NULL OR p_occurred > now() THEN
        RAISE EXCEPTION 'record_external: occurred_at must be a past time' USING ERRCODE = 'check_violation';
    END IF;
    PERFORM audit.append(p_table, p_key, 'U', p_old, p_new, p_occurred);
END;
$$;
REVOKE ALL ON FUNCTION audit.record_external(text, jsonb, jsonb, jsonb, timestamptz) FROM PUBLIC;

-- Recompute the chain from p_from (default: the start). Returns the number
-- of rows checked and the id of the first broken row (null = intact).
CREATE FUNCTION audit.verify(p_from bigint DEFAULT NULL, OUT checked bigint, OUT first_bad bigint)
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    SET timezone = 'UTC'
AS $$
DECLARE
    c audit.change;
    prev bytea;     -- hash of the row before; null before the first row
BEGIN
    checked := 0;
    IF p_from IS NOT NULL THEN
        SELECT hash INTO prev FROM audit.change WHERE id < p_from ORDER BY id DESC LIMIT 1;
    END IF;
    FOR c IN SELECT * FROM audit.change WHERE id >= coalesce(p_from, 0) ORDER BY id LOOP
        IF c.prev_hash IS DISTINCT FROM prev
           OR c.hash <> sha256(coalesce(c.prev_hash, '\x'::bytea) || audit.canonical(c)) THEN
            first_bad := c.id;
            RETURN;
        END IF;
        prev := c.hash;
        checked := checked + 1;
    END LOOP;
END;
$$;

CREATE FUNCTION audit.forbid_change() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit.change is append-only' USING ERRCODE = 'ML002';
END;
$$;
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON audit.change
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_change();
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON audit.change
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_change();

-- Attach to every catalog and library table (lookups included).
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.media_item FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.book FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.movie FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.series FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.season FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.episode FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.edition FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.edition_file FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.person FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.contribution FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.external_id FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.cover FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.data_source FOR EACH ROW EXECUTE FUNCTION audit.capture('code');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.language FOR EACH ROW EXECUTE FUNCTION audit.capture('code');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.media_kind FOR EACH ROW EXECUTE FUNCTION audit.capture('code');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.edition_format FOR EACH ROW EXECUTE FUNCTION audit.capture('code');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.file_format FOR EACH ROW EXECUTE FUNCTION audit.capture('code');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.contributor_role FOR EACH ROW EXECUTE FUNCTION audit.capture('code');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON catalog.provider FOR EACH ROW EXECUTE FUNCTION audit.capture('code');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.status FOR EACH ROW EXECUTE FUNCTION audit.capture('code');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.entry FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.consumption FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.tag FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.item_tag FOR EACH ROW EXECUTE FUNCTION audit.capture('item_id', 'tag_id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.shelf FOR EACH ROW EXECUTE FUNCTION audit.capture('id');
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.shelf_item FOR EACH ROW EXECUTE FUNCTION audit.capture('shelf_id', 'item_id');
