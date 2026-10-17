# 0019 — Pre-earnings informed-trading footprints (hypothesis family X)

Date: 2026-10-16. The user asked whether insider or informed trading before an announcement
can be caught (2026-10-16). Written before any of the quantities below was computed. This
extends ADR 0018 (experiment 0020: free public features do not call the direction).

## Why

- **The channel:** informed trading before earnings, legal or not, should leave footprints in
  the days before d:
  - unusual volume;
  - one-sided order flow;
  - short selling ahead of bad news (Christophe, Ferri & Angel 2004);
  - pressure late in the day.
- **Legal insider trades are mostly barred** by blackout periods and are already in ADR 0018's
  features.
- **Experiment 0018** found no pre-earnings price drift in the direction of the coming reaction.
  So the price-based footprints are expected to be weak; volume and short selling are new
  information.

## Decision

**Events, target, trade, costs, walk-forward:** exactly ADR 0018 with its amendments and
implementation notes.
- Track B earnings days, deal-filtered, with the prior-year coverage rule; |R| > 100% dropped.
- R = sector-hedged close(d−2) → close(d+1); the decision is at close(d−2).
- Yearly walk-forward from 2016; out-of-sample 2018-01 → 2025-03; 30 bp plus borrow.

**Windows** (all data at or before close(d−2)):
- Window W = trading days [d−12, d−3].
- Baseline B = [d−72, d−13], excluding days within ±3 of any earlier 8-K 2.02 day of the stock.
- At least 40 baseline days, else the feature is missing.

**New features (6):**

| Feature | Definition | Data |
|---|---|---|
| X1 abnormal volume | log(mean daily volume over W / mean over B) | daily volume (`prices`) |
| X2 order-flow imbalance | (Σ volume of up bars − Σ volume of down bars) / Σ volume over W, minus the same over B. A bar is up if its close is above its open, down if below; flat bars are excluded | Alpaca 15-minute bars (`intraday.sqlite`) |
| X3 abnormal short ratio | mean daily short-sale volume / total volume over W, minus the same over B | FINRA Reg SHO daily short-sale volume (consolidated NMS file, free) |
| X4 | X1 × sign(X2) | |
| X5 | X1 × X3 | |
| X6 late-day pressure | Σ over W of the sector-hedged return from the 15:00 bar open to the close | Alpaca bars, plus the sector SPDR bars |

FINRA's short-sale volume covers trades reported to its facilities (off-exchange and some
exchange flow), not total short interest. It is noisy and stated as such.

**Tests** (the 2018–2025 data were used by ADR 0018, so everything below is counted as new
trials and a positive result needs the hold-out):
1. **Primary X-L:** ADR 0018's W-L model, with the same settings and walk-forward, on ADR
   0018's 35 features plus X1–X6. Requirements:
   - (a) mean net P&L, with the one-sided 95% week-block lower bound > 0;
   - (b) the paired difference X-L − W-L (same events), with lower bound > 0;
   - (c) the paired difference X-L − W-R0 (always long), with lower bound > 0;
   - (d) mean net > 0 on short predictions;
   - (e) mean net > 0 in 2018–2019 and in 2020–2025Q1.
2. **Rule X-R:** among the events with X2 in the top or bottom decile, where the cut-offs are
   computed on the training years of each walk-forward step, go long if X2 > 0 and short
   otherwise. No fitting. Same requirements (a), (d), (e).
3. **Reported only:**
   - the univariate signed means: R by decile of X1, X2, X3 and X6 (training-year cut-offs);
   - X-G (gradient boosting with the new features).
- **Multiplicity:** Holm at 5% over the two tests, X-L and X-R, using one-sided bootstrap p.
- **A positive verdict is "candidate"**, not "confirmed". It then goes to a once-only hold-out
  test (2025-04 → latest), under a new ADR fixed beforehand.

**Data.**
- FINRA daily short-volume files `CNMSshvol{YYYYMMDD}.txt`, 2015-10 → 2025-03-31, are
  downloaded into `data/shortvol.sqlite`. No file dated on or after the hold-out is fetched.
- The Alpaca bars are already stored.

**Logging.**
- One trial each for X-L, X-R and X-G (track B, hypothesis X). Once only; logged after
  everything is computed.
- DSR n_extra as in ADR 0018 (+156), plus ADR 0018's 6 trials.

## Consequences

- A free FINRA download of about 2,400 small files.
- If null, the free informed-trading channel is closed. Options footprints (implied-volatility
  skew, call/put volume) would need paid data (backlog C/D).

## Amendments after review (2026-10-16, before any feature or outcome was computed)

All items are adopted. Where they conflict with the Decision above, they win.

1. **Announcement day from report dates** (B1).
   - 8-K report dates and acceptance times are pulled from EDGAR submissions
     (`eightk_times`, 109,320 8-Ks).
   - **Announcement day a** = the first trading day on or after min(reportDate, filing date);
     one day later if accepted after 16:00 ET and reportDate ≥ the filing date.
   - Events whose reportDate is more than 1 trading day before the filing date use a (the
     reportDate). Their count is reported. They are not dropped, because a correctly timed a
     makes them usable.
   - Events with another 2.02 8-K (no deduplication, no deal filter) in [a−15, a−1] are dropped
     and counted.
   - The decision is at close(a−2); R = close(a−2) → close(a+1); W and B are relative to a.
   - The ADR 0018 baseline W-L is refit on the same events for the paired test.
2. **Order flow hedged by sector** (B2).
   - A bar is up or down by its return minus the same bar's sector-SPDR return (Alpaca).
   - corr(X2, hedged W return) is reported.
   - **Placebo X-R′:** the same decile rule on the sign of the hedged W return. X-R is credited
     only if X-R − X-R′ (paired, same events) has its one-sided lower bound > 0.
3. **Hold-out plan** (B3, fixed now).
   - At most one strategy goes to the hold-out: the Holm survivor with the smaller adjusted p.
   - It must first pass gate 2 and reach DSR ≥ 0.95 over all Track B trials (ADR 0001).
   - The model and cut-offs are frozen, trained on all events with a+1 < 2025-04-01. The
     universe is Track B as stored on 2025-03-31.
   - The FINRA, Alpaca, 8-K and XBRL hold-out pulls run only after that ADR is committed.
   - The test: the one-sided lower bounds of (b) and (a) both > 0.
   - Hold-out MDE ≈ 0.35%/trade (accepted now).
4. **Full ADR 0018 rules** (S1). X-L and X-R also need: (a) winsorized; the calendar NW t >
   1.645; mean net > 0 on the predictable-date subset. The other subsets are reported.
5. **Holm p** (S2): for X-L, the bootstrap p of (b); for X-R, the p of (a).
6. **Volume relative to the market** (S3).
   - X1 = the stock's log ratio minus the cross-sectional median log ratio of all Track B stocks
     over the same calendar W and B.
   - B excludes [a_prev−3, a_prev+10] for every earlier 2.02 day.
7. **FINRA** (S4).
   - X3 = ShortVolume / TotalVolume of FINRA-reported volume, not consolidated volume.
   - CNMS files do not exist before about 2018-08, so for every day the series is the sum of the
     NMS facility files (FNSQ, FNQC, FNYX, FNRA) that exist that day. The OTC file FORF is
     excluded. Level continuity against CNMS on overlap days is reported.
   - There is no free symbol-change history, so (symbol, date) pairs without FINRA rows are
     missing (median-imputed and flagged), and their count is reported.
   - Wholesalers' market-making shorts make the sign of X3 ambiguous, so no expected sign is
     claimed.
8. **Bars and session** (S5).
   - X6 runs from the 15:00 bar open to the official close (`prices`).
   - On NYSE early-close days, X6 uses 12:00 → close and X2 drops bars from 13:00 on.
   - X2 and X6 need bars on ≥ 8 of the 10 W days and ≥ 40 B days, else missing (counted).
   - Sector-SPDR bars are stored (checked 2026-10-11).
9. **Decile tables** (S6). Each of the 4 univariate tables is logged as a `reported_only`
   trial. Follow-ups need a new ADR.
10. **Power** (S7).
    - (b) MDE ≈ 0.12%/trade (about +1.3 pp weighted hit).
    - X-R MDE ≈ 0.3%/trade.
    - Hold-out MDE ≈ 0.35%. A dev effect near the (b) MDE cannot be confirmed on the hold-out;
      accepted.
11. **Nits.**
    - W, B and the new features exclude a−2.
    - X4 = X1·sign(X2) and X5 = X1·X3 are raw, then standardized; missing if an input is
      missing; sign(0) = 0.
    - (c) is the binding test (it implies (d)); both are kept.
    - X-R cut-offs are pooled over the training events; ties go to the inner side; events with
      no X2 are excluded and counted.
    - Peer earnings inside W are public information, not informed trading (stated).
