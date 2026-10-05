-- Experiment 0001 probe: what does _PARTITIONTIME of the GDELT tables hold?
-- Per partition day: rows and the min/max of DATEADDED (events) and DATE (GKG) inside it.
-- Reads only the two integer timestamp columns plus SQLDATE, so it is cheap.
--
-- Parameters: @start, @end DATE, used directly as the _PARTITIONTIME window (no session logic).
SELECT
  'events' AS tbl,
  DATE(_PARTITIONTIME) AS part_day,
  COUNT(*) AS n_rows,
  MIN(DATEADDED) AS min_ts,
  MAX(DATEADDED) AS max_ts,
  MIN(SQLDATE) AS min_sqldate,
  MAX(SQLDATE) AS max_sqldate
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@start) AND TIMESTAMP(@end)
GROUP BY part_day
UNION ALL
SELECT
  'gkg' AS tbl,
  DATE(_PARTITIONTIME) AS part_day,
  COUNT(*) AS n_rows,
  MIN(DATE) AS min_ts,
  MAX(DATE) AS max_ts,
  NULL AS min_sqldate,
  NULL AS max_sqldate
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@start) AND TIMESTAMP(@end)
GROUP BY part_day
ORDER BY tbl, part_day
