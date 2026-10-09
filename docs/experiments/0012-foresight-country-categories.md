# 0012 — Value of foresight for country news, first-firing freshness (ADR 0010)

Date: 2026-10-09. Code: commit `970aa54`, run once from the frozen checkout (`foresight-country`).
154 look-ahead trials were logged (hypothesis O2), all excluded from DSR's N. The store holds
8,163 trials, of which 7,789 count for DSR.

## Question

If we had known a country news burst (conflict, protest, coercion, violence, sanctions, tone,
attention, or producer-country conflict for commodities) 15 minutes to 5 days before GDELT
published it, would the price move over that window have been worth trading? This is the
follow-up to experiment 0011, whose freshness rule left too few country events.

## Data and power

- **Fresh events in discovery** (2016–2019, 60-minute lead): **144–906 per cell.** For example:
  - quad4 dm/em: 488/781;
  - protest r14 dm/em: 563/675;
  - violence dm/em: 493/786;
  - sanctions b163 dm/em: 144/175;
  - producer quad4/violence: 176/155.
- **Every one of the 154 cells was testable.** Under ADR 0009's rule, 21 of 24 pairs were not.
- **The publication and latency segments** are within ±3.3 bp in every cell at the 60-minute lead.

## Results (discovery; signed mean in bp, positive = registered direction; placebo p in brackets)

| Pair | 15m | 60m | 120m | 1d | 2d | 5d |
|---|---|---|---|---|---|---|
| quad4 dm | −0.1 (0.53) | +0.9 (0.09) | +2.4 (0.02) | −1.6 (0.70) | −2.2 (0.70) | −5.1 (0.85) |
| quad4 em | +0.4 (0.32) | +0.1 (0.47) | +0.7 (0.39) | +0.2 (0.48) | +3.3 (0.31) | −4.8 (0.81) |
| protest r14 dm | −0.3 (0.62) | +0.6 (0.31) | +1.1 (0.23) | +3.8 (0.03) | +5.2 (0.02) | +4.0 (0.03) |
| protest r14 em | +1.8 (0.03) | +2.7 (0.08) | +1.5 (0.31) | +3.4 (0.16) | +8.9 (0.05) | +19.0 (0.02) |
| coerce r17 dm | +0.3 (0.21) | −0.3 (0.57) | −1.1 (0.67) | −3.5 (0.94) | −3.4 (0.89) | −11.5 (1.00) |
| violence dm | −0.2 (0.61) | +0.2 (0.34) | +0.4 (0.33) | −1.6 (0.73) | −4.6 (0.84) | −7.5 (0.88) |
| violence em | −0.1 (0.63) | −1.9 (0.91) | 0.0 (0.53) | −2.2 (0.77) | +2.2 (0.32) | +9.8 (0.14) |
| tone dm | +0.8 (0.06) | +1.9 (0.06) | +0.4 (0.35) | −0.5 (0.53) | −9.4 (0.98) | −11.6 (0.95) |
| producer quad4 | +4.5 (0.03) | +2.3 (0.24) | −1.1 (0.54) | +0.9 (0.55) | −14.2 (0.87) | −11.8 (0.82) |
| producer violence | +2.8 (0.28) | +6.1 (0.11) | +9.6 (0.04) | +17.2 (0.015) | +8.4 (0.41) | −1.4 (0.65) |

The remaining pairs (quad1–3, sanctions, attention, tone em) are in the trial notes. All lie
within ±12 bp, with p ≥ 0.046.

**0 of 154 cells discovered.** The smallest p is 0.015, against BH's first threshold of
0.10/154 ≈ 0.00065. No confirmation is run.

**No-value rule.** In **128 of 154** cells the upper 95% bound of the signed mean is below the
round-trip cost (10–14 bp). The other 26 are inconclusive, not positive. They are mostly the noisy
1–5-day leads, plus the producer-violence leads at 120 minutes and 1 day.

## Interpretation

- **Perfect foresight of fresh GDELT country-news bursts would not have paid.**
  - **Intraday leads (15–120 min):** the move before publication is about 0–3 bp, an order of
    magnitude below costs.
  - **Day-scale leads:** the signs are mixed and none is significant after correcting for 154
    cells.
- **Combined with experiment 0011:**
  - company news showed a pre-news drift in 2016–2019 that did not replicate;
  - country news shows none even with enough events.
- **What it means for the "predict the event" idea.** Forecasting the arrival of these GDELT
  event types from alternative data would chase a price move that, on this evidence, is not
  there. A perfect forecast would have earned nothing tradable.
- **The two weak positives match their own priors**, but neither survives correction:
  - protests (r14) at day leads;
  - producer-country violence for commodities at 120 minutes to 1 day.

## Limits

- One configuration (L = 15, W = 60, θ = 3) defines the events; other definitions are untested.
- Effects beyond 5 days, or from other event sources (ACLED, filings, earnings), are out of
  scope.
- The 2016–2025Q1 data were already used by earlier studies, as disclosed in ADR 0010. The
  hold-out is untouched.
