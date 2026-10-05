-- GDELT GKG 2.1 -> daily documents mentioning each requested theme, global only ('_GLOBAL').
-- Output: session_date, entity, metric, value_sum, n (Python adds source='gkg').
--
-- Parameters
--   @start, @end                    DATE           inclusive session_date window
--   @cutoff_et                      STRING         'HH:MM' America/New_York decision cutoff
--   @themes                         ARRAY<STRING>  exact GKG theme names, e.g. 'ENV_OIL'
--   @gkg_part_start, @gkg_part_end  DATE           _PARTITIONTIME window, computed by Python
--
-- Partitioning (experiment 0001): gkg_partitioned is partitioned by the UTC day of DATE.
-- Timing: DATE (UTC YYYYMMDDHHMMSS of the 15-min batch that processed the document).
-- Column: the V1 `Themes` field ('THEME;THEME;...', one entry per distinct theme) rather than
--   V2Themes ('THEME,offset;...'): the same leaf themes for less than half the bytes, and it
--   also carries parent themes (e.g. TAX_FNCACT) that V2Themes omits. A few newer leaf themes
--   exist only in V2Themes (TAX_DISEASE_COVID_19 and a handful of TAX_* names: 0.03% of
--   document-theme pairs on the day measured) -- check a theme exists in V1 before relying on it.
--   A theme matches only as a whole token.
-- Metrics: theme_<T>_docs  value_sum = docs mentioning T, n = all GKG docs that session day
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
    SAFE_CAST(SPLIT(V2Tone, ',')[SAFE_OFFSET(0)] AS FLOAT64) AS tone
  FROM `gdelt-bq.gdeltv2.gkg_partitioned`
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@gkg_part_start) AND TIMESTAMP(@gkg_part_end)
),
in_range AS (
  SELECT * FROM docs WHERE session_date BETWEEN @start AND @end
),
totals AS (
  SELECT session_date, COUNT(*) AS n_docs FROM in_range GROUP BY session_date
),
hits AS (
  SELECT d.session_date, t AS theme, COUNT(*) AS n_hit, SUM(d.tone) AS tone_sum
  FROM in_range AS d, UNNEST(d.matched) AS t
  GROUP BY d.session_date, t
)
SELECT h.session_date, '_GLOBAL' AS entity, m.metric, m.value_sum, m.n
FROM hits AS h
JOIN totals AS tt USING (session_date),
UNNEST([
  STRUCT(CONCAT('theme_', h.theme, '_docs') AS metric, CAST(h.n_hit AS FLOAT64) AS value_sum, tt.n_docs AS n),
  (CONCAT('theme_', h.theme, '_tone'), h.tone_sum, h.n_hit)
]) AS m
ORDER BY session_date, entity, metric
