# 0015 — Post-earnings drift on the S&P 1500 (ADR 0013)

Date: 2026-10-11. Code: commit `cff10be`, run once from the frozen checkout (`earnings-study`).
6 trials logged (hypothesis P1, discovery only). The store holds 8,191 trials.

## Question

After an earnings announcement whose price reaction is extreme for the stock (|z| ≥ 2), does the
stock keep drifting in the direction of that reaction over 5, 20 or 60 trading days? Returns are
hedged with the sector SPDR and measured open to open from d+1. The comparison is with the same
stock's ordinary earnings (|z| < 2).

## Data

- **Earnings days:** 34,868 from 8-K Item 2.02 in `company_filings`, across 889 tickers. Days
  carrying Items 1.01/2.01 (deals) were dropped, and days within 5 trading days were merged.
  There were 35,931 raw 2.02 days.
- **Timing check (amendment 2):**
  - About 23% of the reaction comes before d in every year (2015–2025: 0.21–0.32). That is above
    the 0.10 limit, so the reaction window is close(d−2) → close(d+1).
  - This rule is fixed by the ADR and depends on no outcome.
- **Surprises:** 31,758 earnings days have a z score. 38.1% have |z| ≥ 2, so the threshold marks
  "large relative to the stock's ordinary 3-day moves", not "rare".
- **Coverage:** at least 3 Item 2.02 days in every year. 750 of 896 tickers pass in discovery
  and 820 in confirmation.

## Results (discovery 2016-01-04 → 2019-12-31)

The mean is the raw hedged return. For `neg` cells the drift hypothesis needs a negative mean;
the p values are one-sided in the drift direction.

| Cell | Events | Mean | NW t | Matched p (primary) | Big-move p (secondary) | Unmatched p |
|---|---|---|---|---|---|---|
| pos h5 | 2,833 | +0.07% | 0.58 | 0.450 | 0.016 | 0.940 |
| neg h5 | 2,295 | +0.18% | 1.40 | 0.857 | 1.000 | 1.000 |
| pos h20 | 2,827 | +0.16% | 0.64 | 0.678 | 0.010 | 1.000 |
| neg h20 | 2,285 | +0.17% | 0.55 | 0.507 | 0.0005 | 1.000 |
| pos h60 | 2,662 | +1.29% | 2.63 | 0.356 | 0.131 | 0.753 |
| neg h60 | 2,173 | +1.28% | 2.02 | 0.888 | 1.000 | 0.823 |

**BH (q = 0.10): 0 of 6 cells discovered.** Under the pre-registration nothing goes to
confirmation, so the 2020–2025 period was never tested. **Verdict: null.**

## Interpretation

- **There is no post-earnings drift in this large-cap universe in 2016–2019.**
  - Stocks after big positive reactions do no better than after the same stock's ordinary
    earnings.
  - Stocks after big negative reactions do no worse: their mean is **positive** (+0.2% at
    20 days, +1.3% at 60 days), which is mild reversal, not drift.
- **Both h60 means (+1.3%) are about the same for positive and negative surprises.** That is the
  general upward drift of these surviving S&P 1500 names against their sector, not a response to
  the surprise. The matched null removes it (p 0.36 / 0.89).
- **The secondary big-move p values are low for pos h5/h20 and neg h20.** They are reported only
  and say something else: earnings moves reverse less than equally large non-earnings moves.
  - Non-earnings jumps partly revert.
  - Earnings jumps do not revert, but they do not continue either.
  - That is a known microstructure fact, not a tradable drift. The primary null on the same
    stock's calm earnings is what the ADR tests.
- **This matches the literature:** PEAD is largely gone in liquid large caps after the 2000s.
  The return-based surprise proxy (no analyst consensus) is weaker than SUE, but at n ≈ 2,800
  per cell even a 0.5%/20-day drift would have shown up.

## Limits

- The surprise is price-based, not consensus-based, so analyst-revision drift is not tested.
- The universe is the 2026 S&P 1500 (survivors), which is why the raw 60-day means are positive.
- The hold-out is untouched.
