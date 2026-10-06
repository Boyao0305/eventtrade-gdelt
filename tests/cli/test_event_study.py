"""ADR 0005 event-study orchestration: fake stores (a real SqliteStore on a tmp file).

Short synthetic windows replace the ADR's 2015-2019 / 2020-2025Q1 split; the hold-out starts
2024-07-01 and the store holds price and GDELT rows after it that must never be read. The store
is built once per module and copied per test (building GDELT rows is the slow part).
"""

import dataclasses
import hashlib
import json
import math
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import main as cli
from eventtrade.cli import pipelines as pl
from eventtrade.config import Config
from eventtrade.core import event_signals as es
from eventtrade.core.features import FEATURES_VERSION
from eventtrade.schemas import GDELT_DAILY_COLUMNS, PRICE_COLUMNS, Asset

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
DATA = (date(2021, 1, 4), date(2024, 9, 30))  # runs into the hold-out on purpose
DISC = (date(2021, 7, 1), date(2023, 6, 30))
CONF = (date(2023, 7, 3), date(2024, 6, 28))
HOLDOUT = date(2024, 7, 1)
N_BOOT = 999  # the smallest n_boot the event study accepts
GROUPS_A = {"dm": ["DM1", "DM2", "DM3", "DM4", "DM5"], "em": ["EM1", "EM2", "EM3", "EM4", "EM5"],
            "commodity": ["CO1", "CO2", "CO3"]}
SECTORS_B = {"Energy": 4, "Utilities": 4}
FAMILY_METRICS = ["quad1", "quad2", "quad3", "quad4", "root_14_protest", "root_163_sanctions",
                  "root_18_assault", "root_19_fight", "root_20_massviolence"]
CELLS_A = [c for c in es.CELLS if c.track == "A"]
CELLS_B = [c for c in es.CELLS if c.track == "B"]
N_GRID = len(es.GRID)  # 32: the full ADR 0005 grid


def _silent(_: str) -> None:
    pass


def _bars(tickers, start, end, seed):
    days = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    rows = []
    for t in tickers:
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
        for d, p in zip(days, px, strict=True):
            rows.append((d, t, p, p * 1.01, p * 0.99, p, 1e6, p, p))
    return pd.DataFrame(rows, columns=PRICE_COLUMNS)


def _spiky(rng, size, base):
    """Counts with occasional large spikes, so that z >= 2..4 happens often enough."""
    x = rng.poisson(base, size).astype(float)
    return x * np.where(rng.random(size) < 0.04, rng.uniform(3, 8, size), 1.0)


def _assets_a(with_groups: bool = True) -> list[Asset]:
    out = [Asset(ticker="SPY", name="SPY", kind="etf", track="A", group="market",
                 fips_countries=["US"])]
    for g, tickers in GROUPS_A.items():
        out += [Asset(ticker=t, name=t, kind="etf", track="A", group="country",
                      fips_countries=[t[:1] + t[-1]], screen_group=g if with_groups else None)
                for t in tickers]
    return out


def _assets_b() -> list[Asset]:
    return [Asset(ticker=f"{s[:2].upper()}{i}", name=f"{s} {i}", kind="stock", track="B",
                  group="sp500", org_aliases=[f"{s.lower()} co {i}"], screen_group=s)
            for s, k in SECTORS_B.items() for i in range(k)]


def _fill(store: SqliteStore) -> None:
    a, b = _assets_a(), _assets_b()
    store.upsert_assets(a + b)
    store.upsert_prices(_bars([x.ticker for x in a + b], *DATA, seed=3), "yfinance")
    days = pd.date_range(*DATA, freq="D")
    rng = np.random.default_rng(4)
    parts = []
    for e in sorted({x.fips_countries[0] for x in a}):
        n = rng.integers(200, 300, len(days)).astype(float)
        parts.append(pd.DataFrame({"session_date": days, "source": "events", "entity": e,
                                   "metric": "n_events", "value_sum": n, "n": n.astype(int)}))
        for m in FAMILY_METRICS:
            parts.append(pd.DataFrame({"session_date": days, "source": "events", "entity": e,
                                       "metric": m, "value_sum": _spiky(rng, len(days), 8),
                                       "n": n.astype(int)}))
        arts = rng.integers(500, 900, len(days))
        tone = rng.normal(-2, 0.5, len(days)) - np.where(rng.random(len(days)) < 0.04, 5, 0)
        parts.append(pd.DataFrame({"session_date": days, "source": "events", "entity": e,
                                   "metric": "tone", "value_sum": tone * arts, "n": arts}))
    store.upsert_gdelt_daily(pd.concat(parts, ignore_index=True)[GDELT_DAILY_COLUMNS],
                             "events_daily")
    parts = []
    total = rng.integers(50_000, 60_000, len(days))
    for x in b:
        k = np.maximum(_spiky(rng, len(days), 10), 1.0)
        parts += [pd.DataFrame({"session_date": days, "source": "gkg", "entity": x.org_aliases[0],
                                "metric": "org_docs", "value_sum": k, "n": total}),
                  pd.DataFrame({"session_date": days, "source": "gkg", "entity": x.org_aliases[0],
                                "metric": "org_tone",
                                "value_sum": rng.normal(-1, 2, len(days)) * k,
                                "n": k.astype(int)})]
    store.upsert_gdelt_daily(pd.concat(parts, ignore_index=True)[GDELT_DAILY_COLUMNS],
                             "gkg_orgs_daily")


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    home = tmp_path_factory.mktemp("event_template")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    _fill(store)
    return cfg.db_path


@pytest.fixture
def ecfg(tmp_path: Path, monkeypatch, template: Path) -> Config:
    monkeypatch.setattr(pl, "EVENT_DISCOVERY", DISC)
    monkeypatch.setattr(pl, "EVENT_CONFIRMATION", CONF)
    cfg = Config(home=tmp_path, gcp_key_path=tmp_path / "k.json", start=DATA[0],
                 holdout_start=HOLDOUT)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(template, cfg.db_path)
    return cfg


@pytest.fixture
def store(ecfg: Config) -> SqliteStore:
    return SqliteStore(ecfg.db_path)


@pytest.fixture
def small_grid(monkeypatch) -> int:
    """A 4-configuration grid (theta 2, 3 x W 60 x h 1, 5) for orchestration tests: the full
    32-configuration grid is exercised once, in the logging test. Returns the grid size."""
    from itertools import product

    monkeypatch.setattr(es, "THETAS", (2.0, 3.0))
    monkeypatch.setattr(es, "WINDOWS", (60,))
    monkeypatch.setattr(es, "HORIZONS", (1, 5))
    monkeypatch.setattr(es, "GRID", tuple(product(es.THETAS, es.WINDOWS, es.HORIZONS)))
    return len(es.GRID)


def _force_p(monkeypatch, p_cell: dict[str, float]):
    """Run the real placebo-date max-t, then overwrite the cell p (orchestration tests only).
    Cells not named get p = 1."""
    real = pl._event_search

    def fake(cell_id, evals, sign, tdays, n_boot, seed):
        res = real(cell_id, evals, sign, tdays, n_boot, seed)
        if res is None:
            assert cell_id not in p_cell, f"{cell_id} has no sufficient configuration"
            return None
        return dataclasses.replace(res, p=p_cell.get(cell_id, 1.0))

    monkeypatch.setattr(pl, "_event_search", fake)


def _trials(store, track, period=None):
    return [t for t in store.read_trials(track, "signal") if t.hypothesis in pl.EVENT_HYPOTHESES
            and (period is None or t.params["period"] == period)]


def _members_hash(tickers) -> str:
    return hashlib.sha256(",".join(sorted(tickers)).encode()).hexdigest()[:16]


def _cfg_of(t) -> tuple[float, int, int]:
    return (t.params["theta"], t.params["window"], t.params["horizon"])


# --- refusals -------------------------------------------------------------------------------

def test_refuses_n_boot_below_999(ecfg, store):
    out: list[str] = []
    assert pl.event_study(ecfg, store, "A", lambda: NOW, out.append, n_boot=998) == pl.REFUSED
    assert "n_boot" in out[-1]
    assert store.read_trials() == []


def test_refuses_periods_reaching_the_holdout(ecfg, store, monkeypatch):
    monkeypatch.setattr(pl, "EVENT_CONFIRMATION", (CONF[0], HOLDOUT))
    assert pl.event_study(ecfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT) == pl.REFUSED
    assert store.read_trials() == []


def test_missing_without_screen_groups(ecfg, store):
    store.delete_assets([a.ticker for a in _assets_a()])
    store.upsert_assets(_assets_a(with_groups=False))
    out: list[str] = []
    assert pl.event_study(ecfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT) == pl.MISSING
    assert "load-universe" in out[-1]
    assert store.read_trials() == []


def test_cli_n_boot_below_999_is_a_usage_error(tmp_path: Path):
    cli.main(["--home", str(tmp_path), "init-db"])
    with pytest.raises(SystemExit) as e:
        cli.main(["--home", str(tmp_path), "event-study", "--track", "A", "--n-boot", "998"])
    assert e.value.code == 2


def test_cli_without_assets_is_missing(tmp_path: Path):
    cli.main(["--home", str(tmp_path), "init-db"])
    assert cli.main(["--home", str(tmp_path), "event-study", "--track", "B"]) == pl.MISSING


# --- discovery logging ----------------------------------------------------------------------

def test_logs_every_cell_x_configuration_and_waits_for_the_other_track(ecfg, store):
    # Real placebo-date max-t end to end. Track B has not been run, so BH across the 47 cells cannot run:
    # discovery is logged, nothing is confirmed.
    out: list[str] = []
    assert pl.event_study(ecfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT) == pl.OK
    trials = _trials(store, "A")
    assert len(trials) == len(CELLS_A) * N_GRID == 44 * 32
    assert {t.params["period"] for t in trials} == {"discovery"}
    assert {t.hypothesis for t in trials} == {"E1", "E1c", "E2", "E3"}
    assert all((t.data_start, t.data_end) == DISC for t in trials)
    assert any("event-study --track B" in line for line in out)
    by_cell: dict[str, list] = {}
    for t in trials:
        assert t.trial_id == pl.trial_id("signal", "A", t.params, *DISC)
        by_cell.setdefault(t.params["cell"], []).append(t)
    assert set(by_cell) == {c.id for c in CELLS_A}
    n_suff = n_insuff = 0
    for c in CELLS_A:
        ts = by_cell[c.id]
        assert sorted(_cfg_of(t) for t in ts) == sorted(es.GRID)
        notes = {_cfg_of(t): json.loads(t.notes) for t in ts}
        suff = {g: n for g, n in notes.items() if n["sufficient"]}
        n_suff += len(suff)
        n_insuff += len(notes) - len(suff)
        for n in notes.values():
            # ADR 0005 Amendment: >= 30 events AND >= 30 distinct event dates
            assert n["sufficient"] is (n["n_events"] >= 30 and n["n_event_days"] >= 30)
            assert (n["min_events"], n["min_event_days"]) == (30, 30)
            assert (n["note"] == "insufficient events") is (not n["sufficient"])
            # embargo: an event's outcome window ends by the last discovery day
            assert n["event_dates"][0] == DISC[0].isoformat()
            assert n["event_dates"][1] < DISC[1].isoformat()
        chosen = [g for g, n in notes.items() if n["selected"]]
        if not suff:
            assert chosen == [] and all(n["cell_p"] is None for n in notes.values())
            continue
        # the selected configuration has the largest directional t among the sufficient ones,
        # and can never be an insufficient one
        assert len(chosen) == 1 and chosen[0] in suff
        best = max(n["z"] for n in suff.values() if n["z"] is not None)
        assert notes[chosen[0]]["z"] == best
        p = {n["cell_p"] for n in notes.values()}
        assert len(p) == 1 and 0 < p.pop() <= 1
        search = notes[chosen[0]]["search"]
        assert search["test"] == "placebo_dates_max_t" and search["n_draws"] == N_BOOT
        assert search["n_sufficient"] == len(suff)
    assert n_suff > 0 and n_insuff > 0  # the fixture exercises both


def test_params_and_trial_identity(ecfg, store, monkeypatch, small_grid):
    _force_p(monkeypatch, {})
    pl.event_study(ecfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT)
    t = next(t for t in _trials(store, "A")
             if t.params["cell"] == "E1|tone|em" and _cfg_of(t) == (3.0, 60, 5))
    assert t.hypothesis == "E1"
    assert t.params == {"cell": "E1|tone|em", "theta": 3.0, "window": 60, "horizon": 5,
                        "period": "discovery", "features_version": FEATURES_VERSION,
                        "expected_sign": -1.0, "n_boot": N_BOOT, "null": "placebo_dates",
                        "seed": ecfg.seed, "members_hash": _members_hash(GROUPS_A["em"])}
    e2 = next(t for t in _trials(store, "A") if t.params["cell"] == "E2|violence|commodity")
    assert e2.params["expected_sign"] == 1.0
    assert e2.params["members_hash"] == _members_hash(GROUPS_A["commodity"])
    # any other bootstrap setting or membership is another trial id (a new, counted trial)
    for k, v in (("n_boot", N_BOOT + 1), ("null", "stationary_bootstrap"), ("seed", 1),
                 ("members_hash", "0" * 16)):
        assert pl.trial_id("signal", "A", {**t.params, k: v}, *DISC) != t.trial_id
    # ADR 0005 Amendment: never the id of a trial of the bootstrap design (mean_block, no null)
    old = {k: v for k, v in t.params.items() if k != "null"} | {"mean_block": 40.0}
    assert pl.trial_id("signal", "A", old, *DISC) != t.trial_id
    # a rerun with the same settings logs nothing new
    first = sorted(x.trial_id for x in store.read_trials("A"))
    out: list[str] = []
    pl.event_study(ecfg, store, "A", lambda: NOW + timedelta(days=1), out.append, n_boot=N_BOOT)
    assert sorted(x.trial_id for x in store.read_trials("A")) == first
    assert f"0 new trial(s) logged ({44 * small_grid} already logged)" in out[-1]


def test_discovery_p_is_the_placebo_max_t_over_sufficient_configurations(ecfg, store,
                                                                         monkeypatch, small_grid):
    """ADR 0005 Amendment: the cell p is eventstudy.max_t_placebo over the sufficient
    configurations, with their embargoed discovery windows, n_boot draws and the config seed."""
    calls = []
    current: list[str] = []
    real_search, real_max_t = pl._event_search, pl.eventstudy.max_t_placebo

    def spy_search(cell_id, evals, sign, tdays, n_boot, seed):
        current[:] = [cell_id]
        return real_search(cell_id, evals, sign, tdays, n_boot, seed)

    def spy(events, outcomes, horizons, windows, sign, tdays, n_draws, seed):
        res = real_max_t(events, outcomes, horizons, windows, sign, tdays, n_draws, seed)
        calls.append((current[0], events, horizons, windows, sign, n_draws, seed, res))
        return res

    monkeypatch.setattr(pl, "_event_search", spy_search)
    monkeypatch.setattr(pl.eventstudy, "max_t_placebo", spy)
    assert pl.event_study(ecfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT) == pl.OK
    assert calls
    notes = {(t.params["cell"], _cfg_of(t)): json.loads(t.notes)
             for t in _trials(store, "A", "discovery")}
    sign_of = {c.id: c.sign for c in CELLS_A}
    for cell, events, horizons, windows, sign, n_draws, seed, res in calls:
        assert (n_draws, seed, sign) == (N_BOOT, ecfg.seed, sign_of[cell])
        for g, ev in events.items():
            assert horizons[g] == g[2]
            assert windows[g][0] == DISC[0] and windows[g][1] < DISC[1]
            assert len(ev) >= 30 and ev["date"].nunique() >= 30
        # every sufficient configuration of the cell is in the search, no other
        assert set(events) == {g for (k, g), n in notes.items() if k == cell and n["sufficient"]}
        assert notes[(cell, res.best_config)]["selected"] is True
        assert notes[(cell, res.best_config)]["cell_p"] == pytest.approx(res.p)


def test_sufficiency_needs_30_distinct_event_dates():
    def ev(n_events, n_days):
        return pl._EventEval(series=pd.Series(dtype=float), events=pd.DataFrame(),
                             outcome=pd.DataFrame(), n_events=n_events, n_event_days=n_days,
                             n_days=0, n_no_outcome=0, mean=0.0, t=0.0, z=0.0,
                             event_dates=DISC)
    assert ev(30, 30).sufficient and ev(300, 30).sufficient
    assert not ev(300, 29).sufficient  # 300 events on 29 dates: clustered, insufficient
    assert not ev(29, 29).sufficient


# --- BH across both tracks, confirmation ----------------------------------------------------

def test_bh_across_47_cells_and_confirmation_of_discovered_cells_only(ecfg, store, monkeypatch,
                                                                      small_grid):
    found = {"E1|quad4|dm", "E3|violence|em"}
    _force_p(monkeypatch, dict.fromkeys(found, 1e-4))
    seen: dict = {}
    real_bh = pl.screen_stats.benjamini_hochberg

    def spy_bh(pvals, q):
        seen["bh"] = dict(pvals)
        return real_bh(pvals, q)

    monkeypatch.setattr(pl.screen_stats, "benjamini_hochberg", spy_bh)
    assert pl.event_study(ecfg, store, "B", lambda: NOW, _silent, n_boot=N_BOOT) == pl.OK
    assert "bh" not in seen  # track A not logged yet: B stops after discovery
    assert _trials(store, "B", "confirmation") == []
    periods = []
    real_eval = pl._event_eval

    def spy_eval(events, outcome, h, sign, period, tdays):
        periods.append(period)
        return real_eval(events, outcome, h, sign, period, tdays)

    monkeypatch.setattr(pl, "_event_eval", spy_eval)
    out: list[str] = []
    assert pl.event_study(ecfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT) == pl.OK
    assert len(seen["bh"]) == 47
    # a cell with no sufficient configuration keeps its place in m with p = NaN
    no_suff = {k for k, p in seen["bh"].items() if math.isnan(p)}
    for k in no_suff & {c.id for c in CELLS_A}:
        notes = [json.loads(t.notes) for t in _trials(store, "A", "discovery")
                 if t.params["cell"] == k]
        assert not any(n["sufficient"] for n in notes)
    assert periods.count(CONF) == len(found)
    conf = _trials(store, "A", "confirmation")
    assert {t.params["cell"] for t in conf} == found
    disc = {(t.params["cell"], _cfg_of(t)): t for t in _trials(store, "A", "discovery")}
    for t in conf:
        assert (t.data_start, t.data_end) == CONF
        note = json.loads(t.notes)
        d = disc[(t.params["cell"], _cfg_of(t))]
        assert json.loads(d.notes)["selected"] is True  # the selected configuration only
        assert note["discovery_trial"] == d.trial_id
        assert {k: t.params[k] for k in t.params if k != "period"} == {
            k: d.params[k] for k in d.params if k != "period"}
        assert note["holm_complete"] is True and isinstance(note["holm_reject"], bool)
        assert isinstance(note["confirmed"], bool)
        assert note["event_dates"][0] == CONF[0].isoformat()
        if t.params["cell"].startswith("E3"):  # volatility: no cost leg
            assert note["legs"] == 0 and note["net_mean"] == note["directional_mean"]
        else:  # abnormal return, ETF: 4 legs x 5 bp
            assert note["legs"] == 4 and note["cost_bps"] == 5.0
            assert note["net_mean"] == pytest.approx(note["directional_mean"] - 4 * 5e-4)
    assert any("2 discovered (2 in track A)" in line for line in out)


def test_holm_waits_for_the_other_tracks_confirmation(ecfg, store, monkeypatch, small_grid):
    found = {"E1|quad3|em", "E5|org_docs|all"}
    _force_p(monkeypatch, dict.fromkeys(found, 1e-4))
    pl.event_study(ecfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT)  # B missing: stop
    assert _trials(store, "A", "confirmation") == []
    out: list[str] = []
    pl.event_study(ecfg, store, "B", lambda: NOW, out.append, n_boot=N_BOOT)
    conf_b = _trials(store, "B", "confirmation")
    assert [t.params["cell"] for t in conf_b] == ["E5|org_docs|all"]
    note = json.loads(conf_b[0].notes)
    assert note["holm_complete"] is False and note["holm_reject"] is None
    assert note["confirmed"] is None and note["legs"] == 0
    assert any("Holm across discovered cells is pending" in line for line in out)
    assert note["conf_test"]["test"] == "placebo_dates" and note["conf_test"]["n_draws"] == N_BOOT
    assert pl.event_verdicts(store, N_BOOT, ecfg.seed) == {"E5|org_docs|all": None}
    n_disc_a = len(_trials(store, "A", "discovery"))
    pl.event_study(ecfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT)
    assert len(_trials(store, "A", "discovery")) == n_disc_a == 44 * small_grid  # not re-logged
    conf_a = _trials(store, "A", "confirmation")
    assert [t.params["cell"] for t in conf_a] == ["E1|quad3|em"]
    note = json.loads(conf_a[0].notes)
    assert note["holm_complete"] is True and isinstance(note["confirmed"], bool)
    # A completes Holm: its note records the final verdict of BOTH discovered cells, including
    # B's confirmation that was logged while Holm was pending (trials are append-only)
    hv = note["holm_verdicts"]
    assert set(hv) == {"E1|quad3|em", "E5|org_docs|all"}
    assert hv["E5|org_docs|all"]["trial_id"] == conf_b[0].trial_id
    assert hv["E1|quad3|em"]["trial_id"] == conf_a[0].trial_id
    assert hv["E1|quad3|em"]["confirmed"] == note["confirmed"]
    b_note = json.loads(conf_b[0].notes)
    assert hv["E5|org_docs|all"]["confirmed"] is pl._event_confirmed(
        hv["E5|org_docs|all"]["holm_reject"], b_note["directional_mean"], b_note["net_mean"])
    verdicts = pl.event_verdicts(store, N_BOOT, ecfg.seed)
    assert set(verdicts) == {"E1|quad3|em", "E5|org_docs|all"}
    assert all(isinstance(v, bool) for v in verdicts.values())  # A, B, A is enough
    # a 4th run (B again) logs nothing and changes nothing; the verdicts stay final
    out = []
    pl.event_study(ecfg, store, "B", lambda: NOW, out.append, n_boot=N_BOOT)
    assert "0 new trial(s) logged" in out[-1]
    assert pl.event_verdicts(store, N_BOOT, ecfg.seed) == verdicts
    # ... and the printed table agrees with the logged verdict of B's cell
    row = next(line for line in out if line.startswith("E5|org_docs|all"))
    assert row.rstrip().endswith("CONFIRMED" if verdicts["E5|org_docs|all"] else "no")


def test_never_reads_holdout(ecfg, monkeypatch, small_grid):
    inner = SqliteStore(ecfg.db_path)
    assert not inner.read_prices(None, HOLDOUT, None).empty  # hold-out rows exist

    class Recording:
        def __init__(self) -> None:
            self.reads: list[tuple[str, date | None, date | None]] = []

        def __getattr__(self, name):
            return getattr(inner, name)

        def read_prices(self, tickers, start, end):
            self.reads.append(("prices", start, end))
            return inner.read_prices(tickers, start, end)

        def read_gdelt_daily(self, source, start, end, entities=None):
            self.reads.append(("gdelt", start, end))
            return inner.read_gdelt_daily(source, start, end, entities)

    store = Recording()
    seen = []
    real = pl.eventstudy.calendar_time_series

    def spy(events, outcome, h, trading_days):
        seen.extend([events["date"].max(), outcome["date"].max()])
        return real(events, outcome, h, trading_days)

    monkeypatch.setattr(pl.eventstudy, "calendar_time_series", spy)
    _force_p(monkeypatch, {"E4|org_docs|all": 1e-4})
    # B with A's discovery missing still evaluates discovery; pre-log A so that B confirms too
    pl.event_study(ecfg, inner, "A", lambda: NOW, _silent, n_boot=N_BOOT)
    seen.clear()
    assert pl.event_study(ecfg, store, "B", lambda: NOW, _silent, n_boot=N_BOOT) == pl.OK
    assert _trials(inner, "B", "confirmation")
    assert store.reads and all(end is not None and end <= HOLDOUT for _, _, end in store.reads)
    assert max(d for d in seen if pd.notna(d)) < pd.Timestamp(CONF[1])
    assert all(t.data_end < HOLDOUT for t in inner.read_trials())


# --- pure helpers: case tables ----------------------------------------------------------------

def _scores(rows):
    return pd.DataFrame(rows, columns=["date", "ticker", "signal", "value"]).assign(
        date=lambda d: pd.to_datetime(d["date"]))


DAYS = [d.date() for d in pd.bdate_range("2024-01-01", periods=60)]

# (attention z by day index, tone z by day index) -> expected event day indices per cell.
# Declustering is on the attention signal; E4 / E4p then split the declustered events by the
# same-day tone z (tone undefined: in neither; tone 0: positive).
SPLIT_CASES = [
    ({0: 3.0, 5: 3.0, 25: 3.0}, {0: -1.0, 5: 1.0, 25: 0.5}, {"E5": [0, 25], "E4": [0], "E4p": [25]}),
    ({0: 3.0, 21: 3.0}, {0: 1.0, 21: -2.0}, {"E5": [0, 21], "E4": [21], "E4p": [0]}),
    ({0: 3.0, 20: 3.0}, {0: 1.0, 20: -2.0}, {"E5": [0], "E4": [], "E4p": [0]}),
    ({0: 3.0, 30: 1.9}, {0: 0.0, 30: -2.0}, {"E5": [0], "E4": [], "E4p": [0]}),
    ({0: 3.0, 30: 3.0}, {30: -2.0}, {"E5": [0, 30], "E4": [30], "E4p": []}),
]


@pytest.mark.parametrize("attention,tone,expected", SPLIT_CASES)
def test_cell_events_decluster_then_split(attention, tone, expected):
    rows = [(DAYS[i], "XX", "org_docs", v) for i, v in attention.items()]
    rows += [(DAYS[i], "XX", "org_tone", v) for i, v in tone.items()]
    scores = _scores(rows)
    cells = {c.hypothesis: c for c in CELLS_B}
    for hyp, days in expected.items():
        c = cells[hyp]
        base = es.cell_scores(scores, dataclasses.replace(c, split=None))
        ev = pl._cell_events(base, scores, c, 2.0, DAYS)
        assert [DAYS.index(d.date()) for d in ev["date"]] == days, hyp


HOLM_CASES = [
    # (this track's confirmation p, other track's discovered cells, its logged p) -> rejections
    ({"a": 0.01, "b": 0.04}, set(), {}, {"a", "b"}),  # 0.01 <= 0.05/2, then 0.04 <= 0.05/1
    ({"a": 0.03, "b": 0.04}, set(), {}, set()),  # 0.03 > 0.05/2 stops the step-down
    ({"a": 0.01}, {"x"}, {}, None),  # other track's confirmation not logged yet
    ({"a": 0.02}, {"x"}, {"x": 0.03}, {"a", "x"}),
    ({"a": 0.03}, {"x"}, {"x": float("nan")}, set()),  # NaN counts in m: 0.03 > 0.05/2
    ({}, {"x"}, {"x": 0.01, "y": 0.5}, {"x"}),  # undiscovered logged cells are ignored
]


@pytest.mark.parametrize("mine,other_disc,other_conf,expected", HOLM_CASES)
def test_event_holm(mine, other_disc, other_conf, expected):
    assert pl._event_holm(mine, other_disc, other_conf) == expected


CONFIRM_CASES = [
    (None, 0.01, 0.005, None),
    (True, 0.01, 0.005, True),
    (False, 0.01, 0.005, False),
    (True, 0.001, -0.001, False),  # positive gross, negative net of costs
    (True, -0.01, -0.012, False),  # wrong direction
    (True, 0.01, float("nan"), False),
]


@pytest.mark.parametrize("reject,directional,net,expected", CONFIRM_CASES)
def test_event_confirmed(reject, directional, net, expected):
    assert pl._event_confirmed(reject, directional, net) is expected


def test_legs():
    assert [pl._event_legs(o) for o in es.OUTCOMES] == [4, 2, 0]


def test_event_members():
    groups = {"A1": "dm", "A2": "dm", "B1": "em", "S1": "Energy", "S2": "Utilities"}
    cells = {c.id: c for c in es.CELLS}
    assert pl._event_members(cells["E1|quad4|dm"], groups) == ("A1", "A2")
    assert pl._event_members(cells["E5|org_docs|all"], groups) == tuple(sorted(groups))
