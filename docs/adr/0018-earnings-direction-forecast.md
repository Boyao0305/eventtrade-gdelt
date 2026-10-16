# 0018 — Forecasting the direction of the earnings reaction from free data (hypothesis family W)

Date: 2026-10-16. The user chose this direction (research-backlog direction A, 2026-10-16).
Written before any feature was computed or matched to returns.

## Why

- **Experiment 0018:** the hedged earnings reaction R (close(d−2) → close(d+1)) averages 4.4–4.8%,
  2–3× an ordinary 4-day move. A sign forecaster breaks even at about 53–55% |R|-weighted
  accuracy against a 30 bp round trip.
- **Experiment 0019:** the jump is permanent, with no reversal.
- **The question:** can information public at close(d−2) call sign(R) well enough to beat
  costs?

## Decision

**Events.**
- ADR 0013 earnings days d (Track B, 8-K item 2.02, deduplicated and deal-filtered, coverage
  rule).
- Target y = sign(R), where R = sector-hedged close(d−2) → close(d+1).
- The trade is decided at close(d−2): long if the predicted sign is +, short if −; exit at
  close(d+1).
- Cost: 30 bp round trip, plus ADR 0013 borrow (50 bp/yr pro rata) on shorts.

**The date is assumed known by d−2.**
- Large caps schedule earnings weeks ahead, but our d comes from the 8-K, which is a look-ahead
  on the exact date.
- **Robustness (reported, and also required in confirmation):** the subset of "predictable
  dates", where d is within ±5 trading days of the stock's same-quarter date one year earlier
  (d − 252 ± 5).

**Features** (all known at close(d−2); no prices or filings after that time):

| Group | Features |
|---|---|
| Past reactions | sign and z of the stock's previous 1–4 earnings reactions (ADR 0013 z), and their mean |
| Price | sector-hedged returns over [d−7, d−2], [d−22, d−2], [d−63, d−2], [d−252, d−2]; 60-day realized σ |
| Fundamentals (SEC XBRL, filed < d−2) | year-on-year growth of quarterly diluted EPS and of revenue in the last reported quarter; sign of the last EPS change; the "earnings momentum" of the last two quarters |
| Insiders | count and value of ADR 0011 qualifying purchases in [d−92, d−3] |
| Timing | d minus the same-quarter date a year earlier, in trading days (late reporting) |
| Sector | sector one-hot (the 11 SPDR groups) |

- **XBRL:** `us-gaap:EarningsPerShareDiluted` (fallback Basic) and revenue
  (`Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax` → `SalesRevenueNet`).
  Quarterly values are derived from 10-Q quarters and 10-K annual minus 9-month values, using
  only facts with filed < d−2.
- **Missing values:** imputed with the training-set median, plus a missing flag.
- **Scaling:** standardized on the training set.

**Models** (fixed now, with no tuning on any test year):
1. **W-L:** L2 logistic regression, C = 1.0, class weight balanced, sample weight |R| (we care
   about |R|-weighted accuracy).
2. **W-G:** gradient-boosted trees (scikit-learn `HistGradientBoostingClassifier`,
   max_depth 3, learning_rate 0.05, 200 iterations, min_samples_leaf 200, sample weight |R|).
3. **Rule baselines** (no fitting):
   - **W-R1:** the sign of the previous reaction;
   - **W-R2:** the sign of the [d−63, d−2] return;
   - **W-R3:** the majority vote of the signs of the last 4 reactions.

**Walk-forward.**
- Models are trained on all events with d in [2016-01-04, Y−1 end] and predict year Y. They
  are retrained every calendar year, with no event's outcome used before it is known (training
  events must have d+1 < the first prediction date).
- **Discovery:** Y ∈ {2018, 2019}, trained from 2016.
- **Confirmation:** Y ∈ {2020, …, 2025-Q1}, the same procedure.

**Statistics per model × period.**
- Hit rate, |R|-weighted hit rate (vs the break-even q*), and mean net P&L per trade
  (sgn_pred·R − cost − borrow).
- A quarter-block bootstrap 95% CI of the mean net P&L (10,000 draws, `config.seed`).
- A calendar-time portfolio of the event trades, with its annualized mean and Sharpe.

**Selection and test.**
- **Discovery:** a model is "discovered" if its mean net P&L's lower 95% bound is > 0 in
  2018–2019. The five models are corrected with Holm at 5% using bootstrap p-values (one-sided).
- **Selection:** among discovered models, the one with the highest discovery mean net P&L.
- **Confirmation** (the selected model only, once), all required:
  - mean net P&L > 0 with the lower 95% bound > 0 in 2020–2025;
  - |R|-weighted hit rate > q* (from the period's mean |R| and the cost);
  - the same with 2020-02-20 → 2020-04-30 excluded;
  - mean net P&L > 0 on the predictable-date subset.
- **If confirmed:** gate 2 (a portfolio backtest with position limits, DSR counting all
  trials), then the hold-out once, under a new ADR.

**Logging.**
- Every model × period is a `signal` trial (track B, hypothesis W). Once only; logged after
  everything is computed.
- The DSR N includes all trials, plus the ADR 0017 note (17 × 8 combinations), because this
  direction was suggested by ADRs 0016/0017.

## Consequences

- New data: SEC XBRL company concepts for about 900 Track B CIKs (≈ 3 concepts each, free,
  SEC User-Agent rules).
- Survivorship: current constituents.
- The literature reports out-of-sample sign accuracy for earnings surprises around 55–60% with
  analyst data. Without it, a weak signal is the likely outcome.

## Alternatives rejected

- *Deep models, or feature or hyperparameter search:* overfitting risk with about 3,000
  events/yr. Two fixed models plus three rules.
- *Analyst consensus:* paid (backlog direction B).

## Amendments after review (2026-10-16, before any feature or outcome was computed)

All items are adopted. Where they conflict with the Decision above, they win.

1. **One primary test, pooled out-of-sample** (B1; the reviewer's alternative).
   - **Power:** a two-year discovery window can detect only about 56–58% weighted accuracy;
     the pooled test detects about 12–17 bp of net P&L.
   - **Primary model W-L**, tested once on all walk-forward out-of-sample predictions,
     2018-01-01 → 2025-03-31.
   - W-G, W-R0…W-R3 are reported only, but are computed, logged and counted as trials.
   - **All of these are required:**
     a. mean net P&L > 0, with the one-sided 95% lower bound > 0 (week-block bootstrap,
        10,000 draws, `config.seed`), both raw and with R winsorized at 1%/99%;
     b. the calendar-time portfolio NW t > 1.645;
     c. the paired difference W-L − W-R0 in mean net P&L, with one-sided 95% lower bound > 0;
     d. mean net > 0 on W-L's short predictions alone;
     e. mean net > 0 in both 2018–2019 and 2020–2025-Q1;
     f. mean net > 0 on each subset: predictable dates; no 1-day hedged |z| ≥ 2 in [d−4, d−2];
        W-L refitted without the timing feature; without the deal filter; with 2020-02-20 →
        2020-04-30 excluded. Each subset's n and CI are reported.
2. **Blocks are calendar weeks of the decision date** (B2). Quarter blocks are reported only.
3. **Baseline W-R0 = always long** (B3). It counts as a trial.
4. **XBRL point in time** (B4).
   - Facts are selected by start/end dates, never by fp, fy or frame. A quarter is 80–100 days.
   - The latest 10-Q/10-K filed ≤ d−3 is used, with that filing's own current and comparative
     periods, so splits, tag changes and restatements known by then are handled.
   - The revenue tag fallback applies per filing.
   - Q4 revenue and net income = annual − 9 months. Q4 EPS = Q4 net income / Q4 diluted shares,
     else missing.
   - A last quarter that ended more than 135 days before d is set to missing.
   - The pull adds `NetIncomeLoss` and `WeightedAverageNumberOfDilutedSharesOutstanding`, and
     keeps only filings dated before the hold-out.
5. **Year and label rules** (B5).
   - An event belongs to the year of close(d−2). Events with d+1 ≥ 2025-04-01 are dropped.
   - Training events need d+1 strictly before the first decision date of year Y.
6. **Model settings** (S1, S2).
   - W-G: `early_stopping=False`, `random_state=config.seed`.
   - No class weights. Sample weights = |R| clipped at the training 99th percentile, rescaled
     to mean 1.
   - Probabilities are not used for sizing. y = 1[R > 0]; events with R = 0 or NaN are dropped
     and counted.
7. **Price errors** (S3).
   - Closes are screened like `small_insider.clean_opens` (reverting spikes are set to NaN).
   - R is winsorized at 1%/99% for features and weights.
   - P&L is reported raw and winsorized (1a).
8. **Filters using information after the decision** (S4).
   - The coverage rule is evaluated on the calendar year before Y.
   - Results are also reported without the deal filter (1f).
9. **Features defined** (S6).
   - EPS growth = (EPS_q − EPS_{q−4}) / close(d−2).
   - SUE_q = (EPS_q − EPS_{q−4}) / sd of the last 8 seasonal differences (at least 6), plus
     SUE_{q−1} … SUE_{q−3}. The "earnings momentum" feature is replaced by these.
   - Revenue growth = (Rev_q − Rev_{q−4}) / |Rev_{q−4}|, clipped to ±1.
   - Past reactions use the ADR 0013 amendment-2 window, with d_prev + 1 ≤ d−3.
   - Insider buys are counted by Form 4 filing date ≤ d−3.
10. **Implementation** (S7). New pure functions:
    - pooled standardization fitted on the training set;
    - weighted logistic regression;
    - a yearly walk-forward splitter that applies rule 5, with a test.
    `models.fit_logistic` (per-date standardization) is not reused.
11. **DSR accounting** (S8). This direction was suggested by ADRs 0016 and 0017, so N at gate 2
    adds 20 (the U cells, excluded as look-ahead) and 136 (the V cells × horizons) as an
    explicit `n_extra`.
12. **Nits adopted.**
    - q_w is reported only; it is redundant with mean net before borrow.
    - Bounds are one-sided 95%.
    - Sectors are today's (GICS changed in 2018), stated.
    - Borrow is ≈ 0.6 bp per trade; hard-to-borrow names before earnings are not modelled,
      stated.
    - Break-even q* ≈ 0.533 at E|R| ≈ 4.6% (0.55 with median |R|).
    - The prior includes Bernard–Thomas (SUE autocorrelation) and the earnings-announcement
      premium (Frazzini–Lamont, Savor–Wilson), hence W-R0.
