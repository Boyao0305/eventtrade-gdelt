# 0013 — Post-earnings announcement drift with a price-reaction surprise (hypothesis P1)

Date: 2026-10-11. Written before any earnings date was matched to prices. The user delegated the
choice and order of event sources. This is the second non-GDELT source, after the SEC insider
studies (experiments 0013, 0014).

## Prior evidence

Post-earnings announcement drift (PEAD) is one of the most replicated anomalies: prices keep
drifting in the direction of the earnings surprise for weeks. The surprise is classically
measured against analyst consensus (paid data). A free alternative is the announcement-window
abnormal return, which also predicts drift in the literature. The effect has weakened in large
caps since the 2000s.

## Decision

**Universe and data.**
- Track B (current S&P 500/400) with stored daily prices, and the sector SPDR hedge (SPY for XLC
  before 2018-06-19), as in ADR 0011.
- **Earnings dates:** 8-K filings (form `8-K`, not `8-K/A`) whose items include **2.02** in
  `company_filings` (EDGAR submissions).
- **Announcement day d:** the first trading day on or after the filing date. Several 2.02 8-Ks
  within 5 trading days count once (the first).
- **Limitation:** holding-company CIK changes lose earlier filings (ADR 0011 notes).

**Surprise (no look-ahead).**
- Reaction r = sector-hedged close-to-close return from close(d−1) to close(d+1). That window
  covers both before-open and after-close announcements.
- z = r / σ, where σ is the standard deviation of the stock's overlapping 3-day hedged
  close-to-close returns over the 250 trading days ending at d−2.
  - At least 150 values are needed.
  - Days within ±2 trading days of an earnings day are excluded from σ.

**Events (decided at the close of d+1; entry at the d+2 open).**
- `pos`: z ≥ +2. Sign **+** (drift up).
- `neg`: z ≤ −2. Sign **−** (drift down).
- The event date passed to the ADR 0005/0011 machinery is t = d+1, with entry at the t+1 open.

**Outcome:** sector-hedged open-to-open return from t+1 to t+1+h, with **h ∈ {5, 20, 60}**.
**6 cells** (2 signs × 3 horizons).

**Inference** (ADR 0011 machinery):
- **Statistic:** the calendar-time portfolio mean and NW t (horizon h).
- **Primary null:** placebo dates of the same stock (`eventstudy.placebo_p`, 2,000 draws, seed
  `config.seed`), one-sided in the cell's sign.
- **Secondary null, reported only:** the matched placebo restricted to the same stock's
  **non-earnings** days with a same-sign 3-day move of |z| ≥ 2. Days within ±5 trading days of an
  earnings day are excluded. This asks whether earnings moves drift more than other large moves.
- **Discovery** 2016-01-04 → 2019-12-31 (outcomes inside): BH q = 0.10 across the 6 cells.
- **Confirmation** 2020-01-02 → 2025-03-31: discovered cells, all four required:
  - Holm 0.05, one-sided;
  - mean in the registered direction;
  - net of a 30 bp round trip (2 × 10 bp stock + 2 × 5 bp ETF);
  - same direction with events dated 2020-02-20 → 2020-04-30 excluded.
- **Logging:** every cell × period is a `signal` trial (hypothesis `P1`). Once only; logged after
  everything is computed.

**Reading.**
- **Confirmed cells:** gate 2 needs a new ADR fixing the portfolio, long `pos` and short `neg`
  with sector hedges.
- **Short side:** borrow cost (`config.borrow_bps_annual`) is deducted from the `neg` cells' net
  mean in confirmation: 50 bp per year, pro rata over h.

## Consequences

- +6 discovery trials (plus confirmations). No new data or cost.
- **Survivorship:** current constituents. Firms that collapsed after bad earnings are missing,
  which biases the `neg` drift toward zero and the `pos` drift up. Stated as a limitation.

## Alternatives rejected

- *Analyst-consensus SUE:* paid data.
- *An EPS time-series surprise from XBRL:* feasible later, but it adds parsing and a lag (10-Q
  filed weeks after the 8-K).
- *A threshold grid:* no search; one threshold (|z| ≥ 2).

## Amendments after review (2026-10-11, before any outcome was computed)

`core/earnings.py` existed (functions and synthetic tests only); no earnings day had been
matched to returns. All review items are adopted.

1. **Primary null (blocker): matched to non-surprise earnings.**
   - Each event's placebo date is drawn from the **same stock's** earnings decision days (d+1)
     in the period with |z| < 2, using the same draws, seed and p formula as `matched_placebo`.
   - The unmatched `placebo_p` is reported only.
2. **Timing check, fixed before outcomes.**
   - Per year, report the share of earnings days whose largest absolute 1-day hedged return in
     [d−4, d+1] falls before d.
   - If that share exceeds 10% in discovery, the reaction window becomes close(d−2) → close(d+1),
     and σ uses the same 4-day window.
3. **Coverage:** a ticker is excluded from a period if any full calendar year of that period
   has fewer than 3 Item 2.02 days. The count is reported.
4. **Survivorship guard for `pos`:** a `pos` cell is confirmed only if, in addition, either:
   - its secondary matched p ≤ 0.10, or
   - its mean keeps its sign when tickers whose first stored price is after 2015-03-02 are
     excluded.
5. **Secondary null:**
   - Big non-earnings moves are declustered to the first qualifying centre day per 10 trading
     days.
   - Centres within [d−5, d+20] of any earnings day are excluded.
6. **Data details:**
   - Prices are read from 2015-03-02.
   - 2.02 8-Ks that also carry Item 1.01 or 2.01 are excluded (deal news).
   - The σ exclusion uses **all** Item 2.02 days.
   - Pre-announcement 2.02s count as separate earnings days.
   - Reported only: the fraction of earnings days with |z| ≥ 2, `n_matched`, a winsorized
     (1%/99%) event mean, and events dropped by the outcome embargo or XLRE availability.
7. **Net mean:** the cell mean minus 30 bp; for `neg`, also minus borrow at 50 bp/year × h/252.
   Hard-to-borrow costs after bad news are not modelled, and this is stated.
