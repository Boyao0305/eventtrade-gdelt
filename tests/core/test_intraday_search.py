"""ADR 0008 search core: window algebra, fast z == ADR 0007 z, exits, placebo max-t calibration."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import intraday_search as se
from eventtrade.core import intraday_study as st


def test_window_bounds_match_adr0007():
    assert se.window_bounds(15, 60) == st.WINDOW
    assert se.window_bounds(0, 15) == (1, 1)    # tau uses the batch of slot s-1 (available at s)
    assert se.window_bounds(15, 120) == (9, 2)
    with pytest.raises(ValueError):
        se.window_bounds(10, 60)


def test_window_sums_and_validity_generalize():
    rng = np.random.default_rng(0)
    m = rng.integers(0, 9, (3, st.N_SLOTS)).astype(float)
    np.testing.assert_array_equal(se.window_sums_lw(m, 15, 60), st.window_sums(m))
    w = se.window_sums_lw(m, 0, 30)
    assert w[1, 50] == m[1, 48] + m[1, 49]
    miss = np.zeros((2, st.N_SLOTS), dtype=bool)
    miss[1, 40] = True
    np.testing.assert_array_equal(se.window_valid_lw(miss, 15, 60), st.window_valid(miss))
    v = se.window_valid_lw(miss, 0, 15)  # window slot s-1; invalid for s-1 in 40..42
    assert not v[1, 41] and not v[1, 43] and v[1, 44] and v[1, 40]


def test_fast_z_equals_reference():
    rng = np.random.default_rng(1)
    nd = 60
    den = rng.integers(0, 300, (nd, st.N_SLOTS)).astype(float)
    x = np.where(den > 0, rng.binomial(30, 0.2, (nd, st.N_SLOTS)) / np.maximum(den, 1), np.nan)
    valid = rng.random((nd, st.N_SLOTS)) > 0.1
    for kind in ("share", "tone"):
        a = st.baseline_z(x, den, valid, kind)
        b = se.baseline_z_fast(x, den, valid, kind)
        np.testing.assert_allclose(a, b, rtol=1e-7, atol=1e-9, equal_nan=True)


def test_exit_bars_clip_to_close():
    j = np.array([0, 20, 21])
    assert list(se.exit_bars(j, "15")) == [1, 21, 22]
    assert list(se.exit_bars(j, "120")) == [8, 25, 25]
    assert list(se.exit_bars(j, "C")) == [25, 25, 25]


def test_grid_has_160_configs_and_32_trigger_sets():
    g = se.grid()
    assert len(g) == 160 and len({c.key for c in g}) == 160
    assert len(se.configs_by_trigger(g)) == 32


def _world(effect, seed, n_cfg=6):
    rng = np.random.default_rng(seed)
    nd = 400
    vals = rng.normal(0, 0.01, (n_cfg, nd, 3, st.N_TAU))
    events = {}
    for c in range(n_cfg):
        days = rng.choice(nd, 80, replace=False)
        ev = pd.DataFrame({"day": days, "asset": rng.integers(0, 3, 80),
                           "j": rng.integers(0, st.N_TAU, 80)})
        if c == 0:
            vals[c, ev["day"], ev["asset"], ev["j"]] += effect
        events[f"c{c}"] = ev

    def at(k, d, a, j):
        return vals[int(k[1:]), d, a, j]

    return events, at, np.array([2016 + i // 252 for i in range(nd)]), np.arange(nd)


def test_max_t_search_calibrated_and_powerful():
    ps = [se.max_t_search(*_world(0.0, s)[:2], *_world(0.0, s)[2:], 1, 99, s).p
          for s in range(25)]
    ps = np.array(ps)
    assert (ps < 0.05).mean() <= 0.16 and 0.2 < ps.mean() < 0.8
    ev, at, years, pool = _world(0.008, 77)
    r = se.max_t_search(ev, at, years, pool, 1, 99, 3)
    assert r.best == "c0" and r.p < 0.05


def test_block_placebo_permutes_within_blocks():
    rng = np.random.default_rng(3)
    pool = np.arange(10, 300, 2)
    for _ in range(20):
        m = se.block_placebo(rng, pool)
        assert sorted(m) == sorted(pool)
        assert np.abs(np.searchsorted(pool, m) - np.arange(len(pool))).max() < st.PLACEBO_RADIUS


def _sp(open_, close, hedge=None):
    pairs = [(open_, close), *([hedge] if hedge else [])]
    ones = np.ones((2, st.N_BARS))
    p = se.empty_search_panel(len(pairs), 2, [1, -1] if hedge else [-1])
    for i, (o, c) in enumerate(pairs):
        se.fill_search_row(p, i, st.BarMatrix(np.array(o, dtype=float), np.array(c, dtype=float),
                                              ones, ones, ones))
    return p


def test_search_return_enters_at_next_open_and_hedges():
    o = [[100.0] * 26, [200.0 + k for k in range(26)]]
    c = [[100.0] * 26, [200.5 + k for k in range(26)]]
    p = _sp(o, c)
    a, d = np.array([0]), np.array([1])
    # tau index j=2 -> entry open of bar 3 (=203), exit 60 min -> close of bar 6 (=206.5)
    assert se.search_return(p, a, d, np.array([2]), "60")[0] == pytest.approx(206.5 / 203 - 1)
    # 15-min exit: open of bar 3 to close of bar 3
    assert se.search_return(p, a, d, np.array([2]), "15")[0] == pytest.approx(203.5 / 203 - 1)
    hedge = ([[50.0] * 26, [50.0] * 26], [[50.0] * 26, [50.0] * 25 + [55.0]])
    ph = _sp(o, c, hedge)
    r = se.search_return(ph, a, d, np.array([2]), "C")[0]
    assert r == pytest.approx((225.5 / 203 - 1) - (55 / 50 - 1))


def test_price_at_uses_previous_close_before_open():
    o = [[100.0] * 26, [200.0 + k for k in range(26)]]
    c = [[100.0 + k for k in range(26)], [200.5 + k for k in range(26)]]
    p = _sp(o, c)
    a, d = np.array([0, 0, 0]), np.array([1, 1, 1])
    got = se.price_at(p, a, d, np.array([9 * 60, 9 * 60 + 45, 16 * 60 + 30]))
    assert list(got) == [125.0, 200.5, 225.5]  # prev close; bar 0 close; last close


def test_fast_cell_equals_reference():
    rng = np.random.default_rng(9)
    for n in (5, 40, 300):
        d = rng.integers(0, 500, n)
        v = rng.normal(0.001, 0.01, n)
        v[rng.random(n) < 0.1] = np.nan
        mean, t, ne, nd = se.fast_cell(d, v, 500)
        ok = np.isfinite(v)
        ref = st.daily_series(d[ok], v[ok])
        rm, rt = st.cell_t(ref)
        assert nd == len(ref) and ne == int(ok.sum())
        assert mean == pytest.approx(rm) and t == pytest.approx(rt, rel=1e-9)



def test_table_max_t_equals_reference():
    for seed, effect in ((0, 0.0), (5, 0.004), (11, 0.0)):
        ev, at, years, pool = _world(effect, seed)
        a = se.max_t_search(ev, at, years, pool, 1, 59, seed)
        b = se.max_t_search_reference(ev, at, years, pool, 1, 59, seed)
        assert a.best == b.best and a.n_valid_draws == b.n_valid_draws
        assert a.p == pytest.approx(b.p) and a.best_stat == pytest.approx(b.best_stat)
        two = se.max_t_search(ev, at, years, pool, 0, 59, seed)
        two_ref = se.max_t_search_reference(ev, at, years, pool, 0, 59, seed)
        assert two.p == pytest.approx(two_ref.p)
