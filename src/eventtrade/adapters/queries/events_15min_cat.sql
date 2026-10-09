-- GDELT 2.0 events -> 15-minute batch counts by category per country and attribution
-- (ADR 0008, "events_15min_cat"). Output: entity, geo, batch_ts, n_events, q1, q2, q3, q4, r14,
-- r16, r17, r18, r19, r20, b163, n_articles, tone_x_articles.
--
-- Parameters
--   @start, @end                  DATE           inclusive America/New_York calendar days
--   @countries                    ARRAY<STRING>  FIPS countries to output ('_GLOBAL' is always added
--                                                for geo 'action')
--   @cameo_codes                  ARRAY<STRING>  CAMEO actor country codes (e.g. 'JPN')
--   @cameo_fips                   ARRAY<STRING>  same length: @cameo_codes[i] is FIPS @cameo_fips[i]
--   @lookback_days                INT64          keep a row only if its event date (SQLDATE) is at
--                                                most this many days before its ET day
--   @ev_part_start, @ev_part_end  DATE           _PARTITIONTIME window, computed by Python
--                                                (@start - lookback .. @end)
--
-- Eligible rows are those of events_15min (ADR 0007):
-- Partitioning (experiment 0001): events_partitioned is partitioned by SQLDATE, not DATEADDED, so
-- the window reaches back @lookback_days. Per-row rules, independent of chunking: SQLDATE >= ET
-- day - lookback, and SQLDATE <= the UTC day of DATEADDED. The ET day of DATEADDED is in
-- @start..@end.
-- Timing: DATEADDED only (UTC, YYYYMMDDHHMMSS); SQLDATE is never used for timing.
-- Batch: the stamp of the kept row (earliest DATEADDED of its dedup group) rounded UP to the
-- 15-minute grid, as epoch seconds UTC. Unlike events_15min, EVERY batch is kept (weekends and all
-- 96 UTC slots): ADR 0008 needs the batches before 12:00 UTC for long windows, their outage check
-- and the 24-hour freshness check.
--
-- geo (ADR 0008, "Data"); each geo is deduplicated on its own, over the WHOLE ET calendar day of
-- DATEADDED; the kept row is the EARLIEST of its group (by DATEADDED, then GLOBALEVENTID) and
-- supplies the stamp and every measure; nothing from later rows is read.
--   'action': dedup on (ET day, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode)
--             as events_15min. entity = ActionGeo_CountryCode (FIPS) if in @countries;
--             '_GLOBAL' = every group, including those with no country.
--   'actor':  a row is attributed to each FIPS country that Actor1CountryCode or Actor2CountryCode
--             maps to through @cameo_codes/@cameo_fips (only countries in @countries). Empty or
--             NULL actor countries are not recovered from Actor*Code; regional / IGO codes (AFR,
--             EUR, WSB, ...) have no mapping and are dropped. Dedup on (ET day, SOURCEURL,
--             EventCode, FIPS country), so an event counts once per distinct actor country. No
--             '_GLOBAL'.
-- Categories: q1..q4 = QuadClass 1..4; rNN = EventRootCode NN (14 protest, 16 reduce relations,
-- 17 coerce, 18 assault, 19 fight, 20 mass violence); b163 = EventBaseCode 163 (sanctions).
WITH raw AS (
  SELECT
    PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)) AS added,
    DATE(_PARTITIONTIME) AS event_day,
    DATEADDED, GLOBALEVENTID, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode,
    QuadClass, EventRootCode, EventBaseCode, NumArticles, AvgTone,
    Actor1CountryCode, Actor2CountryCode
  FROM `gdelt-bq.gdeltv2.events_partitioned`
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@ev_part_start) AND TIMESTAMP(@ev_part_end)
),
dated AS (
  SELECT *, DATE(added, 'America/New_York') AS et_day FROM raw
),
eligible AS (
  SELECT * FROM dated
  WHERE et_day BETWEEN @start AND @end
    AND event_day >= DATE_SUB(et_day, INTERVAL @lookback_days DAY)
    AND event_day <= DATE(added)
),
action_ev AS (
  SELECT
    NULLIF(ActionGeo_CountryCode, '') AS country,
    ARRAY_AGG(
      STRUCT(added, QuadClass AS quad, EventRootCode AS root, EventBaseCode AS base,
             NumArticles AS n_articles, AvgTone AS tone)
      ORDER BY DATEADDED, GLOBALEVENTID LIMIT 1
    )[OFFSET(0)] AS first
  FROM eligible
  GROUP BY et_day, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode
),
actor_mapped AS (
  SELECT e.*, @cameo_fips[OFFSET(i)] AS fips
  FROM eligible AS e
  JOIN UNNEST(@cameo_codes) AS k WITH OFFSET AS i
    ON k IN (e.Actor1CountryCode, e.Actor2CountryCode)
),
actor_ev AS (
  SELECT
    fips,
    ARRAY_AGG(
      STRUCT(added, QuadClass AS quad, EventRootCode AS root, EventBaseCode AS base,
             NumArticles AS n_articles, AvgTone AS tone)
      ORDER BY DATEADDED, GLOBALEVENTID LIMIT 1
    )[OFFSET(0)] AS first
  FROM actor_mapped
  WHERE fips IN UNNEST(@countries)
  GROUP BY et_day, SOURCEURL, EventCode, fips
),
tagged AS (
  SELECT entity, 'action' AS geo, first
  FROM action_ev,
  UNNEST(IF(country IN UNNEST(@countries), [country, '_GLOBAL'], ['_GLOBAL'])) AS entity
  UNION ALL
  SELECT fips AS entity, 'actor' AS geo, first
  FROM actor_ev
),
batched AS (
  SELECT
    entity,
    geo,
    DIV(UNIX_SECONDS(first.added) + 899, 900) * 900 AS batch_ts,
    first.quad AS quad,
    first.root AS root,
    first.base AS base,
    first.n_articles AS n_articles,
    first.tone AS tone
  FROM tagged
)
SELECT
  entity,
  geo,
  batch_ts,
  COUNT(*) AS n_events,
  COUNTIF(quad = 1) AS q1,
  COUNTIF(quad = 2) AS q2,
  COUNTIF(quad = 3) AS q3,
  COUNTIF(quad = 4) AS q4,
  COUNTIF(root = '14') AS r14,
  COUNTIF(root = '16') AS r16,
  COUNTIF(root = '17') AS r17,
  COUNTIF(root = '18') AS r18,
  COUNTIF(root = '19') AS r19,
  COUNTIF(root = '20') AS r20,
  COUNTIF(base = '163') AS b163,
  IFNULL(SUM(n_articles), 0) AS n_articles,
  IFNULL(SUM(tone * n_articles), 0) AS tone_x_articles
FROM batched
GROUP BY entity, geo, batch_ts
ORDER BY entity, geo, batch_ts
