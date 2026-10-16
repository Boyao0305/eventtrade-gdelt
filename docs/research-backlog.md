# Research backlog

One page listing what has been tested, what is open and what needs a decision. Details live in
`docs/experiments/` (results), `docs/adr/` (pre-registrations) and `docs/agent/log.md` (session
handoff). Update this file when an experiment closes or a direction opens.

Last update: 2026-10-16. Trials: 8,258 (look-ahead diagnostics are excluded from DSR's N).
Spend: ≈ $5.25 of the $20 cap (BigQuery only). The hold-out (≥ 2025-04-01) is untouched.

## 1. Signals tested (all pre-registered, run once)

| # | Signal / event | ADR → exp | Verdict | One-line reason |
|---|---|---|---|---|
| 1 | GDELT daily country/company signals, screens | 0001–0004 → 0003–0005 | null | no IC beyond noise |
| 2 | GDELT extreme shocks → ETF / stock returns and volatility | 0005 → 0006 | null | 0/47 after search correction (EM volatility hint, p ≈ 0.005, not significant after correction) |
| 3 | F1: EM conflict → volatility follow-up | → 0007 | null | did not confirm |
| 4 | GDELT themes (A2) | 0004 → 0008 | null | |
| 5 | GDELT intraday, fixed configuration (15-minute) | 0007 → 0009 | null | means within ±2.3 bp |
| 6 | GDELT intraday parameter search (5,440 configurations) | 0008 → 0010 | null | nothing beats costs |
| 7 | Foresight on GDELT news (known 15 minutes to 5 days early) | 0009/0010 → 0011/0012 | no value | company pre-news drift did not replicate; country 0/154 |
| 8 | SEC insider purchases, S&P 500/400 | 0011 → 0013 | near miss | +2–3% at 60 days, replicated in sign; Holm p 0.009 vs 0.0071 |
| 9 | SEC 13D activist filings | 0011 → 0013 | null | |
| 10 | Insider purchases, fresh small/mid caps | 0012 → 0014 | partial | small effect; fails costs and survivorship |
| 11 | Post-earnings drift (price-reaction surprise) | 0013 → 0015 | null | 0/6 |
| 12 | Scheduled macro on SPY (pre-FOMC drift, FOMC cycle, CPI/NFP days, gaps) | 0014 → 0016 | null | 0/5 |
| 13 | DoD contract awards ≥ 0.1% / ≥ 1% of market cap | 0015 → 0017 | null | 0/6; material +1.5% vs +0.7% over 20 days, p 0.12 |
| 14 | Value of foresight: earnings, macro, DoD, insiders | 0016 → 0018 | diagnostic | knowing *when* is worthless; knowing earnings *direction* is valuable (break-even accuracy ≈ 53–55%) |
| 15 | Event response curves (magnitude, direction, reversal, time scale) | 0017 → 0019 | diagnostic | country news moves nothing at any horizon; news spikes are directionless volatility; no reversals; insiders and earnings directional |

## 2. Open research directions

| Direction | Evidence for it | What it needs | Cost | Status |
|---|---|---|---|---|
| **A. Forecast the direction of the earnings reaction before the announcement** | Exp 0018: reactions average 4.4–4.8% (2–3× an ordinary move); a forecaster breaks even at ≈ 53–55% accuracy against 30 bp costs | Features from data we already have or can pull for free: past results from SEC XBRL, the stock's past earnings reactions, pre-announcement price/volume, insider buying | free | **recommended next**, waiting for the user's go |
| B. Earnings direction with alternative data | same as A | Google Trends, Wikipedia page views, job postings (free but patchy), or analyst consensus / card-spend panels (paid) | free to paid | after A |
| C. Volatility around company news spikes | Exp 0019: variance 1.3–1.5× normal for 1–3 days, direction random | option prices (implied volatility) to check whether options already price this in, and to trade straddles | paid, over the $20 cap | needs the user's approval |
| D. Volatility around FOMC afternoons and CPI/NFP mornings | Exp 0019: FOMC afternoons 2–3× normal variance, CPI/NFP mornings 1.4–1.75× (2020–25 only; FOMC 2016–19 has too few events) | option data, as for C. Scheduled events are usually priced into implied volatility, so the edge is doubtful | paid | low priority |
| E. Insider purchases as one input, not a standalone strategy | Exp 0013/0019: +0.7–1.6% within 2–10 days, +2–3% over 60 days; failed costs and confirmation alone | combine with A, or a portfolio ADR | free | open |
| F. 8-K distress items (4.02 restatements, 3.01 delisting notices) | many events in small caps | prices of firms that later delisted (survivorship), and short-borrow costs | partly paid | parked |
| G. Index changes, spin-offs, 13F "best ideas" | literature | S&P change dates, Form 10 filings, 13F data sets (free from SEC) | free | not started |

## 3. Data and method fixes (carry into the next study)

- **Winsorize signed returns, or screen price errors** (`small_insider.clean_opens`).
  CHRD 2020-11-19 (bankruptcy re-listing) shows a fake +25,700% one-day move in Yahoo prices
  (exp 0019).
- **Check XBRL units before any public-float rule.** Some filers tag `EntityPublicFloat` in
  thousands, so ADR 0015's check dropped KTOS, WWD, OSIS and others (exp 0017).
- **Placebo nulls must keep event days in the draw** (a permutation within the stratum). See ADR
  0014's implementation notes.
- **DoD award amounts:** 40% are IDIQ ceilings, not revenue (`dod_study.is_ceiling`).
- **Alpaca bars on NYSE half-days** run past 13:00 (extended hours). Use
  `macro_calendar.NYSE_EARLY_CLOSES`.
- **Multiplicity:** any ADR that picks a cell or horizon suggested by the exp 0019 curves must
  count all V cell × horizon combinations (≈ 17 × 8) in its DSR N.

## 4. Decisions waiting for the user

1. Go ahead with direction A (earnings direction from free in-house data)?
2. Spend beyond the $20 cap on option data (C, D) or consensus estimates (B)? Not approved so
   far.
3. Merge `dev` into `main`? Not done; `main` still holds only the initial commit.
