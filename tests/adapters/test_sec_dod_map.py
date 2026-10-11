"""ADR 0015 amendment 3 SEC adapter and store parts: Exhibit 21 search paging and filter,
EDGAR former names (404), and the resumable `ex21_hits` / `sec_names` caches."""

from datetime import UTC, datetime

import pandas as pd
import pytest

from eventtrade.adapters import sec
from eventtrade.adapters.sec_store import SecStore

NOW = datetime(2026, 10, 11, tzinfo=UTC)


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.content, self.text = status, payload, b"", text

    def json(self):
        return self._p


class _Sess:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.resps.pop(0)


def _client(resps):
    s = _Sess(resps)
    return sec.SecClient("x y@z.com", session=s, sleep=lambda t: None, clock=lambda: 0.0), s


def _hit(adsh, ftype, ciks, fd="2018-02-12"):
    return {"_id": f"{adsh}:doc.htm", "_source": {"adsh": adsh, "file_type": ftype, "ciks": ciks,
                                                  "file_date": fd, "form": "10-K"}}


def _page(total, hits):
    return {"hits": {"total": {"value": total}, "hits": hits}}


def test_ex21_pages_filters_and_dedups():
    p1 = _page(5, [_hit("a", "EX-21", ["0000040533"]), _hit("b", "10-K", ["0000000001"]),
                   _hit("c", "EX-21.1", ["40533", "0000000009"], "2019-02-13")])
    p2 = _page(5, [_hit("c", "ex-21.1", ["40533"], "2019-02-13"), _hit("d", "EX-22", ["1"])])
    c, s = _client([_Resp(200, p1), _Resp(200, p2)])
    df = sec.ex21_parents(c, "electric boat", '"electric boat"')
    assert list(df.columns) == sec.EX21_COLS
    assert list(df["adsh"]) == ["a", "c"] and list(df["doc"]) == ["doc.htm", "doc.htm"]
    assert set(df["cik"]) == {"0000040533"} and set(df["phrase"]) == {"electric boat"}
    assert df.attrs == {"total": 5, "capped": False}
    assert s.calls[0][1]["q"] == '"electric boat"' and s.calls[0][1]["forms"] == sec.EX21_FORMS
    assert s.calls[0][1]["startdt"] == "2014-01-01" and s.calls[0][1]["enddt"] == "2025-03-31"
    assert [p["from"] for _, p in s.calls] == [0, 3]  # offset advances by the page length


def test_ex21_empty_and_cap(monkeypatch):
    c, _ = _client([_Resp(200, _page(0, []))])
    df = sec.ex21_parents(c, "nothing here")
    assert df.empty and df.attrs["capped"] is False
    monkeypatch.setattr(sec, "EFTS_CAP", 2)
    c, s = _client([_Resp(200, _page(50, [_hit("a", "EX-21", ["1"]), _hit("b", "10-K", ["2"])]))])
    df = sec.ex21_parents(c, "common words")
    assert df.attrs == {"total": 50, "capped": True} and len(s.calls) == 1 and len(df) == 1


def test_former_names_dates_and_404():
    payload = {"name": "RTX Corp", "formerNames": [
        {"name": "UNITED TECHNOLOGIES CORP /DE/", "from": "1994-05-03T00:00:00.000Z",
         "to": "2020-04-03T00:00:00.000Z"},
        {"name": "RAYTHEON TECHNOLOGIES CORP", "from": "2020-04-03T00:00:00.000Z",
         "to": "2023-07-17T00:00:00.000Z"}]}
    payload["filings"] = {"recent": {"filingDate": ["2026-10-01", "2026-10-09", "2025-01-02"]}}
    c, s = _client([_Resp(200, payload)])
    assert sec.former_names(c, "101829") == ([
        ("UNITED TECHNOLOGIES CORP /DE/", "1994-05-03", "2020-04-03"),
        ("RAYTHEON TECHNOLOGIES CORP", "2020-04-03", "2023-07-17"),
        ("RTX Corp", "2023-07-17", None)], "2026-10-09")
    assert s.calls[0][0].endswith("CIK0000101829.json")
    c, _ = _client([_Resp(200, {"name": "Boeing Co", "formerNames": []})])
    assert sec.former_names(c, "12927") == ([("Boeing Co", None, None)], None)
    c, _ = _client([_Resp(404)])
    assert sec.former_names(c, "1") == ([], None)
    c, _ = _client([_Resp(403)])
    with pytest.raises(sec.SecError):
        sec.former_names(c, "1")


def test_ex21_document_url_and_404():
    c, s = _client([_Resp(200, text="<p>Electric Boat Corporation</p>")])
    assert sec.ex21_document(c, "0000040533", "0000040533-18-000008", "ex21.htm").startswith("<p>")
    assert s.calls[0][0] == ("https://www.sec.gov/Archives/edgar/data/40533/"
                             "000004053318000008/ex21.htm")
    c, _ = _client([_Resp(404)])
    assert sec.ex21_document(c, "1", "0000000001-18-000001", "x.htm") == ""


def test_store_caches_are_resumable_and_replace(tmp_path):
    st = SecStore(tmp_path / "sec.sqlite")
    st.init()
    df = pd.DataFrame([("electric boat", "0000040533", "2018-02-12", "a", "d.htm")],
                      columns=sec.EX21_COLS)
    assert st.write_ex21("electric boat", df, NOW) == 1
    assert st.write_ex21("nothing", df.iloc[:0], NOW) == 0
    assert st.pulled("ex21") == {"electric boat", "nothing"}
    st.write_ex21("electric boat", pd.concat([df, df.assign(adsh="b")]), NOW, capped=True,
                  total=12000)
    assert len(st.read("ex21_hits")) == 2 and st.pulled("ex21cap") == {"electric boat"}
    st.write_ex21("electric boat", df, NOW)  # a full rerun clears the cap mark
    assert st.pulled("ex21cap") == set() and len(st.read("ex21_hits")) == 1
    st.write_names("0000101829", [("A", None, "2020-04-03"), ("B", "2020-04-03", None)], NOW)
    st.write_names("0000000001", [], NOW)
    st.write_names("0000101829", [("B", "2020-04-03", None)], NOW, last_filing="2026-10-09")
    nm = st.read("sec_names")
    assert list(nm["name"]) == ["B"] and nm["valid_to"].isna().all()
    assert list(nm["last_filing"]) == ["2026-10-09"]
    assert st.pulled("names") == {"0000101829", "0000000001"}
    assert st.write_ex21_entries("a", "d.htm", "0000040533", "2018-02-12",
                                 {"electric boat", "bath iron works"}, NOW) == 2
    assert st.write_ex21_entries("b", "e.htm", "0000040533", "2019-02-12", set(), NOW) == 0
    st.write_ex21_entries("a", "d.htm", "0000040533", "2018-02-12", {"electric boat"}, NOW)
    st.write_ex21_entries("b", "e.htm", "0000040533", "2019-02-12", set(), NOW, version=2)
    assert st.pulled("ex21doc") == {"v1:a/d.htm", "v1:b/e.htm", "v2:b/e.htm"}
    got = st.read_ex21_entries({"electric boat", "nothing"})
    assert list(got["entry_norm"]) == ["electric boat"] and list(got["cik"]) == ["0000040533"]
    st.init()  # idempotent on an existing store
    assert st.counts()["sec_names"] == 1 and st.counts()["ex21_entries"] == 1


def test_store_migrates_old_cache_tables(tmp_path):
    import sqlite3
    path = tmp_path / "sec.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE ex21_hits (phrase TEXT, cik TEXT, file_date TEXT, adsh TEXT);
        CREATE TABLE sec_names (cik TEXT, name TEXT, valid_from TEXT, valid_to TEXT);
        CREATE TABLE cik_tickers (cik TEXT, ticker TEXT, title TEXT);
        INSERT INTO cik_tickers VALUES ('1', 'A', 'A');
        CREATE TABLE sec_pulls (kind TEXT NOT NULL, period TEXT NOT NULL, rows INTEGER NOT NULL,
            pulled_at TEXT NOT NULL, PRIMARY KEY (kind, period));
        INSERT INTO sec_pulls VALUES ('ex21', 'x', 1, 't'), ('names', '1', 1, 't'),
            ('shares', '1', 1, 't');""")
    con.commit()
    con.close()
    st = SecStore(path)
    st.init()
    assert "doc" in st.read("ex21_hits").columns and "last_filing" in st.read("sec_names").columns
    assert st.pulled("ex21") == set() and st.pulled("names") == set()
    assert st.pulled("shares") == {"1"} and len(st.read("cik_tickers")) == 1  # untouched
