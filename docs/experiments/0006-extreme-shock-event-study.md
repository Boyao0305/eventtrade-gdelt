# 0006 — Extreme-shock event study with nested optimization (ADR 0005)

Date: 2026-10-06. Code: commit `ba718c2`, run from a frozen checkout, order A → B → A.

## Question

Do rare, large GDELT shocks (rather than daily rankings) predict abnormal returns, absolute
commodity returns or abnormal volatility, after searching 32 configurations per cell and
correcting for that search?

## Setup

- Discovery 2015-03-02 → 2019-12-31 (labels embargoed); confirmation 2020-01-01 → 2025-03-31 only
  for discovered cells (none ran); hold-out untouched.
- Track A: 48 ETFs (DM 20, EM 21, commodity 7), 44 cells; Track B: 900 stocks, 3 cells.
- Grid θ {2, 2.5, 3, 4} × W {60, 120} × h {1, 5, 10, 20}; 20-day declustering; sufficiency ≥ 30
  events and ≥ 30 event dates; calendar-time portfolios with Newey-West t.
- Search-corrected cell p: placebo-date null of the maximum directional t over the cell's
  sufficient configurations (n_boot 2,000, seed 20261004). Simulated size 2.4–4.7% at 5%.
- BH q = 0.10 across all 47 cells.

## Results

**0 of 47 cells discovered.** No confirmation test ran.

Smallest search-corrected p-values (BH would need p ≤ 0.0021 for the first rejection):

| Cell | Selected θ/W/h | Events | Mean outcome | t | p |
|---|---|---|---|---|---|
| E3 tone → abnormal vol, EM | 3/60/20 | 225 | +0.067 | 3.77 | 0.005 |
| E3 verbal conflict → abnormal vol, EM | 3/120/20 | 144 | +0.098 | 3.76 | 0.0085 |
| E3 material conflict → abnormal vol, EM | 2.5/60/20 | 360 | +0.039 | 3.37 | 0.015 |
| E1c cooperation → abnormal return, EM | 3/60/1 | 87 | +0.29% | 2.85 | 0.064 |
| E3 cooperation → abnormal vol, DM | 2.5/60/10 | 158 | +0.055 | 2.09 | 0.083 |
| E4p positive attention spike → stock abnormal return | 2/120/5 | 5,292 | +0.12% | 2.59 | 0.118 |

- Direction cells (E1, E1c, E2, E4, E4p): all p ≥ 0.06; conflict → abnormal country-ETF return
  has the registered (negative) sign in all 12 E1 cells, but none is significant after the
  32-configuration search (best p 0.11).
- Volatility cells: a coherent pattern in **emerging markets** — conflict-type and low-tone shocks
  are followed by higher 20-day abnormal volatility (tone, quad3, quad4 with p 0.005–0.015;
  protest, violence p ≈ 0.15), not seen in DM or commodities. Not significant after BH over 47
  cells.
- Trials logged: Track A 1,408, Track B 96. Total in the store: 1,842.

## What this does not establish

- That the EM volatility pattern is real: it is a family of correlated cells selected after the
  fact. It is only a hypothesis for a separate, pre-registered test (ADR 0006).
- Tradability of any volatility effect: no options or volatility-instrument data is used.
- Effects at other thresholds, horizons beyond 20 days, or with other event mappings.
- A2 (GKG themes): not yet runnable.
