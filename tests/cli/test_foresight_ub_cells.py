"""ADR 0016 cell helpers on planted values (the full pipeline is exercised by a blind dry run)."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.cli import pipelines_foresight_ub as pfu


def test_signed_cell_detects_value_and_no_value():
    rng = np.random.default_rng(2)
    d = pd.date_range("2016-01-01", periods=600, freq="D")
    v = rng.normal(0.02, 0.01, 600)
    note = pfu._signed_cell("x", "A", d, v, np.zeros(600), 0.003, 2000, 1)
    assert note["D"] == pytest.approx(v.mean() - 0.003) and note["D_lo"] > 0
    note2 = pfu._signed_cell("x", "A", d, rng.normal(0, 0.01, 600), np.zeros(600), 0.003, 2000, 1)
    assert note2["D_hi"] < 0
    cost = np.full(600, 0.003)
    assert pfu._signed_cell("x", "A", d, v, np.zeros(600), cost, 500, 1)["cost"] == \
        pytest.approx(0.003)


def test_oracle_cell_sizes_moves_and_hit_rates():
    rng = np.random.default_rng(3)
    d = pd.date_range("2016-01-01", periods=300, freq="D")
    ev = np.abs(rng.normal(0, 0.04, 300))
    note = pfu._oracle_cell("A", d, ev, np.full(300, 0.01), np.abs(rng.normal(0, 0.0125, 3000)),
                            0.003, 1000, 1)
    assert note["ratio"] > 2 and note["delta_lo"] > 0
    assert 0.5 < note["event_q_mean"] < note["placebo_q_mean"] < 1


def test_pool_means_by_ticker_stratum():
    pool = pd.DataFrame({"ticker": ["A", "A", "B"], "stratum": [1, 1, 1], "value": [1.0, 3.0, 5.0]})
    got = pfu._pool_means([("A", 1), ("B", 1), ("C", 1)], pool)
    assert got[0] == 2.0 and got[1] == 5.0 and np.isnan(got[2])
