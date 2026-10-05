# 0001 — GDELT on BigQuery: partitioning, bytes per query shape, timing lag, duplicates

Date: 2026-10-05. Measured by the `gdelt_bq` adapter (`src/eventtrade/adapters/gdelt_bq.py`,
`src/eventtrade/adapters/queries/*.sql`).

## Question

1. What does `_PARTITIONTIME` hold in `gdelt-bq.gdeltv2.events_partitioned` and
   `gdelt-bq.gdeltv2.gkg_partitioned`, and how do we filter on `DATEADDED` cheaply?
2. How many bytes does each pull shape scan per day, month and year, and for the whole
   development range 2015-03-02 → 2025-03-31?
3. How late are events added relative to the date they describe (`DATEADDED` vs `SQLDATE`)?
4. How many event rows are duplicates?
5. Which pull plan fits inside the 900 GB/month budget (`config.bq_monthly_budget_bytes`)?

## Setup

- Project `ventong-487205`, on-demand pricing, service-account key, google-cloud-bigquery 3.46.1,
  Python 3.12. Wall time measured from Windows 11 over a home connection.
- Sources read before writing SQL: GDELT Event Codebook V2.0, GKG Codebook V2.1 (both PDFs from
  data.gdeltproject.org), and the live table schemas (`client.get_table`, free).
  - `events_partitioned`: 914,460,984 rows, 396.6 GB, `TimePartitioning(DAY)`, no clustering,
    no required partition filter. Same 61 columns as the codebook; `DATEADDED` and `SQLDATE` are
    INT64, `EventCode`/`EventBaseCode`/`EventRootCode` are STRING (leading zeros kept).
  - `gkg_partitioned`: 1,837,934,488 rows, 21.96 TB, `TimePartitioning(DAY)`, no clustering.
    `DATE` is INT64 `YYYYMMDDHHMMSS`; both V1 (`Themes`, `Locations`, `Organizations`) and V2
    (`V2Themes`, `V2Locations`, `V2Organizations`, with char offsets) columns are present.
- Every query, dry or real, went through `GdeltBigQuery` and was recorded in the `bq_usage`
  table of `data/eventtrade.sqlite`: 125 dry runs and 16 real jobs, **2.14 GB billed in total**
  (budget for this task: 5 GB). Two real jobs were exploratory queries outside `queries/`
  (`v1v2_equiv`, `v1v2_diff`, 0.85 GB together); their SQL is quoted in the method below.
- Bytes below are BigQuery dry-run `total_bytes_processed` (1 GB = 10^9 B) for the query files as
  committed, with `cutoff_et = "15:30"` and the default `lookback_days = 30`. Bytes do not depend
  on how many themes or aliases are passed: the cost is the column scan.

## Method

- **Partitioning.** `partition_probe.sql` (real, 3 × 3 partitions around 2016-06-15, 2020-03-10,
  2025-03-10): per partition, row count and min/max of `DATEADDED`/`SQLDATE` (events) and `DATE`
  (GKG).
- **Bytes.** Dry runs of every query shape over one session day (three different years), one
  month, one year (2019, 2024), the full dev range, and each calendar year separately; plus one
  dry run per single column over 3 partitions to see which columns dominate.
- **Lag.** `lag_sample.sql` (real) over events partitions 2016-06-12..2016-06-20 and
  2024-06-09..2024-06-17: distribution of `DATE(DATEADDED) − SQLDATE` in days.
- **Duplicates.** `dup_sample.sql` (real) over partitions 2016-06-14..16 and 2024-06-12: distinct
  `GLOBALEVENTID`, distinct `(UTC day of DATEADDED, SOURCEURL, EventCode, ActionGeo_FeatureID,
  ActionGeo_CountryCode)`, and the same key plus `Actor1Code, Actor2Code`.
- **V1 vs V2 GKG columns.** One real query on GKG partition 2025-03-09 (229,878 docs) comparing,
  per document, the sets in `Themes` vs `V2Themes` (offsets stripped), `Organizations` vs
  `V2Organizations` (lowercased, offsets stripped), and the country codes in `Locations` vs
  `V2Locations`; a second listed the most frequent tokens present in only one of each pair.
- **Session-date rule.** `session_date_check.sql` applies the SQL expression to literal
  `DATEADDED` values (0 bytes) and is compared with the Python `session_date_of` across DST
  changes and four cutoffs (slow test `test_bigquery_session_date_matches_python_rule`).
- **End-to-end.** One real pull of each shape for session 2025-03-10 (events with
  `lookback_days=1` to keep it at 59 MB); the `_GLOBAL` rows of the by-country theme pull were
  compared with the global theme pull.

## Results

### 1. `_PARTITIONTIME`

| Table | `_PARTITIONTIME` is | Evidence |
|---|---|---|
| `events_partitioned` | **`SQLDATE`** — the date the event happened, not when it was added | In all 9 partitions `min(SQLDATE) = max(SQLDATE) = partition day`, while `DATEADDED` runs from the partition day 00:00 up to a year or more later (partition 2016-06-14 holds rows added as late as 2026-06-12) |
| `gkg_partitioned` | the UTC day of `DATE` (the 15-minute batch time) | In all 9 partitions `DATE` spans exactly 00:00:00–23:45:00 of the partition day |

Consequences, implemented in the adapter:

- **GKG**: a session window maps exactly to the UTC days spanned from the previous day's cutoff to
  the end day's cutoff (`partition_window`). One session day with a 15:30 cutoff reads 2 partitions.
- **Events**: rows *added* on day *t* sit in partition *t* and in every earlier partition. There is
  no cheap exact `DATEADDED` filter. The adapter scans `[start − lookback_days, end]` and, per row,
  keeps only events whose `SQLDATE` partition is ≥ `session_date − lookback_days`. This makes the
  output independent of the pull window (a one-day pull equals the same day inside a ten-year
  pull), so backtest and live pulls agree. `DATEADDED − SQLDATE` was never negative, so no
  partition after `end` is needed and an end of 2025-03-31 never reads the 2025-04-01 partition.

### 2. Bytes per query shape (GB, dry run)

| Window | events_daily | events, no dedup | gkg_themes_daily | gkg_themes_country_daily | gkg_orgs_daily |
|---|---|---|---|---|---|
| 1 session day 2016-06-15 | 1.68 | 0.82 | 1.07 | 1.31 | 0.28 |
| 1 session day 2020-03-10 | 1.21 | 0.55 | 0.55 | 0.68 | 0.13 |
| 1 session day 2025-03-10 | 1.07 | 0.49 | 0.36 | 0.44 | 0.09 |
| 1 month 2019-06 | 2.42 | 1.11 | 8.05 | 9.87 | 2.01 |
| 1 year 2019 | 15.6 | 7.2 | 94.5 | 115.6 | 23.6 |
| 1 year 2024 | 13.3 | 6.0 | 78.6 | 95.4 | 19.7 |
| **Dev range, one query** | **143.0** | **66.5** | **982.8** | **1,205.9** | **245.2** |
| Dev range, as 11 yearly chunks | 153.5 | — | 984.7 | 1,208.2 | 245.6 |

Per calendar year (GB): events 14.7 (2015 from Mar), 20.5, 19.3, 17.6, 15.6, 12.7, 11.5, 11.0,
13.3, 13.3, 4.0 (2025 Q1); themes by country 121.6, 191.6, 164.5, 141.3, 115.6, 98.3, 86.1, 74.4,
96.5, 95.4, 22.9; orgs 26.0, 39.3, 33.6, 28.9, 23.6, 18.7, 16.8, 14.6, 19.8, 19.7, 4.7. GKG volume
peaked in 2016 and has fallen since.

A single-session events pull costs ~1.1–1.7 GB because it reads the 30-day lookback; the per-day
marginal cost inside a long pull is ~0.04 GB. Of the events bytes, `SOURCEURL` (needed only for
dedup) is ~60%: dedup roughly doubles event cost (143 vs 66.5 GB over the dev range).

Column bytes over 3 partitions around 2020-03-10 (GB): `V2Themes` 1.51, `Themes` 0.70,
`V2Locations` 0.57, `Locations` 0.20, `V2Organizations` 0.097, `Organizations` 0.060, `V2Tone`
0.13, `DATE` 0.011; events `SOURCEURL` 0.075, `DATEADDED` 0.0057.

### 3. V1 vs V2 GKG columns (2025-03-09, 229,878 docs)

| Pair | Same per-doc set | Notes |
|---|---|---|
| `Locations` vs `V2Locations` (country codes) | 99.4% of docs | V1 is 35% of the bytes |
| `Organizations` vs `V2Organizations` | 90.5% of docs | No organisation appeared only in V2; V1 had 28,862 extra doc-org pairs (e.g. `u s secret service` 539 docs, `washington post` 228). V1 is 62% of the bytes |
| `Themes` vs `V2Themes` | 14.5% of docs | V1 additionally lists parent themes (`TAX_FNCACT` 180k docs, `EPU_POLICY`, `TAX_ETHNICITY`, …); only 1,169 doc-theme pairs were V2-only (`TAX_DISEASE_COVID_19` 1,129, a few `TAX_POLITICAL_PARTY_*`). `ENV_OIL`: 4,404 docs in both. V1 is 47% of the bytes |

The committed queries use the V1 columns (`Themes`, `Locations`, `Organizations`) for matching.

### 4. Lag `DATE(DATEADDED) − SQLDATE` (events dated in the window)

| Partitions | Rows | 0 d | 1 d | 2–7 d | 8–30 d | 31–365 d | > 365 d | Negative |
|---|---|---|---|---|---|---|---|---|
| 2016-06-12..20 | 2,779,641 | 97.42% | 0.78% | 0.69% | 0.41% | 0.69% | 0.007% (max 3,650 d) | 0 |
| 2024-06-09..17 | 1,671,553 | 98.31% | 0.34% | 0.57% | 0.40% | 0.39% | 0 (max 365 d) | 0 |

So `lookback_days = 30` drops ~0.4–0.7% of event rows (the ones describing events more than a month
old — anniversaries, trials, retrospectives); 7 days would drop ~0.8–1.1%. The default is 30.

### 5. Duplicates

| Partitions | Rows | Distinct GLOBALEVENTID | Distinct URL+EventCode+ActionGeo (+UTC day) | + Actor1/2 codes | Distinct URLs |
|---|---|---|---|---|---|
| 2016-06-14..16 | 1,115,369 | 100% | 783,357 (−29.8%) | 1,038,835 (−6.9%) | 352,951 |
| 2024-06-12 | 217,090 | 100% | 149,862 (−31.0%) | 202,316 (−6.8%) | 63,843 |

There are no exact duplicate records. About 30% of rows repeat the same action code at the same
place from the same article (typically the same action coded with different actor strings);
~7% repeat even the actors. `events_daily.sql` collapses on URL + EventCode + ActionGeo
(FeatureID and CountryCode) within a session day, as specified, so `n_events` counts "articles
reporting one kind of action at one place", about 30% below raw row counts.

### 6. End-to-end checks

- `session_date_check`: BigQuery and `session_date_of` agree on 20 timestamps × 4 cutoffs,
  including both 2024/2025 DST changes and seconds-past-cutoff.
- Events 2025-03-10 (lookback 1): 126,118 deduplicated events, 228 entities, 20 metrics;
  quad1..4 sum to `n_events`; article-weighted tone −1.79.
- Orgs 2025-03-10: `white house` 4,803 docs, `reuters` 3,975, `united nations` 1,605,
  `u s secret service` 130, `apple inc` 27 of 351,905 docs. Plain `apple` does not occur as an
  organisation.
- Themes 2025-03-10: `_GLOBAL` rows of the by-country pull equal the global pull (max difference
  2e-12 in a tone sum); `ENV_OIL` 8,550 docs globally, US 3,002, UK 1,903, RS 1,584.
- Wall time of one-day real queries: 2–5 s including the dry run. Repeating an identical query
  hit the BigQuery cache and billed 0 B.

## Recommended pull plan

Rules: per-query cap 50 GB (`config.bq_max_bytes_per_query`), so chunk events by **year**
(≤ 21.5 GB with lookback; monthly chunks would re-read 30 lookback days each time, +~1 GB per
chunk), orgs by **year** (≤ 39.3 GB), themes by **month** (≤ ~17 GB in 2016). Because bytes do
not depend on the number of themes or aliases, **fix the full theme list and alias universe before
pulling**: adding one theme later costs the same as pulling all of them again.

| Billing month | Pull | GB |
|---|---|---|
| 1 | events, full dev range | 154 |
| 1 | GKG orgs, full dev range | 246 |
| 1 | GKG themes by country, 2021-01 → 2025-03 | 375 |
| | *month 1 total* | *775* |
| 2 | GKG themes by country, 2017-01 → 2020-12 | 520 |
| 3 (only if needed) | GKG themes by country, 2015-03 → 2016-12 | 313 |

The by-country theme pull includes `_GLOBAL`, so the global-only shape never needs a separate
full-range pull. Daily incremental pulls after the backfill cost ~1.1 GB (events) + ~0.4 GB
(themes by country) + ~0.1 GB (orgs) per session day, ~35 GB per month. If two billing months are
not acceptable, start themes in 2017 (8 dev years) and finish everything in ~1.3 months, or
start themes in 2019 and fit month 1 (events + orgs + themes 2019 → 2025-03 ≈ 990 GB is still
too much; 2020 → 2025-03 ≈ 874 GB fits only with no other usage that month).

## What this does not establish

- Lag and duplicate rates come from two short windows (one week in 2016 and 2024; three days and
  one day). They are not shown to be stable across all years or across event types; the lag for
  2024 partitions is censored at the measurement date.
- Whether deduplication on URL + EventCode + ActionGeo removes duplicates or real distinct events
  (different actors in one article). It reduces counts by ~30%; whether that improves signal is
  untested.
- V1 vs V2 equivalence was measured on one day (2025-03-09). Theme vocabularies change over time;
  any theme intended for use should be checked to exist in V1 `Themes` over the dev range
  (`TAX_DISEASE_COVID_19` does not, on the day measured).
- Dry-run bytes are what BigQuery would bill under on-demand pricing at the time of measurement;
  public-dataset layouts and storage compression can change. No full-range query was run, so
  full-range wall time and slot behaviour are unknown.
- The month-to-date budget check counts only queries recorded through `UsageLog`. The job list
  for this project also shows queries from other sessions using the same key; those are not
  in `bq_usage` unless they went through the adapter.
- Hold-out: no analysis output covers sessions ≥ 2025-04-01. The partition probe and lag sample
  did, however, read `DATEADDED` values later than 2025-04-01 (as maxima and lag counts of events
  dated before the hold-out). Those are statistics on GDELT publication timing, not on features or
  returns.
