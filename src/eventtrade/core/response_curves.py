"""ADR 0017 (event response curves, hypothesis family V): per-asset cumulative abnormal returns
from every possible base day, standardised excess variance, signed and continuation curves
against each event's own placebo pool, quarter-block bootstrap CIs, time-scale readouts.
Pure: no I/O.

Positions index one asset's trading-day array. A base b is the last close before the event can
be public; CAR(k) = c[b+k]/c[b] - 1 - beta_b * (h[b+k]/h[b] - 1), with beta_b and the abnormal
daily sigma_b estimated over [b-250, b-21] (amendments 1, 5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

KS = (1, 2, 3, 5, 10, 20, 40, 60)
BETA_WIN, BETA_GAP, BETA_MIN = 230, 21, 150
MEASURES = ("z2", "signed", "abs", "cont")


@dataclass(frozen=True)
class AssetCurves:
    car: dict[int, np.ndarray]   # k -> CAR(k) for every base position (NaN if unavailable)
    pre: np.ndarray              # CAR(-5): base close vs 5 days earlier
    sigma: np.ndarray            # abnormal daily sigma at each base


def asset_curves(c: np.ndarray, h: np.ndarray | None, ks: Sequence[int] = KS) -> AssetCurves:
    """CAR(k) for every base position of one asset; h None = raw (beta 0)."""
    c = np.asarray(c, dtype=float)
    n = len(c)
    r = pd.Series(np.concatenate([[np.nan], c[1:] / c[:-1] - 1]))
    if h is None:
        hh = np.ones(n)
        rh = pd.Series(np.zeros(n))
    else:
        hh = np.asarray(h, dtype=float)
        rh = pd.Series(np.concatenate([[np.nan], hh[1:] / hh[:-1] - 1]))
    ok = r.notna() & rh.notna()
    rr, hr = r.where(ok), rh.where(ok)

    def roll(x: pd.Series) -> np.ndarray:
        return x.rolling(BETA_WIN, min_periods=BETA_MIN).mean().shift(BETA_GAP).to_numpy()

    m_r, m_h = roll(rr), roll(hr)
    var_r = roll(rr * rr) - m_r ** 2
    var_h = roll(hr * hr) - m_h ** 2
    cov = roll(rr * hr) - m_r * m_h
    with np.errstate(invalid="ignore", divide="ignore"):
        beta = np.where(var_h > 1e-14, cov / var_h, 0.0) if h is not None else np.zeros(n)
        res_var = var_r - 2 * beta * cov + beta ** 2 * var_h
    sigma = np.sqrt(np.where(res_var > 0, res_var, np.nan))
    car = {}
    idx = np.arange(n)
    for k in ks:
        j = idx + k
        out = np.full(n, np.nan)
        m = j < n
        with np.errstate(invalid="ignore", divide="ignore"):
            out[m] = (c[j[m]] / c[idx[m]] - 1) - beta[m] * (hh[j[m]] / hh[idx[m]] - 1)
        car[k] = out
    pre = np.full(n, np.nan)
    m = idx >= 5
    with np.errstate(invalid="ignore", divide="ignore"):
        pre[m] = (c[idx[m]] / c[idx[m] - 5] - 1) - beta[m] * (hh[idx[m]] / hh[idx[m] - 5] - 1)
    return AssetCurves(car, pre, sigma)


def measures(ac: AssetCurves, k: int) -> dict[str, np.ndarray]:
    """Per base position: z2 = CAR^2/(sigma^2 k), signed CAR, |CAR|, continuation
    sgn(CAR(1)) * (CAR(k) - CAR(1)) (NaN at k = 1)."""
    car = ac.car[k]
    with np.errstate(invalid="ignore", divide="ignore"):
        z2 = car ** 2 / (ac.sigma ** 2 * k)
    c1 = ac.car[min(ac.car)]
    cont = np.sign(c1) * (car - c1) if k != min(ac.car) else np.full(len(car), np.nan)
    return {"z2": z2, "signed": car, "abs": np.abs(car), "cont": cont}


@dataclass(frozen=True)
class CellEvents:
    asset: np.ndarray    # asset name per event
    pos: np.ndarray      # base position per event (in that asset's array)
    date: np.ndarray     # base date per event (for blocks)


def event_x(curves: Mapping[str, AssetCurves], ev: CellEvents, pool_ok: Mapping[str, np.ndarray],
            key: Mapping[str, np.ndarray], k: int, exclusive: bool = True,
            winsor: tuple[float, float] = (0.01, 0.99)) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per measure: (event value, event value - own pool mean) per event, NaN when missing.

    Pool of an event = positions p of the same asset with pool_ok[p], the same key (e.g. year or
    year x weekday) and a finite value; exclusive: drop p (not itself an event) whose window
    (p, p+k] contains an event of the cell for that asset (amendment 10). z2 is winsorized at
    the cell's 1%/99% quantiles of events and pools together (amendment 1)."""
    names = sorted(set(ev.asset.tolist()))
    per: dict[str, dict[str, np.ndarray]] = {}
    pmask: dict[str, np.ndarray] = {}
    for a in names:
        m = measures(curves[a], k)
        per[a] = m
        n = len(m["z2"])
        epos = np.unique(ev.pos[ev.asset == a])
        mask = np.asarray(pool_ok[a], dtype=bool).copy()
        if exclusive and len(epos):
            idx = np.arange(n)
            nxt = np.searchsorted(epos, idx, side="right")
            has = nxt < len(epos)
            contains = np.zeros(n, dtype=bool)
            contains[has] = epos[nxt[has]] <= idx[has] + k
            is_ev = np.zeros(n, dtype=bool)
            is_ev[epos] = True
            mask &= ~contains | is_ev
        pmask[a] = mask
    lo = hi = None
    allz = np.concatenate([per[a]["z2"][pmask[a]] for a in names] +
                          [np.array([per[a]["z2"][p] for a, p in zip(ev.asset, ev.pos, strict=True)])])
    allz = allz[np.isfinite(allz)]
    if len(allz):
        lo, hi = np.quantile(allz, winsor)
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for meas in MEASURES:
        means: dict[str, dict[int, float]] = {}
        for a in names:
            v = per[a][meas]
            if meas == "z2" and lo is not None:
                v = np.clip(v, lo, hi)
            ok = pmask[a] & np.isfinite(v)
            kk = np.asarray(key[a])[ok]
            vv = v[ok]
            u, inv = np.unique(kk, return_inverse=True)
            s = np.bincount(inv, weights=vv)
            cnt = np.bincount(inv)
            means[a] = dict(zip(u.tolist(), (s / cnt).tolist(), strict=True))
        val = np.full(len(ev.pos), np.nan)
        x = np.full(len(ev.pos), np.nan)
        for i, (a, p) in enumerate(zip(ev.asset, ev.pos, strict=True)):
            v = per[a][meas][p]
            if meas == "z2" and lo is not None and np.isfinite(v):
                v = float(np.clip(v, lo, hi))
            pm = means[a].get(np.asarray(key[a])[p].item())
            val[i] = v
            if pm is not None and np.isfinite(v):
                x[i] = v - pm
        out[meas] = (val, x)
    return out


def block_ci(dates: Sequence, x: np.ndarray, n_boot: int, seed: int, level: float,
             freq: str = "Q") -> tuple[float, float]:
    """CI of mean(x) resampling calendar blocks (quarters by default) with replacement."""
    x = np.asarray(x, dtype=float)
    d = pd.to_datetime(pd.Series(list(dates)))
    ok = np.isfinite(x) & d.notna().to_numpy()
    x, d = x[ok], d[ok]
    if len(x) < 2:
        return float("nan"), float("nan")
    blk = d.dt.to_period(freq).astype(str).to_numpy()
    uniq, inv = np.unique(blk, return_inverse=True)
    sums = np.bincount(inv, weights=x)
    cnts = np.bincount(inv).astype(float)
    pick = np.random.default_rng(seed).integers(len(uniq), size=(n_boot, len(uniq)))
    means = sums[pick].sum(axis=1) / cnts[pick].sum(axis=1)
    a = (1 - level) / 2
    lo, hi = np.quantile(means, [a, 1 - a])
    return float(lo), float(hi)


def peak_half_life(ks: Sequence[int], y: Sequence[float]) -> tuple[int | None, int | str | None]:
    """(k*, half-life): k* = argmax y over finite values; half-life = the smallest k > k* with
    y <= y(k*)/2, 'persistent' if none; (None, None) if no positive peak."""
    ks, y = list(ks), np.asarray(y, dtype=float)
    if not np.isfinite(y).any() or np.nanmax(y) <= 0:
        return None, None
    i = int(np.nanargmax(y))
    for j in range(i + 1, len(ks)):
        if np.isfinite(y[j]) and y[j] <= y[i] / 2:
            return ks[i], ks[j]
    return ks[i], "persistent"


def reversal_half_life(ks: Sequence[int], c: Sequence[float], mean_abs_1: float) -> int | None:
    """Smallest k >= 2 with C(k) <= -mean|CAR(1)|/2 (None if never)."""
    for k, v in zip(ks, c, strict=True):
        if k >= 2 and np.isfinite(v) and v <= -mean_abs_1 / 2:
            return int(k)
    return None
