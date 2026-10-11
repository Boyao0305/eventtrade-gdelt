"""ADR 0015 amendment 2 inputs: prices and splits of mapped DoD issuers (fakes, no network)."""

from datetime import date
from types import SimpleNamespace

import pandas as pd

from eventtrade.adapters import prices
from eventtrade.adapters.dod_store import DodStore
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_dod as pdod
from eventtrade.config import Config

MAP = pd.DataFrame({"cik": ["1", "1", "2", None, "3"],
                    "ticker_now": ["AAA", "AAA", "BRK-B", "ZZZ", None]})


def test_mapped_tickers_plain_symbols_only():
    assert pdod.mapped_tickers(MAP) == ["AAA"]


def test_fetch_splits_filters_by_date_and_drops_tz():
    idx = pd.DatetimeIndex(["2018-06-01", "2025-06-01"]).tz_localize("America/New_York")
    fake = {"AAA": SimpleNamespace(splits=pd.Series([2.0, 3.0], index=idx)),
            "BBB": SimpleNamespace(splits=pd.Series(dtype=float))}
    df = prices.fetch_splits(["AAA", "BBB"], date(2025, 4, 1), fake.__getitem__)
    assert df.values.tolist() == [["AAA", "2018-06-01", 2.0]]


def test_ingest_dod_prices_fetches_missing_then_resumes(tmp_path):
    cfg = Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")
    ps = SqliteStore(tmp_path / "dodcap.sqlite")
    ds = DodStore(tmp_path / "dod.sqlite", date(2015, 1, 1), cfg.holdout_start)
    asked: list[list[str]] = []

    def fetch(ts, start, end):
        asked.append(list(ts))
        d = pd.bdate_range("2016-01-04", periods=3)
        return pd.DataFrame([(x, t, 10.0, 10.0, 10.0, 10.0, 1e6, 10.0, 10.0)
                             for t in ts for x in d],
                            columns=["date", "ticker", "open", "high", "low", "close", "volume",
                                     "adj_open", "adj_close"])

    split_calls: list[list[str]] = []

    def splits(ts):
        split_calls.append(list(ts))
        return pd.DataFrame([("AAA", "2018-06-01", 2.0)], columns=["ticker", "date", "ratio"])

    out: list[str] = []
    assert pdod.ingest_dod_prices(cfg, MAP, ps, ds, fetch, splits, None, out.append) == pdod.OK
    assert "AAA" in asked[0] and "IWM" in asked[0] and "ITA" in asked[0]
    assert ds.splits() == [("AAA", "2018-06-01", 2.0)]
    assert pdod.ingest_dod_prices(cfg, MAP, ps, ds, fetch, splits, None, out.append) == pdod.OK
    assert len(asked) == 1 and len(split_calls) == 1          # nothing refetched
