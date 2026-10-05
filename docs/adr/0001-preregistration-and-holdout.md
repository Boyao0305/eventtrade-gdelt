# 0001 — Pre-registered hypotheses, gates and hold-out

Date: 2026-10-04. Written before any GDELT feature or price was looked at.

## Context

The closest published test (Alexander 2026, SSRN 7274618) ran ~3,000 configurations of GDELT
*country tone and volume* on 24 equity-index and 9 bond futures, 2015–2026, and found nothing
significant after deflating for the number of trials (best DSR 0.80 < 0.95). News effects in the
literature concentrate in small, volatile single stocks (Ke, Kelly & Xiu) and persist longer when
aggregated weekly (Heston & Sinha 2017). Realistic prior for liquid ETFs at daily frequency:
IC 0.01–0.03, net Sharpe < 0.5. A negative result is the most likely outcome; the system is built
so that a negative is trustworthy and cheap.

## Decision

**Split.** Development data: 2015-03-02 → 2025-03-31. Hold-out: 2025-04-01 → latest. Nothing in
the hold-out is loaded into any analysis until the final stage, and it is evaluated **once** per
surviving strategy. `config.HOLDOUT_START` encodes this; pipelines must filter on it.

**Decision clock.** A feature for trading day *t* uses only GDELT rows with `DATEADDED` at or before
15:30 America/New_York on *t* (and after the previous session's cutoff). Positions are entered at
the *t+1* open. `SQLDATE` is never used for timing.

**Hypotheses.** Each is tested on the stated universe and horizons (1, 5, 20 trading days).

| Id | Track | Hypothesis | Expected sign |
|---|---|---|---|
| H0 | A | Country tone and volume z-scores → country ETF returns (benchmark; replicates the null) | none |
| A1 | A | Conflict/coercion shock (CAMEO roots 14, 17–20; negative Goldstein mass) vs 60-day baseline, normalised by global volume → country ETF returns | negative |
| A2 | A | Supply-disruption shock in major oil/commodity producers (conflict + GKG energy themes) → commodity and energy ETFs | positive |
| A3 | A | Weekly (5-day) tone surprise → 5- and 20-day country ETF returns | same as tone |
| A4 | A | Event shocks → future realised volatility (not direction) | positive |
| B1 | B | Company attention surprise × tone → stock returns, S&P 500 + 400 | same as tone |
| B2 | B | Negative-tone attention spikes → continued drift (slow absorption of bad news) | negative |

**Gates.**

1. *Signal gate* (univariate, dev period): |Newey-West t| of the daily IC ≥ 3.0, same sign in at
   least 2 of 3 equal sub-periods, and top-minus-bottom spread positive after one-way costs.
   Only features passing go to models.
2. *Model gate*: the model must beat the rule baseline out-of-sample under combinatorial purged CV.
3. *Overfitting gate*: Deflated Sharpe ≥ 0.95 computed over **every** logged trial of that track,
   and PBO < 0.5.
4. *Hold-out*: evaluated once; reported with a bootstrap 95% CI. Pass = CI excludes zero.
   Then paper trading.

Every evaluated configuration is written to the `trials` table, including ones that fail.

## Consequences

- Most configurations will fail gate 1; that is the intended cheap exit.
- Track B uses current index members (free data lacks delisted stocks) — survivorship bias
  inflates it, so Track B results are reported with that caveat and need gate 3 at DSR ≥ 0.95
  without exceptions.
- Adding a hypothesis after seeing data is allowed but must be a new ADR, and its trials count
  toward the same DSR.

## Alternatives rejected

- *Country tone/volume as the main hypothesis*: already measured null at scale (above).
- *Walk-forward-only validation*: single path, high variance; CPCV is used for model selection.
- *No hold-out*: with thousands of trials the in-sample best is selected noise.
