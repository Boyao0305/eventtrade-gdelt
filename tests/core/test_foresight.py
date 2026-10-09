"""ADR 0009 core: fresh filter, foresight placebo with exclusion, shocks, 24h response rate."""

import numpy as np
import pytest

from eventtrade.core import foresight as fs
from eventtrade.core import intraday_study as st


def test_fresh_ignores_overlapping_windows_and_checks_24h():
    z = np.zeros((3, st.N_SLOTS))
    d, s = np.array([2]), np.array([60])
    assert fs.fresh(z, d, s)[0]
    z[2, 58] = 5.0  # overlaps the event window (s' >= s-3): ignored
    assert fs.fresh(z, d, s)[0]
    z[2, 56] = 1.5  # earlier today, non-overlapping: not fresh
    assert not fs.fresh(z, d, s)[0]
    z[2, 56] = 0.0
    z[1, 70] = 2.0  # previous day after s-4: within 24h -> not fresh
    assert not fs.fresh(z, d, s)[0]
    z[1, 70] = 0.0
    z[1, 40] = 9.0  # previous day before s-4: more than 24h earlier -> fine
    z[2, 59] = np.nan
    assert fs.fresh(z, d, s)[0]


def _null_world(seed, effect=0.0):
    rng = np.random.default_rng(seed)
    nd, na = 600, 5
    vals = rng.normal(0, 0.01, (nd, na))
    d = rng.choice(nd, 120, replace=False)
    a = rng.integers(0, na, 120)
    vals[d, a] += effect

    def at(dd, aa, jj):
        return vals[dd, aa]

    return d, a, at, np.arange(nd)


def test_foresight_test_calibrated_powerful_and_excludes():
    ps = [fs.foresight_test(*_null_world(s)[:2], _null_world(s)[2], np.zeros(120, int),
                            _null_world(s)[3], 1, 1, 199, s).p for s in range(40)]
    ps = np.array(ps)
    assert (ps < 0.05).mean() <= 0.15 and 0.25 < ps.mean() < 0.75
    d, a, at, pool = _null_world(1, effect=0.006)
    assert fs.foresight_test(d, a, at, np.zeros(120, int), pool, 1, 1, 199, 2).p < 0.02
    # huge exclusion: almost every placebo event is dropped -> few valid draws, NaN-safe
    r = fs.foresight_test(d, a, at, np.zeros(120, int), pool, 1, 10**6, 19, 2)
    assert r.n_valid_draws == 0 and np.isnan(r.p)


def test_shocks_detect_planted_moves():
    rng = np.random.default_rng(4)
    r = rng.normal(0, 0.001, (60, st.N_BARS))
    r[:, 0] = np.nan
    r[40, 10] = 0.02
    d, k = fs.shocks(r, np.ones(60, dtype=bool))
    assert (40, 10) in set(zip(d.tolist(), k.tolist(), strict=True))
    assert d.min() >= 15  # needs >= 15 prior observations
    d2, _ = fs.shocks(r, np.arange(60) != 40)
    assert 40 not in set(d2.tolist())


def test_bar_returns_hedged():
    c = np.array([[100.0, 101.0, 101.0]])
    h = np.array([[50.0, 50.5, 50.0]])
    r = fs.bar_returns(c, h)
    assert np.isnan(r[0, 0])
    assert r[0, 1] == pytest.approx(0.01 - 0.01) and r[0, 2] == pytest.approx(0 - (-0.0099009901))


def test_response_rate_wraps_to_next_day():
    fire = np.zeros((2, st.N_SLOTS), dtype=bool)
    usable = np.ones((2, st.N_SLOTS), dtype=bool)
    fire[0, 70] = True
    fire[1, 10] = True
    fire[1, 60] = True  # after the 24h horizon of a shock at slot 50
    rate = fs.response_rate(fire, usable, np.array([0]), np.array([50]))
    assert rate[0] == pytest.approx(2 / 96)
    usable[:, :48] = False
    rate2 = fs.response_rate(fire, usable, np.array([0]), np.array([50]))
    assert rate2[0] == pytest.approx(1 / (45 + 3))  # day0 51..95 + day1 48..50



def test_table_foresight_equals_reference():
    for seed, effect, excl in ((0, 0.0, 1), (3, 0.005, 2), (8, 0.0, 6)):
        d, a, at, pool = _null_world(seed, effect)
        j = np.zeros(len(d), int)
        x = fs.foresight_test(d, a, at, j, pool, 1, excl, 59, seed)
        y = fs.foresight_test_reference(d, a, at, j, pool, 1, excl, 59, seed)
        assert x.n_valid_draws == y.n_valid_draws and x.p == pytest.approx(y.p, nan_ok=True)
