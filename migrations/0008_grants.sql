-- 0008 grants: least privilege. DESIGN.md §5.2.
-- Roles are created by `medialib bootstrap` (superuser, once per cluster).
--   ml_app       reads and writes catalog/library/ingest (soft delete only for
--                entities), reads audit; never DDL, never TRUNCATE
--   ml_readonly  reads stats.* only

GRANT USAGE ON SCHEMA ext, catalog, library, ingest, stats, audit TO ml_app;
GRANT USAGE ON SCHEMA stats TO ml_readonly;

-- catalog and library: read + insert + update everywhere ...
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA catalog, library TO ml_app;
-- ... except the vocabularies, which only migrations change
REVOKE INSERT, UPDATE ON catalog.data_source, catalog.language, catalog.media_kind,
    catalog.edition_format, catalog.file_format, catalog.contributor_role, catalog.provider,
    library.status FROM ml_app;
-- hard delete only for link tables; entities are soft-deleted (deleted_at)
GRANT DELETE ON catalog.contribution, catalog.external_id, library.item_tag, library.shelf_item TO ml_app;

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ingest TO ml_app;
GRANT SELECT ON audit.change TO ml_app;
GRANT EXECUTE ON FUNCTION audit.record_external(text, jsonb, jsonb, jsonb, timestamptz) TO ml_app;
GRANT EXECUTE ON FUNCTION audit.verify(bigint) TO ml_app;

GRANT SELECT ON ALL TABLES IN SCHEMA stats TO ml_readonly, ml_app;

-- Objects created by later migrations get the same rights.
ALTER DEFAULT PRIVILEGES FOR ROLE ml_owner IN SCHEMA catalog, library, ingest
    GRANT SELECT, INSERT, UPDATE ON TABLES TO ml_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ml_owner IN SCHEMA stats
    GRANT SELECT ON TABLES TO ml_readonly, ml_app;
