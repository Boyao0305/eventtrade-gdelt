"""ADR 0011 SEC adapter and store: client rules, 13D paging, date parsing, store, ingest resume."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from eventtrade.adapters import sec
from eventtrade.adapters.sec_store import SecStore
from eventtrade.cli import pipelines_sec as ps
from eventtrade.config import Config

NOW = datetime(2026, 10, 10, tzinfo=UTC)


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._p, self.content = status, payload, b""

    def json(self):
        return self._p


class _Sess:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), headers))
        return self.resps.pop(0)


def _client(resps):
    s = _Sess(resps)
    return sec.SecClient("x y@z.com", session=s, sleep=lambda t: None, clock=lambda: 0.0), s


def test_user_agent_needs_contact_and_retries():
    with pytest.raises(sec.SecError):
        sec.SecClient("anonymous")
    c, s = _client([_Resp(503), _Resp(200, {"ok": 1})])
    assert c.get("u").json() == {"ok": 1} and len(s.calls) == 2
    assert s.calls[0][2]["User-Agent"] == "x y@z.com"
    c2, _ = _client([_Resp(403)])
    with pytest.raises(sec.SecError, match="403"):
        c2.get("u")


def _hit(adsh, form):
    return {"_id": adsh, "_source": {"adsh": adsh, "form": form, "file_date": "2016-03-31",
                                     "ciks": ["0000000001", "0000000002"],
                                     "display_names": ["SUBJ (CIK 0000000001)", "FILER"]}}


def test_13d_pages_until_total_and_dedups():
    page1 = {"hits": {"total": {"value": 3}, "hits": [_hit("a", "SC 13D"), _hit("b", "SC 13D/A")]}}
    page2 = {"hits": {"total": {"value": 3}, "hits": [_hit("b", "SC 13D/A"), _hit("c", "SCHEDULE 13D")]}}
    c, s = _client([_Resp(200, page1), _Resp(200, page2)])
    df = sec.filings_13d(c, date(2016, 3, 1), date(2016, 3, 31))
    assert list(df["adsh"]) == ["a", "b", "c"]
    assert s.calls[1][1]["from"] == 2 and "SCHEDULE 13D" in s.calls[0][1]["forms"]


def test_parse_dates():
    assert sec.parse_sec_date("31-MAR-2016") == date(2016, 3, 31)
    assert sec.parse_sec_date("2016-03-31") == date(2016, 3, 31)
    with pytest.raises(ValueError):
        sec.parse_sec_date("03/31/2016")


def test_quarters_and_months():
    assert ps.quarters(date(2015, 10, 1), date(2016, 4, 2)) == [(2015, 4), (2016, 1), (2016, 2)]
    assert ps.months(date(2016, 1, 15), date(2016, 3, 2)) == [
        (date(2016, 1, 15), date(2016, 1, 31)), (date(2016, 2, 1), date(2016, 2, 29)),
        (date(2016, 3, 1), date(2016, 3, 2))]


def _parts(acc):
    return {"subs": pd.DataFrame([[acc, "31-MAR-2016", "4", "0000320193", "AAPL"]],
                                 columns=sec.SUB_COLS),
            "owners": pd.DataFrame([[acc, "1", "Cook Tim", "Officer", "CEO"]],
                                   columns=sec.OWNER_COLS),
            "trans": pd.DataFrame([[acc, "30-MAR-2016", "P", "100", "10.0", "A", "Common"]],
                                  columns=sec.TRANS_COLS)}


def test_ingest_resumes_and_stops_before_holdout(tmp_path):
    cfg = Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")
    st = SecStore(cfg.sec_db_path)
    calls = {"q": [], "m": []}

    def iq(y, q):
        calls["q"].append((y, q))
        return _parts(f"acc-{y}-{q}")

    def f13(a, b):
        calls["m"].append(a)
        return pd.DataFrame([[f"x{a}", "SC 13D", a.isoformat(), "[]", "[]"]],
                            columns=["adsh", "form", "file_date", "ciks", "names"])

    def tk():
        return pd.DataFrame([["0000320193", "AAPL", "Apple"]], columns=["cik", "ticker", "title"])

    assert ps.ingest_sec(cfg, st, iq, f13, tk, lambda: NOW, lambda s: None) == ps.OK
    assert calls["q"][0] == (2015, 4) and calls["q"][-1] == (2025, 1)
    assert calls["m"][-1] == date(2025, 3, 1)
    n_q, n_m = len(calls["q"]), len(calls["m"])
    assert ps.ingest_sec(cfg, st, iq, f13, tk, lambda: NOW, lambda s: None) == ps.OK
    assert len(calls["q"]) == n_q and len(calls["m"]) == n_m  # nothing re-pulled
    trans = st.read("insider_trans")
    assert len(trans) == n_q and set(trans["trans_code"]) == {"P"}
    assert st.read("cik_tickers")["ticker"].tolist() == ["AAPL"]
