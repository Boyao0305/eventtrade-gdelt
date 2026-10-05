-- Experiment 0001: how many GDELT event rows are duplicates?
--   n_ids        distinct GLOBALEVENTID (exact duplicates of the same record)
--   n_url_code   distinct (SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode)
--                within a UTC day -- the key events_daily.sql dedups on
--   n_url_code_actors  same plus Actor1Code, Actor2Code (stricter: different actors kept)
--
-- Parameters: @start, @end DATE, used directly as the _PARTITIONTIME window (no session logic).
WITH e AS (
  SELECT
    GLOBALEVENTID, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode,
    Actor1Code, Actor2Code, DIV(DATEADDED, 1000000) AS utc_day
  FROM `gdelt-bq.gdeltv2.events_partitioned`
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP(@start) AND TIMESTAMP(@end)
)
SELECT
  COUNT(*) AS n_rows,
  COUNT(DISTINCT GLOBALEVENTID) AS n_ids,
  COUNT(DISTINCT FORMAT('%d|%s|%s|%s|%s', utc_day, IFNULL(SOURCEURL, ''), IFNULL(EventCode, ''),
                        IFNULL(ActionGeo_FeatureID, ''), IFNULL(ActionGeo_CountryCode, ''))) AS n_url_code,
  COUNT(DISTINCT FORMAT('%d|%s|%s|%s|%s|%s|%s', utc_day, IFNULL(SOURCEURL, ''), IFNULL(EventCode, ''),
                        IFNULL(ActionGeo_FeatureID, ''), IFNULL(ActionGeo_CountryCode, ''),
                        IFNULL(Actor1Code, ''), IFNULL(Actor2Code, ''))) AS n_url_code_actors,
  COUNT(DISTINCT SOURCEURL) AS n_urls
FROM e
