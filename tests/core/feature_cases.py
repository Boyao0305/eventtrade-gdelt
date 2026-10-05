"""Case tables for core/features.py and core/labels.py — the specification, written by hand.

Each group is one rule; the comment above it is the rule in prose.
"""

from datetime import UTC, date, datetime
from math import log, nan, sqrt

D = date

# NYSE sessions around three holidays: Good Friday 2024-03-29, Independence Day Thu 2024-07-04,
# and the Memorial Day long weekend (Mon 2024-05-27).
TRADING_DAYS = [
    D(2024, 3, 25), D(2024, 3, 26), D(2024, 3, 27), D(2024, 3, 28),             # Good Friday 3-29
    D(2024, 4, 1), D(2024, 4, 2),
    D(2024, 5, 23), D(2024, 5, 24),                                            # Memorial Day 5-27
    D(2024, 5, 28),
    D(2024, 7, 1), D(2024, 7, 2), D(2024, 7, 3),                                # holiday 7-04
    D(2024, 7, 5), D(2024, 7, 8), D(2024, 7, 9),
]

# --- Cutoff: DATEADDED (UTC) -> New York session date, cutoff 15:30 ET inclusive ---------------
# At or before 15:30 New York time -> that calendar day; after -> the next calendar day.
# DST matters: 15:30 ET is 19:30 UTC in summer (EDT) and 20:30 UTC in winter (EST).
# (utc datetime, expected session_date)
SESSION_DATE_CASES = [
    (datetime(2024, 7, 5, 19, 0, tzinfo=UTC), D(2024, 7, 5)),    # Fri 15:00 EDT -> Friday
    (datetime(2024, 7, 5, 19, 30, tzinfo=UTC), D(2024, 7, 5)),   # Fri 15:30 EDT exactly -> Friday (inclusive)
    (datetime(2024, 7, 5, 19, 45, tzinfo=UTC), D(2024, 7, 6)),   # Fri 15:45 EDT -> Saturday
    (datetime(2024, 7, 5, 20, 0, tzinfo=UTC), D(2024, 7, 6)),    # Fri 16:00 EDT -> Saturday (-> Monday below)
    (datetime(2024, 7, 6, 2, 0, tzinfo=UTC), D(2024, 7, 6)),     # Fri 22:00 EDT (UTC already Sat) -> Saturday
    (datetime(2024, 7, 8, 3, 0, tzinfo=UTC), D(2024, 7, 8)),     # Sun 23:00 EDT -> Monday (after cutoff)
    (datetime(2024, 7, 8, 13, 0, tzinfo=UTC), D(2024, 7, 8)),    # Mon 09:00 EDT -> Monday
    (datetime(2024, 1, 5, 20, 0, tzinfo=UTC), D(2024, 1, 5)),    # Fri 15:00 EST (winter) -> Friday
    (datetime(2024, 1, 5, 20, 45, tzinfo=UTC), D(2024, 1, 6)),   # Fri 15:45 EST -> Saturday
]

# --- Roll forward: session_date -> first trading day >= session_date ---------------------------
# Trading days map to themselves; weekend and holiday sessions roll to the next session.
# Sessions outside the calendar are dropped (None): after the last day there is no session yet;
# before the first day the previous session (and so the row's true bucket) is unknown.
# (session_date, expected trading day or None)
ROLL_FORWARD_CASES = [
    (D(2024, 7, 5), D(2024, 7, 5)),     # Friday trading day -> itself
    (D(2024, 7, 6), D(2024, 7, 8)),     # Saturday -> Monday (the Fri 16:00 ET row ends here)
    (D(2024, 7, 7), D(2024, 7, 8)),     # Sunday -> Monday
    (D(2024, 7, 4), D(2024, 7, 5)),     # Thursday holiday -> Friday
    (D(2024, 3, 29), D(2024, 4, 1)),    # Good Friday -> Monday
    (D(2024, 3, 31), D(2024, 4, 1)),    # Easter Sunday -> Monday
    (D(2024, 5, 25), D(2024, 5, 28)),   # Saturday of a long weekend -> Tuesday
    (D(2024, 5, 27), D(2024, 5, 28)),   # Memorial Day -> Tuesday
    (D(2024, 4, 3), D(2024, 5, 23)),    # gap in this calendar -> next listed session
    (D(2024, 7, 10), None),             # after the last trading day -> dropped
    (D(2024, 3, 24), None),             # before the first trading day -> dropped
]

# --- Re-aggregation is exact: value_sum and n are summed, means are recomputed from the sums ----
# Sat + Sun + Mon rows of one (source, entity, metric) collapse into one Monday row whose mean is
# sum(value_sum) / sum(n) — not the mean of the three daily means.
# rows: (session_date, value_sum, n); expected: (trading day, value_sum, n, mean)
REAGG_ROWS = [(D(2024, 7, 6), 3.0, 2), (D(2024, 7, 7), -1.0, 1), (D(2024, 7, 8), 4.0, 4)]
REAGG_EXPECTED = (D(2024, 7, 8), 6.0, 7, 6.0 / 7.0)
REAGG_WRONG_MEAN_OF_MEANS = (1.5 - 1.0 + 1.0) / 3

# --- Surprise baseline excludes day t (feature definitions v2, ADR 0002 item 1) -----------------
# z_t = (x_t - mean(x[t-shift-window+1 .. t-shift])) / std(same, ddof=1); fewer than min_periods
# baseline values -> NaN; baseline std below 1e-9 -> NaN; z clipped to +-10.
# lr_t = ln((x_t + 1) / (baseline mean + 1)) with pseudo-count 1 (default k): defined on zero days.
# (values, window, min_periods, shift, expected z per row, expected lr per row)
SURPRISE_CASES = [
    # baseline [1,2,3]: mean 2, std 1 -> z = 8; with day t included it would be ~1.5
    ([1.0, 2.0, 3.0, 10.0], 3, 3, 1, [nan, nan, nan, 8.0], [nan, nan, nan, log(11.0 / 3.0)]),
    # rolling: row 4's baseline is [2,3,10] (row 0 dropped)
    ([1.0, 2.0, 3.0, 10.0, 5.0], 3, 3, 1,
     [nan, nan, nan, 8.0, (5.0 - 5.0) / sqrt(19.0)], [nan, nan, nan, log(11.0 / 3.0), 0.0]),
    # min_periods 2 lets row 2 through on baseline [1,2]
    ([1.0, 2.0, 4.0], 3, 2, 1, [nan, nan, (4.0 - 1.5) / sqrt(0.5)], [nan, nan, log(5.0 / 2.5)]),
    # constant baseline -> std 0 -> z NaN; lr still defined
    ([2.0, 2.0, 2.0, 4.0], 3, 3, 1, [nan, nan, nan, nan], [nan, nan, nan, log(5.0 / 3.0)]),
    # shift 2 (e.g. 2-day sums): baseline for row 3 is rows 0..1 only
    ([1.0, 3.0, 100.0, 4.0], 2, 2, 2, [nan, nan, nan, (4.0 - 2.0) / sqrt(2.0)],
     [nan, nan, nan, log(5.0 / 3.0)]),
    # row 2: first event after an all-zero baseline -> lr ln 2 (v1: NaN, m = 0);
    # row 3: zero value on a non-zero baseline -> lr finite and negative (v1: NaN, x = 0)
    ([0.0, 0.0, 1.0, 0.0], 2, 2, 1, [nan, nan, nan, (0.0 - 0.5) / sqrt(0.5)],
     [nan, nan, log(2.0), log(1.0 / 1.5)]),
    # zero day on an all-zero baseline: z NaN (no variance), lr = ln(1/1) = 0 (v1: NaN)
    ([0.0, 0.0, 0.0, 0.0], 3, 3, 1, [nan, nan, nan, nan], [nan, nan, nan, 0.0]),
    # clip: raw z = 98 -> +10, raw z = -11 -> -10
    ([1.0, 2.0, 3.0, 100.0], 3, 3, 1, [nan, nan, nan, 10.0], [nan, nan, nan, log(101.0 / 3.0)]),
    ([10.0, 11.0, 12.0, 0.0], 3, 3, 1, [nan, nan, nan, -10.0], [nan, nan, nan, log(1.0 / 12.0)]),
    # std floor: baseline [0, 1e-10, 0] has std 5.8e-11 < 1e-9 -> z NaN (v1: z = 1.15)
    ([0.0, 1e-10, 0.0, 1e-10], 3, 3, 1, [nan, nan, nan, nan],
     [nan, nan, nan, log((1.0 + 1e-10) / (1.0 + 1e-10 / 3.0))]),
]

# --- Regression inputs from experiment 0003 ----------------------------------------------------
# Bug 4, EPHE-like: a share series with some history, then an all-zero 60-day window, then a
# small spike. Pandas' rolling std leaves residue (~1e-10) on the all-zero window, so v1's
# `s > 0` guard let z reach ~1.9e7 (EPHE massviolence 2022-10-13: 1.31e7). v2: z is NaN there and
# |z| <= 10 everywhere. Head = np.random.default_rng(EPHE_SEED).random(40) * 0.01.
EPHE_SEED = 1
EPHE_HEAD_LEN, EPHE_ZEROS, EPHE_SPIKE = 40, 60, 0.002
EPHE_WINDOW, EPHE_MIN_PERIODS = 60, 20

# Bug 3, quiet-then-spike: a sparse count that appeared once, then 60 zero days, then a spike.
# v1 lr = ln(x / m) is NaN because m = 0 (the first event after a quiet window was dropped).
# v2 lr = ln((40 + 1) / (0 + 1)) = ln 41, finite and large; z is NaN (all-zero baseline).
SPIKE_FIRST, SPIKE_ZEROS, SPIKE_VALUE = 3.0, 60, 40.0
SPIKE_WINDOW, SPIKE_MIN_PERIODS = 60, 20
SPIKE_LR = log(SPIKE_VALUE + 1.0)

# Bugs 5 and 6, MSFT-like GKG org attention. org_docs rows: value_sum = docs mentioning the alias,
# n = ALL GKG docs that session day (the same on every row of the day).
# Day 0 (Mon 2024-07-01): MSFT hits three aliases; another org is also present.
# Day 1 (Tue 2024-07-02): no MSFT alias row (zero mentions); the day's total is known from "apple".
# Sat 2024-07-06 + Sun 2024-07-07 + Mon 2024-07-08 roll into Monday: MSFT only on Sat and Mon,
# "apple" only on Sun, so Monday's denominator must be the sum of all three day totals.
# rows: (session_date, entity, docs, day_total_docs)
MSFT_ALIASES = ["microsoft", "microsoft corp", "microsoft corporation"]
MSFT_ORG_ROWS = [
    (D(2024, 7, 1), "microsoft", 120.0, 400_000), (D(2024, 7, 1), "microsoft corp", 30.0, 400_000),
    (D(2024, 7, 1), "microsoft corporation", 10.0, 400_000),
    (D(2024, 7, 1), "apple", 5.0, 400_000),
    (D(2024, 7, 2), "apple", 7.0, 350_000),
    (D(2024, 7, 6), "microsoft", 4.0, 100_000),
    (D(2024, 7, 7), "apple", 2.0, 90_000),
    (D(2024, 7, 8), "microsoft corp", 6.0, 410_000),
]
MSFT_DAYS = [D(2024, 7, 1), D(2024, 7, 2), D(2024, 7, 3), D(2024, 7, 5), D(2024, 7, 8)]
# expected gkg.org_docs.mean (attention share) for MSFT on MSFT_DAYS:
# d0: (120+30+10) / 400k, v1 gave 160 / (3 * 400k); d1: 0 (no mention = 0), v1 gave NaN;
# 07-03 and 07-05: no GKG rows at all -> unobserved -> NaN; 07-08: (4+6) / (100k+90k+410k).
MSFT_SHARE = [160.0 / 400_000, 0.0, nan, nan, 10.0 / 600_000]
MSFT_SHARE_V1_DAY0 = 160.0 / 1_200_000
# org_tone rows for the same two days: tone mean stays NaN on the zero day.
# (session_date, entity, tone_sum, docs)
MSFT_TONE_ROWS = [(D(2024, 7, 1), "microsoft", -12.0, 120), (D(2024, 7, 1), "microsoft corp", 3.0, 30),
                  (D(2024, 7, 2), "apple", 1.0, 7)]
MSFT_TONE = [-9.0 / 150.0, nan]

# --- sm5: 5-row trailing mean of the v2 z feature, rows t-4..t, all five required -------------
# Baseline window 2: x alternates 0, 2 so every baseline is {0, 2}: mean 1, std sqrt 2, and
# z alternates -c, +c from row 2 (c = 1/sqrt 2). sm5 exists from row 6 = mean of rows 2..6.
_C = 1.0 / sqrt(2.0)
SM5_VALUES = [0.0, 2.0, 0.0, 2.0, 0.0, 2.0, 0.0, 2.0, 0.0]
SM5_WINDOW, SM5_MIN_PERIODS = 2, 2
SM5_EXPECTED = [nan] * 6 + [-_C / 5.0, _C / 5.0, -_C / 5.0]

# --- Statistics per (date, entity), with share normalised by the same-day _GLOBAL total ----------
# mean = value_sum/n (NaN when n = 0); share = n / n(_GLOBAL); count-like stats are 0 on an
# observed day after the entity's first row, NaN before it; a day with no rows of the source at
# all is unobserved -> NaN for everything.
# Rows on days d0..d3 (trading days). Entity JA first appears on d1; the source is silent on d2.
STAT_DAYS = [D(2024, 7, 1), D(2024, 7, 2), D(2024, 7, 3), D(2024, 7, 5)]
STAT_ROWS = [  # (day index, entity, value_sum, n)
    (0, "_GLOBAL", -10.0, 100),
    (1, "_GLOBAL", -20.0, 200), (1, "JA", -6.0, 4),
    (3, "_GLOBAL", -5.0, 50),
]
# (stat, entity, expected values on d0..d3)
STAT_CASES = [
    ("count", "JA", [nan, 4.0, nan, 0.0]),          # NaN before first row; 0 after; NaN unobserved
    ("share", "JA", [nan, 4.0 / 200.0, nan, 0.0]),
    ("mean", "JA", [nan, -1.5, nan, nan]),          # n = 0 -> mean undefined
    ("sum_share", "JA", [nan, 6.0 / 20.0, nan, 0.0]),
    ("share", "_GLOBAL", [1.0, 1.0, nan, 1.0]),
]
# agg_days = 2: raw sums over the last 2 grid days first, any NaN -> NaN.
AGG2_CASES = [
    ("count", "_GLOBAL", [nan, 300.0, nan, nan]),
    ("mean", "_GLOBAL", [nan, -30.0 / 300.0, nan, nan]),
]

# --- Mapping entities to tickers: sums of raw value_sum and n --------------------------------
# A stock sums its org_aliases; an ETF sums its fips_countries and gkg_themes, so its mean is the
# n-weighted (article-weighted) mean across entities, not the mean of entity means.
# rows: (entity, value_sum, n); expected per ticker: (value_sum, n)
MAP_ROWS = [("apple", 2.0, 1), ("apple inc", 0.0, 3), ("JA", 2.0, 1), ("KS", 0.0, 3),
            ("ENV_OIL", 5.0, 5), ("unmapped", 9.0, 9)]
MAP_EXPECTED = {"AAPL": (2.0, 4), "EWJ": (2.0, 1), "AAXJ": (2.0, 4), "XLE": (5.0, 5)}
MAP_ETF_MEAN = 2.0 / 4.0          # AAXJ: n-weighted
MAP_ETF_MEAN_OF_MEANS = 1.0       # what an unweighted combination would give (wrong)

# --- Labels: entry at t+1 open; positional on the shared trading calendar ----------------------
# ret_oo(h) = adj_open[t+1+h]/adj_open[t+1]-1; ret_overnight = adj_open[t+1]/adj_close[t]-1;
# ret_intraday = adj_close[t+1]/adj_open[t+1]-1. A missing bar is NaN for every label needing it,
# never skipped and never forward-filled; bars past the end of the data are missing.
LABEL_DAYS = [D(2024, 7, 1), D(2024, 7, 2), D(2024, 7, 3), D(2024, 7, 5), D(2024, 7, 8)]
# (ticker, day index) -> (adj_open, adj_close). Ticker BBB has no bar on day 2.
LABEL_PRICES = {
    ("AAA", 0): (10.0, 11.0), ("AAA", 1): (12.0, 13.0), ("AAA", 2): (15.0, 14.0),
    ("AAA", 3): (18.0, 20.0), ("AAA", 4): (24.0, 25.0),
    ("BBB", 0): (100.0, 100.0), ("BBB", 1): (110.0, 105.0),
    ("BBB", 3): (90.0, 99.0), ("BBB", 4): (80.0, 81.0),
}
# (ticker, day index, horizon) -> (ret_oo, ret_overnight, ret_intraday)
LABEL_CASES = {
    ("AAA", 0, 1): (15.0 / 12.0 - 1, 12.0 / 11.0 - 1, 13.0 / 12.0 - 1),
    ("AAA", 0, 2): (18.0 / 12.0 - 1, 12.0 / 11.0 - 1, 13.0 / 12.0 - 1),
    ("AAA", 2, 1): (24.0 / 18.0 - 1, 18.0 / 14.0 - 1, 20.0 / 18.0 - 1),
    ("AAA", 3, 1): (nan, 24.0 / 20.0 - 1, 25.0 / 24.0 - 1),   # exit bar past the data
    ("AAA", 4, 1): (nan, nan, nan),                           # no next bar at all
    # BBB: day-2 bar missing
    ("BBB", 0, 1): (nan, 110.0 / 100.0 - 1, 105.0 / 110.0 - 1),  # exit open missing
    ("BBB", 0, 2): (90.0 / 110.0 - 1, 110.0 / 100.0 - 1, 105.0 / 110.0 - 1),
    ("BBB", 1, 1): (nan, nan, nan),                           # entry bar missing: never skipped
    ("BBB", 3, 1): (nan, 80.0 / 99.0 - 1, 81.0 / 80.0 - 1),
}
# No label row for a (date, ticker) without a bar: (BBB, day 2) is absent.
LABEL_ABSENT = [("BBB", 2)]

# --- Realised vol: r_s = ln(close_s/close_{s-1}), s in t+1..t+h; zero-mean RMS by default -----
# (ticker, day index, horizon, demean) -> value
_lr = lambda a, b: log(b / a)
VOL_CASES = {
    ("AAA", 0, 1, False): abs(_lr(11.0, 13.0)),
    ("AAA", 0, 2, False): sqrt((_lr(11.0, 13.0) ** 2 + _lr(13.0, 14.0) ** 2) / 2),
    ("AAA", 0, 2, True): abs(_lr(11.0, 13.0) - _lr(13.0, 14.0)) / sqrt(2.0),
    ("AAA", 0, 1, True): nan,                     # sample std of one return
    ("AAA", 3, 2, False): nan,                    # runs past the data
    ("BBB", 0, 2, False): nan,                    # crosses the missing day-2 close
    ("BBB", 3, 1, False): abs(_lr(99.0, 81.0)),
}
