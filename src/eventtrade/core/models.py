"""Cross-sectional scoring models, score -> weight mapping, and out-of-sample stitching.

Frames used here (SCORE_COLUMNS and OOS_COLUMNS live in schemas.py; X/y are local):
  X       wide design matrix: columns ["date", "ticker", <feature>...], one row per (date, ticker).
  y       pandas Series of labels with the same index as X.
  scores  long frame with SCORE_COLUMNS = ["date", "ticker", "score"].

Every model standardises X cross-sectionally *per date* before fitting and predicting, so only the
ranking of names within a day is learned, not the level of a feature over time. Missing feature
values become 0 after standardisation (the day's mean). Seeds are parameters; nothing is random
otherwise.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy.stats import norm

from eventtrade.core.evaluate import newey_west_t, subperiod_signs
from eventtrade.schemas import OOS_COLUMNS, SCORE_COLUMNS

__all__ = [
    "OOS_COLUMNS",
    "SCORE_COLUMNS",
    "FittedModel",
    "fit_lgbm",
    "fit_logistic",
    "fit_ridge",
    "oos_predictions",
    "predict",
    "rule_score",
    "scores_to_weights",
    "select_features",
    "standardise_cross_section",
    "to_design",
]

_KEYS = ["date", "ticker"]

Standardise = Literal["zscore", "rankgauss"]
WeightMethod = Literal["rank_long_short", "top_bottom_k", "long_only_top_k"]


@dataclass(frozen=True)
class FittedModel:
    """A fitted estimator plus what is needed to apply it to new rows."""

    kind: Literal["ridge", "logistic", "lgbm"]
    estimator: Any
    features: tuple[str, ...]
    standardise: Standardise


# --- design matrix ------------------------------------------------------------------------------


def to_design(
    features: pd.DataFrame, labels: pd.DataFrame, horizon: int, label: str = "ret_oo",
    keep_unlabelled: bool = False,
) -> tuple[pd.DataFrame, pd.Series]:
    """Pivot long FEATURE_COLUMNS to wide X and join one LABEL_COLUMNS horizon as y.

    Default: inner join (rows without a label are dropped) -- for fitting only. For scoring, pass
    keep_unlabelled=True: every (date, ticker) with a feature stays, y is NaN where the label is
    missing. A label at t needs prices at t+1 and t+1+h, so dropping unlabelled rows would build
    day t's cross-section from future data availability (a later halt or gap removes the name).
    """
    wide = features.pivot_table(index=_KEYS, columns="feature", values="value", aggfunc="first")
    wide.columns = [str(c) for c in wide.columns]
    lab = labels.loc[labels["horizon"] == horizon].set_index(_KEYS)[label].dropna()
    how = "left" if keep_unlabelled else "inner"
    joined = wide.join(lab.rename("__y__"), how=how).sort_index().reset_index()
    y = joined.pop("__y__").astype(float)
    return joined, y


def select_features(
    ic: pd.DataFrame, dates: Sequence[Any], horizon: int, min_abs_t: float = 3.0
) -> list[str]:
    """Columns of a date x feature daily-IC table that pass the gate-1 IC test on `dates` only.

    Pass = |NW t| >= min_abs_t and the sign held in >= 2 of 3 sub-periods (the cost condition of
    gate 1 is not applied). Call it with a fold's training dates so the choice of model inputs
    never sees the fold's test period.
    """
    sub = ic.loc[ic.index.isin(list(dates))]
    out = []
    for col in sub.columns:
        s = sub[col].dropna()
        t = newey_west_t(s, horizon=horizon)
        if np.isnan(t) or abs(t) < min_abs_t:  # +/-inf (constant non-zero IC) passes
            continue
        held = sum(1 for m in subperiod_signs(s) if np.isfinite(m) and m * np.sign(t) > 0)
        if held >= 2:
            out.append(str(col))
    return out


def standardise_cross_section(X: pd.DataFrame, method: Standardise = "zscore") -> pd.DataFrame:
    """Standardise every feature column within each date (z-score or rank-gauss); NaN -> 0."""
    feats = _feature_cols(X)
    g = X.groupby("date", sort=False)[feats]
    if method == "zscore":
        mean = g.transform("mean")
        std = g.transform("std", ddof=0)
        z = (X[feats] - mean) / std.where(std > 0)
        # A constant cross-section carries no ranking information.
        z = z.where(std > 0, 0.0)
    elif method == "rankgauss":
        rank = g.rank(method="average")
        count = g.transform("count")
        z = pd.DataFrame(norm.ppf((rank - 0.5) / count), index=X.index, columns=feats)
    else:
        raise ValueError(f"unknown standardisation {method!r}")
    out = X[_KEYS].copy()
    out[feats] = z.fillna(0.0).astype(float)
    return out


# --- models -------------------------------------------------------------------------------------


def rule_score(features_wide: pd.DataFrame, feature: str, sign: float) -> pd.DataFrame:
    """Baseline: the per-date z-score of one feature times sign, as SCORE_COLUMNS."""
    z = standardise_cross_section(features_wide[[*_KEYS, feature]], "zscore")
    return z[_KEYS].assign(score=float(np.sign(sign)) * z[feature]).reset_index(drop=True)


def fit_ridge(X: pd.DataFrame, y: pd.Series, alpha: float, standardise: Standardise = "zscore") -> FittedModel:
    """Ridge regression of y on cross-sectionally standardised X."""
    from sklearn.linear_model import Ridge

    Z, yv = _prep(X, y, standardise)
    return FittedModel("ridge", Ridge(alpha=alpha).fit(Z, yv), tuple(_feature_cols(X)), standardise)


def fit_logistic(X: pd.DataFrame, y_sign: pd.Series, C: float, standardise: Standardise = "zscore") -> FittedModel:
    """L2 logistic regression of 1[y > 0] on standardised X; scores are decision-function values."""
    from sklearn.linear_model import LogisticRegression

    Z, yv = _prep(X, y_sign, standardise)
    target = (yv > 0).astype(int)
    if target.min() == target.max():
        raise ValueError("logistic needs both positive and non-positive labels")
    est = LogisticRegression(C=C, max_iter=1000).fit(Z, target)
    return FittedModel("logistic", est, tuple(_feature_cols(X)), standardise)


def fit_lgbm(
    X: pd.DataFrame, y: pd.Series, params: dict, seed: int, standardise: Standardise = "rankgauss"
) -> FittedModel:
    """LightGBM regressor on standardised X; single-threaded and deterministic for a given seed."""
    from lightgbm import LGBMRegressor

    Z, yv = _prep(X, y, standardise)
    fixed = {"random_state": seed, "n_jobs": 1, "deterministic": True, "force_row_wise": True, "verbose": -1}
    est = LGBMRegressor(**{**params, **fixed}).fit(Z, yv)
    return FittedModel("lgbm", est, tuple(_feature_cols(X)), standardise)


def predict(model: FittedModel, X: pd.DataFrame) -> pd.DataFrame:
    """Score rows of X with a fitted model, as SCORE_COLUMNS."""
    missing = set(model.features) - set(X.columns)
    if missing:
        raise ValueError(f"X lacks features the model was fitted on: {sorted(missing)}")
    Z = standardise_cross_section(X[[*_KEYS, *model.features]], model.standardise)
    arr = Z[list(model.features)].to_numpy()
    if model.kind == "logistic":
        s = model.estimator.decision_function(arr)
    else:
        s = model.estimator.predict(arr)
    return Z[_KEYS].assign(score=np.asarray(s, dtype=float)).reset_index(drop=True)


# --- out-of-sample stitching --------------------------------------------------------------------


def oos_predictions(
    X: pd.DataFrame,
    y: pd.Series,
    splits: Sequence[Any],
    fit_fn: Callable[[pd.DataFrame, pd.Series], Any],
    predict_fn: Callable[[Any, pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    """Fit on each split's train dates, score its test dates; OOS_COLUMNS, one row per (date, ticker, path).

    `splits` are cv.Split values (train, test, path); for walk-forward every path is 0.
    """
    if not y.index.equals(X.index):
        raise ValueError("y must have the same index as X")
    parts = []
    for sp in splits:
        tr = X["date"].isin(sp.train)
        te = X["date"].isin(sp.test)
        if not tr.any() or not te.any():
            continue
        model = fit_fn(X.loc[tr], y.loc[tr])
        pred = predict_fn(model, X.loc[te])
        path_of = pd.Series(sp.path, index=pd.Index(sp.test, name="date"), name="path")
        pred = pred.join(path_of, on="date")
        parts.append(pred[OOS_COLUMNS])
    if not parts:
        return pd.DataFrame(columns=OOS_COLUMNS)
    out = pd.concat(parts, ignore_index=True)
    out["path"] = out["path"].astype(int)
    return out.sort_values(["path", "date", "ticker"], ignore_index=True)


# --- scores -> weights --------------------------------------------------------------------------


def scores_to_weights(
    scores: pd.DataFrame,
    method: WeightMethod = "rank_long_short",
    k: int | None = None,
    gross: float = 1.0,
    max_weight: float | None = None,
) -> pd.DataFrame:
    """Per-date target weights (WEIGHT_COLUMNS); long-short methods are dollar-neutral, sum |w| = gross.

    rank_long_short: w proportional to (rank - mean rank). top_bottom_k: equal weight long the k
    highest, short the k lowest (k shrinks to n // 2 on thin days). long_only_top_k: equal weight
    long the k highest, sum w = gross. Each side gets gross/2 (long-only: gross); `max_weight` caps
    |w| and the excess is redistributed within the side, so a side whose names cannot absorb it
    ends below its budget. Ties are broken by ticker. NaN scores are dropped; names not selected
    get weight 0; dates with fewer than 2 names (long-short) get all-zero weights.
    """
    if method in ("top_bottom_k", "long_only_top_k") and (k is None or k < 1):
        raise ValueError(f"{method} needs k >= 1")
    if gross <= 0 or (max_weight is not None and max_weight <= 0):
        raise ValueError("gross and max_weight must be positive")
    s = scores.dropna(subset=["score"]).sort_values(["date", "score", "ticker"], ignore_index=True)
    parts = []
    for _, day in s.groupby("date", sort=True):
        w = _day_weights(day["score"].to_numpy(dtype=float), method, k, gross, max_weight)
        parts.append(day[_KEYS].assign(weight=w))
    if not parts:
        return pd.DataFrame(columns=["date", "ticker", "weight"])
    return pd.concat(parts, ignore_index=True).sort_values(_KEYS, ignore_index=True)


def _day_weights(sc: np.ndarray, method: str, k: int | None, gross: float, cap: float | None) -> np.ndarray:
    """Weights for one date; `sc` is sorted ascending (ties already ordered by ticker)."""
    n = len(sc)
    w = np.zeros(n)
    if method == "long_only_top_k":
        m = min(k, n)
        w[n - m :] = _water_fill(np.ones(m), gross, cap)
        return w
    if n < 2:
        return w
    if method == "rank_long_short":
        r = pd.Series(sc).rank(method="average").to_numpy()
        raw = r - r.mean()
        long, short = raw > 0, raw < 0
        if long.any():
            w[long] = _water_fill(raw[long], gross / 2, cap)
            w[short] = -_water_fill(-raw[short], gross / 2, cap)
        return w
    if method == "top_bottom_k":
        m = min(k, n // 2)
        w[n - m :] = _water_fill(np.ones(m), gross / 2, cap)
        w[:m] = -_water_fill(np.ones(m), gross / 2, cap)
        return w
    raise ValueError(f"unknown weighting method {method!r}")


def _water_fill(strength: np.ndarray, budget: float, cap: float | None) -> np.ndarray:
    """Split `budget` proportionally to positive `strength`, capping each at `cap` and refilling."""
    w = budget * strength / strength.sum()
    if cap is None:
        return w
    capped = np.zeros(len(w), dtype=bool)
    for _ in range(len(w)):
        over = (w > cap + 1e-12) & ~capped
        if not over.any():
            break
        capped |= over
        w[capped] = cap
        left = budget - cap * capped.sum()
        free = ~capped
        if left <= 0 or not free.any():
            w[free] = np.minimum(w[free], cap) if left > 0 else 0.0
            break
        w[free] = left * strength[free] / strength[free].sum()
    return np.minimum(w, cap)


def _feature_cols(X: pd.DataFrame) -> list[str]:
    return [c for c in X.columns if c not in _KEYS]


def _prep(X: pd.DataFrame, y: pd.Series, standardise: Standardise) -> tuple[np.ndarray, np.ndarray]:
    if not y.index.equals(X.index):
        raise ValueError("y must have the same index as X")
    ok = y.notna().to_numpy()
    Z = standardise_cross_section(X, standardise)
    return Z[_feature_cols(X)].to_numpy()[ok], y.to_numpy(dtype=float)[ok]
