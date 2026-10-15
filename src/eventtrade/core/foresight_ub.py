"""ADR 0016 (value of foresight, hypothesis family U, look-ahead diagnostic): break-even hit
rates, month-block bootstrap of event-minus-placebo differences, window returns and matched pool
means. Pure: no I/O."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

N_BOOT = 10_000


def q_star(abs_vals: np.ndarray, cost: float) -> dict[str, float]:
    """|R|-weighted break-even accuracy (1 + c / scale) / 2 with scale = mean, median and the
    1%/99%-winsorized mean of |R| (amendment 1)."""
    a = np.abs(np.asarray(abs_vals, dtype=float))
    a = a[np.isfinite(a)]
    if not len(a):
        nan = float("nan")
        return {"q_mean": nan, "q_median": nan, "q_wins": nan}
    lo, hi = np.quantile(a, [0.01, 0.99])
    out = {}
    for k, s in (("q_mean", a.mean()), ("q_median", float(np.median(a))),
                 ("q_wins", np.clip(a, lo, hi).mean())):
        out[k] = float((1 + cost / s) / 2) if s > 0 else float("nan")
    return out


def month_block_ci(dates: Sequence, x: np.ndarray, n_boot: int, seed: int,
                   level: float = 0.95) -> tuple[float, float]:
    """CI of mean(x) by resampling calendar months with replacement (amendment 3)."""
    x = np.asarray(x, dtype=float)
    d = pd.to_datetime(pd.Series(list(dates)))
    ok = np.isfinite(x) & d.notna().to_numpy()
    x, d = x[ok], d[ok]
    if len(x) < 2:
        return float("nan"), float("nan")
    months = d.dt.year.to_numpy() * 12 + d.dt.month.to_numpy()
    uniq, inv = np.unique(months, return_inverse=True)
    sums = np.bincount(inv, weights=x)
    cnts = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(len(uniq), size=(n_boot, len(uniq)))
    s, c = sums[pick].sum(axis=1), cnts[pick].sum(axis=1)
    means = s / c
    a = (1 - level) / 2
    lo, hi = np.quantile(means, [a, 1 - a])
    return float(lo), float(hi)


def hedged_window(c: np.ndarray, h: np.ndarray, i0: np.ndarray, i1: np.ndarray) -> np.ndarray:
    """c[i1]/c[i0] - h[i1]/h[i0] for position arrays (NaN where out of range or missing)."""
    i0 = np.asarray(i0, dtype=int)
    i1 = np.asarray(i1, dtype=int)
    n = len(c)
    ok = (i0 >= 0) & (i1 >= 0) & (i0 < n) & (i1 < n)
    out = np.full(len(i0), np.nan)
    a, b = i0[ok], i1[ok]
    with np.errstate(invalid="ignore", divide="ignore"):
        out[ok] = (c[b] / c[a]) - (h[b] / h[a])
    return out


def pool_mean_window(c: np.ndarray, h: np.ndarray, starts: np.ndarray, length: int) -> float:
    """Mean hedged return over windows [p, p + length] for pool start positions p."""
    if length <= 0 or not len(starts):
        return float("nan")
    v = hedged_window(c, h, starts, np.asarray(starts) + length)
    return float(np.nanmean(v)) if np.isfinite(v).any() else float("nan")


def verdict(lo_by_period: dict[str, float], hi_by_period: dict[str, float]) -> str:
    """Amendment 3: 'value' if D's lower bound > 0 in every period; 'no value' if D's upper
    bound < 0 in any period; else 'inconclusive'."""
    los, his = list(lo_by_period.values()), list(hi_by_period.values())
    if los and all(np.isfinite(v) and v > 0 for v in los):
        return "value"
    if any(np.isfinite(v) and v < 0 for v in his):
        return "no value"
    return "inconclusive"
