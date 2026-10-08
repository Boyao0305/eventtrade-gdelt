"""ADR 0007 study pipeline end to end on synthetic bars and 15-minute GDELT rows.

A planted effect (EWJ conflict-share spikes followed by a 2% drop vs EFA) must be discovered and
confirmed; cells without events are logged as insufficient; a rerun logs nothing; periods
reaching the hold-out are refused.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.intraday_store import (
    EVENTS_15MIN_COLUMNS,
    ORGS_15MIN_COLUMNS,
    IntradayStore,
)
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_intraday_study as ps
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_study as st
from eventtrade.schemas import Asset

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 7, tzinfo=UTC)
START, END = date(2015, 12, 1), date(2018, 12, 31)
DISC = (date(2016, 1, 4), date(2017, 6, 30))
CONF = (date(2017, 7, 3), date(2018, 12, 31))
PLANT_EVERY = 10


def _weekdays() -> list[date]:
    out, d = [], START
    while d <= END:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _asset(t, track, group, fips=(), aliases=()):
    return Asset(ticker=t, name=t, kind="etf" if track == "A" else "stock", track=track,
                 group="x", fips_countries=list(fips), org_aliases=list(aliases),
                 screen_group=group)


ASSETS = [_asset("EWJ", "A", "dm", ["JA"]), _asset("EWG", "A", "dm", ["GM"]),
          _asset("FXI", "A", "em", ["CH"]), _asset("EWZ", "A", "em", ["BR"]),
          _asset("USO", "A", "commodity", ["SA", "US"]),
          _asset("AAA", "B", "Energy", aliases=["aaa"]),
          _asset("BBB", "B", "Communication Services", aliases=["bbb"])]


def _plants(days: list[date]) -> dict[int, int]:
    """day position -> j_tau of the planted EWJ spike (every 10th day, rotating tau)."""
    return {i: (i * 7) % st.N_TAU for i in range(25, len(days), PLANT_EVERY)}


def _bars(ticker: str, days: list[date], plants: dict[int, int], rng) -> pd.DataFrame:
    rows = []
    for i, d in enumerate(days):
        price = 100.0
        drop_from = plants[i] + 1 if ticker == "EWJ" and i in plants else None
        for k in range(st.N_BARS):
            price *= 1 + rng.normal(0, 0.0005)
            c = price * (0.98 if drop_from is not None and k >= drop_from else 1.0)
            m = 570 + 15 * k
            ts = int(datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=NY).timestamp())
            rows.append([ts, ticker, c, c * 1.0003, c * 0.9997, c, 1000.0, c, 50])
    return pd.DataFrame(rows, columns=["ts", "ticker", "open", "high", "low", "close", "volume",
                                       "vwap", "trade_count"])


def _gdelt(days: list[date], plants: dict[int, int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    slots = st.tau_utc_slots(days)
    ev, org = [], []
    for i, d in enumerate(days):
        base = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
        hot = set()
        if i in plants:
            s = slots[i, plants[i]]
            hot = set(range(s - 5, s - 1))
        for sl in range(48, 80):
            ts = base + sl * 900
            ev.append(["_GLOBAL", ts, 1000, 100, 5000.0, -5000.0])
            for c in ("JA", "GM", "CH", "BR", "SA", "US"):
                conflict = 40 if (c == "JA" and sl in hot) else 4
                ev.append([c, ts, 40, conflict, 200.0, -200.0])
            org.append(["_GLOBAL", ts, 2000, -2000.0])
            org.append(["AAA", ts, 5, -5.0])
            org.append(["BBB", ts, 5, -5.0])
    return (pd.DataFrame(ev, columns=EVENTS_15MIN_COLUMNS),
            pd.DataFrame(org, columns=ORGS_15MIN_COLUMNS))


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    store.upsert_assets(ASSETS)
    istore = IntradayStore(cfg.intraday_db_path)
    istore.init()
    days = _weekdays()
    plants = _plants(days)
    rng = np.random.default_rng(5)
    for t in [*iu.HEDGES, *(a.ticker for a in ASSETS)]:
        b = _bars(t, days, plants, rng)
        yrs = pd.to_datetime(b["ts"], unit="s", utc=True).dt.year
        for y in sorted(set(yrs)):
            istore.write_bars_year(t, int(y), b[yrs == y], NOW)
    ev, org = _gdelt(days, plants)
    istore.write_gdelt_chunk("events_15min", "events_15min", (START, END), ev, 0, NOW)
    istore.write_gdelt_chunk("orgs_15min", "gkg_orgs_15min", (START, END), org, 0, NOW)
    return cfg, store, istore


def test_planted_effect_is_discovered_and_confirmed_then_once_only(world):
    cfg, store, istore = world
    out: list[str] = []
    rc = ps.intraday_study(cfg, store, istore, lambda: NOW, out.append, n_boot=199,
                           discovery=DISC, confirmation=CONF)
    assert rc == ps.OK, "\n".join(out)
    trials = [t for t in store.read_trials() if t.hypothesis in ps.HYPOTHESES]
    disc = {t.params["cell"]: t for t in trials if t.params["period"] == "discovery"}
    conf = {t.params["cell"]: t for t in trials if t.params["period"] == "confirmation"}
    assert len(disc) == 18
    import json
    planted = {"A|I1|conflict|dm|h60", "A|I1|conflict|dm|hC"}
    for k in planted:
        n = json.loads(disc[k].notes)
        assert n["discovered"] and n["mean"] < -0.015 and n["n_events"] >= 30, (k, n)
    assert set(conf) == planted, "\n".join(out)
    for k in planted:
        n = json.loads(conf[k].notes)
        assert n["verdict"] == "CONFIRMED", (k, n)
    # cells without events are insufficient (p NaN) and not discovered
    n = json.loads(disc["A|I2|tone|dm|h60"].notes)
    assert n["p"] is None and not n["discovered"] and n["n_events"] == 0
    # once-only: rerun computes and logs nothing
    out2: list[str] = []
    assert ps.intraday_study(cfg, store, istore, lambda: NOW, out2.append, n_boot=199,
                             discovery=DISC, confirmation=CONF) == ps.OK
    assert "already logged" in out2[0]
    assert len([t for t in store.read_trials() if t.hypothesis in ps.HYPOTHESES]) == len(trials)


def test_refuses_periods_in_holdout(tmp_path):
    cfg = Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")
    rc = ps.intraday_study(cfg, None, None, lambda: NOW, lambda s: None,
                           confirmation=(date(2020, 1, 2), date(2025, 4, 1)))
    assert rc == ps.REFUSED


def test_eighteen_cells_with_registered_signs():
    cs = ps.cells()
    assert len(cs) == 18 and len({c.key for c in cs}) == 18
    signs = {(c.hyp, c.sign) for c in cs}
    assert signs == {("I1", -1), ("I2", -1), ("I3", 0), ("I4", 1), ("I5", -1), ("I6", 1)}
