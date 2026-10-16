"""ADR 0018 point-in-time XBRL features: restatements, Q4 derivation, revenue tags, SUE, staleness,
filing-date cutoff."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.xbrl_features import FEATURE_COLS, fundamental_features

CIK = "0000000042"
EPS = "EarningsPerShareDiluted"
REV = "Revenues"
RFC = "RevenueFromContractWithCustomerExcludingAssessedTax"
NI = "NetIncomeLoss"
SH = "WeightedAverageNumberOfDilutedSharesOutstanding"
Q_START = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
Q_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
FILED = {1: "05-10", 2: "08-10", 3: "11-10"}


def fact(tag, start, end, val, filed, form="10-Q", cik=CIK, accn=None):
    return {"cik": cik, "tag": tag, "start": start, "end": end, "val": val, "fy": None,
            "fp": None, "form": form, "filed": filed, "accn": accn or f"a-{filed}"}


def qfact(tag, y, q, val, filed, form="10-Q", **kw):
    return fact(tag, f"{y}-{Q_START[q]}", f"{y}-{Q_END[q]}", val, filed, form, **kw)


def events(*ds, cik=CIK):
    d = pd.to_datetime(list(ds))
    return pd.DataFrame({"cik": cik, "d": d, "decision": d - pd.Timedelta(days=2)})


def run(rows, ev):
    return fundamental_features(pd.DataFrame(rows), ev)


def test_restatement_applies_only_once_filed_before_the_cutoff():
    rows = [qfact(EPS, 2019, 1, 1.0, "2019-05-10"),
            qfact(EPS, 2019, 1, 0.8, "2019-07-01", form="10-Q/A")]   # restated
    out = run(rows, events("2019-06-15", "2019-07-10"))
    assert out["eps_q"].tolist() == [1.0, 0.8]


def test_filed_on_or_after_the_decision_date_is_ignored():
    rows = [qfact(EPS, 2019, 1, 1.0, "2019-05-10"),
            qfact(EPS, 2019, 2, 2.0, "2019-07-08")]
    # d 2019-07-10: decision 07-08 = filed -> Q2 invisible; d 07-11: decision 07-09 -> visible
    out = run(rows, events("2019-07-10", "2019-07-11"))
    assert out["eps_q"].tolist() == [1.0, 2.0]
    assert out["q_end_age_days"].tolist() == [(pd.Timestamp("2019-07-10")
                                               - pd.Timestamp("2019-03-31")).days, 11]
    # forms other than 10-Q/10-K are ignored
    out2 = run([qfact(EPS, 2019, 1, 1.0, "2019-05-10", form="8-K")], events("2019-06-15"))
    assert out2["fund_missing"].iloc[0] == 1.0 and np.isnan(out2["eps_q"].iloc[0])


def _annual(tag, y, a, nine, k_filed, q_filed):
    return [fact(tag, f"{y}-01-01", f"{y}-12-31", a, k_filed, "10-K"),
            fact(tag, f"{y}-01-01", f"{y}-09-30", nine, q_filed)]


def test_q4_from_annual_minus_nine_months():
    rows = (_annual(REV, 2017, 900, 650, "2018-02-10", "2017-11-05")
            + _annual(REV, 2018, 1000, 700, "2019-02-10", "2018-11-05")
            + _annual(NI, 2017, 80, 60, "2018-02-10", "2017-11-05")
            + _annual(NI, 2018, 100, 70, "2019-02-10", "2018-11-05")
            + [qfact(SH, 2017, 4, 10, "2018-02-10", "10-K"),
               qfact(SH, 2018, 4, 10, "2019-02-10", "10-K")])
    out = run(rows, events("2019-02-20")).iloc[0]
    assert out["rev_growth"] == pytest.approx((300 - 250) / 250)
    assert out["eps_q"] == pytest.approx(3.0) and out["eps_q4"] == pytest.approx(2.0)
    assert out["eps_growth_raw"] == pytest.approx(1.0)
    assert out["q_end_age_days"] == 51 and out["fund_missing"] == 0.0
    # without Q4 diluted shares, Q4 EPS is missing (revenue still derived)
    out2 = run([r for r in rows if r["tag"] != SH], events("2019-02-20")).iloc[0]
    assert np.isnan(out2["eps_q"]) and np.isnan(out2["eps_growth_raw"])
    assert out2["rev_growth"] == pytest.approx(0.2)


def test_direct_q4_fact_wins_over_derivation():
    rows = (_annual(REV, 2018, 1000, 700, "2019-02-10", "2018-11-05")
            + [qfact(REV, 2018, 4, 310, "2019-02-10", "10-K"),
               qfact(REV, 2017, 4, 250, "2018-02-10", "10-K")])
    out = run(rows, events("2019-02-20")).iloc[0]
    assert out["rev_growth"] == pytest.approx(60 / 250)


def test_revenue_tag_switch_asc606():
    old = qfact(REV, 2017, 1, 100.0, "2017-05-05")
    new = qfact(RFC, 2018, 1, 120.0, "2018-05-04")
    comparative = qfact(RFC, 2017, 1, 105.0, "2018-05-04")
    ev = events("2018-06-01")
    # the 2018 10-Q's own comparative (latest filing for 2017-Q1) is used
    assert run([old, new, comparative], ev)["rev_growth"].iloc[0] == pytest.approx(15 / 105)
    # without a comparative, the 2017 value under the old tag is used
    assert run([old, new], ev)["rev_growth"].iloc[0] == pytest.approx(0.2)
    # within one filing, Revenues is preferred to RFC
    both = [qfact(REV, 2018, 1, 130.0, "2018-05-04"), new, old]
    assert run(both, ev)["rev_growth"].iloc[0] == pytest.approx(0.3)
    # clipped to [-1, 1]
    big = [old, qfact(RFC, 2018, 1, 500.0, "2018-05-04")]
    assert run(big, ev)["rev_growth"].iloc[0] == 1.0


def _eps_history(skip=()):
    rng = np.random.default_rng(0)
    vals, rows = {}, []
    for y in (2016, 2017, 2018):
        for q in (1, 2, 3, 4):
            vals[(y, q)] = float(np.round(rng.normal(1.0, 0.3), 2))
            if (y, q) in skip:
                continue
            filed = f"{y + 1}-02-10" if q == 4 else f"{y}-{FILED[q]}"
            rows.append(qfact(EPS, y, q, vals[(y, q)], filed, "10-K" if q == 4 else "10-Q"))
    return vals, rows


def _sue(vals, y, q, n=8):
    diffs = []
    t = y * 4 + q - 1
    for k in range(n):
        yy, qq = divmod(t - k, 4)
        a, b = vals.get((yy, qq + 1)), vals.get((yy - 1, qq + 1))
        if a is not None and b is not None:
            diffs.append(a - b)
    return diffs[0] / np.std(diffs, ddof=1), len(diffs)


def test_sue_needs_six_seasonal_differences():
    vals, rows = _eps_history()
    out = run(rows, events("2019-02-20")).iloc[0]
    exp, n = _sue(vals, 2018, 4)
    assert n == 8 and out["sue_q"] == pytest.approx(exp)
    assert out["sue_q1"] == pytest.approx(_sue(vals, 2018, 3)[0])      # 7 differences
    assert out["sue_q2"] == pytest.approx(_sue(vals, 2018, 2)[0])      # 6 differences
    assert np.isnan(out["sue_q3"])                                     # 5 differences
    # dropping 2016-Q1/Q2 leaves 6 differences for 2018-Q4; also dropping Q3 leaves 5
    vals6, rows6 = _eps_history(skip={(2016, 1), (2016, 2)})
    o6 = run(rows6, events("2019-02-20")).iloc[0]
    assert o6["sue_q"] == pytest.approx(_sue({k: v for k, v in vals6.items()
                                              if k not in {(2016, 1), (2016, 2)}}, 2018, 4)[0])
    _, rows5 = _eps_history(skip={(2016, 1), (2016, 2), (2016, 3)})
    assert np.isnan(run(rows5, events("2019-02-20"))["sue_q"].iloc[0])


def test_stale_last_quarter_sets_everything_missing():
    rows = [qfact(EPS, 2018, 3, 1.0, "2018-11-01"), qfact(EPS, 2017, 3, 0.5, "2017-11-01")]
    out = run(rows, events("2019-02-10", "2019-02-15"))     # 133 vs 138 days after 09-30
    assert out["eps_growth_raw"].iloc[0] == pytest.approx(0.5)
    assert out["fund_missing"].tolist() == [0.0, 1.0]
    assert out.iloc[1].drop("fund_missing").isna().all()


def test_order_index_and_unknown_cik_preserved():
    rows = [qfact(EPS, 2019, 1, 1.0, "2019-05-10"),
            qfact(EPS, 2019, 1, 7.0, "2019-05-10", cik="0000000007")]
    ev = pd.concat([events("2019-06-15", cik="7"), events("2019-06-15", cik="0000000099"),
                    events("2019-06-15")]).set_axis([10, 11, 12])
    out = run(rows, ev)
    assert list(out.columns) == FEATURE_COLS and out.index.tolist() == [10, 11, 12]
    assert out["eps_q"].iloc[0] == 7.0 and np.isnan(out["eps_q"].iloc[1])
    assert out["eps_q"].iloc[2] == 1.0
