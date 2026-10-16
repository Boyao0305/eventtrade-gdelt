# 0017 — Event response curves: magnitude, direction and time scale (hypothesis family V, descriptive diagnostic)

Date: 2026-10-16. The user asked whether the null results come from testing fixed horizons when
each event type moves prices on its own time scale and then reverts (a half-life), and from
events whose direction varies. Written before any curve was computed.

## Question

For each event type, three questions:
1. Does the event move prices at all, regardless of direction?
2. On what time scale: where is the peak, and what is the half-life?
3. Is the direction consistent?

The answer separates "no effect" from "effect at another horizon" and from "large but
directionless" (a volatility case, not a directional one).

## Decision

**Event types** reuse the frozen event definitions; nothing is re-selected. t is the first
trading day on which the event can be public.

| Cell | Events | t | Asset | Hedge (abnormal return) |
|---|---|---|---|---|
| V-EARN | ADR 0013 earnings days (8-K 2.02, deduplicated and deal-filtered), Track B, coverage rule | d | stock | sector SPDR |
| V-CPI | ADR 0014 CPI/NFP days (no FOMC overlap) | d | SPY | none (raw) |
| V-FOMC | ADR 0014 scheduled FOMC days held | d | SPY | none |
| V-DOD-mat / V-DOD-mod | ADR 0015 material / moderate issuer-days (frozen map, exclusions, rules; h = 1 declustering) | next trading day after d (17:00 release) | stock | sector SPDR / IWM |
| V-INS-all / V-INS-cxo | ADR 0011 `ins_all` / `ins_cxo` | ADR 0011 event day (filing) | stock | sector SPDR |
| V-GD-conf-dm / V-GD-conf-em | ADR 0005 Track A shocks of any conflict family or low tone (quad3, quad4, root_14, root_163, violence, tone), pooled per country ETF | shock day | country ETF | EFA (DM) / EEM (EM) |
| V-GD-coop-dm / V-GD-coop-em | the same for cooperation (quad1, quad2) | shock day | country ETF | EFA / EEM |
| V-GD-comm | ADR 0005 E2 producer-country conflict (quad4, violence, root_163, root_14) | shock day | commodity ETFs | none |
| V-GD-att-neg / V-GD-att-pos | ADR 0005 Track B attention spikes, split by same-day tone | spike day | stock | sector SPDR |

- **GDELT settings:** fixed θ = 3 and W = 60, one configuration from the ADR 0005 grid, chosen
  now without looking. Any trigger in a pooled family set counts, and events are declustered
  at 20 days per asset.

**Curves (daily).** The base is close(t−1), the last close before the event can be public.
- Cumulative abnormal return CAR(k) = asset close(t−1+k) / close(t−1) − 1, minus the hedge's,
  for k ∈ {1, 2, 3, 5, 10, 20, 40, 60}. k = 1 is the event day.
- Pre-event run-up: CAR(−5) = close(t−1) / close(t−6) − 1, hedged.

**Curves (intraday, SPY 15-minute bars).**
- V-CPI-intraday: from close(d−1) to 09:30 open, 09:45, 10:00, 10:30, 11:30, 13:00 and close.
- V-FOMC-intraday: from p(13:45) to 14:00, 14:15, 14:30, 14:45, 15:00, 15:30 and close.

**Placebo.**
- Daily: the same asset's CAR(k), computed identically from every other trading day of the same
  calendar year (event days included; ADR 0014 lesson).
- SPY cells: same-year and same-weekday days, excluding announcement days ±1.

**Statistics per cell × period × horizon.** Periods: A = 2016–2019, B = 2020–2025-03.
- Per event: s_i = CAR_i − its placebo pool mean, and a_i = |CAR_i| − its placebo pool's mean
  |CAR|.
- **Signed excess** S(k) = mean(s), and **absolute excess** M(k) = mean(a). CIs come from a
  month-block bootstrap (10,000 draws, `config.seed`) at 99.4% (Bonferroni over the 9 horizons
  k = −5…60, two-sided 5%).
- **Time scale:**
  - peak k* = the horizon k ≥ 1 with the largest M(k);
  - half-life = the smallest k > k* with M(k) ≤ M(k*)/2 ("persistent" if none by 60).
  - The same for |S|, from its own peak.
  - For intraday cells, the horizons are the listed times.

**Reading** (fixed now; a descriptive diagnostic with no strategy and no costs):
- **Moves prices:** M's lower 99.4% bound > 0 at some k ≥ 1 in both periods.
- **Directional:** S's 99.4% CI excludes 0, with the same sign in both periods, at some k ≥ 1.
- **Directionless:** moves prices but not directional, so a volatility event.
- **No response:** neither.
- Reported with every cell: n, k*, half-life, S and M at every k, and the run-up CAR(−5).

**Logging.**
- One `signal` trial per cell × period (hypothesis V), with `params.lookahead = true`. The base
  includes the event day's reaction, so these are not tradable signals, and they are excluded
  from DSR's N.
- Once only, logged after everything is computed.

## Consequences

- No new data and no cost.
- **Directionless types point to volatility or options strategies**, which need option data
  (paid) or realized-volatility trades.
- **Directional types at a new time scale** need a new pre-registered ADR before any trade.
- **The verdicts of ADRs 0005–0016 are unchanged.**

## Amendments after review (2026-10-16, before any curve was computed)

All items are adopted. Where they conflict with the Decision above, they win.

1. **Primary magnitude: standardised excess variance** (B1).
   - EV(k) = mean_i[z_i²] − the mean of the event's own placebo pool, where
     z_i² = CAR_i(k)² / (σ_i²·k).
   - σ_i is the abnormal daily return σ over [b−250, b−21], where b is the base position; pool
     windows use their own σ.
   - z² is winsorized at the cell × k 1%/99% quantiles of events and pool together.
   - **Time scale is read from EV(k)·k:**
     - a permanent jump keeps it flat, a reverting one makes it fall, and continued drift makes
       it rise;
     - the peak k* = argmax EV(k)·k, and the half-life is the smallest k > k* with
       EV·k ≤ peak/2 ("persistent" if none).
   - M(k) (excess |CAR|) is reported only.
2. **Continuation curve** (B2).
   - C(k) = mean_i[sgn(CAR_i(1))·(CAR_i(k) − CAR_i(1))] − the placebo analogue, for k ≥ 2.
     Negative means reversal.
   - Reversal half-life: the smallest k with C(k) ≤ −mean|CAR_i(1)|/2.
   - Per-interval excess variance between consecutive horizons is reported.
   - **"Directionless / volatility"** requires S's and C's CIs to contain 0 at every k.
3. **Hold-out.** Prices end before 2025-04-01. An event enters horizon k only if close(b+k)
   exists, and n per k is reported.
4. **Earnings base:** close(d−2), so t = d−1 (ADR 0013 amendment 2).
5. **Hedges with β.**
   - Abnormal r = r − β̂·r_hedge, with β̂ by OLS over [b−250, b−21].
   - GDELT country cells use ADR 0005's leave-one-out equal-weighted group as the hedge,
     replacing EFA/EEM.
   - Commodity cells: raw, and also SPY-β-hedged (reported).
   - SPY cells are raw.
6. **Attention spikes:** base close(t), not close(t−1), because day t's news often reacts to
   day t's price move.
7. **CIs and sufficiency.**
   - CIs use a quarter-block bootstrap (10,000 draws, `config.seed`).
   - Bonferroni over k ≥ 1 only: 8 daily horizons (99.375%) and 7 intraday horizons (99.29%).
   - A cell × period needs ≥ 30 events and ≥ 30 distinct dates; otherwise it is logged as
     insufficient.
   - FOMC (n ≈ 30) also reports a permutation p (ADR 0014 machinery).
8. **The same k in both periods** for "moves prices" (EV) and "directional" (S, or C for
   continuation and reversal). k* is reported only when EV's lower bound at k* is > 0.
9. **Multiplicity for the future.** Trials are logged with `lookahead = false`, since C is
   tradable from the t+1 open, so they enter N. Any later ADR that picks a horizon or cell
   from this diagnostic counts all V cell × horizon combinations (≈ 16 × 8) in its DSR N.
10. **Placebo windows.**
    - Primary: placebo start days whose window (b, b+k] contains another event of the cell are
      excluded. The cell's own events stay in the pool as permutation members.
    - The inclusive pool is reported.
    - SPY cells: the pool excludes *other* announcement days ±1 and includes the cell's own
      events.
11. **Foregone conclusions.** Earnings, CPI and FOMC moving prices is not a finding (ADR 0016).
    For them, only the time scale, C and S are read.
12. **Nits.**
    - GDELT conflict and cooperation events are pooled with `event_signals.composite_events`
      logic (union, then one decluster per ETF and cell), and the overlap is reported.
    - DoD: if d is not a trading day, b = the last trading day before d.
    - Insider filings after the close fall into CAR(2) (no timestamp).
    - GDELT news after the 15:30 cutoff on t−1 sits before the base (stated).
    - 16 cells × 2 periods = 32 trials.

## Implementation notes (2026-10-16, before the run)

- **Blind dry run** (trials stubbed out; only counts and run time printed). Events per cell over
  the full sample:

  | Cell | Events |
  |---|---|
  | V-EARN | 33,418 (826 tickers) |
  | CPI/NFP days / FOMC days | 230 / 73 |
  | DoD material / moderate | 426 / 1,393 |
  | Insider all / CEO/CFO | 4,600 / 1,207 |
  | GDELT conflict DM / EM | 1,424 / 1,493 |
  | GDELT cooperation DM / EM | 461 / 476 |
  | GDELT commodity | 424 |
  | GDELT attention negative / positive | 14,403 / 8,863 |

  - Conflict and cooperation fire on the same day 13 times in DM and 11 in EM.
  - **V-FOMC in period A has 27 events, below the 30 minimum**, so its reading will be
    "insufficient" (amendment 7, applied as written).
- **The SPY-β-hedged commodity variant** (amendment 5) is its own cell, V-GD-comm-spy. That makes
  **17 cells × 2 = 34 trials**, not 32.
