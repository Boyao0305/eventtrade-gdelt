"""Intraday store: atomic per-(ticker, year) writes, resume ledgers, half-open reads."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from eventtrade.adapters.intraday_store import EVENTS_15MIN_COLUMNS, IntradayStore

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _ts(s: str) -> int:
    return int(pd.Timestamp(s).timestamp())


def _bars(ticker: str, stamps: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"ts": [_ts(s) for s in stamps], "ticker": ticker, "open": 1.0,
                         "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0,
                         "vwap": [1.2, *[float("nan")] * (len(stamps) - 1)][:len(stamps)],
                         "trade_count": 9})


@pytest.fixture
def store(tmp_path) -> IntradayStore:
    s = IntradayStore(tmp_path / "intraday.sqlite")
    s.init()
    s.init()  # idempotent
    return s


def test_write_year_replaces_and_records_fetch(store):
    assert store.write_bars_year("EWJ", 2016, _bars("EWJ", ["2016-01-04T14:30Z", "2016-01-04T14:45Z"]),
                                 NOW) == 2
    assert store.write_bars_year("EWJ", 2016, _bars("EWJ", ["2016-01-05T14:30Z"]), NOW) == 1
    out = store.read_bars(["EWJ"], None, None)
    assert out["ts"].tolist() == [_ts("2016-01-05T14:30Z")]
    assert store.fetched_years() == {("EWJ", 2016)}
    assert store.write_bars_year("EEM", 2017, _bars("EEM", [])[0:0], NOW) == 0  # empty year counts
    assert ("EEM", 2017) in store.fetched_years()


def test_write_year_rejects_wrong_ticker_or_year(store):
    with pytest.raises(ValueError, match="expected only"):
        store.write_bars_year("EWJ", 2016, _bars("FXI", ["2016-01-04T14:30Z"]), NOW)
    with pytest.raises(ValueError, match="outside year"):
        store.write_bars_year("EWJ", 2016, _bars("EWJ", ["2017-01-03T14:30Z"]), NOW)
    assert store.fetched_years() == set()


def test_read_bars_half_open_and_nan_vwap(store):
    store.write_bars_year("A", 2016, _bars("A", ["2016-01-04T14:30Z", "2016-01-04T14:45Z"]), NOW)
    store.write_bars_year("B", 2016, _bars("B", ["2016-01-04T14:30Z"]), NOW)
    out = store.read_bars(None, _ts("2016-01-04T14:30Z"), _ts("2016-01-04T14:45Z"))
    assert out["ticker"].tolist() == ["A", "B"]
    assert pd.isna(store.read_bars(["A"], None, None)["vwap"].iloc[1])


def test_gdelt_chunk_ledger_and_columns(store):
    rows = pd.DataFrame([["_GLOBAL", 100, 50, 7, 70.0, -3.5], ["JA", 100, 5, 1, 9.0, -1.0]],
                        columns=EVENTS_15MIN_COLUMNS)
    chunk = (date(2016, 1, 1), date(2016, 12, 31))
    assert store.write_gdelt_chunk("events_15min", "events_15min", chunk, rows, 123, NOW) == 2
    assert store.pulled_chunks("events_15min") == {chunk}
    assert store.pulled_chunks("orgs_15min") == set()
    assert store.read_gdelt("events_15min", None, None)["entity"].tolist() == ["JA", "_GLOBAL"]
    with pytest.raises(ValueError, match="columns"):
        store.write_gdelt_chunk("orgs_15min", "orgs_15min", chunk, rows, 0, NOW)
    assert store.counts()["events_15min"] == 2
