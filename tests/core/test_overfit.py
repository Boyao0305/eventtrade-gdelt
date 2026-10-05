"""Case tables for core/overfit.py (ADR 0001 gate 3).

Reference numbers come from Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio",
section "A Numerical Example" (journal pp. 9-10 of the davidhbailey.com preprint): N=100 trials,
V[SR]=1/2 annualised, T=1250 daily obs (250/yr), skew=-3, kurtosis=10, annualised SR_hat=2.5
=> SR_0 = 0.1132 (non-annualised), DSR = 0.9004; with N=46, DSR = 0.9505; with Normal
returns (skew 0, kurt 3), DSR crosses 0.95 at N=88.
"""

import math
from itertools import combinations

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from eventtrade.core.overfit import (
    _stationary_indices,
    deflated_sharpe,
    expected_max_sharpe,
    overfit_report,
    pbo_cscv,
    probabilistic_sharpe,
    sharpe,
    stationary_bootstrap_sharpe_ci,
    to_wide,
)
from eventtrade.schemas import OverfitReport

PAPER_SR = 2.5 / math.sqrt(250)  # annualised 2.5 -> per-period
PAPER_VAR = 0.5 / 250  # annualised variance 1/2 -> per-period


def _noise(seed: int, rows: int, trials: int, vol: float = 0.01) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        rng.normal(0.0, vol, (rows, trials)), columns=[f"t{i:02d}" for i in range(trials)]
    )


# --- sharpe: mean / std(ddof=1) * sqrt(periods); degenerate inputs ------------------------------
SHARPE_CASES = [
    ([1.0, 2.0, 3.0], 1, 2.0),  # mean 2, sd 1
    ([1.0, 2.0, 3.0], 252, 2.0 * math.sqrt(252)),
    ([-1.0, -2.0, -3.0], 1, -2.0),
    ([0.01, 0.01, 0.01], 252, 0.0),  # zero vol -> 0, not inf
    ([0.01], 252, float("nan")),  # < 2 obs
    ([1.0, float("nan"), 3.0, 2.0], 1, 2.0),  # NaN ignored
]


@pytest.mark.parametrize("returns, ppy, expected", SHARPE_CASES)
def test_sharpe(returns, ppy, expected):
    got = sharpe(np.array(returns), ppy)
    if math.isnan(expected):
        assert math.isnan(got)
    else:
        assert got == pytest.approx(expected)


# --- probabilistic_sharpe: per-period SR, RAW kurtosis ----------------------------------------
PSR_CASES = [
    # SR_hat equal to benchmark -> exactly one half, whatever the moments
    (0.1, 0.1, 500, -1.0, 6.0, 0.5),
    # Normal moments: z = SR sqrt(T-1) / sqrt(1 + SR^2 / 2)
    (0.1, 0.0, 101, 0.0, 3.0, stats.norm.cdf(0.1 * 10 / math.sqrt(1 + 0.01 / 2))),
    # Negative skew and fat tails widen the SR std error -> lower PSR than Normal
    (0.1, 0.0, 101, -2.0, 9.0, stats.norm.cdf(0.1 * 10 / math.sqrt(1 + 0.2 + 0.08 * 0.01 * 25))),
    # DSR paper example: PSR against SR_0 is the DSR
    (PAPER_SR, 0.11317, 1250, -3.0, 10.0, 0.9004),
    (0.1, 0.0, 1, 0.0, 3.0, float("nan")),  # too few obs
]


@pytest.mark.parametrize("sr, bm, n, sk, ku, expected", PSR_CASES)
def test_probabilistic_sharpe(sr, bm, n, sk, ku, expected):
    got = probabilistic_sharpe(sr, bm, n, sk, ku)
    if math.isnan(expected):
        assert math.isnan(got)
    else:
        assert got == pytest.approx(expected, abs=1e-4)


# --- expected_max_sharpe: paper value, scaling, degenerate N --------------------------------
EMAX_CASES = [
    (100, PAPER_VAR, 0.1132),  # paper "A Numerical Example"
    (1, PAPER_VAR, 0.0),  # no selection
    (100, 0.0, 0.0),  # no dispersion across trials
]


@pytest.mark.parametrize("n, var, expected", EMAX_CASES)
def test_expected_max_sharpe(n, var, expected):
    assert expected_max_sharpe(n, var) == pytest.approx(expected, abs=1e-4)


def test_expected_max_sharpe_scales_with_sd_and_grows_with_n():
    assert expected_max_sharpe(50, 4.0) == pytest.approx(2 * expected_max_sharpe(50, 1.0))
    assert expected_max_sharpe(10, 1.0) < expected_max_sharpe(100, 1.0) < expected_max_sharpe(1000, 1.0)


def test_expected_max_sharpe_close_to_simulated_max_of_normals():
    # Independent check: Monte Carlo E[max] of N standard normals (paper Appendix 2 experiment).
    rng = np.random.default_rng(7)
    sim = rng.standard_normal((20000, 100)).max(axis=1).mean()  # ~2.51
    assert expected_max_sharpe(100, 1.0) == pytest.approx(sim, abs=0.05)


# --- DSR paper example composed from PSR(E[max]) ---------------------------------------------
DSR_PAPER_CASES = [
    (100, -3.0, 10.0, 0.9004),
    (46, -3.0, 10.0, 0.9505),
    (88, 0.0, 3.0, 0.9505),  # Normal returns: crosses 0.95 at N=88
]


@pytest.mark.parametrize("n, sk, ku, expected", DSR_PAPER_CASES)
def test_dsr_reproduces_paper(n, sk, ku, expected):
    sr0 = expected_max_sharpe(n, PAPER_VAR)
    assert probabilistic_sharpe(PAPER_SR, sr0, 1250, sk, ku) == pytest.approx(expected, abs=1e-4)


# --- deflated_sharpe on a frame: independent straightforward computation --------------------
def test_deflated_sharpe_matches_manual_computation():
    w = _noise(3, 750, 20)
    w["t05"] += 0.002
    m = w.to_numpy()
    srs = m.mean(axis=0) / m.std(axis=0, ddof=1)
    j = int(np.argmax(srs))
    v = np.var(srs, ddof=1)
    g = 0.5772156649
    sr0 = math.sqrt(v) * (
        (1 - g) * stats.norm.ppf(1 - 1 / 20) + g * stats.norm.ppf(1 - 1 / (20 * math.e))
    )
    x = m[:, j] - m[:, j].mean()
    m2, m3, m4 = (x**2).mean(), (x**3).mean(), (x**4).mean()
    sk, ku = m3 / m2**1.5, m4 / m2**2
    z = (srs[j] - sr0) * math.sqrt(750 - 1) / math.sqrt(1 - sk * srs[j] + (ku - 1) / 4 * srs[j] ** 2)
    best, dsr = deflated_sharpe(w)
    assert best == "t05" == w.columns[j]
    assert dsr == pytest.approx(stats.norm.cdf(z), abs=1e-10)


def test_deflated_sharpe_more_trials_deflate_more():
    # Same best trial, more noise trials around it -> higher SR_0 -> lower DSR
    w = _noise(4, 1000, 60)
    w["t00"] += 0.0012
    _, dsr_few = deflated_sharpe(w.iloc[:, :5])
    _, dsr_many = deflated_sharpe(w)
    assert dsr_many < dsr_few


# --- n_trials override: selection done before the return matrix still counts -----------------
# Review regression: ~80 signal trials screened, 1-2 survivors backtested. Deflating over the
# survivors only left SR_0 ~ 0 (no deflation at all with one column).
def test_n_trials_override_deflates_survivors():
    w = _noise(5, 1000, 2)
    w["t00"] += 0.0012
    best, dsr_cols = deflated_sharpe(w)
    best_n, dsr_n = deflated_sharpe(w, n_trials=81)
    assert best == best_n == "t00"
    assert dsr_n < dsr_cols


def test_n_trials_override_single_column_uses_null_variance():
    # One column has no cross-trial dispersion; V[SR] falls back to 1/(T-1).
    w = _noise(6, 1000, 1)
    w["t00"] += 0.0012
    r = w["t00"].to_numpy()
    sr = r.mean() / r.std(ddof=1)
    sk, ku = _skew_kurt_raw(r)
    expected = probabilistic_sharpe(sr, expected_max_sharpe(81, 1 / 999), 1000, sk, ku)
    _, dsr1 = deflated_sharpe(w)
    _, dsr81 = deflated_sharpe(w, n_trials=81)
    assert dsr81 == pytest.approx(expected, abs=1e-10)
    assert dsr81 < dsr1
    assert overfit_report(w, seed=0, n_trials=81).n_trials == 81


def test_n_trials_below_columns_refused():
    with pytest.raises(ValueError):
        deflated_sharpe(_noise(7, 100, 3), n_trials=2)


def _skew_kurt_raw(r: np.ndarray) -> tuple[float, float]:
    x = r - r.mean()
    m2, m3, m4 = (x**2).mean(), (x**3).mean(), (x**4).mean()
    return m3 / m2**1.5, m4 / m2**2


# --- pbo_cscv: behaviour on known regimes ----------------------------------------------------
def test_pbo_near_one_when_is_winner_is_oos_loser():
    # Pure noise with every trial's full-sample mean forced to 0: IS outperformance is exactly
    # OOS underperformance, so selecting the IS-best always picks a below-median OOS trial.
    w = _noise(1, 1000, 50)
    assert pbo_cscv(w - w.mean()) >= 0.95


def test_pbo_near_zero_when_one_trial_dominates():
    w = _noise(0, 1000, 50)
    w["t00"] += 0.003
    assert pbo_cscv(w) <= 0.05


def test_pbo_averages_about_half_for_iid_noise():
    # For iid noise the IS-best's OOS rank is ~uniform, so PBO ~ 0.5 on average over datasets.
    pbos = [pbo_cscv(_noise(s, 400, 20), n_splits=8) for s in range(30)]
    assert 0.35 <= float(np.mean(pbos)) <= 0.65


PBO_NONE_CASES = [
    (1000, 9, 16),  # too few trials
    (31, 20, 16),  # fewer than 2 rows per split
]


@pytest.mark.parametrize("rows, trials, splits", PBO_NONE_CASES)
def test_pbo_none_when_insufficient(rows, trials, splits):
    assert pbo_cscv(_noise(0, rows, trials), n_splits=splits) is None


def test_pbo_rejects_odd_splits():
    with pytest.raises(ValueError):
        pbo_cscv(_noise(0, 100, 12), n_splits=5)


def test_pbo_matches_brute_force():
    # Independent, slow-but-obvious CSCV on a small problem
    w = _noise(9, 43, 12)  # 43 rows, 4 splits -> 10 rows per block, 3 dropped
    s, n = 4, 12
    blocks = [w.iloc[i * 10 : (i + 1) * 10] for i in range(s)]
    logits = []
    for c in combinations(range(s), s // 2):
        is_ = pd.concat([blocks[i] for i in c])
        oos = pd.concat([blocks[i] for i in range(s) if i not in c])
        sr_is = is_.mean() / is_.std()
        sr_oos = oos.mean() / oos.std()
        best = sr_is.idxmax()
        rank = sr_oos.rank()[best]
        omega = rank / (n + 1)
        logits.append(math.log(omega / (1 - omega)))
    expected = float(np.mean(np.array(logits) <= 0))
    assert pbo_cscv(w, n_splits=4) == pytest.approx(expected)


# --- stationary bootstrap ---------------------------------------------------------------------
def test_bootstrap_ci_covers_true_sharpe_in_most_seeds():
    mu, vol = 0.0005, 0.01
    true_sr = mu / vol * math.sqrt(252)
    covered = 0
    for s in range(20):
        r = np.random.default_rng(100 + s).normal(mu, vol, 1500)
        lo, hi = stationary_bootstrap_sharpe_ci(r, n_boot=400, mean_block=10, seed=s)
        assert lo < sharpe(r) < hi
        covered += lo <= true_sr <= hi
    assert covered >= 16  # nominal 95%; percentile block bootstrap undercovers slightly


def test_bootstrap_is_deterministic_in_seed():
    r = np.random.default_rng(0).normal(0.0005, 0.01, 500)
    a = stationary_bootstrap_sharpe_ci(r, 200, 5, seed=42)
    assert a == stationary_bootstrap_sharpe_ci(r, 200, 5, seed=42)
    assert a != stationary_bootstrap_sharpe_ci(r, 200, 5, seed=43)


STATIONARY_INDEX_CASES = [
    # mean_block -> expected fraction of positions that start a new block (= 1/mean_block)
    (1.0, 1.0),
    (5.0, 0.2),
    (20.0, 0.05),
]


@pytest.mark.parametrize("mean_block, rate", STATIONARY_INDEX_CASES)
def test_stationary_indices_block_rate(mean_block, rate):
    idx = _stationary_indices(2000, 200, mean_block, np.random.default_rng(0))
    assert idx.shape == (200, 2000)
    assert idx.min() >= 0 and idx.max() < 2000
    continues = (np.diff(idx, axis=1) % 2000) == 1  # within a block, step is +1 (wrapping)
    # a fresh block can coincidentally continue (prob 1/n), negligible here
    assert 1 - continues.mean() == pytest.approx(rate, abs=0.01)


# --- to_wide and overfit_report ---------------------------------------------------------------
def test_to_wide_fills_missing_days_with_zero():
    long = pd.DataFrame(
        {
            "trial_id": ["a", "a", "b"],
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-03"]),
            "ret": [0.01, -0.02, 0.03],
        }
    )
    w = to_wide(long)
    assert list(w.columns) == ["a", "b"]
    assert w.loc["2024-01-02", "b"] == 0.0
    assert w.loc["2024-01-03", "a"] == -0.02


def test_overfit_report_fields():
    w = _noise(0, 1000, 30)
    w["t07"] += 0.003
    rep = overfit_report(w, seed=1, n_boot=300)
    assert isinstance(rep, OverfitReport)
    assert rep.n_trials == 30
    assert rep.best_trial_id == "t07"
    assert rep.best_sharpe == pytest.approx(sharpe(w["t07"]))
    assert rep.deflated_sharpe > 0.95
    assert rep.pbo is not None and rep.pbo < 0.1
    lo, hi = rep.sharpe_ci95
    assert lo < rep.best_sharpe < hi


def test_overfit_report_pbo_none_with_few_trials():
    rep = overfit_report(_noise(0, 500, 5), seed=0, n_boot=100)
    assert rep.pbo is None
    assert rep.n_trials == 5
