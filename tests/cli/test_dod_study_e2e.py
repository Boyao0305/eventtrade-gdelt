"""ADR 0015 study end to end on synthetic data: material awards followed by a drift are found
against the issuer's own days; moderate awards (no effect) are not; once-only."""

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.dod_store import DodStore
from eventtrade.adapters.sec_store import SecStore
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_dod_study as pds
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.schemas import Asset

NOW = datetime(2026, 10, 11, tzinfo=UTC)
CAL = pd.bdate_range("2015-03-02", "2025-03-31")
TICKERS = [f"T{i}" for i in range(20)]
CIK = {t: f"{i + 1:010d}" for i, t in enumerate(TICKERS)}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    store.upsert_assets([Asset(ticker=t, name=t, kind="stock", track="B", group="x",
                               screen_group="Industrials") for t in TICKERS[:10]])
    prices = SqliteStore(home / "data" / "dodcap.sqlite")
    prices.init()
    dstore = DodStore(cfg.dod_db_path, pd.Timestamp("2015-01-01").date(), cfg.holdout_start)
    dstore.init()
    sst = SecStore(cfg.sec_db_path)
    sst.init()
    rng = np.random.default_rng(9)
    n = len(CAL)
    awards: dict[int, list[dict]] = {}
    rows = []
    for k, t in enumerate(TICKERS):
        mat = list(range(60 + 3 * k, n - 30, 61))          # material award days
        mod = [d + 30 for d in mat if d + 30 < n - 30]      # moderate award days, no effect
        r = rng.normal(0, 0.012, n)
        for d in mat:
            r[d + 1:d + 6] += 0.006                          # +3% over the next 5 sessions
        close = 50 * np.exp(np.cumsum(r))
        opn = np.concatenate([[50.0], close[:-1]])
        for i in range(n):
            rows.append((CAL[i], t, opn[i], close[i], 1e6))
        for d, amt in [(d, "$10,000,000") for d in mat] + [(d, "$1,000,000") for d in mod]:
            awards.setdefault(d, []).append({"t": t, "text": f"{t} Corp., Somewhere, Virginia,"
                                                             f" is awarded a {amt} contract."})
        filed = pd.date_range("2014-12-15", "2025-03-01", freq="90D")
        sst.write_shares(CIK[t], pd.DataFrame({
            "cik": CIK[t], "tag": "EntityCommonStockSharesOutstanding",
            "end": (filed - pd.Timedelta(days=8)).strftime("%Y-%m-%d"), "val": 1e7,
            "filed": filed.strftime("%Y-%m-%d"), "form": "10-Q",
            "accn": [f"{t}-{j}" for j in range(len(filed))]}), NOW)
    for h in sorted(set(iu.SECTOR_SPDR.values()) | {"SPY", "IWM", "ITA"}):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, n)))
        o = np.concatenate([[100.0], c[:-1]])
        rows += [(CAL[i], h, o[i], c[i], 1e6) for i in range(n)]
    df = pd.DataFrame(rows, columns=["date", "ticker", "open", "close", "volume"])
    df["high"], df["low"], df["adj_open"], df["adj_close"] = df["close"], df["close"], \
        df["open"], df["close"]
    prices.upsert_prices(df[["date", "ticker", "open", "high", "low", "close", "volume",
                             "adj_open", "adj_close"]], "yfinance")
    maprows, snaps = [], {}
    for aid, (d, lst) in enumerate(sorted(awards.items()), start=1000):
        rd = CAL[d].date()
        dstore.write_article(aid, "x", "u", rd, "ok", [
            {"seq": s, "agency": "ARMY", "awardees": [a["t"] + " Corp."], "amount": None,
             "kind": "contract", "small_business": 0, "text": a["text"]}
            for s, a in enumerate(lst, start=1)], NOW)
        snaps[aid] = [((CAL[d] + pd.Timedelta(days=1)).strftime("%Y%m%d%H%M%S"), "u")]
        maprows += [(str(aid), str(s), rd.isoformat(), a["t"] + " Corp.", a["t"].lower(),
                     CIK[a["t"]], "title", a["t"]) for s, a in enumerate(lst, start=1)]
    dstore.write_snapshots(snaps)
    mp = pd.DataFrame(maprows, columns=["article_id", "seq", "release_date", "awardee", "norm",
                                        "cik", "rule", "ticker_now"])
    return cfg, store, prices, dstore, sst, mp


def test_material_drift_found_moderate_not_and_once_only(world):
    cfg, store, prices, dstore, sst, mp = world
    out: list[str] = []
    rc = pds.dod_study(cfg, store, prices, dstore, sst, mp, lambda: NOW, out.append, n_boot=99)
    assert rc == pds.OK, "\n".join(out)
    trials = [t for t in store.read_trials() if t.hypothesis == "D"]
    disc = {t.params["cell"]: json.loads(t.notes) for t in trials
            if t.params["period"] == "discovery"}
    assert len(disc) == 6, "\n".join(out)
    m5 = disc["material|h5"]
    assert m5["discovered"] and m5["event_avg"] > 0.02, ("\n".join(out), m5)
    assert not disc["moderate|h5"]["discovered"], "\n".join(out)
    conf = {t.params["cell"]: json.loads(t.notes) for t in trials
            if t.params["period"] == "confirmation"}
    assert conf["material|h5"]["verdict"] == "CONFIRMED", ("\n".join(out), conf)
    assert any("power discovery" in line for line in out)
    out2: list[str] = []
    assert pds.dod_study(cfg, store, prices, dstore, sst, mp, lambda: NOW, out2.append) == pds.OK
    assert "already logged" in out2[0]
