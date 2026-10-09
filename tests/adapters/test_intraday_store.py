"""Intraday store: atomic per-(ticker, year) writes, resume ledgers, half-open reads."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from eventtrade.adapters.intraday_store import (
    EVENTS_15MIN_CAT_COLUMNS,
    EVENTS_15MIN_COLUMNS,
    IntradayStore,
)

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


def _cat_row(entity: str, geo: str, ts: int, n: int) -> list:
    return [entity, geo, ts, n, 1, 0, 2, 0, 1, 0, 0, 1, 0, 0, 1, 7.0, -2.5]


def test_events_15min_cat_key_includes_geo_and_reads_in_key_order(store):
    assert EVENTS_15MIN_CAT_COLUMNS[:3] == ["entity", "geo", "batch_ts"]
    assert len(EVENTS_15MIN_CAT_COLUMNS) == 17
    rows = pd.DataFrame([_cat_row("JA", "actor", 200, 3), _cat_row("JA", "action", 200, 5),
                         _cat_row("JA", "action", 100, 4), _cat_row("_GLOBAL", "action", 100, 9)],
                        columns=EVENTS_15MIN_CAT_COLUMNS)
    chunk = (date(2016, 1, 1), date(2016, 12, 31))
    n = store.write_gdelt_chunk("events_15min_cat", "events_15min_cat", chunk, rows, 5, NOW)
    assert n == 4  # (JA, action, 200) and (JA, actor, 200) are distinct keys
    out = store.read_gdelt("events_15min_cat", None, None)
    assert list(out.columns) == EVENTS_15MIN_CAT_COLUMNS
    assert list(zip(out["entity"], out["geo"], out["batch_ts"], strict=True)) == [
        ("JA", "action", 100), ("JA", "action", 200), ("JA", "actor", 200),
        ("_GLOBAL", "action", 100)]
    assert out["b163"].tolist() == [1, 1, 1, 1] and out["tone_x_articles"].iloc[0] == -2.5
    assert len(store.read_gdelt("events_15min_cat", 150, None)) == 2  # half-open on batch_ts
    assert store.pulled_chunks("events_15min_cat") == {chunk}
    assert store.pulled_chunks("events_15min") == set()
    # a re-pulled chunk replaces its rows (upsert on the key)
    again = pd.DataFrame([_cat_row("JA", "actor", 200, 8)], columns=EVENTS_15MIN_CAT_COLUMNS)
    store.write_gdelt_chunk("events_15min_cat", "events_15min_cat", chunk, again, 5, NOW)
    got = store.read_gdelt("events_15min_cat", None, None)
    assert got.loc[got["geo"] == "actor", "n_events"].tolist() == [8]
    assert store.counts()["events_15min_cat"] == 4
    with pytest.raises(ValueError, match="columns"):
        store.write_gdelt_chunk("events_15min_cat", "events_15min_cat", chunk,
                                rows.drop(columns="geo"), 0, NOW)
