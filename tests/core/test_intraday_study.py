"""ADR 0007 core: clock and DST, bars grid, outages, windows, z baseline, outcomes, placebo null."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import intraday_study as st

NY = ZoneInfo("America/New_York")


def _ts(d: date, hh: int, mm: int) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp())


def _bars(d: date, ks: list[int], close: list[float] | None = None, trades: int = 50) -> pd.DataFrame:
    close = close or [100.0 + k for k in ks]
    rows = []
    for k, c in zip(ks, close, strict=True):
        m = 570 + 15 * k
        rows.append([_ts(d, m // 60, m % 60), "X", c - 0.5, c + 1, c - 1, c, 10.0, c, trades])
    return pd.DataFrame(rows, columns=["ts", "ticker", "open", "high", "low", "close", "volume",
                                       "vwap", "trade_count"])


# --- clock ------------------------------------------------------------------------------------

def test_tau_utc_slots_follow_dst():
    est, edt = date(2016, 1, 4), date(2016, 7, 5)
    s = st.tau_utc_slots([est, edt])
    assert s[0, 0] == (14 * 60 + 45) // 15  # 09:45 EST = 14:45Z
    assert s[1, 0] == (13 * 60 + 45) // 15  # 09:45 EDT = 13:45Z
    assert s[0, -1] == (20 * 60) // 15      # 15:00 EST = 20:00Z
    assert s.shape == (2, st.N_TAU)


def test_trading_calendar_flags_early_close():
    full, half = date(2016, 11, 23), date(2016, 11, 25)
    spy = pd.concat([_bars(full, list(range(26))), _bars(half, list(range(14)))])
    days, early = st.trading_calendar(spy["ts"])
    assert list(days) == [full, half] and list(early) == [False, True]


def test_bar_matrix_grid_and_session_filter():
    d = date(2016, 1, 4)
    b = _bars(d, [0, 1, 25])
    pre = b.iloc[[0]].copy()
    pre["ts"] = _ts(d, 9, 15)  # pre-market bar: k = -1, dropped
    m = st.bar_matrix(pd.concat([b, pre]), [d])
    assert m.close[0, 0] == 100 and m.close[0, 25] == 125 and np.isnan(m.close[0, 2])
    assert m.trades[0, 2] == 0 and m.trades[0, 1] == 50


def test_liquidity_counts_missing_bars_as_zero():
    d = [date(2016, 1, 4)]
    m = st.bar_matrix(_bars(d[0], list(range(10)), trades=100), d)
    assert st.median_trades(m, np.array([True])) == 0.0  # 16 of 26 bars missing


# --- outages and windows ----------------------------------------------------------------------

def test_missing_batches_and_window_validity():
    nd = 25
    g = np.full((nd, st.N_SLOTS), 100.0)
    pulled = np.zeros(st.N_SLOTS, dtype=bool)
    pulled[48:80] = True
    g[24, 60] = 10.0  # < 25% of the trailing median
    g[24, 70] = 0.0
    miss = st.missing_batches(g, pulled)
    assert miss[24, 60] and miss[24, 70] and miss.sum() == 2
    g2 = g.copy()
    g2[:, 10] = 0.0  # outside the pulled range: never missing
    assert not st.missing_batches(g2, pulled)[:, 10].any()
    valid = st.window_valid(miss)
    # window of tau slot s covers s-5..s-2; invalid also for the two slots after a missing one
    for s in range(60 + 2, 60 + 8):
        assert not valid[24, s], s
    assert valid[24, 60 + 1] and valid[24, 60 + 8]


def test_window_sums():
    m = np.zeros((1, st.N_SLOTS))
    m[0, 50:54] = [1, 2, 3, 4]
    w = st.window_sums(m)
    assert w[0, 55] == 10  # slots 50..53
    assert w[0, 56] == 9   # 51..54
    assert np.isnan(w[0, 4])


def test_slot_matrix_drops_non_trading_days():
    d = date(2016, 1, 4)
    rows = pd.DataFrame({"batch_ts": [_ts(d, 9, 0), _ts(d + timedelta(days=1), 9, 0)],
                         "n": [5, 7]})
    m = st.slot_matrix(rows, "n", [d])
    assert m.sum() == 5 and m[0, (14 * 60) // 15] == 5


# --- z baseline -------------------------------------------------------------------------------

def test_baseline_z_needs_15_values_floor_and_no_lookahead():
    rng = np.random.default_rng(0)
    nd = 40
    den = np.full((nd, st.N_SLOTS), 200.0)
    x = rng.normal(0.1, 0.01, (nd, st.N_SLOTS))
    valid = np.ones((nd, st.N_SLOTS), dtype=bool)
    z = st.baseline_z(x, den, valid, "share")
    assert np.isnan(z[:15]).all() and np.isfinite(z[15:]).all()
    # sd floor: a constant baseline cannot produce infinite z
    xc = np.full((nd, st.N_SLOTS), 0.1)
    xc[30, 7] = 0.1 + 2 / 200  # two extra counts over a denominator of 200 -> z = 2
    zc = st.baseline_z(xc, den, valid, "share")
    assert zc[30, 7] == pytest.approx(2.0)
    # changing day 35 leaves every earlier z unchanged
    x2 = x.copy()
    x2[35:] += 5
    z2 = st.baseline_z(x2, den, valid, "share")
    np.testing.assert_array_equal(z[:35], z2[:35])
    # invalid current window or zero denominator -> NaN; invalid baselines are skipped
    v2 = valid.copy()
    v2[30, 3] = False
    assert np.isnan(st.baseline_z(x, den, v2, "share")[30, 3])


def test_tone_sd_floor():
    nd = 30
    x = np.full((nd, st.N_SLOTS), -2.0)
    x[25, 0] = -3.0
    z = st.baseline_z(x, np.ones_like(x), np.ones_like(x, dtype=bool), "tone")
    assert z[25, 0] == pytest.approx(-2.0)  # -1 / 0.5


def test_first_firing_declusters():
    f = np.zeros((2, st.N_TAU), dtype=bool)
    f[0, [3, 7]] = True
    assert list(st.first_firing(f)) == [3, -1]


# --- outcomes ---------------------------------------------------------------------------------

def _panel_one_day(asset_ks, asset_close, hedge_ks=None, hedge_close=None):
    d = [date(2016, 1, 4)]
    mats = [st.bar_matrix(_bars(d[0], asset_ks, asset_close), d)]
    hedge = [-1]
    if hedge_ks is not None:
        mats.append(st.bar_matrix(_bars(d[0], hedge_ks, hedge_close), d))
        hedge = [1, -1]
    return st.build_panel(mats, hedge)


def test_exit_rules_and_hedge():
    ks = list(range(26))
    close = [100.0] * 26
    close[2], close[6], close[25] = 100.0, 110.0, 120.0
    p = _panel_one_day(ks, close, ks, [50.0] * 25 + [55.0])
    a, d, j = np.array([0]), np.array([0]), np.array([2])
    assert st.outcome(p, a, d, j, "h60")[0] == pytest.approx(0.10)       # bar 6
    assert st.outcome(p, a, d, j, "hC")[0] == pytest.approx(0.20 - 0.10)  # hedge +10%
    # missing exit bar -> last bar after entry; none after entry -> 0
    p2 = _panel_one_day([2, 4], [100.0, 105.0])
    assert st.outcome(p2, a, d, j, "h60")[0] == pytest.approx(0.05)
    p3 = _panel_one_day([2], [100.0])
    assert st.outcome(p3, a, d, j, "hC")[0] == 0.0
    # no entry bar -> not eligible (NaN)
    assert np.isnan(st.outcome(p2, a, d, np.array([3]), "h60")[0])
    assert not st.eligible(p2, a, d, np.array([3]))[0]


def test_hedge_missing_entry_makes_ineligible():
    p = _panel_one_day([2, 6], [100.0, 101.0], [6], [50.0])
    a, d, j = np.array([0]), np.array([0]), np.array([2])
    assert not st.eligible(p, a, d, j)[0]


def test_pre_move_uses_first_open_before_1100():
    ks = list(range(26))
    close = [100.0 + k for k in ks]
    p = _panel_one_day(ks, close)
    a, d = np.array([0, 0]), np.array([0, 0])
    pre = st.outcome_pre(p, a, d, np.array([2, 8]))
    assert pre[0] == pytest.approx(102.0 / 99.5 - 1)   # first open = close(bar 0) - 0.5
    assert pre[1] == pytest.approx(108.0 / 103.0 - 1)  # close of bar 3 (= tau - 75)


def test_future_prices_do_not_change_entry_or_pre():
    ks = list(range(26))
    c1 = [100.0 + k for k in ks]
    c2 = c1[:7] + [999.0] * 19
    p1, p2 = _panel_one_day(ks, c1), _panel_one_day(ks, c2)
    a, d, j = np.array([0]), np.array([0]), np.array([6])
    assert st.outcome_pre(p1, a, d, j)[0] == st.outcome_pre(p2, a, d, j)[0]
    assert p1.entry[0, 0, 6] == p2.entry[0, 0, 6]


# --- placebo ----------------------------------------------------------------------------------

def test_placebo_days_distinct_local_then_widen():
    rng = np.random.default_rng(1)
    years = np.array([2016] * 300)
    orig = np.array([100, 101, 150])
    for _ in range(50):
        out = st.placebo_days(rng, orig, years, np.arange(300))
        assert len(set(out)) == 3
        assert all(abs(o - x) <= st.PLACEBO_RADIUS for o, x in zip(orig, out, strict=True))
    # pool of 3 days for 3 events: forced distinct
    out = st.placebo_days(rng, np.array([0, 1, 2]), np.array([2016] * 3), np.arange(3))
    assert sorted(out) == [0, 1, 2]
    with pytest.raises(ValueError):
        st.placebo_days(rng, np.array([0, 1]), np.array([2016, 2016]), np.array([0]))
    # only pool days are used (e.g. early closes excluded)
    pool = np.array([d for d in range(300) if d % 2 == 0])
    for _ in range(20):
        assert all(x % 2 == 0 for x in st.placebo_days(rng, orig, years, pool))


def _synthetic(effect: float, seed: int):
    rng = np.random.default_rng(seed)
    nd, n_ev = 400, 120
    vals = rng.normal(0, 0.01, (nd, 3, st.N_TAU))
    ev_days = rng.choice(nd, n_ev, replace=False)
    ev = pd.DataFrame({"day": ev_days, "asset": rng.integers(0, 3, n_ev),
                       "j": rng.integers(0, st.N_TAU, n_ev)})
    vals[ev["day"], ev["asset"], ev["j"]] += effect

    def at(day, asset, j):
        return vals[np.asarray(day), np.asarray(asset, dtype=int), np.asarray(j)]

    return ev, at, np.array([2016 + i // 252 for i in range(nd)])


def test_placebo_null_calibration_and_power():
    ps = []
    for seed in range(40):
        ev, at, years = _synthetic(0.0, seed)
        ps.append(st.placebo_test(ev, at, years, np.arange(400), 1, 199, seed).p)
    ps = np.array(ps)
    assert 0.25 < ps.mean() < 0.75 and (ps < 0.05).mean() <= 0.15
    ev, at, years = _synthetic(0.006, 99)
    r = st.placebo_test(ev, at, years, np.arange(400), 1, 199, 7)
    assert r.p < 0.02 and r.mean > 0
    r2 = st.placebo_test(ev, at, years, np.arange(400), 0, 199, 7)  # two-sided also detects it
    assert r2.p < 0.02
    r3 = st.placebo_test(ev, at, years, np.arange(400), -1, 199, 7)  # wrong direction: large p
    assert r3.p > 0.5


def test_placebo_insufficient_events_gives_nan():
    ev, at, years = _synthetic(0.0, 3)
    r = st.placebo_test(ev.iloc[:10], at, years, np.arange(400), 1, 99, 1)
    assert np.isnan(r.p) and r.n_events == 10
    with pytest.raises(ValueError):
        st.placebo_test(ev, at, years, np.arange(400), 2, 99, 1)


def test_abdi_ranaldo_zero_for_flat_bars():
    d = [date(2016, 1, 4)]
    b = _bars(d[0], list(range(26)), [100.0] * 26)
    b["high"] = b["low"] = 100.0
    m = st.bar_matrix(b, d)
    assert st.abdi_ranaldo_spread(m, np.array([True])) == 0.0
