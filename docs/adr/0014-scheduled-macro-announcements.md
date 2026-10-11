# 0014 — Scheduled macro announcements on SPY (hypothesis family M)

Date: 2026-10-11. Written before any announcement date was matched to prices. The user delegated
the choice and order of event sources. Single-stock event lines are exhausted on free data
(experiments 0003–0015). This is the market-level line the user raised: central-bank and macro
announcements visibly move the market.

## Prior evidence

- **Pre-FOMC drift** (Lucca & Moench 2015): SPX earned about +49 bp in the 24 hours before
  scheduled FOMC statements (1994–2011), most of all equity premium in that period. Later work
  reports it weakened or vanished after 2015.
- **FOMC cycle** (Cieslak, Morse & Vissing-Jorgensen 2019): stock returns are concentrated in
  even weeks (0, 2, 4, 6) of the FOMC cycle.
- **Announcement-day premium** (Savor & Wilson 2013): market returns on CPI, employment and FOMC
  days are higher than on other days.
- **Opening-gap continuation on 08:30 releases** and **FOMC statement-reaction continuation or
  reversal** around the 14:30 press conference: mixed, mostly practitioner evidence.

All five are tradable with SPY alone, which costs ~1 bp per side. The announcement times are
public and scheduled, so positions can be set in advance.

## Decision

**Data.**
- SPY 15-minute regular-session SIP bars (`intraday.sqlite`, Alpaca, from 2016-01-04).
- The trading calendar and early closes come from SPY's bars (`intraday_study.trading_calendar`).
- `close(d)` = close of SPY's last regular-session bar on d; `p(d, hh:mm)` = close of the bar
  ending at hh:mm ET; `open(d)` = open of the 09:30 bar.
- **Announcement dates:** `core/macro_calendar.py`, transcribed from federalreserve.gov and
  bls.gov (sources in the module docstring):
  - scheduled FOMC statement days (14:00 ET);
  - unscheduled FOMC announcements (used only to exclude days from placebo pools);
  - CPI and Employment Situation releases (08:30 ET).
- A day is an **announcement day** if any of these falls on it. A release on a non-trading day
  (e.g. a Sunday) makes the next trading day an announcement day.

**Cells (one statistic each; sign registered).**

| Cell | Days | Per-day value | Sign |
|---|---|---|---|
| M1 pre-FOMC drift | scheduled FOMC days d | p(d, 13:45) / close(d−1) − 1 | + |
| M2 FOMC cycle | all trading days | close-to-close return; even-week indicator | + (even > other) |
| M3 announcement premium | CPI, NFP or scheduled FOMC days | close(d) / close(d−1) − 1 | + |
| M4 08:30 gap continuation | CPI or NFP days | sgn(open(d)/close(d−1) − 1) × (close(d) / p(d, 09:45) − 1) | + |
| M5 FOMC statement continuation | scheduled FOMC days | sgn(p(d, 14:30)/p(d, 13:45) − 1) × (close(d) / p(d, 14:30) − 1) | two-sided |

- **M2 weeks** follow CMVJ. Trading day 0 is the scheduled FOMC statement day. Even weeks are
  days −1..3, 9..13, 19..23 and 29..33 counted from the most recent statement day; days before
  the next statement's day −1 keep counting from the previous one.
  - Value: the NW slope b (5 lags) in r_t = a + b·even_t + e_t, one-sided b > 0.
- **Signs of zero** (gap or reaction exactly 0) drop the day.
- **A day with a missing needed bar** drops the day. Early-close days are excluded from M1, M4
  and M5.

**Nulls.**
- **M1, M3, M4, M5: placebo days.**
  - Each event day is replaced by a non-announcement trading day of the same weekday in the same
    calendar year, drawn without replacement within a draw.
  - Days adjacent to an announcement day (d−1, d+1) are excluded from the pool.
  - The per-day value is computed identically on placebo days. For M4 and M5 that uses the
    placebo day's own gap or reaction sign.
  - 10,000 draws, seed `config.seed`.
  - p = (1 + #{placebo mean ≥ event mean}) / (1 + B), one-sided in the sign. For M5 it is
    two-sided: 2 × min(p_up, p_down), capped at 1.
- **M2:** the NW one-sided p of b (normal approximation). Reported only: a placebo that shifts
  every statement day by the same random k ∈ {−15..−3, 3..15} trading days (1,000 draws).

**Periods.**
- **Discovery** 2016-01-04 → 2019-12-31: BH q = 0.10 across the 5 cells.
- **Confirmation** 2020-01-02 → 2025-03-31, for discovered cells only. All four are required:
  - Holm 0.05 (two-sided for M5, in the direction found in discovery);
  - the mean in the registered direction (for M5, the discovery direction);
  - net of costs > 0;
  - the same direction with 2020-02-20 → 2020-04-30 excluded.
- **Costs:** SPY 1 bp per side, so 2 bp per round trip per event day (M1, M3, M4, M5).
  - M2: 2 bp per even-week block. The net is the mean even-week daily return minus the block
    cost spread over its days, and it must be > 0.
- **Logging:** each cell × period is a `signal` trial (track A, hypothesis `M`,
  `params.cell` = M1..M5). Once only; logged after everything is computed.

**Reading.**
- **A confirmed cell** goes to gate 2 under a new ADR: a SPY backtest with costs and DSR counting
  all trials, then the hold-out once.
- **No discovery:** the market-level scheduled-announcement line on SPY closes. The literature
  effects are then not present at a tradable size in 2016–2019.

## Consequences

- +5 discovery trials (plus confirmations). No new data or cost: SPY bars are stored, and the
  calendar is public.
- **Power:** M1/M5 have ~32 discovery events and ~42 confirmation events.
  - A +49 bp effect against SPY's ~0.7% daily σ gives t ≈ 4.
  - A +10 bp effect is undetectable.
  - M3 (~280 days) and M4 (~190 days) have more power.

## Alternatives rejected

- *Consensus surprises* (actual − forecast for CPI/NFP): paid data. The gap sign is the free,
  real-time proxy.
- *Rates or sector ETFs (TLT, XLU, XLF):* TLT has no stored 15-minute bars. Sectors would add
  cells without a prior; they can come later with their own ADR.
- *A window grid for M1/M5:* no search, one window each, taken from the literature or from the
  14:30 press-conference time.

## Amendments after review (2026-10-11, before any return was computed)

`core/macro.py` and `cli/pipelines_macro.py` exist as drafts with synthetic tests only. No SPY
return has been computed on an announcement day. All review items are adopted; where this
section conflicts with the Decision above, this section wins.

1. **M5 press conferences (blocker).** Before 2019 the press conference followed only the
   March/June/September/December statements.
   - M5 is still computed on all scheduled statement days, with 14:30 fixed for all.
   - `macro_calendar` flags press-conference days. The press-conference subsample is reported
     only.
2. **Power, corrected (blocker).**
   - Discovery has M3 ≈ 125 days, M4 ≈ 96 and M1/M5 = 32.
   - A Savor–Wilson-sized M3 premium (~11 bp) gives t ≈ 1.4. An M3/M4 null therefore only
     excludes effects of about 25 bp or more, and a null is read that way.
3. **Same-day 08:30 release and FOMC statement.**
   - These days are dropped from M1, M4 and M5 and kept once in M3.
   - They are listed in the output, and a test pins the list.
4. **Releases on non-trading days** (e.g. NFP on Good Friday).
   - They are not events in M3 or M4.
   - They and the next trading day are excluded from the placebo pool.
   - A release moved by the agency to another trading day stays an event.
5. **Scheduled versus actual FOMC.**
   - A scheduled day counts only if a 14:00 statement was released that day. Cancelled meetings
     (2020-03-18, replaced by the 2020-03-15 action) are dropped and listed.
   - Unscheduled actions, including weekend ones, exclude their day and the next trading day from
     the pool only.
6. **Trading calendar.**
   - Trading days are the dates of SPY's **daily** prices (yfinance, independent of Alpaca),
     2016-01-04 → 2025-03-31.
   - A daily-calendar day with no Alpaca bars, or with a missing bar that a cell needs, is dropped
     from that cell and counted in the output.
   - An early close is a day whose last SPY bar starts at or before 12:45 and which is in the
     NYSE early-close list in `macro_calendar` (the days before/after Independence Day,
     Thanksgiving and Christmas). Any disagreement between the two is reported and the day is
     dropped.
7. **M2 shift placebo** (reported only): k uniform on {±1..±14} without multiples of 5, which
   map even weeks onto even or odd weeks.
8. **Net tests compare with ordinary days.**
   - M2: b − 2 bp × (number of even-week blocks) / (number of even-week days) > 0. Touching
     week 6 and week 0 form one block.
   - M1, M3, M4, M5: event mean − placebo mean − 2 bp > 0, in the cell's direction.
9. **M5 confirmation** is one-sided in the discovery direction. The Holm family is the
   discovered cells at α = 0.05.
10. **The calendar is frozen before computation.**
    - `macro_calendar.py` is committed, and its SHA-256 is recorded below before any return is
      computed.
    - Tests pin the per-year counts and the overlap and cancellation lists.
    - Any later correction is a new ADR.
11. **Pools.**
    - A year × weekday pool smaller than that year's events on that weekday makes the run fail,
      with no fallback.
    - Placebo pools exclude early closes and announcement days ±1.
12. **Reported only:**
    - M3 without FOMC days;
    - M2's b with 2018-02 and 2018-10 → 2018-12 excluded;
    - the t-statistic on the COVID-excluded sample;
    - a note that M1 is a SPY-session adaptation (close(d−1) → 13:45), not the Lucca–Moench
      14:00 → 13:55 window, with entry by MOC and no auction slippage modelled.
13. **Multiplicity.** M1, M3 and M5 share FOMC days, and M3 and M4 share CPI/NFP days. The
    dependence is positive, so BH remains valid.

## Implementation notes (2026-10-11, before the run)

- **Calendar frozen.**
  - `src/eventtrade/core/macro_calendar.py`, SHA-256 of the LF-normalised file (as stored in
    git)
    `85658d1e1d84aa64dc395436ae3eb94454f7bf0afe070ce90062518766d6ebcc`.
  - Contents: 82 scheduled FOMC days, 1 cancelled (2020-03-18), 3 unscheduled actions, 65 press
    conferences, 123 CPI and 123 NFP releases, 20 early closes and 97 holidays.
  - It was transcribed from federalreserve.gov, bls.gov and nyse.com archives, with no market
    data.
  - The 7 FOMC/08:30 overlap days (2016-03-16, 2017-03-15, 2017-06-14, 2017-12-13, 2019-12-11,
    2020-06-10, 2024-06-12) are pinned in `tests/core/test_macro.py`.
- **Placebo null as a label permutation (found on synthetic data, before any real return).**
  - Drawing placebo days only from non-announcement days makes the null too narrow by about
    √(1 − n/N) when events are a large share of a year × weekday. NFP is 12 of about 40 eligible
    Fridays a year.
  - The synthetic end-to-end test showed it: a cell with no planted effect (M4) was "discovered"
    and "confirmed".
  - `macro.placebo_test` now draws, within each year × weekday, from the pool days **plus the
    events' own days**: a permutation test with exact size under exchangeability. A size test
    pins it.
  - Amendment 11's failure rule now requires at least as many pool days as events in every
    year × weekday.
- **The same issue in earlier studies:**
  - ADR 0011/0012's matched pools contain all of a ticker's stratum days (event days included,
    thousands of days), so the effect is negligible.
  - ADR 0013's primary pool (the same stock's calm earnings) is small, so its null was somewhat
    too narrow. That is anti-conservative, but the study was null (p 0.36–0.89), so the verdict
    stands.
- **Trading calendar:** SPY daily dates from 2016-01-04. M2's cycle starts at the first 2016
  statement (2016-01-27); earlier days have no cycle day and drop out.
