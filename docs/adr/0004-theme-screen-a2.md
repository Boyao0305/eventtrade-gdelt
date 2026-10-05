# 0004 — Pre-registered GKG theme screen (hypothesis A2)

Date: 2026-10-05. Written while the GKG theme pull was running and **before any theme row was
read or analysed**.

## Context

Experiments 0003–0005 found no signal in event-country features (Track A), company mentions
(Track B), or the pre-registered sub-category screen. Hypothesis A2 and the 12 ETFs mapped only to
GKG themes (sector SPDRs and ITA) are the remaining untested part of ADR 0001. Theme data costs
~1 TB of BigQuery for the dev period: the pull started on 2026-10-05 with the rest of that month's
free tier and resumes in November (`ingest-gdelt gkg-themes` is resumable).

## Decision

**Universe.** The 19 ETFs with a theme mapping in `universe_etf.csv`: commodities + XLE
(USO, UNG, GLD, SLV, CPER, DBA, XLE — 7) and sectors + defence (XLB, XLC, XLE excluded, XLF, XLI,
XLK, XLP, XLRE, XLU, XLV, XLY, ITA — 11). The mapping is the one committed in `universe_etf.csv`
at the time of this ADR; it is not changed after seeing theme data.

**Signals (one recipe).** For each ETF, over its mapped themes:
- `attention` = documents mentioning any mapped theme / all GKG documents that day;
- `tone` = document-weighted mean tone of those documents.
Both transformed `sm5_z.60` exactly as in ADR 0002/0003.

**Cells.** 2 signals × 2 groups (commodity, sector) × horizons 5 and 20 = **8 cells**.
Pre-registered signs: attention two-sided in both groups; tone **+** for sectors, two-sided for
commodities (bad news about supply can raise prices).

**Statistics.** Exactly ADR 0003 and its implementation notes: discovery 2015-03-02 → 2019-12-31
with embargoed labels, joint stationary-bootstrap test (2,000 draws, mean block 40, seed
`config.seed`), pass if min(p_S, p_M) < 0.025, then EB shrinkage, BH q = 0.10, confirmation
2020-01-01 → 2025-03-31 with Holm and the cost-adjusted spread, combined portfolio of confirmed
cells, hold-out once. Every cell × period is a logged trial (hypothesis `A2`).

**Run condition.** Only when `ingest-gdelt gkg-themes` reports 0 months to pull for the whole dev
period. No partial-period analysis.

## Consequences

- 8 cells (+ confirmation trials) are added to the DSR count, which is already 338.
- The cross-sections are small (7 and 11 names); power is limited and stated as such in the
  experiment that reports the result.
- After A2, every hypothesis pre-registered in ADR 0001 will have been tested.

## Alternatives rejected

- *Running A2 on the part of the period already pulled*: a partial sample chosen by billing dates,
  and a second look later would double the trials.
- *Per-theme cells*: dozens of cells with one ETF each; no cross-section, no power.
- *Country × theme (`--by-country`)*: removed in experiment 0003 (rows never reached a feature)
  and ~1.2× the cost.
