"""ADR 0015 core: award rows, issuer days, shares as-of, market caps, float check, cells,
declustering, placebo expectation."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import dod_study as ds

MAP = pd.DataFrame({"article_id": ["1", "1", "1", "2", "2"], "seq": ["1", "2", "3", "1", "2"],
                    "cik": ["5", "5", None, "5", "6"],
                    "ticker_now": ["AAA", "AAA", None, "AAA", "BBB"],
                    "rule": ["title", "ex21", "none", "title", "title"]})
AWARDS = pd.DataFrame({
    "article_id": [1, 1, 1, 2, 2], "seq": [1, 2, 3, 1, 2],
    "release_date": ["2017-03-01", "2017-03-01", "2017-03-01", "2017-03-02", "2017-03-02"],
    "kind": ["contract", "modification", "contract", "correction", "contract"],
    "agency": ["NAVY", "ARMY", "NAVY", "NAVY", "ARMY"], "small_business": [0, 0, 0, 0, 1],
    "text": ["A Corp., is awarded a $10,000,000 contract.",
             "A Corp., is awarded a $5 million modification.",
             "X Inc., is awarded a $9,000,000 contract.",
             "CORRECTION: A Corp. $1,000,000,000.",
             "B Inc., is awarded a $2,000,000 contract to deobligate funds."]})


def test_award_rows_and_issuer_days():
    rows = ds.award_rows(MAP, AWARDS)
    assert rows["amount"].tolist() == [10e6, 5e6]           # unmapped, correction, de-obligation out
    assert rows["cik"].tolist() == ["0000000005"] * 2
    days = ds.issuer_days(rows)
    assert len(days) == 1 and days["size"].iloc[0] == 15e6 and days["n"].iloc[0] == 2
    assert days["kinds"].iloc[0] == "contract,modification"


SHARES = pd.DataFrame({
    "cik": ["0000000005"] * 5,
    "tag": [ds.DEI_SHARES, ds.DEI_SHARES, ds.DEI_SHARES, ds.FLOAT_TAG, ds.DEI_SHARES],
    "end": ["2016-10-20", "2016-10-20", "2017-04-20", "2016-11-30", "2015-01-20"],
    "val": [100.0, 50.0, 160.0, 1000.0, 90.0],
    "filed": ["2016-10-28", "2016-10-28", "2017-04-28", "2017-02-01", "2015-01-28"],
    "form": ["10-Q"] * 5, "accn": ["a", "a", "b", "c", "z"]})


def test_filing_shares_sum_classes_and_asof_rule():
    fs = ds.filing_shares(SHARES)
    assert dict(zip(fs["accn"], fs["shares"], strict=True)) == {"a": 150.0, "b": 160.0, "z": 90.0}
    days = pd.DataFrame({"d": pd.to_datetime(["2017-03-01", "2017-04-28", "2017-04-29",
                                              "2016-04-01"]), "cik": ["0000000005"] * 4})
    sa = ds.shares_asof(days, fs)
    assert sa["shares"].tolist()[:3] == [150.0, 150.0, 160.0]     # filed strictly before d
    assert np.isnan(sa["shares"].iloc[3])                          # 2015 filing > 400 days old


def test_market_cap_undoes_later_splits_and_float_check():
    idx = pd.bdate_range("2016-01-01", "2017-12-29")
    close = pd.DataFrame({"AAA": 10.0}, index=idx)                 # adjusted, today's basis
    splits = {"AAA": pd.DataFrame({"date": ["2017-01-10"], "ratio": [2.0]})}
    fs = ds.filing_shares(SHARES)
    days = pd.DataFrame({"d": pd.to_datetime(["2017-03-01", "2017-05-01"]),
                         "cik": ["0000000005"] * 2, "ticker": ["AAA", "AAA"]})
    mc = ds.market_caps(days, fs, close, splits)
    # cover 2016-10-20 (150 shares, before the 2-for-1) -> 300 today-basis shares x 10
    assert mc.tolist() == [3000.0, 1600.0]
    out = ds.float_outliers(SHARES, fs, close, splits, {"0000000005": "AAA"})
    assert out == {}                                               # 3000 / 1000 = 3x
    big = SHARES.assign(val=np.where(SHARES["tag"] == ds.FLOAT_TAG, 10.0, SHARES["val"]))
    assert ds.float_outliers(big, ds.filing_shares(big), close, splits,
                             {"0000000005": "AAA"}) == {"0000000005": pytest.approx(300.0)}


def test_cells_decluster_placebo_and_power():
    assert ds.cell_of(np.array([0.0005, 0.001, 0.0099, 0.01, 2.0])).tolist() == \
        ["", "moderate", "moderate", "material", "material"]
    keep = ds.decluster(np.array([10, 12, 16, 10, 30]), np.array(["A", "A", "A", "B", "A"]), 5)
    assert keep.tolist() == [True, False, True, True, True]
    ev = pd.DataFrame({"ticker": ["A", "A", "B"], "stratum": [2017, 2018, 2017]})
    pool = pd.DataFrame({"ticker": ["A", "A", "A", "B"], "stratum": [2017, 2017, 2018, 2017],
                         "value": [0.01, 0.03, -0.02, 0.0]})
    assert ds.placebo_expectation(ev, pool) == pytest.approx((0.02 - 0.02 + 0.0) / 3)
    assert ds.mde(1, 100) == pytest.approx(2.487 * 0.02 / 10)
    assert ds.underpowered(39, 20) and ds.underpowered(50, 14) and not ds.underpowered(40, 15)
