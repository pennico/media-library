-- 0017 places to watch (0.8.1): SSD and Streaming join the list, and the
-- owner can add their own from the console (POST /api/platforms). Other stays
-- last. D51.

SELECT set_config('ml.source', 'system', true), set_config('ml.client', 'migration 0017', true);
UPDATE library.platform SET position = 15 WHERE code = 'other';
INSERT INTO library.platform (code, label, position) VALUES ('ssd', 'SSD', 13), ('streaming', 'Streaming', 14);
GRANT SELECT, INSERT, UPDATE (position) ON library.platform TO ml_app;
