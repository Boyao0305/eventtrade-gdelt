"""Alpaca intraday adapter: paging, regular-session filter across DST, retries, credentials."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from eventtrade.adapters import intraday as ia


class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, responses: list[_Resp]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, headers, params, timeout):
        self.calls.append(dict(params))
        return self.responses.pop(0)


def _bar(t: str, o: float = 1.0, c: float = 2.0, v: float = 10.0) -> dict:
    return {"t": t, "o": o, "h": 3.0, "l": 0.5, "c": c, "v": v, "vw": 1.5, "n": 7}


KEY = ("id", "secret")


def _fetch(session: _Session, **kw) -> pd.DataFrame:
    return ia.fetch_alpaca_bars(["EWJ", "FXI"], date(2016, 1, 4), date(2016, 1, 4), KEY,
                                session=session, sleep=lambda s: None, clock=lambda: 0.0, **kw)


def test_pages_and_filters_regular_session_winter():
    # 2016-01-04 is EST (UTC-5): 09:30 ET = 14:30Z, last regular bar starts 15:45 ET = 20:45Z.
    s = _Session([
        _Resp(200, {"bars": {"EWJ": [_bar("2016-01-04T14:15:00Z"), _bar("2016-01-04T14:30:00Z")]},
                    "next_page_token": "p2"}),
        _Resp(200, {"bars": {"EWJ": [_bar("2016-01-04T20:45:00Z")],
                             "FXI": [_bar("2016-01-04T21:00:00Z"), _bar("2016-01-04T15:00:00Z")]},
                    "next_page_token": None}),
    ])
    df = _fetch(s)
    assert list(df.columns) == ["ts", "ticker", "open", "high", "low", "close", "volume", "vwap",
                                "trade_count"]
    assert df["trade_count"].tolist() == [7, 7, 7] and df["vwap"].tolist() == [1.5] * 3
    got = {(r.ticker, pd.Timestamp(r.ts, unit="s", tz="UTC").strftime("%H:%M")) for r in df.itertuples()}
    assert got == {("EWJ", "14:30"), ("EWJ", "20:45"), ("FXI", "15:00")}
    assert s.calls[0]["feed"] == "sip" and s.calls[0]["timeframe"] == "15Min"
    assert s.calls[0]["adjustment"] == "all" and "page_token" not in s.calls[0]
    assert s.calls[1]["page_token"] == "p2"


def test_regular_session_summer_offset():
    # 2016-07-05 is EDT (UTC-4): 09:30 ET = 13:30Z; 13:15Z is pre-market, 19:45Z is the last bar.
    ts = [int(pd.Timestamp(t).timestamp()) for t in
          ("2016-07-05T13:15:00Z", "2016-07-05T13:30:00Z", "2016-07-05T19:45:00Z",
           "2016-07-05T20:00:00Z")]
    df = pd.DataFrame({"ts": ts, "ticker": "X", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                       "volume": 1.0, "vwap": 1.0, "trade_count": 1})
    kept = ia.regular_session(df)["ts"].tolist()
    assert kept == ts[1:3]


def test_retries_429_then_succeeds():
    s = _Session([_Resp(429, {"message": "slow down"}),
                  _Resp(200, {"bars": {"EWJ": [_bar("2016-01-04T15:00:00Z")]}})])
    assert len(_fetch(s)) == 1 and len(s.calls) == 2


def test_client_error_raises_without_retry():
    s = _Session([_Resp(403, {"message": "forbidden"})])
    with pytest.raises(ia.AlpacaError, match="403"):
        _fetch(s)
    assert len(s.calls) == 1


def test_gives_up_after_max_retries():
    s = _Session([_Resp(503)] * ia.MAX_RETRIES)
    with pytest.raises(ia.AlpacaError, match="gave up"):
        _fetch(s)


def test_end_date_is_inclusive_in_new_york():
    s = _Session([_Resp(200, {"bars": {}})])
    ia.fetch_alpaca_bars(["SPY"], date(2016, 1, 4), date(2016, 1, 5), KEY, session=s,
                         sleep=lambda x: None, clock=lambda: 0.0)
    assert s.calls[0]["start"].startswith("2016-01-04T00:00:00-05:00")
    assert s.calls[0]["end"].startswith("2016-01-06T00:00:00-05:00")


def test_load_credentials(tmp_path: Path):
    p = tmp_path / "alpaca.env"
    p.write_text("# comment\nAPCA_API_KEY_ID = abc\nAPCA_API_SECRET_KEY=xyz\n", encoding="utf-8")
    assert ia.load_credentials(p) == ("abc", "xyz")
    p.write_text("APCA_API_KEY_ID=abc\n", encoding="utf-8")
    with pytest.raises(ia.AlpacaError, match="APCA_API_SECRET_KEY"):
        ia.load_credentials(p)
    with pytest.raises(ia.AlpacaError, match="no Alpaca credentials"):
        ia.load_credentials(tmp_path / "missing.env")
