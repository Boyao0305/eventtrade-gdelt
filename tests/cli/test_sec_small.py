"""ADR 0012 pipeline end to end on synthetic data: a planted +8% 60-day effect after insider buys
of SMA (listed, tradable) is found; a SPAC-named period, a sub-$5 period and a mismatching ticker
are excluded; delisted failures feed the survivorship stress; once-only."""

import json
import sqlite3
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.sec_store import SecStore
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_sec_small as pss
from eventtrade.config import Config

NOW = datetime(2026, 10, 10, tzinfo=UTC)
CAL = pd.bdate_range("2015-10-01", "2025-03-31")


def _px(tk, rng, planted=(), level=20.0):
    p, rows = level, []
    pset = set(planted)
    for i, d in enumerate(CAL):
        r = rng.normal(0, 0.01) + (0.0015 if any(0 < i - e <= 60 for e in pset) else 0.0)
        p *= 1 + r
        rows.append([d, tk, p, p, p, p, 5e6, p, p])
    return rows


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    small = SqliteStore(home / "data" / "smallcap.sqlite")
    small.init()
    rng = np.random.default_rng(2)
    ev_idx = list(range(80, len(CAL) - 80, 70))      # non-overlapping 60-day windows
    ev_idx2 = [i + 35 for i in ev_idx if i + 35 < len(CAL) - 80]
    sma = _px("SMA", rng, ev_idx)
    smc = _px("SMC", rng, ev_idx2)
    rows = _px("IWM", rng) + sma + smc + _px("BAD", rng, level=20.0)
    close_of = {("0000000001", r[0]): r[5] for r in sma} | {("0000000003", r[0]): r[5] for r in smc}
    small.upsert_prices(pd.DataFrame(rows, columns=["date", "ticker", "open", "high", "low",
                                                    "close", "volume", "adj_open", "adj_close"]),
                        "yfinance")
    with sqlite3.connect(small.path) as c:
        pd.DataFrame([["0000000001", "SMA", "Small A", "Nasdaq"],
                      ["0000000002", "BAD", "Bad B", "NYSE"],
                      ["0000000003", "SMC", "Small C", "Nasdaq"]],
                     columns=["cik", "ticker", "name", "exchange"]).to_sql("small_universe", c, index=False)
        pd.DataFrame([["0000000009"]], columns=["cik"]).to_sql("delisted", c, index=False)
        pd.DataFrame(columns=["ticker", "date", "ratio", "fetched_at"]).to_sql("splits", c, index=False)
    sst = SecStore(home / "data" / "sec.sqlite")
    sst.init()
    subs, owners, trans = [], [], []
    plan = ([(i, "0000000001") for i in ev_idx] + [(i, "0000000002") for i in ev_idx]
            + [(i, "0000000009") for i in ev_idx] + [(i, "0000000003") for i in ev_idx2])
    for k, (i, cik) in enumerate(plan):
        d = CAL[i].strftime("%d-%b-%Y").upper()
        price = {"0000000002": 400.0, "0000000009": 30.0}.get(cik) or round(close_of[(cik, CAL[i])], 2)
        acc = f"a{cik[-1]}-{k}"
        subs.append([acc, d, "4", cik, "X"])
        owners.append([acc, f"o{k % 2}", "N", "Director", "CEO"])
        trans.append([acc, d, "P", "1000", str(price), "A", "Common"])
    sst.write_insider("all", {
        "subs": pd.DataFrame(subs, columns=["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE",
                                            "ISSUERCIK", "ISSUERTRADINGSYMBOL"]),
        "owners": pd.DataFrame(owners, columns=["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME",
                                                "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"]),
        "trans": pd.DataFrame(trans, columns=["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE",
                                              "TRANS_SHARES", "TRANS_PRICEPERSHARE",
                                              "TRANS_ACQUIRED_DISP_CD", "SECURITY_TITLE"])}, NOW)
    for cik, extra in (("0000000001", []), ("0000000002", []), ("0000000003", []),
                       ("0000000009", [["0000000009", "8-K", "2019-01-01", "1.03", "z"]])):
        q = [[cik, "10-Q", d.strftime("%Y-%m-%d"), "", f"{cik}{d:%Y%m}"]
             for d in pd.date_range("2015-11-05", "2025-03-01", freq="91D")]
        q.append([cik, "8-A12B", "2010-01-01", "", f"{cik}8a"])
        names = "[]"
        if cik == "0000000001":  # former SPAC name until 2017-06-30
            names = json.dumps([{"name": "Old Acquisition Corp", "from": "2014-01-01",
                                 "to": "2017-06-30"}])
        sst.write_profile({"cik": cik, "name": cik, "sic": "3711", "former_names": names},
                          pd.DataFrame(q + extra, columns=["cik", "form", "filing_date", "items",
                                                           "accession"]), NOW)
    return cfg, store, small, sst, ev_idx


def test_planted_effect_found_with_exclusions_and_stress(world):
    cfg, store, small, sst, ev_idx = world
    out: list[str] = []
    assert pss.sec_small_study(cfg, store, small, sst, lambda: NOW, out.append, n_boot=99) == pss.OK, \
        "\n".join(out)
    text = "\n".join(out)
    assert "ticker check: 1 stocks dropped" in text  # BAD: Form 4 price 400 vs ~20
    trials = {t.params["cell"]: json.loads(t.notes) for t in store.read_trials()
              if t.hypothesis == "S2"}
    assert set(trials) == {"ins_all|h60", "ins_cxo|h60", "ins_cluster|h60"}
    a = trials["ins_all|h60"]
    # events before 2017-06-30 (SPAC name) are excluded
    n_sma_max = sum(1 for i in ev_idx if CAL[i] > pd.Timestamp("2017-06-30"))
    # the 250-day distress stratum needs 250 days of history: earlier events cannot be matched
    n_smc = sum(1 for i in ev_idx if i + 35 < len(CAL) - 80 and i + 35 >= 250)
    assert a["n_events"] == n_sma_max + n_smc  # SPAC-named SMA events excluded, BAD dropped
    # planted windows cover most of each stock's dates, so same-stock placebo dates are
    # contaminated by other events' drift: the 3-dim matched test is conservative here
    assert a["mean"] > 0.05 and a["t"] > 3 and a["p"] < 0.5 and a["p_2dim"] <= 0.05, a
    assert a["surv"]["delisted_events"] > 0 and a["surv"]["f_fail"] > 0
    assert a["stressed_mean"] < a["mean"]
    out2: list[str] = []
    assert pss.sec_small_study(cfg, store, small, sst, lambda: NOW, out2.append) == pss.OK
    assert "already logged" in out2[0]
