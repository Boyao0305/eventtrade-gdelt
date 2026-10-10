"""ADR 0011 core: accession hygiene, joint filings, event types, 13D deal filter, matched placebo."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import sec_events as se

CAL = pd.bdate_range("2016-01-04", "2016-12-30")
MAP = {"0000000001": "AAA", "0000000002": "BBB"}


def _sub(acc, fdate, cik="0000000001", doc="4"):
    return [acc, fdate, doc, cik, "X"]


def _own(acc, owner, rel="Director", title=None):
    return [acc, owner, "N", rel, title]


def _tr(acc, tdate, shares=1000, price=20.0, code="P", ad="A"):
    return [acc, tdate, code, str(shares), str(price), ad, "Common"]


def _frames(subs, owners, trans):
    return (pd.DataFrame(subs, columns=["accession_number", "filing_date", "document_type",
                                        "issuercik", "issuertradingsymbol"]),
            pd.DataFrame(owners, columns=["accession_number", "rptownercik", "rptownername",
                                          "rptowner_relationship", "rptowner_title"]),
            pd.DataFrame(trans, columns=["accession_number", "trans_date", "trans_code",
                                         "trans_shares", "trans_pricepershare",
                                         "trans_acquired_disp_cd", "security_title"]))


def test_cxo_titles():
    assert se.is_cxo("Chief Executive Officer") and se.is_cxo("President & CEO")
    assert se.is_cxo("CFO") and not se.is_cxo("Vice President, Sales")
    assert not se.is_cxo("President, Consumer Division") and not se.is_cxo(None)


def test_accession_hygiene_and_joint_filings():
    subs, owners, trans = _frames(
        [_sub("a1", "10-MAR-2016"), _sub("a2", "11-MAR-2016"), _sub("a3", "30-MAR-2016"),
         _sub("a4", "10-MAR-2016", doc="4/A"), _sub("a5", "10-MAR-2016"), _sub("a6", "10-MAR-2016")],
        [_own("a1", "o1", "Director"), _own("a1", "fund", "TenPercentOwner"),
         _own("a2", "fund", "TenPercentOwner"), _own("a3", "o3", "Officer", "CEO"),
         _own("a4", "o4"), _own("a5", "o5"), _own("a6", "o6", "TenPercentOwner")],
        [_tr("a1", "08-MAR-2016"), _tr("a1", "09-MAR-2016", 500, 20.0),
         _tr("a2", "08-MAR-2016"),                       # same purchase as a1 row 1 -> counted once
         _tr("a3", "01-MAR-2016"),                       # filed 21 bdays later -> dropped
         _tr("a4", "08-MAR-2016"),                       # amendment -> dropped
         _tr("a5", "08-MAR-2016", price=0.0),            # zero price -> dropped
         _tr("a6", "09-MAR-2016", 77, 1.0)])             # 10% owner only -> not an insider
    acc = se.qualifying_accessions(subs, owners, trans, MAP)
    assert list(acc["accession"]) == ["a1"]
    assert acc["value"].iloc[0] == pytest.approx(1000 * 20 + 500 * 20)  # per accession, not owner
    assert acc["owner_key"].iloc[0] == "fund,o1"


def _acc(rows):
    df = pd.DataFrame(rows, columns=["accession", "ticker", "filing_date", "value", "cxo",
                                     "owner_key"])
    df["filing_date"] = pd.to_datetime(df["filing_date"])
    return df


def test_event_types_threshold_cxo_cluster_and_decluster():
    acc = _acc([["1", "AAA", "2016-03-05", 6000, False, "o1"],   # Saturday -> Monday 03-07
                ["2", "AAA", "2016-03-07", 6000, True, "o2"],    # 12k on 03-07 -> ins_all
                ["3", "AAA", "2016-03-15", 50000, False, "o3"],  # within 20 days -> declustered
                ["4", "AAA", "2016-03-20", 100, False, "o3"],    # same owner again
                ["5", "AAA", "2016-05-02", 20000, True, "o9"],
                ["6", "BBB", "2016-06-01", 1, False, "x1"], ["7", "BBB", "2016-06-02", 1, False, "x2"],
                ["8", "BBB", "2016-06-03", 1, False, "x3"], ["9", "BBB", "2016-06-06", 1, False, "x4"],
                ["10", "BBB", "2016-08-01", 1, False, "y1"], ["11", "BBB", "2016-08-02", 1, False, "y2"],
                ["12", "BBB", "2016-08-03", 1, False, "y3"]])
    ev = se.insider_events(acc, CAL)
    assert ev["ins_all"].values.tolist() == [[pd.Timestamp("2016-03-07"), "AAA"],
                                             [pd.Timestamp("2016-05-02"), "AAA"]]
    assert ev["ins_cxo"]["date"].tolist() == [pd.Timestamp("2016-05-02")]  # 6k cxo < 10k on 03-07
    cl = ev["ins_cluster"]
    # AAA: o1,o2,o3 within 30 days -> fires on 03-15; BBB fires 06-03 (third owner), re-arms
    # after the count drops below 3, fires again 08-03
    assert cl[cl.ticker == "AAA"]["date"].tolist() == [pd.Timestamp("2016-03-15")]
    assert cl[cl.ticker == "BBB"]["date"].tolist() == [pd.Timestamp("2016-06-03"),
                                                       pd.Timestamp("2016-08-03")]


def test_13d_subject_and_deal_filter():
    filings = pd.DataFrame([["x1", "SC 13D", "2016-04-01", "[]", "[]"],
                            ["x2", "SC 13D/A", "2016-04-01", "[]", "[]"],
                            ["x3", "SC 13D", "2016-05-02", "[]", "[]"],
                            ["x4", "SC 13D", "2016-06-01", "[]", "[]"]],
                           columns=["adsh", "form", "file_date", "ciks", "names"])
    subjects = pd.DataFrame([["x1", "1"], ["x2", "1"], ["x3", "2"], ["x4", "99"]],
                            columns=["adsh", "subject_cik"])
    cf = pd.DataFrame([["0000000002", "8-K", "2016-04-20", "1.01,9.01", "q"],
                       ["0000000001", "8-K", "2016-03-01", "2.02", "r"]],
                      columns=["cik", "form", "filing_date", "items", "accession"])
    ev, audit = se.events_13d(filings, subjects, cf, MAP, CAL)
    assert ev.values.tolist() == [[pd.Timestamp("2016-04-01"), "AAA"]]
    assert audit.set_index("adsh")["deal"].to_dict() == {"x1": False, "x3": True}


def test_hedged_open_returns():
    idx = CAL[:6]
    op = pd.DataFrame({"AAA": [10, 10, 11, 12, 12, 12.0]}, index=idx)
    hedge = {"AAA": pd.Series([5, 5, 5, 5.5, 5.5, 5.5], index=idx)}
    oc = se.hedged_open_returns(op, hedge, 2)
    v = oc.set_index("date")["value"]
    assert v[idx[0]] == pytest.approx((12 / 10 - 1) - (5.5 / 5 - 1))  # open t+3 / open t+1
    assert idx[4] not in v.index  # no t+1+h open


def _world(effect, seed):
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range("2016-01-04", periods=600)
    tick = [f"T{i}" for i in range(15)]
    rows, strat = [], []
    for tk in tick:
        trend = rng.normal(0, 0.002)
        for d in cal:
            rows.append((d, tk, rng.normal(trend, 0.02)))
            strat.append((d, tk, rng.choice(["a", "b", "c"])))
    oc = pd.DataFrame(rows, columns=["date", "ticker", "value"])
    st = pd.DataFrame(strat, columns=["date", "ticker", "stratum"])
    ev = oc.sample(120, random_state=seed)[["date", "ticker"]]
    oc = oc.merge(ev.assign(e=1), on=["date", "ticker"], how="left")
    oc.loc[oc["e"] == 1, "value"] += effect
    return ev, oc.drop(columns="e"), st, cal


def test_matched_placebo_calibration_and_power():
    ps = [se.matched_placebo(*_world(0.0, s)[:3], 5, 1, _world(0.0, s)[3], 99, s).p
          for s in range(20)]
    ps = np.array(ps)
    assert (ps < 0.05).mean() <= 0.2 and 0.2 < ps.mean() < 0.8
    ev, oc, st, cal = _world(0.02, 3)
    r = se.matched_placebo(ev, oc, st, 5, 1, cal, 99, 1)
    assert r.p <= 0.03 and r.mean > 0.01 and r.n_matched == r.n_events
