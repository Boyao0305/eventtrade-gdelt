"""Case tables for core/evaluate.py — the signal gate of ADR 0001."""

import math

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.evaluate import (
    assign_quantiles,
    daily_ic,
    evaluate_signal,
    ic_by_return_leg,
    newey_west_t,
    passes_gate,
    quantile_spread,
    quantile_turnover,
    subperiod_signs,
)
from eventtrade.schemas import FEATURE_COLUMNS, LABEL_COLUMNS, SignalReport

D1, D2, D3 = pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-03"), pd.Timestamp("2020-01-06")


def _frames(rows: dict, horizon: int = 1, feature: str = "f") -> tuple[pd.DataFrame, pd.DataFrame]:
    """rows: {date: (feature values, ret_oo values)}; tickers T0..Tn-1. Other legs = 0."""
    f, lab = [], []
    for d, (xs, ys) in rows.items():
        for i, (x, y) in enumerate(zip(xs, ys)):
            f.append((d, f"T{i}", feature, x))
            lab.append((d, f"T{i}", horizon, y, 0.0, 0.0))
    return pd.DataFrame(f, columns=FEATURE_COLUMNS), pd.DataFrame(lab, columns=LABEL_COLUMNS)


def _ref_nw_t(x: list[float], lags: int) -> float:
    """Independent, loop-only Newey-West t of the mean (Bartlett weights, 1/T autocovariances)."""
    n = len(x)
    m = sum(x) / n
    def gamma(k: int) -> float:
        return sum((x[t] - m) * (x[t - k] - m) for t in range(k, n)) / n
    s = gamma(0) + sum(2 * (1 - k / (lags + 1)) * gamma(k) for k in range(1, lags + 1))
    return m / math.sqrt(s / n)


# --- daily_ic -------------------------------------------------------------------------------

UP = [1, 2, 3, 4, 5, 6]
IC_CASES = [
    # Rank IC is +1 for any monotone increasing relation and -1 for any decreasing one.
    ("perfect", {D1: (UP, [0.01 * v for v in UP])}, "spearman", {D1: 1.0}),
    ("reversed", {D1: (UP, [-0.01 * v for v in UP])}, "spearman", {D1: -1.0}),
    ("monotone nonlinear", {D1: (UP, [v ** 3 for v in UP])}, "spearman", {D1: 1.0}),
    # Pearson sees the nonlinearity; Spearman does not.
    ("pearson linear", {D1: (UP, [2 * v + 1 for v in UP])}, "pearson", {D1: 1.0}),
    # Dates with fewer than min_names (5) names are skipped, not zero-filled.
    ("too few names", {D1: (UP, UP), D2: ([1, 2, 3, 4], [1, 2, 3, 4])}, "spearman", {D1: 1.0}),
    # A constant feature (or label) makes the correlation undefined: the date is skipped.
    ("constant feature", {D1: ([3] * 6, UP), D2: (UP, UP)}, "spearman", {D2: 1.0}),
    ("constant label", {D1: (UP, [0.0] * 6), D2: (UP, UP)}, "spearman", {D2: 1.0}),
    # NaNs are dropped before counting names: 6 names with 2 NaN -> 4 < 5 -> skipped.
    ("nan drops below min", {D1: (UP, [1, 2, np.nan, 4, np.nan, 6])}, "spearman", {}),
    # Ties use average ranks: [1,1,2,2,3,3] vs [1,2,3,4,5,6] -> corr of midranks.
    ("ties", {D1: ([1, 1, 2, 2, 3, 3], UP)}, "spearman", {D1: float(np.corrcoef([1.5, 1.5, 3.5, 3.5, 5.5, 5.5], UP)[0, 1])}),
]


@pytest.mark.parametrize("name,rows,method,expected", IC_CASES, ids=[c[0] for c in IC_CASES])
def test_daily_ic_cases(name, rows, method, expected):
    f, lab = _frames(rows)
    ic = daily_ic(f, lab, horizon=1, method=method, min_names=5)
    assert dict(ic) == pytest.approx(expected)


def test_daily_ic_random_is_near_zero():
    rng = np.random.default_rng(12345)
    rows = {pd.Timestamp("2020-01-01") + pd.Timedelta(days=i): (rng.normal(size=50), rng.normal(size=50)) for i in range(400)}
    f, lab = _frames(rows)
    ic = daily_ic(f, lab, 1)
    assert len(ic) == 400
    assert abs(ic.mean()) < 0.02  # sd of the mean ~ 0.143/sqrt(400) = 0.007


def test_daily_ic_uses_only_requested_horizon():
    f, lab1 = _frames({D1: (UP, UP)}, horizon=1)
    _, lab5 = _frames({D1: (UP, [-v for v in UP])}, horizon=5)
    lab = pd.concat([lab1, lab5])
    assert daily_ic(f, lab, 1).iloc[0] == pytest.approx(1.0)
    assert daily_ic(f, lab, 5).iloc[0] == pytest.approx(-1.0)


def test_daily_ic_rejects_several_features():
    f1, lab = _frames({D1: (UP, UP)}, feature="a")
    f2, _ = _frames({D1: (UP, UP)}, feature="b")
    with pytest.raises(ValueError):
        daily_ic(pd.concat([f1, f2]), lab, 1)


# --- newey_west_t ---------------------------------------------------------------------------

def test_nw_hand_computed():
    # x=[1,2,3,4]: mean 2.5, gamma0=1.25, gamma1=0.3125.
    # lags=0: se=sqrt(1.25/4)  -> t = 2.5/0.5590 = 4.4721
    # lags=1: lrv=1.25+2*0.5*0.3125=1.5625, se=sqrt(1.5625/4)=0.625 -> t = 4.0
    assert newey_west_t([1, 2, 3, 4], lags=0) == pytest.approx(2.5 / math.sqrt(1.25 / 4))
    assert newey_west_t([1, 2, 3, 4], lags=1) == pytest.approx(4.0)


@pytest.mark.parametrize("lags", [0, 1, 4, 19])
def test_nw_matches_reference_on_ar1(lags):
    rng = np.random.default_rng(7)
    e = rng.normal(size=300)
    x = [0.0] * 300
    for t in range(1, 300):
        x[t] = 0.6 * x[t - 1] + e[t] + 0.1
    assert newey_west_t(pd.Series(x), lags=lags) == pytest.approx(_ref_nw_t(x, lags), rel=1e-10)


def _ref_hh_t(x: list[float], lags: int) -> float:
    """Hansen-Hodrick t of the mean (uniform weights on lags 1..lags); inf if its variance <= 0."""
    n = len(x)
    m = sum(x) / n
    def gamma(k: int) -> float:
        return sum((x[t] - m) * (x[t - k] - m) for t in range(k, n)) / n
    s = gamma(0) + 2 * sum(gamma(k) for k in range(1, lags + 1))
    return m / math.sqrt(s / n) if s > 0 else math.inf


def test_nw_default_lags():
    rng = np.random.default_rng(3)
    x = list(rng.normal(0.05, 1, size=100))
    # Horizon 1: Bartlett with T=100 -> floor(4*(1)^(2/9)) = 4 lags.
    assert newey_west_t(x) == pytest.approx(_ref_nw_t(x, 4))
    # Horizon h > 1: the smaller |t| of Bartlett(max(auto, 2h)) and Hansen-Hodrick(h-1).
    for h in (5, 20):
        expected = min(abs(_ref_nw_t(x, max(4, 2 * h))), abs(_ref_hh_t(x, h - 1)))
        assert abs(newey_west_t(x, horizon=h)) == pytest.approx(expected)


def _null_rejection_rate(h: int, lags: int | None, reps: int = 400, n: int = 2500) -> float:
    """P(|t| >= 1.96) for an IC series that is an h-day moving average of iid N(0,1) (null true)."""
    rng = np.random.default_rng(20261005)
    hits = 0
    for _ in range(reps):
        ic = np.convolve(rng.standard_normal(n + h - 1), np.ones(h) / h, mode="valid")
        hits += abs(newey_west_t(ic, lags=lags, horizon=h)) >= 1.96
    return hits / reps


# Review regression (size under overlap): Bartlett with h-1 lags rejected the true null at 5%
# about 12-14% of the time at h=5 and h=20; the default must stay near nominal.
NW_SIZE_CASES = [
    (5, None, 0.0, 0.085),
    (20, None, 0.0, 0.085),
    (5, 4, 0.10, 1.0),    # the old default, kept as the triggering input
    (20, 19, 0.10, 1.0),
]


@pytest.mark.parametrize("h,lags,lo,hi", NW_SIZE_CASES)
def test_nw_size_under_overlapping_labels(h, lags, lo, hi):
    assert lo <= _null_rejection_rate(h, lags) <= hi


def test_nw_positive_autocorrelation_shrinks_t():
    # Overlapping-horizon ICs: positively autocorrelated, so the naive t overstates.
    rng = np.random.default_rng(11)
    e = rng.normal(size=500)
    x = np.convolve(e, np.ones(10), mode="valid") + 1.0
    assert abs(newey_west_t(x, lags=9)) < abs(newey_west_t(x, lags=0))


NW_EDGE = [
    # Fewer than two observations: undefined.
    ([], float("nan")),
    ([0.3], float("nan")),
    # Zero variance: +/- inf by the sign of the mean, nan if the mean is zero.
    ([0.2, 0.2, 0.2], math.inf),
    ([-0.2, -0.2, -0.2], -math.inf),
    ([0.0, 0.0], float("nan")),
]


@pytest.mark.parametrize("x,expected", NW_EDGE)
def test_nw_edges(x, expected):
    got = newey_west_t(x, lags=1)
    assert (math.isnan(got) and math.isnan(expected)) or got == expected


# --- quantiles ------------------------------------------------------------------------------

Q_CASES = [
    # Distinct values, n divisible by q: equal buckets.
    ("ten distinct", list(range(10)), 5, [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]),
    # Order does not matter.
    ("shuffled", [9, 0, 8, 1, 7, 2, 6, 3, 5, 4], 5, [5, 1, 5, 1, 4, 2, 4, 2, 3, 3]),
    # Uneven: ceil(rank*q/n).
    ("seven in five", list(range(7)), 5, [1, 2, 3, 3, 4, 5, 5]),
    # Ties share a quantile (average rank); a pair straddling a boundary goes to its mean rank.
    ("tie across boundary", [0, 1, 2, 2, 4, 5, 6, 7, 8, 9], 5, [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]),
    ("tie at 2/3", [0, 1, 1, 3, 4, 5, 6, 7, 8, 9], 5, [1, 2, 2, 2, 3, 3, 4, 4, 5, 5]),
    # Everything tied -> all in the middle quantile; no top or bottom.
    ("all tied", [1.0] * 10, 5, [3] * 10),
    # A large bottom tie block lands where its mean rank falls, leaving quantile 1 empty.
    ("bottom tie block", [0, 0, 0, 0, 0, 0, 1, 2, 3, 4], 5, [2, 2, 2, 2, 2, 2, 4, 4, 5, 5]),
]


@pytest.mark.parametrize("name,values,nq,expected", Q_CASES, ids=[c[0] for c in Q_CASES])
def test_assign_quantiles(name, values, nq, expected):
    assert assign_quantiles(pd.Series(values, dtype=float), nq).tolist() == expected


def test_assign_quantiles_keeps_nan():
    q = assign_quantiles(pd.Series([np.nan, 1, 2, 3, 4, 5]), 5)
    assert math.isnan(q.iloc[0]) and q.iloc[1:].tolist() == [1, 2, 3, 4, 5]


TEN = list(range(10))
SPREAD_CASES = [
    # Top quintile mean minus bottom quintile mean of ret_oo.
    ("monotone", {D1: (TEN, [0.01 * v for v in TEN])}, {D1: 0.01 * ((8 + 9) / 2 - (0 + 1) / 2)}),
    ("reversed", {D1: (TEN, [-0.01 * v for v in TEN])}, {D1: -0.08}),
    # No top/bottom (all tied) -> date skipped; too few names -> skipped.
    ("all tied skipped", {D1: ([1] * 10, TEN), D2: (TEN, TEN)}, {D2: 8.0}),
    ("too few names", {D1: ([0, 1, 2, 3], [0, 1, 2, 3]), D2: (TEN, TEN)}, {D2: 8.0}),
]


@pytest.mark.parametrize("name,rows,expected", SPREAD_CASES, ids=[c[0] for c in SPREAD_CASES])
def test_quantile_spread(name, rows, expected):
    f, lab = _frames(rows)
    assert dict(quantile_spread(f, lab, 1, n_quantiles=5)) == pytest.approx(expected)


TURNOVER_CASES = [
    # Same ordering every day: nothing replaced.
    ("stable", {D1: TEN, D2: TEN, D3: TEN}, 0.0),
    # Full reversal: top and bottom swap entirely each day.
    ("flip", {D1: TEN, D2: TEN[::-1], D3: TEN}, 1.0),
    # Day 2 swaps one top name (T9 -> T7) and nothing in the bottom: (0.5 + 0) / 2 = 0.25.
    ("one swap", {D1: TEN, D2: [0, 1, 2, 3, 4, 5, 6, 9, 8, 7]}, 0.25),
    # A single date has nothing to compare against.
    ("single date", {D1: TEN}, float("nan")),
]


@pytest.mark.parametrize("name,rows,expected", TURNOVER_CASES, ids=[c[0] for c in TURNOVER_CASES])
def test_quantile_turnover(name, rows, expected):
    f, _ = _frames({d: (v, v) for d, v in rows.items()})
    got = quantile_turnover(f, 5)
    assert (math.isnan(got) and math.isnan(expected)) or got == pytest.approx(expected)


# --- subperiods -----------------------------------------------------------------------------

SUB_CASES = [
    # Equal-count consecutive chunks in date order.
    ([1, 1, 1, -1, -1, -1, 2, 2, 2], 3, [1.0, -1.0, 2.0]),
    # Uneven length: first chunks get the extra observation (numpy array_split).
    ([1, 1, -1, -1, 3], 3, [1.0, -1.0, 3.0]),
    # Fewer observations than periods: undefined.
    ([1, 2], 3, [float("nan")] * 3),
]


@pytest.mark.parametrize("values,n,expected", SUB_CASES)
def test_subperiod_signs(values, n, expected):
    idx = pd.date_range("2020-01-01", periods=len(values))
    got = subperiod_signs(pd.Series(values, index=idx, dtype=float), n)
    assert len(got) == len(expected)
    for g, e in zip(got, expected):
        assert (math.isnan(g) and math.isnan(e)) or g == pytest.approx(e)


def test_subperiod_signs_sorts_by_date():
    idx = pd.to_datetime(["2020-01-03", "2020-01-01", "2020-01-02"])
    assert subperiod_signs(pd.Series([3.0, 1.0, 2.0], index=idx), 3) == [1.0, 2.0, 3.0]


# --- gate -----------------------------------------------------------------------------------

def _report(ic_t: float, spread: float, turnover: float = 0.1, horizon: int = 5) -> SignalReport:
    return SignalReport(feature="f", horizon=horizon, track="A", n_days=500, ic_mean=0.02 * np.sign(ic_t),
                        ic_std=0.1, ic_t=ic_t, ic_hit_rate=0.55, q_spread_mean=spread, turnover=turnover)


# cost per period = 4 * cost_bps * 1e-4 * min(1, horizon * turnover) ($1 long + $1 short, both
# sides of each leg traded); with 10 bps, h=5, to=0.1 -> 0.002.
GATE_CASES = [
    # All three conditions hold.
    ("pass", _report(3.5, 0.003), [0.02, 0.01, 0.03], 10, True),
    # |t| exactly at the threshold passes; just below fails.
    ("t at threshold", _report(3.0, 0.003), [0.02, 0.01, 0.03], 10, True),
    ("t below threshold", _report(2.99, 0.003), [0.02, 0.01, 0.03], 10, False),
    # Sign must match the full-sample sign in >= 2 of 3 sub-periods.
    ("two of three", _report(3.5, 0.003), [0.02, -0.01, 0.03], 10, True),
    ("one of three", _report(3.5, 0.003), [0.02, -0.01, -0.03], 10, False),
    # Spread must exceed the cost: 0.0019 < 0.002 fails, 0.0021 passes.
    ("spread below cost", _report(3.5, 0.0019), [0.02, 0.01, 0.03], 10, False),
    ("spread above cost", _report(3.5, 0.0021), [0.02, 0.01, 0.03], 10, True),
    # Turnover scaled to the horizon caps at full replacement: h=20, to=0.1 -> cost 4*10bp = 0.004.
    ("turnover capped", _report(3.5, 0.0039, 0.1, 20), [0.02, 0.01, 0.03], 10, False),
    ("turnover capped ok", _report(3.5, 0.0041, 0.1, 20), [0.02, 0.01, 0.03], 10, True),
    # Review regression: full daily replacement at 5 bp costs 4*5bp = 20bp > a 15bp spread.
    # The old 2x accounting charged 10bp and passed this with net +5bp.
    ("full replacement 4x cost", _report(5.0, 0.0015, 1.0, 1), [0.05, 0.05, 0.05], 5, False),
    # A negative signal is traded short-top/long-bottom: negative spread and negative sub-periods pass.
    ("negative signal", _report(-4.0, -0.003), [-0.02, -0.01, 0.01], 10, True),
    ("negative t positive spread", _report(-4.0, 0.003), [-0.02, -0.01, -0.01], 10, False),
    # Undefined t never passes.
    ("nan t", _report(float("nan"), 0.003), [0.02, 0.01, 0.03], 10, False),
]


@pytest.mark.parametrize("name,report,subs,cost_bps,expected", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_passes_gate(name, report, subs, cost_bps, expected):
    ok, reason = passes_gate(report, subs, cost_bps)
    assert ok is expected
    assert reason.startswith("pass" if expected else "fail")


def test_gate_reason_lists_every_failure():
    ok, reason = passes_gate(_report(1.0, -0.01), [0.02, -0.01, -0.03], 10)
    assert not ok and "NW t" in reason and "sub-periods" in reason and "spread" in reason


# --- evaluate_signal and the overnight diagnostic -------------------------------------------

def _panel(n_days: int, n_names: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Feature x; ret_oo(h) = x*h*1e-3 + noise; ret_overnight = noise only."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2018-01-01", periods=n_days)
    f, lab = [], []
    for d in dates:
        x = rng.normal(size=n_names)
        for i in range(n_names):
            f.append((d, f"T{i}", "sig", x[i]))
            for h in (1, 5):
                lab.append((d, f"T{i}", h, x[i] * h * 1e-3 + rng.normal(0, 2e-3), rng.normal(0, 1e-3), 0.0))
    return pd.DataFrame(f, columns=FEATURE_COLUMNS), pd.DataFrame(lab, columns=LABEL_COLUMNS)


def test_evaluate_signal_strong_signal_passes():
    f, lab = _panel(120, 20, seed=1)
    rep = evaluate_signal(f, lab, "sig", horizons=[1, 5], track="A")
    assert rep.horizon == 1 and rep.n_days == 120 and set(rep.decay) == {1, 5}
    assert rep.decay[5] > rep.decay[1] > 0.3
    assert rep.ic_t > 3 and rep.q_spread_mean > 0 and 0.5 < rep.turnover <= 1
    ic = daily_ic(f, lab, 1)
    ok, reason = passes_gate(rep, subperiod_signs(ic), cost_bps=5)
    assert ok, reason


def test_evaluate_signal_unknown_feature():
    f, lab = _panel(5, 6, seed=2)
    with pytest.raises(ValueError):
        evaluate_signal(f, lab, "nope", [1], "A")


def test_ic_by_return_leg_separates_overnight():
    # Feature predicts only the (non-tradable) overnight leg: stale-pricing signature.
    rng = np.random.default_rng(5)
    rows = []
    feats = []
    for d in pd.bdate_range("2019-01-01", periods=60):
        x = rng.normal(size=15)
        for i in range(15):
            feats.append((d, f"T{i}", "s", x[i]))
            rows.append((d, f"T{i}", 1, rng.normal(), x[i], rng.normal()))
    out = ic_by_return_leg(pd.DataFrame(feats, columns=FEATURE_COLUMNS), pd.DataFrame(rows, columns=LABEL_COLUMNS), 1)
    assert set(out) == {"ret_oo", "ret_overnight", "ret_intraday"}
    assert out["ret_overnight"] == pytest.approx(1.0)
    assert abs(out["ret_oo"]) < 0.15 and abs(out["ret_intraday"]) < 0.15
