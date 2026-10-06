# 0006 — One pre-registered follow-up: EM conflict shocks → abnormal volatility

Date: 2026-10-06. Written after experiment 0006 and before any computation on the period this
test uses.

## Context

Experiment 0006 found no discovery after correcting for 47 cells, but a coherent pattern among
the emerging-market volatility cells (E3, EM): tone, verbal-conflict and material-conflict shocks
had search-corrected p 0.005–0.015, protest and violence ≈ 0.15, all with the registered positive
sign. This hypothesis is **suggested by the data**. It can only be tested on data the discovery
did not use, as a single test, with every choice fixed in advance.

Disclosure: the 2020-01-01 → 2025-03-31 period was used by earlier *continuous* volatility tests
(A4 v1/v2, experiments 0003–0004, null), so it is not pristine for "conflict ↔ volatility" in
general. It has never been used for this *event* formulation. The hold-out (≥ 2025-04-01) remains
untouched and is not used here.

## Decision

**Single hypothesis F1.** In emerging-market country ETFs, a conflict-type news shock is followed
by higher abnormal volatility over the next 20 trading days.

- **Assets:** the 21 ETFs with `screen_group = 'em'`.
- **Composite event:** on day t an ETF fires if any of its conflict signals shocks: family share
  z ≥ θ for quad3, quad4, protest (root 14) or violence (roots 18–20), or tone z ≤ −θ. One
  composite declustering per ETF: no new event within 20 trading days of the last.
- **Configuration, fixed by rule:** the configuration selected most often among the seven EM E3
  cells of experiment 0006 — θ = 3, W = 60, h = 20 (4 of 7 cells; the others chose 2.5/60/20).
- **Outcome:** ADR 0005 abnormal volatility at h = 20, as implemented in `ba718c2`.
- **Statistic:** calendar-time portfolio mean and Newey-West t (ADR 0005).
- **Test:** placebo-date p (distinct, unspaced dates within the window, n_boot 2,000, seed
  `config.seed`), one-sided in the + direction, on 2020-01-01 → 2025-03-31 with labels embargoed to
  end before the hold-out. Requires ≥ 30 events and ≥ 30 event dates, otherwise "inconclusive".
- **Pass:** p < 0.05 and mean > 0. One trial, hypothesis `F1`, logged.

**Interpretation either way.** A pass means GDELT conflict shocks carry information about EM ETF
risk — useful for risk sizing — but not a tradable return strategy without volatility
instruments, which this project does not have. A fail closes the volatility line.

## Consequences

- One more trial (N = 1,843).
- No new data or BigQuery spend.

## Alternatives rejected

- *Re-running the 47-cell search on 2020–2025*: a second search, not a confirmation.
- *Testing each of the five EM cells separately*: five tests where the hypothesis is one.
- *Choosing the configuration with the best discovery t*: the modal-selection rule is fixed and
  does not reward the single best cell.
- *Using the hold-out*: reserved for strategies that pass gates 1–3.

## Amendment (2026-10-06, before the F1 run): correction of a description

The Decision describes the modal-selection count incorrectly. ADR 0005 has **eight** EM E3 cells
(seven families plus low tone), not seven. Their discovery (2015-03-02 → 2019-12-31) selections,
read from the logged trials: quad1, quad2, root_14 (protest), tone → 3/60/20; quad4, root_163
(sanctions), violence → 2.5/60/20; quad3 → 3/120/20. So 3/60/20 is chosen by 4 of 8 cells,
2.5/60/20 by 3 and 3/120/20 by 1. The modal configuration is still θ = 3, W = 60, h = 20.
**No rule changes**: the assets, composite event, configuration, outcome, statistic, test, pass
rule and the single F1 trial are as stated above.

Implementation note: "one trial" is enforced on the hypothesis. Once any trial with hypothesis
`F1` is logged, `followup --id F1` reads and computes nothing and prints the stored verdict,
whatever the n_boot, seed, features version or EM members. n_boot is fixed at 2,000 and is not a
CLI option.
