"""Orchestration: fake GDELT and price sources, a real SqliteStore on a tmp file."""

import json
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


def test_trial_id_is_deterministic():
    a = pl.trial_id("signal", "A", {"x": 1, "y": [1, 2]}, date(2020, 1, 1), date(2021, 1, 1))
    b = pl.trial_id("signal", "A", {"y": [1, 2], "x": 1}, date(2020, 1, 1), date(2021, 1, 1))
    c = pl.trial_id("signal", "A", {"x": 1, "y": [1, 2]}, date(2020, 1, 1),
                    date(2021, 1, 1) + timedelta(days=1))
    assert a == b != c and a.startswith("signal-A-")
