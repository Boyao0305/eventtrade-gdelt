# 0007 — Pre-registered intraday event study (hypotheses I1–I6)

Date: 2026-10-07. Written before any intraday price or intraday GDELT row was pulled or read.

The first draft was reviewed the same day by two independent reviewers (statistics/look-ahead
and data feasibility). It was revised before any data was pulled: the reviews found three
look-ahead or validity blockers and a resume bug. This text is the frozen version. Later changes
are amendments only, made before the first intraday statistic is computed.

## Context

Every daily test (experiments 0003–0008, 1,851 trials) traded at the **next open** after a
15:30 ET cutoff. If markets absorb GDELT-visible news within hours, those tests could not see it.
This ADR tests the fastest horizon GDELT supports: GDELT 2.0 publishes in 15-minute batches, so
hours are the finest meaningful scale.

Prices come from Alpaca market data: free Basic plan, paper account, key in
`secrets/alpaca.env`. It serves historical 15-minute **SIP** bars from 2016. The IEX feed has no
2016 bars and covers ~2.5% of volume, so it is not used. Smoke test 2026-10-07: 10 tickers ×
2018, ~98% of regular-session bars present even for thin ETFs (EPHE, GREK, VNM).

## Decision

### Clock

- **Trading days:** days on which SPY has regular-session bars. **Early-close days are
  excluded:** days with no SPY bar starting at 15:45 ET. All times below are New York time unless
  marked UTC.
- **Batch b:** a GDELT 15-minute stamp in UTC. A `DATEADDED`/`DATE` value that is not on the
  15-minute grid is rounded **up** to the next stamp. A batch's rows are treated as available at
  b + 15 min.
- **Decision times τ:** every 15 minutes from 09:45 to 15:00 (22 per day).
- **Signal window at τ:** the four batches b ∈ {τ−75, τ−60, τ−45, τ−30} min, i.e. everything
  available by τ − 15.
- **Entry at τ:** the close of the 15-minute bar [τ−15, τ). The asset **and** its hedge must both
  have that bar (a trade in it). Otherwise there is no event at τ.
- **Exits:**
  - **h60** = τ + 60 min, at the close of bar [x−15, x).
  - **hC** = the close of bar [15:45, 16:00).
  - If the exit bar has no trade, use the last bar after entry. If there is none, the return is 0.
  - Positions never cross a session close.

### Data

**Prices** go in a separate store, `data/intraday.sqlite`.
- Alpaca 15-minute bars, feed `sip`, `adjustment=all`, regular session, 2016-01-04 → 2025-03-31.
- Columns: open, high, low, close, volume, vwap, trade_count.
- Assets:
  - Track A: the 41 country ETFs (`screen_group` dm/em) and the 7-member commodity group.
  - Track B: the 900 stocks, mapped by Alpaca's current-symbol history, so renames like FB→META
    are one series. `BRK-B`-style tickers become `BRK.B`.
- Hedges (not trial assets): **EFA** (DM), **EEM** (EM), **SPY**, and the 11 sector SPDRs.
  - A stock's hedge is its GICS sector SPDR, using the current sector as stored.
  - SPY is used for Communication Services before XLC's first bar (2018-06-19).

**`events_15min`** (BigQuery, Track A):
- Dedup over the **whole ET calendar day**, with the 30-day `SQLDATE` lookback, **before** any
  time filter. One row per (ET day, SOURCEURL, EventCode, ActionGeo_FeatureID, country).
- Each kept row is timed at its **earliest** `DATEADDED`, and its measures (QuadClass,
  NumArticles, AvgTone) come from that earliest row (`ARRAY_AGG … ORDER BY DATEADDED,
  GLOBALEVENTID LIMIT 1`). Nothing from later batches is read.
- Output per (entity, batch): n_events, n_conflict (QuadClass 3 or 4), Σ NumArticles,
  Σ AvgTone × NumArticles.
  - Entities: the 54 FIPS countries mapped by Track A assets, plus `_GLOBAL`. `_GLOBAL` is
    computed over **all** countries, before the country filter.
- Kept batches: weekdays, 12:00–19:45 UTC. This covers 08:30–14:45 ET in both DST regimes and
  the same-UTC baselines below.

**`gkg_orgs_15min`** (BigQuery, Track B):
- Same alias matching as `gkg_orgs_daily` (V1 `Organizations`, exact lower-cased aliases), but
  **a document counts once per company** however many of its aliases it names.
- Output per (ticker, batch): docs and Σ doc tone (V2Tone first field). `_GLOBAL` = all GKG docs
  per batch.
- Same batch filter.

**Range and pulls:**
- GDELT ET days 2015-12-01 → 2025-03-31. The baselines need the 20 trading days before 2016-01-04.
- Pulls are tracked per chunk in a pulls table, not by month-end rows. Every pull is dry-run first.
- Estimated cost ≈ 370–480 GB, ≈ $2.1–2.7 at $6.25/TiB (user cap: $20 cumulative).

### GDELT outages

A batch is **missing** if its `_GLOBAL` count is absent, or below 25% of the median at the same
UTC stamp over the previous 20 trading days. A window is **invalid** if it contains a missing
batch or one of the two batches right after a missing batch (backlog). An invalid window makes
no decision and is no baseline value.

### Signals and events (one configuration — no grid)

**Baseline:** the value of the window with the **same UTC batch stamps** on each of the previous
20 trading days, counting only valid windows whose denominator is > 0 (zeros count). At least 15
are needed.

z = (x − mean) / max(sd, sd_min), clipped to ±10:
- for share signals, sd_min = 1 / mean(baseline denominators), the share of one count;
- for tone, sd_min = 0.5 tone points.

| Signal | x in the window | Fires when | Current-window minimum |
|---|---|---|---|
| A `conflict` | Σn_conflict / Σn_events of the asset's country | z ≥ 3 | n_events ≥ 10 |
| A `tone` | Σ(tone × articles) / Σ articles of the country | z ≤ −3 | n_events ≥ 10 |
| A `attention` | n_events(country) / n_events(_GLOBAL) | z ≥ 3 | n_events ≥ 10 |
| A `producer_conflict` | Σn_conflict / Σn_events pooled over the commodity ETF's producer countries | z ≥ 3 | n_events ≥ 10 |
| B `spike` | company docs / all docs | z ≥ 3 | docs ≥ 5 |

**Track B tone split.** Compare the window's mean doc tone (equal weight per doc) with the
company's mean doc tone over all its pulled docs on the previous 20 trading days (current day
excluded).
- Below → **negative** spike; at or above → **positive** spike.
- With fewer than 20 baseline docs the spike is dropped.

**Declustering:** the first firing per (asset, signal) per trading day.

**Liquidity filter** (price data only, decided before any outcome is computed): exclude assets
whose median discovery-period trade_count per 15-minute bar is < 20.

### Outcomes

- **Abnormal return** (country ETFs, stocks): r(asset) − r(hedge) over the same interval, using
  the bar closes defined above.
- **Raw return** (commodity ETFs): r(asset).
- **Pre-move** (diagnostic and control): the same outcome over [τ−75, τ), using the closes of bars
  [τ−90, τ−75) and [τ−15, τ). It is reported for every cell and never used to select cells.

### Cells and pre-registered signs

| Id | Track | Event → outcome | Groups | Sign | Cells |
|---|---|---|---|---|---|
| I1 | A | `conflict` → abnormal return | dm, em | − | 2 × 2 h |
| I2 | A | `tone` (low) → abnormal return | dm, em | − | 2 × 2 h |
| I3 | A | `attention` → abnormal return | dm, em | two-sided | 2 × 2 h |
| I4 | A | `producer_conflict` → raw return | commodity | + | 1 × 2 h |
| I5 | B | negative spike → abnormal return | all stocks pooled | − | 2 h |
| I6 | B | positive spike → abnormal return | all stocks pooled | + | 2 h |

h ∈ {h60, hC}. **18 cells.**
- A cell is evaluable only with ≥ 30 events and ≥ 30 distinct event days in the period.
  Otherwise it is logged as "insufficient events" with p = NaN and is counted in BH's m.
- Each cell reports its firing rate: events per eligible (asset, day).

### Inference

**Cell statistic.**
- Average the cell's event outcomes per trading day; this gives a daily series.
- The statistic is the mean of that series and its Newey-West t with 5 lags.

**Placebo-day null.**
- In each draw, the cell's events from one original day move **together** to one trading day
  drawn uniformly within ±63 trading days of the original day.
  - Days are distinct across the cell's original days in that draw. If no distinct day is left
    in the ±63 range, the range widens to the same calendar year.
  - Each event keeps its asset and τ. Its outcome is read at (placebo day, asset, τ) under the
    same entry/exit rules, and events not eligible there drop out of the draw.
- p = (1 + #{placebo t ≥ observed t}) / (1 + n_boot), with n_boot = 2,000 and seed `config.seed`.
  - For I1, I2, I4, I5 and I6, the comparison is in the registered direction.
  - For I3 it uses |t|.

**Discovery**, 2016-01-04 → 2019-12-31:
- Benjamini-Hochberg step-up at q = 0.10 across the 18 cells.
- Every cell is logged as a trial (hypothesis `I1`…`I6`).

**Confirmation**, 2020-01-02 → 2025-03-31: discovered cells only, one test each. A cell passes
when **all** of the following hold. Write s for the registered sign; for I3, s = sign(t) in
discovery.
1. The one-sided placebo p in direction s is < 0.05 after Holm across the discovered cells.
2. **Net of costs:** s · mean(daily series) − RT > 0.
   - RT = 2·c_asset + 2·c_hedge for abnormal-return cells, and 2·c_asset for raw-return cells.
   - c per leg = max(registered bp, ½ · the instrument's median Abdi–Ranaldo spread estimate from
     discovery-period 15-minute bars).
   - Registered bp: ETF 5, stock 10, hedge 1.
3. **Not just intraday momentum:** s · mean of the daily series of residuals > 0. A residual is
   the outcome minus β × its pre-move.
   - β comes from OLS of the outcome on the pre-move, per (track, horizon), over 200,000 random
     non-event (asset, day, τ) cells of the discovery period (seed `config.seed`).

Confirmed cells go to gates 2–3 as one combined intraday event portfolio, and DSR's N counts
every logged trial. The hold-out is then used once; it needs a hold-out intraday pull, made only
then.

## Implementation notes (2026-10-07, before any intraday statistic was computed)

These were settled from the code, synthetic tests and API behaviour. No intraday outcome or event
count from real data had been computed when they were written.

**Data and calendar**
- **Alpaca has no bars before 2016-01-01.** The 20 baseline days before 2016-01-04 come from the
  daily SPY calendar (`prices`). They are GDELT baseline days only: no decisions, no placebo days.
- **Bar pages** hold ~900 bars, so the pull costs ~15 requests per ticker-year (~9 h for ~960
  tickers).
- **`events_15min`** adds one per-row rule: SQLDATE must not be after the UTC day of `DATEADDED`.
  This makes the rows independent of chunking; future-dated rows are coding errors.
- **`gkg_orgs_15min`** pulls by calendar year (≤ 39.3 GB per chunk, under the 50 GB cap).
  - NULL sums are stored as 0.
  - A document whose tone cannot be parsed still counts in `docs`.
  - An alias shared by two tickers counts the document for each.
- **Dry-run estimates:** events 127.96 GB, orgs 221.82 GB.

**Prices, hedges and costs**
- **Pre-move before 11:00** (τ − 75 before the open): the price is the session's first open.
- **XLC hedge before its first bar** is SPY, assembled day by day as one hedge series.
- **Liquidity:** median trade count over all regular-session bar slots of the discovery decision
  days, with missing bars counted as 0.
- **Abdi–Ranaldo spread:** two-bar estimates within a day (negative products set to 0), averaged
  per day, then the median over discovery decision days.
- **Round-trip cost of a cell:** the mean over its events of 2·c_asset (+ 2·c_hedge).
- **Prices are stored as float32** in memory; returns are computed in float64.

**Events and inference**
- **Eligibility** (entry bar for the asset and its hedge) is checked **before** declustering. The
  first *eligible* firing of the day is the event.
- **Track B:** spikes are declustered first, then split by tone.
- **Tone baseline values** count when the window has articles > 0.
- **Placebo draws:**
  - A placebo day may coincide with the original day.
  - A draw in which fewer than two event days are eligible has no t and is not counted.
  - p = (1 + exceed) / (1 + valid draws).
- **β** (momentum control) is fitted on cells sampled with replacement from the liquid assets of
  the track. Cells that are events of any signal are removed after sampling.

## Consequences

- +18 discovery trials (plus confirmations).
- ≈ $2.1–2.7 of BigQuery.
- A new `data/intraday.sqlite` store, ~2–3 GB: ~50M stock bars, ~2.5M event rows, ~6–12M
  company rows.
- One configuration per cell: no search penalty, but a wrong θ, window or latency can hide an
  effect. That is accepted, and it is not a reason to retest with other settings on these data.

**Limitations, stated in advance:**
- survivorship (current index constituents) and current GICS sectors;
- hedging a country ETF with EFA/EEM is partial, which attenuates effects (e.g. FXI vs EEM);
- the cost model is approximate.

## Alternatives rejected

- *Minute bars*: GDELT's resolution is 15 minutes. Minute bars add ~15× the storage and no
  information.
- *Grids over θ, window or latency*: the daily search (1,504 configurations) found nothing and
  cost a large N.
- *The pre-open window (news overnight → trade at 09:30)*: this is the daily design, already
  tested.
- *yfinance intraday*: about 60 days (15-minute) or 730 days (1-hour) of history, almost all in
  the hold-out.
- *Group-basket abnormal returns*: stale prices of thin ETFs would dominate a 15-minute basket.
  A single liquid hedge is what a trader would use.
- *A placebo over the whole period*: it assumes returns are exchangeable across years. Hedged
  returns drift (e.g. EM vs EEM in 2020), so draws are kept local (±63 days).
- *Waiting for November's free tier*: it saves ≈ $2.7 at the cost of ~3.5 weeks.
