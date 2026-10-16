"""ADR 0018 (forecasting the direction of the earnings reaction, hypothesis family W): yearly
walk-forward splits with the label-availability rule, train-fitted imputation and scaling, the two
fixed models, rule baselines, and the P&L statistics. Pure: no I/O."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from eventtrade.core.evaluate import newey_west_t

COST = 0.0030
BORROW_ANNUAL = 0.0050
HOLD_DAYS = 3
C_LOGIT = 1.0
HGB = {"max_depth": 3, "learning_rate": 0.05, "max_iter": 200, "min_samples_leaf": 200}


def yearly_splits(decision: pd.Series, label_date: pd.Series, years: Sequence[int],
                  first_train_year: int = 2016) -> list[tuple[int, np.ndarray, np.ndarray]]:
    """[(Y, train_idx, test_idx)]: test = events whose decision date is in year Y; train = events
    with decision year >= first_train_year whose label date (d+1) is strictly before the first
    decision date of year Y (amendment 5)."""
    dec = pd.to_datetime(decision).reset_index(drop=True)
    lab = pd.to_datetime(label_date).reset_index(drop=True)
    out = []
    for y in years:
        test = np.flatnonzero(dec.dt.year.to_numpy() == y)
        if not len(test):
            continue
        first = dec.iloc[test].min()
        train = np.flatnonzero((dec.dt.year.to_numpy() >= first_train_year) & (lab < first).to_numpy())
        out.append((int(y), train, test))
    return out


@dataclass
class Prep:
    med: np.ndarray
    mu: np.ndarray
    sd: np.ndarray


def fit_prep(x: np.ndarray) -> Prep:
    """Train-set medians (imputation), then mean/sd of the imputed train matrix."""
    med = np.nanmedian(x, axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    xi = np.where(np.isfinite(x), x, med)
    mu, sd = xi.mean(axis=0), xi.std(axis=0)
    return Prep(med, mu, np.where(sd > 0, sd, 1.0))


def apply_prep(p: Prep, x: np.ndarray) -> np.ndarray:
    """Impute with train medians, standardize, append missing flags (one per input column)."""
    miss = ~np.isfinite(x)
    xi = np.where(miss, p.med, x)
    return np.hstack([(xi - p.mu) / p.sd, miss.astype(float)])


def weights(r: np.ndarray) -> np.ndarray:
    """|R| clipped at its 99th percentile, rescaled to mean 1 (amendment 6)."""
    a = np.abs(np.asarray(r, dtype=float))
    a = np.minimum(a, np.nanquantile(a, 0.99))
    return a / a.mean() if a.mean() > 0 else np.ones(len(a))


def fit_predict(model: str, x_tr: np.ndarray, y_tr: np.ndarray, w_tr: np.ndarray,
                x_te: np.ndarray, seed: int) -> np.ndarray:
    """Predicted sign (+1 / -1) on the test rows. model 'L' (weighted logistic, C = 1) or 'G'
    (histogram gradient boosting, fixed settings, no early stopping)."""
    p = fit_prep(x_tr)
    a_tr, a_te = apply_prep(p, x_tr), apply_prep(p, x_te)
    if model == "L":
        from sklearn.linear_model import LogisticRegression

        m = LogisticRegression(C=C_LOGIT, max_iter=2000)
    elif model == "G":
        from sklearn.ensemble import HistGradientBoostingClassifier

        m = HistGradientBoostingClassifier(early_stopping=False, random_state=seed, **HGB)
    else:
        raise ValueError(f"unknown model {model!r}")
    m.fit(a_tr, y_tr, sample_weight=w_tr)
    return np.where(m.predict_proba(a_te)[:, 1] >= 0.5, 1, -1)


def rule_predictions(feat: pd.DataFrame) -> dict[str, np.ndarray]:
    """W-R0 always long; W-R1 sign of the previous reaction; W-R2 sign of the [d-63, d-2]
    return; W-R3 majority of the last 4 reaction signs (ties and missing -> long)."""
    n = len(feat)

    def sgn(v: np.ndarray) -> np.ndarray:
        return np.where(np.nan_to_num(v) < 0, -1, 1)

    signs = feat[["prev_sign_1", "prev_sign_2", "prev_sign_3", "prev_sign_4"]].to_numpy()
    vote = np.nansum(signs, axis=1)
    return {"W-R0": np.ones(n, dtype=int), "W-R1": sgn(feat["prev_sign_1"].to_numpy()),
            "W-R2": sgn(feat["ret_63"].to_numpy()), "W-R3": sgn(vote)}


def net_pnl(pred: np.ndarray, r: np.ndarray, cost: float = COST) -> np.ndarray:
    """pred x R - round trip - borrow on shorts (50 bp/yr over the 3-day hold)."""
    pred = np.asarray(pred, dtype=float)
    borrow = np.where(pred < 0, BORROW_ANNUAL * HOLD_DAYS / 252, 0.0)
    return pred * np.asarray(r, dtype=float) - cost - borrow


def week_block_lower(dates: Sequence, x: np.ndarray, n_boot: int, seed: int,
                     level: float = 0.95) -> tuple[float, float]:
    """(one-sided lower bound, bootstrap p of mean <= 0): calendar weeks resampled (amendment 2)."""
    x = np.asarray(x, dtype=float)
    d = pd.to_datetime(pd.Series(list(dates)))
    ok = np.isfinite(x) & d.notna().to_numpy()
    x, d = x[ok], d[ok]
    if len(x) < 2:
        return float("nan"), float("nan")
    wk = d.dt.to_period("W").astype(str).to_numpy()
    uniq, inv = np.unique(wk, return_inverse=True)
    s = np.bincount(inv, weights=x)
    c = np.bincount(inv).astype(float)
    pick = np.random.default_rng(seed).integers(len(uniq), size=(n_boot, len(uniq)))
    means = s[pick].sum(axis=1) / c[pick].sum(axis=1)
    return float(np.quantile(means, 1 - level)), float((1 + np.sum(means <= 0)) / (1 + n_boot))


def calendar_t(dates: Sequence, x: np.ndarray, lags: int = 5) -> float:
    """NW t of the per-decision-date mean P&L series."""
    s = pd.Series(np.asarray(x, dtype=float), index=pd.to_datetime(pd.Series(list(dates))))
    s = s[np.isfinite(s.to_numpy())].groupby(level=0).mean().sort_index()
    return float(newey_west_t(s, lags=lags)) if len(s) > 2 else float("nan")


def weighted_hit(pred: np.ndarray, r: np.ndarray) -> float:
    a = np.abs(np.asarray(r, dtype=float))
    ok = np.isfinite(a)
    hit = (np.sign(np.asarray(r)[ok]) == np.asarray(pred)[ok])
    return float((a[ok] * hit).sum() / a[ok].sum()) if a[ok].sum() > 0 else float("nan")
