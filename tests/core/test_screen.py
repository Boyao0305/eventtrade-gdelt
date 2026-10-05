"""Case tables for core/screen.py — the ADR 0003 sub-category screen statistics.

Reference numbers:
- Benjamini & Hochberg (1995), JRSS-B 57(1), section 4: the 15 p-values of the myocardial
  infarction example. At q = 0.05 BH rejects 4 hypotheses; Bonferroni-type step-down (Holm)
  at alpha = 0.05 rejects 3.
- Holm (1979) as on Wikipedia "Holm-Bonferroni method", example: p = (0.01, 0.04, 0.03, 0.005),
  alpha = 0.05 -> H1 and H4 rejected.
- eb_shrink: method-of-moments normal-normal, hand-computed below.
- shrink_directional (ADR 0003 implementation notes): signed cells pool sign * IC, two-sided
  cells pool their signed IC unfolded; the direction is applied after shrinkage.
"""

import math

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.evaluate import daily_ic, newey_west_t
from eventtrade.core.screen import (
    CellVerdict,
    ConfirmResult,
    EbResult,
    JointResult,
    _nw_t_rows,
    benjamini_hochberg,
    cell_estimate,
    cell_ic,
    confirm,
    eb_shrink,
    holm,
    joint_test,
    shrink_directional,
)
from eventtrade.schemas import FEATURE_COLUMNS, LABEL_COLUMNS

BH_PAPER = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344, 0.0459,
            0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.000]


def _paper() -> dict[str, float]:
    return {f"h{i + 1:02d}": p for i, p in enumerate(BH_PAPER)}


def _first(k: int) -> set[str]:
    return {f"h{i + 1:02d}" for i in range(k)}


# --- benjamini_hochberg: step-up -------------------------------------------------------------
BH_CASES = [
    ("paper q=0.05 -> 4", _paper(), 0.05, _first(4)),
    ("paper q=0.10 -> 9", _paper(), 0.10, _first(9)),
    # step-up: p_(3)=0.05 <= 3q/3 rejects all three although p_(1)=0.04 > q/3
    ("step-up", {"a": 0.04, "b": 0.045, "c": 0.05}, 0.05, {"a", "b", "c"}),
    ("none", {"a": 0.5, "b": 0.2, "c": 0.04}, 0.05, set()),
    ("single", {"a": 0.05}, 0.05, {"a"}),
    ("empty", {}, 0.10, set()),
    # NaN counts as a test (m=3) that is never rejected: 0.03 <= 2*0.05/3, 0.02 <= 0.05/3? no
    ("nan counted", {"a": 0.02, "b": 0.03, "c": float("nan")}, 0.05, {"a", "b"}),
    ("nan raises m", {"a": 0.02, "b": float("nan"), "c": float("nan")}, 0.05, set()),
    ("tuple ids", {("q1", "dm", 5): 0.001, ("q1", "dm", 20): 0.9}, 0.10, {("q1", "dm", 5)}),
]


@pytest.mark.parametrize("name, pvals, q, expected", BH_CASES, ids=[c[0] for c in BH_CASES])
def test_benjamini_hochberg(name, pvals, q, expected):
    assert benjamini_hochberg(pvals, q) == expected


# --- holm: step-down -------------------------------------------------------------------------
HOLM_CASES = [
    ("paper alpha=0.05 -> 3", _paper(), 0.05, _first(3)),
    ("wikipedia", {"H1": 0.01, "H2": 0.04, "H3": 0.03, "H4": 0.005}, 0.05, {"H1", "H4"}),
    # step-down stops at the first failure: 0.03 > 0.05/2 so 0.04 <= 0.05 is not rejected
    ("stops", {"a": 0.001, "b": 0.03, "c": 0.04}, 0.05, {"a"}),
    ("all", {"a": 0.01, "b": 0.011}, 0.05, {"a", "b"}),
    ("two at 0.03", {"a": 0.03, "b": 0.03}, 0.05, set()),
    ("single", {"a": 0.049}, 0.05, {"a"}),
    ("empty", {}, 0.05, set()),
    ("nan counted", {"a": 0.02, "b": float("nan")}, 0.05, {"a"}),
    ("nan raises m", {"a": 0.03, "b": float("nan")}, 0.05, set()),
]


@pytest.mark.parametrize("name, pvals, alpha, expected", HOLM_CASES, ids=[c[0] for c in HOLM_CASES])
def test_holm(name, pvals, alpha, expected):
    assert holm(pvals, alpha) == expected


@pytest.mark.parametrize("fn", [benjamini_hochberg, holm])
@pytest.mark.parametrize("level", [0.0, 1.5, -0.1])
def test_multiple_testing_rejects_bad_level(fn, level):
    with pytest.raises(ValueError):
        fn({"a": 0.01}, level)


# --- eb_shrink: hand computations ------------------------------------------------------------
# Equal se = 0.01: mean 0.02, sample var (ddof=1) = (1+1+9+9)e-4/3 = 6.6667e-4, mean se^2 = 1e-4
#   tau^2 = 5.6667e-4; B = 1e-4 / 6.6667e-4 = 0.15; post = 0.02 + 0.85 (est - 0.02).
# Unequal se = (.01,.02,.01,.02): mean se^2 = 2.5e-4, tau^2 = 4.1667e-4;
#   B = (0.193548, 0.489796, 0.193548, 0.489796).
EB_CASES = [
    ("equal se",
     [0.03, 0.01, -0.01, 0.05], [0.01] * 4, None,
     5.6667e-4, 0.02, [0.0285, 0.0115, -0.0055, 0.0455]),
    ("unequal se",
     [0.03, 0.01, -0.01, 0.05], [0.01, 0.02, 0.01, 0.02], None,
     4.1667e-4, 0.02, [0.0280645, 0.0148980, -0.0041935, 0.0353061]),
    # var = (0.0083^2... ) tiny vs se^2 = 0.0025: tau^2 truncated at 0, all pooled to the mean
    ("within noise -> tau2=0",
     [0.01, -0.01, 0.006], [0.05] * 3, None,
     0.0, 0.002, [0.002, 0.002, 0.002]),
    # prior mean fixed at 0: tau^2 = mean(est^2) - mean(se^2) = (9+1+1+25)e-4/4 - 1e-4 = 8e-4;
    #   B = 1e-4/9e-4 = 1/9; post = est * 8/9
    ("prior mean 0",
     [0.03, 0.01, -0.01, 0.05], [0.01] * 4, 0.0,
     8.0e-4, 0.0, [0.0266667, 0.0088889, -0.0088889, 0.0444444]),
]


@pytest.mark.parametrize("name, est, se, prior, tau2, mu, post", EB_CASES, ids=[c[0] for c in EB_CASES])
def test_eb_shrink(name, est, se, prior, tau2, mu, post):
    cells = [f"c{i}" for i in range(len(est))]
    res = eb_shrink(dict(zip(cells, est)), dict(zip(cells, se)), prior_mean=prior)
    assert isinstance(res, EbResult)
    assert res.tau2 == pytest.approx(tau2, rel=1e-3, abs=1e-12)
    assert res.prior_mean == pytest.approx(mu, abs=1e-12)
    for c, p in zip(cells, post):
        assert res.posterior[c] == pytest.approx(p, abs=1e-6)
        b = se[cells.index(c)] ** 2 / (se[cells.index(c)] ** 2 + res.tau2)
        assert res.shrinkage[c] == pytest.approx(b)
        assert res.posterior_sd[c] == pytest.approx(math.sqrt(se[cells.index(c)] ** 2 * (1 - b)))
    notes = res.notes()
    assert notes["tau2"] == pytest.approx(tau2, rel=1e-3, abs=1e-12)
    assert set(notes["posterior"]) == set(cells)


def test_eb_shrink_skips_nan_and_needs_two_cells():
    res = eb_shrink({"a": 0.03, "b": 0.01, "c": float("nan")}, {"a": 0.01, "b": 0.01, "c": 0.01})
    assert math.isnan(res.posterior["c"])
    assert res.prior_mean == pytest.approx(0.02)
    with pytest.raises(ValueError):
        eb_shrink({"a": 0.03}, {"a": 0.01})
    with pytest.raises(ValueError):
        eb_shrink({"a": 0.03, "b": 0.01}, {"a": 0.01})


# --- shrink_directional: what is pooled, and when the direction is applied --------------------
# Pooled values equal the "equal se" EB case: a (+1, IC .03) -> .03; b (-1, IC -.01) -> .01;
# c (two-sided, IC -.01, discovery t -2) -> -.01 UNFOLDED; d (two-sided, IC .05, t 3) -> .05.
# Posterior = .0285, .0115, -.0055, .0455; direction applied after shrinkage:
# a .0285, b .0115, c (-1) x -.0055 = .0055, d .0455. Folding c before pooling (|IC| = .01)
# would change the prior mean and give c .0115.
def test_shrink_directional_pools_unfolded_two_sided_ic():
    est = {"a": 0.03, "b": -0.01, "c": -0.01, "d": 0.05}
    se = dict.fromkeys(est, 0.01)
    signs = {"a": 1, "b": -1, "c": None, "d": None}
    t = {"a": 2.5, "b": -1.0, "c": -2.0, "d": 3.0}
    eb, directional = shrink_directional(est, se, signs, t)
    assert isinstance(eb, EbResult)
    assert eb.prior_mean == pytest.approx(0.02)
    assert eb.tau2 == pytest.approx(5.6667e-4, rel=1e-3)
    assert eb.posterior == pytest.approx({"a": 0.0285, "b": 0.0115, "c": -0.0055, "d": 0.0455})
    assert directional == pytest.approx({"a": 0.0285, "b": 0.0115, "c": 0.0055, "d": 0.0455})


def test_shrink_directional_undefined_direction_and_nan_cells():
    est = {"a": 0.03, "b": -0.01, "c": -0.01, "d": float("nan")}
    se = {"a": 0.01, "b": 0.01, "c": 0.01, "d": float("nan")}
    signs = {"a": 1, "b": -1, "c": None, "d": 1}
    t = {"a": 2.0, "b": -1.0, "c": float("nan"), "d": float("nan")}
    eb, directional = shrink_directional(est, se, signs, t)
    # c is pooled (finite IC) but has no direction; d is out of the moments
    assert eb.n_cells == 3 and math.isfinite(eb.posterior["c"])
    assert math.isnan(directional["c"]) and math.isnan(directional["d"])
    assert directional["a"] == pytest.approx(eb.posterior["a"])
    with pytest.raises(ValueError, match="sign"):
        shrink_directional({"a": 0.1, "b": 0.2}, {"a": 0.1, "b": 0.1}, {"a": 2, "b": 1},
                           {"a": 1.0, "b": 1.0})


def test_cell_estimate_matches_nw():
    rng = np.random.default_rng(3)
    ic = pd.Series(0.02 + rng.normal(0, 0.1, 400))
    mean, se = cell_estimate(ic, horizon=5)
    assert mean == pytest.approx(ic.mean())
    assert mean / se == pytest.approx(newey_west_t(ic, horizon=5))
    _, s2 = cell_estimate(pd.Series([0.1]), horizon=5)
    assert math.isnan(s2)


# --- cell_ic -----------------------------------------------------------------------------------

def _cell_frames(seed: int = 0, n_days: int = 30, n_tickers: int = 8):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-12-02", periods=n_days)
    tickers = [f"T{i}" for i in range(n_tickers)]
    f, lab = [], []
    for d in dates:
        x = rng.normal(size=n_tickers)
        y = 0.5 * x + rng.normal(size=n_tickers)
        for t, xi, yi in zip(tickers, x, y):
            f.append((d, t, "sig", xi))
            for h in (5, 20):
                lab.append((d, t, h, yi * (1 if h == 5 else -1), 0.0, 0.0))
    return (pd.DataFrame(f, columns=FEATURE_COLUMNS), pd.DataFrame(lab, columns=LABEL_COLUMNS),
            dates, tickers)


def test_cell_ic_restricts_members_dates_and_horizon():
    feat, lab, dates, tickers = _cell_frames()
    members = set(tickers[:6]) | {"NOT_IN_DATA"}
    start, end = dates[5], dates[20]
    got = cell_ic(feat, lab, members, horizon=5, start=start, end=end)
    sub = feat[feat["ticker"].isin(members) & (feat["date"] >= start) & (feat["date"] <= end)]
    want = daily_ic(sub, lab, 5, method="spearman", min_names=5)
    pd.testing.assert_series_equal(got, want)
    assert got.index.min() == start and got.index.max() == end  # inclusive both ends
    # horizon 20 labels are negated in the fixture: IC flips sign
    got20 = cell_ic(feat, lab, members, horizon=20, start=start, end=end)
    np.testing.assert_allclose(got20.to_numpy(), -got.to_numpy())
    # string dates are accepted
    got_s = cell_ic(feat, lab, members, 5, str(start.date()), str(end.date()))
    pd.testing.assert_series_equal(got_s, got)


def test_cell_ic_min_names():
    feat, lab, dates, tickers = _cell_frames()
    assert cell_ic(feat, lab, set(tickers[:4]), 5, dates[0], dates[-1]).empty
    assert len(cell_ic(feat, lab, set(tickers[:4]), 5, dates[0], dates[-1], min_names=4)) == len(dates)


def test_cell_ic_rejects_multiple_features():
    feat, lab, dates, tickers = _cell_frames()
    two = pd.concat([feat, feat.assign(feature="other")])
    with pytest.raises(ValueError):
        cell_ic(two, lab, set(tickers), 5, dates[0], dates[-1])


# --- vectorised HAC t equals evaluate.newey_west_t ---------------------------------------------

@pytest.mark.parametrize("horizon", [1, 5, 20])
def test_nw_t_rows_matches_newey_west_t(horizon):
    rng = np.random.default_rng(horizon)
    x = rng.normal(0.01, 0.1, (12, 300))
    x[x > 0.15] = np.nan  # ragged NaN pattern per row
    x[3, :] = np.nan
    x[4, :299] = np.nan  # one observation
    x[5, :] = 0.0  # constant zero
    x[6, :] = np.where(np.isnan(x[6]), np.nan, 0.02)  # constant non-zero
    got = _nw_t_rows(x, horizon)
    for i in range(x.shape[0]):
        want = newey_west_t(pd.Series(x[i]), horizon=horizon)
        if math.isnan(want):
            assert math.isnan(got[i]), i
        elif math.isinf(want):
            assert got[i] == want, i
        else:
            assert got[i] == pytest.approx(want, rel=1e-9), i


# --- joint_test -------------------------------------------------------------------------------

def _null_cells(rng, n_cells: int, n_days: int, horizon: int, rho: float = 0.5, shift=None):
    """IC-like series: common + idiosyncratic noise, MA(horizon-1) smoothed (overlapping labels)."""
    shift = shift or {}
    dates = pd.bdate_range("2016-01-04", periods=n_days)
    common = rng.normal(size=n_days + horizon)
    out = {}
    for k in range(n_cells):
        e = math.sqrt(rho) * common + math.sqrt(1 - rho) * rng.normal(size=n_days + horizon)
        ma = np.convolve(e, np.ones(horizon) / horizon, mode="valid")[:n_days]
        out[f"c{k:02d}"] = pd.Series(0.05 * ma + shift.get(f"c{k:02d}", 0.0), index=dates)
    return out


def _signs(n_cells: int) -> dict:
    cycle = [1, -1, None]
    return {f"c{k:02d}": cycle[k % 3] for k in range(n_cells)}


def test_joint_test_deterministic_with_seed():
    rng = np.random.default_rng(11)
    ics = _null_cells(rng, 6, 200, 5)
    hz = {c: 5 for c in ics}
    a = joint_test(ics, hz, _signs(6), n_boot=199, mean_block=10, seed=42)
    b = joint_test(ics, hz, _signs(6), n_boot=199, mean_block=10, seed=42)
    c = joint_test(ics, hz, _signs(6), n_boot=199, mean_block=10, seed=43)
    assert isinstance(a, JointResult)
    assert a.notes() == b.notes()
    assert (a.p_s, a.p_m, a.p_cell) == (b.p_s, b.p_m, b.p_cell)
    assert a.s_stat == c.s_stat and a.t == c.t  # observed stats do not depend on the seed
    assert (a.p_s, a.p_m, a.p_cell) != (c.p_s, c.p_m, c.p_cell)
    assert 1 / 200 <= a.p_s <= 1 and 1 / 200 <= a.p_m <= 1
    notes = a.notes()
    for key in ("s_stat", "m_stat", "p_s", "p_m", "n_boot", "mean_block", "seed", "n_dates", "t", "p_cell"):
        assert key in notes


def test_joint_test_statistics_follow_registered_direction():
    """z = sign * t (|t| when no sign); S = sum max(z,0)^2, M = max z."""
    rng = np.random.default_rng(5)
    ics = _null_cells(rng, 3, 250, 1, rho=0.0)
    signs = {"c00": 1, "c01": -1, "c02": None}
    res = joint_test(ics, {c: 1 for c in ics}, signs, n_boot=99, mean_block=5, seed=0)
    t = {c: newey_west_t(s, horizon=1) for c, s in ics.items()}
    z = {"c00": t["c00"], "c01": -t["c01"], "c02": abs(t["c02"])}
    assert res.t == pytest.approx(t)
    assert res.z == pytest.approx(z)
    assert res.s_stat == pytest.approx(sum(max(v, 0.0) ** 2 for v in z.values()))
    assert res.m_stat == pytest.approx(max(z.values()))


@pytest.mark.parametrize(
    "sign, small_p",
    [(-1, True), (None, True), (1, False)],
    ids=["registered-negative", "two-sided", "registered-positive-wrong-way"],
)
def test_joint_test_per_cell_sign_handling(sign, small_p):
    rng = np.random.default_rng(8)
    ics = _null_cells(rng, 4, 300, 5, shift={"c00": -0.02})  # c00 strongly negative IC
    signs = {"c00": sign, "c01": 1, "c02": -1, "c03": None}
    res = joint_test(ics, {c: 5 for c in ics}, signs, n_boot=499, mean_block=10, seed=1)
    assert res.t["c00"] < -4
    if small_p:
        assert res.p_cell["c00"] < 0.01
        assert res.p_m < 0.05
    else:
        assert res.p_cell["c00"] > 0.95
        assert res.z["c00"] < 0
        assert res.p_m > 0.05  # a wrong-way cell does not make the track significant


def test_joint_test_handles_ragged_dates_and_empty_cell():
    rng = np.random.default_rng(2)
    ics = _null_cells(rng, 3, 200, 5)
    ics["c01"] = ics["c01"].iloc[20:150]  # different date coverage
    ics["c02"] = ics["c02"].iloc[:0]  # no data -> t NaN, p NaN, excluded from S and M
    res = joint_test(ics, {c: 5 for c in ics}, {c: None for c in ics}, n_boot=99, mean_block=10, seed=3)
    assert math.isnan(res.t["c02"]) and math.isnan(res.p_cell["c02"])
    assert res.n_dates == 200
    assert math.isfinite(res.s_stat) and math.isfinite(res.p_s)


def test_joint_test_validates_inputs():
    rng = np.random.default_rng(0)
    ics = _null_cells(rng, 2, 50, 1)
    with pytest.raises(ValueError):
        joint_test(ics, {"c00": 1}, {c: 1 for c in ics}, n_boot=9, mean_block=5, seed=0)
    with pytest.raises(ValueError):
        joint_test(ics, {c: 1 for c in ics}, {"c00": 1, "c01": 2}, n_boot=9, mean_block=5, seed=0)
    with pytest.raises(ValueError):
        joint_test({}, {}, {}, n_boot=9, mean_block=5, seed=0)


def test_joint_test_size_under_null():
    """200 null simulations of 20 correlated, overlapping-horizon cells: P(p < 0.05) ~ 0.05."""
    n_sims, n_cells = 200, 20
    rej_s = rej_m = 0
    rej_cell = []
    for sim in range(n_sims):
        rng = np.random.default_rng(1000 + sim)
        ics = _null_cells(rng, n_cells, 250, 5, rho=0.5)
        res = joint_test(ics, {c: 5 for c in ics}, _signs(n_cells), n_boot=199, mean_block=10, seed=sim)
        rej_s += res.p_s < 0.05
        rej_m += res.p_m < 0.05
        rej_cell.append(np.mean([p < 0.05 for p in res.p_cell.values()]))
    # binomial sd at p=0.05, n=200 is 0.0154; allow ~3.5 sd above, and not degenerate below
    assert 0.01 <= rej_s / n_sims <= 0.105, rej_s
    assert 0.01 <= rej_m / n_sims <= 0.105, rej_m
    assert 0.02 <= float(np.mean(rej_cell)) <= 0.09


def test_joint_test_power_sparse_one_real_cell():
    """One of 20 cells has a real IC (NW t ~ 5.5, registered sign +1): M finds it."""
    n_sims, hits_m, top = 30, 0, 0
    for sim in range(n_sims):
        rng = np.random.default_rng(5000 + sim)
        ics = _null_cells(rng, 20, 250, 5, rho=0.5, shift={"c03": 0.018})  # c03 sign is +1
        res = joint_test(ics, {c: 5 for c in ics}, _signs(20), n_boot=199, mean_block=10, seed=sim)
        hits_m += res.p_m < 0.05
        top += min(res.p_cell, key=res.p_cell.get) == "c03"
    assert hits_m / n_sims >= 0.8
    assert top / n_sims >= 0.8


def test_joint_test_power_dense_small_effects():
    """Every cell has a small IC in its registered direction (NW t ~ 1.5 each): S finds it."""
    signs = _signs(20)
    shift = {c: 0.005 * (s if s is not None else 1) for c, s in signs.items()}
    n_sims, hits_s = 30, 0
    for sim in range(n_sims):
        rng = np.random.default_rng(7000 + sim)
        ics = _null_cells(rng, 20, 250, 5, rho=0.0, shift=shift)
        res = joint_test(ics, {c: 5 for c in ics}, signs, n_boot=199, mean_block=10, seed=sim)
        hits_s += res.p_s < 0.05
    assert hits_s / n_sims >= 0.8


# --- confirm ----------------------------------------------------------------------------------

def _series_with_t(t_target: float, seed: int, horizon: int = 5, n: int = 300) -> pd.Series:
    """IC series with an exact NW t: adding a constant does not change the HAC variance."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.05, n)
    noise = noise - noise.mean()
    se = 1.0 / newey_west_t(pd.Series(noise + 1.0), horizon=horizon)
    return pd.Series(noise + t_target * se, index=pd.bdate_range("2020-01-02", periods=n))


def _confirm_case(cells: dict, cost_bps: float = 10.0):
    """cells: id -> (registered sign, discovery t, confirmation t, spread, daily turnover)."""
    ics = {c: _series_with_t(v[2], seed=i) for i, (c, v) in enumerate(cells.items())}
    return confirm(
        discovered=set(cells),
        ic_conf=ics,
        signs={c: v[0] for c, v in cells.items()},
        spreads={c: v[3] for c, v in cells.items()},
        turnovers={c: v[4] for c, v in cells.items()},
        cost_bps=cost_bps,
        horizon_of={c: 5 for c in cells},
        discovery_t={c: v[1] for c, v in cells.items()},
    )


# cost per period at 10 bps, h=5: 4 * 0.001 * min(1, 5 * turnover)
CONFIRM_CASES = [
    ("confirmed", {"a": (1, 4.0, 3.0, 0.01, 0.1)}, {"a": True}),
    ("negative registered", {"a": (-1, -4.0, -3.0, -0.01, 0.1)}, {"a": True}),
    ("sign flipped", {"a": (1, 4.0, -3.0, -0.01, 0.1)}, {"a": False}),
    ("not significant", {"a": (1, 4.0, 1.0, 0.01, 0.1)}, {"a": False}),
    # spread 0.003 in direction, cost 4 * 0.001 * 1 = 0.004 -> net < 0
    ("spread below cost", {"a": (1, 4.0, 3.0, 0.003, 0.5)}, {"a": False}),
    # same spread, low turnover: cost 4 * 0.001 * 0.25 = 0.001 -> net > 0
    ("spread above cost", {"a": (1, 4.0, 3.0, 0.003, 0.05)}, {"a": True}),
    ("spread wrong way", {"a": (1, 4.0, 3.0, -0.01, 0.1)}, {"a": False}),
    # two-sided cell: direction from the discovery t
    ("two-sided follows discovery", {"a": (None, -4.0, -3.0, -0.01, 0.1)}, {"a": True}),
    ("two-sided flipped", {"a": (None, -4.0, 3.0, 0.01, 0.1)}, {"a": False}),
    # Holm across discovered cells: one-sided t = 1.9 -> p = 0.0287 each; 0.0287 > 0.05/2
    ("holm blocks both", {"a": (1, 4.0, 1.9, 0.01, 0.1), "b": (1, 4.0, 1.9, 0.01, 0.1)},
     {"a": False, "b": False}),
    ("holm alone passes", {"a": (1, 4.0, 1.9, 0.01, 0.1)}, {"a": True}),
    # 3.5 -> p=2.3e-4 <= 0.025, then 1.9 -> 0.0287 <= 0.05
    ("holm step-down", {"a": (1, 4.0, 3.5, 0.01, 0.1), "b": (1, 4.0, 1.9, 0.01, 0.1)},
     {"a": True, "b": True}),
]


@pytest.mark.parametrize("name, cells, expected", CONFIRM_CASES, ids=[c[0] for c in CONFIRM_CASES])
def test_confirm(name, cells, expected):
    res = _confirm_case(cells)
    assert isinstance(res, ConfirmResult)
    got = {c: v.confirmed for c, v in res.verdicts.items()}
    assert got == expected
    for c, v in res.verdicts.items():
        assert isinstance(v, CellVerdict)
        assert v.t == pytest.approx(cells[c][2], rel=1e-9)
        assert (v.reason == "confirmed") == v.confirmed
    assert res.confirmed == {c for c, ok in expected.items() if ok}
    notes = res.notes()
    assert set(notes["verdicts"]) == {str(c) for c in cells}


def test_confirm_requires_discovery_t_for_two_sided_and_known_cells():
    s = _series_with_t(3.0, 0)
    kw = {"spreads": {"a": 0.01}, "turnovers": {"a": 0.1}, "cost_bps": 10.0, "horizon_of": {"a": 5}}
    with pytest.raises(ValueError):
        confirm({"a"}, {"a": s}, {"a": None}, **kw)
    with pytest.raises(ValueError):
        confirm({"a"}, {}, {"a": 1}, **kw)
    empty = confirm(set(), {}, {}, spreads={}, turnovers={}, cost_bps=10.0, horizon_of={})
    assert empty.verdicts == {} and empty.confirmed == set()
