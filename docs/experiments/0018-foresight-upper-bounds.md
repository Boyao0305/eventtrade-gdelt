# 0018 — Value of foresight for earnings, macro releases, DoD awards and insider buys (ADR 0016)

Date: 2026-10-15. Code: commit `c75dc11`, run once from the frozen checkout (`foresight-ub`),
after a blind dry run (no output but run time and counts). 22 trials logged (hypothesis U),
all with `lookahead = true`, so they are excluded from DSR's N.

Periods: A = 2016–2019, B = 2020–2025-03. Costs are round trips.

## 1. If we knew the direction in advance (oracle; descriptive, no verdict)

| Event | Period | n | Mean move on event | Ordinary days | Ratio | Break-even hit rate q* (mean / median / winsorized) |
|---|---|---|---|---|---|---|
| Earnings, hedged close(d−2) → close(d+1) | A | 12,409 | 4.40% | 1.56% | ×2.8 | 0.534 / 0.549 / 0.535 |
| | B | 17,865 | 4.81% | 2.18% | ×2.2 | 0.531 / 0.544 / 0.532 |
| SPY 08:30 gap on CPI/NFP days | A | 89 | 0.35% | 0.36% | ×1.0 | 0.528 |
| | B | 120 | 0.68% | 0.49% | ×1.4 | 0.515 |
| SPY 13:45 → close on FOMC days | A | 27 | 0.50% | 0.32% | ×1.6 | 0.520 |
| | B | 39 | 0.86% | 0.45% | ×1.9 | 0.512 |

- **Earnings reactions are large and specific.** They are 2–3 times an ordinary 4-day move
  (Δ +2.8% and +2.6%, tight CIs).
- **A forecaster of the reaction's direction would break even at about 53–55% accuracy** with
  |R| weighting, against 30 bp costs. On ordinary days the same forecaster needs 57–60%,
  because the moves are smaller.
- **Macro releases:**
  - CPI/NFP gaps are no larger than ordinary-day gaps in 2016–2019.
  - They are 1.4× larger in 2020–2025, the inflation years.
  - FOMC afternoons move 1.6–1.9× more than other afternoons.
  - SPY costs are tiny, so the break-even accuracy is about 51–53%. But the moves are small
    (0.4–0.9%) and there are only 27–120 events per period.

## 2. If we knew the event earlier (signed; verdict on D = event − placebo − cost)

| Cell | A: event / placebo / D [95% CI] | B: event / placebo / D [95% CI] | Verdict |
|---|---|---|---|
| Earnings pre-drift d−5 → d−2, in the direction of the coming reaction | +0.01% / −0.03% / −0.27% [−0.31, −0.23] | −0.03% / +0.00% / −0.33% [−0.44, −0.23] | no value |
| SPY d−2 → d−1 before CPI/NFP, in the direction of the coming gap | +0.04% / −0.10% / +0.12% [−0.06, +0.29] | −0.03% / −0.03% / −0.02% [−0.23, +0.18] | inconclusive |
| DoD material award, 16:00 → next open | −0.09% / +0.02% / −0.56% | +0.20% / +0.01% / −0.23% | no value |
| DoD moderate award, 16:00 → next open | +0.02% / +0.03% / −0.25% | −0.01% / +0.02% / −0.27% | no value |
| DoD day before (material / moderate) | ≈ 0 | ≈ 0 | no value |
| Insider buy, trade date → day before filing (all) | +0.16% / +0.02% / −0.16% | +0.05% / +0.02% / −0.26% | no value |
| Insider buy, CEO/CFO | +0.23% / +0.01% / −0.08% | −0.02% / +0.02% / −0.34% | no value |

- **Insider buys:** 1,435 purchases filed more than 5 business days late are excluded under the
  ADR 0011 rule.
- **No leak, and nothing to gain from knowing the event early.**
  - Prices do not drift ahead of earnings in the direction of the coming surprise.
  - DoD awards barely move their stock overnight: +0.2% at most, below costs.
  - Insiders' own purchases are followed by only +0.05–0.23% before the filing.

## Reading

- **Knowing *that* or *when* an event happens is worth nothing in these sources.**
  - Scheduled events (earnings, CPI, FOMC) are already public.
  - DoD awards and insider buys move prices too little for early knowledge to pay.
- **Knowing the *direction* is where the value sits, mainly for earnings.**
  - Reactions average 4.4–4.8%.
  - A model that calls the sign of the earnings reaction right more than about 53–55% of the
    time (|R|-weighted) would beat 30 bp costs. This is a pre-announcement trade: enter at
    close(d−2), two days before the announcement.
- **Macro direction** also needs only ~52% accuracy, but with small moves and few events per
  year. Its statistical power and capacity are limited.
- **This is the only remaining direction backed by evidence:** forecasting the sign of the
  earnings reaction (or of earnings surprises) before the announcement, from alternative data.

## Limits

- These are upper bounds. Event |R| includes the reaction to the surprise itself; a forecaster
  only captures what its accuracy allows.
- Survivorship: current constituents.
- DoD and insider cells use event-average differences.
- The hold-out is untouched.
