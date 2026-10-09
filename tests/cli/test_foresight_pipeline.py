"""ADR 0009 pipeline end to end: a price drop planted BEFORE the GDELT window of fresh EWJ quad4
events must show up as foresight value at the 30-minute lead (and not as post-window drift);
every trial is flagged look-ahead; once-only."""

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
import test_intraday_search_pipeline as w  # same synthetic world (pytest prepends this dir)

from eventtrade.adapters.intraday_store import IntradayStore
from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines_foresight as pf
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_study as st

NY = w.NY
NOW = datetime(2026, 10, 9, tzinfo=UTC)
SHIFT = 5  # EWG events are EWJ's planted days shifted by 5 trading days


def _bars_pre(t, days, plants, rng):
    """EWJ drops 2% inside [t_a - 30, t_a) of the event, nothing after.

    The hot batches are slots s-5..s-2 of the planted tau (index j); the first window that
    contains a hot batch is the one at tau - 45 (index j - 3), so the event fires there and
    t_a = tau - 105, i.e. the end of bar j - 7. The drop is at bar j - 8 (ends tau - 120)."""
    rows = []
    for i, d in enumerate(days):
        price = 100.0
        lo = None
        if t == "EWJ" and i in plants:
            lo = plants[i] - 8
        if t == "EWG" and (i - SHIFT) in plants:
            lo = plants[i - SHIFT] - 8
        for k in range(st.N_BARS):
            price *= 1 + rng.normal(0, 0.0005)
            c = price * (0.98 if lo is not None and k >= lo else 1.0)
            m = 570 + 15 * k
            ts = int(datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=NY).timestamp())
            rows.append([ts, t, c, c * 1.0003, c * 0.9997, c, 1000.0, c, 50])
    return pd.DataFrame(rows, columns=["ts", "ticker", "open", "high", "low", "close", "volume",
                                       "vwap", "trade_count"])


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    store.upsert_assets(w.ASSETS)
    istore = IntradayStore(cfg.intraday_db_path)
    istore.init()
    days = w._weekdays()
    plants = {i: 12 + (i * 7) % 9 for i in range(25, len(days), 10)}  # tau index 12..20
    rng = np.random.default_rng(5)
    for t in [*iu.HEDGES, *(a.ticker for a in w.ASSETS)]:
        b = _bars_pre(t, days, plants, rng)
        yrs = pd.to_datetime(b["ts"], unit="s", utc=True).dt.year
        for y in sorted(set(yrs)):
            istore.write_bars_year(t, int(y), b[yrs == y], NOW)
    cat, org = w._gdelt(days, plants)
    # plant the same quad4 spikes for Germany so two dm ETFs carry events (enough placebo
    # events survive the same-asset exclusion)
    ja = cat[(cat["entity"] == "JA") & (cat["geo"] == "action")]
    gm_idx = cat.index[(cat["entity"] == "GM") & (cat["geo"] == "action")]
    cat.loc[gm_idx, "q4"] = np.roll(ja["q4"].to_numpy(), SHIFT * 96)  # GM spikes 5 days later
    istore.write_gdelt_chunk("events_15min_cat", "events_15min_cat", (w.START, w.END), cat, 0, NOW)
    istore.write_gdelt_chunk("orgs_15min", "gkg_orgs_15min", (w.START, w.END), org, 0, NOW)
    return cfg, store, istore


def test_planted_pre_window_drop_is_foresight_value_and_once_only(world, monkeypatch):
    # With 99 draws the smallest p is 0.01, above BH's 0.1/168; relax the levels for the test
    # (the production run uses 2,000 draws: smallest p 1/2001 < 0.1/168).
    monkeypatch.setattr(pf, "BH_Q", 1.0)
    monkeypatch.setattr(pf, "HOLM_ALPHA", 1.0)
    cfg, store, istore = world
    out: list[str] = []
    rc = pf.foresight(cfg, store, istore, lambda: NOW, out.append, n_boot=99,
                      discovery=w.DISC, confirmation=w.CONF)
    assert rc == pf.OK, "\n".join(out)
    trials = [t for t in store.read_trials() if t.hypothesis in ("O", "R")]
    assert trials and all(t.params["lookahead"] for t in trials)
    disc = {t.params["cell"]: json.loads(t.notes) for t in trials
            if t.hypothesis == "O" and t.params["period"] == "discovery"}
    assert len(disc) == len(pf.pairs()) * len(pf.leads())
    n30 = disc["quad4|dm|30m"]
    assert n30["discovered"] and n30["mean_signed"] > 0.01 and n30["t"] > 5, n30
    assert n30["p"] == pytest.approx(0.01)  # no placebo draw reaches the observed t
    assert disc["quad4|dm|15m"]["mean_signed"] < 0.002  # the drop is 15-30 min before t_a
    # the drop happened before the window: no publication-segment move
    assert abs(n30["pub_mean"]) < 0.002
    conf = {t.params["cell"]: json.loads(t.notes) for t in trials
            if t.hypothesis == "O" and t.params["period"] == "confirmation"}
    assert conf["quad4|dm|30m"]["verdict"] == "CONFIRMED", "\n".join(out)
    rev = [t for t in trials if t.hypothesis == "R"]
    assert rev  # reverse study logged
    out2: list[str] = []
    assert pf.foresight(cfg, store, istore, lambda: NOW, out2.append, n_boot=99,
                        discovery=w.DISC, confirmation=w.CONF) == pf.OK
    assert "already logged" in out2[0]


def test_24_pairs_7_leads():
    assert len(pf.pairs()) == 24 and len(pf.leads()) == 7
