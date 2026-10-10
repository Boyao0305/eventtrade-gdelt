# 0013 — SEC insider purchases and initial 13Ds (ADR 0011)

Date: 2026-10-10. Code: commit `5c9ab58`, run once from the frozen checkout (`sec-study`).

An earlier attempt (commit `ee81146`) crashed while building strata, before any statistic and
before logging: a ticker with no 10-Q/10-K on record. The fix gives such tickers the stratum
"none", and a regression test covers it. 19 trials were logged (hypothesis S1). The store holds
8,182 trials, of which 7,808 count for DSR.

## Question

After an officer or director buys their company's stock on the open market, or after an initial
13D is filed, does the stock beat its sector ETF over the next 5, 20 or 60 trading days?
- Entry at the next open after the filing date.
- The null compares each event with dates of the same stock at the same distance from its
  earnings filing and after a similar 20-day move.

## Results

**Events with outcomes:**

| Period | Any insider buy | CEO/CFO buy | Insider cluster | Initial 13D |
|---|---|---|---|---|
| Discovery (2016–2019) | ≈ 1,900 | ≈ 470 | ≈ 185 | ≈ 63 |
| Confirmation (2020–2025Q1) | ≈ 2,400 | ≈ 625 | ≈ 270 | — |

**Discovery, 2016-01-04 → 2019-12-31** (sector-hedged open-to-open mean; NW t; matched-placebo
p; unmatched p in brackets):

| Event | h5 | h20 | h60 |
|---|---|---|---|
| Any insider purchase (`ins_all`) | +0.25%, t 2.19, p 0.058 (0.043) | +0.59%, t 2.41, p 0.054 (0.081) | **+1.82%, t 2.57, p 0.027** (0.039) |
| CEO/CFO purchase (`ins_cxo`) | +0.32%, t 1.47, p 0.072 (0.014) | **+0.92%, t 2.40, p 0.012** (0.001) | **+3.23%, t 3.36, p 0.0075** (0.0005) |
| ≥ 3 insiders in 30 days (`ins_cluster`) | **+0.61%, t 1.81, p 0.044** (0.032) | +1.12%, t 1.45, p 0.237 (0.029) | **+2.61%, t 2.01, p 0.039** (0.0005) |
| Initial non-deal 13D (`act_13d`) | −0.43%, p 0.80 | −0.71%, p 0.77 | −1.55%, p 0.78 |

**BH q = 0.10:** 7 of 12 discovered. These are the four bold cells, plus ins_all h5 and h20 and
ins_cxo h5.

**Confirmation, 2020-01-02 → 2025-03-31**, discovered cells only:

| Cell | Mean | NW t | Matched p | Net of 30 bp | Mean excl. 2020-02-20 → 04-30 | Holm |
|---|---|---|---|---|---|---|
| ins_all h20 | +0.72% | 2.41 | 0.0090 | +0.42% | +0.77% | ✗ (needs ≤ 0.0071) |
| ins_all h60 | +2.04% | 2.95 | 0.0115 | +1.74% | +1.98% | ✗ |
| ins_cluster h60 | +3.78% | 2.39 | 0.0465 | +3.48% | +3.72% | ✗ |
| ins_cxo h60 | +2.68% | 2.19 | 0.0475 | +2.37% | +2.30% | ✗ |
| ins_cluster h5 | +0.81% | 1.55 | 0.083 | +0.50% | +0.79% | ✗ |
| ins_cxo h20 | +0.41% | 1.08 | 0.283 | +0.11% | +0.48% | ✗ |
| ins_all h5 | +0.15% | 1.25 | 0.317 | −0.15% | +0.14% | ✗ |

**Verdict under the pre-registered rule: not confirmed.** The smallest confirmation p (0.0090)
misses Holm's first threshold (0.05/7 ≈ 0.0071).

## Interpretation

- **This is the strongest and most consistent result of the project, though not a formal pass.**
  - Every insider cell has the registered sign in both periods.
  - The 20- and 60-day sizes replicate: all-insider h60 +1.8% → +2.0%; CEO/CFO h60 +3.2% →
    +2.7%; cluster h60 +2.6% → +3.8%.
  - The effects survive the 30 bp round trip and the exclusion of the March 2020 wave.
- **This fits the literature:** insider purchases carry information that the market absorbs
  over weeks. CEO/CFO and clustered purchases carry more.
- **The matched placebo is more conservative than the unmatched one** (e.g. CEO/CFO h60:
  0.0075 vs 0.0005). Some of the naive effect is post-earnings timing and reversal, as the
  review predicted, but not all of it.
- **Initial 13Ds:** no effect. About half are not activist campaigns (audit in ADR 0011), and
  the universe is large caps.
- **Process:** under the project's rules the result cannot be "rescued" by re-testing the
  same data with different settings. Any next step must use data this study did not use.

## Limits

- Current S&P 500/400 constituents (survivorship) and current GICS sectors.
- Holding-company CIK changes lose some pre-reorganization filings.
- Beta-1 sector hedge.
- Daily resolution with entry at the next open: the reaction on the filing day itself is not
  captured, by design.
- The hold-out is untouched.
