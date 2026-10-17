"""ADR 0019 core: bar grid, hedged flow, late-day return, windows, volume and short features,
announcement day."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import informed as inf


def _grid(nd, ret_fn, vol=100.0):
    o = np.full((nd, inf.N_BARS), 100.0)
    c = o * (1 + ret_fn(nd))
    v = np.full((nd, inf.N_BARS), vol)
    return {"open": o, "close": c, "volume": v}


def test_bar_arrays_places_bars():
    days = [date(2019, 3, 1), date(2019, 3, 4)]
    ts = int(pd.Timestamp("2019-03-04 09:45", tz="America/New_York").timestamp())
    bars = pd.DataFrame({"ts": [ts], "open": [1.0], "close": [2.0], "volume": [5.0]})
    g = inf.bar_arrays(bars, days)
    assert g["close"][1, 1] == 2.0 and np.isnan(g["close"][0, 1])


def test_daily_flow_hedged_classification_and_late_return():
    nd = 3
    asset = _grid(nd, lambda n: np.full((n, inf.N_BARS), 0.002))
    hedge = _grid(nd, lambda n: np.full((n, inf.N_BARS), 0.001))
    asset["close"][1] = asset["open"][1] * 0.999       # day 1: trails the hedge
    f = inf.daily_flow(asset, hedge, np.array([False, False, True]))
    assert f["up"][0] == 2600 and f["down"][0] == 0
    assert f["down"][1] == 2600
    assert f["vol"][2] == 100 * inf.K_1300               # early close: bars from 13:00 dropped
    # all bars flat-priced within a bar -> last close / 15:00 open - 1 = 0.002 - 0.001
    assert f["late"][0] == pytest.approx(0.001)


def test_windows_exclude_earlier_earnings_and_features():
    n = 200
    a = 150
    earn = np.array([100])
    b = inf.baseline_mask(a, n, earn)
    assert b[a - 72] and b[a - 13] and not b[a - 12] and not b[103] and not b[110] and b[111]
    w = inf.window_mask(a, n)
    assert w[a - 12] and w[a - 3] and not w[a - 2]
    v = np.full(n, 100.0)
    v[a - 12:a - 2] = 200.0
    assert inf.volume_ratio(v, a, earn) == pytest.approx(np.log(2))
    sr = np.full(n, 0.4)
    sr[a - 12:a - 2] = 0.5
    assert inf.short_feature(sr, a, earn) == pytest.approx(0.1)
    flow = {"up": np.full(n, 60.0), "down": np.full(n, 40.0), "vol": np.full(n, 100.0),
            "has": np.ones(n, bool), "late": np.full(n, 0.001)}
    flow["up"][a - 12:a - 2] = 80.0
    flow["down"][a - 12:a - 2] = 20.0
    got = inf.flow_features(flow, a, earn)
    assert got["X2"] == pytest.approx(0.6 - 0.2) and got["X6"] == pytest.approx(0.01)
    flow["has"][a - 12:a - 5] = False                   # only 3 W days with bars
    assert np.isnan(inf.flow_features(flow, a, earn)["X2"])


def test_market_log_ratio_and_announcement_day():
    vol = np.full((120, 3), 100.0)
    vol[100:110, :] = 300.0
    mlr = inf.market_log_ratio(vol)
    assert mlr[112] == pytest.approx(np.log(3)) and np.isnan(mlr[10])
    cal = pd.bdate_range("2019-01-01", periods=30)
    assert inf.announcement_day(date(2019, 1, 10), date(2019, 1, 7), None, cal) == \
        pd.Timestamp("2019-01-07")
    assert inf.announcement_day(date(2019, 1, 10), date(2019, 1, 10),
                                "2019-01-10T21:30:00.000Z", cal) == pd.Timestamp("2019-01-11")
    assert inf.announcement_day(date(2019, 1, 10), None, "2019-01-10T14:00:00.000Z", cal) == \
        pd.Timestamp("2019-01-10")


def test_other_earnings_excludes_the_events_own_filing():
    import numpy as np

    from eventtrade.cli.pipelines_informed import _other_earnings

    ep = np.array([40, 99])           # 99 = the event's own 8-K, filed after 16:00 (a = 100)
    assert not _other_earnings(ep, 100, 99)
    assert _other_earnings(np.array([90, 99]), 100, 99)      # a 2.02 8-K 10 days before
    assert not _other_earnings(None, 100, 99)
