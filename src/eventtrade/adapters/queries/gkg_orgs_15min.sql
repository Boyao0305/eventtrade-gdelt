-- GDELT GKG 2.1 -> 15-minute batch document counts per company (entity = ticker) and '_GLOBAL'
-- (ADR 0007, "gkg_orgs_15min"). Output: entity, batch_ts, docs, tone_sum.
--
-- Parameters
--   @start, @end                    DATE           inclusive America/New_York calendar days
--   @aliases                        ARRAY<STRING>  lowercase organisation names as GKG writes them
--   @alias_tickers                  ARRAY<STRING>  same length: @alias_tickers[i] owns @aliases[i]
--                                                  (an alias may appear once per owning ticker)
--   @gkg_part_start, @gkg_part_end  DATE           _PARTITIONTIME window = @start .. @end (UTC
--                                                  days; every kept batch lies inside them)
--
-- Matching as gkg_orgs_daily: V1 `Organizations` ('name;name;...'), lower-cased, an alias matches
-- only an identical entry. A document counts ONCE per ticker however many of the ticker's aliases
-- it names. tone = V2Tone field 1 (document tone).
-- Batch: GKG DATE (UTC YYYYMMDDHHMMSS) rounded UP to the 15-minute grid, epoch seconds UTC. Kept
-- batches: weekdays (UTC) with UTC time 12:00..19:45 inclusive; the ET day of such a batch is its
-- UTC day, which must be in @start..@end.
-- '_GLOBAL': all GKG documents of the batch and the sum of their tone.
WITH docs AS (
  SELECT
    DIV(UNIX_SECONDS(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATE AS STRING))) + 899, 900) * 900
      AS batch_ts,
    Organizations,
    SAFE_CAST(SPLIT(V2Tone, ',')[SAFE_OFFSET(0)] AS FLOAT64) AS tone
  FROM `gdelt-bq.gdeltv2.gkg_partitioned`
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@gkg_part_start) AND TIMESTAMP(@gkg_part_end)
),
kept AS (
  SELECT * FROM docs
  WHERE EXTRACT(DAYOFWEEK FROM TIMESTAMP_SECONDS(batch_ts)) BETWEEN 2 AND 6
    AND MOD(batch_ts, 86400) BETWEEN 43200 AND 71100  -- 12:00:00 .. 19:45:00 UTC
    AND DATE(TIMESTAMP_SECONDS(batch_ts)) BETWEEN @start AND @end
),
matched AS (
  SELECT
    batch_ts,
    tone,
    ARRAY(
      SELECT DISTINCT @alias_tickers[OFFSET(i)]
      FROM UNNEST(SPLIT(IFNULL(Organizations, ''), ';')) AS o
      JOIN UNNEST(@aliases) AS a WITH OFFSET AS i ON LOWER(o) = a
    ) AS tickers
  FROM kept
)
SELECT ticker AS entity, batch_ts, COUNT(*) AS docs, IFNULL(SUM(tone), 0) AS tone_sum
FROM matched, UNNEST(tickers) AS ticker
GROUP BY ticker, batch_ts
UNION ALL
SELECT '_GLOBAL' AS entity, batch_ts, COUNT(*) AS docs, IFNULL(SUM(tone), 0) AS tone_sum
FROM kept
GROUP BY batch_ts
ORDER BY entity, batch_ts
