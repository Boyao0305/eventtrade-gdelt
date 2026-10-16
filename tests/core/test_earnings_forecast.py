"""ADR 0018 core: splitter label rule, prep, models on a planted signal, rules, P&L stats."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import earnings_forecast as ef


def test_yearly_splits_respect_label_availability():
    dec = pd.Series(pd.to_datetime(["2016-03-01", "2017-12-28", "2018-01-03", "2018-06-01"]))
    lab = pd.Series(pd.to_datetime(["2016-03-04", "2018-01-02", "2018-01-08", "2018-06-04"]))
    sp = ef.yearly_splits(dec, lab, [2018])
    y, tr, te = sp[0]
    assert y == 2018 and te.tolist() == [2, 3]
    assert tr.tolist() == [0, 1]                      # label 2018-01-02 < first decision 01-03
    lab2 = lab.copy()
    lab2[1] = pd.Timestamp("2018-01-05")              # label not yet known -> excluded
    assert ef.yearly_splits(dec, lab2, [2018])[0][1].tolist() == [0]


def test_prep_imputes_with_train_medians_and_flags_missing():
    x = np.array([[1.0, np.nan], [3.0, 2.0], [5.0, 4.0]])
    p = ef.fit_prep(x)
    out = ef.apply_prep(p, np.array([[np.nan, 2.0]]))
    assert out.shape == (1, 4) and out[0, 2] == 1.0 and out[0, 3] == 0.0
    assert out[0, 0] == pytest.approx(0.0)            # median 3 = mean 3 -> 0


@pytest.mark.parametrize("model", ["L", "G"])
def test_models_learn_a_planted_signal(model):
    rng = np.random.default_rng(1)
    n = 6000
    x = rng.normal(size=(n, 3))
    r = 0.04 * np.sign(x[:, 0] + rng.normal(0, 1.5, n)) * np.abs(rng.normal(1, 0.3, n))
    y = (r > 0).astype(int)
    pred = ef.fit_predict(model, x[:4000], y[:4000], ef.weights(r[:4000]), x[4000:], 3)
    assert ef.weighted_hit(pred, r[4000:]) > 0.6


def test_rules_and_pnl():
    f = pd.DataFrame({"prev_sign_1": [1, -1, np.nan], "prev_sign_2": [1, -1, -1],
                      "prev_sign_3": [-1, -1, -1], "prev_sign_4": [np.nan, 1, -1],
                      "ret_63": [0.1, -0.2, np.nan]})
    r = ef.rule_predictions(f)
    assert r["W-R0"].tolist() == [1, 1, 1]
    assert r["W-R1"].tolist() == [1, -1, 1] and r["W-R2"].tolist() == [1, -1, 1]
    assert r["W-R3"].tolist() == [1, -1, -1]
    pnl = ef.net_pnl(np.array([1, -1]), np.array([0.05, 0.05]))
    assert pnl[0] == pytest.approx(0.047)
    assert pnl[1] == pytest.approx(-0.05 - 0.003 - 0.005 * 3 / 252)


def test_week_block_lower_and_calendar_t():
    rng = np.random.default_rng(2)
    d = pd.date_range("2018-01-01", periods=3000, freq="D")
    x = rng.normal(0.002, 0.03, 3000)
    lo, p = ef.week_block_lower(d, x, 2000, 1)
    assert lo < x.mean() and 0 < p < 1
    lo2, p2 = ef.week_block_lower(d, x + 0.01, 2000, 1)
    assert lo2 > 0 and p2 < 0.01
    assert ef.calendar_t(d, x + 0.01) > 3
