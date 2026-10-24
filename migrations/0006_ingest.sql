-- 0006 ingest: import runs, raw source records, source -> target id map.
-- DESIGN.md §4.6. Not audited (raw data; the audit covers what it produced).

CREATE TABLE ingest.run (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tool         catalog.code NOT NULL,                 -- 'import', 'fill'
    mode         text NOT NULL CHECK (mode IN ('dry_run', 'apply')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
    sources      jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(sources) = 'object'),
    counts       jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(counts) = 'object'),
    report_path  text,
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK ((status = 'running') = (finished_at IS NULL))
);

CREATE TABLE ingest.source_record (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_-]+)+$'),  -- 'yamtrack.app_book'
    source_key      text NOT NULL CHECK (source_key <> '' AND length(source_key) <= 500),
    payload         jsonb NOT NULL,
    payload_sha256  catalog.sha256_hex NOT NULL,
    first_run_id    bigint NOT NULL REFERENCES ingest.run,
    last_run_id     bigint NOT NULL REFERENCES ingest.run,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_key)
);
COMMENT ON TABLE ingest.source_record IS 'Every source row as it was read: nothing from a source is lost, mapped or not';

CREATE TABLE ingest.id_map (
    source        text NOT NULL,
    source_key    text NOT NULL,
    target_table  text NOT NULL CHECK (target_table IN (
                      'catalog.media_item', 'catalog.edition', 'catalog.edition_file', 'catalog.cover',
                      'catalog.person', 'library.entry', 'library.consumption')),
    target_id     bigint NOT NULL,
    run_id        bigint NOT NULL REFERENCES ingest.run,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_key, target_table)
);
CREATE INDEX id_map_target ON ingest.id_map (target_table, target_id);

CREATE TRIGGER a00_updated_at BEFORE UPDATE ON ingest.source_record
    FOR EACH ROW EXECUTE FUNCTION catalog.set_updated_at();
