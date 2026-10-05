# Domain

Facts about GDELT, prices and backtest statistics that constrain any implementation. Measurements
behind some of them are in `docs/experiments/0001` (BigQuery cost and timing) and `0002` (GKG
organisation aliases).

## Glossary

| Term | Meaning |
|---|---|
| **Track A / Track B** | A: event and theme shocks -> ETFs (country, sector, commodity, rates, SPY as `market`). B: company news -> S&P 500 + 400 stocks. |
| **Development period / hold-out** | Dev: 2015-03-02 to 2025-03-31. Hold-out: 2025-04-01 onward, evaluated once per strategy (ADR 0001). |
| **Cutoff** | 15:30 America/New_York. The decision clock of a trading day. |
| **Session date** | The New York calendar date a GDELT row belongs to: the local date of its timestamp, or the next calendar day if the local time is after the cutoff. A timestamp exactly at the cutoff stays on the same day. Not yet a trading day. |
| **Trading day** | A date with price bars. A session date on a weekend or holiday rolls forward to the next trading day. |
| **Entity** | The GDELT-side key of a `gdelt_daily` row: a FIPS 10-4 country code, `_GLOBAL` (all entities), a GKG theme, or a lowercase GKG organisation alias. |
| **Metric, value_sum, n** | A raw daily aggregate kept as a numerator (`value_sum`) and a denominator (`n`) so sums over days, countries or aliases stay exact. Meanings per metric are below. |
| **Stat** | What a feature computes from `value_sum` and `n`: `mean` = value_sum / n; `count` = n; `sum` = value_sum; `share` = n / n(_GLOBAL); `sum_share` = value_sum / value_sum(_GLOBAL of a denominator metric). |
| **Surprise** | A stat against the entity's own trailing `w`-day baseline that excludes the current period (features v2, ADR 0002). `_z.<w>` = (x - mean) / std, NaN when std < 1e-9, clipped to +-10. `_lr.<w>` = ln((x + k) / (mean + k)) with k = one count in the stat's units (1 for count/sum, 1/n for mean, 1/denominator for share/sum_share), so it is defined on zero days. `_sm5_z.<w>` = mean of the last 5 z values (all 5 required), the low-turnover variant. `.0` = untransformed level. |
| **Feature name** | `<source>.<metric>[/<denominator>].<stat>[<k>d]_<z\|lr\|sm5_z>.<window>`, e.g. `events.root_19_fight/n_events.sum_share_z.60`; `<k>d` = summed over k trading days (A3 weekly). Build names with `features.feature_name(spec, transform)`. |
| **Features version** | `features.FEATURES_VERSION` (2 since ADR 0002). Every signal trial's params carry it as `features_version`; v1 trials have no such key. The store does not record which version built the stored features: `signal-study` refuses when no `sm5` feature is stored (a v1 build). |
| **Label** | `ret_oo` = adj_open[t+1+h] / adj_open[t+1] - 1, the tradable return of a signal formed at t's cutoff. `ret_overnight` (close t -> open t+1) and `ret_intraday` are diagnostics, not tradable. |
| **Trial** | One evaluated configuration on one data window, logged append-only, failures included; DSR and PBO need all of them. Stages: signal, model, backtest, holdout. |
| **IC** | Daily cross-sectional Spearman correlation between a feature and a label over the names present that day. |
| **Gate 1/2/3/4** | ADR 0001: signal gate (\|NW t\| >= 3, sign held in 2 of 3 sub-periods, spread beats costs); model gate; overfitting gate (DSR >= 0.95, PBO < 0.5); hold-out (bootstrap CI excludes 0). ADR 0002 amends gate 1: the observed IC sign must equal the pre-registered sign (H0 has none and accepts either), and cost = 4 x per-leg cost x min(1, h x turnover). |
| **Pre-registered sign** | `_Family.sign` in `cli/pipelines.py`, written before any data was seen: A1 conflict counts -1, Goldstein +1, A2/A3 +1, B1 +1, B2 -1, H0 None. `backtest` refuses a feature with no sign unless `--sign` is given. |
| **A4 vol gate (v2)** | Label = ln(realised vol over t+1..t+h / realised vol over t-21..t). IC = Spearman of the residuals of feature and label after a per-day cross-sectional OLS on ln rv5, ln rv22, ln rv60 and ln expanding long-run vol (all ending at t). Passes on \|NW t\| >= 3 with the expected sign held in 2 of 3 sub-periods, and only if the placebo (each ticker's expanding mean of its own earlier feature values) does not pass. Expected sign is "positive in shock units": +1 for A1 conflict counts, -1 for Goldstein (a shock is a low Goldstein). |

## Timing

- **DATEADDED, never SQLDATE, for timing.** SQLDATE is when the event happened and can be years
  before publication. DATEADDED (events) and DATE (GKG) are UTC `YYYYMMDDHHMMSS` of the 15-minute
  batch that processed the row.
- **The cutoff moves in UTC.** 15:30 ET is 19:30 UTC under EDT and 20:30 UTC under EST, so a
  fixed UTC cutoff is wrong half the year. A Friday 16:00 ET row gets a Saturday session date and
  lands on Monday.
- **Weekends and holidays roll forward.** Monday (and the day after a holiday) aggregates 2-4
  calendar days, so raw counts spike there; shares normalised by the same-day `_GLOBAL` total do
  not. Prefer share and mean stats when z-scoring volume.
- **events_partitioned is partitioned by SQLDATE, not DATEADDED.** All 9 partitions probed had
  min = max SQLDATE = the partition day, while DATEADDED ran up to years later. Rows for one
  session therefore sit in earlier partitions too. The adapter scans 30 extra days and drops any
  event dated more than 30 days before its session, per row, so a pull's result does not depend on
  its window. DATEADDED - SQLDATE was never negative; 97-98% of events are added on their own day;
  0.4-0.7% are added more than 30 days later (anniversaries, trials) and are dropped.
- **gkg_partitioned is partitioned by the UTC day of DATE**; each partition holds 00:00-23:45 of
  that day.
- **Event measures are frozen at first sight.** AvgTone, NumArticles, NumMentions and IsRootEvent
  describe only the first 15-minute batch an event was seen in and are never updated.

## GDELT vocabulary traps

- **Country codes are FIPS 10-4, not ISO.** AU = Austria (Australia is AS), EI = Ireland,
  DA = Denmark, SW = Sweden, SZ = Switzerland, SP = Spain, SN = Singapore, RP = Philippines,
  CI = Chile, VM = Vietnam, CG = DR Congo (CF = Congo-Brazzaville), ZA = Zambia, UP = Ukraine,
  NI = Nigeria, KU = Kuwait, IZ = Iraq, AE = UAE, JA = Japan, GM = Germany, CH = China,
  SF = South Africa. Lookup: gdeltproject.org/data/lookups/FIPS.country.txt (latin-1, CRLF).
- **GKG theme names must exist exactly.** Verified: ENV_OIL, ECON_OILPRICE, ENV_NATURALGAS,
  ECON_NATGASPRICE, WB_2936_GOLD, ECON_GOLDPRICE, WB_2937_SILVER, WB_2934_COPPER, ENV_MINING,
  ENV_METALS, FOOD_SECURITY, TAX_FOODSTAPLES_WHEAT/CORN/SOYBEAN, NATURAL_DISASTER_DROUGHT,
  ECON_INTEREST_RATES, ECON_CENTRALBANK, ECON_INFLATION, ECON_SOVEREIGN_DEBT, WB_1045_TREASURY,
  MILITARY, ARMEDCONFLICT, TAX_WEAPONS, HEALTH_PANDEMIC, CYBER_ATTACK, ECON_STOCKMARKET. Do not
  exist: DROUGHT, ECON_TARIFFS, ECON_REAL_ESTATE, ECON_SUPPLYCHAIN, TECH, anything with
  SEMICONDUCTOR or ALUMINUM. Lookup: data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT.
- **GKG V1 vs V2 columns.** V1 `Themes` also lists parent themes (TAX_FNCACT, EPU_POLICY) that
  V2Themes omits, at 47% of the bytes; a few newer leaf themes exist only in V2 (e.g.
  TAX_DISEASE_COVID_19). V1 `Organizations` is a superset of V2Organizations (about 8% more
  document-organisation pairs) at 62% of the bytes. V1 `Locations` gives the same per-document
  country set as V2 in 99.4% of documents at 35% of the bytes.
- **GKG organisation strings** are lowercased with punctuation stripped: no dots or apostrophes
  (`u s secret service`, `us bancorp`), `&` dropped (`procter gamble`, `m t bank`), hyphens kept,
  legal suffixes often kept (`goldman sachs group inc`). An alias matches only an identical entry.
- **Dictionary-word brands are almost never extracted bare.** On 2024-03-12, `apple`, `amazon`,
  `tesla`, `meta`, `target`, `ford`, `bank of america` had zero occurrences; only suffixed forms
  appear (`apple inc` 79 docs). Org-based attention for these stocks is heavily undercounted.
- **A mention is not an article about the company.** Boeing, Intel, Chevron, Alcoa mentions were
  ~9-10 of 12 on topic; Google, Netflix, Hyatt, Verizon 1-2 of 12; Nasdaq, New York Times,
  Gartner 0 of 12 (cited as sources or ticker tags). `core/aliases.is_ambiguous` drops these.
  Nasdaq, Gartner and Morningstar are dropped with any legal suffix too (`nasdaq inc`, `gartner
  inc`, `morningstar inc`: 138k/77k/47k docs in experiment 0003), so NDAQ, IT and MORN have no
  GKG alias. Bare `eaton` is mostly the January 2025 Eaton Fire; `eaton corp` and `eaton plc` stay.
- **Duplicates.** GLOBALEVENTID is unique, but ~30% of event rows repeat (UTC day, SOURCEURL,
  EventCode, ActionGeo FeatureID + country): one article coded with different actor strings. The
  events query counts each such group once.
- **GDELT volume grows and then falls** (GKG peaked in 2016). Raw counts trend; normalise by the
  same-day global total or z-score against a trailing window.

## Metric meanings (`gdelt_daily`)

| Source | Metric | value_sum | n |
|---|---|---|---|
| events | n_events | deduplicated events | same |
| events | n_articles | sum of NumArticles | n_events |
| events | goldstein | sum of GoldsteinScale | n_events |
| events | tone | sum of AvgTone x NumArticles | sum of NumArticles |
| events | root_14_protest, root_17_coerce, root_18_assault, root_19_fight, root_20_massviolence, root_163_sanctions, quad1..quad4 | count of matching events | n_events |
| events | n_root_events; root_<code>_<name>_isroot | count among root events | n_root_events |
| gkg | theme_<T>_docs (entity `_GLOBAL` or FIPS) | documents mentioning T | all documents that day (and country) |
| gkg | theme_<T>_tone | sum of document tone (V2Tone field 1) | documents mentioning T |
| gkg | org_docs (entity = alias) | documents mentioning the alias | all documents that day |
| gkg | org_tone | sum of document tone | documents mentioning the alias |
| gkg | org_neg_docs | mentioning documents with tone < -2 | documents mentioning the alias |

Before features are built, global theme rows are re-keyed so the theme is the entity
(`_GLOBAL`, `theme_ENV_OIL_docs`) -> (`ENV_OIL`, `theme_docs`), which lets an ETF's `gkg_themes`
map like its `fips_countries`. GKG tone is roughly -10..+10 (positive minus negative score).

## Aggregation rules

- **Combine entities on raw sums, never on means or z-scores.** A multi-country ETF's mean is the
  article-weighted mean (sum value_sum / sum n), not the mean of country means.
- **An article matching two aliases of one stock counts twice in the numerator.** Deduplication
  needs article-level data and belongs in the query (`gkg_orgs_daily.sql` is DISTINCT per alias,
  not per ticker). The denominator is different: `org_docs`' n is the day's total documents and is
  counted once per ticker, never summed over aliases (features v2; v1 divided by the number of
  aliases hit). Metrics with a day-total n are listed in `features.DAY_TOTAL_N_METRICS`.
- **Missing is not zero, but no mention is.** A day with no rows of a source at all (an outage) is
  NaN. After an entity's first appearance, count metrics are 0 on observed days with no rows
  (events and GKG orgs; `org_docs` then gets n = the day total, so its share is 0). Tone and
  Goldstein means stay NaN on zero days. Before the first appearance everything is NaN, so a
  ticker's presence at t never depends on later mentions.
- **GKG cost is a column scan.** It does not depend on how many themes or aliases are requested,
  so fix the full lists before pulling.

## Prices

- **Yahoo's raw Open/High/Low/Close/Volume are split-adjusted but not dividend-adjusted; Adj Close
  is both** (checked on AAPL's 2020-08-31 4:1 split and on SPY). `adj_open = open * adj_close /
  close` puts the open on the same total-return scale. Compute returns from adj_open/adj_close.
- `yf.download` does not raise on a failed ticker; it returns all-NaN columns. Liquidated funds
  (RSX, ERUS) have no Yahoo history at all, so a survivorship-free ETF universe is impossible from
  Yahoo; Russia is missing from Track A.
- Stooq answers scripted CSV requests with a JavaScript proof-of-work page (as of 2026-10), so
  there is no free second price source for cross-checks right now.
- Wikipedia S&P lists carry current members only (survivorship bias in Track B), use dotted share
  classes (BRK.B; Yahoo uses BRK-B), and short brand names rather than legal names. They list
  both share classes of a few companies (GOOG/GOOGL, FOX/FOXA, NWS/NWSA); `fetch_index_members`
  keeps one per company (GOOGL, FOXA, NWSA), and `load-universe` deletes the track's stored
  tickers that are no longer listed.
- Track A rates ETFs (TLT, IEF, SHY) have no country mapping since ADR 0002; only SPY maps to
  `US`. Before that all four shared one identical `US` feature.
- `first_trade` is Yahoo's first bar, not the official inception date. KSA, XLRE, XLC start after
  2015-03-02.

## Statistics

- **Overlapping labels make daily ICs autocorrelated** (MA(h-1)); the Newey-West lag must be at
  least h-1, or the t-stat is overstated by about sqrt(h).
- **"Same sign in 2 of 3 sub-periods" is relative to the full-sample sign**; three nonzero numbers
  always have two sharing a sign among themselves.
- **Negative expected signs (A1, B2) are traded short-top/long-bottom**; the gate's sub-periods
  and spread are measured in the pre-registered direction (the observed one only for H0). A
  strong IC of the wrong sign fails (experiment 0003: massviolence `share_z.60` t = +3.07 against
  a registered -1).
- **`sm5` features are evaluated at h = 5 and 20 only**, at gate 1 and in A4.
- **An IC in ret_overnight but not ret_oo is stale pricing**, not an edge: country ETFs whose home
  market closed before the US session carry stale prices.
- **PSR, E[max SR] and DSR take per-period Sharpe ratios and raw (Pearson) kurtosis** (Normal = 3).
  Passing excess kurtosis silently inflates DSR. E[max SR] assumes independent trials; using the
  raw trial count is conservative for correlated trials.
- **PBO is about 0.5, not 1, for no-skill trial sets.** It is near 1 only when in-sample
  outperformance is anti-persistent.
- **Turnover** in a backtest = mean daily sum |traded notional| / pre-trade value, so fee drag is
  about cost_bps x turnover; in a signal report it is the share of the top/bottom quantile sets
  replaced per day.
- **Purge = h** removes one more day than strictly needed: labels at t and s overlap only if
  |t - s| < h.
