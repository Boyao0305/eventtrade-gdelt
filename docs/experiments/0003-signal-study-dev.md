# 0003: Gate-1 signal study on the development period, and its verification

Date: 2026-10-05. Development data only (2015-03-02 to 2025-03-31). The hold-out was not loaded.

## Question

Does any pre-registered feature (ADR 0001: H0, A1, A3, A4 on Track A; B1, B2 on Track B) pass
gate 1 on the development period? If something passes, is the pass a real event-driven effect or
an artefact of how the feature, label or gate is built? And if nothing passes, how large an effect
could the test have missed?

**Answer: no signal survives.** 0 of 42 Track A direction trials and 0 of 21 Track B trials pass.
The 6 Track A vol-target (A4) trials that the pipeline logged as passes do not hold up once ticker
level or trailing volatility is controlled for: they come from static between-ticker differences
plus volatility persistence. **Nothing is eligible for gate 2.**

## Setup

- Code: branch `dev`, commit `3ee816d`, run via `signal-study --track A` and `--track B`.
  Trials were written between 2026-10-05 05:46 and 06:00 UTC.
- Store: `workspace/gdelt/data/eventtrade.sqlite`. All verification ran on a read-only
  connection (`mode=ro`) and logged no trials.
- Versions: Python 3.12.13, pandas 3.0.6, numpy 2.5.3, scipy 1.18.1, scikit-learn 1.9.1,
  SQLite 3.50.4.
- GDELT (`gdelt_daily`):
  - `events_daily`: 17,132,900 rows, 266 entities.
  - `gkg_orgs_daily`: 7,399,140 rows, 4,049 aliases.
  - Both cover session dates 2015-03-02 to 2025-03-31.
  - **No GKG theme rows are loaded.** Theme features do not exist yet, so A2 was not tested and
    the 11 theme-only sector ETFs have no features.
- Universe (`assets`):
  - Track A: 63 ETFs, with 158,706 price rows. Event features cover 52 of them, because the 11
    sector ETFs have no FIPS countries.
  - Track B: 903 stocks, of which 899 have prices (2,147,100 rows). A median of 846 stocks per
    day have an h=1 label.
- Features: 21 feature names, 9,572,500 rows, first date 2015-03-30 (after the 60-day warm-up).
  Labels: 2,305,806 rows per horizon.
- Gate-1 parameters (`config.py`):
  - Horizons 1, 5 and 20 trading days.
  - Cost 5 bp for ETFs and 10 bp for stocks.
  - Charged cost is `4 * cost_bps * min(1, h * turnover)` (`core/evaluate.py:passes_gate`).
- Trial count after this run: 96 signal trials (75 Track A, 21 Track B). That is the N that
  enters the Deflated Sharpe from now on.

## Method

1. **Gate 1 as implemented** (`cli/pipelines.py:signal_study`). For each feature and horizon it
   computes:
   - the daily cross-sectional Spearman IC against the `ret_oo` label;
   - the Newey-West t of the IC series;
   - the IC mean in each of 3 equal sub-periods;
   - the Q5-Q1 spread net of cost.

   It passes if |t| ≥ 3, the sign holds in at least 2 of 3 sub-periods, and the directional spread
   exceeds cost. A4 (`_signal_study_vol`) uses the same IC and t against the realised volatility
   of the next h days. It has no spread condition, which is a local variant: the ADR defines no
   vol gate.
2. **Five independent read-only verification checks**, run after the gate. Their scripts are
   scratch files outside the repo, under `C:/Users/boyao/.claude/jobs/d07061ba/tmp/verify/<key>/`,
   and may not persist.
   - *static*: decomposes the A4 ICs into between-ticker and within-ticker parts (ticker
     demeaning, a trailing-baseline version, a static-mean placebo, and dropping US-duplicate
     and multi-country ETFs).
   - *clustering*: tests whether A4 adds anything beyond volatility clustering. It uses partial
     IC given |r_t|, rv5, rv22, rv60 and each ticker's expanding long-run vol, a pooled log-HAR
     panel with Driscoll-Kraay SEs, and expanding-window OOS incremental R².
   - *definitions*: audits timing and leakage of `surprise()` and `realized_vol_label`, runs a
     Poisson null for the `lr` and `z` transforms, and takes a census of extreme z values.
   - *power*: checks label alignment, recovery of injected ICs, minimum detectable IC (MDE) with
     real features as the noise carrier, and price-anomaly positive controls.
   - *coverage*: Track B alias coverage and sparsity, the Track A event coverage, and the
     composition of the A4 cross-section.
3. All gate-1 numbers below are read from the `trials` table (`stage='signal'`), not retyped.

## Results

### Track A, direction (`ret_oo`): 0 of 42 pass

Legend for "fails": `t` = |NW t| < 3; `sub` = sign held in fewer than 2 of 3 sub-periods;
`cost` = directional spread ≤ cost. The spread column is the Q5-Q1 spread in the direction of the
observed IC sign.

| hyp | feature | h | days | IC | NW t | sub-period IC | spread bp | cost bp | fails |
|---|---|---|---|---|---|---|---|---|---|
| A1 | `events.goldstein.mean_z.60` | 1 | 2512 | +0.0026 | +0.75 | +0.0041 / +0.0043 / -0.0007 | +0.1 | 11.2 | t,cost |
| A1 | `events.goldstein.mean_z.60` | 5 | 2508 | -0.0039 | -0.75 | -0.0031 / +0.0028 / -0.0115 | +5.0 | 20.0 | t,cost |
| A1 | `events.goldstein.mean_z.60` | 20 | 2493 | -0.0093 | -1.22 | -0.0120 / -0.0078 / -0.0081 | +11.6 | 20.0 | t,cost |
| A1 | `events.root_14_protest/n_events.sum_share_lr.60` | 1 | 2512 | +0.0017 | +0.55 | +0.0011 / -0.0046 / +0.0087 | +0.7 | 11.5 | t,cost |
| A1 | `events.root_14_protest/n_events.sum_share_lr.60` | 5 | 2508 | +0.0020 | +0.45 | -0.0094 / -0.0005 / +0.0160 | +1.7 | 20.0 | t,sub,cost |
| A1 | `events.root_14_protest/n_events.sum_share_lr.60` | 20 | 2493 | -0.0011 | -0.17 | -0.0290 / -0.0041 / +0.0297 | -1.4 | 20.0 | t,cost |
| A1 | `events.root_14_protest/n_events.sum_share_z.60` | 1 | 2512 | +0.0038 | +1.15 | +0.0035 / +0.0016 / +0.0063 | +1.4 | 11.8 | t,cost |
| A1 | `events.root_14_protest/n_events.sum_share_z.60` | 5 | 2508 | +0.0024 | +0.49 | -0.0076 / +0.0031 / +0.0116 | +2.0 | 20.0 | t,cost |
| A1 | `events.root_14_protest/n_events.sum_share_z.60` | 20 | 2493 | +0.0032 | +0.48 | -0.0179 / +0.0027 / +0.0247 | +13.1 | 20.0 | t,cost |
| A1 | `events.root_17_coerce/n_events.sum_share_lr.60` | 1 | 2512 | -0.0036 | -1.19 | -0.0026 / -0.0082 / -0.0000 | +0.4 | 10.9 | t,cost |
| A1 | `events.root_17_coerce/n_events.sum_share_lr.60` | 5 | 2508 | -0.0025 | -0.53 | -0.0113 / -0.0079 / +0.0118 | -2.1 | 20.0 | t,cost |
| A1 | `events.root_17_coerce/n_events.sum_share_lr.60` | 20 | 2493 | +0.0083 | +1.20 | -0.0022 / +0.0034 / +0.0237 | +17.3 | 20.0 | t,cost |
| A1 | `events.root_17_coerce/n_events.sum_share_z.60` | 1 | 2512 | -0.0004 | -0.13 | -0.0001 / -0.0036 / +0.0024 | -0.4 | 12.1 | t,cost |
| A1 | `events.root_17_coerce/n_events.sum_share_z.60` | 5 | 2508 | +0.0022 | +0.44 | -0.0036 / -0.0011 / +0.0113 | +5.7 | 20.0 | t,sub,cost |
| A1 | `events.root_17_coerce/n_events.sum_share_z.60` | 20 | 2493 | +0.0125 | +1.71 | +0.0067 / +0.0128 / +0.0181 | +28.0 | 20.0 | t |
| A1 | `events.root_18_assault/n_events.sum_share_lr.60` | 1 | 2512 | -0.0027 | -0.95 | -0.0042 / -0.0021 / -0.0019 | +0.1 | 11.7 | t,cost |
| A1 | `events.root_18_assault/n_events.sum_share_lr.60` | 5 | 2508 | +0.0001 | +0.03 | -0.0087 / -0.0024 / +0.0115 | +0.2 | 20.0 | t,sub,cost |
| A1 | `events.root_18_assault/n_events.sum_share_lr.60` | 20 | 2493 | -0.0035 | -0.55 | -0.0059 / -0.0034 / -0.0012 | +0.7 | 20.0 | t,cost |
| A1 | `events.root_18_assault/n_events.sum_share_z.60` | 1 | 2512 | +0.0010 | +0.31 | +0.0019 / +0.0024 / -0.0014 | +1.0 | 12.3 | t,cost |
| A1 | `events.root_18_assault/n_events.sum_share_z.60` | 5 | 2508 | +0.0074 | +1.49 | +0.0045 / +0.0039 / +0.0136 | +8.0 | 20.0 | t,cost |
| A1 | `events.root_18_assault/n_events.sum_share_z.60` | 20 | 2493 | +0.0042 | +0.66 | +0.0052 / +0.0060 / +0.0013 | +14.0 | 20.0 | t,cost |
| A1 | `events.root_19_fight/n_events.sum_share_lr.60` | 1 | 2512 | -0.0025 | -0.82 | -0.0094 / -0.0000 / +0.0020 | -0.7 | 10.5 | t,cost |
| A1 | `events.root_19_fight/n_events.sum_share_lr.60` | 5 | 2508 | +0.0026 | +0.57 | -0.0109 / +0.0066 / +0.0122 | +5.4 | 20.0 | t,cost |
| A1 | `events.root_19_fight/n_events.sum_share_lr.60` | 20 | 2493 | +0.0054 | +0.79 | -0.0050 / -0.0005 / +0.0215 | +15.4 | 20.0 | t,sub,cost |
| A1 | `events.root_19_fight/n_events.sum_share_z.60` | 1 | 2512 | -0.0019 | -0.54 | -0.0063 / +0.0002 / +0.0005 | -0.3 | 11.1 | t,sub,cost |
| A1 | `events.root_19_fight/n_events.sum_share_z.60` | 5 | 2508 | +0.0056 | +1.04 | -0.0033 / +0.0081 / +0.0118 | +6.5 | 20.0 | t,cost |
| A1 | `events.root_19_fight/n_events.sum_share_z.60` | 20 | 2493 | +0.0105 | +1.42 | +0.0049 / +0.0053 / +0.0211 | +24.7 | 20.0 | t |
| A1 | `events.root_20_massviolence/n_events.sum_share_lr.60` | 1 | 2510 | +0.0112 | +2.03 | +0.0271 / +0.0010 / +0.0055 | +1.0 | 14.4 | t,cost |
| A1 | `events.root_20_massviolence/n_events.sum_share_lr.60` | 5 | 2506 | +0.0224 | +2.50 | +0.0396 / +0.0053 / +0.0223 | +3.1 | 20.0 | t,cost |
| A1 | `events.root_20_massviolence/n_events.sum_share_lr.60` | 20 | 2491 | +0.0216 | +1.45 | +0.0398 / +0.0080 / +0.0171 | +19.3 | 20.0 | t,cost |
| A1 | `events.root_20_massviolence/n_events.sum_share_z.60` | 1 | 2512 | +0.0099 | +3.07 | +0.0119 / +0.0118 / +0.0060 | +4.2 | 12.4 | cost |
| A1 | `events.root_20_massviolence/n_events.sum_share_z.60` | 5 | 2508 | +0.0095 | +2.11 | +0.0135 / +0.0102 / +0.0049 | +5.9 | 20.0 | t,cost |
| A1 | `events.root_20_massviolence/n_events.sum_share_z.60` | 20 | 2493 | +0.0077 | +1.30 | +0.0138 / +0.0099 / -0.0006 | +15.5 | 20.0 | t,cost |
| A3 | `events.tone.mean5d_z.60` | 1 | 2492 | +0.0015 | +0.46 | -0.0030 / +0.0089 / -0.0013 | -0.8 | 3.9 | t,sub,cost |
| A3 | `events.tone.mean5d_z.60` | 5 | 2488 | -0.0047 | -0.69 | -0.0090 / +0.0032 / -0.0084 | +6.6 | 19.3 | t,cost |
| A3 | `events.tone.mean5d_z.60` | 20 | 2473 | -0.0185 | -1.76 | -0.0157 / -0.0276 / -0.0123 | +25.1 | 20.0 | t |
| H0 | `events.n_events.share_z.60` | 1 | 2512 | -0.0018 | -0.53 | -0.0038 / -0.0040 / +0.0023 | +0.6 | 10.0 | t,cost |
| H0 | `events.n_events.share_z.60` | 5 | 2508 | +0.0050 | +0.88 | -0.0062 / +0.0035 / +0.0177 | +4.6 | 20.0 | t,cost |
| H0 | `events.n_events.share_z.60` | 20 | 2493 | +0.0119 | +1.41 | -0.0112 / +0.0098 / +0.0371 | +28.6 | 20.0 | t |
| H0 | `events.tone.mean_z.60` | 1 | 2512 | +0.0066 | +1.89 | +0.0081 / +0.0112 / +0.0005 | +2.1 | 10.7 | t,cost |
| H0 | `events.tone.mean_z.60` | 5 | 2508 | +0.0023 | +0.43 | +0.0019 / +0.0066 / -0.0017 | -0.1 | 20.0 | t,cost |
| H0 | `events.tone.mean_z.60` | 20 | 2493 | -0.0084 | -1.04 | -0.0066 / -0.0140 / -0.0045 | +11.9 | 20.0 | t,cost |

Points to note:

- The only |t| ≥ 3 result is `root_20_massviolence ... share_z.60` at h=1 (t +3.07). Its spread
  is 4.2 bp against a cost of 12.4 bp, so it fails.
- Its sign is **positive**, but ADR 0001 pre-registers A1 as **negative**. Most of the larger A1
  ICs (massviolence z and lr, coerce z at h=20, fight z at h=20) are also positive, the opposite
  of the hypothesis.
- The gate takes its sign from the observed t, not from the pre-registration (bug 2 below). A
  wrong-signed A1 feature with a cheap enough spread would therefore have been logged as a pass.
- The power check bounds the true h=1 IC of this feature at about 0.0099 + 2·0.0033 ≈ 0.017.

### Track A, vol target (A4, `realized_vol`): 6 of 33 logged as pass, 0 survive verification

| feature | h | days | IC | NW t | sub-period IC | gate (as logged) |
|---|---|---|---|---|---|---|
| `events.goldstein.mean_z.60` | 1 | 2513 | -0.0017 | -0.47 | +0.0037 / -0.0064 / -0.0024 | fail |
| `events.goldstein.mean_z.60` | 5 | 2509 | -0.0032 | -0.56 | -0.0021 / -0.0062 / -0.0012 | fail |
| `events.goldstein.mean_z.60` | 20 | 2494 | -0.0053 | -0.60 | -0.0006 / -0.0107 / -0.0047 | fail |
| `events.root_14_protest/n_events.sum_share_lr.60` | 1 | 2513 | -0.0134 | -3.91 | -0.0097 / -0.0147 / -0.0157 | **pass** |
| `events.root_14_protest/n_events.sum_share_lr.60` | 5 | 2509 | -0.0218 | -4.37 | -0.0144 / -0.0240 / -0.0269 | **pass** |
| `events.root_14_protest/n_events.sum_share_lr.60` | 20 | 2494 | -0.0239 | -3.44 | -0.0132 / -0.0222 / -0.0364 | **pass** |
| `events.root_14_protest/n_events.sum_share_z.60` | 1 | 2513 | -0.0022 | -0.62 | +0.0011 / -0.0014 / -0.0061 | fail |
| `events.root_14_protest/n_events.sum_share_z.60` | 5 | 2509 | -0.0051 | -0.99 | +0.0011 / -0.0062 / -0.0102 | fail |
| `events.root_14_protest/n_events.sum_share_z.60` | 20 | 2494 | -0.0036 | -0.52 | +0.0049 / -0.0050 / -0.0107 | fail |
| `events.root_17_coerce/n_events.sum_share_lr.60` | 1 | 2513 | -0.0037 | -1.13 | -0.0065 / +0.0009 / -0.0056 | fail |
| `events.root_17_coerce/n_events.sum_share_lr.60` | 5 | 2509 | -0.0030 | -0.65 | -0.0025 / +0.0098 / -0.0164 | fail |
| `events.root_17_coerce/n_events.sum_share_lr.60` | 20 | 2494 | -0.0046 | -0.64 | -0.0119 / +0.0139 / -0.0159 | fail |
| `events.root_17_coerce/n_events.sum_share_z.60` | 1 | 2513 | -0.0004 | -0.11 | -0.0035 / +0.0031 / -0.0007 | fail |
| `events.root_17_coerce/n_events.sum_share_z.60` | 5 | 2509 | -0.0029 | -0.61 | -0.0007 / +0.0059 / -0.0139 | fail |
| `events.root_17_coerce/n_events.sum_share_z.60` | 20 | 2494 | -0.0042 | -0.61 | -0.0086 / +0.0092 / -0.0133 | fail |
| `events.root_18_assault/n_events.sum_share_lr.60` | 1 | 2513 | -0.0068 | -2.07 | -0.0064 / -0.0090 / -0.0050 | fail |
| `events.root_18_assault/n_events.sum_share_lr.60` | 5 | 2509 | -0.0097 | -2.19 | -0.0036 / -0.0111 / -0.0146 | fail |
| `events.root_18_assault/n_events.sum_share_lr.60` | 20 | 2494 | -0.0077 | -1.08 | -0.0081 / -0.0041 / -0.0110 | fail |
| `events.root_18_assault/n_events.sum_share_z.60` | 1 | 2513 | +0.0014 | +0.40 | -0.0025 / +0.0036 / +0.0032 | fail |
| `events.root_18_assault/n_events.sum_share_z.60` | 5 | 2509 | -0.0019 | -0.38 | -0.0008 / +0.0004 / -0.0052 | fail |
| `events.root_18_assault/n_events.sum_share_z.60` | 20 | 2494 | +0.0009 | +0.13 | -0.0047 / +0.0077 / -0.0002 | fail |
| `events.root_19_fight/n_events.sum_share_lr.60` | 1 | 2513 | -0.0028 | -0.85 | -0.0023 / -0.0084 / +0.0023 | fail |
| `events.root_19_fight/n_events.sum_share_lr.60` | 5 | 2509 | -0.0014 | -0.28 | +0.0006 / -0.0067 / +0.0018 | fail |
| `events.root_19_fight/n_events.sum_share_lr.60` | 20 | 2494 | +0.0015 | +0.18 | +0.0004 / +0.0012 / +0.0029 | fail |
| `events.root_19_fight/n_events.sum_share_z.60` | 1 | 2513 | -0.0009 | -0.23 | -0.0002 / -0.0058 / +0.0033 | fail |
| `events.root_19_fight/n_events.sum_share_z.60` | 5 | 2509 | +0.0001 | +0.01 | +0.0017 / -0.0060 / +0.0045 | fail |
| `events.root_19_fight/n_events.sum_share_z.60` | 20 | 2494 | +0.0025 | +0.28 | +0.0026 / +0.0003 / +0.0046 | fail |
| `events.root_20_massviolence/n_events.sum_share_lr.60` | 1 | 2511 | +0.0492 | +8.53 | +0.0691 / +0.0487 / +0.0299 | **pass** |
| `events.root_20_massviolence/n_events.sum_share_lr.60` | 5 | 2507 | +0.0696 | +8.62 | +0.0910 / +0.0715 / +0.0464 | **pass** |
| `events.root_20_massviolence/n_events.sum_share_lr.60` | 20 | 2492 | +0.0755 | +6.11 | +0.0925 / +0.0828 / +0.0513 | **pass** |
| `events.root_20_massviolence/n_events.sum_share_z.60` | 1 | 2513 | -0.0067 | -1.92 | -0.0006 / -0.0152 / -0.0044 | fail |
| `events.root_20_massviolence/n_events.sum_share_z.60` | 5 | 2509 | -0.0077 | -1.58 | +0.0002 / -0.0161 / -0.0072 | fail |
| `events.root_20_massviolence/n_events.sum_share_z.60` | 20 | 2494 | -0.0105 | -1.47 | -0.0088 / -0.0120 / -0.0108 | fail |

The `trials` table is append-only, so these 6 rows will keep saying "pass". **This experiment is
the record that they are invalid.** The verification checks reproduced all six pooled ICs
exactly, then decomposed them as follows (h = 1 / 5 / 20).

| control | massviolence `share_lr.60` | protest `share_lr.60` | check |
|---|---|---|---|
| as logged | IC +0.049 / +0.070 / +0.076, t 8.5 / 8.6 / 6.1 | IC -0.013 / -0.022 / -0.024, t -3.9 / -4.4 / -3.4 | all |
| feature replaced by each ticker's full-sample mean (static placebo) | IC +0.186 / +0.258 / +0.280, t 35.7 / 36.5 / 23.8 | IC -0.078 / -0.111 / -0.134, t -22 / -20 / -13 | static, definitions, coverage |
| feature and label demeaned within ticker | IC -0.003 / +0.000 / -0.004, t -0.5 / 0.0 / -0.5 | IC -0.004 / -0.008 / -0.008, t -1.2 / -1.7 / -1.0 | static |
| trailing baseline (no look-ahead), label ln(fwd vol / trailing 20d vol) | t -0.8 / +0.6 / +0.5 | t +0.8 / +0.7 / +0.8 | static |
| raw feature vs vol surprise ln(fwd vol / trailing 60d vol) | IC -0.011 / -0.002 / -0.002, t -2.0 / -0.3 / -0.1 | IC +0.005 / +0.008 / +0.014, t +1.5 / +1.7 / +1.9 (sign flips) | definitions, coverage |
| partial Spearman IC given \|r_t\|, rv5, rv22, rv60, long-run vol | t +0.66 / +0.66 / -0.63 | t -0.71 / -0.33 / -0.06 | clustering |
| pooled log-HAR, date FE and all controls, Driscoll-Kraay | t -2.0 / -1.9 / -2.6 (wrong sign) | t -0.1 / +1.2 / +1.0 | clustering |
| expanding-window OOS incremental R² over HAR + long-run vol | +0.0001 / +0.0000 / +0.0004 (DM t ≤ 1.15) | -0.0000 / +0.0001 / -0.0003 | clustering |
| drop SPY/TLT/IEF/SHY + 7 multi-country ETFs | IC +0.003 / +0.015 / +0.013, t 0.4 / 1.5 / 1.1 | t -2.5 / -2.9 / -2.3 (below gate) | static |
| only the 12 names present every day | t 1.5 / 2.0 / 1.3 | t 0.3 / 0.2 / 0.2 | coverage |

How the artefact works:

- **`lr` selects the sample by sparsity.** `lr = ln(x / m)` is NaN when x = 0 or m = 0. For
  mass violence (zero on a median 83% of ETF-days), `share_lr.60` has 57,038 rows against
  128,037 for `share_z.60`, which is a median of 22 names per day against 51. A sparse country
  appears only on spike days, with a large positive value.
  - Across tickers, Spearman(mean lr, coverage) = -0.975.
  - Commodity ETFs (about 2,510 days each) sit near -0.12. EWT/EWH/UAE/EWS (75 to 120 days)
    average +2.7 to +3.3.
  - On a stationary Poisson series that carries no information, mean lr is +3.11 at λ = 0.05,
    +0.88 at 0.5 and -0.01 at 50, while mean z stays at about 0.
  - In other words, lr is a per-ticker sparsity label.
- **The label is a vol level.** It is compared across bonds, commodities and equity ETFs. Any
  persistent per-ticker offset therefore ranks with it.
  - SPY, TLT, IEF and SHY all map to FIPS `['US']`, so their feature values are identical
    (max abs difference 0.0), while their vols differ by about 10x.
  - SHY and IEF alone contribute +0.064 of the +0.0755 massviolence IC at h=20.
- **The feature reflects past vol as well as future vol.** Massviolence lr has a cross-sectional
  IC with *trailing* 20d vol of +0.074 (t 9.3). That is larger than its h=1 forward IC.
- **Why the z versions disagree.** `z` removes the static level: between-ticker variance share is
  0.08% (massviolence z) against 45% (massviolence lr).
- **Protest lr is the wrong sign as well.** Its negative pooled IC contradicts A4's pre-registered
  positive sign. It is entirely static: its per-ticker time-series correlation has the opposite
  sign.
- The only weak residual found is massviolence `share_z.60` after vol controls: partial t +2.4
  at h=5 and +2.2 at h=20. That is below the gate and not robust in the panel or OOS checks.
  **It is not a pass.**

**Timing is clean** (definitions check):

- The `surprise()` baseline is rows t-60..t-1. Perturbing rows at t+1 or later changes no feature
  at t or earlier.
- The vol label at t uses r_{t+1..t+h} with r_{t+1} = ln(C_{t+1}/C_t).
- Stored return labels equal `make_labels(prices)` exactly (max |diff| 0.0 on 476,118 Track A rows
  and 6,441,300 Track B rows).
- An exact future return as the feature gives IC 1.0000; shifted by one day it gives -0.003 (A).

There is **no look-ahead leak**. The A4 result is a cross-sectional design artefact.

Correction to one check: the definitions check said 9 tickers (SPY, SHY, IEF, TLT, GLD, DBA, CPER,
UNG, USO) map to `['US']`. The `assets` table shows only the first four do. The five commodity ETFs
have multi-country lists that *include* US, so their features are not tied to SPY's. Its ex-US
ICs (sign flip to t -5.4) used "fips contains US". Its `fips == ['US']` variant also flips
(-0.031 / -0.053 / -0.059), and both variants vanish after within-ticker demeaning, so the
conclusion does not change.

### Track B, direction (`ret_oo`): 0 of 21 pass

| hyp | feature | h | days | IC | NW t | sub-period IC | spread bp | cost bp | fails |
|---|---|---|---|---|---|---|---|---|---|
| B1 | `gkg.org_docs.mean_z.60` | 1 | 2513 | -0.0004 | -0.36 | -0.0014 / -0.0014 / +0.0017 | +0.2 | 28.0 | t,cost |
| B1 | `gkg.org_docs.mean_z.60` | 5 | 2509 | -0.0012 | -0.85 | -0.0038 / +0.0000 / +0.0002 | +1.9 | 40.0 | t,sub,cost |
| B1 | `gkg.org_docs.mean_z.60` | 20 | 2494 | -0.0016 | -0.96 | -0.0022 / +0.0013 / -0.0038 | +6.7 | 40.0 | t,cost |
| B1 | `gkg.org_docs.mean_z.60*gkg.org_tone.mean.0` | 1 | 2513 | +0.0003 | +0.29 | -0.0017 / -0.0003 / +0.0028 | +0.8 | 30.1 | t,sub,cost |
| B1 | `gkg.org_docs.mean_z.60*gkg.org_tone.mean.0` | 5 | 2509 | -0.0014 | -1.12 | -0.0055 / +0.0006 / +0.0006 | -1.1 | 40.0 | t,sub,cost |
| B1 | `gkg.org_docs.mean_z.60*gkg.org_tone.mean.0` | 20 | 2494 | +0.0015 | +0.89 | -0.0011 / +0.0049 / +0.0006 | +6.8 | 40.0 | t,cost |
| B1 | `gkg.org_tone.mean.0` | 1 | 2533 | +0.0035 | +3.14 | +0.0062 / +0.0038 / +0.0004 | -0.8 | 25.8 | cost |
| B1 | `gkg.org_tone.mean.0` | 5 | 2529 | +0.0052 | +2.42 | +0.0104 / +0.0061 / -0.0009 | -0.2 | 40.0 | t,cost |
| B1 | `gkg.org_tone.mean.0` | 20 | 2514 | +0.0033 | +0.90 | +0.0074 / +0.0054 / -0.0029 | -9.3 | 40.0 | t,cost |
| B1 | `gkg.org_tone.mean_z.60` | 1 | 2513 | +0.0001 | +0.10 | +0.0012 / -0.0010 / +0.0000 | -0.2 | 29.3 | t,cost |
| B1 | `gkg.org_tone.mean_z.60` | 5 | 2509 | +0.0015 | +1.16 | +0.0041 / +0.0001 / +0.0001 | +1.0 | 40.0 | t,cost |
| B1 | `gkg.org_tone.mean_z.60` | 20 | 2494 | -0.0029 | -1.71 | -0.0030 / -0.0051 / -0.0006 | +8.8 | 40.0 | t,cost |
| B2 | `gkg.org_neg_docs.mean_z.60` | 1 | 2513 | -0.0008 | -0.72 | -0.0020 / +0.0009 / -0.0013 | -0.3 | 23.4 | t,cost |
| B2 | `gkg.org_neg_docs.mean_z.60` | 5 | 2509 | -0.0022 | -1.46 | -0.0039 / +0.0016 / -0.0043 | +1.8 | 40.0 | t,cost |
| B2 | `gkg.org_neg_docs.mean_z.60` | 20 | 2494 | +0.0011 | +0.48 | +0.0025 / +0.0066 / -0.0059 | +8.1 | 40.0 | t,cost |
| B2 | `gkg.org_neg_docs.sum_lr.60` | 1 | 2513 | -0.0003 | -0.15 | -0.0015 / +0.0042 / -0.0035 | +1.2 | 27.2 | t,cost |
| B2 | `gkg.org_neg_docs.sum_lr.60` | 5 | 2509 | -0.0001 | -0.04 | -0.0005 / +0.0050 / -0.0048 | +2.9 | 40.0 | t,cost |
| B2 | `gkg.org_neg_docs.sum_lr.60` | 20 | 2494 | +0.0032 | +0.66 | +0.0116 / +0.0096 / -0.0115 | +7.4 | 40.0 | t,cost |
| B2 | `gkg.org_neg_docs.sum_z.60` | 1 | 2513 | -0.0010 | -1.08 | -0.0013 / +0.0008 / -0.0025 | +0.6 | 19.3 | t,cost |
| B2 | `gkg.org_neg_docs.sum_z.60` | 5 | 2509 | -0.0013 | -0.88 | -0.0021 / +0.0006 / -0.0023 | +1.8 | 40.0 | t,cost |
| B2 | `gkg.org_neg_docs.sum_z.60` | 20 | 2494 | -0.0006 | -0.24 | +0.0027 / +0.0011 / -0.0056 | -3.2 | 40.0 | t,sub,cost |

The best Track B result is `gkg.org_tone.mean.0` at h=1, which has t +3.14 but a *negative*
spread (-0.8 bp). Its sign is decaying (sub-periods +0.0062 / +0.0038 / +0.0004). It is also
confounded with coverage: mean tone is +1.09 on 1-doc stock-days and -0.17 on stock-days with more
than 100 docs. So its IC partly reflects a size or attention tilt. The power check bounds its true
h=1 IC at about 0.0035 + 2·0.0011 ≈ 0.006.

### Positive controls and minimum detectable IC (power check)

**Pipeline correctness:**

- Injected ICs are recovered at about 0.94 to 1.0x through `evaluate_signal`.
- The null has the right size: P(|t| ≥ 3) = 0 in 20 Track A and 8 Track B seeds at each horizon.

**Price-anomaly positive controls**, run through the same gate with no logging. All 18 fail
(9 per track):

| control | Track A IC (t), h=1 / 5 / 20 | Track B IC (t), h=1 / 5 / 20 |
|---|---|---|
| 5-day reversal | +0.010 (2.4) / +0.014 (1.7) / +0.012 (1.2) | +0.009 (2.8) / +0.015 (2.5) / +0.014 (2.1) |
| 12-1 momentum | +0.008 (1.6) / +0.010 (0.9) / -0.000 (-0.0) | +0.012 (2.9) / +0.010 (1.0) / -0.002 (-0.1) |
| 1-month momentum | -0.002 (-0.4) / -0.008 (-0.8) / -0.019 (-1.2) | -0.005 (-1.6) / -0.013 (-1.9) / -0.017 (-1.6) |

In 2015-2025, known anomalies have IC of about 0.01 with t of 1 to 3 in this gate. They are
therefore *not* usable as a "should pass" control. They also show that a factor-structured signal
needs IC of about 0.013 to 0.03 to reach |t| ≥ 3.

**MDE.** MDE50 is the true IC detected at |t| ≥ 3 with 50% power. It is computed with real stored
features as the noise carrier, as 3·se, where se = IC / NW t. MDE80 = 3.84·se.

| | h=1 | h=5 | h=20 |
|---|---|---|---|
| Track A MDE50, median (worst) over 14 features | 0.010 (0.017) | 0.015 (0.027) | 0.021 (0.045) |
| Track A MDE80, median (worst) | 0.013 (0.021) | 0.019 (0.034) | 0.027 (0.057) |
| Track B MDE50, median over 7 features | 0.005 | 0.007 | 0.011 |
| IC needed for the spread to clear cost, Track A (turnover ~0.6/day) | ~0.049 | ~0.036 | ~0.017 |
| IC needed for the spread to clear cost, Track B | ~0.054 | ~0.030 | ~0.015 |

Reading the table:

- The worst Track A case is massviolence `share_lr.60` (22.7 names per day). At a true IC of
  0.01 it has P(|t| ≥ 3) of only 0.25 / 0.20 / 0.10.
- **At h=1 and h=5, the cost leg needs IC of 0.03 to 0.05.** That is above the ADR's own prior
  (0.01 to 0.03), so at the prior's effect sizes no feature could pass there.
- The informative part of the null is the IC leg:
  - At h=1 it excludes true IC of about 0.017 or more for every Track A feature, and about 0.006
    or more for the best Track B feature.
  - Track A at h=20 only excludes IC of about 0.02 to 0.045.

### Coverage

**Track A:**

- `n_events` is dense for all 52 country-mapped ETFs: zero-event days are under 0.1%, and the
  lowest median is 171 events per day (EWH).
- `root_20` mass violence is zero on a median 83% of ETF-days, and 45 of the 63 ETFs have more
  than 50% zero days.
- `root_14` protest is zero on a median 1.8% of days.
- The 11 sector ETFs have no features. Their theme rows were never pulled, and only XLE has
  countries.

**Track B (GKG `V2Organizations` exact-alias match):**

- 56.4% of priced stock-days have at least 1 doc, varying between 48.5% (2021) and 63.3% (2018)
  with no trend.
- GKG volume *falls* over the period, from a median of 775k docs per day (2016) to 294k (2022).
- 54 stocks have **zero** docs over the entire dev period, including T, MMM, BKNG, STT and PSX.
  126 stocks have docs on fewer than 5% of days.
- Dictionary-word brands are nearly invisible: AMZN 13,548 docs in 10 years, BAC 2,933, UBER
  725, against MSFT 5.15M.
- Coverage is concentrated: the top 10 stocks hold 41.9% of matched docs and the top 50 hold
  75.7%.
- Feature cross-sections (median names per day):

  | feature | names/day |
  |---|---|
  | `org_docs.mean_z.60` | 470 |
  | `org_tone.mean.0` | 490 |
  | `org_neg_docs.sum_z.60` | 609 |
  | `org_neg_docs.sum_lr.60` | 184 |

- 48% of `org_docs.mean_z.60` rows rest on 5 or fewer docs. Only about 260 names per day have 5
  or more.
- False-positive aliases:
  - `eaton` matches the January 2025 Eaton Fire (12,396 docs in one week against about 100 per
    week before).
  - `pinterest` (501k), `morningstar inc` (138k), `nasdaq inc` (77k) and `gartner inc` (47k)
    look like boilerplate or citation mentions. The bare `nasdaq` and `gartner` are flagged
    ambiguous, but their `... inc` forms pass.
- Share-class pairs (GOOG/GOOGL, FOX/FOXA, NWS/NWSA) get identical features.

## Signals that survive and are eligible for gate 2

**None.** Every direction trial failed the gate as implemented. The six A4 passes do not survive
any of these controls:

- ticker demeaning (max |t| 1.7);
- a trailing-baseline label (max |t| 2.3);
- HAR plus long-run vol controls (max |partial t| 0.71);
- OOS incremental R² (≤ 0.0004);
- dropping the tied US block and the multi-country ETFs.

Do not backtest these features or take them to the hold-out. Doing so would spend trials and
hold-out credibility on a known artefact.

## Suspected bugs to fix before any further trial

Listed most important first. Each fix that changes a gate or a feature definition changes the
hypothesis being tested. It has to be pre-registered through an ADR amendment before new trials are
logged, and those trials add to the same DSR count (96 signal trials so far).

1. **A4 gate tests the vol level, not the vol shock** (`cli/pipelines.py:464-494`,
   `_signal_study_vol`). It takes a raw cross-sectional IC of the feature against the forward
   realised-vol level, with no ticker fixed effect and no control for trailing vol. Repro: a
   static per-ticker mean of the feature gives t = +35.7 at h=1. Fix options:
   - use a vol-surprise label, ln(fwd vol / trailing vol);
   - or use a partial IC given rv1/rv5/rv22/long-run vol.

   Either way, add the static-mean placebo and a ticker-demeaned IC as standard diagnostics.
2. **Gates ignore the pre-registered sign** (`core/evaluate.py:241` `sign = 1.0 if t > 0 else -1.0`,
   `cli/pipelines.py:482`, and `signal_study` discarding the sign from `hypothesis_of` at line 441).
   - Protest lr was logged as an A4 pass with the sign opposite to ADR 0001.
   - A1's best features are positive against a pre-registered negative sign.

   The gate should require the pre-registered sign where one exists (`none` / `same as tone`
   need an explicit rule).
3. **`lr` transform is a sparsity selector** (`core/features.py:194-196`). It is NaN when x = 0 or
   m = 0, so for sparse counts the feature exists only on spike days. It also drops the first
   event after a quiet window (m = 0). Spearman(coverage, mean lr) = -0.975, and the Poisson null
   gives mean lr +3.1 at λ = 0.05. Fix: use a pseudo-count, ln((x+k)/(m+k)), or drop lr for
   sparse metrics.
4. **`z` guard misses zero-variance windows** (`core/features.py:193`, `s.where(s > 0)`). Pandas
   rolling std leaves residue (6.35e-13) on all-zero windows, so stored z reaches 1.31e7 (EPHE
   massviolence, 2022-10-13). There are 327 rows with |z| > 10. Rank ICs barely move, but quantile
   spreads, Pearson IC and the models would be distorted. Fix: a relative tolerance or a floor on
   std.
5. **GKG attention share divided by the number of aliases hit** (`core/features.py:98-114`
   `map_to_tickers` summing `n` across aliases, then `mean = vs / n` at line 146). A ticker that
   hits k aliases gets docs / (k·N). 42.7% of ticker-days have k > 1, and k correlates +0.46
   with docs within a ticker, so spikes are damped. The pipeline `org_docs.mean_z.60` has a
   daily rank correlation of only 0.79 with the intended share z. Affects B1 and the B1 product.
6. **Zero-mention days are NaN, not 0, for attention** (`core/features.py` `_raw_wide`, about
   lines 283-290, with the mean at line 146). The attention z is defined only on mention days
   (469 names per day against 755 if zero-filled), so quiet-then-spike, the B1 case, is lost for
   sparse names.
7. **Universe and alias config.**
   - SPY/TLT/IEF/SHY carry identical event features. Dedupe them or cluster them in ICs.
   - The 11 sector ETFs have no features until themes are pulled.
   - Flag `eaton` and the `<ambiguous> inc` alias forms.
   - Dedupe share classes.
8. **Gate cost wording vs code (decision needed, not a code defect).** ADR 0001 says "positive
   after one-way costs", but the code charges 4·cost·min(1, h·turnover) for a $1-long/$1-short
   book. With about 0.6 daily turnover, the cost leg is unpassable at the prior's effect sizes
   for h = 1 and 5. Separately, the IC is rank-based while the spread uses raw mean returns, so
   the two can disagree in sign (B tone h=1: IC +, spread -).

## What this does not establish

- **Not that GDELT news carries no information for these assets.** The test covers the features
  that were built (14 Track A, 7 Track B), with one 60-day baseline, three horizons, one cutoff
  (15:30 ET) and one label type (open-to-open). Weekly aggregation beyond A3, other baselines,
  intraday labels and non-linear combinations were not tested.
- **A2 was not tested.** No GKG theme rows are loaded (about 1 TB for the dev range, deferred to
  a later billing month), so the commodity/energy supply-shock hypothesis has no trial. Theme
  features for the sector ETFs do not exist either.
- **Track B is weak evidence.** Only 7 features were tested. Alias recall misses large
  dictionary-word brands, half the cross-section rests on 5 or fewer docs, and bugs 5 and 6
  distort the attention feature. The null rules out *this construction*, not org-news
  predictability.
- **Survivorship.** Track B uses today's S&P 500/400 members over 2015-2025. That inflates any
  positive result; it does not rescue a null.
- **Economic vs statistical null.** At h=1 and h=5 the cost leg could not be passed at the
  prior's IC (0.01 to 0.03). Failing the gate there says nothing beyond the IC-leg bounds in the
  power table. Track A at h=20 and sparse features are underpowered (MDE50 0.021 median, 0.045
  worst).
- **The A4 rejection is about these two lr features under a level label.** A properly specified
  vol-surprise test of event shocks has not been run. The only residual (massviolence z, partial
  t about 2.2 to 2.4) is below the gate and would be a new, pre-registered trial.
- **The verification checks are diagnostics, not trials.** They used dev data that gate 1 had
  already seen, and their own specification choices (control sets, exclusion lists) were made
  after seeing the A4 result. They can invalidate a pass. They cannot create one.
- The verification scripts are scratch files outside the repo. The numbers here are what they
  printed on 2026-10-05; rerunning them requires the same store snapshot.
