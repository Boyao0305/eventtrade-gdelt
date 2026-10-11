# 0012 — Replicate insider purchases on fresh stocks (outside the S&P 500/400) (hypothesis S2)

Date: 2026-10-10. Written after experiment 0013 and before any price outside the S&P 500/400 was
downloaded or any event on those stocks was computed.

## Context

In S&P 500/400 stocks, officer and director open-market purchases were followed by positive
sector-hedged returns at 20 and 60 days in both periods (experiment 0013). The effect was net of
costs, but the smallest confirmation p (0.009) missed Holm (0.0071).

The project's rules forbid re-testing the same data with new settings. A clean test needs data
the study never used. Stocks **outside** the S&P 500/400 are such data: the insider files cover
them, and no price of theirs has ever been loaded. The literature also expects the effect to be
**stronger** in smaller firms.

## Decision

### Universe (fixed before any price is read)

- **Issuers:** issuer CIKs in the stored insider data sets (ADR 0011) whose current ticker
  (`company_tickers_exchange.json`) trades on **NYSE, Nasdaq or NYSE American**.
  - Excluded: CIKs mapped to the 900 Track B stocks, and tickers with a `-`, `.` or `^`
    (preferreds, units, warrants and other share classes).
  - One ticker per CIK: the first listed.
- **Prices:** yfinance daily `adj_open` / `adj_close` / volume, 2015-10-01 → 2025-03-31, in a
  separate store `data/smallcap.sqlite`. Tickers without data are dropped and counted.
- **Hedge:** **IWM** (Russell 2000 ETF) for every stock.
- **Tradability at event day t**, using data up to t only:
  - close ≥ $5;
  - median daily dollar volume (close × volume) over the 60 trading days ending at t ≥ $1M.
  - Events failing either rule are dropped. The same rule screens placebo dates.

### Events, outcomes, inference: as ADR 0011 with its amendments, except

- **Cells:** `ins_all`, `ins_cxo`, `ins_cluster`, all at **h = 60**, with sign +. These are the
  three cells with the largest confirmation effects in experiment 0013. That choice is disclosed.
  **3 cells.**
- **Period:** a single test on events dated 2016-01-04 → 2025-03-31, with outcomes ending by
  2025-03-31. The stocks are fresh, so there is no discovery/confirmation split.
- **Outcome:** stock open-to-open return from t+1 to t+61, minus IWM's.
- **Matched placebo (ADR 0011 amendment 2):**
  - Earnings proximity comes from the EDGAR submissions of each issuer (10-Q/10-K filing dates).
  - The 20-day pre-return is IWM-relative.
  - Terciles are taken within the stock.
  - Placebo dates must also pass the tradability rule.
  - 2,000 draws, seed `config.seed`.
- **Pass (each cell):**
  - Holm across the 3 cells at α = 0.05, one-sided;
  - mean > 0;
  - mean net of costs > 0, with round trip = 2 × 25 bp (small/mid caps) + 2 × 1 bp (IWM) = 52 bp;
  - mean > 0 with events dated 2020-02-20 → 2020-04-30 excluded.
- **Logging:** every cell is a `signal` trial (hypothesis `S2`), counted in DSR's N. Once only,
  logged only after everything is computed.

### Reading the result (fixed now)

- **Two or more cells pass:** the insider effect is treated as **replicated**. The project moves
  to gates 2–3 with an insider-purchase portfolio, on the S&P 1500 and fresh-stock sets together,
  then the hold-out once.
- **None pass:** experiment 0013's result is treated as fragile, and the insider line closes.
- **One cell passes:** reported as partial, with no gate-2 work without a new ADR.

## Known biases (stated in advance)

- **Survivorship.** Only firms listed **today** are included. Small firms that delisted after
  insiders bought are missing, so the sample is tilted toward survivors and returns are biased
  **upward**.
  - The matched placebo draws from the same stock, which is also a survivor, so it removes the
    part of the bias that is common to all of that stock's dates.
  - It cannot remove event-specific survivorship: insiders buying just before a collapse that
    led to delisting.
  - This is the main caveat of a pass.
- **The IWM hedge is beta-1:** small caps differ in beta and sector.
- **Costs** are approximate; small caps can be wider.

## Alternatives rejected

- *Re-testing S&P 1500 stocks with new settings:* same data, forbidden.
- *Using the hold-out now:* reserved for strategies that pass gates 1–3.
- *Including OTC or penny stocks:* untradable at these costs.

## Amendments after review (2026-10-10, before any price was analysed)

An independent review found four blockers. They are adopted, together with all should-fix items.
Prices were being downloaded during the review (data collection only); no return or event
outcome had been computed.

1. **Survivorship count and stress test (blocker).**
   - From SEC data only, before any price is read: count the qualifying events (2016–2025Q1)
     whose issuer CIK has no current listed ticker.
   - Classify each such issuer's exit from its EDGAR submissions:
     - *failure*: 8-K Item 1.03, or Form 25/15 without a merger filing;
     - *merger*: DEFM14A, 8-K Item 2.01 or S-4 within a year before delisting;
     - *other*.
   - Stressed mean = (1 − f_fail) × observed mean + f_fail × (−30%), where f_fail is the
     failure share of all qualifying events in the cell and merger exits count as 0.
   - "Replicated" requires a stressed mean > 0 in every passing cell (see 8).
2. **Raw price for the $5 rule (blocker).**
   - Raw close = yfinance close × the product of split ratios **after** t (from
     `Ticker.splits`).
   - Dollar volume uses split-neutral close × volume.
   - Placebo dates use the same rule.
3. **Who the issuer was at t (blocker).**
   - Drop events before the CIK's first 8-A12B filing. If no 8-A12B is on file, require a
     10-Q/10-K in the 180 days before t.
   - Drop issuers with SIC 6770 (blank-check companies), and dates before a name change away
     from a name containing "Acquisition".
   - Drop issuers with no 10-Q/10-K at all.
4. **Ticker identity per stock (blocker).**
   - For every Form 4 purchase, compare the reported price with the raw yfinance close on the
     transaction date. A mismatch is a ratio outside [0.67, 1.5].
   - Drop a stock (events and placebo dates alike) if more than 20% of its transactions
     mismatch.
   - Report the number of stocks dropped.
5. **Distress matching (should-fix).** A third placebo stratum is the tercile, within the stock,
   of its 250-day IWM-relative return. The 2-dimension match is reported as a secondary result.
6. **Tiered costs:**
   - round trip = 2 × 50 bp if median dollar volume at t is below $5M, else 2 × 25 bp, plus
     2 × 1 bp for IWM;
   - a 2 × 75 bp stress is reported.
7. **Price-error screen** (all dates alike):
   - an open on a zero-volume day counts as missing;
   - a day with an open-to-open return above +300% or below −75% that reverses by at least 2/3
     within 5 days is an error;
   - the exit is the first valid open on or after t+1+h, with these cases counted;
   - there is no event without a t+1 open;
   - a winsorized (1%/99%) mean is reported as sensitivity only.
8. **Decision rule:**
   - "Replicated" requires `ins_all` to pass (it is the superset of the other two cells), plus at
     least one other cell, plus amendment 1 for each passing cell.
   - Otherwise the result is "partial" (if any cell passes) or "not replicated".
   - Gate-2 portfolio specifications come in a new ADR before any gate-2 run.
9. **Notes:**
   - The cell choice used the confirmation effect sizes of experiment 0013. p-values stay valid;
     the expected size is inflated (winner's curse).
   - The calendar years overlap experiment 0013, so this is not a fully independent test.
   - Universe membership uses 2026 information.
   - The terciles use the whole period, for events and placebo alike.
   - The price downloads keep their fetch date.
   - SEC folds NYSE American into "NYSE": the exchanges kept are NYSE and Nasdaq.

## Implementation notes (2026-10-10, before any return was computed)

**Data**
- 2,815 listed issuers (NYSE or Nasdaq, not Track B) with qualifying purchases. Prices for 2,792
  of them; 23 have no Yahoo data.
- 5,784 EDGAR profiles (2,815 listed + 2,969 delisted), 3,651 yfinance split rows.
- **Survivorship is material.** Issuers no longer listed hold about 39% of the qualifying
  accessions outside Track B (more than half in 2016–2018), so the amendment 1 stress test can
  decide the verdict.

**Metadata dated after 2025-03-31 is used.** Two kinds, neither of them a return:
- split dates, because yfinance's close is adjusted for every later split and amendment 2 must
  undo that;
- 8-A12B registrations, because a first registration after the window means the issuer was not
  exchange-listed during it.

**Matching**
- The 250-day distress stratum needs 250 days of price history, so events in a stock's first
  year of prices cannot be matched and drop out (recent IPOs).
- Placebo dates of a stock may fall inside the outcome windows of its other events. That biases
  the matched test toward the null (conservative). A synthetic test with dense events shows it.

**Scope:** universe, delisted list and splits are stored as snapshot tables in
`data/smallcap.sqlite` (`small_universe`, `delisted`, `splits`).
