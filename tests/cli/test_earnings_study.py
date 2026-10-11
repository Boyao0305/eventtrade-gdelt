"""ADR 0013 study end to end on synthetic data: positive earnings surprises followed by drift are
found against the stock's own calm earnings; negative cells are not; once-only."""

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.sec_store import SecStore
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_earnings as pe
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.schemas import Asset

NOW = datetime(2026, 10, 11, tzinfo=UTC)
CAL = pd.bdate_range("2015-03-02", "2025-03-31")
STOCKS = {f"S{i}": (f"{i:010d}", "Energy") for i in range(1, 11)}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    store.upsert_assets([Asset(ticker=t, name=t, kind="stock", track="B", group="x",
                               screen_group=g) for t, (_, g) in STOCKS.items()])
    rng = np.random.default_rng(4)
    rows, filings = [], []
    for k, (tk, (cik, _)) in enumerate(STOCKS.items()):
        q_days = list(range(130 + 4 * k, len(CAL) - 70, 63))  # quarterly, staggered by stock
        p = 100.0
        surprise = {d for j, d in enumerate(q_days) if (j + k) % 3 == 0}
        for i, d in enumerate(CAL):
            r = rng.normal(0, 0.01)
            if i in surprise:
                r += 0.08                                  # positive surprise on d
            if any(1 < i - s <= 21 for s in surprise):
                r += 0.002                                 # drift d+2 .. d+21
            p *= 1 + r
            rows.append([d, tk, p, p, p, p, 1e6, p, p])
        for d in q_days:
            filings.append([cik, "8-K", CAL[d].strftime("%Y-%m-%d"), "2.02,9.01", f"{tk}{d}"])
    for tk in sorted(set(iu.SECTOR_SPDR.values()) | {"SPY"}):
        p = 100.0
        for d in CAL:
            p *= 1 + rng.normal(0, 0.005)
            rows.append([d, tk, p, p, p, p, 1e6, p, p])
    store.upsert_prices(pd.DataFrame(rows, columns=["date", "ticker", "open", "high", "low",
                                                    "close", "volume", "adj_open", "adj_close"]),
                        "yfinance")
    sst = SecStore(cfg.sec_db_path)
    sst.init()
    sst.write_tickers(pd.DataFrame([[c, t, t] for t, (c, _) in STOCKS.items()],
                                   columns=["cik", "ticker", "title"]))
    fdf = pd.DataFrame(filings, columns=["cik", "form", "filing_date", "items", "accession"])
    for cik, g in fdf.groupby("cik"):
        sst.write_submissions(cik, g, NOW)
    return cfg, store, sst


def test_positive_drift_found_negative_not_and_once_only(world):
    cfg, store, sst = world
    out: list[str] = []
    assert pe.earnings_study(cfg, store, sst, lambda: NOW, out.append, n_boot=99) == pe.OK, "\n".join(out)
    trials = [t for t in store.read_trials() if t.hypothesis == "P1"]
    disc = {t.params["cell"]: json.loads(t.notes) for t in trials if t.params["period"] == "discovery"}
    assert len(disc) == 6
    p20 = disc["pos|h20"]
    assert p20["discovered"] and p20["mean"] > 0.02, (p20, "\n".join(out))
    assert disc["neg|h20"]["n_events"] < 30   # no planted negative surprises
    conf = {t.params["cell"]: json.loads(t.notes) for t in trials
            if t.params["period"] == "confirmation"}
    assert conf["pos|h20"]["verdict"] == "CONFIRMED", (conf, "\n".join(out))
    out2: list[str] = []
    assert pe.earnings_study(cfg, store, sst, lambda: NOW, out2.append) == pe.OK
    assert "already logged" in out2[0]
