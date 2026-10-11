"""ADR 0014 core: bar-grid values, FOMC cycle, NW slope, placebo test."""

from datetime import date

import numpy as np
import pytest

from eventtrade.core import macro as mc
from eventtrade.core.intraday_study import N_BARS, BarMatrix


def grid(close: np.ndarray, open0: np.ndarray | None = None) -> BarMatrix:
    c = np.asarray(close, float)
    o = c.copy()
    if open0 is not None:
        o[:, 0] = open0
    return BarMatrix(o, c, c, c, np.ones_like(c))


def test_positions_move_to_next_trading_day_and_drop_out_of_range():
    days = [date(2020, 3, 13), date(2020, 3, 16), date(2020, 3, 17)]
    got = mc.positions(days, ["2020-03-15", "2020-03-13", "2020-03-16", "2020-03-20"])
    assert got.tolist() == [0, 1]


def test_last_close_handles_early_close():
    c = np.full((2, N_BARS), 100.0)
    c[1, 14:] = np.nan
    c[1, 13] = 99.0
    assert mc.last_close(grid(c)).tolist() == [100.0, 99.0]


def test_day_values_timing():
    c = np.full((3, N_BARS), 100.0)
    c[1, :] = 101.0
    c[1, mc.K_1345] = 102.0      # pre-statement price on day 1
    c[1, mc.K_1430] = 103.0      # after the statement: up
    c[1, -1] = 104.0             # close of day 1
    c[2, :] = 100.0
    o = np.array([100.0, 100.5, 99.0])   # gap up on day 1, gap down on day 2
    v = mc.day_values(grid(c, o), np.array([False, False, False]))
    assert v["M1"][1] == pytest.approx(102 / 100 - 1)
    assert v["M3"][1] == pytest.approx(104 / 100 - 1)
    assert v["M4"][1] == pytest.approx(104 / 101 - 1)          # +1 x (close / p0945 - 1)
    assert v["M5"][1] == pytest.approx(104 / 103 - 1)          # reaction up, so +1
    assert v["M4"][2] == pytest.approx(-(100 / 100 - 1))
    assert np.isnan(v["M5"][2])                                # reaction exactly 0
    assert np.isnan(v["M1"][0])                                # no previous close
    early = mc.day_values(grid(c, o), np.array([False, True, False]))
    assert all(np.isnan(early[k][1]) for k in ("M1", "M3", "M4", "M5"))


def test_cycle_day_and_even_weeks():
    cd = mc.cycle_day(50, np.array([5, 40]))
    assert np.isnan(cd[:4]).all()
    assert cd[4] == -1 and cd[5] == 0 and cd[38] == 33 and cd[39] == -1 and cd[40] == 0
    e = mc.even_week(cd)
    assert e[4] == 1 and e[8] == 1 and e[9] == 0 and e[14] == 1 and e[13] == 0
    assert np.isnan(e[0])
    assert mc.blocks(np.array([1, 1, 0, 1, 0, 0, 1, 1])) == 3


def test_nw_slope_recovers_difference_in_means():
    rng = np.random.default_rng(1)
    x = (np.arange(4000) // 5) % 2.0
    y = 0.001 * x + rng.normal(0, 0.01, 4000)
    b, t = mc.nw_slope(y, x)
    assert b == pytest.approx(y[x == 1].mean() - y[x == 0].mean())
    assert t > 2


def test_placebo_test_finds_planted_effect_and_not_noise():
    rng = np.random.default_rng(2)
    n = 2000
    year = np.arange(n) // 250
    weekday = np.arange(n) % 5
    v = rng.normal(0, 0.01, n)
    ev = np.arange(2, n, 25)                     # all weekday 2
    pool = np.setdiff1d(np.arange(n), np.concatenate([ev - 1, ev, ev + 1]))
    null_res = mc.placebo_test(v, ev, pool, weekday, year, 2000, 7, 1)
    assert null_res.p > 0.05
    v2 = v.copy()
    v2[ev] += 0.008
    hit = mc.placebo_test(v2, ev, pool, weekday, year, 2000, 7, 1)
    assert hit.p < 0.01 and hit.n == len(ev)
    two = mc.placebo_test(-v2, ev, pool, weekday, year, 2000, 7, 0)
    assert two.p < 0.02 and two.mean < 0


def test_placebo_test_refuses_too_small_pool():
    v = np.zeros(10)
    with pytest.raises(ValueError, match="too small"):
        mc.placebo_test(v, np.array([0, 5, 7]), np.array([1, 2]), np.zeros(10, int), np.zeros(10, int),
                        10, 1, 1)


def test_session_flags_cross_check_list_and_bars():
    days = [date(2019, 11, 27), date(2019, 11, 29), date(2019, 12, 2), date(2019, 12, 3)]
    c = np.full((4, N_BARS), 100.0)
    c[1, 14:] = np.nan            # listed early close, bars stop at 13:00: early
    c[2, 14:] = np.nan            # not listed, bars stop at 13:00: bad
    c[3, :] = np.nan              # no bars: bad
    early, bad = mc.session_flags(grid(c), days, ["2019-11-29", "2019-12-24"])
    assert early.tolist() == [False, True, False, False]
    assert bad.tolist() == [False, False, True, True]


def test_event_sets_rules():
    days = [date(2021, 4, 1), date(2021, 4, 5), date(2021, 4, 6), date(2021, 4, 7),
            date(2021, 4, 8), date(2021, 4, 9), date(2021, 4, 12), date(2021, 4, 13),
            date(2021, 4, 14), date(2021, 4, 15), date(2021, 4, 16), date(2021, 4, 19),
            date(2021, 4, 20), date(2021, 4, 21), date(2021, 4, 22)]
    drop = np.zeros(len(days), dtype=bool)
    drop[14] = True
    es = mc.event_sets(days, drop,
                       fomc_scheduled=["2021-04-07", "2021-04-13", "2021-04-20"],
                       fomc_cancelled=["2021-04-20"], fomc_unscheduled=["2021-04-17"],
                       cpi=["2021-04-13"], nfp=["2021-04-02", "2021-04-09"],
                       press=["2021-04-07"])
    assert es.fomc.tolist() == [3, 7]                  # 04-20 cancelled
    assert es.overlap.tolist() == [7]                  # FOMC and CPI on 04-13
    assert es.events["M1"].tolist() == [3]
    assert es.events["M4"].tolist() == [5]             # Good Friday NFP is not an event
    assert es.events["M3"].tolist() == [3, 5, 7]
    assert es.press.tolist() == [3]
    # pool: no announcement day (incl. 04-02 -> 04-05, 04-17 -> 04-19, 04-20) or neighbour, no drop
    assert es.pool.tolist() == [9]


def test_placebo_test_without_events_is_nan():
    r = mc.placebo_test(np.full(10, np.nan), np.array([1]), np.arange(10), np.zeros(10, int),
                        np.zeros(10, int), 10, 1, 1)
    assert r.n == 0 and np.isnan(r.p)


def test_calendar_is_pinned():
    """Amendment 10: per-year counts, cancellations and the FOMC/08:30 overlaps are frozen."""
    from collections import Counter

    from eventtrade.core import macro_calendar as mcal

    def per_year(xs):
        return Counter(int(x[:4]) for x in xs)

    assert per_year(mcal.FOMC_SCHEDULED) == {**{y: 8 for y in range(2015, 2025)}, 2025: 2}
    assert per_year(mcal.CPI_RELEASES) == {**{y: 12 for y in range(2015, 2025)}, 2025: 3}
    assert per_year(mcal.NFP_RELEASES) == {**{y: 12 for y in range(2015, 2025)}, 2025: 3}
    assert mcal.FOMC_CANCELLED == ("2020-03-18",)
    assert len(mcal.FOMC_PRESS_CONFERENCE) == 65
    assert len(mcal.NYSE_EARLY_CLOSES) == 20 and len(mcal.NYSE_HOLIDAYS) == 97
    overlap = sorted(set(mcal.FOMC_SCHEDULED) & (set(mcal.CPI_RELEASES) | set(mcal.NFP_RELEASES)))
    assert overlap == ["2016-03-16", "2017-03-15", "2017-06-14", "2017-12-13", "2019-12-11",
                       "2020-06-10", "2024-06-12"]


def test_placebo_test_has_correct_size_when_events_are_a_large_share():
    """Events = 12 of ~40 same-weekday days per year (NFP Fridays): rejections at 5% stay ~5%."""
    n_years, per_year = 6, 52
    n = n_years * per_year
    year = np.arange(n) // per_year
    weekday = np.zeros(n, int)
    ev = np.concatenate([y * per_year + np.arange(0, 48, 4) for y in range(n_years)])
    pool = np.setdiff1d(np.arange(n), ev)
    rng = np.random.default_rng(11)
    rej = 0
    reps = 400
    for i in range(reps):
        v = rng.normal(0, 1, n)
        rej += mc.placebo_test(v, ev, pool, weekday, year, 199, i, 1).p <= 0.05
    assert rej / reps < 0.08, rej / reps
