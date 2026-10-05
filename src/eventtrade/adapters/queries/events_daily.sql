-- GDELT 2.0 events -> daily aggregates per ActionGeo country (FIPS 10-4) and '_GLOBAL'.
-- Output: session_date, entity, metric, value_sum, n (long format; Python adds source='events').
--
-- Parameters
--   @start, @end                  DATE    inclusive session_date window
--   @cutoff_et                    STRING  'HH:MM' America/New_York decision cutoff
--   @lookback_days                INT64   keep an event only if its event date (SQLDATE) is at
--                                         most this many days before its session_date
--   @ev_part_start, @ev_part_end  DATE    _PARTITIONTIME window, computed by Python
--
-- Partitioning (experiment 0001): events_partitioned is partitioned by SQLDATE (the event's own
-- date), NOT by DATEADDED. Rows for one session can therefore sit in any earlier partition. The
-- lookback rule is applied per row so the result does not depend on how wide the pull window is:
-- a one-day pull and a ten-year pull give identical rows for the same session_date.
--
-- Timing: DATEADDED (UTC, YYYYMMDDHHMMSS, 15-min batches) only; SQLDATE is never used for timing.
-- Dedup: rows sharing (session_date, SOURCEURL, EventCode, ActionGeo_FeatureID,
--        ActionGeo_CountryCode) count once: one article reporting one kind of action at one place.
--        Measures come from that group (MAX IsRootEvent, MAX NumArticles, ANY_VALUE otherwise;
--        all are per-first-article and identical or near-identical within the group).
WITH raw AS (
  SELECT
    -- session_date:begin
    IF(
      TIME(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)), 'America/New_York'))
        > PARSE_TIME('%H:%M', @cutoff_et),
      DATE_ADD(DATE(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)), 'America/New_York')), INTERVAL 1 DAY),
      DATE(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)), 'America/New_York'))
    ) AS session_date,
    -- session_date:end
    DATE(_PARTITIONTIME) AS event_day,
    SOURCEURL, EventCode, EventBaseCode, EventRootCode, IsRootEvent, QuadClass,
    GoldsteinScale, NumArticles, AvgTone, ActionGeo_CountryCode, ActionGeo_FeatureID
  FROM `gdelt-bq.gdeltv2.events_partitioned`
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@ev_part_start) AND TIMESTAMP(@ev_part_end)
),
ev AS (
  SELECT
    session_date,
    NULLIF(ActionGeo_CountryCode, '') AS country,
    ANY_VALUE(EventBaseCode) AS base,
    ANY_VALUE(EventRootCode) AS root,
    MAX(IsRootEvent) AS is_root,
    ANY_VALUE(QuadClass) AS quad,
    ANY_VALUE(GoldsteinScale) AS goldstein,
    MAX(NumArticles) AS n_articles,
    ANY_VALUE(AvgTone) AS tone
  FROM raw
  WHERE session_date BETWEEN @start AND @end
    AND event_day >= DATE_SUB(session_date, INTERVAL @lookback_days DAY)
  GROUP BY session_date, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode
),
agg AS (
  SELECT
    session_date,
    IF(GROUPING(country) = 1, '_GLOBAL', country) AS entity,
    COUNT(*) AS n_events,
    COUNTIF(is_root = 1) AS n_root_events,
    SUM(n_articles) AS n_articles,
    SUM(goldstein) AS goldstein,
    SUM(tone * n_articles) AS tone_x_articles,
    COUNTIF(root = '14') AS r14, COUNTIF(root = '17') AS r17, COUNTIF(root = '18') AS r18,
    COUNTIF(root = '19') AS r19, COUNTIF(root = '20') AS r20,
    COUNTIF(base = '163') AS b163,
    COUNTIF(quad = 1) AS q1, COUNTIF(quad = 2) AS q2, COUNTIF(quad = 3) AS q3, COUNTIF(quad = 4) AS q4,
    COUNTIF(is_root = 1 AND root = '14') AS rr14, COUNTIF(is_root = 1 AND root = '17') AS rr17,
    COUNTIF(is_root = 1 AND root = '18') AS rr18, COUNTIF(is_root = 1 AND root = '19') AS rr19,
    COUNTIF(is_root = 1 AND root = '20') AS rr20
  FROM ev
  GROUP BY GROUPING SETS ((session_date, country), (session_date))
  HAVING GROUPING(country) = 1 OR country IS NOT NULL
)
SELECT session_date, entity, m.metric, m.value_sum, m.n
FROM agg,
UNNEST([
  STRUCT('n_events' AS metric, CAST(n_events AS FLOAT64) AS value_sum, n_events AS n),
  ('n_articles', CAST(n_articles AS FLOAT64), n_events),
  ('goldstein', goldstein, n_events),
  ('tone', tone_x_articles, n_articles),
  ('root_14_protest', r14, n_events),
  ('root_17_coerce', r17, n_events),
  ('root_18_assault', r18, n_events),
  ('root_19_fight', r19, n_events),
  ('root_20_massviolence', r20, n_events),
  ('root_163_sanctions', b163, n_events),
  ('quad1', q1, n_events),
  ('quad2', q2, n_events),
  ('quad3', q3, n_events),
  ('quad4', q4, n_events),
  ('n_root_events', n_root_events, n_root_events),
  ('root_14_protest_isroot', rr14, n_root_events),
  ('root_17_coerce_isroot', rr17, n_root_events),
  ('root_18_assault_isroot', rr18, n_root_events),
  ('root_19_fight_isroot', rr19, n_root_events),
  ('root_20_massviolence_isroot', rr20, n_root_events)
]) AS m
ORDER BY session_date, entity, m.metric
