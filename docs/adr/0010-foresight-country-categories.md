# 0010 — Value of foresight for country news, looser freshness rule (hypothesis O2)

Date: 2026-10-09. Written after experiment 0011 and before any computation under the rule below.
The user chose this follow-up.

## Context and disclosure

ADR 0009's freshness rule (z < 1 in every window of the previous 24 hours) left ≤ 33 events per
country cell, so 21 of the 24 country and producer cells could not be tested. Those are the
categories that matter for "predict the arrival of the event from alternative data".

The point estimates of those cells (n ≤ 33) were printed in experiment 0011. They are noise, and
they did not shape the rule below, which follows the most common definition of a "new" event
burst. The same 2016–2019 and 2020–2025Q1 periods are reused. This is a second, disclosed look,
and every trial is look-ahead flagged (excluded from DSR's N).

## Decision

Everything is as in ADR 0009, with its amendments and implementation notes, except the four
changes below. The unchanged parts:
- registered configuration L = 15, W = 60, θ = 3;
- anchor t_a = τ − 60, foresight return over [t_a − Δ, t_a);
- the 7 leads;
- block-permutation placebo with same-asset exclusion, 2,000 draws, seed `config.seed`;
- ≥ 30 events and ≥ 30 days;
- BH q = 0.10 in discovery, then Holm 0.05 in confirmation;
- the no-value rule and the reading rules;
- `params.lookahead = true`.

1. **Freshness = first firing in 24 hours.** An event is fresh when the same (country or
   producer basket, category) did **not fire** (z ≥ 3 with the minimum activity) in any window of
   the previous 24 hours that does not overlap its own window. The overlap and day-boundary rules
   are those of ADR 0009's implementation notes. ADR 0009 required z < 1 instead of "no firing".
2. **Cells:** the 22 country and producer pairs of ADR 0009 × 7 leads = **154 cells.** The
   company spikes were answered in experiment 0011 and are not repeated.
3. **Hypothesis label:** `O2`. The pipeline refuses to run if any `O2` trial exists.
4. **The reverse event study is not repeated.**

## Consequences

- +154 look-ahead discovery trials (plus confirmations), excluded from DSR's N.
- No new data. Compute ≈ 40 minutes.
- **If nothing is discovered or confirmed:** knowing country events up to 5 days early carried no
  robust value in these data. That closes the event-arrival-prediction route for GDELT country
  categories.
