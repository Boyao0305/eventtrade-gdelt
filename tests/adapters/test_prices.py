"""adapters/prices.py — yfinance/Stooq parsing with fakes, retries, cross-check and sanity flags.

Fast tests use fake downloaders and a hand-written Stooq CSV in Stooq's historical
`Date,Open,High,Low,Close,Volume` layout (the live endpoint was behind a JS bot check when this
was written, so no live capture exists). Network tests are marked slow.
"""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.prices import (
    PriceFetchError,
    StooqError,
    _from_yf_frame,
    _parse_stooq_csv,
    cross_check,
    fetch_stooq,
    fetch_yfinance,
    sanity_check,
)
from eventtrade.schemas import PRICE_COLUMNS

FIX = Path(__file__).parent / "fixtures"


# --- fakes ----------------------------------------------------------------------------------

def _yf_frame(data: dict[str, list[tuple]], dates: list[str]) -> pd.DataFrame:
    """Build what yf.download(group_by='ticker', auto_adjust=False) returns.

    data: ticker -> rows of (Open, High, Low, Close, Adj Close, Volume); None row = all NaN.
    """
    fields = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    cols, vals = [], []
    for t, rows in data.items():
        for j, f in enumerate(fields):
            cols.append((t, f))
            vals.append([np.nan if r is None else r[j] for r in rows])
    df = pd.DataFrame(np.array(vals, dtype=float).T, index=pd.DatetimeIndex(dates, name="Date"),
                      columns=pd.MultiIndex.from_tuples(cols, names=["Ticker", "Price"]))
    return df


class FakeDownload:
    """Scripted yf.download: `script[call_no]` maps ticker -> rows, or is an Exception to raise."""

    def __init__(self, script, dates=("2020-01-02", "2020-01-03")):
        self.script, self.dates, self.calls = script, list(dates), []

    def __call__(self, tickers, **kw):
        self.calls.append((list(tickers), kw))
        step = self.script[min(len(self.calls), len(self.script)) - 1]
        if isinstance(step, Exception):
            raise step
        data = {t: step.get(t, [None] * len(self.dates)) for t in tickers}
        return _yf_frame(data, self.dates)


ROW_A = [(10.0, 11.0, 9.0, 10.0, 9.0, 100), (11.0, 12.0, 10.0, 12.0, 10.8, 200)]
ROW_B = [(50.0, 51.0, 49.0, 50.0, 50.0, 5), (50.0, 51.0, 49.0, 51.0, 51.0, 6)]


# --- _from_yf_frame: column mapping and adj_open = open * adj_close / close -----------------

def test_from_yf_frame_maps_columns_and_adjusts_open():
    raw = _yf_frame({"AAA": ROW_A + [None]}, ["2020-01-02", "2020-01-03", "2020-01-06"])
    out = _from_yf_frame(raw, "AAA")
    assert list(out.columns) == PRICE_COLUMNS
    assert len(out) == 2  # the all-NaN row is dropped
    assert out["date"].tolist() == [pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-03")]
    assert out["adj_open"].tolist() == pytest.approx([9.0, 11.0 * 10.8 / 12.0])
    assert out["adj_close"].tolist() == [9.0, 10.8]
    assert out["close"].tolist() == [10.0, 12.0]
    assert out["ticker"].unique().tolist() == ["AAA"]


def test_from_yf_frame_tz_aware_index_becomes_naive_midnight():
    raw = _yf_frame({"AAA": ROW_A}, ["2020-01-02", "2020-01-03"])
    raw.index = raw.index.tz_localize("America/New_York")
    out = _from_yf_frame(raw, "AAA")
    assert out["date"].dt.tz is None
    assert out["date"].tolist() == [pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-03")]


# --- fetch_yfinance: batching, retry with backoff, failures never dropped silently ----------

def test_fetch_all_ok_one_call_end_is_inclusive():
    fake = FakeDownload([{"AAA": ROW_A, "BBB": ROW_B}])
    sleeps: list[float] = []
    out = fetch_yfinance(["aaa", "BBB"], date(2020, 1, 2), date(2020, 1, 3),
                         download=fake, sleep=sleeps.append)
    assert len(fake.calls) == 1 and sleeps == []
    kw = fake.calls[0][1]
    assert kw["auto_adjust"] is False and kw["group_by"] == "ticker"
    assert (kw["start"], kw["end"]) == ("2020-01-02", "2020-01-04")  # yfinance end is exclusive
    assert list(out.columns) == PRICE_COLUMNS
    assert out[["date", "ticker"]].astype(str).values.tolist() == [
        ["2020-01-02", "AAA"], ["2020-01-02", "BBB"], ["2020-01-03", "AAA"], ["2020-01-03", "BBB"]]


def test_fetch_retries_only_failed_tickers_with_exponential_backoff():
    fake = FakeDownload([{"AAA": ROW_A}, {}, {"BBB": ROW_B}])
    sleeps: list[float] = []
    out = fetch_yfinance(["AAA", "BBB"], date(2020, 1, 2), date(2020, 1, 3),
                         download=fake, sleep=sleeps.append, backoff_s=1.0)
    assert [c[0] for c in fake.calls] == [["AAA", "BBB"], ["BBB"], ["BBB"]]
    assert sleeps == [1.0, 2.0]
    assert sorted(out["ticker"].unique()) == ["AAA", "BBB"]


def test_fetch_permanent_failure_raises_with_partial_prices():
    fake = FakeDownload([{"AAA": ROW_A}])
    with pytest.raises(PriceFetchError) as ei:
        fetch_yfinance(["AAA", "ZZZ"], date(2020, 1, 2), date(2020, 1, 3),
                       download=fake, sleep=lambda s: None, max_retries=2)
    assert set(ei.value.failures) == {"ZZZ"}
    assert ei.value.prices["ticker"].unique().tolist() == ["AAA"]
    assert len(fake.calls) == 3  # first try + 2 retries


def test_fetch_exception_from_download_is_retried():
    fake = FakeDownload([ConnectionError("boom"), {"AAA": ROW_A}])
    out = fetch_yfinance(["AAA"], date(2020, 1, 2), date(2020, 1, 3),
                         download=fake, sleep=lambda s: None)
    assert len(fake.calls) == 2 and len(out) == 2


def test_fetch_batches():
    fake = FakeDownload([{"A": ROW_A, "B": ROW_A, "C": ROW_B}])
    out = fetch_yfinance(["A", "B", "C"], date(2020, 1, 2), date(2020, 1, 3),
                         download=fake, sleep=lambda s: None, batch_size=2)
    assert [c[0] for c in fake.calls] == [["A", "B"], ["C"]]
    assert sorted(out["ticker"].unique()) == ["A", "B", "C"]


# --- Stooq parsing ----------------------------------------------------------------------------

def test_parse_stooq_csv_fixture():
    out = _parse_stooq_csv((FIX / "prices_stooq_spy.csv").read_text(), "SPY")
    assert list(out.columns) == PRICE_COLUMNS
    assert len(out) == 4
    assert out["date"].iat[0] == pd.Timestamp("2020-08-27")
    assert (out["adj_close"] == out["close"]).all() and (out["adj_open"] == out["open"]).all()


@pytest.mark.parametrize("body", [
    # browser verification page (what stooq.com served to scripts in 2026-10)
    '<!DOCTYPE html><html><body><noscript>This site requires JavaScript</noscript></body></html>',
    # legacy "no data" / quota messages
    "No data",
    "Exceeded the daily hits limit",
    # header only
    "Date,Open,High,Low,Close,Volume\n",
])
def test_parse_stooq_rejects_non_csv(body):
    with pytest.raises(StooqError):
        _parse_stooq_csv(body, "SPY")


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_fetch_stooq_builds_symbol_and_filters_dates():
    seen = {}

    def get(url, params, timeout, headers):
        seen.update(params)
        return _Resp((FIX / "prices_stooq_spy.csv").read_text())

    out = fetch_stooq("BRK-B", date(2020, 8, 28), date(2020, 8, 31), get=get)
    assert seen["s"] == "brk.b.us" and seen["i"] == "d"
    assert (seen["d1"], seen["d2"]) == ("20200828", "20200831")
    assert out["date"].dt.strftime("%Y-%m-%d").tolist() == ["2020-08-28", "2020-08-31"]
    assert out["ticker"].unique().tolist() == ["BRK-B"]


# --- cross_check: return-level agreement per ticker ----------------------------------------

def _px(ticker, closes, start="2020-01-01"):
    d = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"date": d, "ticker": ticker, "open": c, "high": c, "low": c, "close": c,
                         "volume": 1.0, "adj_open": c, "adj_close": c})[PRICE_COLUMNS]


@pytest.mark.parametrize("a, b, expect", [
    # identical series (scale does not matter: returns are compared) -> no mismatch
    (_px("X", [10, 11, 12, 13]), _px("X", [20, 22, 24, 26]),
     {"n_common_days": 4, "n_mismatch_days": 0, "median_abs_ret_diff": 0.0}),
    # one day differs by 2% -> two mismatching returns (into and out of the bad print)
    (_px("X", [10, 10, 10, 10]), _px("X", [10, 10.2, 10, 10]),
     {"n_common_days": 4, "n_mismatch_days": 2}),
    # extra days on one side are counted, not compared
    (_px("X", [10, 11, 12]), _px("X", [10, 11, 12, 13, 14]),
     {"n_common_days": 3, "n_only_a": 0, "n_only_b": 2, "n_mismatch_days": 0}),
])
def test_cross_check_cases(a, b, expect):
    row = cross_check(a, b).set_index("ticker").loc["X"]
    for k, v in expect.items():
        assert row[k] == pytest.approx(v)


def test_cross_check_ticker_missing_on_one_side():
    out = cross_check(_px("X", [1, 2]), _px("Y", [1, 2])).set_index("ticker")
    assert out.loc["X", "n_common_days"] == 0 and out.loc["X", "n_only_a"] == 2
    assert np.isnan(out.loc["Y", "median_abs_ret_diff"])


# --- sanity_check: what gets flagged ---------------------------------------------------------

def _with_dates(ticker, dates, closes):
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"date": pd.to_datetime(dates), "ticker": ticker, "open": c, "high": c,
                         "low": c, "close": c, "volume": 1.0, "adj_open": c,
                         "adj_close": c})[PRICE_COLUMNS]


@pytest.mark.parametrize("frame, expected", [
    # clean series -> nothing
    (_px("X", [10, 10.5, 10.2, 10.4]), []),
    # zero or negative price anywhere in OHLC/adjusted -> nonpositive_price on that row
    (_px("X", [10, 0, 10]), [("2020-01-02", "nonpositive_price")]),
    # |return| > 40% flagged; exactly 39% is not
    (_px("X", [10, 15, 15]), [("2020-01-02", "extreme_return")]),
    (_px("X", [10, 13.9, 13.9]), []),
    (_px("X", [10, 5.9, 5.9]), [("2020-01-02", "extreme_return")]),
    # gap: Mon 2020-01-06 -> Tue 2020-01-14 skips 5 weekdays (not flagged);
    #      Mon 2020-01-06 -> Wed 2020-01-15 skips 6 weekdays (flagged on the later date)
    (_with_dates("X", ["2020-01-06", "2020-01-14"], [10, 10]), []),
    (_with_dates("X", ["2020-01-06", "2020-01-15"], [10, 10]), [("2020-01-15", "gap")]),
])
def test_sanity_check_cases(frame, expected):
    out = sanity_check(frame)
    got = [(d.strftime("%Y-%m-%d"), i) for d, i in zip(out["date"], out["issue"])]
    assert got == expected


# --- live (slow) ----------------------------------------------------------------------------

@pytest.mark.slow
def test_yfinance_live_three_tickers():
    # AAPL split 4:1 on 2020-08-31; SPY, EWJ are Track A. Window is pre-hold-out.
    out = fetch_yfinance(["SPY", "EWJ", "AAPL"], date(2020, 8, 24), date(2020, 9, 4))
    assert set(out["ticker"]) == {"SPY", "EWJ", "AAPL"}
    assert out.groupby("ticker").size().min() >= 9
    assert (out["adj_close"] <= out["close"] + 1e-9).all()  # dividends only lower history
    assert sanity_check(out).empty  # split is adjusted, so no 75% "drop"
    assert out["date"].max() <= pd.Timestamp("2020-09-04")


@pytest.mark.slow
def test_stooq_live_or_blocked():
    try:
        out = fetch_stooq("SPY", date(2020, 8, 24), date(2020, 9, 4))
    except StooqError as e:
        pytest.skip(f"Stooq unavailable: {e}")
    assert len(out) >= 9
