"""ADR 0008 search pipeline end to end on synthetic data: a planted quad4 effect on EWJ (dm) must be
discovered and confirmed; other cells are logged with all their configurations; once-only."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.intraday_store import (
    EVENTS_15MIN_CAT_COLUMNS,
    ORGS_15MIN_COLUMNS,
    IntradayStore,
)
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_intraday_search as ps
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_study as st
from eventtrade.schemas import Asset

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 9, tzinfo=UTC)
START, END = date(2015, 12, 1), date(2018, 12, 31)
DISC = (date(2016, 1, 4), date(2017, 6, 30))
CONF = (date(2017, 7, 3), date(2018, 12, 31))
CELLS = {"A|quad4|dm", "A|r14|em", "B|spike_any|stocks"}


def _weekdays():
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
          _asset("AAA", "B", "Energy", aliases=["aaa"]),
          _asset("BBB", "B", "Communication Services", aliases=["bbb"])]


def _plants(days):
    return {i: 4 + (i * 7) % 16 for i in range(25, len(days), 10)}


def _bars(t, days, plants, rng):
    rows = []
    for i, d in enumerate(days):
        price = 100.0
        drop = plants[i] + 2 if t == "EWJ" and i in plants else None
        for k in range(st.N_BARS):
            price *= 1 + rng.normal(0, 0.0005)
            c = price * (0.98 if drop is not None and k >= drop else 1.0)
            m = 570 + 15 * k
            ts = int(datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=NY).timestamp())
            o = price * (0.98 if drop is not None and k > drop else 1.0)
            rows.append([ts, t, o, max(o, c) * 1.0003, min(o, c) * 0.9997, c, 1000.0, c, 50])
    return pd.DataFrame(rows, columns=["ts", "ticker", "open", "high", "low", "close", "volume",
                                       "vwap", "trade_count"])


def _gdelt(days, plants):
    slots = st.tau_utc_slots(days)
    cat, org = [], []
    zeros = [0] * 9
    for i, d in enumerate(days):
        base = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
        hot = set()
        if i in plants:
            s = slots[i, plants[i]]
            hot = set(range(s - 5, s - 1))
        for sl in range(96):
            ts = base + sl * 900
            cat.append(["_GLOBAL", "action", ts, 1000, 100, 100, 100, 100, *zeros[:7], 5000.0, -5000.0])
            for c in ("JA", "GM", "CH", "BR"):
                q4 = 40 if (c == "JA" and sl in hot) else 4
                cat.append([c, "action", ts, 40, 4, 4, 4, q4, 2, 1, 1, 1, 1, 1, 1, 200.0, -200.0])
                cat.append([c, "actor", ts, 30, 3, 3, 3, 3, 1, 1, 1, 1, 1, 1, 1, 150.0, -150.0])
            if 48 <= sl < 80:
                org.append(["_GLOBAL", ts, 2000, -2000.0])
                org.append(["AAA", ts, 5, -5.0])
                org.append(["BBB", ts, 5, -5.0])
    return (pd.DataFrame(cat, columns=EVENTS_15MIN_CAT_COLUMNS),
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
    cat, org = _gdelt(days, plants)
    istore.write_gdelt_chunk("events_15min_cat", "events_15min_cat", (START, END), cat, 0, NOW)
    istore.write_gdelt_chunk("orgs_15min", "gkg_orgs_15min", (START, END), org, 0, NOW)
    return cfg, store, istore


def test_planted_quad4_found_confirmed_all_configs_logged_once_only(world):
    cfg, store, istore = world
    out: list[str] = []
    rc = ps.intraday_search(cfg, store, istore, lambda: NOW, out.append, n_boot=99,
                            discovery=DISC, confirmation=CONF, only_cells=CELLS)
    assert rc == ps.OK, "\n".join(out)
    trials = [t for t in store.read_trials() if t.hypothesis == "J"]
    disc = [t for t in trials if t.params["period"] == "discovery"]
    # 160 configs for one-sided cells, 320 (both signs) for the two-sided B cell
    assert len(disc) == 160 + 160 + 320, len(disc)
    sel = {t.params["cell"]: t for t in disc if t.params["selected"]}
    q4 = json.loads(sel["A|quad4|dm"].notes)
    assert q4["cell_discovered"] and q4["cell_p"] < 0.05 and q4["mean_net"] > 0.01, q4
    conf = [t for t in trials if t.params["period"] == "confirmation"]
    assert [t.params["cell"] for t in conf] == ["A|quad4|dm"], "\n".join(out)
    assert json.loads(conf[0].notes)["verdict"] == "CONFIRMED", conf[0].notes
    out2: list[str] = []
    assert ps.intraday_search(cfg, store, istore, lambda: NOW, out2.append, n_boot=99,
                              discovery=DISC, confirmation=CONF, only_cells=CELLS) == ps.OK
    assert "already logged" in out2[0]


def test_thirty_four_cells():
    cs = ps.cells()
    assert len(cs) == 34 and len({c.key for c in cs}) == 34
    assert {c.sign for c in cs if c.signal in ("attention", "spike_any")} == {0}
