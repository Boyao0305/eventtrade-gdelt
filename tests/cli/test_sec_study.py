"""ADR 0011 study end to end on synthetic daily prices and SEC rows: planted +4% 5-day abnormal
returns after insider purchases of AAA are discovered (h5) and confirmed; other cells logged;
once-only; the audit reads no prices."""

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.sec_store import SecStore
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_sec_study as pss
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.schemas import Asset

NOW = datetime(2026, 10, 10, tzinfo=UTC)
STOCKS = {"AAA": ("0000000001", "Energy"), "BBB": ("0000000002", "Energy"),
          "CCC": ("0000000003", "Financials"), "DDD": ("0000000004", "Financials")}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    store.upsert_assets([Asset(ticker=t, name=t, kind="stock", track="B", group="x",
                               screen_group=g) for t, (_, g) in STOCKS.items()])
    cal = pd.bdate_range("2015-10-01", "2025-03-31")
    rng = np.random.default_rng(1)
    event_days = cal[60::15]  # an AAA purchase every 15 trading days
    ev_pos = set(cal.get_indexer(event_days))
    rows = []
    for tk in [*STOCKS, *sorted(set(iu.SECTOR_SPDR.values()) | {"SPY"})]:
        p = 100.0
        for i, d in enumerate(cal):
            r = rng.normal(0, 0.01)
            if tk == "AAA" and (i - 2) in ev_pos:  # rises between t+1 and t+1+h opens
                r += 0.04
            p *= 1 + r
            rows.append([d, tk, p, p, p, p, 1e6, p, p])
    px = pd.DataFrame(rows, columns=["date", "ticker", "open", "high", "low", "close", "volume",
                                     "adj_open", "adj_close"])
    store.upsert_prices(px, "yfinance")
    sst = SecStore(cfg.sec_db_path)
    sst.init()
    subs, owners, trans = [], [], []
    for k, d in enumerate(event_days):
        acc = f"acc{k}"
        fd = d.strftime("%d-%b-%Y").upper()
        subs.append([acc, fd, "4", "0000000001", "AAA"])
        owners.append([acc, f"o{k % 2}", "N", "Director", "Chief Executive Officer"])
        trans.append([acc, fd, "P", "1000", "50", "A", "Common"])
    sst.write_insider("all", {
        "subs": pd.DataFrame(subs, columns=["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE",
                                            "ISSUERCIK", "ISSUERTRADINGSYMBOL"]),
        "owners": pd.DataFrame(owners, columns=["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME",
                                                "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"]),
        "trans": pd.DataFrame(trans, columns=["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE",
                                              "TRANS_SHARES", "TRANS_PRICEPERSHARE",
                                              "TRANS_ACQUIRED_DISP_CD", "SECURITY_TITLE"])}, NOW)
    sst.write_tickers(pd.DataFrame([[c, t, t] for t, (c, _) in STOCKS.items()],
                                   columns=["cik", "ticker", "title"]))
    for t, (c, _) in STOCKS.items():
        q = [[c, "10-Q", d.strftime("%Y-%m-%d"), "", f"{t}{d:%Y%m}"]
             for d in pd.date_range("2015-11-05", "2025-03-01", freq="91D")]
        sst.write_submissions(c, pd.DataFrame(q, columns=["cik", "form", "filing_date", "items",
                                                          "accession"]), NOW)
    sst.write_13d("all", pd.DataFrame([["x1", "SC 13D", "2017-03-01", "[]", json.dumps(["n"])]],
                                      columns=["adsh", "form", "file_date", "ciks", "names"]), NOW)
    sst.write_subject("x1", "0000000002", NOW)
    return cfg, store, sst


def test_audit_reads_no_prices(world, monkeypatch):
    cfg, store, sst = world

    def boom(*a, **k):
        raise AssertionError("audit must not read prices")

    monkeypatch.setattr(store, "read_prices", boom, raising=False)
    monkeypatch.setattr(pss, "_calendar", lambda s, c: pd.bdate_range("2015-10-01", "2025-03-31"))
    out: list[str] = []
    assert pss.sec_audit(cfg, store, sst, out.append) == pss.OK
    assert any("BBB" in line for line in out)


def test_planted_insider_effect_found_and_once_only(world):
    cfg, store, sst = world
    out: list[str] = []
    assert pss.sec_study(cfg, store, sst, lambda: NOW, out.append, n_boot=99) == pss.OK, "\n".join(out)
    trials = [t for t in store.read_trials() if t.hypothesis == "S1"]
    disc = {t.params["cell"]: json.loads(t.notes) for t in trials if t.params["period"] == "discovery"}
    assert len(disc) == 12
    a5 = disc["ins_all|h5"]
    assert a5["discovered"] and a5["mean"] > 0.03 and a5["p"] <= 0.02, a5
    conf = {t.params["cell"]: json.loads(t.notes) for t in trials
            if t.params["period"] == "confirmation"}
    assert conf["ins_all|h5"]["verdict"] == "CONFIRMED", conf["ins_all|h5"]
    out2: list[str] = []
    assert pss.sec_study(cfg, store, sst, lambda: NOW, out2.append, n_boot=99) == pss.OK
    assert "already logged" in out2[0]


def test_cik_to_ticker_first_listed_wins():
    assets = [Asset(ticker=t, name=t, kind="stock", track="B", group="x") for t in ("GOOGL", "GOOG")]
    m = pss.cik_to_ticker(assets, pd.DataFrame([["1", "GOOGL"], ["1", "GOOG"]],
                                               columns=["cik", "ticker"]))
    assert m == {"0000000001": "GOOGL"}
    assert date(2025, 3, 31) == pss.CONFIRMATION[1]
