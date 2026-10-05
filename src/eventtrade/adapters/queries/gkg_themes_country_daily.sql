-- GDELT GKG 2.1 -> daily documents mentioning each requested theme, per country and '_GLOBAL'.
-- Output: session_date, entity, metric, value_sum, n (Python adds source='gkg').
--
-- Parameters: as gkg_themes_daily.sql
--   (@start, @end, @cutoff_et, @themes, @gkg_part_start, @gkg_part_end).
--
-- Country: a document belongs to every distinct FIPS 10-4 country code in its V1 `Locations`
-- field ('type#fullname#CC#adm1#lat#lon#featureid;...', field 3 = CC). V1 Locations gives the
-- same per-document country set as V2Locations in 99.4% of documents (experiment 0001) for about
-- a third of the bytes. Documents with no location count only toward '_GLOBAL'. A document can
-- count toward several countries.
-- Metrics: theme_<T>_docs  value_sum = docs mentioning T, n = all docs for that entity and day
--          theme_<T>_tone  value_sum = sum of document tone (V2Tone field 1), n = docs mentioning T
WITH docs AS (
  SELECT
    -- session_date:begin
    IF(
      TIME(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATE AS STRING)), 'America/New_York'))
        > PARSE_TIME('%H:%M', @cutoff_et),
      DATE_ADD(DATE(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATE AS STRING)), 'America/New_York')), INTERVAL 1 DAY),
      DATE(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATE AS STRING)), 'America/New_York'))
    ) AS session_date,
    -- session_date:end
    ARRAY(
      SELECT t FROM UNNEST(@themes) AS t
      WHERE STRPOS(CONCAT(';', IFNULL(Themes, ''), ';'), CONCAT(';', t, ';')) > 0
    ) AS matched,
    ARRAY_CONCAT(['_GLOBAL'], ARRAY(
      SELECT DISTINCT SPLIT(loc, '#')[SAFE_OFFSET(2)]
      FROM UNNEST(SPLIT(IFNULL(Locations, ''), ';')) AS loc
      WHERE IFNULL(SPLIT(loc, '#')[SAFE_OFFSET(2)], '') != ''
    )) AS entities,
    SAFE_CAST(SPLIT(V2Tone, ',')[SAFE_OFFSET(0)] AS FLOAT64) AS tone
  FROM `gdelt-bq.gdeltv2.gkg_partitioned`
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@gkg_part_start) AND TIMESTAMP(@gkg_part_end)
),
per_entity AS (
  SELECT d.session_date, e AS entity, d.matched, d.tone
  FROM docs AS d, UNNEST(d.entities) AS e
  WHERE d.session_date BETWEEN @start AND @end
),
totals AS (
  SELECT session_date, entity, COUNT(*) AS n_docs
  FROM per_entity
  GROUP BY session_date, entity
),
hits AS (
  SELECT p.session_date, p.entity, t AS theme, COUNT(*) AS n_hit, SUM(p.tone) AS tone_sum
  FROM per_entity AS p, UNNEST(p.matched) AS t
  GROUP BY p.session_date, p.entity, t
)
SELECT h.session_date, h.entity, m.metric, m.value_sum, m.n
FROM hits AS h
JOIN totals AS tt USING (session_date, entity),
UNNEST([
  STRUCT(CONCAT('theme_', h.theme, '_docs') AS metric, CAST(h.n_hit AS FLOAT64) AS value_sum, tt.n_docs AS n),
  (CONCAT('theme_', h.theme, '_tone'), h.tone_sum, h.n_hit)
]) AS m
ORDER BY session_date, entity, metric
