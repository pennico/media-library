-- 0011: a reading that started on 24 October and finished "October 2026"
-- (month precision) is valid. Compare the start with the END of the finished
-- period, not its first day.

CREATE FUNCTION catalog.period_end(d date, p text) RETURNS date
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN CASE p
    WHEN 'year' THEN (date_trunc('year', d) + interval '1 year - 1 day')::date
    WHEN 'month' THEN (date_trunc('month', d) + interval '1 month - 1 day')::date
    ELSE d
END;

ALTER TABLE library.consumption DROP CONSTRAINT consumption_dates_ordered;
ALTER TABLE library.consumption ADD CONSTRAINT consumption_dates_ordered
    CHECK (started_on <= catalog.period_end(ended_on, ended_precision));
