"""ADR 0016 core: break-even hit rate, month-block CI, window returns, pool means, verdict."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import foresight_ub as fu


def test_q_star():
    q = fu.q_star(np.array([0.01, -0.03, 0.02]), 0.003)
    assert q["q_mean"] == pytest.approx((1 + 0.003 / 0.02) / 2)
    assert q["q_median"] == pytest.approx((1 + 0.003 / 0.02) / 2)
    assert np.isnan(fu.q_star(np.array([]), 0.003)["q_mean"])


def test_month_block_ci_covers_mean_and_shrinks_with_n():
    rng = np.random.default_rng(1)
    d = pd.date_range("2016-01-01", periods=2000, freq="D")
    x = rng.normal(0.01, 0.02, 2000)
    lo, hi = fu.month_block_ci(d, x, 2000, 3)
    assert lo < x.mean() < hi and hi - lo < 0.004
    lo2, hi2 = fu.month_block_ci(d[:200], x[:200], 2000, 3)
    assert hi2 - lo2 > hi - lo


def test_hedged_window_and_pool_mean():
    c = np.array([100.0, 110.0, 121.0, 100.0])
    h = np.array([10.0, 10.0, 11.0, 10.0])
    got = fu.hedged_window(c, h, np.array([0, 1, 3]), np.array([1, 2, 5]))
    assert got[0] == pytest.approx(0.1) and got[1] == pytest.approx(0.0) and np.isnan(got[2])
    assert fu.pool_mean_window(c, h, np.array([0, 1]), 1) == pytest.approx(0.05)
    assert np.isnan(fu.pool_mean_window(c, h, np.array([0]), 0))


def test_verdict():
    assert fu.verdict({"A": 0.001, "B": 0.002}, {"A": 0.01, "B": 0.01}) == "value"
    assert fu.verdict({"A": -0.01, "B": 0.002}, {"A": -0.001, "B": 0.01}) == "no value"
    assert fu.verdict({"A": -0.01, "B": 0.002}, {"A": 0.01, "B": 0.01}) == "inconclusive"
