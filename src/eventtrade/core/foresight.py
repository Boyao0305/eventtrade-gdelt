"""ADR 0009 (value of foresight) statistics: fresh-event filter, foresight placebo test with
same-asset exclusion, and the reverse event study (do news categories follow price shocks?).
Pure numpy/pandas; no I/O. Slot and grid conventions as in `intraday_study`.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from eventtrade.core import intraday_search as se
from eventtrade.core import intraday_study as st

FRESH_Z = 1.0
SHOCK_SIGMAS = 3.0
SIGMA_DAYS = 20
SIGMA_MIN_OBS = 15
HORIZON_SLOTS = 96  # 24 hours of 15-minute slots


def fresh(z: np.ndarray, d: np.ndarray, s: np.ndarray, overlap: int = 3) -> np.ndarray:
    """Event (d, tau slot s) is fresh when every window of the previous 24 hours that does not
    overlap the event's own window has z < 1 (NaN counts as not elevated).

    Window at slot s' covers s'-5..s'-2; it overlaps the event window iff s' >= s - 3, so the
    prior windows are slots (d-1, s-4 .. 95) and (d, 0 .. s-4). Day d-1 is the previous
    trading-day row (weekends and holidays are skipped; implementation note).
    """
    d = np.asarray(d, dtype=int)
    s = np.asarray(s, dtype=int)
    out = np.ones(len(d), dtype=bool)
    zz = np.where(np.isfinite(z), z, -np.inf)
    for i, (dd, ss) in enumerate(zip(d, s, strict=True)):
        cut = ss - overlap - 1
        today = zz[dd, :max(cut + 1, 0)]
        prev = zz[dd - 1, max(cut, 0):] if dd >= 1 else np.empty(0)
        if (today.size and today.max() >= FRESH_Z) or (prev.size and prev.max() >= FRESH_Z):
            out[i] = False
    return out


@dataclass(frozen=True)
class ForesightResult:
    mean: float
    t: float
    p: float
    n_events: int
    n_days: int
    n_valid_draws: int


def foresight_test(
    d: np.ndarray, a: np.ndarray, value_at: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray],
    j: np.ndarray, pool: np.ndarray, direction: int, exclusion: int, n_boot: int, seed: int,
) -> ForesightResult:
    """Observed directional NW t of the daily series of `value_at(d, a, j)` and its placebo p.

    Draws: ADR 0008 block permutation of the pool days; an event whose placebo day lies within
    `exclusion` trading days of any (observed) event day of the same asset is dropped from that
    draw (ADR 0009 amendment 3). direction +1/-1 one-sided, 0 two-sided (|t|).
    p = (1 + #{stat_placebo >= stat_obs}) / (1 + valid draws).
    """
    d = np.asarray(d, dtype=int)
    a = np.asarray(a, dtype=int)
    j = np.asarray(j, dtype=int)
    pool = np.asarray(pool, dtype=int)
    n_days = int(max(pool.max(initial=0), d.max(initial=0))) + 1
    v = value_at(d, a, j)
    mean, t, ne, nd = se.fast_cell(d, v, n_days)

    def stat(x: float) -> float:
        return abs(x) if direction == 0 else direction * x

    if not st.sufficient(ne, nd) or not math.isfinite(t):
        return ForesightResult(mean, t, float("nan"), ne, nd, 0)
    obs = stat(t)
    own = {int(x): np.unique(d[a == x]) for x in np.unique(a)}
    rng = np.random.default_rng(seed)
    lookup = np.full(n_days, -1)
    exceed = valid = 0
    for _ in range(n_boot):
        lookup[pool] = se.block_placebo(rng, pool)
        dp = lookup[d]
        keep = dp >= 0
        for x, days_x in own.items():
            m = (a == x) & keep
            if m.any():
                pos = np.searchsorted(days_x, dp[m])
                near = np.zeros(m.sum(), dtype=bool)
                for off in (-1, 0):
                    q = np.clip(pos + off, 0, len(days_x) - 1)
                    near |= np.abs(days_x[q] - dp[m]) <= exclusion
                idx = np.flatnonzero(m)
                keep[idx[near]] = False
        if not keep.any():
            continue
        vv = value_at(dp[keep], a[keep], j[keep])
        _, tp, ne_p, nd_p = se.fast_cell(dp[keep], vv, n_days)
        if st.sufficient(ne_p, nd_p) and math.isfinite(tp):
            valid += 1
            exceed += stat(tp) >= obs
    return ForesightResult(mean, t, (1 + exceed) / (1 + valid) if valid else float("nan"),
                           ne, nd, valid)


# --- reverse event study ------------------------------------------------------------------------

def bar_returns(close_ff: np.ndarray, hedge_close_ff: np.ndarray | None) -> np.ndarray:
    """[nd, 26] 15-minute close-to-close returns within the day (bar 0: NaN), minus the hedge's."""
    c = close_ff.astype(np.float64)
    r = np.full(c.shape, np.nan)
    r[:, 1:] = c[:, 1:] / c[:, :-1] - 1.0
    if hedge_close_ff is not None:
        h = hedge_close_ff.astype(np.float64)
        rh = np.full(h.shape, np.nan)
        rh[:, 1:] = h[:, 1:] / h[:, :-1] - 1.0
        r = r - rh
    return r


def shocks(r: np.ndarray, allowed_days: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(day, bar) of |r| > 3 sigma, sigma = std of the same bar over the previous 20 trading days
    (>= 15 finite values); only on allowed days."""
    nd = r.shape[0]
    fin = np.isfinite(r)
    x = np.where(fin, r, 0.0)
    c1 = np.concatenate([np.zeros((1, r.shape[1])), np.cumsum(x, axis=0)])
    c2 = np.concatenate([np.zeros((1, r.shape[1])), np.cumsum(x * x, axis=0)])
    cn = np.concatenate([np.zeros((1, r.shape[1])), np.cumsum(fin, axis=0)])
    idx = np.arange(nd)
    lo = np.maximum(idx - SIGMA_DAYS, 0)
    n = cn[idx] - cn[lo]
    with np.errstate(all="ignore"):
        mean = (c1[idx] - c1[lo]) / n
        var = ((c2[idx] - c2[lo]) - n * mean * mean) / (n - 1)
        sig = np.sqrt(np.clip(var, 0, None))
        hit = fin & (n >= SIGMA_MIN_OBS) & (np.abs(r) > SHOCK_SIGMAS * sig) & (sig > 0)
    hit &= np.asarray(allowed_days, dtype=bool)[:, None]
    dd, kk = np.nonzero(hit)
    return dd, kk


def response_rate(fire: np.ndarray, usable: np.ndarray, d: np.ndarray, s: np.ndarray
                  ) -> np.ndarray:
    """Mean of `fire` over the usable slots of the next 24 hours after slot s of day d (slots
    s+1..95 of day d and 0..s of the next trading-day row). NaN if no usable slot."""
    d = np.asarray(d, dtype=int)
    s = np.asarray(s, dtype=int)
    nd = fire.shape[0]
    f = np.where(usable, fire.astype(float), 0.0)
    u = usable.astype(float)
    cf = np.concatenate([np.zeros((nd, 1)), np.cumsum(f, axis=1)], axis=1)
    cu = np.concatenate([np.zeros((nd, 1)), np.cumsum(u, axis=1)], axis=1)
    num = cf[d, -1] - cf[d, s + 1]
    den = cu[d, -1] - cu[d, s + 1]
    nxt = d + 1
    ok = nxt < nd
    num[ok] += cf[nxt[ok], s[ok] + 1]
    den[ok] += cu[nxt[ok], s[ok] + 1]
    with np.errstate(all="ignore"):
        return np.where(den > 0, num / den, np.nan)


def bar_end_slot(days_utc_tau_slot: np.ndarray, k: np.ndarray, d: np.ndarray) -> np.ndarray:
    """UTC slot at which bar k of day d ends: bar k ends at tau_j with j = k (ADR 0007 grid),
    for k <= 21; later bars extrapolate by whole slots from tau_21."""
    k = np.asarray(k, dtype=int)
    d = np.asarray(d, dtype=int)
    base = days_utc_tau_slot[d, np.minimum(k, st.N_TAU - 1)]
    return base + np.maximum(k - (st.N_TAU - 1), 0)
