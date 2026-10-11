"""ADR 0014 study end to end on synthetic SPY bars over the real announcement calendar: a planted
pre-FOMC drift is discovered and confirmed, the 08:30 gap cell is not; once-only."""

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.intraday_store import IntradayStore
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_macro as pm
from eventtrade.config import Config
from eventtrade.core import macro as mc
from eventtrade.core import macro_calendar as mcal
from eventtrade.core.intraday_study import N_BARS

NOW = datetime(2026, 10, 11, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    ist = IntradayStore(cfg.intraday_db_path)
    ist.init()
    holidays = pd.DatetimeIndex(pd.to_datetime(list(mcal.NYSE_HOLIDAYS)))
    days = pd.bdate_range("2016-01-04", "2025-03-31").difference(holidays)
    dlist = [d.date() for d in days]
    early = np.zeros(len(days), dtype=bool)
    early[mc.exact_positions(dlist, mcal.NYSE_EARLY_CLOSES)] = True
    fomc = np.setdiff1d(mc.exact_positions(dlist, mcal.FOMC_SCHEDULED),
                        mc.exact_positions(dlist, mcal.FOMC_CANCELLED))
    rng = np.random.default_rng(5)
    r = rng.normal(0, 0.0015, (len(days), N_BARS))
    r[fomc, :mc.K_1345 + 1] += 0.006 / (mc.K_1345 + 1)      # +0.6% before 13:45 on FOMC days
    px = 100 * np.exp(np.cumsum(r.ravel())).reshape(r.shape)
    gaps = rng.normal(0, 0.003, len(days))
    minutes = 570 + 15 * np.arange(N_BARS)
    rows = []
    for i, d in enumerate(days):
        nb = 14 if early[i] else N_BARS
        start = pd.DatetimeIndex([d + pd.Timedelta(minutes=int(m)) for m in minutes[:nb]]
                                 ).tz_localize("America/New_York").tz_convert("UTC")
        ts = start.as_unit("s").asi8.astype(np.int64)
        prev = px[i - 1, -1] if i else 100.0
        for k in range(nb):
            o = prev * (1 + gaps[i]) if k == 0 else px[i, k - 1]
            rows.append(("SPY", int(ts[k]), o, max(o, px[i, k]), min(o, px[i, k]), px[i, k],
                         1e6, px[i, k], 500))
        px[i, nb:] = px[i, nb - 1]
    bars = pd.DataFrame(rows, columns=["ticker", "ts", "open", "high", "low", "close", "volume",
                                       "vwap", "trade_count"])
    yr = pd.to_datetime(bars["ts"], unit="s", utc=True).dt.year
    for y, g in bars.groupby(yr):
        ist.write_bars_year("SPY", int(y), g, NOW)
    last = px[:, -1]
    store.upsert_prices(pd.DataFrame({"date": days, "ticker": "SPY", "open": last, "high": last,
                                      "low": last, "close": last, "volume": 1e6,
                                      "adj_open": last, "adj_close": last}), "yfinance")
    return cfg, store, ist


def test_pre_fomc_drift_found_gap_cell_not_and_once_only(world):
    cfg, store, ist = world
    out: list[str] = []
    rc = pm.macro_study(cfg, store, ist, lambda: NOW, out.append, n_boot=499, n_shift=50)
    assert rc == pm.OK, "\n".join(out)
    trials = [t for t in store.read_trials() if t.hypothesis == "M"]
    disc = {t.params["cell"]: json.loads(t.notes) for t in trials
            if t.params["period"] == "discovery"}
    assert set(disc) == set(mc.CELLS)
    assert disc["M1"]["discovered"] and disc["M1"]["mean"] > 0.004, "\n".join(out)
    assert not disc["M4"]["discovered"], json.dumps(disc["M4"])
    assert "dropped sessions []" in out[0], out[0]
    conf = {t.params["cell"]: json.loads(t.notes) for t in trials
            if t.params["period"] == "confirmation"}
    assert conf["M1"]["verdict"] == "CONFIRMED", "\n".join(out)
    out2: list[str] = []
    assert pm.macro_study(cfg, store, ist, lambda: NOW, out2.append) == pm.OK
    assert "already logged" in out2[0]
