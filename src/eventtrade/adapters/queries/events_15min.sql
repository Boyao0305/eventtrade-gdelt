-- GDELT 2.0 events -> 15-minute batch counts per ActionGeo country (FIPS 10-4) and '_GLOBAL'
-- (ADR 0007, "events_15min"). Output: entity, batch_ts, n_events, n_conflict, n_articles,
-- tone_x_articles.
--
-- Parameters
--   @start, @end                  DATE           inclusive America/New_York calendar days
--   @countries                    ARRAY<STRING>  FIPS countries to output ('_GLOBAL' is always added)
--   @lookback_days                INT64          keep a row only if its event date (SQLDATE) is at
--                                                most this many days before its ET day
--   @ev_part_start, @ev_part_end  DATE           _PARTITIONTIME window, computed by Python
--                                                (@start - lookback .. @end)
--
-- Partitioning (experiment 0001): events_partitioned is partitioned by SQLDATE, not DATEADDED, so
-- the window reaches back @lookback_days. Rules are applied per row so the result does not depend
-- on how the range is chunked: SQLDATE >= ET day - lookback, and SQLDATE <= the UTC day of
-- DATEADDED (a future-dated row would otherwise be kept inside a chunk and lost at its last day,
-- whose next partition is not read).
--
-- Timing: DATEADDED only (UTC, YYYYMMDDHHMMSS); SQLDATE is never used for timing.
-- Dedup over the WHOLE ET calendar day of DATEADDED, before any time filter: rows sharing
-- (ET day, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode) count once. The kept
-- row is the EARLIEST one (by DATEADDED, then GLOBALEVENTID): its stamp, QuadClass, NumArticles
-- and AvgTone are used; nothing from later batches is read.
-- Batch: the earliest DATEADDED rounded UP to the 15-minute grid, as epoch seconds UTC. Kept
-- batches: weekdays (UTC) with UTC time 12:00..19:45 inclusive.
-- '_GLOBAL' is computed over all rows, including rows with no country, before the country filter.
WITH raw AS (
  SELECT
    PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)) AS added,
    DATE(_PARTITIONTIME) AS event_day,
    DATEADDED, GLOBALEVENTID, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode,
    QuadClass, NumArticles, AvgTone
  FROM `gdelt-bq.gdeltv2.events_partitioned`
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@ev_part_start) AND TIMESTAMP(@ev_part_end)
),
dated AS (
  SELECT *, DATE(added, 'America/New_York') AS et_day FROM raw
),
ev AS (
  SELECT
    NULLIF(ActionGeo_CountryCode, '') AS country,
    ARRAY_AGG(
      STRUCT(added, QuadClass AS quad, NumArticles AS n_articles, AvgTone AS tone)
      ORDER BY DATEADDED, GLOBALEVENTID LIMIT 1
    )[OFFSET(0)] AS first
  FROM dated
  WHERE et_day BETWEEN @start AND @end
    AND event_day >= DATE_SUB(et_day, INTERVAL @lookback_days DAY)
    AND event_day <= DATE(added)
  GROUP BY et_day, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode
),
batched AS (
  SELECT
    country,
    DIV(UNIX_SECONDS(first.added) + 899, 900) * 900 AS batch_ts,
    first.quad AS quad,
    first.n_articles AS n_articles,
    first.tone AS tone
  FROM ev
),
kept AS (
  SELECT * FROM batched
  WHERE EXTRACT(DAYOFWEEK FROM TIMESTAMP_SECONDS(batch_ts)) BETWEEN 2 AND 6
    AND MOD(batch_ts, 86400) BETWEEN 43200 AND 71100  -- 12:00:00 .. 19:45:00 UTC
)
SELECT
  entity,
  batch_ts,
  COUNT(*) AS n_events,
  COUNTIF(quad IN (3, 4)) AS n_conflict,
  IFNULL(SUM(n_articles), 0) AS n_articles,
  IFNULL(SUM(tone * n_articles), 0) AS tone_x_articles
FROM kept,
UNNEST(IF(country IN UNNEST(@countries), [country, '_GLOBAL'], ['_GLOBAL'])) AS entity
GROUP BY entity, batch_ts
ORDER BY entity, batch_ts
