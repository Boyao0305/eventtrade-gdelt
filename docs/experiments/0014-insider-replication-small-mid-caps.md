# 0014 — Insider purchases on fresh small/mid caps (ADR 0012)

Date: 2026-10-10. Code: commit `bc707c2`, run once from the frozen checkout (`sec-small-study`).
3 trials logged (hypothesis S2). The store holds 8,185 trials.

## Question

Does experiment 0013's insider-purchase effect (S&P 500/400) replicate on stocks the study never
used? Those are the 2,815 currently listed NYSE/Nasdaq issuers outside the S&P 1500 that had
qualifying officer or director purchases in 2016-01 → 2025-03.

## Data after the review's filters

- **Prices:** 2,792 stocks.
  - The ticker check dropped 185 (more than 20% of Form 4 purchase prices inconsistent with the
    price history).
- **Admissible stock-days:** 2.34M. A day counts when the stock is exchange-listed, not a blank
  check, has a 10-Q/10-K on record, a raw price ≥ $5 and median dollar volume ≥ $1M.
- **Price screen:** 38 price spikes screened, 37,412 exits delayed to the next valid open.
- **Events with outcomes:**

| Cell | Events | Event days |
|---|---|---|
| `ins_all` | 6,764 | 1,711 |
| `ins_cxo` | 2,698 | 1,227 |
| `ins_cluster` | 1,137 | 705 |

- **Survivorship:** issuers that later delisted have about as many SEC-identified events as the
  listed set. Their exits are mostly mergers. **Failures make up 9.6–10.7% of all qualifying
  events**:

| Cell | Merger exits | Failure exits | Other exits | f_fail |
|---|---|---|---|---|
| `ins_all` | 5,973 | 2,950 | 3,105 | 0.096 |
| `ins_cxo` | 2,370 | 1,363 | 1,287 | 0.107 |
| `ins_cluster` | 1,261 | 598 | 593 | 0.098 |

## Results (h = 60 days, open-to-open vs IWM)

| Cell | Mean | NW t | Matched p (3-dim) | 2-dim p | Unmatched p | Round trip | Net | Net, 2 × 75 bp stress | Ex-Mar-2020 | Survivorship-stressed | Holm | Pass |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| any insider | +0.44% | 1.24 | 0.0025 | 0.0005 | 0.0005 | 0.74% | −0.30% | −1.08% | +0.56% | −2.50% | ✓ | ✗ (net) |
| CEO/CFO | +0.88% | 1.31 | 0.0025 | 0.0010 | 0.0005 | 0.74% | +0.14% | −0.64% | +0.85% | −2.43% | ✓ | ✓ |
| cluster | +0.39% | 0.52 | 0.167 | 0.0105 | 0.0005 | 0.77% | −0.39% | −1.13% | +0.08% | −2.59% | ✗ | ✗ |

**Verdict under the pre-registered rule: "partial".** Only `ins_cxo` passes. `ins_all` does not
(net of costs is negative), and every cell fails the survivorship stress. Gate 2 is not entered.

## Interpretation

- **Relative to comparable dates of the same stock, insider purchases do look informative.**
  The matched p is 0.0025 for all and CEO/CFO purchases: those stocks did better after insider
  buys than on dates with the same earnings timing and prior drawdowns. The **absolute**
  abnormal return is small, though (+0.4–0.9% over 60 days, t ≈ 1.3). The matched p is low
  because comparable distressed dates tend to do worse, not because event returns are large.
- **The effect is much smaller than in the S&P 1500** (+1.8 to +3.8% in experiment 0013). That
  runs **against** the literature's "stronger in small caps", and it fits experiment 0013's
  sizes being partly luck (winner's curse).
- **It does not survive realistic costs for small caps, or the survivorship stress.** Counting
  insider buys at companies that later failed (about 10% of events, at a −30% delisting return)
  turns every cell clearly negative.
- **Taken with experiment 0013:** insider purchases carry some information about relative
  performance. But on this evidence a long-only insider-following strategy with sector or IWM
  hedges is **not robustly profitable after costs and survivorship**, and the insider line
  closes as a standalone strategy.

## Limits

- The −30% failure return is a literature assumption. The delisted issuers' actual outcomes are
  unknown (no prices).
- Costs are tiered approximations.
- The universe uses 2026 listings.
- The hold-out is untouched.
