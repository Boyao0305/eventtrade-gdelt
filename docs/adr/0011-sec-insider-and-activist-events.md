# 0011 — SEC EDGAR events: insider purchases (Form 4) and activist stakes (13D) (hypothesis S1)

Date: 2026-10-10. Written before any SEC filing was downloaded or matched to prices. The user
asked for event signals other than GDELT and delegated the choice and order of sources. This is
the first: free, exactly dated, firm-level SEC filings with the strongest prior evidence.

## Prior evidence (why these and not others)

- **Insider purchases.** Open-market purchases by officers and directors are followed by
  positive abnormal returns in many studies. The effect is concentrated in non-routine,
  CEO/CFO and clustered purchases, and is stronger in smaller firms. Sales are weak signals.
- **Initial 13D filings.** A fund crossing 5% with intent to influence management is followed
  by positive abnormal returns around and after the filing.
- **Caveats.** Published anomalies weaken after publication. The universe here is S&P 500/400
  (current constituents), where the effects should be smaller than in small caps.

## Decision

### Data (free; user-agent identifies the user, as SEC requires)

**Form 4.** SEC "Insider Transactions Data Sets", quarterly 2015Q4 → 2025Q1.
- Documents: `DOCUMENT_TYPE = 4` only (no amendments).
- Transactions: non-derivative, `TRANS_CODE = P` (open-market or private purchase),
  `TRANS_ACQUIRED_DISP_CD = A`.
- Reporting owner's relationship includes Director or Officer.
- Value = Σ shares × price.

**13D.** EDGAR full-text search (`efts`), forms `SC 13D` and, after the December 2024 format
change, `SCHEDULE 13D`.
- Initial filings only; amendments `/A` are excluded.
- The subject company is the first listed CIK of the filing. If that CIK is not a universe
  company, the filing is dropped.

**Mapping.** Issuer CIK → universe ticker via SEC `company_tickers.json` (current tickers,
matching the current-constituent universe). Class shares are matched by CIK.

**Event date t** is the first NYSE trading day on or after the filing date. The position is
entered at the **t+1 open**.
- Form 4s are accepted until 22:00 ET with that day's date. 13Ds filed after 17:30 already
  carry the next day's date.
- So the entry never precedes publication, at the cost of up to a day's delay. No intraday
  timestamps are used.

### Events (four types, Track B stocks)

| Id | Event on day t for a stock | Sign |
|---|---|---|
| `ins_all` | Officer or director purchases filed on t worth ≥ $10,000 in total | + |
| `ins_cxo` | as `ins_all`, by an owner whose title matches CEO, Chief Executive, CFO, Chief Financial or President (case-insensitive) | + |
| `ins_cluster` | the filing on t brings to ≥ 3 the distinct officers or directors with a qualifying purchase (any size) filed in the 30 calendar days ending on t | + |
| `act_13d` | an initial 13D on the stock filed on t | + |

**Declustering:** per (stock, event type), no new event within 20 trading days of the last
accepted one, as in ADR 0005.

### Outcomes

- **Abnormal return:** the stock's open-to-open return from the t+1 open to the t+1+h open,
  minus its GICS sector SPDR's over the same window. SPY replaces XLC before 2018-06-19.
- **Prices:** stored daily `adj_open` (yfinance).
- **Horizons:** h ∈ {5, 20, 60} trading days.

**Cells:** 4 event types × 3 horizons = **12.**

### Inference (ADR 0005 machinery, no parameter search)

**Statistic:** the calendar-time portfolio (`eventstudy.calendar_time_series`): the mean and NW
t with horizon h (`cell_stat`).

**Discovery:** events dated 2016-01-04 → 2019-12-31, with outcomes embargoed to end by
2019-12-31.
- Test: one-sided placebo-date p (`eventstudy.placebo_p`, 2,000 draws, seed `config.seed`).
- A cell needs ≥ 30 events and ≥ 30 event dates.
- Benjamini–Hochberg at q = 0.10 across the 12 cells.

**Confirmation:** events dated 2020-01-02 → 2025-03-31, with outcomes ending before
2025-04-01. A discovered cell is confirmed when all three hold:
- one-sided p < 0.05 after Holm across discovered cells;
- mean > 0;
- mean net of costs > 0: round trip = 2 × 10 bp (stock) + 2 × 1 bp (sector ETF hedge) = 22 bp.

**Logging:** every cell × period is a `signal` trial, hypothesis `S1`, counted in DSR's N. A
second run is refused. Trials are logged only after everything is computed.

## Consequences

- +12 discovery trials (plus confirmations). No BigQuery cost.
- About 500 MB of SEC downloads, plus monthly `efts` queries.
- Confirmed cells go to gates 2–3 as a long-only, sector-hedged event portfolio, then the hold-out
  once. Backtests need the blocked scikit-learn DLL allowed first.

## Alternatives rejected

- *Insider sales:* weak and noisy in the literature.
- *13F holdings:* filed up to 45 days after quarter end, so stale.
- *Intraday entry using acceptance timestamps:* per-filing requests for little gain at h ≥ 5 days.
- *Small caps outside the S&P 1500:* no price data stored; that would be a separate study.

## Amendments after review (2026-10-10, before any filing was matched to prices)

An independent review found three blockers and eight should-fix items. All of them are adopted.

1. **Joint filings (blocker).**
   - Purchase value is summed per accession over its transaction rows, never per owner.
   - An accession qualifies when at least one of its owners' relationship includes Director or
     Officer.
   - For `ins_cluster`, all co-filers of one accession count as one owner.
   - Purchases with identical (issuer CIK, TRANS_DATE, TRANS_SHARES, TRANS_PRICEPERSHARE) in
     different accessions count once.
2. **Matched placebo (blocker).** The primary null draws each event's placebo date from the
   **same stock's** eligible dates in the **same stratum** of:
   - (a) trading days since the stock's last 10-Q or 10-K filing date, from the EDGAR
     submissions API: {0–10, 11–30, 31+};
   - (b) its 20-day sector-relative return before t, in terciles of that stock's discovery- or
     confirmation-period distribution.
   - Same draws, seed and p formula as `placebo_p` (2,000 draws).
   - The unmatched `placebo_p` is reported but does not decide.
   - Survivorship (current constituents) is reduced, not removed, by the matching; this is
     stated as a limitation.
3. **13D subject and type (blocker).**
   - The subject is taken from the filing header's SUBJECT COMPANY CIK (`-index-headers.html`).
     A filing is dropped if the subject is not a universe company.
   - It is also dropped if the subject filed an 8-K with Item 1.01 or 2.01, or a DEFM14A, in the
     30 calendar days before (deal-related 13Ds).
   - 50 random events (names only, no outcomes) are audited by hand before any outcome is
     computed.
4. **One ticker per CIK:** the universe ticker that `company_tickers.json` lists first. The
   match rate is reported per year. An event dated before the ticker's first stored price is
   dropped.
5. **Transaction hygiene** (all insider types):
   - TRANS_PRICEPERSHARE > 0;
   - filing date at most 5 business days after TRANS_DATE.
   - The proposed check of the price against the day's unadjusted low–high is **not** used:
     stored prices are split-adjusted and no split history is stored.
6. **`ins_cxo` titles:** word-boundary match of CEO, Chief Executive, CFO, Chief Financial or
   President, excluding titles containing "Vice" or "Division".
7. **`ins_cluster`** fires on the first day the 30-day count of distinct qualifying owners goes
   from below 3 to 3 or more. It cannot fire again until the count has fallen below 3, and the
   20-day declustering still applies.
8. **Costs:** round trip = 2 × 10 bp (stock) + 2 × 5 bp (sector ETF) = 30 bp, as in ADR 0005.
9. **Robustness:** a confirmed cell must also have mean > 0 when events dated 2020-02-20 →
   2020-04-30 are excluded (the March 2020 insider-buying wave).
10. **13D hits** are deduplicated by accession, and only dates before 2025-04-01 are queried.
    The February 2024 change of the 13D deadline (from 10 days to 5 business days) is reported
    as a split of the confirmation window, not a test.
