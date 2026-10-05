-- Experiment 0001: lag in days between when an event happened (SQLDATE) and when GDELT added
-- it (DATEADDED, UTC). Reads SQLDATE and DATEADDED only. Because events_partitioned is
-- partitioned by SQLDATE, this measures, for events dated in the window, how long after their
-- date they were added (censored at today).
--
-- Parameters: @start, @end DATE, used directly as the _PARTITIONTIME window (no session logic).
WITH lags AS (
  SELECT DATE_DIFF(
           DATE(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING))),
           SAFE.PARSE_DATE('%Y%m%d', CAST(SQLDATE AS STRING)),
           DAY) AS lag_days
  FROM `gdelt-bq.gdeltv2.events_partitioned`
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@start) AND TIMESTAMP(@end)
)
SELECT
  COUNT(*) AS n,
  COUNTIF(lag_days IS NULL) AS n_unparsed,
  COUNTIF(lag_days < 0) AS n_negative,
  COUNTIF(lag_days = 0) AS n_0,
  COUNTIF(lag_days = 1) AS n_1,
  COUNTIF(lag_days BETWEEN 2 AND 7) AS n_2_7,
  COUNTIF(lag_days BETWEEN 8 AND 30) AS n_8_30,
  COUNTIF(lag_days BETWEEN 31 AND 365) AS n_31_365,
  COUNTIF(lag_days > 365) AS n_over_365,
  APPROX_QUANTILES(lag_days, 100) AS pct
FROM lags
