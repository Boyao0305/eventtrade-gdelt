# 0009 — Value of foresight: how much would knowing an event earlier be worth? (hypotheses O*)

Date: 2026-10-09. Written together with ADR 0008, before any new data was pulled or computed. It
is independent of ADR 0008's results: the events here use fixed definitions, not the ADR 0008
selections.

## Context

Every tradable test so far is null. One explanation is **delay**: prices may react before GDELT
publishes, as hinted by experiment 0009's pre-move diagnostic (t −2.2 for EM attention). If
that is the problem, a better route is to **predict the arrival** of an event type from
alternative data (proxy signals) rather than to trade on GDELT itself. This study measures the
upper bound of that route. It asks how large the price move is between the time a perfectly
informed trader could have acted (Δ before availability) and the time GDELT makes the event
available.

**This is a deliberately look-ahead diagnostic, not a strategy.** No result here can be traded
as is. A positive result only justifies a separate, pre-registered event-arrival prediction
project.

## Decision

### Events (fixed definitions, no search)

ADR 0007 rules at its registered configuration: L = 15, W = 60 min, θ = 3. Signals:
- Track A: action-geo families quad1–quad4, r14, r17, r18 + r19 + r20 (violence), b163, low
  tone, attention, for dm and em; producer quad4 and producer violence for commodity. Counts use
  the ADR 0008 `events_15min_cat` pull.
- Track B: negative and positive company spikes.

### Foresight returns

For an event available at decision time τ on day t, the **foresight return at lead Δ** is the
same abnormal (or, for commodity, raw) return as ADR 0007, measured **from τ − Δ to τ**. It is
the move a trader who knew Δ earlier would have captured before the information became public.

**Intraday Δ ∈ {15, 30, 60, 120 min}.**
- Start price: the close of bar [τ−Δ−15, τ−Δ).
- When τ − Δ is before 09:45, the start is the session's first open.
- Events whose start bar is missing for the asset or hedge are dropped for that Δ.

**Daily Δ ∈ {1, 2, 5 trading days}.**
- Start price: the close of the last regular bar of day t − Δ.
- Bars come from the same Alpaca adjusted series, so splits and dividends are consistent.

### Cells and signs

Each (signal, group) × Δ is a cell. Its sign is the signal's registered sign: + for
cooperation and producer cells, − for conflict, protest, sanctions, violence and low tone, and
two-sided for attention.

That gives 22 Track A and 2 Track B (signal, group) pairs × 7 Δ = **168 cells**.

### Inference

- **Statistic and null:** the ADR 0007 statistic (daily series, NW t with 5 lags) and the ADR 0007
  placebo-day null (2,000 draws, ±63-day local draws). The placebo uses the same Δ window at the
  placebo day.
- **Discovery 2016–2019:** BH q = 0.10 across the 168 cells.
- **Confirmation 2020–2025Q1:** discovered cells, Holm at 0.05, same sign.
- Every cell × period is logged as a trial (hypotheses O1…), stage `signal`, with
  `params.lookahead = true`. These trials can never be passed to a backtest or the hold-out.

### Interpretation, fixed in advance

**Reverse causality.** News is often *caused by* price moves: a stock jumps and is then
mentioned, or markets fall and tone darkens. A pre-event move in such categories means GDELT
follows prices, not that the news is predictable.

The categories are therefore classified now:
- **Plausibly exogenous at intraday-to-days horizons:** r14 protest, r17 coerce, violence,
  quad4 material conflict, producer violence and producer quad4.
- **Plausibly endogenous:** attention, tone, quad1–quad3, b163 sanctions (policy responds to
  markets) and the Track B company spikes.

**Conclusions.**
- A confirmed foresight effect in an **exogenous** category, larger than costs, supports a
  follow-up ADR. That ADR would predict the arrival of that event type from alternative data
  (e.g. ACLED, social media, flight or shipping data, Google Trends) and trade the forecast.
- A confirmed effect only in **endogenous** categories means GDELT lags prices. Predicting
  those events amounts to predicting prices, so no event-arrival project follows.
- Nothing confirmed: even perfect foresight of these events within 5 days carries no tradable
  information.

## Consequences

- +168 discovery trials (plus confirmations), all flagged look-ahead.
- No new data beyond ADR 0008's pull.

## Amendments after review (2026-10-09, before any data was pulled or computed)

An independent statistics review found that the original foresight window [τ − Δ, τ) mostly lies
**after** GDELT had published the window's news, so it measured the publication period, not
foresight. The design is replaced by an event-time profile, plus a test of which way causality
runs. Everything below is fixed now.

1. **Anchor.** t_a = τ − L − W + 15, the first availability of the window's oldest batch: τ − 60
   at the registered L = 15, W = 60.
   - **Foresight** is the move over [t_a − Δ, t_a).
   - Two segments are reported but are **never foresight cells**:
     - *publication*: [t_a, τ − L);
     - *latency*: [τ − L, τ).
   - Price at time x: the close of the last regular bar ending at or before x on that day. When x
     is before 09:30, the previous trading day's last close.
   - When t_a − Δ and t_a fall on different sessions, the overnight part is reported as its own
     segment.
2. **Fresh events only.** For the event's (country or company, category), every window with the
   same stamps over the previous 24 hours must have z < 1. This keeps the move from being a
   reaction to the event's own earlier coverage. It uses the all-batch pull of ADR 0008.
3. **Placebo.** ADR 0008 block permutation. Placebo days within Δ_days + 1 trading days of any
   event day of the same asset are excluded.
4. **No-value rule.** A cell supports "no value of foresight" only if the upper 95% bound of
   s·mean is below its round-trip cost RT. Otherwise it is "inconclusive" when not significant.
5. **Reverse event study (does news follow price?).**
   - Price shocks: 15-minute abnormal returns |r| > 3σ, with σ the asset's trailing 20-day
     standard deviation of 15-minute abnormal returns at the same bar. Only bars with no event of
     that category in the previous 24 hours count.
   - For each category, measure the asset's share z over the next 0–24 hours against the same
     placebo.
   - A category counts as **exogenous** only if this response is **not** significant (BH
     q = 0.10 across categories in discovery). This replaces the classification by assumption.
     The assumed labels are kept only as a prior for reporting.
6. **Trials.** Look-ahead trials (`params.lookahead = true`) are **excluded from DSR's N** and
   refused by `backtest`, `holdout` and `followup`. Code and tests enforce both.
7. **Disclosure.** In discovery, the Δ = 60/120 min segments overlap ADR 0007's published
   pre-move diagnostic (EM attention t −2.2, positive spikes t −2.0). Confirmation (2020–2025Q1,
   never computed intraday) decides.
8. **Not used:** GKG `PAGE_PRECISEPUBTIMESTAMP` (it would separate source publication from
   GDELT ingestion). It sits in an expensive extras column and is often absent.
