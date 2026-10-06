# 0007 — F1: EM conflict shocks → abnormal volatility, single confirmation test (ADR 0006)

Date: 2026-10-06. Code: commit `344d4bf`, run once from a frozen checkout after a full verified
suite (1,275 passed; 6 import contracts kept).

## Question

Does the emerging-market volatility pattern suggested by experiment 0006's discovery period
(conflict and low-tone shocks → higher 20-day abnormal volatility) hold on data the discovery did
not use?

## Setup

- 21 EM country ETFs; composite event = any of quad3, quad4, protest, violence share z ≥ 3 or tone
  z ≤ −3 (W = 60), one 20-day declustering per ETF; outcome ADR 0005 abnormal volatility, h = 20.
- Window: events dated 2020-01-01 → 2025-02-28 (labels end before the 2025-04-01 hold-out).
- Calendar-time portfolio, Newey-West t; one-sided placebo-date p, 2,000 draws, seed 20261004.
- Pass rule fixed in advance: p < 0.05 and mean > 0.

## Results

| Events | Event dates | Mean abnormal vol | NW t | Placebo p (one-sided) | Verdict |
|---|---|---|---|---|---|
| 642 | 494 | −0.0051 | −0.57 | 0.678 | **fail** |

The discovery-period effect (+0.04 to +0.10, t 3.4–3.8 in the strongest cells) does not appear;
the confirmation estimate is slightly negative. One trial logged (hypothesis `F1`); the store
now holds 1,843 trials.

## What this does not establish

- Volatility effects at other horizons, thresholds, or in DM/commodity assets (not tested here by
  design).
- Anything about the hold-out period, which remains untouched.
- That the 2020–2025 sample is representative: it contains COVID and the 2022 rate shock, which
  dominate volatility; an effect could be masked there. This is a limitation, not a reason to
  re-test.
