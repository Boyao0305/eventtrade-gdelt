# 0009 — Intraday event study (ADR 0007)

Date: 2026-10-09. Code: commit `479543f`, run once from the frozen checkout (`intraday-study`).
Before the run, the full suite was verified: 1,340 passed, 6 import contracts kept.

## Question

Do GDELT shocks move prices within the next hour or by the same day's close, once the news is
available to a trader? The daily tests (0003–0008) could not see such an effect, because they
traded at the next open.

## Data

- **Prices.** Alpaca 15-minute SIP bars for 961 tickers, 2016-01-04 → 2025-03-31: 53.9M
  regular-session bars, all ticker-years fetched. Four recent symbols have no bars (FDXF, HONA,
  PSKY, SOLS).
- **GDELT.** 15-minute events (3.85M rows, 54 countries + `_GLOBAL`) and GKG company mentions
  (5.11M rows, 900 stocks + `_GLOBAL`) for 2015-12-01 → 2025-03-31.
- **Cost.** 350 GB of BigQuery. October billed 1,883 GB in total, ≈ 784 GB over the free tier,
  so cumulative spend is ≈ $4.5 of the user's $20 cap. A network outage interrupted the orgs
  pull after 2023; the ledger resumed it, and BigQuery served the 2024 chunk from cache.
- **Calendar.** 2,366 trading days, of which 43 are no-decision days: early closes plus the
  December 2015 baseline days.
- **Liquidity filter.** Kept 833 of 948 assets, excluded 115.

## Results (discovery, 2016-01-04 → 2019-12-31; placebo-day p, 2,000 draws)

| Cell | Events | Days | Mean | NW t | p | Pre-move t | Firing rate |
|---|---|---|---|---|---|---|---|
| I1 conflict · dm · h60 | 824 | 476 | −0.1 bp | −0.14 | 0.434 | 0.78 | 0.068 |
| I1 conflict · dm · hC | 824 | 476 | +0.1 bp | 0.18 | 0.728 | 0.78 | 0.068 |
| I1 conflict · em · h60 | 1175 | 613 | −0.4 bp | −0.49 | 0.379 | −1.58 | 0.073 |
| I1 conflict · em · hC | 1175 | 613 | +1.5 bp | 1.20 | 0.942 | −1.58 | 0.073 |
| I2 tone · dm · h60 | 677 | 437 | +0.2 bp | 0.43 | 0.664 | −0.32 | 0.056 |
| I2 tone · dm · hC | 677 | 437 | −0.4 bp | −0.51 | 0.439 | −0.32 | 0.056 |
| I2 tone · em · h60 | 1022 | 576 | −0.3 bp | −0.35 | 0.365 | −0.72 | 0.064 |
| I2 tone · em · hC | 1022 | 576 | +0.7 bp | 0.48 | 0.733 | −0.72 | 0.064 |
| I3 attention · dm · h60 | 1074 | 598 | 0.0 bp | 0.04 | 0.978 | 0.46 | 0.089 |
| I3 attention · dm · hC | 1074 | 598 | −0.7 bp | −1.01 | 0.327 | 0.46 | 0.089 |
| I3 attention · em · h60 | 1595 | 700 | +0.1 bp | 0.14 | 0.896 | −2.22 | 0.099 |
| I3 attention · em · hC | 1595 | 700 | −0.2 bp | −0.17 | 0.864 | −2.22 | 0.099 |
| I4 producer conflict · commodity · h60 | 375 | 176 | −0.4 bp | −0.18 | 0.522 | −1.94 | 0.062 |
| I4 producer conflict · commodity · hC | 375 | 176 | −0.6 bp | −0.16 | 0.509 | −1.94 | 0.062 |
| I5 negative spike · stocks · h60 | 11909 | 835 | −0.3 bp | −0.38 | 0.320 | −0.76 | 0.026 |
| I5 negative spike · stocks · hC | 11909 | 835 | +0.3 bp | 0.32 | 0.508 | −0.76 | 0.026 |
| I6 positive spike · stocks · h60 | 9736 | 835 | +0.5 bp | 0.69 | 0.340 | −1.98 | 0.022 |
| I6 positive spike · stocks · hC | 9736 | 835 | +2.3 bp | 1.89 | 0.061 | −1.98 | 0.022 |

**0 of 18 cells discovered** (BH q = 0.10). The study stops at discovery and no confirmation is
run. 18 trials are logged (hypotheses I1–I6), and the store now holds 1,869 trials.

## Interpretation

- **The intraday reactions are economically nil.** Every cell's mean hedged return over the hour
  or to the close is within ±2.3 bp, an order of magnitude below a realistic round trip (≈ 12 bp
  for ETFs with a hedge, ≈ 22 bp for stocks).
- **Speed was not the reason the daily tests failed.** The daily (0003–0008) and intraday nulls
  together say GDELT country, tone and company-mention shocks, as measured here, carry no
  tradable return information at horizons from one hour to 20 days.
- **The pre-move diagnostic gives a weak hint that prices lead GDELT.** It is mostly small. Its
  largest values, EM attention −2.2 and positive company spikes −2.0, have the opposite sign to
  any later drift. That would fit prices moving before GDELT stamps the news, but it is
  descriptive and was never tested.
- **Firing rates are 2–10% of eligible asset-days.** These are frequent shifts, not rare shocks,
  as the stats review predicted.

## What this does not establish

- Other definitions (θ, windows, latency, sentiment from article text) were not tested, by
  design: one configuration was registered.
- Effects within 15 minutes are below GDELT's resolution.
- Survivorship bias (current index members) and current GICS sectors remain limitations.
- The hold-out is untouched.
