"""ADR 0007 intraday ingest: resumable per (ticker, year), hold-out refused, symbols mapped."""

from datetime import UTC, date, datetime

import pandas as pd

from eventtrade.adapters.intraday_store import IntradayStore
from eventtrade.cli import pipelines_intraday as pi
from eventtrade.config import Config
from eventtrade.core import intraday
from eventtrade.schemas import INTRADAY_COLUMNS, Asset

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _asset(t: str, track: str, group: str | None) -> Asset:
    return Asset(ticker=t, name=t, kind="etf" if track == "A" else "stock", track=track,
                 group="country" if track == "A" else "stock", screen_group=group)


class _Store:
    def __init__(self, assets):
        self.assets = assets

    def read_assets(self, track=None):
        return [a for a in self.assets if track is None or a.track == track]


def _fake_fetch(calls):
    def fetch(symbols, start, end):
        calls.append((tuple(symbols), start, end))
        ts = int(datetime(start.year, start.month, start.day, 15, 0, tzinfo=UTC).timestamp())
        return pd.DataFrame([[ts, symbols[0], 1, 1, 1, 1, 10, 1, 3]], columns=INTRADAY_COLUMNS)
    return fetch


def _cfg(tmp_path) -> Config:
    return Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")


def test_resumes_and_maps_class_shares(tmp_path):
    istore = IntradayStore(tmp_path / "i.sqlite")
    calls: list = []
    out: list[str] = []
    rc = pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), istore, _fake_fetch(calls),
                                   ["BRK-B"], date(2016, 1, 4), date(2017, 6, 30), lambda: NOW,
                                   out.append)
    assert rc == pi.OK
    assert calls == [(("BRK.B",), date(2016, 1, 4), date(2016, 12, 31)),
                     (("BRK.B",), date(2017, 1, 1), date(2017, 6, 30))]
    assert istore.fetched_years() == {("BRK-B", 2016), ("BRK-B", 2017)}
    assert set(istore.read_bars(None, None, None)["ticker"]) == {"BRK-B"}
    calls.clear()
    pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), istore, _fake_fetch(calls), ["BRK-B"],
                              date(2016, 1, 4), date(2017, 6, 30), lambda: NOW, out.append)
    assert calls == []  # everything already in the ledger


def test_refuses_holdout(tmp_path):
    rc = pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), IntradayStore(tmp_path / "i.sqlite"),
                                   _fake_fetch([]), ["SPY"], None, date(2025, 4, 1),
                                   lambda: NOW, lambda s: None)
    assert rc == pi.REFUSED


def test_default_order_hedges_then_a_then_b_and_missing_universe(tmp_path):
    assets = [_asset("ZZZ", "B", "Energy"), _asset("EWJ", "A", "dm"), _asset("SPY", "A", None),
              _asset("NOPE", "B", None), _asset("USO", "A", "commodity")]
    order = intraday.fetch_order(assets)
    assert order[:len(intraday.HEDGES)] == list(intraday.HEDGES)
    assert order[len(intraday.HEDGES):] == ["EWJ", "USO", "ZZZ"]
    rc = pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), IntradayStore(tmp_path / "i.sqlite"),
                                   _fake_fetch([]), None, None, None, lambda: NOW, lambda s: None)
    assert rc == pi.MISSING


def test_max_tickers_limits_a_trial_run(tmp_path):
    calls: list = []
    pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), IntradayStore(tmp_path / "i.sqlite"),
                              _fake_fetch(calls), ["A", "B", "C"], date(2016, 1, 4),
                              date(2016, 2, 1), lambda: NOW, lambda s: None, max_tickers=2)
    assert [c[0] for c in calls] == [("A",), ("B",)]
