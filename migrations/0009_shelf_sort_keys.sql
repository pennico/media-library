-- 0009: more sort keys for a collection (the console's list view sorts by
-- any column: pages, language, status, genre).

ALTER TABLE library.shelf DROP CONSTRAINT shelf_sort_key_check;
ALTER TABLE library.shelf ADD CONSTRAINT shelf_sort_key_check CHECK (sort_key IN (
    'manual', 'title', 'author', 'added', 'finished', 'rating', 'published', 'pages', 'language', 'status', 'genre'));
