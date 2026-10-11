"""DoD store (resume ledger, hold-out filter) and Wayback client rules (rate, retries, gzip)."""

import gzip
from datetime import UTC, date, datetime

import pytest
import requests

from eventtrade.adapters import dod
from eventtrade.adapters.dod_store import DodStore

NOW = datetime(2026, 10, 11, tzinfo=UTC)
AW = [{"seq": 0, "agency": "NAVY", "awardees": ["A Inc.", "B Corp."], "amount": 5e6,
       "kind": "contract", "small_business": True, "text": "A Inc., ... B Corp., ... are awarded"}]


@pytest.fixture
def st(tmp_path):
    s = DodStore(tmp_path / "dod.sqlite", date(2015, 1, 1), date(2025, 4, 1))
    s.init()
    return s


def test_snapshots_round_trip_in_rank_order(st):
    snaps = {7: [("2022", "https://x/Article/7/"), ("2019", "https://x/Article/7//")],
             3: [("2020", "https://x/Article/3/")]}
    assert st.write_snapshots(snaps) == 2
    assert st.read_snapshots() == {3: snaps[3], 7: snaps[7]}
    st.write_snapshots({3: snaps[3]})  # a relist replaces the cache
    assert st.read_snapshots() == {3: snaps[3]}


def test_awards_written_and_final_statuses_are_skipped(st):
    assert st.write_article(1, "2022", "u1", date(2016, 5, 2), "ok", AW, NOW) == "ok"
    st.write_article(2, "2022", "u2", None, "fetch_failed", [], NOW)
    st.write_article(3, "2022", "u3", date(2016, 5, 3), "no_awards", [], NOW)
    st.write_article(4, "2022", "u4", date(2016, 5, 4), "not_contracts", [], NOW)
    assert st.done() == {1, 4}  # failures and empty parses are retried by a rerun
    [row] = st.awards()
    assert row["awardees"] == ["A Inc.", "B Corp."] and row["small_business"] == 1
    assert row["release_date"] == "2016-05-02" and row["amount"] == 5e6
    # a rerun replaces an article's awards instead of duplicating them
    st.write_article(1, "2023", "u1", date(2016, 5, 2), "ok", AW + [{**AW[0], "seq": 1}], NOW)
    assert len(st.awards()) == 2
    assert st.counts() == {"dod_snapshots": 0, "dod_articles": 4, "dod_awards": 2}


@pytest.mark.parametrize("rd", [date(2025, 4, 1), date(2026, 1, 5), date(2014, 12, 31), None])
def test_holdout_and_pre_2015_awards_are_never_stored(st, rd):
    status = st.write_article(9, "2025", "u9", rd, "ok", AW, NOW)
    assert status == "out_of_range"
    assert st.awards() == []
    [art] = st.articles()
    assert art["status"] == "out_of_range" and art["n_awards"] == 0


def test_range_edges(st):
    assert st.in_range(date(2015, 1, 1)) and st.in_range(date(2025, 3, 31))
    assert not st.in_range(date(2025, 4, 1)) and not st.in_range(None)


class _Resp:
    def __init__(self, status, content=b"", text=""):
        self.status_code, self.content, self.text = status, content, text


class _Sess:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), headers))
        r = self.resps.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(resps):
    sleeps: list[float] = []
    s = _Sess(resps)
    return dod.WaybackClient(session=s, sleep=sleeps.append, clock=lambda: 0.0), s, sleeps


def test_client_rate_limit_retries_and_no_contact_data():
    c, s, sleeps = _client([_Resp(429), requests.Timeout("t"), _Resp(503), _Resp(200, b"ok")])
    assert c.get("u").content == b"ok" and len(s.calls) == 4
    ua = s.calls[0][2]["User-Agent"]
    assert ua == "eventtrade-research/0.1" and "@" not in ua
    assert [x for x in sleeps if x >= 5] == [5.0, 10.0, 20.0]  # backoff
    assert sleeps.count(1.0) == 3  # >= 1 s between requests (the clock is frozen)
    c2, _, _ = _client([_Resp(404)])
    with pytest.raises(dod.DodError, match="404"):
        c2.get("u")
    c3, _, _ = _client([_Resp(500)] * dod.MAX_RETRIES)
    with pytest.raises(dod.DodError, match="gave up"):
        c3.get("u")


def test_article_ids_and_listing():
    assert dod.article_id("https://www.defense.gov/News/Contracts/Contract/Article/1000857/") \
        == 1000857
    assert dod.article_id("https://www.defense.gov/News/Contracts/Contract/Article/1611035/dei08/") \
        == 1611035
    assert dod.article_id("https://www.defense.gov/News/Contracts/Contract/Article/4218062//?ref=x") \
        == 4218062
    assert dod.article_id("http://www.defense.gov/News/Contracts/Contract/Article/?2399096=x") is None
    assert dod.article_id("https://www.defense.gov/News/Contracts/Contract/Article/") is None
    page = ("20250101000000 https://www.defense.gov/News/Contracts/Contract/Article/\n"
            "20240714134858 https://www.defense.gov/News/Contracts/Contract/Article/5/source/GovDelivery/\n"
            "20220107095731 https://www.defense.gov/News/Contracts/Contract/Article/5/\n"
            "20200101000000 http://www.defense.gov/News/Contracts/Contract/Article/5?x=1\n"
            "20230101000000 https://www.defense.gov/News/Contracts/Contract/Article/6//\n")
    c, s, _ = _client([_Resp(200, text="1\n"), _Resp(200, text=page)])
    snaps = dod.list_snapshots(c, prefixes=(dod.PREFIXES[0],))
    assert list(snaps) == [5, 6]
    assert snaps[5][0] == ("20220107095731",
                           "https://www.defense.gov/News/Contracts/Contract/Article/5/")
    assert snaps[5][-1][1].endswith("5?x=1")  # query strings last
    assert s.calls[0][1]["showNumPages"] == "true"
    assert s.calls[1][1] == {"url": dod.PREFIXES[0], "matchType": "prefix",
                                  "filter": "statuscode:200", "collapse": "urlkey",
                                  "fl": "timestamp,original", "page": 0}


def test_fetch_uses_id_flag_and_inflates_gzip():
    html = "<html>Contracts For Jan. 2, 2015</html>"
    c, s, _ = _client([_Resp(200, gzip.compress(html.encode()))])
    assert dod.fetch_article(c, "20220107095731", "https://x/Article/5/") == html
    assert s.calls[0][0] == "https://web.archive.org/web/20220107095731id_/https://x/Article/5/"
