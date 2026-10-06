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
| **Screen (S3)** | ADR 0003's pre-registered sub-category screen, run by `eventtrade screen`. Hypothesis id `S3`; its trials are stage `signal` and count in the DSR N like every other trial. |
| **Screen group** | `Asset.screen_group`, filled by `load-universe`. Track A: `dm` / `em` country ETFs by MSCI class as of 2015-03-02, the start of the dev period, not at each fund's inception (Israel DM, Greece EM; Qatar and UAE EM; Argentina and Vietnam frontier and Saudi Arabia standalone on that date, all counted as `em`), `commodity` for commodity ETFs + XLE, empty for everything else (SPY, rates, other sectors). Track B: the stock's GICS sector from the Wikipedia index table (today's sector, not point-in-time). |
| **Screen signal** | `S3.<name>`: the `sm5_z.60` of one family's `mean` stat, built in memory by `core/screen_features` and never stored. Track A families quad1..quad4, root_14 (protest), root_163 (sanctions), violence (roots 18 + 19 + 20 summed before dividing by n_events); Track B org_docs, org_neg_docs, org_tone. |
| **Cell** | signal x screen group x horizon (5 or 20), id `<track>\|<signal>\|<group>\|h<h>`, e.g. `A\|violence\|em\|h20`. 42 in Track A, 66 in Track B. Every pre-registered cell counts: a cell whose IC (>= 5 names with a signal and a label) exists on fewer than half of its discovery days keeps its place, gets p = NaN, counts in BH's m, is never discovered, is left out of the joint test and EB, and is logged as a trial noting "insufficient names". |
| **Screen trial identity** | A screen trial's params include `n_boot`, `mean_block`, `seed` and `members_hash` (first 16 hex digits of the sha256 of the cell's sorted tickers joined by `,`), so a rerun with any other bootstrap setting or membership is a new, counted trial. `n_boot` must be >= 999. A2 trials also carry `hypothesis` (`A2`) and `themes_hash` (the same hash over `TICKER:THEME;THEME` with themes sorted), so a changed theme mapping is a new trial. S3 params have neither key, which keeps their ids unchanged. |
| **Theme screen (A2)** | ADR 0004's pre-registered GKG theme screen, run by `eventtrade screen --track A --set a2` with the S3 statistics unchanged. It has 8 cells: `attention` and `tone` x groups `commodity` (USO UNG GLD SLV CPER DBA XLE) and `sector` (XLB XLC XLF XLI XLK XLP XLRE XLU XLV XLY ITA; XLE is screened with the commodities only) x h 5, 20. Signs: attention two-sided in both groups; tone + for sectors and two-sided for commodities. The groups are pinned in `screen_features.THEME_GROUP_MEMBERS`; the rates ETFs have themes but are not in A2. ADR 0004 says "19 ETFs", but its two lists hold 18 distinct tickers (XLE appears in both lists and is screened only with the commodities). It runs only when every dev month of `gkg_themes_daily` is stored. |
| **Theme signal** | `A2.attention` = the mentions of the ETF's mapped themes (sum over T of the documents mentioning T) / all GKG documents that day. `A2.tone` = summed document tone over the same mentions / the number of mentions. Both are `sm5_z.60`. They count **theme mentions, not distinct documents**: a document carrying two mapped themes counts twice, and weighs twice in tone, because the query stores per-theme counts only (ADR 0004 implementation notes). |
| **Event study (ADR 0005)** | `eventtrade event-study`: threshold-triggered events instead of daily rankings. A **shock score** is the v2 z (floored, clipped) of one signal against a trailing W-day baseline (W 60 or 120, min_periods W // 3) that excludes t, with no smoothing. An **event** fires on day t when the score is >= theta (tone: <= -theta); this is a level rule, not an upward crossing, so a score held above theta re-fires every 21 trading days (judgement call, `core/eventstudy` case table). After an event, the same asset and trigger signal cannot fire for the next 20 trading days (**declustering**: the next event is at p + 21 at the earliest). E4 / E4p split the declustered attention events by same-day tone z (< 0 / >= 0; undefined tone falls in neither), so they partition E5's events. |
| **Event cell / configuration** | Cell = hypothesis x trigger signal x group, id `<E-id>\|<signal>\|<group>`, e.g. `E1\|tone\|dm`, `E5\|org_docs\|all` (Track B pools every stock, each measured against its own GICS sector). There are 47 cells: 44 in A and 3 in B. Configuration = (theta, W, h), with 32 per cell. A configuration needs >= 30 events **and** >= 30 distinct event dates in the period (ADR 0005 Amendment; both counts, `n_events` and `n_event_days`, are in the trial notes); otherwise it is logged as "insufficient events" and cannot be selected. A cell with no sufficient configuration gets p = NaN and still counts in BH's m. |
| **Calendar-time series** | ADR 0005's calendar-time portfolio. An event dated t (entered at the t+1 open, out at the t+1+h open) is active on trading days t+1..t+h. Each active day's value is the equal-weighted mean of the h-day outcomes of the events active that day; days with no active event are left out. Its mean and NW t (horizon h) are the cell statistic. |
| **Event outcomes** | Abnormal return = `ret_oo(h)` minus the equal-weighted `ret_oo(h)` of the other group members that day. Raw return (E2 only) = the commodity ETF's own `ret_oo(h)`. Abnormal vol = ln(forward vol / trailing 22-day vol) minus the group's same-day median (the asset included). The forward vol is open-to-open over opens t+1..t+1+h, from entry, so the close t -> open t+1 gap is not in it; the trailing vol is close-to-close t-21..t. |
| **Event-study multiple testing** | Per cell, the p-value is the **placebo-date max-t** over its sufficient configurations (ADR 0005 Amendment; `eventstudy.max_t_placebo`, n_boot draws, seed = config seed). Each draw takes one uniformly random permutation of the trading days of the embargoed discovery window and moves every event date of every configuration, with all its tickers, to its image (dates stay distinct within a configuration; all h of a (theta, W) trigger set, and the trigger sets, share the mapping). Each configuration's calendar-time t is recomputed and the maximum taken; p = (1 + #{max_null >= max_obs}) / (n_boot + 1). The outcome's drift, the abnormal-vol median bias, clustering and sparsity are thus in the null as in the observed statistic. The stationary bootstrap of the original ADR is not used (it was oversized on sparse date grids). BH at q = 0.10 runs across **all 47 cells of both tracks**, so a track's run reads the other track's logged discovery p-values and stops after discovery until they exist. Confirmation (2020-01-01..2025-03-31, selected configuration, once) needs a one-sided **placebo-date p** below 0.05 after Holm across the discovered cells of both tracks. The placebo p moves every event date, with all its tickers, to random dates of the same period and recomputes the t (n_boot draws, seed = config seed). It also needs a positive mean net of costs in the registered direction: abnormal return 4 legs, raw return 2 legs, ETF 5 bp, stock 10 bp; volatility cells have no cost leg. When both tracks have discovered cells, the first to confirm logs its confirmations with Holm pending. The second completes Holm, and its notes' `holm_verdicts` hold the final verdict of every discovered cell; `pipelines.event_verdicts` reads them. |
| **Discovery / confirmation** | Discovery = feature dates 2015-03-02..2019-12-31, minus an embargo: a horizon-h cell stops at the last date whose label (open t+1 -> open t+1+h) ends by 2019-12-31. Confirmation = 2020-01-01..2025-03-31, discovered cells only, each tested once. Both are dev data; the hold-out is never read. |
| **Joint test** | Per track on discovery data: z = registered sign x NW t (\|t\| for a two-sided cell), S = sum of max(z, 0)^2, M = max z; null from a studentised stationary block bootstrap of dates shared by all cells (mean block 40, seed = config seed). Significant when min(p_S, p_M) < 0.025 (Bonferroni over the two statistics). Not significant: the track stops after logging its discovery trials. |
| **Shrunk IC** | Empirical-Bayes normal-normal posterior of a cell's mean IC in its direction (registered sign, or the discovery-t sign of a two-sided cell), method-of-moments tau^2 across the track's cells, pooled toward their mean. |
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
- **The A2 theme screen counts the day total once, too.** `theme_<T>_docs` is not in
  `DAY_TOTAL_N_METRICS`, so the v2 `gkg.theme_docs.mean` family sums n over the ETF's themes that
  have a row. `core/screen_features.theme_inputs` combines an ETF's themes first instead: the
  numerator is the sum of mentions and n is the session's total, counted once. It also writes a
  zero-mention row on every day whose total is known.
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
- **The screen's joint test is directional.** z = sign x t for a signed cell, |t| for a
  two-sided one; S = sum max(z, 0)^2, M = max z; the track passes if min(p_S, p_M) < 0.025
  (Bonferroni over the two statistics, 5% overall). A cell moving against its registered sign
  adds nothing.
- **Screen shrinkage pools what the direction does not decide.** Signed cells pool sign x IC;
  two-sided cells pool their signed IC unfolded (folding |IC| would bias the pooled mean
  upward). The direction is applied after shrinkage: a two-sided cell's shrunk IC is its
  posterior times the sign of its discovery t.
- **Screen discoveries are BH at q = 0.10, then Holm at 5% on confirmation.** BH controls the
  share of false discoveries among discovered cells, not the chance of any; the confirmation
  period is what protects against a false positive reaching gate 2. Per-cell discovery p-values
  come from the same bootstrap draws as the joint test; confirmation p-values are the one-sided
  normal approximation of the HAC t.
- **Purge = h** removes one more day than strictly needed: labels at t and s overlap only if
  |t - s| < h.
