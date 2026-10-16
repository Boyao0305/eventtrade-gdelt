# 0020 — Forecasting the direction of the earnings reaction from free data (ADR 0018)

Date: 2026-10-16. Code: commit `88465d5`, run once from the frozen checkout (`earn-forecast`),
after a blind dry run. 6 trials logged (hypothesis W; W-L primary). The store holds 8,264
trials. The gate-2 DSR would add n_extra = 156.

## Question

Can information public at close(d−2) call the sign of the sector-hedged earnings reaction
R = close(d−2) → close(d+1) well enough to beat a 30 bp round trip?

Experiment 0018 put the break-even at about 53% |R|-weighted accuracy.

## Setup

- **Events:** 30,423 earnings days (Track B, 8-K 2.02; 2 price-error events dropped).
  30,196 pass the deal filter.
- **Out-of-sample predictions:** 24,795, from 2018-01 → 2025-03 (yearly walk-forward,
  expanding window from 2016).
- **Features (35):**
  - past reactions (signs and z of the previous 4);
  - hedged returns over 5, 20, 63 and 252 days, and 60-day σ;
  - point-in-time XBRL: EPS growth / price, SUE of the last 4 quarters, revenue growth, age of
    the last quarter;
  - insider buying over 90 days;
  - reporting lag versus last year;
  - sector.

## Results (pooled out-of-sample, 2018-01 → 2025-03)

| Model | Hit | Weighted hit (q* 0.531) | Mean net / trade | One-sided lower 95% | p | Net, winsorized | vs always-long | Short trades | Calendar t |
|---|---|---|---|---|---|---|---|---|---|
| **W-L** (logistic, primary) | 51.9% | 53.7% | +0.05% | −0.04% | 0.18 | +0.04% | −0.06% | −0.44% (5,399) | −0.13 |
| W-G (boosted trees) | 51.9% | 53.3% | +0.01% | −0.07% | 0.40 | +0.00% | −0.10% | −0.52% | −0.08 |
| W-R0 (always long) | 52.5% | 54.3% | +0.11% | +0.01% | 0.03 | +0.10% | — | — | −0.31 |
| W-R1 (previous reaction's sign) | 49.9% | 50.0% | −0.31% | | 1.00 | | −0.42% | | −4.90 |
| W-R2 (63-day momentum sign) | 50.6% | 50.4% | −0.26% | | 1.00 | | −0.37% | | −3.23 |
| W-R3 (vote of the last 4 reactions) | 51.8% | 52.7% | −0.05% | | 0.83 | | −0.16% | | −1.62 |

**W-L in sub-periods and subsets** (mean net per trade, p):

| Sub-period or subset | Mean net | p |
|---|---|---|
| 2018–2019 | +0.01% | |
| 2020–2025Q1 | +0.07% | |
| Predictable dates | +0.05% | 0.17 |
| No pre-move | +0.07% | 0.11 |
| Ex-COVID | +0.05% | 0.18 |
| Without the timing feature | +0.06% | 0.17 |
| Without the deal filter | +0.06% | 0.14 |

**Verdict: not confirmed.**
- W-L's net is positive, but its lower bound is negative.
- It loses to always-long.
- Its short predictions lose money.
- Its calendar-time t is negative.

## Interpretation

- **The free public features carry almost no information about the direction of the earnings
  reaction.**
  - The best model reaches 51.9% (53.7% |R|-weighted), right at the break-even.
  - Gradient boosting does no better than the linear model.
  - The rules based on past reactions and momentum are no better than a coin, or worse:
    reaction signs do not persist, and pre-announcement momentum does not predict them.
- **What little edge there is comes from the earnings-announcement premium.**
  - Always-long earns +0.11% net per trade (p 0.03 on the event-weighted mean).
  - But it has a negative calendar-time t, so it is not robust across days.
  - W-L's long calls ride this premium, while its short calls lose 0.44% per trade.
  - R0 was a reported baseline (one of 6 trials, at the DSR's N), not the primary test, so the
    premium is not claimed as a finding.
- **Survivorship inflates the long side:** the universe is current constituents, so firms that
  later collapsed are missing.
- **The free-data path to forecasting earnings direction closes here.**
  - The literature's 55–60% accuracy relies on analyst consensus and revisions (paid, backlog
    B).
  - The free SUE / time-series fundamentals alone do not reach the about 53% break-even in
    2018–2025.

## Limits

- Q4 EPS coverage is lower (net income ÷ quarterly diluted shares).
- Event dates come from the 8-K; the predictable-date subset gives the same result.
- Sectors are today's; borrow costs exclude hard-to-borrow names.
- The hold-out is untouched.
