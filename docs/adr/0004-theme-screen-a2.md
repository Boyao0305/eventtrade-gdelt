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

## Implementation notes (2026-10-05, before any theme value was read)

Written while the theme pull was running. These notes were settled from the SQL contract
(`adapters/queries/gkg_themes_daily.sql`), the code and synthetic fixtures. No stored theme value
(`value_sum` / `n` of a gkg theme row) was read, and no A2 cell has been evaluated. They resolve
ambiguities in the text above.

- **Attention counts theme mentions, not distinct documents.** The query stores, per theme T
  and session day, `theme_<T>_docs` (value_sum = documents mentioning T, n = all GKG documents
  that day) and `theme_<T>_tone` (value_sum = summed document tone, n = documents mentioning T).
  The number of documents mentioning *any* of an ETF's themes is not recoverable from per-theme
  counts, because a document can carry several. The implemented `attention` is therefore
  Σ_T docs(T) / all docs: a document with k mapped themes counts k times. `tone` is
  Σ_T tone_sum(T) / Σ_T docs(T), a mention-weighted mean in which such a document weighs k.
  Counting exact documents would need a new per-ETF query, about 1 TB more. That was rejected.
- **The day total is counted once.** The denominator is the day's n (every docs row of a day
  carries the same total; the code raises if they disagree), never summed over themes. A
  session day with a known total and no mapped-theme row is a true zero for attention. Tone has
  no value that day. Weekend and holiday sessions roll into the next trading day on raw sums
  (mentions and totals), as in v2.
- **Groups are pinned by ticker** in `core/screen_features.THEME_GROUP_MEMBERS`: commodity USO,
  UNG, GLD, SLV, CPER, DBA, XLE; sector XLB, XLC, XLF, XLI, XLK, XLP, XLRE, XLU, XLV, XLY, ITA.
  That is 18 distinct ETFs. The "19" in the Decision counts XLE twice: XLE is listed under
  "sectors" and excluded there, and it is screened with the commodities only. The rates ETFs
  (TLT, IEF, SHY) have theme mappings but are not in A2. A test pins the theme mapping of the 18
  ETFs to the `universe_etf.csv` of this date.
- **Trial identity.** A2 trials use hypothesis `A2`. Their params are the ADR 0003 params plus
  `hypothesis: "A2"` and `themes_hash` (a hash of the members' sorted theme lists), so a changed
  mapping is a new, counted trial. S3 params are unchanged: existing S3 trial ids are pinned by a
  test.
- **Run condition, as implemented.** `screen --set a2` exits 4 before reading any GDELT value
  unless every calendar month of the dev period (2015-03-02 → 2025-03-31) has rows under the
  `gkg_themes_daily` query name on its first and last session date. That is the same test
  `ingest-gdelt gkg-themes` uses to report months to pull.
- **Everything else is the S3 code path** in `cli/pipelines.screen`: split, embargo, joint test
  (n_boot ≥ 999, mean block 40, seed), the thin-cell rule, EB, BH q = 0.10, Holm, the cost
  spread at the ETF cost.
