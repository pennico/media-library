-- 0007 stats: views for Visuals (role ml_readonly sees only these).
-- DESIGN.md §4.7. Views run with their owner's rights (ml_owner), so the
-- read-only role needs no access to the base tables. Reviews are not exposed.

CREATE VIEW stats.book_reading AS
SELECT c.public_id                                   AS reading_id,
       i.public_id                                   AS item_id,
       i.title,
       i.sort_title,
       (SELECT string_agg(p.name, ', ' ORDER BY ct.position)
          FROM catalog.contribution ct JOIN catalog.person p ON p.id = ct.person_id
         WHERE ct.item_id = i.id AND ct.role = 'author')                  AS authors,
       c.state,
       c.started_on, c.started_precision,
       c.ended_on, c.ended_precision,
       extract(year FROM c.ended_on)::int                                 AS year,
       CASE WHEN c.ended_precision IN ('day', 'month')
            THEN extract(month FROM c.ended_on)::int END                  AS month,
       CASE WHEN c.ended_precision = 'day' THEN c.ended_on END            AS day,
       coalesce(ed.pages, pe.pages)                                       AS pages,
       e.rating,
       c.language                                                         AS read_in,
       i.original_language,
       (SELECT array_agg(t.name::text ORDER BY it.position)
          FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
         WHERE it.item_id = i.id AND t.kind = 'genre' AND t.deleted_at IS NULL) AS genres,
       c.progress_pct,
       c.progress_pages
  FROM library.consumption c
  JOIN library.entry e ON e.item_id = c.item_id AND e.deleted_at IS NULL
  JOIN catalog.media_item i ON i.id = c.item_id AND i.deleted_at IS NULL AND i.kind = 'book'
  LEFT JOIN catalog.edition ed ON ed.id = c.edition_id AND ed.deleted_at IS NULL
  LEFT JOIN catalog.edition pe ON pe.work_id = i.id AND pe.is_primary AND pe.deleted_at IS NULL
 WHERE c.deleted_at IS NULL;

CREATE VIEW stats.books_by_year AS
SELECT year,
       count(*)                       AS books,
       sum(pages)                     AS pages,
       count(pages)                   AS books_with_pages,
       round(avg(rating), 2)          AS avg_rating,
       count(rating)                  AS rated
  FROM stats.book_reading
 WHERE state = 'completed' AND year IS NOT NULL
 GROUP BY year;

-- month is null for readings known to the year only
CREATE VIEW stats.books_by_month AS
SELECT year, month, count(*) AS books, sum(pages) AS pages
  FROM stats.book_reading
 WHERE state = 'completed' AND year IS NOT NULL
 GROUP BY year, month;

CREATE VIEW stats.rating_histogram AS
SELECT floor(e.rating)::int AS bucket, count(*) AS books
  FROM library.entry e
  JOIN catalog.media_item i ON i.id = e.item_id AND i.deleted_at IS NULL AND i.kind = 'book'
 WHERE e.deleted_at IS NULL AND e.rating IS NOT NULL
 GROUP BY 1;

CREATE VIEW stats.top_authors AS
SELECT p.public_id AS person_id, p.name, p.sort_name,
       count(DISTINCT r.item_id) AS books_read,
       round(avg(r.rating), 2)   AS avg_rating,
       sum(r.pages)              AS pages
  FROM stats.book_reading r
  JOIN catalog.media_item i ON i.public_id = r.item_id
  JOIN catalog.contribution ct ON ct.item_id = i.id AND ct.role = 'author'
  JOIN catalog.person p ON p.id = ct.person_id
 WHERE r.state = 'completed'
 GROUP BY p.public_id, p.name, p.sort_name;

CREATE VIEW stats.genres AS
SELECT t.name,
       count(*) FILTER (WHERE e.status = 'completed') AS books_read,
       count(*)                                       AS books
  FROM library.item_tag it
  JOIN library.tag t ON t.id = it.tag_id AND t.kind = 'genre' AND t.deleted_at IS NULL
  JOIN library.entry e ON e.item_id = it.item_id AND e.deleted_at IS NULL
  JOIN catalog.media_item i ON i.id = it.item_id AND i.deleted_at IS NULL AND i.kind = 'book'
 GROUP BY t.name;

CREATE VIEW stats.languages AS
SELECT read_in, original_language, count(*) AS books
  FROM stats.book_reading
 WHERE state = 'completed'
 GROUP BY read_in, original_language;

-- days with dated activity (start, finish or progress) for the calendar
CREATE VIEW stats.activity_days AS
SELECT day, count(*) AS events
  FROM (SELECT c.started_on AS day FROM library.consumption c
         WHERE c.started_precision = 'day' AND c.deleted_at IS NULL
        UNION ALL
        SELECT c.ended_on FROM library.consumption c
         WHERE c.ended_precision = 'day' AND c.deleted_at IS NULL
        UNION ALL
        SELECT (c.progress_at AT TIME ZONE 'Europe/Copenhagen')::date FROM library.consumption c
         WHERE c.progress_at IS NOT NULL AND c.deleted_at IS NULL) d
 GROUP BY day;

CREATE VIEW stats.current AS
SELECT reading_id, item_id, title, authors, state, started_on, started_precision,
       progress_pct, progress_pages, pages
  FROM stats.book_reading
 WHERE state IN ('in_progress', 'paused');

CREATE VIEW stats.library_counts AS
SELECT i.kind AS media_kind, e.status, count(*) AS items
  FROM library.entry e
  JOIN catalog.media_item i ON i.id = e.item_id AND i.deleted_at IS NULL
 WHERE e.deleted_at IS NULL
 GROUP BY i.kind, e.status;
