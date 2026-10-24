-- 0010: the console's health check reads the schema version.
GRANT USAGE ON SCHEMA meta TO ml_app;
GRANT SELECT ON meta.schema_migration TO ml_app;
