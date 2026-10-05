"""Case tables for core/simulate.py.

Every hand-computed case is also checked against `_reference`, an independent plain-pandas loop
that implements the documented semantics without vectorbt. Toy data: 1-3 tickers, 6-10 days.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.simulate import simulate, stats_from_returns
from eventtrade.schemas import PRICE_COLUMNS, WEIGHT_COLUMNS, BacktestStats

DAYS = pd.bdate_range("2020-01-06", periods=10)  # Mon 6 Jan .. Fri 17 Jan 2020


# --- builders -------------------------------------------------------------------------------

def _prices(opens: dict[str, list[float]], closes: dict[str, list[float]] | None = None) -> pd.DataFrame:
    """Long PRICE_COLUMNS frame; NaN open -> the row is absent (no bar that day)."""
    rows = []
    for t, os_ in opens.items():
        cs = (closes or {}).get(t, os_)
        for d, o, c in zip(DAYS, os_, cs):
            if o is None or (isinstance(o, float) and math.isnan(o)):
                continue
            rows.append({"date": d, "ticker": t, "open": o, "high": max(o, c), "low": min(o, c),
                         "close": c, "volume": 1e6, "adj_open": o, "adj_close": c})
    return pd.DataFrame(rows, columns=PRICE_COLUMNS)


def _weights(rows: list[tuple[int, str, float]]) -> pd.DataFrame:
    """rows: (day index into DAYS, ticker, weight)."""
    return pd.DataFrame([{"date": DAYS[i], "ticker": t, "weight": w} for i, t, w in rows],
                        columns=WEIGHT_COLUMNS)


def _ret_by_day(out: pd.DataFrame) -> dict[int, float]:
    idx = {d: i for i, d in enumerate(DAYS)}
    return {idx[pd.Timestamp(d)]: r for d, r in zip(out["date"], out["ret"])}


# --- independent reference implementation ---------------------------------------------------

def _reference(weights: pd.DataFrame, prices: pd.DataFrame, cost_bps, borrow_bps_annual: float,
               init_cash: float = 1e6) -> pd.Series:
    """Plain loop: execute day-t targets at the next price date's adj_open, mark at adj_open."""
    opens = prices.pivot(index="date", columns="ticker", values="adj_open").sort_index()
    tickers = sorted(weights["ticker"].unique())
    opens = opens.reindex(columns=tickers)
    cal = list(opens.index)
    fee = {t: (cost_bps[t] if isinstance(cost_bps, dict) else cost_bps) / 1e4 for t in tickers}

    targets: dict[pd.Timestamp, dict[str, float]] = {}
    for d, g in weights.groupby("date"):
        later = [c for c in cal if c > pd.Timestamp(d)]
        if later:
            full = {t: 0.0 for t in tickers}
            full.update(dict(zip(g["ticker"], g["weight"])))
            targets[later[0]] = full  # groupby is date-sorted, so the latest decision wins

    start = min(targets)
    shares = {t: 0.0 for t in tickers}
    last_px: dict[str, float] = {}
    cash, value = init_cash, init_cash
    out = {}
    for d in [c for c in cal if c >= start]:
        prev_value = value
        short = sum(max(-shares[t], 0.0) * last_px.get(t, 0.0) for t in tickers)
        borrow = borrow_bps_annual / 1e4 / 252 * short / prev_value
        for t in tickers:
            if not np.isnan(opens.at[d, t]):
                last_px[t] = opens.at[d, t]
        if d in targets:
            v_pre = cash + sum(shares[t] * last_px.get(t, 0.0) for t in tickers)
            for t in tickers:
                if np.isnan(opens.at[d, t]):
                    continue  # skipped: hold
                p = opens.at[d, t]
                trade = targets[d][t] * v_pre / p - shares[t]
                cash -= trade * p + abs(trade) * p * fee[t]
                shares[t] += trade
        value = cash + sum(shares[t] * last_px.get(t, 0.0) for t in tickers)
        out[d] = value / prev_value - 1.0 - borrow
    return pd.Series(out)


def _check_against_reference(weights, prices, cost_bps, borrow, out):
    ref = _reference(weights, prices, cost_bps, borrow)
    got = pd.Series(out["ret"].to_numpy(), index=pd.to_datetime(out["date"]))
    assert list(got.index) == list(ref.index)
    np.testing.assert_allclose(got.to_numpy(), ref.to_numpy(), atol=1e-12, rtol=0)


# --- Rule 1: look-ahead canary. A day-t decision executes at open(t+1); it cannot earn the
# close(t) -> open(t+1) gap, and it cannot earn open(t) -> open(t+1) either. ----------------

def test_canary_single_gap_earns_nothing():
    # X gaps +10% overnight from close(0)=100 to open(1)=110, then is flat. Weight 1 decided at
    # day 0 "knows" the gap. Correct execution at open(1)=110 earns exactly 0 on every day.
    prices = _prices({"X": [100, 110, 110, 110, 110, 110]})
    w = _weights([(0, "X", 1.0)])
    out, _ = simulate(w, prices, cost_bps=0.0, borrow_bps_annual=0.0)
    assert out["ret"].abs().max() < 1e-12
    assert pd.Timestamp(out["date"].iloc[0]) == DAYS[1]  # first row = execution day
    _check_against_reference(w, prices, 0.0, 0.0, out)


def test_canary_alternating_gaps_pays_for_perfect_overnight_foresight():
    # All movement is overnight: close(t) = open(t), open(t+1) = close(t) * (1 + g_t), with
    # g alternating +5%, -5%. Weight w_t = sign(g_t) predicts the gap it cannot trade; it is held
    # over g_{t+1} = -g_t, so every held day returns exactly -5% (a cheat would show +5%).
    g = [0.05, -0.05] * 4 + [0.05]
    opens = [100.0]
    for x in g:
        opens.append(opens[-1] * (1 + x))
    prices = _prices({"X": opens})
    w = _weights([(t, "X", float(np.sign(g[t]))) for t in range(8)])
    out, _ = simulate(w, prices, cost_bps=0.0, borrow_bps_annual=0.0)
    r = _ret_by_day(out)
    assert r[1] == pytest.approx(0.0, abs=1e-12)  # execution day: no position held before
    for d in range(2, 10):
        assert r[d] == pytest.approx(-0.05, abs=1e-12)
    _check_against_reference(w, prices, 0.0, 0.0, out)


# --- Rule 2: returns are open-to-open and line up with LABEL ret_oo:
# ret[t+2] = sum_i w_i(t) * ret_oo_i(t, h=1) when costs are zero. -------------------------------

def test_returns_match_label_ret_oo():
    rng = np.random.default_rng(7)
    tickers = ["A", "B", "C"]
    opens = {t: list(100 * np.cumprod(1 + rng.normal(0, 0.02, 10))) for t in tickers}
    prices = _prices(opens)
    rows = []
    for t in range(8):
        ws = rng.uniform(-0.5, 0.5, 3)
        rows += [(t, k, float(x)) for k, x in zip(tickers, ws)]
    w = _weights(rows)
    out, _ = simulate(w, prices, cost_bps=0.0, borrow_bps_annual=0.0)
    r = _ret_by_day(out)
    for t in range(8):
        ret_oo = {k: opens[k][t + 2] / opens[k][t + 1] - 1 for k in tickers}
        expected = sum(wt * ret_oo[k] for (dt, k, wt) in rows if dt == t)
        assert r[t + 2] == pytest.approx(expected, abs=1e-12)
    _check_against_reference(w, prices, 0.0, 0.0, out)


# --- Rule 3: fees = cost_bps / 1e4 of traded notional, charged in the execution day's return. --

@pytest.mark.parametrize(
    "cost_bps, enter_ret, exit_ret",
    [
        # 0.5 of 1e6 bought at 10 bps -> -0.5 * 0.001. Selling the same 500k of stock back costs
        # 500 again, but now out of a book worth 1e6 - 500.
        (10.0, -0.0005, -0.0005 / 0.9995),
        (0.0, 0.0, 0.0),
        # per-ticker dict
        ({"X": 20.0}, -0.001, -0.001 / 0.999),
    ],
)
def test_fees_hand_computed(cost_bps, enter_ret, exit_ret):
    prices = _prices({"X": [50.0] * 6})
    w = _weights([(0, "X", 0.5), (2, "X", 0.0)])
    out, stats = simulate(w, prices, cost_bps=cost_bps, borrow_bps_annual=0.0)
    r = _ret_by_day(out)
    assert r[1] == pytest.approx(enter_ret, abs=1e-12)
    assert r[2] == pytest.approx(0.0, abs=1e-12)
    assert r[3] == pytest.approx(exit_ret, rel=1e-9, abs=1e-12)
    assert stats.turnover == pytest.approx((0.5 + 0.5) / len(out), rel=1e-3)
    assert stats.cost_drag == pytest.approx(-(enter_ret + exit_ret) / len(out) * 252, rel=1e-6, abs=1e-12)
    _check_against_reference(w, prices, cost_bps, 0.0, out)


def test_fees_dict_per_ticker_on_moving_prices():
    prices = _prices({"A": [10, 11, 10.5, 12, 12.5, 12, 11, 11.5],
                      "B": [20, 19, 19.5, 21, 20, 22, 21, 20]})
    w = _weights([(0, "A", 0.4), (0, "B", -0.3), (2, "A", -0.2), (2, "B", 0.6), (4, "A", 0.1)])
    out, _ = simulate(w, prices, cost_bps={"A": 5.0, "B": 25.0}, borrow_bps_annual=0.0)
    _check_against_reference(w, prices, {"A": 5.0, "B": 25.0}, 0.0, out)


def test_missing_cost_for_ticker_raises():
    prices = _prices({"A": [10.0] * 4, "B": [10.0] * 4})
    w = _weights([(0, "A", 0.5), (0, "B", 0.5)])
    with pytest.raises(ValueError, match="cost_bps"):
        simulate(w, prices, cost_bps={"A": 5.0}, borrow_bps_annual=0.0)


# --- Rule 4: borrow = borrow_bps_annual / 1e4 / 252 on short notional, deducted daily. -------

def test_long_short_borrow_hand_computed():
    # +0.5 L, -0.5 S on flat prices; 252 bps/yr -> 1 bp/day on half the book = -0.00005 per day
    # held. Execution day carries no borrow (nothing was short over the previous interval).
    prices = _prices({"L": [10.0] * 7, "S": [40.0] * 7})
    w = _weights([(0, "L", 0.5), (0, "S", -0.5)])
    out, stats = simulate(w, prices, cost_bps=0.0, borrow_bps_annual=252.0)
    r = _ret_by_day(out)
    assert r[1] == pytest.approx(0.0, abs=1e-15)
    for d in range(2, 7):
        assert r[d] == pytest.approx(-0.00005, rel=1e-9)
    assert stats.cost_drag == pytest.approx(0.00005 * 5 / 6 * 252, rel=1e-9)
    _check_against_reference(w, prices, 0.0, 252.0, out)


def test_borrow_on_moving_prices_matches_reference():
    prices = _prices({"L": [10, 10.5, 11, 10.8, 11.2, 11.5, 11, 12],
                      "S": [40, 39, 41, 42, 40, 38, 39, 37]})
    w = _weights([(0, "L", 0.5), (0, "S", -0.5), (3, "L", 0.3), (3, "S", -0.8)])
    out, _ = simulate(w, prices, cost_bps=10.0, borrow_bps_annual=300.0)
    _check_against_reference(w, prices, 10.0, 300.0, out)


# --- Rule 5: target rebalancing sells before it buys (call_seq='auto'), so moving a fully
# invested book from one name to another is not starved of cash. ------------------------------

def test_full_switch_sells_first():
    # ZZZ held at 100%; switch to AAA at 100% (decided day 2, executed day 3). AAA sorts first,
    # so without sell-first ordering the AAA buy would find no cash. AAA then rises 10% at day 4.
    prices = _prices({"AAA": [10, 10, 10, 10, 11, 11], "ZZZ": [20.0] * 6})
    w = _weights([(0, "ZZZ", 1.0), (2, "AAA", 1.0), (2, "ZZZ", 0.0)])
    out, _ = simulate(w, prices, cost_bps=0.0, borrow_bps_annual=0.0)
    r = _ret_by_day(out)
    assert r[4] == pytest.approx(0.10, abs=1e-12)
    _check_against_reference(w, prices, 0.0, 0.0, out)


# --- Rule 6: missing adj_open on the execution day -> that order is skipped and the position is
# held (valued at the last known open); other tickers still trade. --------------------------

def test_missing_price_on_execution_day_holds_position():
    nan = float("nan")
    # B has no bar on day 3 (the execution day of the day-2 decision to go flat B).
    prices = _prices({"A": [10, 10, 10, 10, 10, 10, 10], "B": [20, 20, 20, nan, 22, 22, 22]})
    w = _weights([(0, "A", 0.5), (0, "B", 0.5), (2, "A", 0.0), (2, "B", 0.0)])
    out, _ = simulate(w, prices, cost_bps=0.0, borrow_bps_annual=0.0)
    r = _ret_by_day(out)
    # Day 3: A sold; B order skipped, B marked at last known 20 -> 0. Day 4: B (still held, half
    # the book) moves 20 -> 22 -> +5%. No more weight rows, so B is still held afterwards.
    assert r[3] == pytest.approx(0.0, abs=1e-12)
    assert r[4] == pytest.approx(0.05, abs=1e-12)
    _check_against_reference(w, prices, 0.0, 0.0, out)


# --- Rule 7: weights semantics. A date in `weights` is a full target (absent tickers -> 0);
# a decision on a non-trading day executes on the next price date; latest decision wins. -----

def test_absent_ticker_on_weight_date_means_flat():
    prices = _prices({"A": [10, 10, 11, 12, 12, 12], "B": [10, 10, 10, 10, 10, 10]})
    w = _weights([(0, "A", 0.5), (0, "B", 0.5), (1, "B", 0.5)])  # A dropped on day 1 -> 0
    out, _ = simulate(w, prices, cost_bps=0.0, borrow_bps_annual=0.0)
    r = _ret_by_day(out)
    assert r[2] == pytest.approx(0.05, abs=1e-12)  # held A over day1->day2 open
    assert r[3] == pytest.approx(0.0, abs=1e-12)   # A sold at open(2)
    _check_against_reference(w, prices, 0.0, 0.0, out)


def test_weekend_decision_executes_next_trading_day_and_latest_wins():
    prices = _prices({"X": [10, 10, 10, 10, 10, 11, 11, 11, 11, 11]})  # DAYS[5] = Mon 13 Jan
    w = pd.DataFrame({"date": [pd.Timestamp("2020-01-10"), pd.Timestamp("2020-01-11"),
                               pd.Timestamp("2020-01-12")],
                      "ticker": ["X", "X", "X"], "weight": [0.2, 0.4, 1.0]})
    out, _ = simulate(w, prices, cost_bps=0.0, borrow_bps_annual=0.0)
    r = _ret_by_day(out)
    # Fri/Sat/Sun decisions all execute at Mon open (=11, after the move); Sun's 1.0 wins.
    assert min(r) == 5
    assert all(abs(v) < 1e-12 for v in r.values())
    _check_against_reference(w, prices, 0.0, 0.0, out)


def test_decision_on_last_price_date_is_dropped():
    prices = _prices({"X": [10.0] * 4})
    with pytest.raises(ValueError, match="no weight date"):
        simulate(_weights([(3, "X", 1.0)]), prices, cost_bps=0.0, borrow_bps_annual=0.0)


# --- Rule 8: input contract. -----------------------------------------------------------------

@pytest.mark.parametrize(
    "weights, match",
    [
        (_weights([(0, "Q", 1.0)]), "no prices"),
        (_weights([(0, "X", float("nan"))]), "NaN"),
        (_weights([(0, "X", 0.5), (0, "X", 0.5)]), "duplicate"),
        (pd.DataFrame(columns=WEIGHT_COLUMNS), "empty"),
    ],
)
def test_bad_inputs_raise(weights, match):
    with pytest.raises(ValueError, match=match):
        simulate(weights, _prices({"X": [10.0] * 4}), cost_bps=0.0, borrow_bps_annual=0.0)


def test_output_shape_and_date_type_follow_prices():
    prices = _prices({"X": [10.0, 10, 11, 12]})
    prices_d = prices.assign(date=prices["date"].dt.date)
    w = _weights([(0, "X", 1.0)]).assign(date=lambda f: f["date"].dt.date)
    out, stats = simulate(w, prices_d, cost_bps=0.0, borrow_bps_annual=0.0)
    assert list(out.columns) == ["date", "ret"]
    assert isinstance(out["date"].iloc[0], type(DAYS[0].date()))
    assert isinstance(stats, BacktestStats)
    assert stats.n_days == 3


# --- Rule 9: stats_from_returns (252-day year, zero risk-free, drawdown <= 0). --------------

@pytest.mark.parametrize(
    "rets, exp",
    [
        # constant +1%/day: no vol -> sharpe 0 by convention, no drawdown
        ([0.01] * 4, {"n_days": 4, "ann_return": 1.01 ** 252 - 1, "ann_vol": 0.0, "sharpe": 0.0,
                      "max_drawdown": 0.0}),
        # +10% then -50%: drawdown from peak 1.1 to 0.55 -> -50%
        ([0.10, -0.50], {"n_days": 2, "max_drawdown": -0.5}),
        # first day loss counts from the starting equity of 1
        ([-0.2, 0.1], {"max_drawdown": -0.2}),
        # sharpe = mean / std(ddof=1) * sqrt(252)
        ([0.01, -0.01, 0.02, 0.0], {"sharpe": 0.005 / np.std([0.01, -0.01, 0.02, 0.0], ddof=1) * np.sqrt(252),
                                    "ann_vol": np.std([0.01, -0.01, 0.02, 0.0], ddof=1) * np.sqrt(252)}),
        # NaNs are dropped
        ([float("nan"), 0.01, 0.01], {"n_days": 2}),
    ],
)
def test_stats_from_returns(rets, exp):
    s = stats_from_returns(pd.Series(rets), turnover=0.3, cost_drag=0.01)
    for k, v in exp.items():
        assert getattr(s, k) == pytest.approx(v, rel=1e-9, abs=1e-12), k
    assert s.turnover == 0.3 and s.cost_drag == 0.01


def test_stats_accepts_ret_frame_and_rejects_empty():
    f = pd.DataFrame({"date": DAYS[:3], "ret": [0.01, 0.0, -0.01]})
    assert stats_from_returns(f, 0.0, 0.0).n_days == 3
    with pytest.raises(ValueError):
        stats_from_returns(pd.Series([], dtype=float), 0.0, 0.0)
