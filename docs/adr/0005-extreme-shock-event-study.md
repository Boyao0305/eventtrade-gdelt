# 0005 — Pre-registered extreme-shock event study with nested optimization

Date: 2026-10-05. Written before any extreme-event (threshold-triggered) analysis was run.
The dev data has been analysed only as daily cross-sectional rankings (experiments 0003–0005).

## Context

Every test so far ranked all assets every day on a continuous signal. Event trading in practice
acts on **rare, large shocks** and stays out otherwise; a daily ranking dilutes a strong but rare
effect with thousands of quiet days. Also untested: absolute (time-series) direction, and tuning
of thresholds, windows and holding periods. Tuning is allowed here only inside the discovery
period, with every configuration counted and the search corrected for.

## Decision

### Events (triggers)

For an (asset, signal) pair, an **event** occurs on trading day t when the signal's shock score
crosses a threshold θ, computed with data known at t's 15:30 ET cutoff. After an event, the same
pair cannot trigger again for 20 trading days (declustering).

- Shock score = v2 z-score (floored, clipped) of the signal against a trailing baseline of W days
  that excludes t. No smoothing.
- Track A signals: the 7 ADR 0003 families (share within the country's events) plus article-
  weighted tone (event when z ≤ −θ). Assets: country ETFs (DM, EM groups of ADR 0003) and, for
  E2, the commodity group, whose event fires when the shock occurs in any of the ETF's mapped
  producer countries (pooled family share over those countries).
- Track B signals: company attention (`org_docs` share); the event is split by the same-day tone
  z-score into **negative** (tone z < 0) and **positive** (tone z ≥ 0) attention spikes.

### Outcomes (entered at t+1 open, as everywhere)

- **Abnormal return** over h days: the asset's `ret_oo(h)` minus the equal-weighted `ret_oo(h)` of
  the other members of its group on the same day (DM, EM, or the stock's GICS sector).
- **Raw return** (E2 only): the commodity ETF's own `ret_oo(h)` — absolute direction.
- **Abnormal volatility**: ln(forward realised vol over h / trailing 22-day vol) minus the group's
  same-day median of that quantity.

### Hypotheses and pre-registered signs

| Id | Track | Event → outcome | Sign |
|---|---|---|---|
| E1 | A | conflict families (quad3, quad4, protest, sanctions, violence) and low tone in a country → abnormal return of its ETF | − |
| E1c | A | cooperation families (quad1, quad2) → abnormal return | + |
| E2 | A | conflict families (quad4, violence, sanctions, protest) in producer countries → raw return of commodity ETFs | + |
| E3 | A | any family shock → abnormal volatility (DM, EM, commodity) | + |
| E4 | B | negative attention spike → abnormal return | − |
| E4p | B | positive attention spike → abnormal return | + |
| E5 | B | any attention spike → abnormal volatility | + |

Cells: E1/E1c 8 signals × 2 groups = 16; E2 4; E3 8 × 3 = 24; E4, E4p, E5 one each (all 900
stocks pooled) = 3. **47 cells.**

### Inference: calendar-time portfolios

For a cell and a configuration, each trading day holds every event of the last h days
(equal-weighted). The daily mean of the outcome across active events is a time series; its mean
and Newey-West t (lags ≥ h) are the cell statistic. This handles events clustered on the same day
and overlapping holding windows. Days with no active event are excluded. A cell needs at least
30 events in the period to be evaluated; otherwise it is logged as "insufficient events".

### Nested optimization (discovery only)

- Grid per cell: θ ∈ {2, 2.5, 3, 4} × W ∈ {60, 120} × h ∈ {1, 5, 10, 20} = **32 configurations**.
- Discovery 2015-03-02 → 2019-12-31, labels embargoed to end by 2019-12-31. Each of the
  47 × 32 = 1,504 cell-configurations is logged as a trial.
- Per cell, the selected configuration is the one with the largest t in the registered direction.
- **Search correction:** the cell's p-value is the stationary-bootstrap probability that the
  *maximum* t over its 32 configurations (series demeaned to impose the null, all configurations
  resampled on the same dates, mean block 40, n_boot ≥ 999, seed `config.seed`) is at least the
  observed maximum.
- Benjamini–Hochberg at q = 0.10 across the 47 cells on those search-corrected p-values.

### Confirmation and beyond

- Each discovered cell is tested **once** on 2020-01-01 → 2025-03-31 with its single selected
  configuration: same sign, one-sided p < 0.05 after Holm across discovered cells, and the mean
  outcome net of costs positive in the registered direction (ETF 5 bp, stock 10 bp per leg;
  abnormal-return trades pay 4 legs, raw-return trades 2 legs per round trip; volatility cells
  are reported without a cost leg, as they are not directly tradable with the instruments used).
- Confirmed return cells go to gate 2/3 as a combined event portfolio; DSR's N counts every
  logged trial of the track (including all 1,504 discovery configurations). Then the hold-out once.

## Consequences

- N grows by ~1,500; the Deflated Sharpe bar rises sharply. That is the price of searching.
- Uses only data already stored; no BigQuery spend.
- Group-relative abnormal returns remove regional and sector moves but not single-asset beta
  differences; stated as a limitation.

## Alternatives rejected

- *Tuning on the whole dev period*: no out-of-sample confirmation left except the hold-out.
- *Reporting the best configuration's raw t*: ignores the 32-way search; the bootstrap of the
  maximum corrects for it.
- *Event-level t-tests*: events on the same day are not independent; calendar-time portfolios
  are the standard remedy.
- *Finer grids*: more configurations add search penalty faster than they add plausible coverage.
