"""ADR 0006 follow-up F1 orchestration: fake stores (a real SqliteStore on a tmp file).

A short synthetic window replaces the ADR's 2020-01-01..2025-03-31; the hold-out starts
2024-07-01 and the store holds price and GDELT rows after it that must never be read. The store
is built once per module and copied per test.
"""

import hashlib
import inspect
import json
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
from eventtrade.core.features import FEATURES_VERSION
from eventtrade.schemas import GDELT_DAILY_COLUMNS, PRICE_COLUMNS, Asset

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
DATA = (date(2021, 1, 4), date(2024, 9, 30))  # runs into the hold-out on purpose
WINDOW = (date(2022, 7, 1), date(2024, 6, 28))
SHORT = (date(2024, 2, 1), date(2024, 5, 31))  # too few events: inconclusive
HOLDOUT = date(2024, 7, 1)
N_BOOT = 999
EM = ["EM1", "EM2", "EM3", "EM4", "EM5"]
DM = ["DM1", "DM2", "DM3"]
FAMILY_METRICS = ["quad1", "quad2", "quad3", "quad4", "root_14_protest", "root_163_sanctions",
                  "root_18_assault", "root_19_fight", "root_20_massviolence"]


def _silent(_: str) -> None:
    pass


def _bars(tickers, start, end, seed):
    days = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    rows = []
    for t in tickers:
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
        op = px * np.exp(rng.normal(0, 0.004, len(days)))
        for d, p, o in zip(days, px, op, strict=True):
            rows.append((d, t, o, p * 1.01, p * 0.99, p, 1e6, p, o))
    return pd.DataFrame(rows, columns=PRICE_COLUMNS)


def _spiky(rng, size, base):
    x = rng.poisson(base, size).astype(float)
    return x * np.where(rng.random(size) < 0.03, rng.uniform(3, 8, size), 1.0)


def _assets(em_group: bool = True) -> list[Asset]:
    out = [Asset(ticker="SPY", name="SPY", kind="etf", track="A", group="market",
                 fips_countries=["US"])]
    out += [Asset(ticker=t, name=t, kind="etf", track="A", group="country",
                  fips_countries=[t[0] + t[-1]], screen_group="em" if em_group else "dm")
            for t in EM]
    out += [Asset(ticker=t, name=t, kind="etf", track="A", group="country",
                  fips_countries=[t[0] + t[-1]], screen_group="dm") for t in DM]
    return out


def _fill(store: SqliteStore) -> None:
    a = _assets()
    store.upsert_assets(a)
    store.upsert_prices(_bars([x.ticker for x in a], *DATA, seed=5), "yfinance")
    days = pd.date_range(*DATA, freq="D")
    rng = np.random.default_rng(6)
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
        tone = rng.normal(-2, 0.5, len(days)) - np.where(rng.random(len(days)) < 0.03, 5, 0)
        parts.append(pd.DataFrame({"session_date": days, "source": "events", "entity": e,
                                   "metric": "tone", "value_sum": tone * arts, "n": arts}))
    store.upsert_gdelt_daily(pd.concat(parts, ignore_index=True)[GDELT_DAILY_COLUMNS],
                             "events_daily")


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    home = tmp_path_factory.mktemp("f1_template")
    cfg = Config(home=home, gcp_key_path=home / "k.json")
    store = SqliteStore(cfg.db_path)
    store.init()
    _fill(store)
    return cfg.db_path


@pytest.fixture
def fcfg(tmp_path: Path, monkeypatch, template: Path) -> Config:
    monkeypatch.setattr(pl, "F1_WINDOW", WINDOW)
    monkeypatch.setattr(pl, "F1_N_BOOT", N_BOOT)  # ADR 0006 fixes 2000; fewer draws for speed
    cfg = Config(home=tmp_path, gcp_key_path=tmp_path / "k.json", start=DATA[0],
                 holdout_start=HOLDOUT)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(template, cfg.db_path)
    return cfg


@pytest.fixture
def store(fcfg: Config) -> SqliteStore:
    return SqliteStore(fcfg.db_path)


def _members_hash(tickers) -> str:
    return hashlib.sha256(",".join(sorted(tickers)).encode()).hexdigest()[:16]


def _expected_params(cfg: Config) -> dict:
    return {"hypothesis": "F1", "group": "em",
            "signals": ["quad3", "quad4", "root_14", "violence", "tone"],
            "theta": 3.0, "window": 60, "horizon": 20, "decluster_days": 20,
            "outcome": "abnormal_vol", "expected_sign": 1.0, "period": "confirmation",
            "alpha": 0.05, "min_events": 30, "min_event_days": 30,
            "features_version": FEATURES_VERSION, "null": "placebo_dates", "n_boot": N_BOOT,
            "seed": cfg.seed, "members_hash": _members_hash(EM)}


# --- refusals -------------------------------------------------------------------------------

def test_refuses_n_boot_below_999(fcfg, store, monkeypatch):
    monkeypatch.setattr(pl, "F1_N_BOOT", 998)
    out: list[str] = []
    assert pl.followup_f1(fcfg, store, lambda: NOW, out.append) == pl.REFUSED
    assert "n_boot" in out[-1]
    assert store.read_trials() == []


def test_refuses_a_window_reaching_the_holdout(fcfg, store, monkeypatch):
    monkeypatch.setattr(pl, "F1_WINDOW", (WINDOW[0], HOLDOUT))
    out: list[str] = []
    assert pl.followup_f1(fcfg, store, lambda: NOW, out.append) == pl.REFUSED
    assert "hold-out" in out[-1]
    assert store.read_trials() == []


def test_missing_without_em_assets(fcfg, store):
    store.upsert_assets(_assets(em_group=False))
    out: list[str] = []
    assert pl.followup_f1(fcfg, store, lambda: NOW, out.append) == pl.MISSING
    assert "em" in out[-1]
    assert store.read_trials() == []


@pytest.mark.parametrize("n", ["998", "1000", "2000"])
def test_cli_has_no_n_boot_option(tmp_path: Path, n: str):
    """ADR 0006 fixes n_boot at 2000; any --n-boot (even 2000) is a usage error."""
    cli.main(["--home", str(tmp_path), "init-db"])
    with pytest.raises(SystemExit) as e:
        cli.main(["--home", str(tmp_path), "followup", "--id", "F1", "--n-boot", n])
    assert e.value.code == 2


def test_cli_unknown_id_is_a_usage_error(tmp_path: Path):
    cli.main(["--home", str(tmp_path), "init-db"])
    with pytest.raises(SystemExit) as e:
        cli.main(["--home", str(tmp_path), "followup", "--id", "F2"])
    assert e.value.code == 2


def test_cli_without_assets_is_missing(tmp_path: Path):
    cli.main(["--home", str(tmp_path), "init-db"])
    assert cli.main(["--home", str(tmp_path), "followup", "--id", "F1"]) == pl.MISSING


# --- the single trial -----------------------------------------------------------------------

def test_logs_exactly_one_trial_with_every_fixed_setting(fcfg, store, monkeypatch):
    seen: dict = {}
    real_comp, real_placebo = pl.event_signals.composite_events, pl.eventstudy.placebo_p
    real_scores = pl.event_signals.shock_scores

    def spy_scores(daily, tdays, assets, track, window):
        seen.setdefault("windows", []).append(window)
        seen["score_tickers"] = sorted(a.ticker for a in assets)
        return real_scores(daily, tdays, assets, track, window)

    def spy_comp(scores, assets_em, signals, theta, decluster_days, trading_days):
        seen["comp"] = (sorted(assets_em), tuple(signals), theta, decluster_days)
        return real_comp(scores, assets_em, signals, theta, decluster_days, trading_days)

    def spy_placebo(events, outcome, h, sign, tdays, n_draws, seed):
        res = real_placebo(events, outcome, h, sign, tdays, n_draws, seed)
        seen["placebo"] = (events.copy(), outcome.copy(), h, sign, n_draws, seed, res)
        return res

    monkeypatch.setattr(pl.event_signals, "shock_scores", spy_scores)
    monkeypatch.setattr(pl.event_signals, "composite_events", spy_comp)
    monkeypatch.setattr(pl.eventstudy, "placebo_p", spy_placebo)
    out: list[str] = []
    assert pl.followup_f1(fcfg, store, lambda: NOW, out.append) == pl.OK
    trials = store.read_trials()
    assert len(trials) == 1
    t = trials[0]
    assert (t.track, t.stage, t.hypothesis) == ("A", "signal", "F1")
    assert t.params == _expected_params(fcfg)
    assert (t.data_start, t.data_end) == WINDOW
    assert t.trial_id == pl.trial_id("signal", "A", t.params, *WINDOW)
    # EM assets only, W = 60, the five conflict signals, theta 3, one 20-day declustering
    assert seen["windows"] == [60] and seen["score_tickers"] == EM
    assert seen["comp"] == (EM, ("quad3", "quad4", "root_14", "violence", "tone"), 3.0, 20)
    events, outcome, h, sign, n_draws, seed, res = seen["placebo"]
    assert (h, sign, n_draws, seed) == (20, 1.0, N_BOOT, fcfg.seed)
    assert set(events["ticker"]) <= set(EM) and set(outcome["ticker"]) <= set(EM)
    # embargo: events and outcomes inside the window, outcomes ending before the hold-out
    assert events["date"].min() >= pd.Timestamp(WINDOW[0])
    assert outcome["date"].max() < pd.Timestamp(WINDOW[1]) - pd.Timedelta(days=20)
    note = json.loads(t.notes)
    assert note["n_events"] >= 30 and note["n_event_days"] >= 30 and note["sufficient"]
    assert note["p_one_sided"] == pytest.approx(res.p)
    assert note["conf_test"]["test"] == "placebo_dates"
    assert note["verdict"] == ("pass" if res.p < 0.05 and note["mean"] > 0 else "fail")
    assert any(f"F1: {note['verdict']}" in line for line in out)
    assert "1 new trial logged" in out[-1]


def test_rerun_logs_nothing_and_prints_the_stored_verdict(fcfg, store, monkeypatch):
    pl.followup_f1(fcfg, store, lambda: NOW, _silent)
    first = store.read_trials()
    verdict = json.loads(first[0].notes)["verdict"]

    def boom(*a, **k):
        raise AssertionError("a rerun must not recompute the test")

    monkeypatch.setattr(pl.eventstudy, "placebo_p", boom)
    monkeypatch.setattr(pl.event_signals, "shock_scores", boom)
    out: list[str] = []
    assert pl.followup_f1(fcfg, store, lambda: NOW + timedelta(days=1), out.append) == pl.OK
    assert [t.trial_id for t in store.read_trials()] == [first[0].trial_id]
    assert any(f"F1: {verdict}" in line and "already logged" in line for line in out)
    assert first[0].trial_id in "\n".join(out)
    assert f"({N_BOOT} draws, seed {fcfg.seed})" in "\n".join(out)
    assert "0 new trials logged" in out[-1]


SECOND_RUN_CHANGES = [
    # anything that changes the trial id must still not give a second F1 trial (ADR 0006)
    ("n_boot", lambda mp, cfg, store: mp.setattr(pl, "F1_N_BOOT", N_BOOT + 1)),
    ("seed", lambda mp, cfg, store: mp.setattr(cfg, "seed", cfg.seed + 1)),
    ("features_version", lambda mp, cfg, store: mp.setattr(
        pl.features, "FEATURES_VERSION", "other")),
    ("members", lambda mp, cfg, store: store.upsert_assets(
        [a for a in _assets() if a.ticker != "EM5"]
        + [Asset(ticker="EM5", name="EM5", kind="etf", track="A", group="country",
                 fips_countries=["E5"], screen_group="dm")])),
]


@pytest.mark.parametrize("what,change", SECOND_RUN_CHANGES, ids=[c[0] for c in SECOND_RUN_CHANGES])
def test_second_run_with_other_settings_logs_nothing(fcfg, store, monkeypatch, what, change):
    """Regression: the once-only guard was the exact trial id, so another n_boot (or seed,
    features version, members) recomputed the test and logged a second F1 trial."""
    assert pl.followup_f1(fcfg, store, lambda: NOW, _silent) == pl.OK
    first = store.read_trials()
    assert len(first) == 1
    change(monkeypatch, fcfg, store)

    def boom(*a, **k):
        raise AssertionError("a second F1 run must not read data or recompute the test")

    for mod, name in [(pl.eventstudy, "placebo_p"), (pl.event_signals, "shock_scores"),
                      (store, "read_prices"), (store, "read_gdelt_daily"),
                      (store, "read_assets")]:
        monkeypatch.setattr(mod, name, boom)
    out: list[str] = []
    assert pl.followup_f1(fcfg, store, lambda: NOW + timedelta(days=1), out.append) == pl.OK
    assert [t.trial_id for t in store.read_trials()] == [first[0].trial_id]
    assert [t.hypothesis for t in store.read_trials()] == ["F1"]
    assert "already logged" in out[0] and first[0].trial_id in out[0]
    assert "0 new trials logged" in out[-1]


def test_n_boot_is_fixed_at_2000():
    assert pl.F1_N_BOOT == 2000
    assert "n_boot" not in inspect.signature(pl.followup_f1).parameters


def test_inconclusive_with_too_few_events(fcfg, store, monkeypatch):
    monkeypatch.setattr(pl, "F1_WINDOW", SHORT)

    def boom(*a, **k):
        raise AssertionError("no placebo test without enough events")

    monkeypatch.setattr(pl.eventstudy, "placebo_p", boom)
    out: list[str] = []
    assert pl.followup_f1(fcfg, store, lambda: NOW, out.append) == pl.OK
    trials = store.read_trials()
    assert len(trials) == 1
    note = json.loads(trials[0].notes)
    assert not note["sufficient"] and note["n_events"] < 30
    assert note["verdict"] == "inconclusive" and note["p_one_sided"] is None
    assert (trials[0].data_start, trials[0].data_end) == SHORT
    assert any("F1: inconclusive" in line for line in out)


def test_never_reads_holdout(fcfg, monkeypatch):
    inner = SqliteStore(fcfg.db_path)
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

        def read_labels(self, start, end, horizons=None):
            self.reads.append(("labels", start, end))
            return inner.read_labels(start, end, horizons)

        def read_features(self, start, end, features=None):
            self.reads.append(("features", start, end))
            return inner.read_features(start, end, features)

    store = Recording()
    seen = []
    real = pl.eventstudy.calendar_time_series

    def spy(events, outcome, h, trading_days):
        seen.extend([events["date"].max(), outcome["date"].max()])
        assert max(pd.Timestamp(d) for d in trading_days) < pd.Timestamp(HOLDOUT)
        return real(events, outcome, h, trading_days)

    monkeypatch.setattr(pl.eventstudy, "calendar_time_series", spy)
    assert pl.followup_f1(fcfg, store, lambda: NOW, _silent) == pl.OK
    assert store.reads and all(end is not None and end <= HOLDOUT for _, _, end in store.reads)
    assert {k for k, _, _ in store.reads} == {"prices", "gdelt"}
    assert seen and max(d for d in seen if pd.notna(d)) < pd.Timestamp(WINDOW[1])
    assert all(t.data_end < HOLDOUT for t in inner.read_trials())


VERDICT_CASES = [
    # (sufficient, p, mean) -> verdict; ADR 0006: pass iff p < 0.05 and mean > 0
    (True, 0.01, 0.05, "pass"),
    (True, 0.049, 1e-6, "pass"),
    (True, 0.05, 0.05, "fail"),
    (True, 0.01, 0.0, "fail"),
    (True, 0.01, -0.02, "fail"),
    (True, float("nan"), 0.05, "fail"),
    (True, 0.01, float("nan"), "fail"),
    (False, 0.01, 0.05, "inconclusive"),
]


@pytest.mark.parametrize("sufficient,p,mean,expected", VERDICT_CASES)
def test_f1_verdict(sufficient, p, mean, expected):
    assert pl._f1_verdict(sufficient, p, mean) == expected
