"""Case tables and simulations for core/eventstudy.py — ADR 0005 event-study statistics.

Judgement calls pinned here (see the module docstring for the reasoning):
- Declustering is first-come on ACCEPTED events: after an event at trading-day position p the
  same ticker cannot fire at p+1 .. p+decluster_days; the next crossing at distance
  > decluster_days fires and restarts the block. Crossings inside a block do not extend it.
- Triggers fire on the level (score >= theta), not only on an upward crossing; declustering
  limits a persistent shock.
- The calendar-time series (ADR 0005): an event at position p is active on p+1 .. p+h; each
  active day's value is the mean h-day outcome of the events active that day; events without
  an outcome are dropped and counted.
- placebo_p: the null moves each distinct event date (with its tickers) to a date where those
  tickers have an outcome, a draw's dates distinct but NOT spaced (declustering is per ticker
  and signal, so pooled event dates are not spaced either), and recomputes the directional NW t.
- max_t_placebo (ADR 0005 Amendment, discovery): z = sign * NW t (|t| when sign is None),
  M = max z over the sufficient configs; each draw is ONE uniformly random permutation of the
  window's trading days applied to every config's event dates (all tickers of a date move
  together), each config's outcome read inside its own embargoed window only (an event moved
  past it drops out of that draw); p = (1 + #{M* >= M}) / (n_draws + 1).
- Sufficiency: >= 30 events AND >= 30 distinct event dates.
"""

import math
import time

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.evaluate import newey_west_t
from eventtrade.core.eventstudy import (
    LEGS_ABNORMAL,
    LEGS_RAW,
    MIN_EVENT_DAYS,
    MIN_EVENTS,
    MaxTPlaceboResult,
    PlaceboResult,
    _distinct_draws,
    _placebo_permutations,
    calendar_time_series,
    cell_stat,
    max_t_placebo,
    net_mean,
    placebo_p,
    sufficient,
    triggers,
)

DAYS = pd.bdate_range("2016-01-04", periods=60)


def _score(rows: list[tuple[int, str, float]]) -> pd.DataFrame:
    """rows of (trading-day position, ticker, value)."""
    return pd.DataFrame(
        {"date": [DAYS[i] for i, _, _ in rows], "ticker": [t for _, t, _ in rows],
         "value": [v for _, _, v in rows]}
    )


def _events(rows: list[tuple[int, str]]) -> pd.DataFrame:
    return pd.DataFrame({"date": [DAYS[i] for i, _ in rows], "ticker": [t for _, t in rows]})


# --- triggers: declustering case table ----------------------------------------------------------
# (name, score rows, theta, direction, decluster_days, expected events)
TRIGGER_CASES = [
    ("single crossing", [(0, "A", 1.0), (1, "A", 2.5), (2, "A", 0.0)], 2.0, 1, 20, [(1, "A")]),
    ("threshold inclusive", [(3, "A", 2.0)], 2.0, 1, 20, [(3, "A")]),
    ("below threshold", [(3, "A", 1.999)], 2.0, 1, 20, []),
    ("negative direction", [(1, "A", 2.5), (2, "A", -2.5), (3, "A", -1.0)], 2.0, -1, 20,
     [(2, "A")]),
    ("negative inclusive", [(4, "A", -2.0)], 2.0, -1, 20, [(4, "A")]),
    ("NaN never fires", [(1, "A", float("nan")), (2, "A", 3.0)], 2.0, 1, 20, [(2, "A")]),
    # blocked for positions 1..20 after an event at 0; 21 fires
    ("blocked at distance 20", [(0, "A", 3.0), (20, "A", 3.0)], 2.0, 1, 20, [(0, "A")]),
    ("fires at distance 21", [(0, "A", 3.0), (21, "A", 3.0)], 2.0, 1, 20, [(0, "A"), (21, "A")]),
    # a run of crossings: blocked ones do not extend the block (first-come on accepted events)
    # judgement call "level, not crossing" (module docstring): a score held at or beyond theta
    # is a candidate every day, so it re-fires every 21 trading days; an upward-crossing rule
    # would give one event for each of these runs.
    ("run re-fires every 21", [(i, "A", 3.0) for i in range(45)], 2.0, 1, 20,
     [(0, "A"), (21, "A"), (42, "A")]),
    ("level after a dip still blocked", [(0, "A", 3.0), (1, "A", 0.0), (2, "A", 3.0),
                                         (21, "A", 3.0)], 2.0, 1, 20, [(0, "A"), (21, "A")]),
    ("no prior below-theta day needed", [(21, "A", 3.0)], 2.0, 1, 20, [(21, "A")]),
    ("blocked crossing does not extend", [(0, "A", 3.0), (15, "A", 3.0), (25, "A", 3.0)],
     2.0, 1, 20, [(0, "A"), (25, "A")]),
    # declustering is per ticker
    ("per ticker", [(0, "A", 3.0), (0, "B", 3.0), (5, "A", 3.0), (5, "B", 1.0), (6, "B", 3.0)],
     2.0, 1, 20, [(0, "A"), (0, "B")]),
    ("other ticker unaffected", [(0, "A", 3.0), (3, "B", 3.0)], 2.0, 1, 20,
     [(0, "A"), (3, "B")]),
    # trading-day distance, not calendar days: positions 4 and 5 straddle a weekend
    ("trading-day distance", [(4, "A", 3.0), (6, "A", 3.0)], 2.0, 1, 1, [(4, "A"), (6, "A")]),
    ("decluster 1 blocks next day", [(4, "A", 3.0), (5, "A", 3.0)], 2.0, 1, 1, [(4, "A")]),
    ("decluster 0 keeps all", [(4, "A", 3.0), (5, "A", 3.0)], 2.0, 1, 0, [(4, "A"), (5, "A")]),
    # input order does not matter
    ("unsorted input", [(21, "A", 3.0), (0, "A", 3.0), (10, "A", 3.0)], 2.0, 1, 20,
     [(0, "A"), (21, "A")]),
    ("empty", [], 2.0, 1, 20, []),
]


@pytest.mark.parametrize("name, rows, theta, direction, decl, expected", TRIGGER_CASES,
                         ids=[c[0] for c in TRIGGER_CASES])
def test_triggers(name, rows, theta, direction, decl, expected):
    got = triggers(_score(rows), theta, direction, decluster_days=decl, trading_days=DAYS)
    assert list(got.columns) == ["date", "ticker"]
    exp = _events(expected).sort_values(["date", "ticker"], ignore_index=True)
    pd.testing.assert_frame_equal(got.reset_index(drop=True), exp, check_dtype=False)


def test_triggers_rejects_bad_input():
    with pytest.raises(ValueError):
        triggers(_score([(0, "A", 3.0)]), 2.0, 0, trading_days=DAYS)
    with pytest.raises(ValueError):  # a score date that is not a trading day
        bad = pd.DataFrame({"date": [pd.Timestamp("2016-01-09")], "ticker": ["A"], "value": [3.0]})
        triggers(bad, 2.0, 1, trading_days=DAYS)
    with pytest.raises(ValueError):
        triggers(_score([(0, "A", 3.0)]), 2.0, 1, decluster_days=-1, trading_days=DAYS)
    with pytest.raises(ValueError):  # duplicate (date, ticker)
        triggers(_score([(0, "A", 3.0), (0, "A", 2.5)]), 2.0, 1, trading_days=DAYS)


# --- calendar_time_series: ADR 0005's calendar-time portfolio by hand ---------------------------
# An event dated at position p is active on positions p+1 .. p+h; each active day's value is the
# mean h-day outcome of the events active that day.

def test_calendar_time_series_by_hand():
    ev = _events([(0, "A"), (0, "B"), (2, "A"), (5, "C"), (7, "D")])
    out = pd.DataFrame({
        "date": [DAYS[0], DAYS[0], DAYS[0], DAYS[2], DAYS[5], DAYS[7]],
        "ticker": ["A", "B", "C", "A", "C", "D"],
        "value": [0.02, -0.04, 9.9, 0.03, 0.01, float("nan")],  # C on day 0 is not an event
    })
    s = calendar_time_series(ev, out, h=3, trading_days=DAYS)
    # day 1, 2: A0, B0 -> -0.01; day 3: A0, B0, A2 -> 0.01/3; day 4, 5: A2 -> 0.03;
    # day 6, 7, 8: C5 -> 0.01; D7 has no outcome
    expected = pd.Series([-0.01, -0.01, 0.01 / 3, 0.03, 0.03, 0.01, 0.01, 0.01],
                         index=pd.DatetimeIndex([DAYS[i] for i in range(1, 9)]))
    pd.testing.assert_series_equal(s, expected, check_names=False, check_freq=False)
    assert s.attrs == {"n_events": 4, "n_event_days": 3, "n_days": 8, "n_events_no_outcome": 1,
                       "horizon": 3}


def test_calendar_time_series_matches_the_reviewers_example():
    # 3 events at +1 on day 0 and 1 event at -1 on day 2, h = 5: days 1-2 hold the three +1
    # events, days 3-5 all four (mean 0.5), days 6-7 the -1 event: mean 0.214 over 7 days
    # (the earlier event-date form gave 0.0 over 2 observations).
    ev = _events([(0, "A"), (0, "B"), (0, "C"), (2, "A")])
    out = ev.assign(value=[1.0, 1.0, 1.0, -1.0])
    s = calendar_time_series(ev, out, h=5, trading_days=DAYS)
    assert len(s) == 7 and s.mean() == pytest.approx(1.5 / 7)
    assert s.tolist() == pytest.approx([1, 1, 0.5, 0.5, 0.5, -1, -1])


def test_calendar_time_series_truncates_at_the_calendar_end_and_h1_is_next_day():
    ev = _events([(58, "A")])
    s = calendar_time_series(ev, ev.assign(value=[2.0]), h=5, trading_days=DAYS)
    assert list(s.index) == [DAYS[59]] and s.attrs["n_days"] == 1
    s1 = calendar_time_series(_events([(3, "A")]),
                              _events([(3, "A")]).assign(value=[0.5]), h=1, trading_days=DAYS)
    assert list(s1.index) == [DAYS[4]] and s1.iloc[0] == 0.5


def test_calendar_time_series_empty_and_missing_outcome_rows():
    s = calendar_time_series(_events([]), pd.DataFrame(columns=["date", "ticker", "value"]), h=1,
                             trading_days=DAYS)
    assert s.empty and s.attrs["n_events"] == 0 and s.attrs["n_event_days"] == 0
    s = calendar_time_series(_events([(3, "A")]),
                             pd.DataFrame({"date": [DAYS[4]], "ticker": ["A"], "value": [1.0]}),
                             h=1, trading_days=DAYS)
    assert s.empty and s.attrs["n_events_no_outcome"] == 1


def test_calendar_time_series_rejects_bad_input():
    out = pd.DataFrame({"date": [DAYS[0], DAYS[0]], "ticker": ["A", "A"], "value": [1.0, 2.0]})
    with pytest.raises(ValueError):
        calendar_time_series(_events([(0, "A")]), out, h=1, trading_days=DAYS)
    with pytest.raises(ValueError):  # an event date that is not a trading day
        weekend = pd.DataFrame({"date": [pd.Timestamp("2016-01-09")], "ticker": ["A"]})
        calendar_time_series(weekend, weekend.assign(value=[1.0]), h=1, trading_days=DAYS)
    with pytest.raises(ValueError):
        calendar_time_series(_events([(0, "A")]), out.iloc[:1], h=0, trading_days=DAYS)


# --- cell_stat ---------------------------------------------------------------------------------

@pytest.mark.parametrize("sign", [1, -1, None])
def test_cell_stat(sign):
    rng = np.random.default_rng(3)
    s = pd.Series(rng.normal(-0.1, 1.0, 200), index=pd.bdate_range("2016-01-04", periods=200))
    mean, t, z, n = cell_stat(s, 5, sign)
    assert mean == pytest.approx(s.mean())
    assert t == pytest.approx(newey_west_t(s, horizon=5))
    assert z == pytest.approx(abs(t) if sign is None else sign * t)
    assert n == 200


def test_cell_stat_degenerate():
    mean, t, z, n = cell_stat(pd.Series([0.5], dtype=float), 5, 1)
    assert mean == 0.5 and math.isnan(t) and math.isnan(z) and n == 1
    with pytest.raises(ValueError):
        cell_stat(pd.Series([1.0, 2.0]), 5, 2)


# --- net_mean ----------------------------------------------------------------------------------
NET_CASES = [
    ("abnormal ETF", 0.0030, LEGS_ABNORMAL, 5.0, 0.0010),
    ("raw ETF", 0.0030, LEGS_RAW, 5.0, 0.0020),
    ("abnormal stock", 0.0030, LEGS_ABNORMAL, 10.0, -0.0010),
    ("no cost", 0.0030, 0, 5.0, 0.0030),
]


@pytest.mark.parametrize("name, mean, legs, bps, expected", NET_CASES, ids=[c[0] for c in NET_CASES])
def test_net_mean(name, mean, legs, bps, expected):
    assert net_mean(mean, legs, bps) == pytest.approx(expected)


def test_net_mean_rejects_negative():
    assert (LEGS_ABNORMAL, LEGS_RAW, MIN_EVENTS) == (4, 2, 30)
    with pytest.raises(ValueError):
        net_mean(0.01, -1, 5.0)
    with pytest.raises(ValueError):
        net_mean(0.01, 4, -5.0)


# --- max_t_placebo: search-corrected discovery p (ADR 0005 Amendment) ----------------------------

def _have(ev, out):
    """Events with a finite outcome on their own date (the units the placebo moves)."""
    key = out.dropna(subset=["value"]).set_index(["date", "ticker"]).index
    return ev[pd.MultiIndex.from_frame(ev[["date", "ticker"]]).isin(key)].reset_index(drop=True)


def _reference_max_t(events, outcomes, horizons, windows, sign, cal, n_draws, seed):
    """Loop reference: per draw ONE permutation of the window's trading days, applied to every
    configuration's event dates; each configuration's outcome is read inside its own window
    only; the calendar-time series and NW t are recomputed with calendar_time_series."""
    lo = min(pd.Timestamp(w[0]) for w in windows.values())
    hi = max(pd.Timestamp(w[1]) for w in windows.values())
    cand = cal[(cal >= lo) & (cal <= hi)]
    perms = _placebo_permutations(np.random.default_rng(seed), len(cand), n_draws)
    pos = {d: i for i, d in enumerate(cand)}

    def z_of(ev, g):
        w0, w1 = (pd.Timestamp(x) for x in windows[g])
        oc = outcomes[g]
        oc = oc[(oc["date"] >= w0) & (oc["date"] <= w1)]
        s = calendar_time_series(ev, oc, horizons[g], cal)
        t = newey_west_t(s, horizon=horizons[g])
        return float("nan") if math.isnan(t) else (abs(t) if sign is None else sign * t)

    obs = {g: z_of(events[g], g) for g in events}
    live = [g for g in events if not math.isnan(obs[g])]
    m_obs = max(obs[g] for g in live)
    null = []
    for b in range(n_draws):
        zs = []
        for g in live:
            ev = _have(events[g], outcomes[g])
            mapped = ev.assign(date=[cand[perms[b, pos[d]]] for d in ev["date"]])
            zs.append(z_of(mapped, g))
        zs = [z for z in zs if not math.isnan(z)]
        null.append(max(zs) if zs else -np.inf)
    return (1 + np.sum(np.array(null) >= m_obs)) / (n_draws + 1), obs


def _trigger_sets(rng, cal, n_days, tickers, n_dates):
    """Two nested trigger sets (low / high theta): clustered dates (several tickers per date);
    the high-theta set keeps a random half of the low-theta dates."""
    low_days = np.sort(rng.choice(n_days, size=n_dates, replace=False))
    rows = [(cal[d], t) for d in low_days for t in tickers if rng.random() < 0.7]
    low = pd.DataFrame(rows, columns=["date", "ticker"]).drop_duplicates()
    keep = set(cal[rng.choice(low_days, size=n_dates // 2, replace=False)])
    high = low[low["date"].isin(keep)].reset_index(drop=True)
    return {"low": low, "high": high}


def _cell(rng, cal, out_by_h, n_days, tickers, n_dates, hs=(1, 5, 20)):
    """A cell with 2 trigger sets x len(hs) horizons; each config's window is embargoed for h."""
    sets = _trigger_sets(rng, cal, n_days, tickers, n_dates)
    events, outcomes, horizons, windows = {}, {}, {}, {}
    for name, ev in sets.items():
        for h in hs:
            g = (name, h)
            w = (cal[0], cal[n_days - 1 - h])
            events[g] = ev[ev["date"] <= w[1]].reset_index(drop=True)
            outcomes[g], horizons[g], windows[g] = out_by_h[h], h, w
    return events, outcomes, horizons, windows


def _panel_by_h(rng, n_days, n_tickers, hs=(1, 5, 20), drift=0.0):
    """Outcomes of every h from one daily return panel (h-day sums from p+1)."""
    cal = pd.bdate_range("2016-01-04", periods=n_days + max(hs) + 2)
    r = rng.standard_t(4, size=(len(cal), n_tickers)) + drift
    cs = np.vstack([np.zeros((1, n_tickers)), np.cumsum(r, axis=0)])
    p = np.arange(n_days)
    out = {h: pd.concat([pd.DataFrame({"date": cal[p], "ticker": f"T{j}",
                                       "value": cs[p + 1 + h, j] - cs[p + 1, j]})
                         for j in range(n_tickers)], ignore_index=True) for h in hs}
    return cal, out


def test_max_t_placebo_equals_the_loop_reference_with_one_mapping_shared_by_all_configs():
    """Pins the null: every draw is ONE permutation of the window's dates applied to every
    configuration (so the h's of a trigger set, and the trigger sets, share it); events
    whose placebo date is past their configuration's own embargoed window drop out."""
    rng = np.random.default_rng(12)
    cal, out = _panel_by_h(rng, 300, 3)
    events, outcomes, horizons, windows = _cell(rng, cal, out, 300, ["T0", "T1", "T2"], 30)
    for sign in (1, None):
        res = max_t_placebo(events, outcomes, horizons, windows, sign, cal, n_draws=29, seed=3)
        p_ref, z_ref = _reference_max_t(events, outcomes, horizons, windows, sign, cal, 29, 3)
        assert res.p == pytest.approx(p_ref)
        assert res.z == pytest.approx(z_ref)
        assert res.max_z == pytest.approx(max(z_ref.values()))


def test_max_t_placebo_observed_t_is_the_cell_stat():
    rng = np.random.default_rng(13)
    cal, out = _panel_by_h(rng, 300, 3)
    events, outcomes, horizons, windows = _cell(rng, cal, out, 300, ["T0", "T1", "T2"], 30)
    res = max_t_placebo(events, outcomes, horizons, windows, -1, cal, n_draws=19, seed=1)
    for g, ev in events.items():
        s = calendar_time_series(ev, outcomes[g][outcomes[g]["date"] <= windows[g][1]],
                                 horizons[g], cal)
        _, t, z, n = cell_stat(s, horizons[g], -1)
        assert res.t[g] == pytest.approx(t) and res.z[g] == pytest.approx(z)
        assert res.n_obs[g] == n and res.n_events[g] == s.attrs["n_events"]
        assert res.n_event_days[g] == s.attrs["n_event_days"]
    assert res.best_config == max(res.z, key=lambda g: res.z[g])


def test_max_t_placebo_size_with_clustered_sparse_events_and_drift():
    """The reason for the Amendment: few clustered event dates (~30, as E2) and a common drift
    in the outcome. The placebo max-t rejects near nominal, while the observed max t itself is
    usually "significant" under the drift alone. Here 150 reps; 300 reps gave 0.3% / 4.3% /
    10.0% at 0.01 / 0.05 / 0.10."""
    rng = np.random.default_rng(20261005)
    n_sim, alpha = 150, 0.05
    rejects = 0
    big = 0
    for i in range(n_sim):
        cal, out = _panel_by_h(rng, 400, 4, drift=0.15)
        events, outcomes, horizons, windows = _cell(rng, cal, out, 400,
                                                    ["T0", "T1", "T2", "T3"], 32)
        res = max_t_placebo(events, outcomes, horizons, windows, 1, cal, n_draws=199, seed=i)
        rejects += res.p <= alpha
        big += res.max_z > 1.645
    assert 0.01 <= rejects / n_sim <= 0.10, rejects / n_sim
    assert big / n_sim > 0.5  # the drift alone makes the naive max t "significant"


def test_max_t_placebo_power_and_direction():
    rng = np.random.default_rng(14)
    cal, out = _panel_by_h(rng, 400, 3)
    events, outcomes, horizons, windows = _cell(rng, cal, out, 400, ["T0", "T1", "T2"], 40)
    ev = events[("low", 5)]
    key = set(zip(ev["date"], ev["ticker"]))
    o5 = outcomes[("low", 5)]
    hit = np.array([(d, t) in key for d, t in zip(o5["date"], o5["ticker"])])
    shifted = o5.assign(value=o5["value"] + np.where(hit, 4.0, 0.0))
    outs = {g: (shifted if g[1] == 5 else o) for g, o in outcomes.items()}
    up = max_t_placebo(events, outs, horizons, windows, 1, cal, n_draws=199, seed=1)
    down = max_t_placebo(events, outs, horizons, windows, -1, cal, n_draws=199, seed=1)
    assert up.p == pytest.approx(1 / 200) and up.best_config in {("low", 5), ("high", 5)}
    assert down.p > 0.2


def test_max_t_placebo_never_uses_outcomes_outside_a_configs_window():
    rng = np.random.default_rng(15)
    cal, out = _panel_by_h(rng, 300, 3)
    events, outcomes, horizons, windows = _cell(rng, cal, out, 300, ["T0", "T1", "T2"], 30)
    base = max_t_placebo(events, outcomes, horizons, windows, 1, cal, n_draws=99, seed=2)
    poisoned = {}
    for g, o in outcomes.items():
        outside = (o["date"] > pd.Timestamp(windows[g][1])).to_numpy()
        poisoned[g] = o.assign(value=np.where(outside, 1e6, o["value"]))
    again = max_t_placebo(events, poisoned, horizons, windows, 1, cal, n_draws=99, seed=2)
    assert again == base


def test_max_t_placebo_determinism_notes_and_degenerate():
    rng = np.random.default_rng(16)
    cal, out = _panel_by_h(rng, 300, 3)
    events, outcomes, horizons, windows = _cell(rng, cal, out, 300, ["T0", "T1", "T2"], 30)
    r1 = max_t_placebo(events, outcomes, horizons, windows, 1, cal, n_draws=49, seed=4)
    r2 = max_t_placebo(events, outcomes, horizons, windows, 1, cal, n_draws=49, seed=4)
    assert isinstance(r1, MaxTPlaceboResult) and r1 == r2
    notes = r1.notes()
    assert notes["test"] == "placebo_dates_max_t" and notes["n_draws"] == 49
    assert notes["seed"] == 4 and notes["n_configs"] == 6
    assert set(notes["z"]) == {str(g) for g in events}
    assert notes["n_dates"] == 300 - 1  # window cal[0] .. cal[n_days - 2] (the h = 1 embargo)
    # a configuration whose t is undefined is left out of the max; none defined: p NaN
    g1 = ("x", 1)
    res = max_t_placebo({g1: events[("low", 1)].iloc[:1]}, {g1: outcomes[("low", 1)]}, {g1: 1},
                        {g1: windows[("low", 1)]}, 1, cal, n_draws=9, seed=0)
    assert math.isnan(res.p) and res.best_config is None


def test_max_t_placebo_validation():
    rng = np.random.default_rng(17)
    cal, out = _panel_by_h(rng, 200, 2)
    events, outcomes, horizons, windows = _cell(rng, cal, out, 200, ["T0", "T1"], 20)
    with pytest.raises(ValueError):
        max_t_placebo({}, {}, {}, {}, 1, cal, n_draws=9, seed=0)
    with pytest.raises(ValueError):  # a config without a horizon
        max_t_placebo(events, outcomes, {}, windows, 1, cal, n_draws=9, seed=0)
    with pytest.raises(ValueError):
        max_t_placebo(events, outcomes, horizons, windows, 2, cal, n_draws=9, seed=0)
    with pytest.raises(ValueError):
        max_t_placebo(events, outcomes, horizons, windows, 1, cal, n_draws=0, seed=0)
    g = next(iter(events))
    late = {**windows, g: (windows[g][0], cal[5])}  # observed events outside their window
    with pytest.raises(ValueError):
        max_t_placebo(events, outcomes, horizons, late, 1, cal, n_draws=9, seed=0)


def test_placebo_permutations_are_permutations_and_uniform():
    perms = _placebo_permutations(np.random.default_rng(0), 50, 4000)
    assert perms.shape == (4000, 50)
    assert (np.sort(perms, axis=1) == np.arange(50)).all()  # distinct dates in every draw
    counts = np.bincount(perms[:, 0], minlength=50)  # where date 0 goes: uniform
    assert counts.min() > 40 and counts.max() < 125


def test_sufficiency_needs_30_events_and_30_event_dates():
    assert (MIN_EVENTS, MIN_EVENT_DAYS) == (30, 30)
    assert sufficient(30, 30) and sufficient(400, 30)
    assert not sufficient(29, 29) and not sufficient(400, 29) and not sufficient(29, 400)


# --- placebo_p: confirmation test of one configuration ------------------------------------------

def _null_panel(rng, n_days=500, n_tickers=4, h=20, drift=0.0):
    """Overlapping h-day outcomes (sum of iid t(4) daily returns from p+1) for every date."""
    cal = pd.bdate_range("2016-01-04", periods=n_days + h + 2)
    r = rng.standard_t(4, size=(len(cal), n_tickers)) + drift
    cs = np.vstack([np.zeros((1, n_tickers)), np.cumsum(r, axis=0)])
    p = np.arange(n_days)
    out = pd.concat([pd.DataFrame({"date": cal[p], "ticker": f"T{j}",
                                   "value": cs[p + 1 + h, j] - cs[p + 1, j]})
                     for j in range(n_tickers)], ignore_index=True)
    return cal, out


def _spaced_days(rng, n, k, gap=20):
    """k sorted positions in range(n), consecutive ones more than `gap` apart (uniform over such
    sets): what one declustered (ticker, signal) series looks like."""
    free = n - (k - 1) * gap
    assert free >= k
    return np.sort(rng.choice(free, size=k, replace=False)) + np.arange(k) * gap


def _random_events(rng, cal, n_days, n_tickers, n_events, gap=20):
    per = np.full(n_tickers, n_events // n_tickers)
    per[: n_events % n_tickers] += 1
    return pd.concat([pd.DataFrame({"date": cal[_spaced_days(rng, n_days, int(k), gap)],
                                    "ticker": f"T{j}"}) for j, k in enumerate(per)],
                     ignore_index=True)


def test_placebo_p_size_at_h20_with_clustered_events_where_the_normal_p_is_oversized():
    """Null true: 4 tickers fire together on 8 random days (32 events, as commodity ETFs sharing
    producer countries do), h = 20. The placebo p rejects near 5% (it is a randomisation test:
    observed dates are one draw from its null). The one-sided normal p of the same HAC t is
    oversized, the reason it is not the confirmation test."""
    rng = np.random.default_rng(20261005)
    n_sim, h, n_days = 200, 20, 500
    placebo = normal = 0
    for i in range(n_sim):
        cal, out = _null_panel(rng, n_days=n_days, h=h)
        days = _spaced_days(rng, n_days, 8)
        ev = pd.DataFrame([(cal[d], f"T{j}") for d in days for j in range(4)],
                          columns=["date", "ticker"])
        res = placebo_p(ev, out, h, 1, cal, n_draws=199, seed=i)
        placebo += res.p <= 0.05
        normal += 0.5 * math.erfc(res.z / math.sqrt(2)) <= 0.05
    assert 0.01 <= placebo / n_sim <= 0.09, placebo / n_sim
    # here 8% vs 3%; the 300-rep simulation gave 12.7% vs 7.7% (Monte Carlo se ~1.5%)
    assert normal / n_sim >= 0.07 and normal > placebo, (normal / n_sim, placebo / n_sim)


def test_placebo_p_absorbs_a_common_drift():
    """Every date has the same positive drift: the HAC t of the event mean is large, but event
    dates are no different from random dates, so the placebo p is not small (8 panels: the
    typical one, not a single fixture)."""
    z, p, null_mean = [], [], []
    for s in range(3, 11):
        rng = np.random.default_rng(s)
        cal, out = _null_panel(rng, h=5, drift=0.3)
        ev = _random_events(rng, cal, 500, 4, 60)
        res = placebo_p(ev, out, 5, 1, cal, n_draws=199, seed=1)
        z.append(res.z), p.append(res.p), null_mean.append(res.null_z_mean)
    assert np.median(z) > 3 and np.median(p) > 0.2 and min(null_mean) > 2, (z, p, null_mean)


def test_placebo_p_power_and_wrong_sign():
    rng = np.random.default_rng(4)
    cal, out = _null_panel(rng, h=5)
    ev = _random_events(rng, cal, 500, 4, 60)
    key = set(zip(ev["date"], ev["ticker"]))
    hit = np.array([(d, t) in key for d, t in zip(out["date"], out["ticker"])])
    shifted = out.assign(value=out["value"] + np.where(hit, 3.0, 0.0))
    up = placebo_p(ev, shifted, 5, 1, cal, n_draws=199, seed=1)
    down = placebo_p(ev, shifted, 5, -1, cal, n_draws=199, seed=1)
    both = placebo_p(ev, shifted, 5, None, cal, n_draws=199, seed=1)
    assert up.p == pytest.approx(1 / 200) and down.p > 0.5 and both.p == pytest.approx(1 / 200)


def test_placebo_p_observed_t_is_the_cell_stat_and_is_deterministic():
    rng = np.random.default_rng(5)
    cal, out = _null_panel(rng, h=10)
    ev = _random_events(rng, cal, 500, 4, 40)
    r1 = placebo_p(ev, out, 10, -1, cal, n_draws=99, seed=7)
    r2 = placebo_p(ev, out, 10, -1, cal, n_draws=99, seed=7)
    assert isinstance(r1, PlaceboResult) and r1 == r2
    _, t, z, _ = cell_stat(calendar_time_series(ev, out, 10, cal), 10, -1)
    assert (r1.t, r1.z) == (pytest.approx(t), pytest.approx(z))
    assert r1.n_events == 40 and r1.notes()["test"] == "placebo_dates"


def test_placebo_p_degenerate_and_validation():
    cal = pd.bdate_range("2016-01-04", periods=30)
    empty = pd.DataFrame(columns=["date", "ticker"])
    out = pd.DataFrame({"date": cal[:5], "ticker": "A", "value": np.arange(5.0)})
    assert math.isnan(placebo_p(empty, out, 1, 1, cal, n_draws=9, seed=0).p)
    with pytest.raises(ValueError):
        placebo_p(empty, out, 1, 1, cal, n_draws=0, seed=0)
    with pytest.raises(ValueError):
        placebo_p(empty, out, 1, 2, cal, n_draws=9, seed=0)


def test_placebo_p_size_at_h20_with_two_signals_whose_pooled_dates_are_not_spaced():
    """Regression (review): 2 signals x 3 ETFs, each signal declustered (dates 21+ apart) and
    firing for all 3 tickers, so the cell's POOLED dates are not spaced. A null that spaced the
    placebo dates 21 apart rejected 8.5% at 5% (1,000 reps, 1,320 days, 58 dates; 3.8% without
    spacing). Without spacing the observed dates are one draw of a null close to the real one."""
    rng = np.random.default_rng(20261006)
    n_sim, h, n_days, n_t = 200, 20, 600, 3
    rej = 0
    for i in range(n_sim):
        cal, out = _null_panel(rng, n_days=n_days, n_tickers=n_t, h=h, drift=0.05)
        days = np.concatenate([_spaced_days(rng, n_days, 13) for _ in range(2)])
        ev = pd.DataFrame([(cal[d], f"T{j}") for d in days for j in range(n_t)],
                          columns=["date", "ticker"]).drop_duplicates()
        rej += placebo_p(ev, out, h, 1, cal, n_draws=99, seed=i).p <= 0.05
    assert 0.01 <= rej / n_sim <= 0.08, rej / n_sim


DISTINCT_CASES = [
    # (n, k); the review's hang band: n = 1,320 (the 2020-01..2025-03 window) and k ~ 57-660
    (100, 3), (10, 10), (40, 1), (1320, 58), (1320, 150), (1320, 300), (1320, 1320),
]


@pytest.mark.parametrize("n, k", DISTINCT_CASES)
def test_distinct_draws_terminate_and_are_distinct(n, k):
    """Regression (review): rejection sampling of k distinct integers never returned for k
    around sqrt(n) and above (k = 58, 150, 300 of 1,320 hung a confirmation)."""
    t0 = time.perf_counter()
    d = _distinct_draws(np.random.default_rng(0), n, k, 999)
    assert time.perf_counter() - t0 < 20
    assert d.shape == (999, k) and d.min() >= 0 and d.max() < n
    if k > 1:
        assert np.diff(d, axis=1).min() >= 1  # sorted and distinct
    with pytest.raises(ValueError):
        _distinct_draws(np.random.default_rng(0), 3, 4, 1)


def test_distinct_draws_are_uniform_and_unspaced():
    d = _distinct_draws(np.random.default_rng(1), 100, 1, 20000)
    counts = np.bincount(d.ravel(), minlength=100)
    assert counts.min() > 120 and counts.max() < 290  # ~200 each
    # pairs: a uniform 2-subset of 100 has a gap <= 20 with prob 1 - 80*79/(100*99) = 0.362
    pairs = _distinct_draws(np.random.default_rng(2), 100, 2, 20000)
    close = (np.diff(pairs, axis=1)[:, 0] <= 20).mean()
    assert 0.35 <= close <= 0.375, close
