# 0011 — Value of foresight (ADR 0009, deliberate look-ahead diagnostic)

Date: 2026-10-09. Code: commit `99a83b9`, run once from the frozen checkout (`foresight`), after
experiment 0010. 220 trials were logged (hypotheses O and R), all with `lookahead = true`, so
they are excluded from DSR's N. The store holds 8,009 trials, of which 7,789 count for DSR.

## Question

If the problem is delay, would knowing an event earlier have been worth something? How much did
prices move between Δ before the news became available (Δ from 15 min to 5 days) and its first
availability t_a? And do news categories follow price shocks (reverse causality)?

## Results — foresight (discovery 2016–2019; fresh events; 2,000 placebo draws)

**Fresh events were scarce for country news.** The pre-registered freshness rule asked for z < 1
in every window of the previous 24 hours for the same country and category. That left 0–33
events per country cell in discovery: quad4 dm 10, r14 em 33, violence dm/em 10/9, r17 dm/em
9/17, and so on. **21 of the 24 country and producer cells were below the 30-event minimum (no
p).** The rule is far stricter for countries, where some window crosses z = 1 on most days, than
for companies, which were mentioned rarely.

So the study **cannot** answer the question for the categories assumed exogenous, the ones that
matter for an event-arrival project. Their point estimates (n ≤ 33) are noise and are not
interpreted.

Testable cells (signed mean in bp, positive = the registered direction; placebo p):

| Cell | 15m | 30m | 60m | 120m | 1d | 2d | 5d | Round trip |
|---|---|---|---|---|---|---|---|---|
| Positive company spike (3,096 events) | +1.9 (0.051) | +2.8 (0.023) | **+8.0 (0.0005)** | **+11.6 (0.0005)** | **+14.3 (0.0005)** | **+18.7 (0.0005)** | +24.4 (0.013) | 24.3 |
| Negative company spike (3,610 events) | +0.7 (0.114) | +2.4 (0.014) | +2.5 (0.057) | +5.8 (0.005) | +4.8 (0.032) | +6.2 (0.010) | +1.7 (0.060) | 24.3 |
| Protest r14 em (33 events) | +0.3 (0.43) | −1.5 (0.66) | +7.9 (0.22) | −10.2 (0.80) | −7.0 (0.69) | −13.9 (0.80) | +20.8 (0.36) | 13.9 |

**Discovery:** 4 of 168 cells (BH q = 0.10), all positive company spikes, at leads 60m, 120m, 1d
and 2d. Stocks rose before positive news about them appeared.

**Confirmation 2020-01-02 → 2025-03-31 (Holm): all 4 fail.**

| Lead | Signed mean | t | p |
|---|---|---|---|
| 60m | −1.6 bp | −0.72 | 0.85 |
| 120m | −0.3 bp | −0.11 | 0.67 |
| 1d | +0.1 bp | 0.02 | 0.63 |
| 2d | +4.1 bp | 0.81 | 0.36 |

**No-value rule** (upper 95% bound of the mean below the round trip):
- Negative company spikes: holds at all 7 leads.
- Positive company spikes: holds at 5 of 7 leads.

## Results — reverse event study (do categories follow 15-minute price shocks?)

The response is each category's firing rate over the 24 hours after a |abnormal 15-minute
return| > 3σ shock (shocks with no firing in the prior 24 hours), against a placebo.
- **Only protests in DM countries** respond significantly (r14 dm, p = 0.001, BH-significant):
  protests "follow price". Such moves are plausibly market-moving news covered as protests.
- **Every other category does not follow 15-minute shocks within 24 hours.** That includes
  company spikes (p 0.18 / 0.31), violence, coercion, quad4, tone and attention.

## Interpretation (against ADR 0009's pre-registered reading)

- **Company news.** In 2016–2019, prices moved in the news direction during the hours and days
  before positive company news, which is a "news follows price" or leak pattern. It does
  **not** replicate in 2020–2025, and it is below costs at most leads. Knowing company news early
  would not have given a robust, tradable edge in this data.
- **Country news (the "exogenous" categories).** **Inconclusive.** The freshness rule removed
  almost all events, so the study cannot say whether predicting the arrival of violence,
  protest or coercion events would pay. Re-testing with a looser freshness rule would be a new,
  pre-registered study (it cannot be an amendment, now that results have been seen).
- **Reverse causality** is rare at the 15-minute → 24-hour scale. Only DM protests follow price
  shocks.

## What this does not establish

- The value of foresight for country or category events: underpowered, as stated above.
- Anything about longer-horizon anticipation (> 5 days) or other event sources.
- The hold-out is untouched.
