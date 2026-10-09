# 0010 — Intraday parameter search (ADR 0008)

Date: 2026-10-09. Code: commit `99a83b9`, run once from the frozen checkout (`intraday-search`).
Two earlier attempts were stopped before logging anything (no J trials existed): one was
slow, and one stalled for a reason that was never identified. Their orphaned processes were
killed by PID. The logged run took 1h46m.

## Question

Does **any** combination of latency (0/15 min), window (15–120 min), threshold (θ 2–4), exit
(15 min to the close) and news category (14 country categories × DM/EM, 3 producer categories,
3 company-spike variants) make a GDELT signal pay **after trading costs**?

## Data

- **New pull:** `events_15min_cat`, 31M rows covering all 96 batches a day with action and actor
  geography. It cost 139 GB of BigQuery; October billed ≈ 2,022 GB, ≈ $5.25 cumulative of the $20
  cap.
- **Prices:** ADR 0007 prices and liquidity filter (833 liquid assets).
- **Entry and selection:** entry at the open of bar [τ, τ+15). The selection statistic is the
  NW t of the daily series of s·r − round-trip cost.

## Results (discovery 2016-01-04 → 2019-12-31; placebo max-t over each cell's configurations, 2,000 draws)

| Cell | Selected config (L, W, θ, exit) | Best net t | Search-corrected p |
|---|---|---|---|
| A actor_conflict dm | L15 W120 θ4 close | −7.21 | 0.473 |
| A actor_conflict em | L15 W120 θ4 close | −4.09 | 0.513 |
| A attention dm | L15 W120 θ4 close (−) | −7.55 | 0.060 |
| A attention em | L15 W120 θ4 close (−) | −4.85 | 0.510 |
| A b163 (sanctions) dm | L0 W15 θ3 120m | −2.97 | 0.079 |
| A b163 em | L0 W15 θ4 120m | −2.09 | 0.464 |
| A producer quad3 | L15 W30 θ4 close | −0.71 | 0.816 |
| A producer quad4 | L15 W120 θ4 close | −1.50 | 0.929 |
| A producer violence | L15 W120 θ4 close | −0.59 | 0.431 |
| A quad1 dm / em | L0 W60 θ4 close / L15 W15 θ4 close | −4.30 / −1.91 | 0.701 / 0.461 |
| A quad2 dm / em | L15 W120 θ4 close | −8.05 / −5.97 | 0.377 / 0.647 |
| A quad3 dm / em | L0 W120 θ4 close | −5.49 / −5.46 | 0.101 / 0.757 |
| A quad4 dm / em | L0 W120 θ4 close / L0 W60 θ4 close | −9.48 / −7.24 | 0.755 / 0.927 |
| A r14 protest dm / em | L15 W30 / L0 W120, θ4 close | −11.26 / −5.93 | 0.665 / 0.613 |
| A r16 reduce relations dm / em | L15 W120 / L0 W30, θ4 close | −10.32 / −5.31 | 0.623 / 0.461 |
| A r17 coerce dm / em | L15 W120 θ4 close | −11.28 / −5.54 | 0.815 / 0.240 |
| A r18 assault dm / em | L15 W120 / L0 W120, θ4 close | −10.94 / −7.33 | 0.366 / 0.803 |
| A r19 fight dm / em | L0 W120 / L15 W120, θ4 close | −11.00 / −7.96 | 0.765 / 0.908 |
| A r20 mass violence dm / em | L15 W30 θ2.5 close / L0 W120 θ4 120m | −3.11 / −0.53 | 0.384 / 0.257 |
| A tone dm / em | L0 W120 / L0 W15, θ4 close | −7.30 / −4.98 | 0.842 / 0.717 |
| B spike, any tone | L0 W120 θ4 close (+) | −16.91 | 0.032 |
| B spike, negative | L0 W120 θ4 close | −16.15 | 0.094 |
| B spike, positive | L15 W120 θ4 close | −11.32 | 0.079 |

**0 of 34 cells discovered** (BH q = 0.10; the first threshold is 0.10/34 ≈ 0.003). No
confirmation is run. 5,920 trials were logged (hypothesis J). The store now holds 7,789 trials,
of which 7,789 count for DSR (none are look-ahead).

## Interpretation

- **No configuration in any cell has a positive mean net of costs.** Even the best of 160 (or
  320) configurations has a negative net t in every cell.
- **The search consistently picks the sparsest, longest-held configuration** (θ = 4, W = 120,
  exit at the close). That is the corner where a fixed cost per trade hurts least and fewer events
  make the t-statistic least negative. It is the signature of costs dominating, not of a hidden
  effect.
- **The lowest p-values say little.** They are 0.032 (any company spike), 0.060 (DM attention)
  and 0.079 (sanctions DM, positive spikes). They mean the observed best is less negative than
  the placebo best, still far below zero net. They do not survive the 34-cell correction.
- **This closes the intraday line for GDELT event signals at the tested granularity.** That
  covers ADR 0007's fixed configuration and ADR 0008's 5,440-configuration search.

## Limits

- The cost model is approximate: ADR 0007's per-leg costs and Abdi–Ranaldo spreads. A
  market-maker with lower costs is outside this study.
- Survivorship bias (current constituents) and current GICS sectors, as before.
- The hold-out is untouched.
