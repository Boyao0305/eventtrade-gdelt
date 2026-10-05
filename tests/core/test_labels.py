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

from eventtrade.core.labels import VOL_LABEL_COLUMNS, make_labels, realized_vol_label
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
