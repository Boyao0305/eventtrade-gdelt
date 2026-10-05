"""Case tables for core/models.py."""

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.cv import purged_splits, walk_forward_splits
from eventtrade.core.models import (
    OOS_COLUMNS,
    SCORE_COLUMNS,
    fit_lgbm,
    fit_logistic,
    fit_ridge,
    oos_predictions,
    predict,
    rule_score,
    scores_to_weights,
    select_features,
    standardise_cross_section,
    to_design,
)
from eventtrade.schemas import WEIGHT_COLUMNS

D1, D2 = pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-03")
NAN = float("nan")
Z3 = np.sqrt(1.5)  # z-score of the ends of [1, 2, 3] with ddof=0


def _weights(scores: dict, **kw) -> dict:
    df = pd.DataFrame({"date": D1, "ticker": list(scores), "score": list(scores.values())})
    out = scores_to_weights(df, **kw)
    assert list(out.columns) == WEIGHT_COLUMNS
    return {t: round(w, 12) for t, w in zip(out["ticker"], out["weight"])}


# --- scores_to_weights: exact weights for small cross-sections -------------------------------
WEIGHT_CASES = [
    # rank_long_short: w ∝ rank - mean rank, each side gets gross/2.
    ({"A": 1, "B": 2, "C": 3, "D": 4}, {}, {"A": -0.375, "B": -0.125, "C": 0.125, "D": 0.375}),
    ({"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}, {},
     {"A": 2 / 6, "B": 1 / 6, "C": 0.0, "D": -1 / 6, "E": -2 / 6}),
    # ties share the average rank
    ({"A": 1, "B": 1, "C": 2, "D": 2}, {}, {"A": -0.25, "B": -0.25, "C": 0.25, "D": 0.25}),
    # gross scales everything
    ({"A": 1, "B": 2, "C": 3, "D": 4}, {"gross": 2.0}, {"A": -0.75, "B": -0.25, "C": 0.25, "D": 0.75}),
    # max_weight caps |w| and hands the excess to the rest of the same side
    ({"A": 1, "B": 2, "C": 3, "D": 4}, {"max_weight": 0.3}, {"A": -0.3, "B": -0.2, "C": 0.2, "D": 0.3}),
    # all scores equal: no information, no position
    ({"A": 1, "B": 1, "C": 1}, {}, {"A": 0.0, "B": 0.0, "C": 0.0}),
    # one name: cannot be dollar-neutral
    ({"A": 1}, {}, {"A": 0.0}),
    # NaN scores are dropped from the output
    ({"A": 1, "B": NAN, "C": 3}, {}, {"A": -0.5, "C": 0.5}),

    # top_bottom_k: equal weight, gross/2 per side, middle names at 0.
    ({"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}, {"method": "top_bottom_k", "k": 2},
     {"A": -0.25, "B": -0.25, "C": 0.0, "D": 0.0, "E": 0.25, "F": 0.25}),
    # k larger than half the names shrinks to n // 2
    ({"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}, {"method": "top_bottom_k", "k": 3},
     {"A": -0.25, "B": -0.25, "C": 0.0, "D": 0.25, "E": 0.25}),
    # ties broken by ticker: lower ticker sorts lower
    ({"A": 1, "B": 1, "C": 2, "D": 2}, {"method": "top_bottom_k", "k": 1},
     {"A": -0.5, "B": 0.0, "C": 0.0, "D": 0.5}),
    # cap below the equal weight: the side cannot spend its budget
    ({"A": 1, "B": 2, "C": 3, "D": 4}, {"method": "top_bottom_k", "k": 1, "max_weight": 0.2},
     {"A": -0.2, "B": 0.0, "C": 0.0, "D": 0.2}),

    # long_only_top_k: equal weight on the k highest, sum = gross.
    ({"A": 1, "B": 2, "C": 3, "D": 4}, {"method": "long_only_top_k", "k": 2},
     {"A": 0.0, "B": 0.0, "C": 0.5, "D": 0.5}),
    ({"A": 1, "B": 2, "C": 3, "D": 4}, {"method": "long_only_top_k", "k": 2, "max_weight": 0.4},
     {"A": 0.0, "B": 0.0, "C": 0.4, "D": 0.4}),
    ({"A": 1}, {"method": "long_only_top_k", "k": 3}, {"A": 1.0}),
]


@pytest.mark.parametrize("scores,kw,expected", WEIGHT_CASES)
def test_scores_to_weights(scores, kw, expected):
    got = _weights(scores, **kw)
    assert got == pytest.approx({t: round(w, 12) for t, w in expected.items()})


def test_weights_are_per_date_and_dollar_neutral():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"date": np.repeat([D1, D2], 6), "ticker": list("ABCDEF") * 2,
                       "score": rng.normal(size=12)})
    for method, kw in [("rank_long_short", {}), ("top_bottom_k", {"k": 2})]:
        w = scores_to_weights(df, method=method, **kw)
        by = w.groupby("date")["weight"]
        assert by.sum().abs().max() < 1e-12
        assert np.allclose(by.apply(lambda s: s.abs().sum()), 1.0)


@pytest.mark.parametrize("kw", [{"method": "top_bottom_k"}, {"method": "long_only_top_k", "k": 0},
                                {"gross": 0.0}, {"method": "nope"}])
def test_scores_to_weights_rejects(kw):
    df = pd.DataFrame({"date": D1, "ticker": ["A", "B"], "score": [1.0, 2.0]})
    with pytest.raises(ValueError):
        scores_to_weights(df, **kw)


# --- standardisation is per date: only the within-day ranking survives -----------------------
STD_CASES = [
    # (method, {date: [x...]}, {date: [expected...]})
    ("zscore", {D1: [1, 2, 3]}, {D1: [-Z3, 0, Z3]}),
    # level and scale of a day do not matter
    ("zscore", {D1: [1, 2, 3], D2: [110, 120, 130]}, {D1: [-Z3, 0, Z3], D2: [-Z3, 0, Z3]}),
    # constant day -> 0; missing value -> 0 (the day's mean)
    ("zscore", {D1: [5, 5], D2: [1, NAN, 3]}, {D1: [0, 0], D2: [-1, 0, 1]}),
    # rank-gauss: normal quantile of (rank - 0.5) / n
    ("rankgauss", {D1: [5, 1, 3]}, {D1: [0.967421566, -0.967421566, 0.0]}),
    ("rankgauss", {D1: [7], D2: [2, 2]}, {D1: [0.0], D2: [0.0, 0.0]}),
]


@pytest.mark.parametrize("method,days,expected", STD_CASES)
def test_standardise_cross_section(method, days, expected):
    rows = [(d, f"T{i}", x) for d, xs in days.items() for i, x in enumerate(xs)]
    X = pd.DataFrame(rows, columns=["date", "ticker", "x"])
    out = standardise_cross_section(X, method)
    assert list(out.columns) == ["date", "ticker", "x"]
    want = [v for d in days for v in expected[d]]
    assert out["x"].to_numpy() == pytest.approx(want, abs=1e-8)


# --- rule_score: signed z-score of one feature -------------------------------------------------
@pytest.mark.parametrize("sign,expected", [(1, [-Z3, 0, Z3]), (-1, [Z3, 0, -Z3]), (-2.5, [Z3, 0, -Z3])])
def test_rule_score(sign, expected):
    X = pd.DataFrame({"date": D1, "ticker": ["A", "B", "C"], "x": [1.0, 2.0, 3.0], "other": [9.0, 0.0, 1.0]})
    out = rule_score(X, "x", sign)
    assert list(out.columns) == SCORE_COLUMNS
    assert out["score"].to_numpy() == pytest.approx(expected)


# --- models: fitted on standardised X, so per-date affine changes of X do not move scores -----
def _panel(n_days=40, n_names=8, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    X = pd.DataFrame({"date": np.repeat(dates, n_names), "ticker": [f"T{i}" for i in range(n_names)] * n_days,
                      "x1": rng.normal(size=n_days * n_names), "x2": rng.normal(size=n_days * n_names)})
    y = pd.Series(0.02 * X["x1"] + 0.001 * rng.normal(size=len(X)), index=X.index)
    return X, y


FITS = {
    "ridge": lambda X, y: fit_ridge(X, y, alpha=1.0),
    "logistic": lambda X, y: fit_logistic(X, y, C=1.0),
    "lgbm": lambda X, y: fit_lgbm(X, y, {"n_estimators": 30, "num_leaves": 4, "min_child_samples": 5}, seed=1),
}


@pytest.mark.parametrize("name", list(FITS))
def test_model_learns_the_ranking_and_ignores_day_levels(name):
    X, y = _panel()
    model = FITS[name](X, y)
    pred = predict(model, X)
    assert list(pred.columns) == SCORE_COLUMNS and len(pred) == len(X)
    assert np.corrcoef(pred["score"], X["x1"])[0, 1] > 0.8
    shifted = X.copy()
    day = shifted["date"].rank(method="dense")
    shifted["x1"] = shifted["x1"] * day + 100 * day  # per-date scale and level
    assert predict(model, shifted)["score"].to_numpy() == pytest.approx(pred["score"].to_numpy())


def test_lgbm_is_deterministic_for_a_seed():
    X, y = _panel()
    params = {"n_estimators": 20, "num_leaves": 4, "subsample": 0.6, "subsample_freq": 1, "colsample_bytree": 0.5}
    a = predict(fit_lgbm(X, y, params, seed=7), X)["score"].to_numpy()
    b = predict(fit_lgbm(X, y, params, seed=7), X)["score"].to_numpy()
    c = predict(fit_lgbm(X, y, params, seed=8), X)["score"].to_numpy()
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


def test_predict_rejects_missing_feature():
    X, y = _panel()
    model = fit_ridge(X, y, 1.0)
    with pytest.raises(ValueError):
        predict(model, X.drop(columns="x2"))


# --- oos_predictions: test dates only, fitted on train dates only, one row per path ----------
def _spy_fit(seen):
    def fit(X, y):
        seen.append(set(X["date"]))
        return set(X["date"])
    return fit


def _spy_predict(train_dates, X):
    assert not train_dates & set(X["date"])  # never scores a date it trained on
    return X[["date", "ticker"]].assign(score=1.0)


def test_oos_walk_forward():
    X, y = _panel(n_days=20, n_names=3)
    dates = sorted(X["date"].unique())
    splits = walk_forward_splits(dates, train_days=8, test_days=5, purge=2, embargo=0, expanding=False)
    seen = []
    out = oos_predictions(X, y, splits, _spy_fit(seen), _spy_predict)
    assert list(out.columns) == OOS_COLUMNS
    assert sorted(out["date"].unique()) == dates[10:]
    assert (out["path"] == 0).all() and not out.duplicated(["date", "ticker"]).any()
    assert seen[0] == set(dates[0:8])


def test_oos_cpcv_every_path_covers_every_row_once():
    X, y = _panel(n_days=30, n_names=4)
    dates = sorted(X["date"].unique())
    splits = purged_splits(dates, n_folds=5, n_test_folds=2, purge=1, embargo=1)
    out = oos_predictions(X, y, splits, _spy_fit([]), _spy_predict)
    assert sorted(out["path"].unique()) == [0, 1, 2, 3]  # C(5,2) * 2 / 5 paths
    assert not out.duplicated(["date", "ticker", "path"]).any()
    assert (out.groupby("path").size() == len(X)).all()


def test_oos_with_real_model_scores_every_test_row():
    X, y = _panel(n_days=30, n_names=4)
    dates = sorted(X["date"].unique())
    splits = purged_splits(dates, n_folds=3, n_test_folds=1, purge=2, embargo=0)
    out = oos_predictions(X, y, splits, lambda a, b: fit_ridge(a, b, 1.0), predict)
    assert len(out) == len(X) and out["score"].notna().all()


def test_to_design_pivots_and_joins_one_horizon():
    feats = pd.DataFrame({"date": [D1, D1, D1, D2], "ticker": ["A", "A", "B", "A"],
                          "feature": ["f", "g", "f", "f"], "value": [1.0, 2.0, 3.0, 4.0]})
    labels = pd.DataFrame({"date": [D1, D1, D1, D2], "ticker": ["A", "B", "A", "A"], "horizon": [1, 1, 5, 1],
                           "ret_oo": [0.1, 0.2, 0.5, NAN], "ret_overnight": 0.0, "ret_intraday": 0.0})
    X, y = to_design(feats, labels, horizon=1)
    assert list(X.columns) == ["date", "ticker", "f", "g"]
    assert X[["ticker", "f"]].values.tolist() == [["A", 1.0], ["B", 3.0]]  # D2 dropped: label NaN
    assert np.isnan(X.loc[1, "g"])
    assert y.tolist() == [0.1, 0.2] and y.index.equals(X.index)


# --- to_design(keep_unlabelled=True): scoring rows never depend on a future label --------------
# Review regression: a name whose label was missing (its bar at t+1+h absent) vanished from day
# t's design, which also changed every other name's cross-sectional z-score.
def test_to_design_keep_unlabelled():
    feats = pd.DataFrame({"date": [D1] * 3, "ticker": ["A", "B", "C"], "feature": "f",
                          "value": [1.0, 2.0, 3.0]})
    labels = pd.DataFrame({"date": [D1, D1], "ticker": ["A", "B"], "horizon": 5,
                           "ret_oo": [0.1, 0.2], "ret_overnight": 0.0, "ret_intraday": 0.0})
    X_fit, _ = to_design(feats, labels, horizon=5)
    X, y = to_design(feats, labels, horizon=5, keep_unlabelled=True)
    assert X_fit["ticker"].tolist() == ["A", "B"]
    assert X["ticker"].tolist() == ["A", "B", "C"] and np.isnan(y.iloc[2])
    # A's z-score is computed with C in the cross-section: -sqrt(1.5), not -1.
    z = standardise_cross_section(X)
    assert z.loc[z["ticker"] == "A", "f"].item() == pytest.approx(-Z3)


# --- select_features: the gate-1 IC test restricted to the given dates -------------------------
_SEL_DATES = pd.bdate_range("2020-01-01", periods=90)
_SEL_IC = pd.DataFrame({
    "steady": np.tile([0.10, 0.12, 0.08], 30),
    "zero": np.tile([0.1, -0.1, 0.0], 30),
    "late": [0.05, -0.06, 0.0] * 20 + [0.3] * 30,  # weak and negative, then strong at the end
}, index=_SEL_DATES)

SELECT_CASES = [
    # Whole period: 'late' has |t| 3.3 but holds its sign in 1 of 3 sub-periods -> fails.
    ("all dates", _SEL_DATES, ["steady"]),
    # First 60 dates: 'late' is weak (t -1.2) -> fails; 'zero' never passes.
    ("first 60", _SEL_DATES[:60], ["steady"]),
    # Last 30 dates: 'late' is a constant 0.3 (t = +inf, sign held) -> passes.
    ("last 30", _SEL_DATES[60:], ["late", "steady"]),
    # No dates: nothing passes.
    ("empty", _SEL_DATES[:0], []),
]


@pytest.mark.parametrize("name,dates,expected", SELECT_CASES, ids=[c[0] for c in SELECT_CASES])
def test_select_features(name, dates, expected):
    assert sorted(select_features(_SEL_IC, dates, horizon=1)) == expected
