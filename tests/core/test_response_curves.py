"""ADR 0017 core: CAR with beta, standardised excess variance shapes (permanent, reverting),
continuation, pools (exclusive), block CI, time-scale readouts."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import response_curves as rc


def _walk(n, seed, sd=0.01):
    return 100 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, sd, n)))


def test_asset_curves_beta_and_car():
    n = 600
    h = _walk(n, 1)
    rh = np.concatenate([[0], h[1:] / h[:-1] - 1])
    eps = np.random.default_rng(2).normal(0, 0.005, n)
    c = 50 * np.cumprod(1 + 1.5 * rh + eps)
    ac = rc.asset_curves(c, h, (1, 5))
    p = 400
    assert ac.sigma[p] == pytest.approx(0.005, rel=0.2)
    want = (c[p + 5] / c[p] - 1) - 1.5 * (h[p + 5] / h[p] - 1)
    assert ac.car[5][p] == pytest.approx(want, abs=0.004)
    raw = rc.asset_curves(c, None, (1,))
    assert raw.car[1][p] == pytest.approx(c[p + 1] / c[p] - 1)


def _world(kind):
    """20 assets, events every 60 days; a 5% jump on day b+1 that is permanent, reverts by b+10,
    or keeps drifting."""
    n, ks = 1500, (1, 2, 3, 5, 10, 20, 40)
    curves, pool_ok, key, ev_a, ev_p, ev_d = {}, {}, {}, [], [], []
    days = pd.bdate_range("2016-01-04", periods=n)
    for a in range(20):
        r = np.random.default_rng(10 + a).normal(0, 0.01, n)
        sgn = np.where(np.random.default_rng(99 + a).random(n) < 0.5, -1, 1)
        evs = np.arange(300 + a, n - 70, 60)
        for e in evs:
            s = sgn[e]
            r[e + 1] += 0.05 * s
            if kind == "revert":
                r[e + 6:e + 11] -= 0.01 * s
            if kind == "drift":
                r[e + 2:e + 21] += 0.002 * s
        c = 100 * np.cumprod(1 + r)
        name = f"A{a}"
        curves[name] = rc.asset_curves(c, None, ks)
        pool_ok[name] = np.ones(n, dtype=bool)
        key[name] = days.year.to_numpy()
        ev_a += [name] * len(evs)
        ev_p += list(evs)
        ev_d += list(days[evs])
    ev = rc.CellEvents(np.array(ev_a), np.array(ev_p), np.array(ev_d))
    return curves, ev, pool_ok, key, ks


@pytest.mark.parametrize("kind", ["permanent", "revert", "drift"])
def test_shapes(kind):
    curves, ev, pool_ok, key, ks = _world(kind)
    evk, cont = [], []
    for k in ks:
        out = rc.event_x(curves, ev, pool_ok, key, k)
        evk.append(np.nanmean(out["z2"][1]) * k)
        cont.append(np.nanmean(out["cont"][1]))
        assert abs(np.nanmean(out["signed"][1])) < 0.01          # random signs: no direction
    if kind == "permanent":
        assert evk[-1] > 0.6 * max(evk) and abs(cont[-1]) < 0.01
    if kind == "revert":
        k_star, hl = rc.peak_half_life(ks, evk)
        assert k_star <= 5 and hl in (10, 20) and cont[ks.index(10)] < -0.03
        assert rc.reversal_half_life(ks, cont, 0.05) == 10
    if kind == "drift":
        assert evk[ks.index(20)] > evk[0] and cont[ks.index(20)] > 0.03


def test_exclusive_pool_drops_windows_containing_events():
    n = 100
    c = 100 * np.ones(n)
    c[51:] = 110.0                                           # jump right after base 50
    ac = rc.asset_curves(c, None, (1, 5))
    ev = rc.CellEvents(np.array(["X"]), np.array([50]), np.array([pd.Timestamp("2016-03-11")]))
    ok = {"X": np.ones(n, bool)}
    key = {"X": np.zeros(n, int)}
    inc = rc.event_x({"X": ac}, ev, ok, key, 5, exclusive=False)["signed"][1][0]
    exc = rc.event_x({"X": ac}, ev, ok, key, 5, exclusive=True)["signed"][1][0]
    assert exc == pytest.approx(0.1, abs=0.002)   # pool: no jump windows but the event itself
    assert inc < exc


def test_block_ci_and_readouts():
    d = pd.date_range("2016-01-01", periods=2000, freq="D")
    x = np.random.default_rng(4).normal(0.02, 0.05, 2000)
    lo, hi = rc.block_ci(d, x, 2000, 1, 0.99)
    assert lo < 0.02 < hi
    assert rc.peak_half_life([1, 2, 3], [0.1, 0.4, 0.3]) == (2, "persistent")
    assert rc.peak_half_life([1, 2, 3], [-1, -2, -3]) == (None, None)
