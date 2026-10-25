-- 0014 movies and series (0.6.0): a watch can be of one season, and says
-- where it was watched; movies, series and seasons get the details TMDB has
-- (countries, season and episode counts, episode runtime), each value with
-- its provenance like the rest of the catalog. DESIGN.md §15; D45–D48.

-- --------------------------------------------------------------- sources
-- Where watches come from (a viewing-history export or a watchlist).
INSERT INTO catalog.data_source (code, label) VALUES
    ('netflix', 'Netflix (viewing history, ratings)'),
    ('prime_video', 'Prime Video (watchlist)'),
    ('google_play', 'Google Play / YouTube (purchases)');

-- languages TMDB uses that the list lacked (Cantonese is "cn" there)
INSERT INTO catalog.language (code, bcp47, name_en, sort_collation) VALUES
    ('yue', 'yue', 'Cantonese', NULL),
    ('est', 'et', 'Estonian', NULL)
ON CONFLICT DO NOTHING;

-- -------------------------------------------------------------- platforms
CREATE TABLE library.platform (
    code      catalog.code PRIMARY KEY,
    label     catalog.label NOT NULL,
    position  smallint NOT NULL CHECK (position >= 1)
);
COMMENT ON TABLE library.platform IS 'Where a movie or series was watched (a streaming service, the cinema, TV, a disc)';
INSERT INTO library.platform (code, label, position) VALUES
    ('netflix', 'Netflix', 1),
    ('prime_video', 'Prime Video', 2),
    ('hbo_max', 'HBO Max', 3),
    ('disney_plus', 'Disney+', 4),
    ('apple_tv', 'Apple TV+', 5),
    ('youtube', 'YouTube / Google Play', 6),
    ('viaplay', 'Viaplay', 7),
    ('dr', 'DR TV', 8),
    ('tv2', 'TV 2 Play', 9),
    ('cinema', 'Cinema', 10),
    ('tv', 'TV', 11),
    ('disc', 'DVD / Blu-ray', 12),
    ('other', 'Other', 13);
CREATE TRIGGER z_audit AFTER INSERT OR UPDATE OR DELETE ON library.platform
    FOR EACH ROW EXECUTE FUNCTION audit.capture('code');

-- ----------------------------------------------------------- screen details
CREATE FUNCTION catalog.countries_ok(c text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE
RETURN c IS NULL OR (cardinality(c) BETWEEN 1 AND 30 AND array_to_string(c, ',') ~ '^[A-Z]{2}(,[A-Z]{2})*$');

ALTER TABLE catalog.movie
    ADD COLUMN origin_countries text[] CONSTRAINT movie_countries_ok CHECK (catalog.countries_ok(origin_countries)),
    ADD COLUMN provenance jsonb NOT NULL DEFAULT '{}'
        CONSTRAINT movie_provenance_object CHECK (jsonb_typeof(provenance) = 'object');

ALTER TABLE catalog.series
    ADD COLUMN origin_countries text[] CONSTRAINT series_countries_ok CHECK (catalog.countries_ok(origin_countries)),
    ADD COLUMN season_count integer CHECK (season_count >= 0 AND season_count < 1000),
    ADD COLUMN episode_count integer CHECK (episode_count >= 0 AND episode_count < 100000),
    ADD COLUMN episode_runtime_min integer CHECK (episode_runtime_min > 0 AND episode_runtime_min < 2000),
    ADD COLUMN provenance jsonb NOT NULL DEFAULT '{}'
        CONSTRAINT series_provenance_object CHECK (jsonb_typeof(provenance) = 'object');

ALTER TABLE catalog.season
    ADD COLUMN episode_count integer CHECK (episode_count >= 0 AND episode_count < 100000),
    ADD COLUMN provenance jsonb NOT NULL DEFAULT '{}'
        CONSTRAINT season_provenance_object CHECK (jsonb_typeof(provenance) = 'object'),
    -- a watch names its season and its series together (FK below): the season must belong to it
    ADD CONSTRAINT season_of_series UNIQUE (item_id, series_id);

CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.movie
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard('release_date', 'runtime_min', 'origin_countries');
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.series
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'first_air_date', 'last_air_date', 'status', 'origin_countries', 'season_count', 'episode_count',
        'episode_runtime_min');
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON catalog.season
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard('season_number', 'air_date', 'episode_count');

-- ----------------------------------------------------------------- watches
-- A watch of a series can be of one season (D45): `season_id` with the
-- series as `item_id`, so the season must be one of that series'. `episodes`
-- counts the episodes of that season seen in this watch. `platform` says where.
ALTER TABLE library.consumption
    ADD COLUMN season_id bigint,
    ADD COLUMN episodes integer CHECK (episodes >= 0 AND episodes < 100000),
    ADD COLUMN platform catalog.code REFERENCES library.platform,
    ADD CONSTRAINT consumption_season_of_series FOREIGN KEY (season_id, item_id)
        REFERENCES catalog.season (item_id, series_id),
    ADD CONSTRAINT consumption_episodes_need_season CHECK (episodes IS NULL OR season_id IS NOT NULL),
    ADD CONSTRAINT consumption_season_no_edition CHECK (season_id IS NULL OR edition_id IS NULL);
CREATE INDEX consumption_season ON library.consumption (season_id) WHERE season_id IS NOT NULL;

-- the provenance guard tracks the new columns too
DROP TRIGGER b10_provenance ON library.consumption;
CREATE TRIGGER b10_provenance BEFORE INSERT OR UPDATE ON library.consumption
    FOR EACH ROW EXECUTE FUNCTION catalog.provenance_guard(
        'edition_id', 'state', 'started_on', 'started_precision', 'ended_on', 'ended_precision',
        'progress_pct', 'progress_pages', 'progress_at', 'language', 'medium', 'note', 'deleted_at',
        'season_id', 'episodes', 'platform');
