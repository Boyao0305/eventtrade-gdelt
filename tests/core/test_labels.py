import math

import numpy as np
import pandas as pd
import pytest
from feature_cases import (
    LABEL_ABSENT,
    LABEL_CASES,
    LABEL_DAYS,
    LABEL_PRICES,
    VOL_CASES,
)

from eventtrade.core.labels import (
    VOL_CONTROL_NAMES,
    VOL_LABEL_COLUMNS,
    make_labels,
    realized_vol_label,
    vol_controls,
    vol_surprise_label,
)
from eventtrade.schemas import LABEL_COLUMNS, PRICE_COLUMNS


def _prices(table=LABEL_PRICES, days=LABEL_DAYS) -> pd.DataFrame:
    rows = []
    for (ticker, i), (o, c) in table.items():
        rows.append({"date": days[i], "ticker": ticker, "open": o, "high": max(o, c),
                     "low": min(o, c), "close": c, "volume": 1.0, "adj_open": o, "adj_close": c})
    return pd.DataFrame(rows, columns=PRICE_COLUMNS)


def _close(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)


@pytest.fixture(scope="module")
def labels() -> pd.DataFrame:
    return make_labels(_prices(), [1, 2])


def test_columns_and_dtypes(labels):
    assert list(labels.columns) == LABEL_COLUMNS
    assert str(labels["date"].dtype) == "datetime64[ns]"
    assert not labels.duplicated(["date", "ticker", "horizon"]).any()


@pytest.mark.parametrize("key", list(LABEL_CASES))
def test_label_cases(labels, key):
    ticker, i, h = key
    row = labels[(labels["ticker"] == ticker) & (labels["date"] == pd.Timestamp(LABEL_DAYS[i]))
                 & (labels["horizon"] == h)]
    assert len(row) == 1
    got = tuple(float(row[c].iloc[0]) for c in ("ret_oo", "ret_overnight", "ret_intraday"))
    for g, e in zip(got, LABEL_CASES[key]):
        assert _close(g, e), (key, got, LABEL_CASES[key])


@pytest.mark.parametrize("ticker,i", LABEL_ABSENT)
def test_no_row_without_a_bar(labels, ticker, i):
    assert labels[(labels["ticker"] == ticker)
                  & (labels["date"] == pd.Timestamp(LABEL_DAYS[i]))].empty


def test_single_ticker_needs_calendar_to_see_its_gap():
    """With only BBB, its own dates hide the gap unless the exchange calendar is passed."""
    bbb = _prices({k: v for k, v in LABEL_PRICES.items() if k[0] == "BBB"})
    with_cal = make_labels(bbb, [1], trading_days=LABEL_DAYS)
    day1 = with_cal[with_cal["date"] == pd.Timestamp(LABEL_DAYS[1])]
    assert math.isnan(day1["ret_oo"].iloc[0]) and math.isnan(day1["ret_overnight"].iloc[0])


def test_truncating_prices_keeps_later_bars_out():
    """Labels from prices cut at a date never use bars after it (hold-out discipline)."""
    full = make_labels(_prices(), [1, 2])
    cut = pd.Timestamp(LABEL_DAYS[3])
    short = make_labels(_prices().query("date < @LABEL_DAYS[3]"), [1, 2])
    merged = short.merge(full, on=["date", "ticker", "horizon"], suffixes=("_s", "_f"))
    # where short has a value it equals full; values needing bars >= cut are NaN
    for c in ("ret_oo", "ret_overnight", "ret_intraday"):
        s, f = merged[f"{c}_s"], merged[f"{c}_f"]
        assert np.allclose(s[s.notna()], f[s.notna()])
    last = short[short["date"] == pd.Timestamp(LABEL_DAYS[2])]
    assert last["ret_overnight"].isna().all() and last["ret_oo"].isna().all()
    assert (short["date"] < cut).all()


def test_labels_ignore_past_perturbation():
    """Label at t depends only on adj_close[t] and bars t+1..t+1+h; earlier bars are irrelevant."""
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2020-01-01", periods=60)
    df = pd.DataFrame({"date": np.repeat(days, 2), "ticker": ["X", "Y"] * 60})
    df["adj_open"] = rng.uniform(50, 150, len(df))
    df["adj_close"] = rng.uniform(50, 150, len(df))
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = 1.0
    base = make_labels(df[PRICE_COLUMNS], [1, 5])
    t = days[30]
    pert = df.copy()
    pert.loc[pert["date"] < t, ["adj_open", "adj_close"]] *= 3.0
    after = make_labels(pert[PRICE_COLUMNS], [1, 5])
    keep = base["date"] >= t
    pd.testing.assert_frame_equal(base[keep].reset_index(drop=True),
                                  after[keep].reset_index(drop=True))


def test_rejects_bad_horizons_and_duplicates():
    with pytest.raises(ValueError):
        make_labels(_prices(), [0])
    with pytest.raises(ValueError):
        make_labels(pd.concat([_prices(), _prices()]), [1])


def test_vol_columns():
    out = realized_vol_label(_prices(), 2)
    assert list(out.columns) == VOL_LABEL_COLUMNS
    assert set(out["horizon"]) == {2}


@pytest.mark.parametrize("key", list(VOL_CASES))
def test_vol_cases(key):
    ticker, i, h, demean = key
    out = realized_vol_label(_prices(), h, demean=demean)
    row = out[(out["ticker"] == ticker) & (out["date"] == pd.Timestamp(LABEL_DAYS[i]))]
    assert _close(float(row["value"].iloc[0]), VOL_CASES[key])


# --- A4 v2 (ADR 0002 item 4): vol-surprise label and controls known at t ---------------------
# Experiment 0003 bug 1: the A4 label was the forward vol LEVEL, so any static per-ticker offset
# ranked with it. The v2 label is ln(forward vol / trailing 22-day vol) and the controls are
# trailing vols, all ending at t.

def _near(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _one_ticker(closes: list[float], ticker: str = "X") -> pd.DataFrame:
    days = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"date": days, "ticker": ticker, "adj_close": closes})
    df["adj_open"] = df["adj_close"]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = 1.0
    return df[PRICE_COLUMNS]


def _ref_rv(r: list[float], lo: int, hi: int) -> float:
    """sqrt(mean r_s^2) over s = lo..hi (r_s = ln(c_s / c_{s-1})); NaN if out of range."""
    if lo < 1 or hi > len(r):
        return float("nan")
    xs = r[lo - 1:hi]
    return math.sqrt(sum(x * x for x in xs) / len(xs))


def _closes(n: int, seed: int = 0) -> tuple[list[float], list[float]]:
    rng = np.random.default_rng(seed)
    r = list(rng.normal(0, 0.01, n - 1) * rng.uniform(0.5, 2.0, n - 1))
    c = [100.0]
    for x in r:
        c.append(c[-1] * math.exp(x))
    return c, r  # r[s-1] = ln(c[s] / c[s-1])


@pytest.mark.parametrize("h", [1, 5])
def test_vol_surprise_matches_loop_reference(h):
    closes, r = _closes(90)
    out = vol_surprise_label(_one_ticker(closes), h)
    assert list(out.columns) == VOL_LABEL_COLUMNS and set(out["horizon"]) == {h}
    for t in range(len(closes)):
        fwd, trail = _ref_rv(r, t + 1, t + h), _ref_rv(r, t - 21, t)
        expected = math.log(fwd / trail) if math.isfinite(fwd) and math.isfinite(trail) else math.nan
        assert _near(float(out["value"].iloc[t]), expected), t


def test_vol_surprise_nan_without_full_trailing_window():
    closes, _ = _closes(40)
    out = vol_surprise_label(_one_ticker(closes), 1)
    # 22 trailing returns ending at t need closes t-22..t: first defined at t = 22
    assert out["value"].iloc[:22].isna().all() and out["value"].iloc[22:39].notna().all()
    assert math.isnan(out["value"].iloc[39])  # no forward bar


def test_vol_surprise_zero_vol_is_nan_not_inf():
    out = vol_surprise_label(_one_ticker([100.0] * 40), 1)
    assert not np.isinf(out["value"]).any() and out["value"].isna().all()


def test_vol_surprise_ignores_bars_outside_its_windows():
    """Label at t uses closes t-22..t+h only: perturbing earlier or later bars changes nothing."""
    closes, _ = _closes(80)
    t, h = 40, 5
    base = float(vol_surprise_label(_one_ticker(closes), h)["value"].iloc[t])
    pert = list(closes)
    for k in [*range(t - 22), *range(t + h + 1, 80)]:
        pert[k] = closes[k] * (3.0 if k % 2 else 0.3)
    assert _near(float(vol_surprise_label(_one_ticker(pert), h)["value"].iloc[t]), base)


def test_vol_controls_match_loop_reference_and_are_known_at_t():
    closes, r = _closes(120, seed=3)
    ctl = vol_controls(_one_ticker(closes))
    assert list(ctl.columns) == ["date", "ticker", *VOL_CONTROL_NAMES]
    for t in (60, 61, 100, 119):
        row = ctl.iloc[t]
        assert _near(float(row["ln_rv5"]), math.log(_ref_rv(r, t - 4, t)))
        assert _near(float(row["ln_rv22"]), math.log(_ref_rv(r, t - 21, t)))
        assert _near(float(row["ln_rv60"]), math.log(_ref_rv(r, t - 59, t)))
        assert _near(float(row["ln_rv_lr"]), math.log(_ref_rv(r, 1, t)))
    # warm-up: rv60 and the expanding long-run vol need 60 returns (closes 0..60)
    assert ctl[VOL_CONTROL_NAMES].iloc[:60].isna().any(axis=1).all()
    assert ctl[VOL_CONTROL_NAMES].iloc[60:].notna().all(axis=None)
    # causality: perturbing every close after t leaves the controls at t unchanged
    t = 80
    pert = closes[:t + 1] + [c * 5.0 for c in closes[t + 1:]]
    after = vol_controls(_one_ticker(pert))
    pd.testing.assert_frame_equal(ctl.iloc[:t + 1], after.iloc[:t + 1])
