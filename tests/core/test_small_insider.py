"""ADR 0012 core: splits, tradability, spike screen, robust exits, identity, exits, costs, strata."""

import json

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import small_insider as si

IDX = pd.bdate_range("2016-01-04", periods=12)


def test_raw_close_undoes_forward_and_reverse_splits():
    close = pd.DataFrame({"A": [10.0] * 12, "B": [10.0] * 12}, index=IDX)
    splits = {"A": pd.DataFrame({"date": [IDX[5]], "ratio": [2.0]}),     # 2:1 forward
              "B": pd.DataFrame({"date": [IDX[8]], "ratio": [0.1]})}     # 1:10 reverse
    raw = si.raw_close(close, splits)
    assert raw["A"].iloc[4] == 20.0 and raw["A"].iloc[5] == 10.0
    assert raw["B"].iloc[7] == pytest.approx(1.0) and raw["B"].iloc[8] == 10.0
    mdv = pd.DataFrame(2e6, index=IDX, columns=["A", "B"])
    tr = si.tradable(raw, mdv)
    assert not tr["B"].iloc[7] and tr["B"].iloc[8]  # $1 raw before the reverse split


def test_clean_opens_spike_and_zero_volume():
    op = pd.DataFrame({"A": [10, 10, 50, 10.5, 10, 10, 10, 10, 10, 10, 10, 10.0]}, index=IDX)
    vol = pd.DataFrame({"A": [1] * 12}, index=IDX)
    vol.iloc[9, 0] = 0
    o, n = si.clean_opens(op, vol)
    assert n == 1 and np.isnan(o["A"].iloc[2]) and np.isnan(o["A"].iloc[9])
    # a genuine jump that does not revert is kept
    op2 = pd.DataFrame({"A": [10, 10, 50, 50, 51, 52, 50, 50, 50, 50, 50, 50.0]}, index=IDX)
    o2, n2 = si.clean_opens(op2, pd.DataFrame({"A": [1] * 12}, index=IDX))
    assert n2 == 0 and o2["A"].iloc[2] == 50


def test_hedged_outcomes_entry_and_delayed_exit():
    op = pd.DataFrame({"A": [10, 10, 11, np.nan, 12, 13, 14, 15, 16, 17, 18, 19.0]}, index=IDX)
    hedge = pd.Series([100.0] * 12, index=IDX)
    oc, delayed = si.hedged_outcomes(op, hedge, 2)
    v = oc.set_index("date")["value"]
    # t=0: entry idx1 (10); target idx3 is NaN -> first valid open idx4 (12)
    assert v[IDX[0]] == pytest.approx(12 / 10 - 1)
    assert v[IDX[1]] == pytest.approx(12 / 11 - 1)  # entry idx2 (11), exit idx4 (12)
    assert IDX[2] not in v.index  # entry at idx3 is NaN -> no event at t=2
    assert delayed >= 1


def test_ticker_mismatch_share():
    raw = pd.DataFrame({"A": [10.0] * 12, "B": [10.0] * 12}, index=IDX)
    trans = pd.DataFrame({"ticker": ["A", "A", "B", "B"], "trans_date": [IDX[2], IDX[3], IDX[2], IDX[3]],
                          "price": [10.5, 9.0, 50.0, 10.0]})
    m = si.ticker_mismatch(trans, raw)
    assert m["A"] == 0.0 and m["B"] == 0.5


def _filings(rows):
    return pd.DataFrame(rows, columns=["cik", "form", "filing_date", "items", "accession"])


def test_issuer_ok_rules():
    dates = pd.Series(pd.to_datetime(["2016-03-01", "2017-03-01", "2019-03-01"]))
    f = _filings([["1", "8-A12B", "2017-01-10", "", "a"], ["1", "10-Q", "2016-02-01", "", "b"]])
    assert list(si.issuer_ok({"sic": "3711"}, f, dates)) == [False, True, True]
    assert not si.issuer_ok({"sic": "6770"}, f, dates).any()
    f2 = _filings([["1", "10-Q", "2016-02-01", "", "b"], ["1", "10-K", "2019-01-15", "", "c"]])
    assert list(si.issuer_ok({"sic": "1"}, f2, dates)) == [True, False, True]  # 180-day rule
    meta = {"sic": "1", "former_names": json.dumps([{"name": "XYZ Acquisition Corp",
                                                    "to": "2018-06-01"}])}
    assert list(si.issuer_ok(meta, f, dates)) == [False, False, True]
    assert not si.issuer_ok({"sic": "1"}, _filings([["1", "8-K", "2016-01-01", "", "x"]]), dates).any()


def test_classify_exit():
    assert si.classify_exit(_filings([["1", "8-K", "2019-01-01", "1.03", "a"]])) == "failure"
    assert si.classify_exit(_filings([["1", "DEFM14A", "2019-01-01", "", "a"],
                                      ["1", "25-NSE", "2019-06-01", "", "b"]])) == "merger"
    assert si.classify_exit(_filings([["1", "15-12B", "2019-06-01", "", "b"]])) == "failure"
    assert si.classify_exit(_filings([["1", "10-K", "2019-06-01", "", "b"]])) == "other"


def test_costs_and_stress():
    rt = si.cost_round_trip(np.array([1e6, 1e7]))
    assert rt[0] == pytest.approx(0.0102) and rt[1] == pytest.approx(0.0052)
    assert si.cost_round_trip(np.array([1e7]), stress=True)[0] == pytest.approx(0.0152)
    assert si.stressed_mean(0.03, 0.1) == pytest.approx(0.9 * 0.03 - 0.03)


def test_strata3_dimensions():
    cal = pd.bdate_range("2016-01-04", periods=30)
    r = pd.DataFrame({"A": np.linspace(-1, 1, 30)}, index=cal)
    s3 = si.strata3(r, r, {}, cal, np.ones(30, dtype=bool))
    s2 = si.strata3(r, r, {}, cal, np.ones(30, dtype=bool), two_dim=True)
    assert s3["stratum"].str.count(r"\|").eq(2).all() and s2["stratum"].str.count(r"\|").eq(1).all()
