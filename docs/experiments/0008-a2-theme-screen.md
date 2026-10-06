# 0008 — A2: GKG theme screen (ADR 0004)

Date: 2026-10-06. Code: commit `074ff8e`, run once from the frozen checkout
(`screen --track A --set a2`) after the theme pull reported 0 months to pull for 2015-03-02 →
2025-03-31.

## Question

Do GKG theme signals (attention to an ETF's mapped themes, and their tone) predict 5- or 20-day
returns of commodity and sector ETFs, cross-sectionally within each group?

## Setup

- Data: `gkg_themes_daily`, 121 of 121 dev months. The last 91 months (2017-09 → 2025-03) were
  pulled on 2026-10-06: 658.6 GB, with billing enabled. October billed 1,553 GB in total, about
  454 GB over the free 1 TiB, ≈ $2.6 (cap $20).
- 18 ETFs: commodity (USO UNG GLD SLV CPER DBA XLE) and sector (XLB XLC XLF XLI XLK XLP XLRE XLU
  XLV XLY ITA). Signals `attention` and `tone`, `sm5_z.60`. 8 cells = 2 signals × 2 groups ×
  h 5/20.
- ADR 0003 statistics: discovery 2015-03-02 → 2019-12-31 (labels embargoed), joint
  stationary-bootstrap test (2,000 draws, mean block 40, seed 20261004). Significant only if
  min(p_S, p_M) < 0.025.

## Results

Joint test over 8 of 8 cells, 1,179 dates: S = 10.95, p_S = 0.200; M = 2.43, p_M = 0.117 →
**not significant**. The track stops at ADR 0003 step 2: no shrinkage, no discovery, no
confirmation.

| Cell | n | Mean IC | NW t | p (registered direction) |
|---|---|---|---|---|
| attention · commodity · h5 | 1179 | −0.019 | −0.76 | 0.513 |
| attention · commodity · h20 | 1164 | −0.070 | −1.62 | 0.142 |
| attention · sector · h5 | 1179 | −0.022 | −1.22 | 0.168 |
| attention · sector · h20 | 1164 | +0.010 | 0.37 | 0.666 |
| tone · commodity · h5 | 1179 | +0.012 | 0.49 | 0.627 |
| tone · commodity · h20 | 1164 | +0.081 | 2.43 | 0.014 |
| tone · sector · h5 | 1179 | −0.020 | −1.01 | 0.829 |
| tone · sector · h20 | 1164 | −0.022 | −0.86 | 0.828 |

8 trials logged (hypothesis `A2`, discovery period). The store now holds 1,851 trials.

## What this does and does not establish

- With A2 done, every hypothesis pre-registered in ADR 0001 has been tested, plus the
  sub-category screen (ADR 0003), the extreme-shock event study (ADR 0005) and its follow-up
  (ADR 0006). None passed.
- Commodity tone at h20 is the only cell under 0.05. With 8 cells the chance of at least one
  p ≤ 0.014 under the null is about 11%, and the max-statistic test (p_M = 0.117) already
  accounts for that. **It is not followed up**: no untouched dev data is left to confirm it on
  (the 2020–2025 period would be a reuse with a data-suggested hypothesis), and the hold-out is
  reserved for strategies that pass gates 1–3.
- Power is limited: cross-sections of 7 and 11 names. A small theme effect cannot be ruled out.
- Attention counts theme mentions, not distinct documents (ADR 0004 implementation notes).
