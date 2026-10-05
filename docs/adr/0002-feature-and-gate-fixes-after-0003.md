# 0002 — Feature and gate fixes after experiment 0003

Date: 2026-10-05. Amends ADR 0001; does not replace it. Written after seeing the dev-period
gate-1 results, so everything here is post-hoc and is disclosed as such.

## Context

Experiment 0003 found no signal surviving gate 1 plus verification, and found defects that make
some of the 96 logged signal trials uninformative: the `lr` surprise transform exists only on
spike days (a static sparsity proxy), the `z` transform explodes on all-zero windows, GKG org
attention is divided by the number of aliases hit, zero-mention days are NaN instead of 0, the A4
volatility gate compares against the raw volatility *level* (any static per-ticker offset passes),
the gates take the sign from the data instead of the pre-registration, SPY/TLT/IEF/SHY share one
identical `US` feature, and some aliases are noise ("eaton" = the January 2025 Eaton Fire).
The positive controls showed the pipeline itself is aligned and powered for the IC leg, but that
at ~0.6 daily turnover the cost leg needs IC 0.03–0.05 at h = 1 and 5.

## Decision

1. **Feature definitions, version 2.**
   - `lr` = ln((x + 1) / (m + 1)) on counts (pseudo-count), defined on zero days.
   - `z`: a baseline std below 1e-9 gives NaN; z is clipped to ±10.
   - Count metrics are zero-filled on days with no rows (no mention = 0 mentions), for events and
     GKG orgs alike; tone means stay NaN on zero days.
   - GKG org attention share = docs over all aliases of the ticker / the day's total docs (once).
   - New low-turnover transform `sm5`: 5-trading-day mean of the v2 z feature, evaluated at
     h = 5 and h = 20.
2. **Universe.** TLT, IEF and SHY lose their country mapping (rates are not a country-news bet);
   SPY keeps `US`. One share class per company (keep GOOGL, FOXA, NWSA). Aliases: drop
   single-token aliases flagged ambiguous by experiment 0003 (`eaton`) and the `<ambiguous> inc`
   forms (`nasdaq inc`, `gartner inc`, `morningstar inc`).
3. **Gate 1, direction.** The observed IC sign must equal the pre-registered sign (H0 has none:
   either sign). Cost is charged as 4 × one-way cost × turnover (a $1 long + $1 short book trades
   four legs); ADR 0001's "one-way" wording meant the per-leg rate.
4. **Gate 1, volatility (A4).** Label = ln(forward realised vol over h / trailing 22-day realised
   vol). The IC is taken on residuals of both feature and label after a per-day cross-sectional
   regression on ln(rv5), ln(rv22), ln(rv60) and ln(expanding long-run vol), all known at t.
   Pre-registered sign: positive. A placebo (each ticker's trailing mean of the feature) is run
   alongside; if the placebo also passes, the feature fails.
5. **Trial accounting.** Trial params carry `features_version: 2`, so v2 trials are new rows. The
   96 v1 trials stay in the append-only table; DSR's N for each track counts v1 and v2 trials.

## Consequences

- Every v2 trial is a second look at the same dev data. The DSR penalty grows with N, which is
  the intended cost of fixing things after seeing results.
- No new hypothesis is added. A2 (GKG themes) remains untested until the themes are pulled in a
  later billing month.
- The hold-out is still untouched.

## Alternatives rejected

- *Re-running v1 definitions with only the bug fixes and no gate changes*: the A4 gate would still
  pass static per-ticker offsets (placebo t ≈ 36).
- *Dropping the cost leg so more features reach gate 2*: the cost leg is the economics of the
  strategy; a signal that cannot pay for its turnover is not a strategy.
- *Deleting the v1 trials*: they were evaluated, so they count.

## Implementation notes (2026-10-05, before any v2 trial)

The decision text left these choices open. They were made during implementation, before any v2
trial was logged. If the ADR author reads the text differently, each one is a one-line change.

- **lr pseudo-count on non-count stats.** k is one count in the stat's units: 1 for count/sum,
  1/n for a mean, 1/(the `_GLOBAL` denominator on day t) for share/sum_share, applied to both
  x_t and the baseline mean (`features.surprise(pseudo=...)`). With a constant denominator a
  share's lr equals the count lr. lr is only requested on non-negative stats.
- **Zero-fill starts at first appearance.** Before an entity's first row up to t it has no rows,
  so a ticker's presence at t never depends on later mentions.
- **sm5 families.** Every family with a z transform also gets sm5 (including A3 and the A2
  theme families), which adds trials and so DSR penalty.
- **A4 sign is "positive" in shock units.** Expected A4 IC sign = +1 x (minus the feature's A1
  return sign): +1 for conflict counts/shares, -1 for `events.goldstein.mean_z.60` (a shock is a
  low Goldstein). See `pipelines._a4_sign`.
- **H0 gets no sign** (gate 1 accepts either). The other `_FAMILIES` signs are unchanged from
  ADR 0001. A backtest of an H0 feature needs an explicit `--sign`.
- **Stale universe rows.** `load-universe` now deletes a reloaded track's tickers that are no
  longer listed (GOOG, FOX, NWS). So `load-universe` must be rerun before the v2 `build-dataset`.
- **Alias drops go beyond the literal `<core> inc` list.** `aliases._CITED_ANY_FORM` treats
  `nasdaq`, `gartner` and `morningstar` as ambiguous bare *and* with any trailing legal suffix,
  because the source/listing boilerplate is what carries the suffix. The other suffixed forms
  are small (dev docs: nasdaq corp 1, nasdaq corporation 10, gartner corp 2, gartner corporation
  10, morningstar corp 50). Bare `morningstar` is also dropped; bare `nasdaq` and `gartner` were
  already ambiguous in v1. **NDAQ, IT and MORN therefore keep no alias and have no Track B GKG
  features in v2.** Separately, the existing generic-on-ambiguous rule ("co" on an ambiguous core)
  drops `eaton co` (2,993 dev docs) together with `eaton`. ETN keeps `eaton corp(oration)`, `eaton
  inc`, `eaton company/companies`, `eaton plc` and `eaton ltd`. To follow the literal text instead,
  move only `nasdaq inc`, `gartner inc` and `morningstar inc` into `_AMBIGUOUS` and drop
  `_CITED_ANY_FORM`.
- **Backtest trials carry `features_version` too** (rule and model), so a backtest over v2
  features never shares a trial id with the same backtest over v1 features. Hold-out trials copy
  their source backtest's params, so they inherit it.
- **Features version guard.** The store does not record which features version built the stored
  rows. `signal-study` refuses, and logs nothing, when no sm5 feature is stored.
