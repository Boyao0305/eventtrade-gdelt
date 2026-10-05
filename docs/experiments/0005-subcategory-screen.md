# 0005 — Pre-registered sub-category screen (ADR 0003)

Date: 2026-10-05. Code: commit `ad66654` (reviewed for statistics and look-ahead before the run).

## Question

Does any pre-registered (event family × asset group × horizon) cell carry a directional signal
that the pooled tests of experiments 0003–0004 diluted?

## Setup

- Track A: 48 screened ETFs in 3 groups — MSCI developed (20), emerging incl. frontier/standalone
  as of 2015-03-02 (21), commodities + XLE (7). 7 event families (quad 1–4, protest, sanctions,
  violence = roots 18–20), horizons 5 and 20 → 42 cells.
- Track B: 900 stocks in 11 GICS sectors (today's classification). 3 news signals (attention,
  negative-news share, tone), horizons 5 and 20 → 66 cells.
- One recipe: family share within the country's events (A) or the company signal (B), `sm5_z.60`,
  within-group daily rank IC, ≥ 5 names, Newey-West t.
- Discovery 2015-03-02 → 2019-12-31 with labels embargoed to end by 2019-12-31 (1,179 usable days);
  confirmation 2020-01-01 → 2025-03-31 would only run for discovered cells.
- Joint test: stationary block bootstrap (2,000 draws, mean block 40, seed 20261004) of
  directional S = Σ max(z,0)² and M = max z; pass if min(p_S, p_M) < 0.025.

## Results

| Track | Cells | S | p_S | M | p_M | Outcome |
|---|---|---|---|---|---|---|
| A | 42 | 38.72 | 0.116 | 2.44 | 0.260 | stop at step 2 |
| B | 66 | 47.92 | 0.329 | 2.31 | 0.536 | stop at step 2 |

Strongest cells (none significant after accounting for the number of cells):

- A: sanctions → developed-market ETFs, h = 20: IC −0.041, t −2.44 (registered direction).
  Violence → commodities, h = 5: IC +0.040, t 1.82. Cooperation (quad1) → EM, h = 5: t 1.88.
- B: attention → Real Estate, h = 5: t +2.31; attention → Health Care, h = 5: t −2.24;
  negative-news share → Energy, h = 5: t −2.03.

All 108 cells were logged as trials (stage `signal`, hypothesis `S3`). Total trials in the store:
338 (v1 96, v2 134, screen 108).

## What this does not establish

- A2 (GKG themes → commodity and sector ETFs): theme data was not yet pulled.
- Finer cells (single countries, single event codes, single stocks): deliberately not searched;
  per-cell power would be far below the detectable IC.
- Other mappings (exposure-weighted countries, supply-chain and actor-based links), other horizons
  (intraday, > 20 days), or non-linear combinations.
- Statistical note: the confirmation p-value (normal approximation of the HAC t) over-rejects by
  ~1.1–1.4× at h = 20; irrelevant here because no cell reached confirmation.
