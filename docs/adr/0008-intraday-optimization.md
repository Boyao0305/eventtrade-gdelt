# 0008 — Pre-registered intraday parameter search (hypotheses J*)

Date: 2026-10-09. Written after experiment 0009 (ADR 0007, null) and **before** any new data was
pulled or any configuration below was computed. The user asked whether the intraday parameters
were optimized (window, latency, signal definition, label, news category). They were not:
ADR 0007 fixed one configuration. This ADR searches them, with the search corrected for.

## Disclosure

The 2016–2019 discovery and 2020–2025Q1 confirmation periods were already used by ADR 0007 for
4 coarse signals at one configuration. Two of those signals reappear here: tone and attention,
at the same groups, and one configuration of this grid equals ADR 0007's. This is a second
search on the same data. The correction below counts every configuration of this ADR, and the
DSR count includes ADR 0007's trials.

## Decision

### Data (new pull: `events_15min_cat`)

The same rules as `events_15min` (ADR 0007, implementation notes): whole-ET-day dedup before
any time filter, the earliest row's stamp and measures, the lookback rule, SQLDATE ≤ the UTC day
of DATEADDED, weekday batches at 12:00–19:45 UTC, ET days 2015-12-01 → 2025-03-31.

Output per (entity, geo, batch):
- n_events; QuadClass counts q1–q4;
- root counts r14 (protest), r16 (reduce relations), r17 (coerce), r18 (assault), r19 (fight),
  r20 (mass violence); base count b163 (sanctions);
- Σ NumArticles and Σ AvgTone × NumArticles.

`geo` is the attribution of an event to a country:
- **action**: ActionGeo country (FIPS), as before;
- **actor**: the country is Actor1's or Actor2's country. CAMEO country codes are mapped to the
  FIPS codes of the universe by a committed table, written before the pull; an event counts once
  per distinct actor country.

`_GLOBAL` (geo action) counts all events. Every pull is dry-run first, and the estimated cost
stays inside the user's $20 cap.

### Cells (34)

| Signal (x = share of the country's events in the window, unless noted) | Groups | Sign |
|---|---|---|
| quad1, quad2 (cooperation) | dm, em | + |
| quad3, quad4, r14, r16, r17, r18, r19, r20, b163 | dm, em | − |
| tone (article-weighted mean tone; fires when low) | dm, em | − |
| attention (country events / global events) | dm, em | two-sided |
| actor_conflict ((q3 + q4) share, geo actor) | dm, em | − |
| producer: quad3, quad4, violence (r18 + r19 + r20), pooled over producer countries | commodity | + |
| B spike, negative tone | stocks | − |
| B spike, positive tone | stocks | + |
| B spike, any tone | stocks | two-sided |

That is 13 × 2 + 2 + 3 + 3 = **34 cells**.

Outcomes follow ADR 0007: abnormal return against EFA/EEM/the sector SPDR, and raw return for
commodity ETFs.

### Grid (160 configurations per cell)

**Latency L ∈ {0, 15} min.** Batch b is available at b + 15. A decision at τ uses only batches
with b + 15 ≤ τ − L. ADR 0007 used L = 15.

**Window W ∈ {15, 30, 60, 120} min.** The batches with τ − L − W < b + 15 ≤ τ − L.

**Threshold θ ∈ {2, 2.5, 3, 4}.** Fire on z ≥ θ (z ≤ −θ for tone).

**Exit h ∈ {15, 30, 60, 120 min, close}.**
- Exit at min(τ + h, the close of bar [15:45, 16:00)), using the ADR 0007 exit fallback.

Everything else is unchanged from ADR 0007:
- decision times 09:45–15:00;
- entry at the close of bar [τ−15, τ), requiring asset and hedge bars;
- the same-UTC-slot 20-day baseline, the z floors and the minimum activity;
- outages, declustering (first eligible firing per day) and the liquidity filter.

Windows of 15 and 30 minutes contain one and two batches.
- Outage validity: a window is invalid if it contains a missing batch or one of the two
  batches after one.
- Minimum activity scales with the window: n_events ≥ ⌈10 · W / 60⌉ for Track A and
  docs ≥ ⌈5 · W / 60⌉ for Track B.

### Inference: nested search, corrected

**Discovery (2016-01-04 → 2019-12-31).** For each cell:
- Compute all 160 configurations, each with the ADR 0007 statistic (daily series, NW t with 5
  lags, ≥ 30 events and ≥ 30 days).
- Select the configuration with the largest directional t (|t| for two-sided cells).
- **Search-corrected p** = (1 + #{placebo max-t ≥ observed max-t}) / (1 + valid draws), with
  2,000 draws and seed `config.seed`.
- **Placebo draws.** Each draw is a single mapping of every decision day of the period to a
  distinct placebo day, within ±63 trading days (falling back to the same year, then the
  period, as in ADR 0007).
  - The mapping is applied to every configuration of the cell, so the correlation between
    configurations is kept.
  - Events keep their asset and τ, and are re-checked for eligibility on the placebo day.
- Benjamini–Hochberg at q = 0.10 across the 34 cells.
- Every cell × configuration is logged as a trial (hypothesis J + cell; 5,440 trials).

**Confirmation (2020-01-02 → 2025-03-31).** Each discovered cell, at its single selected
configuration, must pass the ADR 0007 rules:
- placebo p < 0.05 after Holm across the discovered cells;
- the same sign (for two-sided cells, the sign seen in discovery);
- net of costs positive;
- the momentum-residual mean in that sign.

Confirmed cells go to gates 2–3, and then the hold-out once.

## Consequences

- N grows by ~5,500, so the DSR bar rises a lot. That is the cost of searching.
- New pull of ≈ 130–160 GB (dry run first), ≈ $1. Compute is a few hours.
- **If nothing survives,** the intraday line is closed for these GDELT event signals.

## Alternatives rejected

- *Searching on the whole period:* it leaves no out-of-sample confirmation.
- *Separate grids per category reported by raw t:* that ignores the 160-way search.
- *A finer θ or window grid:* it adds search penalty faster than plausible coverage.
- *Text sentiment or ML combinations:* these need other data or a passing single signal first.

## Amendments after review (2026-10-09, before any data was pulled or computed)

An independent statistics review found two blockers and several gaps. All of them are adopted.

1. **Entry.** Every configuration enters at the **open of bar [τ, τ+15)**, and exits are measured
   from that entry. The asset and its hedge must both have that bar.
   - Publication check (job tmp `gdelt_lag.py`): `Last-Modified` of 73 random 2016–2025 export
     files gave a lag after the stamp of median −8.5 min, 99th percentile +2.0 min, maximum
     +3.7 min. So b + 15 is a conservative availability time and L = 0 is kept.
2. **Pull range.** `events_15min_cat` keeps **all 96 UTC batches, every day.** W = 120 windows
   before the open, their two-batch outage check, and ADR 0009's 24-hour freshness test need
   them. The outage mask treats every slot as pulled.
3. **Placebo.** Each draw splits the trading days into consecutive **63-day blocks** (uniform
   random offset per draw) and permutes the days within each block. The same mapping is applied
   to all of a cell's configurations.
   - Before real data is touched, the size is checked on synthetic no-signal returns: the
     rejection rate at 5% must be ≤ 6%.
4. **Max set.** The configurations that are sufficient on the observed data enter the max. In a
   draw, a configuration with < 30 eligible events or days is left out of that draw's max.
5. **Statistic, net of cost.** Selection and the placebo max use the NW t of the daily series of
   s·r − RT, where RT is the event's round trip from ADR 0007 (spreads from discovery bars).
   - For two-sided cells, the statistic is the larger over s ∈ {+1, −1}; the chosen s fixes the
     sign at confirmation.
6. **Momentum control.** The pre-move is [τ − L − W − 15, entry). β is fitted per (track, exit,
   L, W) with the ADR 0007 sampling rule.
7. **Disclosure, corrected.**
   - ADR 0007 stopped at discovery, so the 2020-01-02 → 2025-03-31 confirmation period has never
     been computed for intraday outcomes.
   - The grid (L = 0, positive-spike close exits, actor geography) was chosen **after** seeing
     ADR 0007's discovery table.
   - This ADR explicitly overrides ADR 0007's "not a reason to retest with other settings on
     these data", at the user's request (2026-10-09).
   - Discovery p-values are informative only; confirmation decides.
8. **Actor geography.**
   - Countries come from Actor1CountryCode / Actor2CountryCode (CAMEO); empty codes are not
     recovered from Actor*Code, and regional/IGO codes are dropped.
   - The actor-geo dedup key is (ET day, SOURCEURL, EventCode, actor country), with the earliest
     row's stamp and measures. The mapping is the committed `cameo_fips.csv`.
   - Producer cells use action geography.
9. **Cell count.** The table lists 14 Track A signals × 2 groups + 3 producer + 3 Track B = 34
   cells (the "13 × 2 + 2" in the text was a typo).
