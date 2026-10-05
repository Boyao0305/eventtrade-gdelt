-- Reads no table (0 bytes). Applies the exact session_date expression used by every daily query
-- to literal DATEADDED values, so tests can compare BigQuery's answer with the Python rule.
--
-- Parameters: @dateadded ARRAY<INT64>, @cutoff_et STRING; the window params are bound but unused.
SELECT
  DATEADDED AS dateadded,
  -- session_date:begin
    IF(
      TIME(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)), 'America/New_York'))
        > PARSE_TIME('%H:%M', @cutoff_et),
      DATE_ADD(DATE(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)), 'America/New_York')), INTERVAL 1 DAY),
      DATE(DATETIME(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)), 'America/New_York'))
    ) AS session_date
  -- session_date:end
FROM UNNEST(@dateadded) AS DATEADDED
ORDER BY dateadded
