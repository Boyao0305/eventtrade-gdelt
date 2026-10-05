-- GDELT GKG 2.1 -> daily documents mentioning each requested organisation alias (entity = alias).
-- Output: session_date, entity, metric, value_sum, n (Python adds source='gkg').
--
-- Parameters
--   @start, @end                    DATE           inclusive session_date window
--   @cutoff_et                      STRING         'HH:MM' America/New_York decision cutoff
--   @aliases                        ARRAY<STRING>  lowercase organisation names as GKG writes them
--   @gkg_part_start, @gkg_part_end  DATE           _PARTITIONTIME window, computed by Python
--
-- Column: the V1 `Organizations` field ('name;name;...', no character offsets) rather than
-- V2Organizations ('name,offset;...'). Experiment 0001 (one day, 230k docs): V1 is ~40% fewer
-- bytes, no organisation appeared in V2 but not V1, and V1 had ~8% more document-organisation
-- pairs (V2 drops some, e.g. 'u s secret service'). Entries are lowercased; an
-- alias matches only an identical entry (no substring). GKG strips punctuation: 'u s', 'apple inc'.
-- Metrics: org_docs      value_sum = docs mentioning the alias, n = all GKG docs that session day
--          org_tone      value_sum = sum of document tone (V2Tone field 1), n = docs mentioning
--          org_neg_docs  value_sum = mentioning docs with tone < -2, n = docs mentioning
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
      SELECT DISTINCT LOWER(o)
      FROM UNNEST(SPLIT(IFNULL(Organizations, ''), ';')) AS o
      WHERE LOWER(o) IN UNNEST(@aliases)
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
  SELECT d.session_date, a AS alias, COUNT(*) AS n_hit, SUM(d.tone) AS tone_sum,
         COUNTIF(d.tone < -2) AS n_neg
  FROM in_range AS d, UNNEST(d.matched) AS a
  GROUP BY d.session_date, a
)
SELECT h.session_date, h.alias AS entity, m.metric, m.value_sum, m.n
FROM hits AS h
JOIN totals AS tt USING (session_date),
UNNEST([
  STRUCT('org_docs' AS metric, CAST(h.n_hit AS FLOAT64) AS value_sum, tt.n_docs AS n),
  ('org_tone', h.tone_sum, h.n_hit),
  ('org_neg_docs', CAST(h.n_neg AS FLOAT64), h.n_hit)
]) AS m
ORDER BY session_date, entity, metric
