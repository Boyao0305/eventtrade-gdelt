"""ADR 0013 core: earnings days, 3-day reaction, no-look-ahead surprise z, events, big-move strata."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core import earnings as er

CAL = pd.bdate_range("2016-01-04", periods=400)


def test_earnings_days_item_202_and_dedup():
    cf = pd.DataFrame([["1", "8-K", "2016-03-05", "2.02,9.01", "a"],   # Saturday -> Monday
                       ["1", "8-K", "2016-03-08", "2.02", "b"],        # within 5 days: merged
                       ["1", "8-K/A", "2016-06-01", "2.02", "c"],      # amendment: ignored
                       ["1", "8-K", "2016-06-01", "5.02", "d"],        # not 2.02
                       ["2", "8-K", "2016-06-01", "2.02", "e"]],
                      columns=["cik", "form", "filing_date", "items", "accession"])
    e = er.earnings_days(cf, {"0000000001": "AAA", "0000000002": "BBB"}, CAL)
    assert e.values.tolist() == [["AAA", pd.Timestamp("2016-03-07")],
                                 ["BBB", pd.Timestamp("2016-06-01")]]


def test_hedged_3day_window():
    close = pd.DataFrame({"A": [10, 10, 11, 12, 12.0]}, index=CAL[:5])
    h = {"A": pd.Series([5, 5, 5, 5, 5.0], index=CAL[:5])}
    r = er.hedged_3day(close, h)
    assert r["A"].iloc[2] == pytest.approx(12 / 10 - 1)  # close(t+1)/close(t-1)


def test_surprise_z_uses_only_past_and_flags_events():
    rng = np.random.default_rng(0)
    p = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(CAL)))
    p[300:] *= 1.10                                   # +10% jump on day 300
    close = pd.DataFrame({"A": p}, index=CAL)
    h = {"A": pd.Series(100.0, index=CAL)}
    r3 = er.hedged_3day(close, h)
    earn = pd.DataFrame({"ticker": ["A", "A"], "d": [CAL[300], CAL[100]]})
    sz = er.surprise_z(r3, earn)
    assert len(sz) == 1 and sz["z"].iloc[0] > 4      # day 100 lacks 150 history values
    # perturbing prices after d+1 does not change z
    close2 = close.copy()
    close2.iloc[302:, 0] *= 3
    sz2 = er.surprise_z(er.hedged_3day(close2, h), earn)
    assert sz2["z"].iloc[0] == pytest.approx(sz["z"].iloc[0])
    ev = er.events(sz, CAL)
    assert ev["pos"].values.tolist() == [[CAL[301], "A"]] and ev["neg"].empty


def test_bigmove_strata_excludes_earnings_window():
    rng = np.random.default_rng(1)
    p = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(CAL)))
    p[250:] *= 1.12
    p[350:] *= 0.88
    close = pd.DataFrame({"A": p}, index=CAL)
    r3 = er.hedged_3day(close, {"A": pd.Series(100.0, index=CAL)})
    earn = pd.DataFrame({"ticker": ["A"], "d": [CAL[350]]})
    st = er.bigmove_strata(r3, earn, np.ones(len(CAL), dtype=bool))
    dates = set(st["date"])
    assert CAL[250] in dates or CAL[251] in dates     # the non-earnings jump is a placebo day
    assert not any(CAL[345] <= d <= CAL[356] for d in dates)  # earnings window excluded
    assert set(st[st["date"].isin([CAL[250], CAL[251]])]["stratum"]) == {"pos"}
