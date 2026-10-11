# 0016 — Scheduled macro announcements on SPY (ADR 0014)

Date: 2026-10-11. Code: commit `e7da840`, run once from the frozen checkout (`macro-study`), with
the calendar SHA-256 as recorded in the ADR. 5 trials logged (hypothesis M, discovery only). The
store holds 8,196 trials.

## Question

Do scheduled US macro announcements leave a tradable pattern in SPY, in 2016–2019? The cells:

| Cell | Question |
|---|---|
| M1 | Pre-FOMC drift, close(d−1) → 13:45 |
| M2 | FOMC-cycle even weeks |
| M3 | Announcement-day premium on CPI, NFP and FOMC days |
| M4 | Continuation of the 08:30 opening gap |
| M5 | Continuation of the FOMC statement reaction, 14:30 → close |

## Data

- **Calendar:** 2,324 SPY trading days (daily-price calendar, 2016-01-04 → 2025-03-31).
- **18 dropped sessions:** all NYSE 13:00 half-days. Alpaca's regular-session bars continue past
  13:00 on those days, so the list and the bars disagree, and amendment 6 drops them. No
  announcement falls on them. M2 loses those days' and the next days' returns; nothing else
  changes.
- **FOMC:** 73 scheduled statements held, 61 of them with a press conference. The 7 days with
  both FOMC and an 08:30 release are dropped from M1, M4 and M5.
- **Events (full period):** M1/M5 66, M3 284, M4 211. The placebo pool has 1,473 days.

## Results (discovery 2016-01-04 → 2019-12-31)

Placebo means come from a permutation within year × weekday (10,000 draws). Net = cell direction
× (mean − placebo mean) − 2 bp.

| Cell | n | Event mean | Placebo mean | t | p | Net |
|---|---|---|---|---|---|---|
| M1 pre-FOMC (+) | 27 | +2.6 bp | +12.1 bp | 0.41 | 0.807 | −11.5 bp |
| M2 even − other weeks (+) | 517 even days | +0.8 bp/day | — | 0.16 | 0.435 | +0.4 bp/day |
| M3 announcement days (+) | 122 | +9.1 bp | +7.1 bp | 1.23 | 0.379 | 0.0 bp |
| M4 08:30 gap continuation (+) | 90 | +4.0 bp | +2.6 bp | 0.53 | 0.401 | −0.6 bp |
| M5 statement continuation (±) | 27 | +3.8 bp | +0.0 bp | 0.32 | 0.650 | +1.8 bp |

**BH (q = 0.10): 0 of 5 cells discovered.** The 2020–2025 confirmation period was never
evaluated. **Verdict: null.**

**Reported only:**
- **M2:**
  - even-week days average +6.5 bp, other days +5.7 bp;
  - the shift-placebo p is 0.44;
  - b excluding the 2018 volatility episodes is +3.9 bp/day.
- **M3 without FOMC days (CPI/NFP only):** +15.7 bp, placebo p 0.14.
- **M5 press-conference days (n 15):** +7.5 bp.

## Interpretation

- **The pre-FOMC drift is gone in 2016–2019.** SPY rose +2.6 bp from the previous close to 13:45
  on statement days, against +12 bp on comparable Wednesdays. This matches the literature: the
  Lucca–Moench drift faded after its publication in 2015.
- **There is no FOMC-cycle pattern** (b = +0.8 bp/day, t 0.16).
- **The announcement-day premium points the right way but is not distinguishable from ordinary
  days.**
  - Announcement days average +9 bp against +7 bp on matched days.
  - The CPI/NFP-only version (+16 bp, p 0.14) is the closest thing to a lead. It was reported
    only and is not a discovery.
  - As amendment 2 warned, a null here excludes only effects of roughly 25 bp or more. A
    Savor–Wilson-sized premium (~10 bp) can be neither confirmed nor ruled out with 4 years of
    data. At this size it would also barely cover costs.
- **Gap continuation and statement continuation:** both null.
- **The user's point stands only in a weaker form:** announcements move SPY, but this study
  tested direction, not size. The **direction** is not predictable from the schedule, the gap, or
  the first 30 minutes of the reaction, at a size that beats the 2 bp round trip. Volatility
  around announcements was not tested, so options and volatility strategies are a separate
  question.

## Limits

- The power is low for M1/M5 (27 events each) and moderate for M3/M4.
- Surprise measures (actual − consensus) were not used: that needs paid data.
- The half-day drop is a data artefact. It is harmless here, but future intraday studies should
  treat Alpaca bars after 13:00 on half-days as extended hours.
- The hold-out is untouched.
