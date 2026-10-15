# 0016 — Value of foresight for earnings, macro releases, DoD awards and insider buys (hypothesis family U, look-ahead diagnostic)

Date: 2026-10-15. The user asked for this on 2026-10-15 ("pretend that we know the event in
advance and test the return"). Written before any of the quantities below was computed.

**Earlier work.**
- ADR 0009/0010 (experiments 0011/0012) asked this question for GDELT news.
- The later sources were tested only from the moment the event became public, in experiments
  0013–0017: SEC insider purchases, post-earnings drift, macro releases, DoD awards.

**Question.** How large is the price move that a trader with advance knowledge of each event
would capture? If even perfect foresight barely beats costs, no forecasting model can. If the
moves are large, the bottleneck is predicting the event.

**This is a deliberate look-ahead diagnostic.** Trials are logged with `params.lookahead = true`
and excluded from DSR's N (as for O, O2, R), and they are never backtested.

## Decision

Four families. Data, universes and event definitions are reused unchanged from the frozen
earlier studies. Two periods are reported: A = 2016-01-04 → 2019-12-31, B = 2020-01-02 →
2025-03-31. The hold-out is untouched.

**U-E: earnings (ADR 0013 events).**
- **Events:** every Track B earnings day d (8-K 2.02, deduplicated and deal-filtered, coverage
  rule), not only |z| ≥ 2.
- **Reaction:** R = sector-hedged close(d−2) → close(d+1), the ADR 0013 window (timing check:
  about 23% of the reaction comes before d).
- **Oracle:** knows sign(R) at close(d−2), so its gross gain is |R|.
- **Placebo:** |R| of the same stock's 3-day windows starting on days not within ±5 trading days
  of any 2.02 day, in the same calendar year.
- **Cost:** 30 bp round trip, as in ADR 0013.

**U-M: macro releases (ADR 0014 calendar and SPY bars).**
- **U-M1, CPI/NFP days** (no FOMC overlap): the SPY gap open(d)/close(d−1) − 1. The oracle knows
  its sign at close(d−1), so its gain is |gap|.
- **U-M2, scheduled FOMC days:** |close(d)/p(d, 13:45) − 1|. The oracle knows the direction of
  the statement-to-close move at 13:45.
- **Placebo:** the same per-day quantity on non-announcement days of the same year and weekday,
  drawn by label permutation including the event days (ADR 0014 implementation note). 10,000
  draws, `config.seed`.
- **Cost:** 2 bp round trip.

**U-D: DoD awards (ADR 0015 events, frozen map, exclusions and rules).**
- **Events:** the `material` and `moderate` issuer-days, tradable and with a market cap.
- **Overnight value:** the hedged close(d) → open(d+1) return, signed + because an award is
  good news. This is the value of knowing the award before the 17:00 release, i.e. buying at
  close(d). No oracle on the sign: the sign is the award's.
- **Placebo:** the same issuer's overnight returns on all tradable days of the same year,
  events included (expected value of the pool means).
- **Cost:** ADR 0015 round trip (Track B 22 bp; small caps tiered).

**U-I: insider purchases (ADR 0011 qualifying Form 4 purchases, Track B).**
- **Events:** `ins_all` and `ins_cxo`.
- **Trade-to-filing value:** the hedged return from close(trade date) to open(first trading day
  after the filing date), the ADR 0011 entry. Trade date = the earliest `trans_date` of the
  accession's qualifying purchases. Accessions with trade date > filing date, or a gap > 10
  calendar days, are dropped and counted. This is the value of seeing the purchase on the day
  it happens; signed +.
- **Placebo:** the same stock's returns over windows of the same number of trading days
  starting on any tradable day of the same year.
- **Cost:** 22 bp.

**Cells (10):**

| Cell | Quantity |
|---|---|
| U-E | oracle \|R\| |
| U-M1 | oracle \|gap\| |
| U-M2 | oracle \|post-statement move\| |
| U-D material | signed overnight |
| U-D moderate | signed overnight |
| U-I all | signed trade → filing |
| U-I cxo | signed trade → filing |
| U-E with lead | oracle \|R\| but entry at close(d−5) (5-day lead), to show whether earlier knowledge adds |
| U-M1 with lead | oracle \|close(d−1)/close(d−2) − 1 + gap\| (1-day lead) |
| U-D with lead | signed hedged close(d−1) → open(d+1) |

**Statistics per cell × period.**
- **Event mean:** NW t on the calendar-time series, or the plain mean for single-asset U-M.
- **95% CI** of the event mean minus placebo mean.
- **Placebo p** (one-sided, event > placebo). For oracle cells this compares |R| on event and
  ordinary days.
- **Net = event mean − cost.** For oracle cells this is the perfect-foresight profit per trade.
- **Break-even hit rate** for the oracle cells: q* = (1 + cost/E|R|) / 2. A forecaster of the
  sign must be right more than q* of the time to break even.

**Reading (fixed now).**
- **"Foresight value"** requires all of: event mean > placebo mean (placebo p ≤ 0.05) in both
  periods, and the lower 95% bound of the net > 0 in both periods.
- **"No foresight value":** the upper 95% bound of (event − placebo) is below the cost in either
  period.
- Otherwise **"inconclusive"**.
- For oracle cells the headline is q*. A q* above 0.60 means a forecaster needs better than 60%
  sign accuracy, which the literature on earnings and macro surprises rarely reports out of
  sample.
- No multiplicity correction: this is a diagnostic. All 10 cells × 2 periods are reported.

**Logging.** 20 trials (hypothesis U, `lookahead = true`), once only, after everything is
computed.

## Consequences

- No new data and no cost.
- If U-D or U-I show value, the next question is the leak or anticipation channel. Any trade on
  that needs its own non-look-ahead ADR.
- Results cannot reopen the earlier ADRs' verdicts.

## Amendments after review (2026-10-15, before any quantity was computed)

All items are adopted. Where they conflict with the Decision above, they win.

1. **Oracle cells (U-E, U-M1, U-M2) get no verdict.**
   - A perfect sign oracle beats costs on any day, and event days are known to move more. Both
     facts are predictable, so they are stated here so they do not read as findings later.
   - **Reported:**
     - E|R| for events and placebo, and their ratio;
     - Δ = E|R|event − E|R|placebo, with a block-bootstrap CI;
     - the break-even hit rate q* for events and placebo, computed with E|R|, median |R| and
       the 1%/99%-winsorized mean.
   - **q\*** = (1 + c/E|R|)/2 is the |R|-weighted break-even accuracy: Σ|R_i|·1[correct] / Σ|R_i|
     must exceed q*. A forecaster that is right mostly on small moves needs more.
   - Survivorship (current constituents) biases event |R| down.
2. **Lead cells are the earlier segment alone, signed by the coming surprise.**
   - U-E lead: sign(R)·R(close(d−5) → close(d−2)).
   - U-M1 lead: sign(gap)·r(close(d−2) → close(d−1)).
   - U-D lead: close(d−1) → close(d).
   - Placebos apply the same segment and sign rule to placebo windows.
3. **Verdict rule for signed cells** (U-D, U-D lead, U-I(a), U-E lead, U-M1 lead):
   - Per event, x_i = value_i − mean of that event's own matched placebo pool. D = mean(x) − cost.
   - CI: block bootstrap by calendar month, 10,000 draws, `config.seed`.
   - **Value:** D's lower 95% bound > 0 in both periods.
   - **No value:** D's upper 95% bound < 0 in either period.
   - Otherwise inconclusive.
   - This uses event averages, not calendar-time series.
4. **U-I**, cost 30 bp (ADR 0011 amendment 8).
   - **Trade date:** the earliest qualifying trans_date across the accessions of the
     (declustered) event. A non-trading date maps to the last close on or before it.
   - **(a) Foresight segment:** close(trade) → close(filing day − 1), where filing day is the
     ADR 0011 event day. Events with a zero-length segment are dropped and counted.
   - **(b) Filing-day segment:** close(filing − 1) → open(filing + 1), reported only.
   - **Primary placebo:** the ADR 0011 amendment 2 matched strata at the start day, windows of
     equal length. The unmatched same-year pool is reported.
   - **Also reported:** a value-weighted variant (each tranche from its own trans_date), and the
     number of trades ADR 0011's 5-business-day filing rule dropped, by gap. Late filers are
     excluded (stated).
5. **U-E placebo.** Ordinary windows apply the ADR 0013 coverage rule to placebo tickers too.
   Reported per year: the share of events whose largest 1-day move in [d−5, d+1] lies in
   [d−5, d−3].
6. **U-D is labelled "pre-release value (16:00 → next open)".**
   - It uses the h = 1 declustered events. If d is not a trading day, close(d) = the last close
     before d.
   - Also reported: the ±1-day award-excluded pool (ADR 0015 amendment 9).
   - The after-hours move is partly tradable after 17:00, so this is an upper bound.
7. **Nits.**
   - U-M placebo days include other 08:30 releases (PPI, retail sales, claims), which pushes Δ
     toward 0.
   - U-M2 inherits ADR 0014's overlap, cancellation and early-close drops.
   - The lead return compounds: (1 + r1)(1 + gap) − 1, where used.
   - The claim that accuracy above 60% is rare is dropped.
