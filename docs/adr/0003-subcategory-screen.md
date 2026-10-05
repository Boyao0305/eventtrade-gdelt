# 0003 — Pre-registered sub-category screen (contingent on a v2 null)

Date: 2026-10-05. Written **before** any v2 (ADR 0002) trial was evaluated, so the choice of cells,
recipe and statistics below cannot have been influenced by v2 results.

## Context

Experiment 0003 found no v1 signal surviving. If the v2 rerun (ADR 0002) also finds no survivor,
the remaining question is whether an effect exists in a *subset* of events and assets that the
pooled tests diluted. Searching subsets is legitimate but is the classic overfitting machine
unless the search is fixed in advance, every cell is counted, estimates are shrunk jointly, and
discoveries must replicate on data the search did not see (see the methodology discussion in
`docs/agent/log.md`, 2026-10-05: Harvey–Liu–Zhu t ≥ 3, Benjamini–Hochberg FDR, empirical-Bayes
partial pooling, split-sample confirmation, Deflated Sharpe over all trials).

## Decision

**Trigger.** Run this screen if the v2 signal study plus its verification leaves no signal eligible
for gate 2. If v2 produces a verified survivor, that survivor goes to gate 2 first and this screen
is still run afterwards, but its cells are added to the same trial count.

**Cells — Track A (ETFs, direction, target `ret_oo`).**

- Event families (7), each the v2 `share` of that family in the country's events:
  `quad1` verbal cooperation (+), `quad2` material cooperation (+), `quad3` verbal conflict (−),
  `quad4` material conflict (−), `root_14` protest (−), `root_163` sanctions (−),
  `violence` = roots 18+19+20 (−).
- Asset groups (3): developed-market country ETFs, emerging-market country ETFs (MSCI
  classification at the fund's inception), commodity ETFs + XLE. For the commodity group every
  conflict sign flips to (+) (supply disruption raises prices); cooperation signs stay (+)
  for countries and become none (two-sided) for commodities.
- Horizons: 5 and 20 trading days. h = 1 is excluded: experiment 0003 showed the cost leg needs
  IC ≈ 0.05 there, above any plausible effect.
- 7 × 3 × 2 = **42 cells**.

**Cells — Track B (stocks, direction).**

- News signals (3): attention `org_docs` share (two-sided), negative attention `org_neg_docs`
  (−), tone `org_tone` mean (+).
- Stock groups (11): GICS sectors (from the index tables), S&P 500 and 400 pooled within sector.
- Horizons 5 and 20. 3 × 11 × 2 = **66 cells**.

**One recipe for every cell.** Transform `sm5_z.60` (5-day mean of the v2 z-score against a
60-day baseline), daily cross-sectional rank IC *within the group*, minimum 5 names per day,
Newey-West t as implemented in `core/evaluate.py`. No per-cell tuning of window, transform,
horizon or universe.

**Statistics.**

1. *Split.* Discovery 2015-03-02 → 2019-12-31; confirmation 2020-01-01 → 2025-03-31. The hold-out
   (≥ 2025-04-01) stays untouched.
2. *Joint test per track on discovery data.* Stationary block bootstrap of dates (all cells
   resampled together, preserving cross-cell correlation) to get the null distribution of
   Σ t² and max |t|. If the joint test is not significant at 5%, the track stops here.
3. *Shrinkage.* Empirical-Bayes normal–normal partial pooling of the cell ICs within each track
   (method-of-moments τ²); the posterior mean is the cell's estimate.
4. *Discovery.* One-sided p-values (two-sided where no sign) from the bootstrap; Benjamini–
   Hochberg at q = 0.10 across all cells of the track.
5. *Confirmation.* Each discovered cell is tested once on the confirmation period: same sign,
   one-sided p < 0.05 after Holm across the discovered cells, and the confirmation spread after
   4 × cost × turnover positive.
6. Confirmed cells go to gate 2 (model vs rule baseline under purged CV) and gate 3 as a
   **combined** portfolio weighted by shrunk ICs, not the single best cell. Then the hold-out once.

**Accounting.** Every cell × period evaluation is logged as a trial (stage `signal`, hypothesis
`S3`, params include `features_version`, cell, period). The DSR N for each track includes the
v1, v2 and screen trials.

## Consequences

- 108 cells × 2 periods add up to 216 trials; the DSR bar rises accordingly.
- Track B needs the GICS sector per stock; the universe adapter must store it.
- Sanctions (root 163) and quad classes are already pulled; no new BigQuery spend.

## Alternatives rejected

- *Choosing cells after looking at v2 results*: post-hoc and uncountable.
- *Tuning windows or transforms per cell*: multiplies hidden trials.
- *Trading the single best cell*: winner's curse; the combined, shrunk portfolio is the
  pre-registered object.
- *h = 1*: dominated by costs (experiment 0003).
