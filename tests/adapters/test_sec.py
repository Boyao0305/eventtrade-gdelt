"""ADR 0011 SEC adapter and store: client rules, 13D paging, date parsing, store, ingest resume."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

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


class _TResp(_Resp):
    def __init__(self, status, payload=None, text=""):
        super().__init__(status, payload)
        self.text = text


def _page(rows):
    keys = ["accessionNumber", "filingDate", "form", "items"]
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


def test_company_submissions_reads_recent_and_older_pages_and_filters():
    recent = _page([("a-25", "2025-04-02", "8-K", "1.01"),       # hold-out: dropped
                    ("a-24", "2025-03-31", "8-K", "1.01,9.01"),
                    ("a-4", "2024-05-01", "4", ""),              # form not kept
                    ("a-q", "2024-04-30", "10-Q", "")])
    root = {"cik": "320193", "filings": {"recent": recent, "files": [
        {"name": "CIK0000320193-submissions-001.json", "filingFrom": "2010-01-01",
         "filingTo": "2016-01-01"},
        {"name": "CIK0000320193-submissions-002.json", "filingFrom": "1994-01-01",
         "filingTo": "2009-12-31"}]}}
    older = _page([("a-m", "2015-06-01", "DEFM14A", ""), ("a-k", "2014-12-31", "10-K", ""),
                   ("a-ka", "2015-01-01", "10-K/A", "")])
    c, s = _client([_Resp(200, root), _Resp(200, older)])
    df = sec.company_submissions(c, "320193")
    assert s.calls[0][0] == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert s.calls[1][0].endswith("/submissions/CIK0000320193-submissions-001.json")
    assert len(s.calls) == 2  # the page entirely before 2015 is not fetched
    assert list(df.columns) == ["cik", "form", "filing_date", "items", "accession"]
    assert list(df["accession"]) == ["a-ka", "a-m", "a-q", "a-24"]
    assert set(df["cik"]) == {"0000320193"} and df["items"].iloc[-1] == "1.01,9.01"


_HEADER = """<HTML><HEAD><TITLE>SEC EDGAR Submission 0001193125-15-360674</TITLE>
<!--
<SEC-HEADER>0001193125-15-360674.hdr.sgml : 20151030
<TYPE>SC 13D
<SUBJECT-COMPANY>
<COMPANY-DATA>
<CONFORMED-NAME>NEOGENOMICS INC
<CIK>0001077183
</COMPANY-DATA>
</SUBJECT-COMPANY>
<FILED-BY>
<COMPANY-DATA>
<CONFORMED-NAME>GENERAL ELECTRIC CO
<CIK>0000040545
</COMPANY-DATA>
</FILED-BY>
</SEC-HEADER>
-->
</HEAD><BODY>
<PRE>ACCESSION NUMBER:\t\t0001193125-15-360674
CONFORMED SUBMISSION TYPE:\tSC 13D

SUBJECT COMPANY:\t

\tCOMPANY DATA:\t
\t\tCOMPANY CONFORMED NAME:\t\t\tNEOGENOMICS INC
\t\tCENTRAL INDEX KEY:\t\t\t0001077183

FILED BY:\t\t

\tCOMPANY DATA:\t
\t\tCOMPANY CONFORMED NAME:\t\t\tGENERAL ELECTRIC CO
\t\tCENTRAL INDEX KEY:\t\t\t0000040545
</PRE></BODY></HTML>"""


def test_parse_subject_cik():
    assert sec.parse_subject_cik(_HEADER) == "0001077183"
    no_subject = "<PRE>" + _HEADER.split("<PRE>")[1].replace("SUBJECT COMPANY", "XX")
    assert sec.parse_subject_cik(no_subject) is None
    sgml_only = _HEADER.split("<PRE>")[0]
    assert sec.parse_subject_cik(sgml_only) == "0001077183"


def test_filing_subject_tries_each_cik_until_found():
    c, s = _client([_TResp(404), _TResp(200, text=_HEADER)])
    adsh = "0001193125-15-360674"
    assert sec.filing_subject(c, ["0000040545", "0001077183"], adsh) == "0001077183"
    assert s.calls[0][0] == ("https://www.sec.gov/Archives/edgar/data/40545/000119312515360674/"
                             "0001193125-15-360674-index-headers.html")
    assert "/data/1077183/" in s.calls[1][0]
    c2, _ = _client([_TResp(404), _TResp(404)])
    assert sec.filing_subject(c2, ["1", "2"], adsh) is None
    c3, _ = _client([_TResp(503)] * sec.MAX_RETRIES)
    with pytest.raises(sec.SecError, match="gave up"):
        sec.filing_subject(c3, "1", adsh)


class _Assets:
    def __init__(self, tickers):
        self.tickers = tickers

    def read_assets(self, track=None):
        assert track == "B"
        return [SimpleNamespace(ticker=t) for t in self.tickers]


def _f13(adsh, form, d, ciks):
    return [adsh, form, d, json.dumps(ciks), "[]"]


def test_ingest_extra_maps_universe_filters_13ds_and_resumes(tmp_path):
    cfg = Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")
    st = SecStore(cfg.sec_db_path)
    st.init()
    st.write_tickers(pd.DataFrame([["0000000001", "AAA", "A"], ["0000000002", "BRK-B", "B"],
                                   ["0000000009", "AAA", "dup"], ["0000000003", "ZZZ", "Z"]],
                                  columns=["cik", "ticker", "title"]))
    f13 = pd.DataFrame([_f13("x1", "SC 13D", "2016-01-04", ["0000000001", "0000000777"]),
                        _f13("x2", "SC 13D/A", "2016-01-04", ["0000000001"]),      # amendment
                        _f13("x3", "SCHEDULE 13D", "2025-04-01", ["0000000002"]),  # hold-out
                        _f13("x4", "SCHEDULE 13D", "2025-03-31", ["0000000888", "0000000002"]),
                        _f13("x5", "SC 13D", "2020-01-02", ["0000000003"])],       # not universe
                       columns=["adsh", "form", "file_date", "ciks", "names"])
    st.write_13d("2016-01", f13, NOW)
    calls = {"sub": [], "subj": []}

    def subs(cik):
        calls["sub"].append(cik)
        return pd.DataFrame([[cik, "8-K", "2016-02-01", "1.01", f"acc-{cik}"]],
                            columns=sec.SUBMISSION_COLS)

    def subj(ciks, adsh):
        calls["subj"].append((tuple(ciks), adsh))
        return {"x1": "0000000001", "x4": "0000000002"}[adsh]

    out = []
    store = _Assets(["AAA", "BRK-B", "NOPE"])
    assert ps.ingest_sec_extra(cfg, st, store, subs, subj, lambda: NOW, out.append) == ps.OK
    assert calls["sub"] == ["0000000001", "0000000002"]  # AAA keeps the first listed CIK
    assert sorted(a for _, a in calls["subj"]) == ["x1", "x4"]
    assert "2/3" in out[0] and any("NOPE" in s for s in out)
    assert any("subject = first listed CIK in 1/2" in s for s in out)
    assert ps.ingest_sec_extra(cfg, st, store, subs, subj, lambda: NOW, out.append) == ps.OK
    assert len(calls["sub"]) == 2 and len(calls["subj"]) == 2  # nothing re-pulled
    assert len(st.read("company_filings")) == 2
    subj_rows = st.read("subject_13d")
    assert dict(zip(subj_rows["adsh"], subj_rows["subject_cik"], strict=True)) == {
        "x1": "0000000001", "x4": "0000000002"}
    assert ps.ingest_sec_extra(cfg, st, _Assets([]), subs, subj, lambda: NOW,
                               out.append) == ps.MISSING


def test_store_write_subject_none_and_submissions_replace(tmp_path):
    st = SecStore(tmp_path / "s.sqlite")
    st.init()
    st.write_subject("a", None, NOW)
    assert st.read("subject_13d")["subject_cik"].isna().all() and st.pulled("subject13d") == {"a"}
    row = pd.DataFrame([["c", "10-Q", "2016-01-01", "", "q1"]], columns=sec.SUBMISSION_COLS)
    st.write_submissions("c", row, NOW)
    st.write_submissions("c", row, NOW)
    assert len(st.read("company_filings")) == 1 and st.counts()["company_filings"] == 1
