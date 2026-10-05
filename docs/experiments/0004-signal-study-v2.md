# 0004 — Gate-1 signal study with v2 features (ADR 0002)

Date: 2026-10-05. Code: commit `be01931`, run from a frozen snapshot checkout so concurrent edits on
`dev` could not affect it.

## Question

After fixing the v1 defects (experiment 0003) and tightening the gates (ADR 0002), does any
pre-registered GDELT signal pass gate 1 on the dev period?

## Setup

- Dev period 2015-03-02 → 2025-03-31 (2,537 trading days); hold-out untouched.
- Track A: 63 ETFs (TLT/IEF/SHY no longer country-mapped), 23 features (v2 z, lr with pseudo-count,
  sm5 low-turnover variants), 4.05M GDELT event aggregate rows.
- Track B: 900 S&P 500/400 stocks (one share class each, noisy aliases dropped), 11 features,
  7.36M GKG org aggregate rows.
- Horizons 1/5/20 (sm5 features only 5/20). Costs 5 bp (ETF) / 10 bp (stock) per leg, charged
  4 × cost × turnover on the long-short spread.

## Method

ADR 0001 gate 1 with ADR 0002 changes: |Newey-West t| ≥ 3 in the pre-registered direction, same
sign in ≥ 2 of 3 sub-periods, cost-adjusted spread > 0. A4 (volatility): IC of residuals after
per-day regression on ln rv5/rv22/rv60/long-run vol against ln(forward vol / trailing 22-day vol),
plus a static placebo (per-ticker trailing mean) that must not pass.

## Results

| Track | v2 trials | Passed gate 1 |
|---|---|---|
| A, direction (H0, A1, A3) | 60 | 0 |
| A, volatility (A4) | 45 | 0 |
| B, direction (B1, B2) | 29 | 0 |

Closest misses:

- A1 coercion share sm5, h = 20: IC +0.019, t 2.00 — but the pre-registered sign is negative, and
  the sign held in 0 of 3 sub-periods.
- A1 mass-violence share lr, h = 1: IC +0.008, t 2.74 — wrong sign.
- A4 coercion share z, h = 1/5/20: feature |t| ≤ 1.3, while the **static placebo passed**
  (t 3.9–4.9). The placebo caught the same static-country-risk effect that produced v1's six false
  passes, which is what it was added for.
- B tone raw mean, h = 1: t 3.16, but the top-minus-bottom spread is negative (−0.7 bp) before costs.
- B tone sm5, h = 20: t −2.31, wrong sign.

Cumulative signal trials in the `trials` table after this run: 230 (v1 96, v2 134).

## What this does not establish

- Effects confined to subsets of events or assets (diluted in pooled cross-sections) — that is
  ADR 0003's screen.
- A2 (GKG themes) and the 12 theme-only ETFs: themes not pulled yet.
- Other mappings (exposure-weighted countries, supply-chain links, actor-based events).
- Other horizons, intraday timing, or non-linear combinations — gate 2 was never reached.
- Results on the hold-out period.
