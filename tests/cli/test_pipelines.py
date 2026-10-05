"""Orchestration: fake GDELT and price sources, a real SqliteStore on a tmp file."""

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.store import SqliteStore
from eventtrade.cli import pipelines as pl
from eventtrade.config import Config
from eventtrade.core.features import FEATURES_VERSION
from eventtrade.schemas import GDELT_DAILY_COLUMNS, PRICE_COLUMNS, Asset, Trial

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
COUNTRIES = ["JA", "GM", "UK", "CH", "BR", "IN"]
TICKERS = ["EWJ", "EWG", "EWU", "FXI", "EWZ", "INDA"]


class Budget(RuntimeError):
    pass


class FakeGdelt:
    """Records calls; returns one _GLOBAL row per session day of the requested window."""

    def __init__(self, refuse_from: date | None = None) -> None:
        self.estimates: list[tuple[str, date, date]] = []
        self.pulls: list[tuple[date, date]] = []
        self.refuse_from = refuse_from

    def estimate(self, query_name, start, end, **params):
        self.estimates.append((query_name, start, end))
        return 10**9

    def _rows(self, start, end, source):
        days = pd.date_range(start, end, freq="D")
        return pd.DataFrame({"session_date": days, "source": source, "entity": "_GLOBAL",
                             "metric": "n_events", "value_sum": 1.0, "n": 1})[GDELT_DAILY_COLUMNS]

    def pull_events_daily(self, start, end, cutoff_et):
        if self.refuse_from and start >= self.refuse_from:
            raise Budget("over budget")
        self.pulls.append((start, end))
        return self._rows(start, end, "events")

    def pull_gkg_themes_daily(self, start, end, cutoff_et, themes, by_country):
        self.pulls.append((start, end))
        return self._rows(start, end, "gkg")

    def pull_gkg_orgs_daily(self, start, end, cutoff_et, aliases):
        self.pulls.append((start, end))
        return self._rows(start, end, "gkg")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")


@pytest.fixture
def store(cfg: Config) -> SqliteStore:
    s = SqliteStore(cfg.db_path)
    s.init()
    return s


def _silent(_: str) -> None:
    pass


# --- month chunks: calendar months, clipped to the window, both ends inclusive ---------------

MONTH_CHUNK_CASES = [
    ((date(2024, 1, 1), date(2024, 1, 31)), [(date(2024, 1, 1), date(2024, 1, 31))]),
    ((date(2024, 1, 15), date(2024, 3, 2)), [(date(2024, 1, 15), date(2024, 1, 31)),
                                              (date(2024, 2, 1), date(2024, 2, 29)),
                                              (date(2024, 3, 1), date(2024, 3, 2))]),
    ((date(2024, 12, 31), date(2025, 1, 1)), [(date(2024, 12, 31), date(2024, 12, 31)),
                                              (date(2025, 1, 1), date(2025, 1, 1))]),
    ((date(2024, 5, 5), date(2024, 5, 5)), [(date(2024, 5, 5), date(2024, 5, 5))]),
]


@pytest.mark.parametrize("window,expected", MONTH_CHUNK_CASES)
def test_month_chunks(window, expected):
    assert pl.month_chunks(*window) == expected


# --- ingest-gdelt: dry run by default, resumable, hold-out refused ---------------------------

def test_gdelt_dry_run_writes_nothing(cfg, store):
    g = FakeGdelt()
    rc = pl.ingest_gdelt(cfg, store, g, "events", date(2024, 1, 1), date(2024, 2, 10), False,
                         False, lambda: NOW, Budget, _silent)
    assert rc == pl.OK
    assert [(a, b) for _, a, b in g.estimates] == [(date(2024, 1, 1), date(2024, 1, 31)),
                                                   (date(2024, 2, 1), date(2024, 2, 10))]
    assert g.pulls == []
    assert store.gdelt_session_dates("events_daily", None, None) == []


def test_gdelt_execute_is_resumable(cfg, store):
    g = FakeGdelt()
    args = (date(2024, 1, 1), date(2024, 2, 29), True, False, lambda: NOW, Budget, _silent)
    assert pl.ingest_gdelt(cfg, store, g, "events", *args) == pl.OK
    assert len(g.pulls) == 2
    assert len(store.gdelt_session_dates("events_daily", None, None)) == 60
    g2 = FakeGdelt()
    assert pl.ingest_gdelt(cfg, store, g2, "events", *args) == pl.OK
    assert g2.pulls == []  # both months already stored


def test_gdelt_budget_refusal_keeps_earlier_months(cfg, store):
    g = FakeGdelt(refuse_from=date(2024, 2, 1))
    rc = pl.ingest_gdelt(cfg, store, g, "events", date(2024, 1, 1), date(2024, 3, 31), True,
                         False, lambda: NOW, Budget, _silent)
    assert rc == pl.BUDGET
    assert g.pulls == [(date(2024, 1, 1), date(2024, 1, 31))]
    assert len(store.gdelt_session_dates("events_daily", None, None)) == 31


@pytest.mark.parametrize("end", [date(2025, 4, 1), date(2026, 1, 1)])
def test_gdelt_refuses_holdout_end(cfg, store, end):
    g = FakeGdelt()
    rc = pl.ingest_gdelt(cfg, store, g, "events", date(2025, 3, 1), end, False, False,
                         lambda: NOW, Budget, _silent)
    assert rc == pl.REFUSED and g.estimates == [] and g.pulls == []


def test_gdelt_default_end_is_last_dev_day(cfg, store):
    g = FakeGdelt()
    pl.ingest_gdelt(cfg, store, g, "events", date(2025, 3, 1), None, False, False,
                    lambda: NOW, Budget, _silent)
    assert g.estimates[-1][2] == date(2025, 3, 31)


# Review regressions: --by-country rows reached no feature, and the country query relabelled the
# _GLOBAL theme rows so the next plain gkg-themes ingest re-pulled (and re-billed) every month.
def test_gkg_themes_by_country_refused_and_resume_holds(cfg, store):
    store.upsert_assets([Asset(ticker="USO", name="oil", kind="etf", track="A",
                               group="commodity", gkg_themes=["ENV_OIL"])])
    calls = []

    class G(FakeGdelt):
        def pull_gkg_themes_daily(self, start, end, cutoff_et, themes, by_country):
            calls.append(by_country)
            return super().pull_gkg_themes_daily(start, end, cutoff_et, themes, by_country)

    out: list[str] = []
    rcs = [pl.ingest_gdelt(cfg, store, G(), "gkg-themes", date(2024, 1, 1), date(2024, 1, 31),
                           True, bc, lambda: NOW, Budget, out.append)
           for bc in (False, True, False)]
    assert rcs == [pl.OK, pl.REFUSED, pl.OK]
    assert calls == [False]  # one pull; the refused run and the rerun pull nothing
    assert "1 already stored, 0 to pull" in out[-1]


def test_gkg_themes_needs_universe(cfg, store):
    rc = pl.ingest_gdelt(cfg, store, FakeGdelt(), "gkg-themes", date(2024, 1, 1),
                         date(2024, 1, 31), False, False, lambda: NOW, Budget, _silent)
    assert rc == pl.MISSING


# --- ingest-prices: partial failures are stored and reported ---------------------------------

class PartialFail(RuntimeError):
    def __init__(self, failures, prices):
        super().__init__("partial")
        self.failures, self.prices = failures, prices


def _bars(tickers, start, end, seed=0):
    days = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    rows = []
    for t in tickers:
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
        for d, p in zip(days, px, strict=True):
            rows.append((d, t, p, p * 1.01, p * 0.99, p, 1e6, p, p))
    return pd.DataFrame(rows, columns=PRICE_COLUMNS)


def test_ingest_prices_partial_failure(cfg, store):
    seen = []

    def fetch(tickers, start, end):
        seen.append((list(tickers), start, end))
        good = [t for t in tickers if t != "BAD"]
        raise PartialFail({"BAD": "no rows"}, _bars(good, start, end))

    rc = pl.ingest_prices(cfg, store, fetch, ["SPY", "BAD"], None, date(2024, 1, 1), None,
                          echo=_silent)
    assert rc == pl.PARTIAL
    assert seen[0][2] == date(2025, 3, 31)  # inclusive end defaults to the last dev day
    assert set(store.read_prices(None, None, None)["ticker"]) == {"SPY"}


def test_ingest_prices_refuses_holdout(cfg, store):
    rc = pl.ingest_prices(cfg, store, lambda *a: pytest.fail("fetched"), ["SPY"], None,
                          date(2025, 3, 1), date(2025, 4, 1), echo=_silent)
    assert rc == pl.REFUSED


# --- load-universe: Track B gets unambiguous GKG aliases --------------------------------------

def test_load_universe_b_adds_aliases(cfg, store):
    def fetch_index(index):
        if index == "sp500":
            return [Asset(ticker="BA", name="Boeing", kind="stock", track="B", group="sp500")]
        return [Asset(ticker="BA", name="dup", kind="stock", track="B", group="sp400")]

    assert pl.load_universe(cfg, store, "B", list, fetch_index, _silent) == pl.OK
    (ba,) = store.read_assets("B")
    assert ba.group == "sp500" and "boeing" in ba.org_aliases


def test_load_universe_drops_stale_rows_of_reloaded_track(cfg, store):
    # ADR 0002 item 2: v1 stored both share classes (GOOG and GOOGL, FOX/FOXA, NWS/NWSA) and
    # TLT/IEF/SHY with fips ['US']. An upsert-only reload left GOOG/FOX/NWS in assets, so they
    # kept being double-counted. Reloading a track now removes that track's absent tickers;
    # other tracks are untouched.
    def stock(t):
        return Asset(ticker=t, name=t, kind="stock", track="B", group="sp500")

    etf = Asset(ticker="TLT", name="TLT", kind="etf", track="A", group="rates",
                fips_countries=["US"])
    store.upsert_assets([stock("GOOG"), stock("GOOGL"), stock("FOX"), stock("FOXA"), etf])

    def fetch_index(index):
        return [stock("GOOGL"), stock("FOXA")] if index == "sp500" else []

    assert pl.load_universe(cfg, store, "B", list, fetch_index, _silent) == pl.OK
    assert [a.ticker for a in store.read_assets("B")] == ["FOXA", "GOOGL"]
    assert [a.ticker for a in store.read_assets("A")] == ["TLT"]

    fixed = etf.model_copy(update={"fips_countries": []})
    assert pl.load_universe(cfg, store, "A", lambda: [fixed], fetch_index, _silent) == pl.OK
    assert store.read_assets("A") == [fixed]


# --- hypothesis tagging: feature names map back to the pre-registered table -------------------

HYPOTHESIS_CASES = [
    # ADR 0002 item 3: H0 has no pre-registered sign (None = either sign at gate 1).
    ("A", "events.tone.mean_z.60", ("H0", None)),
    ("A", "events.n_events.share_sm5_z.60", ("H0", None)),
    ("A", "events.tone.mean5d_z.60", ("A3", 1.0)),
    ("A", "events.root_19_fight/n_events.sum_share_z.60", ("A1", -1.0)),
    ("A", "events.root_19_fight/n_events.sum_share_lr.60", ("A1", -1.0)),
    ("A", "events.root_19_fight/n_events.sum_share_sm5_z.60", ("A1", -1.0)),
    ("A", "events.goldstein.mean_z.60", ("A1", 1.0)),
    ("A", "gkg.theme_docs.mean_z.60", ("A2", 1.0)),
    ("B", "gkg.org_neg_docs.sum_lr.60", ("B2", -1.0)),
    ("B", "gkg.org_tone.mean.0", ("B1", 1.0)),
    ("B", "gkg.org_docs.mean_sm5_z.60", ("B1", 1.0)),
    ("B", "gkg.org_docs.mean_z.60*gkg.org_tone.mean.0", ("B1", 1.0)),
    ("A", "made.up.name", ("?", None)),
]


@pytest.mark.parametrize("track,name,expected", HYPOTHESIS_CASES)
def test_hypothesis_of(track, name, expected):
    assert pl.hypothesis_of(track, name) == expected


A4_SIGN_CASES = [
    # A4 is positive in shock units: a conflict count shock is HIGH (A1 sign -1) -> IC +1; a
    # Goldstein shock is LOW (A1 sign +1) -> IC -1. Non-A1 features are not A4 features.
    ("events.root_14_protest/n_events.sum_share_lr.60", 1.0),
    ("events.root_20_massviolence/n_events.sum_share_sm5_z.60", 1.0),
    ("events.goldstein.mean_z.60", -1.0),
    ("events.tone.mean_z.60", None),
]


@pytest.mark.parametrize("name,expected", A4_SIGN_CASES)
def test_a4_sign(name, expected):
    assert pl._a4_sign(name) == expected


HORIZON_CASES = [
    # ADR 0002 item 1: sm5 is evaluated at h = 5 and 20 only; everything else at every horizon.
    ("A", "events.tone.mean_sm5_z.60", [5, 20]),
    ("B", "gkg.org_neg_docs.sum_sm5_z.60", [5, 20]),
    ("A", "events.tone.mean_z.60", [1, 5, 20]),
    ("A", "events.tone.mean5d_z.60", [1, 5, 20]),
    ("B", "gkg.org_docs.mean_z.60*gkg.org_tone.mean.0", [1, 5, 20]),
]


@pytest.mark.parametrize("track,name,expected", HORIZON_CASES)
def test_horizons_for(track, name, expected):
    assert pl._horizons_for(track, name, [1, 5, 20]) == expected


# --- end to end on synthetic Track A data ------------------------------------------------------

def _synthetic(store: SqliteStore) -> None:
    store.upsert_assets([Asset(ticker=t, name=t, kind="etf", track="A", group="country",
                               fips_countries=[c]) for t, c in zip(TICKERS, COUNTRIES, strict=True)])
    start, end = date(2023, 1, 2), date(2024, 3, 29)
    store.upsert_prices(_bars(TICKERS, start, end, seed=1), "yfinance")
    rng = np.random.default_rng(2)
    rows = []
    for d in pd.date_range(start, end, freq="D"):
        for e in ["_GLOBAL", *COUNTRIES]:
            n = float(rng.integers(50, 150)) * (6 if e == "_GLOBAL" else 1)
            rows += [(d, "events", e, "n_events", n, int(n)),
                     (d, "events", e, "tone", float(rng.normal(-2, 1)) * n, int(n)),
                     (d, "events", e, "root_19_fight", float(rng.integers(0, 10)), int(n)),
                     (d, "events", e, "goldstein", float(rng.normal(0, 1)) * n, int(n))]
    store.upsert_gdelt_daily(pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS), "events_daily")


def test_build_signal_backtest_overfit(cfg, store):
    _synthetic(store)
    assert pl.build_dataset(cfg, store, "A", _silent) == pl.OK
    feats = store.read_features(None, None)
    assert "events.root_19_fight/n_events.sum_share_z.60" in set(feats["feature"])
    assert len(store.read_labels(None, None, [1])) > 0

    clock = lambda: NOW
    assert pl.signal_study(cfg, store, "A", clock, _silent) == pl.OK
    trials = store.read_trials("A", "signal")
    names = [str(f) for f in feats["feature"].unique()]
    assert any("_sm5_z." in n for n in names)
    n_dir = sum(len(pl._horizons_for("A", n, cfg.horizons)) for n in names)
    n_a4 = sum(len(pl._horizons_for("A", n, cfg.horizons)) for n in names
               if pl.hypothesis_of("A", n)[0] == "A1")
    assert len(trials) == n_dir + n_a4
    assert all(json.loads(t.notes)["gate"].split(":")[0] in ("pass", "fail") for t in trials)
    # ADR 0002 item 5: every v2 trial carries the features version and its registered sign
    assert all(t.params["features_version"] == FEATURES_VERSION for t in trials)
    for t in trials:
        if t.hypothesis == "A4":
            assert t.params["label"] == "vol_surprise_partial"
            assert t.params["expected_sign"] == pl._a4_sign(t.params["feature"])
            assert "placebo_t" in json.loads(t.notes)
        else:
            assert t.params["expected_sign"] == pl.hypothesis_of("A", t.params["feature"])[1]
        if "_sm5_z." in t.params["feature"]:
            assert t.params["horizon"] in (5, 20)
    # re-running the same study is not a new trial
    pl.signal_study(cfg, store, "A", clock, _silent)
    assert len(store.read_trials("A", "signal")) == len(trials)

    for name in ["events.tone.mean_z.60", "events.n_events.share_z.60"]:
        # H0 has no registered sign: the backtest refuses to pick one, an explicit one runs.
        assert pl.backtest(cfg, store, "A", clock, feature=name, horizon=5,
                           echo=_silent) == pl.REFUSED
        assert pl.backtest(cfg, store, "A", clock, feature=name, sign=1.0, horizon=5,
                           echo=_silent) == pl.OK
    bt = store.read_trials("A", "backtest")
    assert len(bt) == 2 and all(t.stats is not None for t in bt)
    rets = store.read_trial_returns([t.trial_id for t in bt])
    assert rets.index.max() < pd.Timestamp(cfg.holdout_start)
    assert pl.overfit_report(cfg, store, "A", _silent) == pl.OK


# ADR 0002 item 5: v1 trials (experiment 0003) are neither re-logged nor overwritten; the same
# feature x horizon under v2 is a new row.
def test_signal_study_keeps_v1_trials_and_adds_v2_rows(cfg, store):
    _synthetic(store)
    pl.build_dataset(cfg, store, "A", _silent)
    name = "events.root_19_fight/n_events.sum_share_lr.60"
    v1 = [_v1_signal_trial(name, 1), _v1_signal_trial(name, 5, label="realized_vol")]
    for t in v1:
        store.add_trial(t)
    pl.signal_study(cfg, store, "A", lambda: NOW, _silent)
    after = store.read_trials("A", "signal")
    ids = [t.trial_id for t in after]
    assert len(ids) == len(set(ids))
    old = [t for t in after if "features_version" not in t.params]
    assert sorted(t.trial_id for t in old) == sorted(t.trial_id for t in v1)
    assert [json.loads(t.notes)["gate"] for t in old] == ["pass: v1", "pass: v1"]
    v2 = [t for t in after if t.params.get("feature") == name
          and t.params.get("features_version") == FEATURES_VERSION]
    assert {(t.params["label"], t.params["horizon"]) for t in v2} == {
        *(("ret_oo", h) for h in cfg.horizons), *(("vol_surprise_partial", h) for h in cfg.horizons)}
    assert not {t.trial_id for t in v2} & {t.trial_id for t in v1}


# Experiment 0003 bug 2: signal_study discarded the sign from hypothesis_of, so a wrong-signed
# A1 feature could pass. Each direction trial must reach the gate with its registered sign.
def test_signal_study_passes_registered_sign_to_gate(cfg, store, monkeypatch):
    _synthetic(store)
    pl.build_dataset(cfg, store, "A", _silent)
    seen: dict[str, set] = {}
    real = pl.evaluate.passes_gate

    def spy(rep, subs, cost, *, expected_sign):
        seen.setdefault(rep.feature, set()).add(expected_sign)
        return real(rep, subs, cost, expected_sign=expected_sign)

    monkeypatch.setattr(pl.evaluate, "passes_gate", spy)
    pl.signal_study(cfg, store, "A", lambda: NOW, _silent)
    assert seen["events.tone.mean_z.60"] == {None}  # H0: either sign
    assert seen["events.root_19_fight/n_events.sum_share_z.60"] == {-1.0}
    assert seen["events.goldstein.mean_z.60"] == {1.0}
    assert seen["events.tone.mean5d_z.60"] == {1.0}


def test_signal_study_refuses_features_built_before_v2(cfg, store, tmp_path):
    # The store has no features version; a v1 build (no sm5 names) must not be logged as v2.
    _synthetic(store)
    pl.build_dataset(cfg, store, "A", _silent)
    feats = store.read_features(None, None)
    old = SqliteStore(tmp_path / "v1.sqlite")
    old.init()
    old.upsert_assets(store.read_assets("A"))
    old.replace_features(feats[~feats["feature"].str.contains("_sm5_z.", regex=False)])
    old.replace_labels(store.read_labels(None, None))
    out: list[str] = []
    assert pl.signal_study(cfg, old, "A", lambda: NOW, out.append) == pl.MISSING
    assert old.read_trials("A", "signal") == [] and "build-dataset" in out[-1]


def test_model_backtest_walk_forward(cfg, store, monkeypatch):
    monkeypatch.setattr(pl, "_WF_TRAIN_DAYS", 100)
    monkeypatch.setattr(pl, "_WF_TEST_DAYS", 40)
    _synthetic(store)
    pl.build_dataset(cfg, store, "A", _silent)
    names = ["events.tone.mean_z.60", "events.n_events.share_z.60"]
    rc = pl.backtest(cfg, store, "A", lambda: NOW, model="ridge", model_features=names,
                     horizon=5, echo=_silent)
    assert rc == pl.OK
    (t,) = store.read_trials("A", "backtest")
    assert t.params["kind"] == "model" and t.params["features"] == sorted(names)
    assert t.params["feature_selection"] == "user"


# --- model inputs are chosen inside each fold, never from dev-period signal trials ------------
# Review regression: the default model inputs were the features that passed signal-study, whose
# IC was measured over every walk-forward test block, so pure-noise inputs came out "OOS" with
# NW t > 4.

def _panel(n_days: int, n_names: int, late_from: int, seed: int = 0):
    """'early' predicts ret_oo on every date; 'late' barely (IC ~0.02) before late_from, then fully."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2016-01-04", periods=n_days)
    f_rows, l_rows = [], []
    for i, d in enumerate(dates):
        y = rng.normal(0, 0.01, n_names)
        early = y + rng.normal(0, 0.01, n_names)
        late = y if i >= late_from else 0.02 * y + rng.normal(0, 0.01, n_names)
        noise = rng.normal(0, 0.01, n_names)
        for j in range(n_names):
            tk = f"T{j:02d}"
            f_rows += [(d, tk, "early", early[j]), (d, tk, "late", late[j]),
                       (d, tk, "noise", noise[j])]
            l_rows.append((d, tk, 1, y[j], 0.0, 0.0))
    from eventtrade.schemas import FEATURE_COLUMNS, LABEL_COLUMNS
    return (pd.DataFrame(f_rows, columns=FEATURE_COLUMNS),
            pd.DataFrame(l_rows, columns=LABEL_COLUMNS))


def test_model_selection_uses_training_dates_only(monkeypatch):
    monkeypatch.setattr(pl, "_WF_TRAIN_DAYS", 200)
    monkeypatch.setattr(pl, "_WF_TEST_DAYS", 50)
    fs, labs = _panel(400, 20, late_from=300)
    # Over the whole period 'late' passes the IC test (what signal-study would have logged).
    full = pl._ic_table(fs, labs, 1)
    assert "late" in pl.models.select_features(full, full.index, 1)
    _, chosen = pl._model_scores(fs, labs, "ridge", 1, 0, select=True)
    # Folds: test 201-250, 251-300, 301-350, 351-399. The fold testing 301-350 trains on 0-299,
    # where 'late' is noise: it must not be chosen even though it predicts that fold's test block.
    assert [("early" in c) for c in chosen] == [True] * 4
    assert "late" not in chosen[2] and "noise" not in chosen[2]


def test_model_default_ignores_signal_trials(cfg, store, monkeypatch):
    monkeypatch.setattr(pl, "_WF_TRAIN_DAYS", 100)
    monkeypatch.setattr(pl, "_WF_TEST_DAYS", 40)
    _synthetic(store)
    pl.build_dataset(cfg, store, "A", _silent)
    pl.signal_study(cfg, store, "A", lambda: NOW, _silent)
    seen = {}
    real = pl._model_scores

    def spy(fs, labs, model, horizon, seed, select):
        seen["names"], seen["select"] = sorted(fs["feature"].unique()), select
        return real(fs, labs, model, horizon, seed, select)

    monkeypatch.setattr(pl, "_model_scores", spy)
    pl.backtest(cfg, store, "A", lambda: NOW, model="ridge", horizon=5, echo=_silent)
    # Candidates are every stored feature; the choice is made per fold, not from gate-1 trials.
    assert seen["select"] is True
    assert seen["names"] == sorted(store.read_features(None, None)["feature"].unique())


# --- the model's day-t cross-section never depends on a future price bar -----------------------
# Review regression: to_design inner-joined labels, so a name whose bar at t+1+h was missing was
# dropped from day t's scores (and shifted every other name's z-score).

def test_model_scores_names_without_future_label(monkeypatch):
    monkeypatch.setattr(pl, "_WF_TRAIN_DAYS", 100)
    monkeypatch.setattr(pl, "_WF_TEST_DAYS", 40)
    fs, labs = _panel(200, 6, late_from=0)
    gap_day = sorted(labs["date"].unique())[150]
    labs = labs[~((labs["ticker"] == "T02") & (labs["date"] == gap_day))]
    sc, _ = pl._model_scores(fs, labs, "ridge", 1, 0, select=False)
    assert set(sc.loc[sc["date"] == gap_day, "ticker"]) == {f"T{j:02d}" for j in range(6)}


def test_weights_need_a_bar_on_the_score_date():
    d0, d1, d2 = pd.bdate_range("2020-01-01", periods=3)
    prices = pd.DataFrame({"date": [d0, d0, d0, d1, d1, d2, d2, d2], "ticker": list("ABCABABC")})
    scores = pd.DataFrame({"date": [d0, d0, d0, d1, d1, d1], "ticker": list("ABCABC"),
                           "score": [1.0, 2.0, 3.0, 1.0, 2.0, 3.0]})
    w = pl._weights(scores, prices, 1)
    assert set(w.loc[w["date"] == d1, "ticker"]) == {"A", "B"}  # C has no bar on d1
    assert set(w.loc[w["date"] == d0, "ticker"]) == {"A", "B", "C"}


# --- gate 3 deflates over every logged trial of the track --------------------------------------
# Review regression: N counted backtest columns only, ignoring the signal trials that chose them.

def _v1_signal_trial(feature: str, horizon: int, label: str = "ret_oo") -> Trial:
    """A trial as experiment 0003 logged it: v1 params, no features_version, no sign."""
    params = {"feature": feature, "horizon": horizon, "label": label}
    d0, d1 = date(2023, 1, 2), date(2024, 3, 29)
    return Trial(trial_id=pl.trial_id("signal", "A", params, d0, d1), created_at=NOW, track="A",
                 stage="signal", hypothesis="A1", params=params, data_start=d0, data_end=d1,
                 notes=json.dumps({"gate": "pass: v1"}))


def test_overfit_report_counts_all_stages(cfg, store, monkeypatch):
    _synthetic(store)
    pl.build_dataset(cfg, store, "A", _silent)
    clock = lambda: NOW
    # ADR 0002 item 5: the v1 trials stay in N next to the v2 ones
    v1 = [_v1_signal_trial("events.root_19_fight/n_events.sum_share_lr.60", h) for h in (1, 5)]
    for t in v1:
        store.add_trial(t)
    pl.signal_study(cfg, store, "A", clock, _silent)
    for name in ["events.tone.mean_z.60", "events.n_events.share_z.60"]:
        pl.backtest(cfg, store, "A", clock, feature=name, sign=1.0, horizon=5, echo=_silent)
    n_all = len(store.read_trials("A"))
    assert {t.trial_id for t in v1} <= {t.trial_id for t in store.read_trials("A")}
    bt = store.read_trials("A", "backtest")
    assert n_all > len(bt) == 2
    seen = {}
    real = pl.overfit.overfit_report

    def spy(wide, seed, **kw):
        seen.update(kw, cols=wide.shape[1])
        return real(wide, seed, **kw)

    monkeypatch.setattr(pl.overfit, "overfit_report", spy)
    out: list[str] = []
    assert pl.overfit_report(cfg, store, "A", out.append) == pl.OK
    assert seen == {"n_trials": n_all, "cols": 2}
    assert any(f"over {n_all} logged trials" in line for line in out)


# ADR 0002 item 5, review regression: backtest params had no features_version, so a v2 backtest
# hashed to the same trial id as the same v1 backtest and was reported "identical trial already
# logged" instead of being logged.
def test_backtest_params_carry_features_version(cfg, store, monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "_WF_TRAIN_DAYS", 100)
    monkeypatch.setattr(pl, "_WF_TEST_DAYS", 40)
    clock = lambda: NOW
    name = "events.n_events.share_z.60"
    _synthetic(store)
    pl.build_dataset(cfg, store, "A", _silent)
    assert pl.backtest(cfg, store, "A", clock, feature=name, sign=1.0, horizon=5,
                       echo=_silent) == pl.OK
    pl.backtest(cfg, store, "A", clock, model="ridge", model_features=[name], horizon=5,
                echo=_silent)
    bt = store.read_trials("A", "backtest")
    assert {t.params["kind"] for t in bt} == {"rule", "model"}
    assert all(t.params["features_version"] == FEATURES_VERSION for t in bt)

    # The same rule backtest as v1 logged it (no features_version) does not block the v2 row.
    (rule,) = [t for t in bt if t.params["kind"] == "rule"]
    v1_params = {k: v for k, v in rule.params.items() if k != "features_version"}
    v1_id = pl.trial_id("backtest", "A", v1_params, rule.data_start, rule.data_end)
    other = SqliteStore(tmp_path / "with_v1.sqlite")
    other.init()
    _synthetic(other)
    pl.build_dataset(cfg, other, "A", _silent)
    other.add_trial(Trial(trial_id=v1_id, created_at=NOW, track="A", stage="backtest",
                          hypothesis="H0", params=v1_params, data_start=rule.data_start,
                          data_end=rule.data_end, notes="{}"))
    assert pl.backtest(cfg, other, "A", clock, feature=name, sign=1.0, horizon=5,
                       echo=_silent) == pl.OK
    ids = {t.trial_id for t in other.read_trials("A", "backtest")}
    assert ids == {v1_id, rule.trial_id} and v1_id != rule.trial_id


def test_backtest_needs_exactly_one_of_feature_or_model(cfg, store):
    assert pl.backtest(cfg, store, "A", lambda: NOW, echo=_silent) == pl.REFUSED


def test_holdout_refuses_without_confirmation(cfg, store):
    rc = pl.holdout(cfg, store, "A", "backtest-A-x", False, None,
                    lambda *a: pytest.fail("fetched"), False, lambda: NOW, Budget, _silent)
    assert rc == pl.REFUSED


def test_holdout_unknown_trial(cfg, store):
    rc = pl.holdout(cfg, store, "A", "nope", True, None, lambda *a: pytest.fail("fetched"),
                    False, lambda: NOW, Budget, _silent)
    assert rc == pl.MISSING


# Review regression: `holdout` without --execute fetched hold-out prices and wrote them.
def test_holdout_dry_run_fetches_and_writes_nothing(cfg, store):
    store.upsert_assets([Asset(ticker="SPY", name="SPY", kind="etf", track="A", group="market")])
    src = Trial(trial_id="backtest-A-src", created_at=NOW, track="A", stage="backtest",
                hypothesis="H0", params={"kind": "rule", "feature": "events.tone.mean_z.60",
                                         "sign": 1.0, "horizon": 5, "rebalance": 5},
                data_start=date(2020, 1, 1), data_end=date(2025, 3, 31))
    store.add_trial(src)
    g = FakeGdelt()
    rc = pl.holdout(cfg, store, "A", src.trial_id, True, g, lambda *a: pytest.fail("fetched"),
                    False, lambda: NOW, Budget, _silent)
    assert rc == pl.OK
    assert g.pulls == [] and len(g.estimates) > 0  # GDELT dry-run estimates only
    assert store.read_prices(None, cfg.holdout_start, None).empty
    assert store.read_trials("A", "holdout") == []


# Review regression: a CI entirely below zero (a significant loss in the pre-registered
# direction) was reported as a gate-4 pass.
def test_gate4_losing_strategy_fails():
    r = np.random.default_rng(1).normal(-0.001, 0.01, 250)
    lo, hi = pl.overfit.stationary_bootstrap_sharpe_ci(r, 2000, 10.0, 0)
    assert hi < 0
    assert pl._gate4_verdict(lo) == "fail"


GATE4_CASES = [
    # Only a CI strictly above zero passes; straddling, below, or undefined fail.
    (0.1, "pass"),
    (0.0, "fail"),
    (-0.5, "fail"),
    (-4.95, "fail"),
    (float("nan"), "fail"),
]


@pytest.mark.parametrize("lo,expected", GATE4_CASES)
def test_gate4_verdict(lo, expected):
    assert pl._gate4_verdict(lo) == expected


# --- sub-category screen (ADR 0003) ------------------------------------------------------------
# Short synthetic windows replace the ADR's 2015-2019 / 2020-2025Q1 split; the hold-out starts
# 2024-07-01 and the store holds price and GDELT rows after it that must never be read.

SCREEN_DISC = (date(2023, 1, 2), date(2023, 12, 29))
SCREEN_CONF = (date(2024, 1, 2), date(2024, 6, 28))
SCREEN_HOLDOUT = date(2024, 7, 1)
SCREEN_DATA = (date(2022, 9, 1), date(2024, 9, 30))  # runs into the hold-out on purpose
SCREEN_METRICS = ["n_events", "quad1", "quad2", "quad3", "quad4", "root_14_protest",
                  "root_163_sanctions", "root_18_assault", "root_19_fight",
                  "root_20_massviolence"]
# group -> tickers (one FIPS code each); commodity has 3 names (< 5): its cells keep their place
# (ADR 0003 implementation notes) with p = NaN, counted in BH's m, never discovered.
SCREEN_A = {"dm": ["DM1", "DM2", "DM3", "DM4", "DM5"], "em": ["EM1", "EM2", "EM3", "EM4", "EM5"],
            "commodity": ["CO1", "CO2", "CO3"]}
N_BOOT = 999  # the smallest n_boot the screen accepts


class RecordingStore:
    """Delegates to a real SqliteStore and records the date window of every dated read."""

    def __init__(self, inner: SqliteStore) -> None:
        self.inner = inner
        self.reads: list[tuple[str, date | None, date | None]] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def read_prices(self, tickers, start, end):
        self.reads.append(("prices", start, end))
        return self.inner.read_prices(tickers, start, end)

    def read_gdelt_daily(self, source, start, end, entities=None):
        self.reads.append(("gdelt_daily", start, end))
        return self.inner.read_gdelt_daily(source, start, end, entities)

    def read_labels(self, start, end, horizons=None):
        self.reads.append(("labels", start, end))
        return self.inner.read_labels(start, end, horizons)

    def read_features(self, start, end, features=None):
        self.reads.append(("features", start, end))
        return self.inner.read_features(start, end, features)


@pytest.fixture
def screen_cfg(tmp_path: Path, monkeypatch) -> Config:
    monkeypatch.setattr(pl, "SCREEN_DISCOVERY", SCREEN_DISC)
    monkeypatch.setattr(pl, "SCREEN_CONFIRMATION", SCREEN_CONF)
    return Config(home=tmp_path, gcp_key_path=tmp_path / "k.json", start=SCREEN_DATA[0],
                  holdout_start=SCREEN_HOLDOUT)


def _screen_track_a(store: SqliteStore, with_groups: bool = True) -> None:
    assets = [Asset(ticker="SPY", name="SPY", kind="etf", track="A", group="market",
                    fips_countries=["US"])]
    for g, tickers in SCREEN_A.items():
        assets += [Asset(ticker=t, name=t, kind="etf", track="A", group="country",
                         fips_countries=[t[:1] + t[-1]],
                         screen_group=g if with_groups else None) for t in tickers]
    store.upsert_assets(assets)
    tickers = [a.ticker for a in assets]
    store.upsert_prices(_bars(tickers, *SCREEN_DATA, seed=3), "yfinance")
    rng = np.random.default_rng(4)
    rows = []
    for d in pd.date_range(*SCREEN_DATA, freq="D"):
        for e in {a.fips_countries[0] for a in assets}:
            n = float(rng.integers(100, 300))
            rows.append((d, "events", e, "n_events", n, int(n)))
            for m in SCREEN_METRICS[1:]:
                rows.append((d, "events", e, m, float(rng.integers(0, 30)), int(n)))
    store.upsert_gdelt_daily(pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS), "events_daily")


def _force_joint(monkeypatch, significant: bool, p_cell: dict[str, float] | None = None):
    """Run the real joint test, then overwrite its p-values (orchestration tests only)."""
    import dataclasses

    real = pl.screen_stats.joint_test

    def fake(ic_by_cell, horizon_of, signs, n_boot, mean_block, seed):
        res = real(ic_by_cell, horizon_of, signs, n_boot, mean_block, seed)
        p = 0.001 if significant else 0.9
        cells = {c: (p_cell or {}).get(c, 1.0) for c in res.p_cell}
        return dataclasses.replace(res, p_s=p, p_m=p, p_cell=cells)

    monkeypatch.setattr(pl.screen_stats, "joint_test", fake)


def _screen_trials(store, period=None):
    return [t for t in store.read_trials("A", "signal") if t.hypothesis == "S3"
            and (period is None or t.params["period"] == period)]


N_A_CELLS = 7 * 3 * 2  # 7 families x {dm, em, commodity} x h {5, 20}; every cell counts
N_A_THIN = 7 * 1 * 2  # the commodity cells (3 names)


def _members_hash(tickers) -> str:
    import hashlib

    return hashlib.sha256(",".join(sorted(tickers)).encode()).hexdigest()[:16]


def test_screen_refuses_without_screen_groups(screen_cfg):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store, with_groups=False)
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT) == pl.MISSING
    assert "load-universe" in out[-1]
    assert store.read_trials("A") == []


def test_screen_stop_path_logs_every_discovery_cell(screen_cfg, monkeypatch):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    _force_joint(monkeypatch, significant=False)
    confirm_calls = []
    monkeypatch.setattr(pl.screen_stats, "confirm", lambda *a, **k: confirm_calls.append(a))
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT) == pl.OK
    trials = _screen_trials(store)
    assert len(trials) == N_A_CELLS and confirm_calls == []
    assert {t.params["period"] for t in trials} == {"discovery"}
    assert {t.params["group"] for t in trials} == {"dm", "em", "commodity"}
    assert any("insufficient names" in line and "14 cell(s)" in line for line in out)
    assert any("stops here" in line for line in out)
    for t in trials:
        note = json.loads(t.notes)
        assert note["joint"]["significant"] is False and note["discovered"] is False
        assert note["shrunk_ic"] is None
        assert (t.data_start, t.data_end) == SCREEN_DISC
        thin = t.params["group"] == "commodity"
        assert (note.get("note") == "insufficient names") is thin
        if thin:
            assert note["p"] is None and note["t"] is None


def test_screen_thin_cells_counted_in_bh_never_discovered(screen_cfg, monkeypatch):
    # ADR 0003 implementation notes: a cell with too few names keeps its place: p = NaN, it
    # counts in BH's m, is never discovered, and is logged noting "insufficient names". It is
    # left out of the joint test (no t to contribute).
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    _force_joint(monkeypatch, significant=True, p_cell={"A|quad1|dm|h5": 0.001})
    seen = {}
    real_joint, real_bh = pl.screen_stats.joint_test, pl.screen_stats.benjamini_hochberg

    def spy_joint(ic_by_cell, *rest):
        seen["joint_cells"] = set(ic_by_cell)
        return real_joint(ic_by_cell, *rest)

    def spy_bh(pvals, q):
        seen["bh"] = dict(pvals)
        return real_bh(pvals, q)

    monkeypatch.setattr(pl.screen_stats, "joint_test", spy_joint)
    monkeypatch.setattr(pl.screen_stats, "benjamini_hochberg", spy_bh)
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT) == pl.OK
    thin = {c for c in seen["bh"] if "|commodity|" in c}
    assert len(seen["bh"]) == N_A_CELLS and len(thin) == N_A_THIN
    assert all(math.isnan(seen["bh"][c]) for c in thin)
    assert seen["joint_cells"] == set(seen["bh"]) - thin
    disc = {t.params["cell"]: json.loads(t.notes) for t in _screen_trials(store, "discovery")}
    assert len(disc) == N_A_CELLS
    for c in thin:
        assert disc[c]["note"] == "insufficient names" and disc[c]["discovered"] is False
        assert disc[c]["p"] is None and disc[c]["shrunk_ic"] is None
    assert disc["A|quad1|dm|h5"]["discovered"] is True


def test_screen_refuses_n_boot_below_999(screen_cfg):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, out.append, n_boot=998) == pl.REFUSED
    assert "n_boot" in out[-1]
    assert store.read_trials("A") == []


def test_screen_other_bootstrap_settings_are_new_trials(screen_cfg, monkeypatch):
    # Trial identity includes n_boot, mean block, seed and the members hash: a rerun with any
    # other setting is a new, counted trial (ADR 0003 implementation notes).
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    _force_joint(monkeypatch, significant=False)
    pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT)
    first = {t.trial_id for t in store.read_trials("A")}
    pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT + 1)
    every = {t.trial_id for t in store.read_trials("A")}
    assert len(first) == N_A_CELLS and len(every) == 2 * N_A_CELLS


def test_screen_params_ids_and_signs(screen_cfg, monkeypatch):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    _force_joint(monkeypatch, significant=False)
    pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT)
    trials = {t.params["cell"]: t for t in _screen_trials(store)}
    t = trials["A|violence|em|h20"]
    assert t.params == {"cell": "A|violence|em|h20", "signal": "S3.violence", "group": "em",
                        "horizon": 20, "period": "discovery",
                        "features_version": FEATURES_VERSION, "expected_sign": -1.0,
                        "recipe": "sm5_z.60", "n_boot": N_BOOT, "mean_block": 40.0,
                        "seed": screen_cfg.seed, "members_hash": _members_hash(SCREEN_A["em"])}
    assert t.trial_id == pl.trial_id("signal", "A", t.params, *SCREEN_DISC)
    assert trials["A|quad1|dm|h5"].params["expected_sign"] == 1.0
    assert trials["A|quad1|dm|h5"].params["members_hash"] == _members_hash(SCREEN_A["dm"])
    assert trials["A|quad1|commodity|h5"].params["expected_sign"] is None  # two-sided
    note = json.loads(t.notes)
    # embargo: an h = 20 discovery label must end by the last discovery day
    assert note["feature_dates"][1] < SCREEN_DISC[1].isoformat()
    assert note["n_days"] > 100 and note["t"] is not None and note["p"] is not None


def test_screen_rerun_does_not_double_log(screen_cfg, monkeypatch):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    _force_joint(monkeypatch, significant=True,
                 p_cell={"A|quad1|dm|h5": 0.001, "A|violence|em|h20": 0.001})
    pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT)
    first = sorted(t.trial_id for t in store.read_trials("A"))
    out: list[str] = []
    pl.screen(screen_cfg, store, "A", lambda: NOW + timedelta(days=1), out.append, n_boot=N_BOOT)
    assert sorted(t.trial_id for t in store.read_trials("A")) == first
    assert len(first) == N_A_CELLS + 2
    assert f"0 new trial(s) logged ({N_A_CELLS + 2} already logged)" in out[-1]


def test_screen_confirms_only_discovered_cells(screen_cfg, monkeypatch):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    found = {"A|quad1|dm|h5", "A|violence|em|h20"}
    _force_joint(monkeypatch, significant=True, p_cell=dict.fromkeys(found, 0.001))
    seen = {}
    real = pl.screen_stats.confirm

    def spy(discovered, ic_conf, *rest):
        seen["cells"] = set(discovered)
        seen["ic_dates"] = {c: (s.index.min(), s.index.max()) for c, s in ic_conf.items()}
        return real(discovered, ic_conf, *rest)

    monkeypatch.setattr(pl.screen_stats, "confirm", spy)
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT) == pl.OK
    assert seen["cells"] == found
    for lo, hi in seen["ic_dates"].values():  # confirmation IC only on confirmation dates
        assert lo >= pd.Timestamp(SCREEN_CONF[0]) and hi <= pd.Timestamp(SCREEN_CONF[1])
    conf = _screen_trials(store, "confirmation")
    assert {t.params["cell"] for t in conf} == found
    assert all((t.data_start, t.data_end) == SCREEN_CONF for t in conf)
    disc = {t.params["cell"]: t for t in _screen_trials(store, "discovery")}
    assert len(disc) == N_A_CELLS
    for t in conf:
        note = json.loads(t.notes)
        assert note["discovery_trial"] == disc[t.params["cell"]].trial_id
        assert {"t", "p_one_sided", "holm_reject", "confirmed", "reason"} <= set(note)
        # every screen trial carries the bootstrap settings and the members hash
        d = disc[t.params["cell"]].params
        assert {k: t.params[k] for k in ("n_boot", "mean_block", "seed", "members_hash")} == {
            k: d[k] for k in ("n_boot", "mean_block", "seed", "members_hash")}
    for c, t in disc.items():
        note = json.loads(t.notes)
        assert note["discovered"] is (c in found)
        # EB ran because the joint test passed; thin (commodity) cells are not pooled
        assert (note["shrunk_ic"] is not None) is ("|commodity|" not in c)
    assert any("2 discovered" in line for line in out)


def test_screen_never_reads_holdout(screen_cfg, monkeypatch):
    inner = SqliteStore(screen_cfg.db_path)
    inner.init()
    _screen_track_a(inner)
    assert not inner.read_prices(None, SCREEN_HOLDOUT, None).empty  # hold-out rows exist
    store = RecordingStore(inner)
    seen_dates = []
    real_ic = pl.screen_stats.cell_ic

    def spy_ic(signal, labels, *a, **k):
        seen_dates.extend([signal["date"].max(), labels["date"].max()])
        return real_ic(signal, labels, *a, **k)

    monkeypatch.setattr(pl.screen_stats, "cell_ic", spy_ic)
    _force_joint(monkeypatch, significant=True, p_cell={"A|quad3|em|h5": 0.001})
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT) == pl.OK
    assert store.reads, "no dated read recorded"
    for _, start, end in store.reads:
        assert end is not None and end <= SCREEN_HOLDOUT
    assert max(d for d in seen_dates if pd.notna(d)) < pd.Timestamp(SCREEN_HOLDOUT)
    assert all(t.data_end < SCREEN_HOLDOUT for t in inner.read_trials("A"))


def test_screen_refuses_periods_reaching_the_holdout(screen_cfg, store, monkeypatch):
    monkeypatch.setattr(pl, "SCREEN_CONFIRMATION", (SCREEN_CONF[0], SCREEN_HOLDOUT))
    rc = pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT)
    assert rc == pl.REFUSED


def test_screen_reuses_stored_labels(screen_cfg, monkeypatch):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    prices = store.read_prices(None, screen_cfg.start, SCREEN_HOLDOUT)
    store.replace_labels(pl.labels.make_labels(prices, [1, 5, 20], pl._trading_days(prices)))
    monkeypatch.setattr(pl.labels, "make_labels", lambda *a, **k: pytest.fail("rebuilt labels"))
    _force_joint(monkeypatch, significant=False)
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT) == pl.OK
    assert "labels: stored dev labels reused" in out


def test_screen_track_b_real_joint_test(screen_cfg):
    # GICS sectors as groups; a 2-name sector keeps its cells, logged as "insufficient names".
    # No forced p-values: the real bootstrap runs end to end on gkg org rows.
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    sectors = {"Energy": 5, "Utilities": 5, "Materials": 2}
    assets = [Asset(ticker=f"{s[:2].upper()}{i}", name=f"{s} {i}", kind="stock", track="B",
                    group="sp500", org_aliases=[f"{s.lower()} co {i}"], screen_group=s)
              for s, k in sectors.items() for i in range(k)]
    store.upsert_assets(assets)
    store.upsert_prices(_bars([a.ticker for a in assets], *SCREEN_DATA, seed=5), "yfinance")
    rng = np.random.default_rng(6)
    rows = []
    for d in pd.date_range(*SCREEN_DATA, freq="D"):
        total = float(rng.integers(50_000, 60_000))
        for a in assets:
            k = float(rng.integers(1, 40))
            rows += [(d, "gkg", a.org_aliases[0], "org_docs", k, int(total)),
                     (d, "gkg", a.org_aliases[0], "org_tone", float(rng.normal(-1, 2)) * k, int(k)),
                     (d, "gkg", a.org_aliases[0], "org_neg_docs", float(rng.integers(0, k + 1)),
                      int(k))]
    store.upsert_gdelt_daily(pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS), "gkg_orgs_daily")
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "B", lambda: NOW, out.append, n_boot=N_BOOT) == pl.OK
    disc = [t for t in store.read_trials("B", "signal") if t.params["period"] == "discovery"]
    assert len(disc) == 3 * 3 * 2
    assert {t.params["group"] for t in disc} == {"Energy", "Utilities", "Materials"}
    assert {t.params["signal"]: t.params["expected_sign"] for t in disc} == {
        "S3.org_docs": None, "S3.org_neg_docs": -1.0, "S3.org_tone": 1.0}
    for t in disc:
        note = json.loads(t.notes)
        thin = t.params["group"] == "Materials"
        assert (note.get("note") == "insufficient names") is thin
        assert (note["p"] is None) is thin
    assert any("insufficient names" in line and "6 cell(s)" in line for line in out)


# --- S3 trial identity is unchanged by the A2 generalisation ----------------------------------
# Literal ids computed with the code as it was before `--set a2` existed (ADR 0003 trials
# already in the log must keep their ids, or a rerun would double-count them).
S3_GOLDEN = [
    (("violence", "S3.violence", "em", 20, -1.0, ("EM1", "EM2", "EM3", "EM4", "EM5")), "A",
     "signal-A-8093ec79d366", "signal-A-c33211715ce8"),
    (("quad1", "S3.quad1", "commodity", 5, None,
      ("CPER", "DBA", "GLD", "SLV", "UNG", "USO", "XLE")), "A",
     "signal-A-31789a3631d2", "signal-A-543ea3de5d83"),
    (("org_tone", "S3.org_tone", "Energy", 5, 1.0, ("XOM", "CVX")), "B",
     "signal-B-8844a907303f", "signal-B-f9b7bead22ba"),
]


@pytest.mark.parametrize("fields,track,disc_id,conf_id", S3_GOLDEN)
def test_s3_trial_ids_unchanged(fields, track, disc_id, conf_id):
    c = pl._ScreenCell(*fields)
    assert c.hypothesis == "S3" and c.themes_hash is None
    k = c.id(track)
    for period, window, expected in (("discovery", (date(2015, 3, 2), date(2019, 12, 31)),
                                      disc_id),
                                     ("confirmation", (date(2020, 1, 1), date(2025, 3, 31)),
                                      conf_id)):
        params = pl._screen_params(c, k, period, 2000, 20261004)
        assert set(params) == {"cell", "signal", "group", "horizon", "period",
                               "features_version", "expected_sign", "recipe", "n_boot",
                               "mean_block", "seed", "members_hash"}
        assert pl.trial_id("signal", track, params, *window) == expected
    assert (pl.SCREEN_DISCOVERY, pl.SCREEN_CONFIRMATION) == (
        (date(2015, 3, 2), date(2019, 12, 31)), (date(2020, 1, 1), date(2025, 3, 31)))


def test_s3_is_the_default_set(screen_cfg, monkeypatch):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_track_a(store)
    _force_joint(monkeypatch, significant=False)
    pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT)
    default = sorted(t.trial_id for t in store.read_trials("A"))
    pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT, screen_set="s3")
    assert sorted(t.trial_id for t in store.read_trials("A")) == default
    assert {t.hypothesis for t in store.read_trials("A")} == {"S3"}
    assert all("hypothesis" not in t.params for t in store.read_trials("A"))


# --- ADR 0004 theme screen (screen --set a2) --------------------------------------------------
# The real pinned tickers (ADR 0004 groups) with synthetic themes; rows on every calendar day of
# SCREEN_DATA under the gkg_themes_daily query name, so the dev period is complete.

A2_GROUPS = {"commodity": ["USO", "UNG", "GLD", "SLV", "CPER", "DBA", "XLE"],
             "sector": ["XLB", "XLC", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
                        "ITA"]}
A2_TICKERS = [t for m in A2_GROUPS.values() for t in m]


def _a2_assets(drop: str | None = None) -> list[Asset]:
    out = [Asset(ticker="TLT", name="TLT", kind="etf", track="A", group="rates",
                 gkg_themes=["TH_RATES"])]
    for g, tickers in A2_GROUPS.items():
        out += [Asset(ticker=t, name=t, kind="etf", track="A",
                      group="commodity" if g == "commodity" and t != "XLE" else "sector",
                      gkg_themes=[f"TH_{t}", "TH_SHARED"],
                      screen_group="commodity" if g == "commodity" else None)
                for t in tickers if t != drop]
    return out


def _theme_rows(start: date, end: date, themes: list[str], seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.date_range(start, end, freq="D"):
        total = int(rng.integers(100_000, 200_000))
        for th in themes:
            k = int(rng.integers(1, 300))
            rows += [(d, "gkg", "_GLOBAL", f"theme_{th}_docs", float(k), total),
                     (d, "gkg", "_GLOBAL", f"theme_{th}_tone", float(rng.normal(-1, 2)) * k, k)]
    return pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS)


def _screen_a2(store: SqliteStore, skip_month: tuple[int, int] | None = None,
               drop: str | None = None) -> None:
    assets = _a2_assets(drop)
    store.upsert_assets(assets)
    store.upsert_prices(_bars([a.ticker for a in assets], *SCREEN_DATA, seed=8), "yfinance")
    themes = sorted({t for a in assets for t in a.gkg_themes})
    rows = _theme_rows(*SCREEN_DATA, themes, seed=9)
    if skip_month is not None:
        sd = pd.to_datetime(rows["session_date"])
        rows = rows[~((sd.dt.year == skip_month[0]) & (sd.dt.month == skip_month[1]))]
    store.upsert_gdelt_daily(rows, "gkg_themes_daily")


def _a2_trials(store, period=None):
    return [t for t in store.read_trials("A", "signal") if t.hypothesis == "A2"
            and (period is None or t.params["period"] == period)]


def test_a2_refuses_until_every_dev_month_is_stored(screen_cfg):
    # ADR 0004 "Run condition": one missing dev month -> MISSING (exit 4), nothing logged, and
    # no GDELT value is read (the check uses stored session dates only).
    inner = SqliteStore(screen_cfg.db_path)
    inner.init()
    _screen_a2(inner, skip_month=(2023, 6))
    store = RecordingStore(inner)
    out: list[str] = []
    rc = pl.screen(screen_cfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT,
                   screen_set="a2")
    assert rc == pl.MISSING
    assert "1 dev month(s)" in out[-1] and "2023-06" in out[-1]
    assert [r for r in store.reads if r[0] == "gdelt_daily"] == []
    assert inner.read_trials("A") == []


def test_a2_month_missing_only_in_holdout_is_not_required(screen_cfg, monkeypatch):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_a2(store, skip_month=(2024, 8))  # after the hold-out start: not a dev month
    _force_joint(monkeypatch, significant=False)
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT,
                     screen_set="a2") == pl.OK


def test_pending_months_reads_session_dates_only(screen_cfg):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    rows = _theme_rows(date(2023, 1, 1), date(2023, 3, 30), ["X"], seed=1)  # March incomplete
    store.upsert_gdelt_daily(rows, "gkg_themes_daily")
    got = pl.pending_months(store, "gkg_themes_daily", date(2023, 1, 1), date(2023, 4, 30))
    assert got == [(date(2023, 3, 1), date(2023, 3, 31)), (date(2023, 4, 1), date(2023, 4, 30))]
    assert pl.pending_months(store, "gkg_orgs_daily", date(2023, 1, 1), date(2023, 1, 31)) == [
        (date(2023, 1, 1), date(2023, 1, 31))]


def test_a2_refuses_track_b(screen_cfg, store):
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "B", lambda: NOW, out.append, n_boot=N_BOOT,
                     screen_set="a2") == pl.REFUSED
    assert "Track A only" in out[-1]


def test_a2_refuses_without_every_pinned_member(screen_cfg):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_a2(store, drop="ITA")
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT,
                     screen_set="a2") == pl.MISSING
    assert "ITA" in out[-1]
    assert store.read_trials("A") == []


def test_a2_real_joint_test_logs_eight_cells(screen_cfg):
    # No forced p-values: the real bootstrap runs end to end on gkg theme rows.
    inner = SqliteStore(screen_cfg.db_path)
    inner.init()
    _screen_a2(inner)
    store = RecordingStore(inner)
    out: list[str] = []
    assert pl.screen(screen_cfg, store, "A", lambda: NOW, out.append, n_boot=N_BOOT,
                     screen_set="a2") == pl.OK
    for _, start, end in store.reads:
        assert end is not None and end <= SCREEN_HOLDOUT
    disc = _a2_trials(inner, "discovery")
    assert len(disc) == 8 and len(inner.read_trials("A")) == len(_a2_trials(inner))
    assert {t.params["cell"] for t in disc} == {
        f"A|{s}|{g}|h{h}" for s in ("attention", "tone") for g in ("commodity", "sector")
        for h in (5, 20)}
    signs = {(t.params["signal"], t.params["group"]): t.params["expected_sign"] for t in disc}
    assert signs == {("A2.attention", "commodity"): None, ("A2.attention", "sector"): None,
                     ("A2.tone", "commodity"): None, ("A2.tone", "sector"): 1.0}
    by_ticker = {a.ticker: a for a in _a2_assets()}
    for t in disc:
        g = t.params["group"]
        assert t.params["hypothesis"] == "A2"
        assert t.params["members_hash"] == _members_hash(A2_GROUPS[g])
        assert t.params["themes_hash"] == pl._themes_hash([by_ticker[x] for x in A2_GROUPS[g]])
        assert {k: t.params[k] for k in ("recipe", "n_boot", "mean_block", "seed",
                                         "features_version")} == {
            "recipe": "sm5_z.60", "n_boot": N_BOOT, "mean_block": 40.0,
            "seed": screen_cfg.seed, "features_version": FEATURES_VERSION}
        assert (t.data_start, t.data_end) == SCREEN_DISC
        note = json.loads(t.notes)
        assert note["note"] is None and note["p"] is not None and note["n_days"] > 100
    assert any("hypothesis A2" in line for line in out)


def test_a2_theme_mapping_is_part_of_trial_identity(screen_cfg, monkeypatch):
    store = SqliteStore(screen_cfg.db_path)
    store.init()
    _screen_a2(store)
    _force_joint(monkeypatch, significant=False)
    pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT, screen_set="a2")
    first = {t.trial_id for t in store.read_trials("A")}
    changed = [a.model_copy(update={"gkg_themes": ["TH_SHARED"]}) if a.ticker == "XLK" else a
               for a in _a2_assets()]
    store.upsert_assets(changed)
    pl.screen(screen_cfg, store, "A", lambda: NOW, _silent, n_boot=N_BOOT, screen_set="a2")
    every = {t.trial_id for t in store.read_trials("A")}
    assert len(first) == 8 and len(every) == 12  # 4 sector cells are new trials


EMBARGO_CASES = [
    # (trading days, period end, horizon) -> last feature date whose label ends by `end`
    (pd.bdate_range("2019-12-02", "2020-01-10"), date(2019, 12, 31), 5, date(2019, 12, 23)),
    (pd.bdate_range("2019-12-02", "2020-01-10"), date(2019, 12, 31), 1, date(2019, 12, 27)),
    # end on a non-trading day: the last trading day before it is the label's last open
    (pd.bdate_range("2019-12-02", "2020-01-10"), date(2019, 12, 29), 1, date(2019, 12, 25)),
]


@pytest.mark.parametrize("days,end,h,expected", EMBARGO_CASES)
def test_embargoed_end(days, end, h, expected):
    tdays = [d.date() for d in days]
    got = pl._embargoed_end(tdays, end, h)
    assert got == expected
    # the label of the returned date ends by `end`; the next date's label does not
    i = tdays.index(got)
    assert tdays[i + 1 + h] <= end < tdays[i + 2 + h]


JOINT_CASES = [
    # Bonferroni over the two statistics: min(p_S, p_M) < 0.025
    (0.024, 0.9, True),
    (0.9, 0.0249, True),
    (0.025, 0.025, False),
    (0.04, 0.04, False),  # both under 5% individually is not enough
    (float("nan"), 0.01, True),
    (float("nan"), float("nan"), False),
]


@pytest.mark.parametrize("p_s,p_m,expected", JOINT_CASES)
def test_joint_significant(p_s, p_m, expected):
    assert pl._joint_significant(p_s, p_m) is expected


def test_trial_id_is_deterministic():
    a = pl.trial_id("signal", "A", {"x": 1, "y": [1, 2]}, date(2020, 1, 1), date(2021, 1, 1))
    b = pl.trial_id("signal", "A", {"y": [1, 2], "x": 1}, date(2020, 1, 1), date(2021, 1, 1))
    c = pl.trial_id("signal", "A", {"x": 1, "y": [1, 2]}, date(2020, 1, 1),
                    date(2021, 1, 1) + timedelta(days=1))
    assert a == b != c and a.startswith("signal-A-")
