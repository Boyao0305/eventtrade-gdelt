"""ADR 0008 search machinery: generalized windows (latency L, window W), a vectorized baseline z,
and the placebo max-t over a cell's configurations with one shared day mapping per draw.

Slot algebra (UTC 15-minute slots; tau's slot is s): batch slot i is available at the start of
slot i + 1. With latency L = 15 l minutes and window W = 15 w minutes, the window at tau uses
slots s - l - w .. s - l - 1. ADR 0007 (L = 15, W = 60) is slots s-5 .. s-2.
"""

from __future__ import annotations

import itertools
import math
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from eventtrade.core import intraday_study as st

LATENCIES = (0, 15)
WINDOWS = (15, 30, 60, 120)
THRESHOLDS = (2.0, 2.5, 3.0, 4.0)
EXITS: tuple[str, ...] = ("15", "30", "60", "120", "C")


def window_bounds(latency_min: int, window_min: int) -> tuple[int, int]:
    """(a, b) such that the window at tau slot s is slots s-a .. s-b inclusive."""
    if latency_min % 15 or window_min % 15 or window_min <= 0 or latency_min < 0:
        raise ValueError("latency and window must be non-negative multiples of 15 (window > 0)")
    l, w = latency_min // 15, window_min // 15
    return l + w, l + 1


def window_sums_lw(m: np.ndarray, latency_min: int, window_min: int) -> np.ndarray:
    """W[d, s] = sum of slots s-a .. s-b of day d; NaN where s - a < 0."""
    a, b = window_bounds(latency_min, window_min)
    nd = m.shape[0]
    cs = np.concatenate([np.zeros((nd, 1)), np.cumsum(m, axis=1)], axis=1)
    out = np.full((nd, st.N_SLOTS), np.nan)
    for s in range(a, st.N_SLOTS):
        out[:, s] = cs[:, s - b + 1] - cs[:, s - a]
    return out


def window_valid_lw(missing: np.ndarray, latency_min: int, window_min: int) -> np.ndarray:
    """No missing batch in the window or in the two batches before its first slot."""
    a, b = window_bounds(latency_min, window_min)
    nd = missing.shape[0]
    valid = np.zeros((nd, st.N_SLOTS), dtype=bool)
    for s in range(a + 2, st.N_SLOTS):
        valid[:, s] = ~missing[:, s - a - 2:s - b + 1].any(axis=1)
    return valid


def baseline_z_fast(x: np.ndarray, den: np.ndarray, valid: np.ndarray,
                    sd_min: Literal["share", "tone"]) -> np.ndarray:
    """Vectorized `intraday_study.baseline_z` (same definition; trailing sums over days)."""
    nd = x.shape[0]
    ok = valid & (den > 0) & np.isfinite(x)
    xv = np.where(ok, x, 0.0)
    dv = np.where(ok, den, 0.0)
    k = st.BASELINE_DAYS

    def trailing(v: np.ndarray) -> np.ndarray:
        c = np.concatenate([np.zeros((1, v.shape[1])), np.cumsum(v, axis=0)], axis=0)
        lo = np.maximum(np.arange(nd) - k, 0)
        return c[np.arange(nd)] - c[lo]  # rows d: sum over days lo..d-1

    n = trailing(ok.astype(float))
    s1 = trailing(xv)
    s2 = trailing(xv * xv)
    sd_den = trailing(dv)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean = s1 / n
        var = (s2 - n * mean * mean) / (n - 1)
        sd = np.sqrt(np.clip(var, 0, None))
        floor = 1.0 / (sd_den / n) if sd_min == "share" else np.full_like(mean, st.TONE_SD_MIN)
        z = (x - mean) / np.fmax(sd, floor)
    use = ok & (n >= st.BASELINE_MIN) & np.isfinite(z)
    out = np.full(x.shape, np.nan)
    out[use] = np.clip(z[use], -st.Z_CLIP, st.Z_CLIP)
    return out


# --- prices (ADR 0008 amendment 1: entry at the open of bar [tau, tau+15) = bar j+1) ---------

@dataclass(frozen=True)
class SearchPanel:
    """float32 prices on the [asset, day, bar] grid.

    - open_[a, d, k]: open of bar k (NaN if no trade).
    - close_ff[a, d, k]: close of the last bar at or before k on that day (NaN before the first).
    - last_close[a, d]: the day's last regular close (NaN if no bar that day).
    - hedge[a]: row of the hedge, -1 for raw-return assets and for hedges.
    """
    open_: np.ndarray
    close_ff: np.ndarray
    last_close: np.ndarray
    hedge: np.ndarray


def empty_search_panel(n_assets: int, n_days: int, hedge: Sequence[int]) -> SearchPanel:
    if len(hedge) != n_assets:
        raise ValueError("one hedge entry per asset")
    shape = (n_assets, n_days, st.N_BARS)
    return SearchPanel(np.full(shape, np.nan, dtype=np.float32),
                       np.full(shape, np.nan, dtype=np.float32),
                       np.full((n_assets, n_days), np.nan, dtype=np.float32),
                       np.asarray(hedge, dtype=int))


def fill_search_row(p: SearchPanel, i: int, m: st.BarMatrix) -> None:
    p.open_[i] = m.open
    ff = pd.DataFrame(m.close.T).ffill().to_numpy().T
    p.close_ff[i] = ff
    p.last_close[i] = ff[:, -1]


def price_at(p: SearchPanel, a: np.ndarray, d: np.ndarray, minute_et: np.ndarray) -> np.ndarray:
    """Price at a clock time (minutes after ET midnight) on day d: the close of the last bar
    ending at or before it; before the first bar's end, the previous trading day's last close."""
    a = np.asarray(a, dtype=int)
    d = np.asarray(d, dtype=int)
    k = (np.asarray(minute_et) - 570) // 15 - 1  # bar ending at or before the time
    k = np.minimum(k, st.N_BARS - 1)
    out = np.full(len(a), np.nan)
    inday = k >= 0
    out[inday] = p.close_ff[a[inday], d[inday], k[inday]]
    need_prev = ~inday | ~np.isfinite(out)
    dm1 = d - 1
    okp = need_prev & (dm1 >= 0)
    out[okp] = p.last_close[a[okp], dm1[okp]]
    return out


def entry_open(p: SearchPanel, a: np.ndarray, d: np.ndarray, j: np.ndarray) -> np.ndarray:
    """Open of bar [tau, tau+15) (index j + 1), NaN if that bar has no trade."""
    return p.open_[np.asarray(a, dtype=int), np.asarray(d, dtype=int), np.asarray(j, dtype=int) + 1]


def search_return(p: SearchPanel, a: np.ndarray, d: np.ndarray, j: np.ndarray, exit_: str
                  ) -> np.ndarray:
    """Asset return from the open of bar j+1 to the close of bar min(j + h/15, 25) (fallback:
    last bar since entry), minus the hedge's over the same bars; raw when no hedge."""
    a = np.asarray(a, dtype=int)
    d = np.asarray(d, dtype=int)
    j = np.asarray(j, dtype=int)
    ke = np.maximum(exit_bars(j, exit_), j + 1)

    def leg(x: np.ndarray, dd: np.ndarray, jj: np.ndarray, kk: np.ndarray) -> np.ndarray:
        return p.close_ff[x, dd, kk].astype(np.float64) / p.open_[x, dd, jj + 1] - 1.0

    r = leg(a, d, j, ke)
    h = p.hedge[a]
    has = h >= 0
    if has.any():
        r = r.copy()
        r[has] -= leg(h[has], d[has], j[has], ke[has])
    return r


def exit_bars(j: np.ndarray, exit_: str) -> np.ndarray:
    """Exit bar index for entry tau index j: min(j + h/15, 25), or 25 for the close."""
    if exit_ == "C":
        return np.full(len(j), st.N_BARS - 1)
    return np.minimum(np.asarray(j) + int(exit_) // 15, st.N_BARS - 1)


def outcome_exit(p: st.Panel, a: np.ndarray, d: np.ndarray, j: np.ndarray, exit_: str
                 ) -> np.ndarray:
    """ADR 0007 outcome with a general exit (abnormal vs hedge, raw without one)."""
    a = np.asarray(a, dtype=int)
    d = np.asarray(d, dtype=int)
    j = np.asarray(j, dtype=int)
    ke = exit_bars(j, exit_)
    r = p.close_ff[a, d, ke].astype(np.float64) / p.entry[a, d, j] - 1.0
    h = p.hedge[a]
    has = h >= 0
    if has.any():
        hh, dd, jj, kk = h[has], d[has], j[has], ke[has]
        r = r.copy()
        r[has] -= p.close_ff[hh, dd, kk].astype(np.float64) / p.entry[hh, dd, jj] - 1.0
    return r


@dataclass(frozen=True)
class Config:
    latency: int
    window: int
    theta: float
    exit: str

    @property
    def key(self) -> str:
        return f"L{self.latency}_W{self.window}_th{self.theta:g}_x{self.exit}"


def grid() -> list[Config]:
    return [Config(lat, w, th, x) for lat in LATENCIES for w in WINDOWS for th in THRESHOLDS
            for x in EXITS]


@dataclass(frozen=True)
class SearchResult:
    stats: dict[str, tuple[float, float, int, int]]  # config -> (mean, t, n_events, n_days)
    best: str | None
    best_stat: float
    p: float
    n_valid_draws: int


def _stat(t: float, direction: int) -> float:
    return abs(t) if direction == 0 else direction * t


def block_placebo(rng: np.random.Generator, pool: np.ndarray, block: int = st.PLACEBO_RADIUS
                  ) -> np.ndarray:
    """One draw: permute the pool days within consecutive blocks of `block` days whose start is
    shifted by a uniform random offset (ADR 0008 amendment 3). Returns mapped[i] for pool[i]."""
    pool = np.asarray(pool, dtype=int)
    n = len(pool)
    off = int(rng.integers(block))
    edges = [0, *range(off if off else block, n, block), n]
    out = pool.copy()
    for lo, hi in itertools.pairwise(edges):
        if hi > lo:
            out[lo:hi] = pool[lo:hi][rng.permutation(hi - lo)]
    return out


def max_t_search_reference(
    events: dict[str, pd.DataFrame],
    outcome_at: Callable[[str, np.ndarray, np.ndarray, np.ndarray], np.ndarray],
    years: np.ndarray,
    pool: np.ndarray,
    direction: int,
    n_boot: int,
    seed: int,
) -> SearchResult:
    """Observed per-config statistics, the best sufficient config and its placebo max-t p.

    `events[config_key]`: day, asset, j for that config (already restricted to the period).
    `outcome_at(config_key, day, asset, j)`: outcomes, NaN where not eligible.
    Each draw maps every pool day to a placebo day by a 63-day block permutation and applies that
    one mapping to all configs. Only configs sufficient on the observed data enter the max; in a
    draw, a config with < 30 eligible events or days is left out of that draw's max (ADR 0008
    amendments 3-4). `outcome_at` must already return the directional net-of-cost value when
    the study's statistic is net of cost (amendment 5); `direction` 0 takes |t|.
    """
    pool = np.asarray(pool, dtype=int)
    n_days = int(max([int(pool.max()) if len(pool) else 0]
                     + [int(e["day"].max()) for e in events.values() if len(e)])) + 1
    arrays = {k: (ev["day"].to_numpy(int), ev["asset"].to_numpy(int), ev["j"].to_numpy(int))
              for k, ev in events.items()}
    stats: dict[str, tuple[float, float, int, int]] = {}
    suff: list[str] = []
    for k, (d, a, j) in arrays.items():
        mean, t, ne, nd = fast_cell(d, outcome_at(k, d, a, j), n_days)
        stats[k] = (mean, t, ne, nd)
        if st.sufficient(ne, nd) and math.isfinite(t):
            suff.append(k)
    if not suff:
        return SearchResult(stats, None, float("nan"), float("nan"), 0)
    best = max(suff, key=lambda k: _stat(stats[k][1], direction))
    obs = _stat(stats[best][1], direction)
    rng = np.random.default_rng(seed)
    lookup = np.full(n_days, -1)
    exceed = valid = 0
    for _ in range(n_boot):
        lookup[pool] = block_placebo(rng, pool)
        m = -math.inf
        for k in suff:
            d0, a, j = arrays[k]
            d = lookup[d0]
            _, t, ne, nd = fast_cell(d, outcome_at(k, d, a, j), n_days)
            if st.sufficient(ne, nd) and math.isfinite(t):
                m = max(m, _stat(t, direction))
        if math.isfinite(m):
            valid += 1
            exceed += m >= obs
    return SearchResult(stats, best, obs, (1 + exceed) / (1 + valid), valid)


def placebo_maps(rng: np.random.Generator, pool: np.ndarray, n_days: int, n_boot: int
                 ) -> np.ndarray:
    """[n_boot, n_days] day mappings (-1 off the pool), drawn in the same order as the reference
    loop (one `block_placebo` per draw from the same generator)."""
    maps = np.full((n_boot, n_days), -1, dtype=np.int64)
    for b in range(n_boot):
        maps[b, pool] = block_placebo(rng, pool)
    return maps


def value_table(k: str, a: np.ndarray, j: np.ndarray, pool: np.ndarray,
                outcome_at: Callable[[str, np.ndarray, np.ndarray, np.ndarray], np.ndarray]
                ) -> tuple[np.ndarray, np.ndarray]:
    """(V [combos, len(pool)], inverse): the outcome of every distinct (asset, tau) of the events
    on every pool day, so a placebo draw is a gather instead of a fresh price computation."""
    combo, inv = np.unique(np.stack([a, j], axis=1), axis=0, return_inverse=True)
    c_n, p_n = len(combo), len(pool)
    cc = np.repeat(np.arange(c_n), p_n)
    dd = np.tile(pool, c_n)
    v = outcome_at(k, dd, combo[cc, 0], combo[cc, 1]).reshape(c_n, p_n)
    return v, np.asarray(inv).reshape(-1)


def max_t_search(
    events: dict[str, pd.DataFrame],
    outcome_at: Callable[[str, np.ndarray, np.ndarray, np.ndarray], np.ndarray],
    years: np.ndarray,
    pool: np.ndarray,
    direction: int,
    n_boot: int,
    seed: int,
) -> SearchResult:
    """Same result as `max_t_search_reference` (tested), computed per config from a value table.

    Requires every event day to be a pool day (events are restricted to the period's decision
    days, which form the pool)."""
    pool = np.asarray(pool, dtype=int)
    n_days = int(max([int(pool.max()) if len(pool) else 0]
                     + [int(e["day"].max()) for e in events.values() if len(e)])) + 1
    arrays = {k: (ev["day"].to_numpy(int), ev["asset"].to_numpy(int), ev["j"].to_numpy(int))
              for k, ev in events.items()}
    stats: dict[str, tuple[float, float, int, int]] = {}
    suff: list[str] = []
    for k, (d, a, j) in arrays.items():
        mean, t, ne, nd = fast_cell(d, outcome_at(k, d, a, j), n_days)
        stats[k] = (mean, t, ne, nd)
        if st.sufficient(ne, nd) and math.isfinite(t):
            suff.append(k)
    if not suff:
        return SearchResult(stats, None, float("nan"), float("nan"), 0)
    best = max(suff, key=lambda k: _stat(stats[k][1], direction))
    obs = _stat(stats[best][1], direction)
    maps = placebo_maps(np.random.default_rng(seed), pool, n_days, n_boot)
    col = np.full(n_days, -1, dtype=np.int64)
    col[pool] = np.arange(len(pool))
    tmax = np.full(n_boot, -math.inf)
    for k in suff:
        d0, a, j = arrays[k]
        if (col[d0] < 0).any():
            raise ValueError(f"config {k}: event days outside the placebo pool")
        table, inv = value_table(k, a, j, pool, outcome_at)
        for b in range(n_boot):
            dp = maps[b, d0]
            _, t, ne, nd = fast_cell(dp, table[inv, col[dp]], n_days)
            if st.sufficient(ne, nd) and math.isfinite(t):
                tmax[b] = max(tmax[b], _stat(t, direction))
    valid = np.isfinite(tmax)
    exceed = int((tmax[valid] >= obs).sum())
    return SearchResult(stats, best, obs, (1 + exceed) / (1 + int(valid.sum())),
                        int(valid.sum()))


def configs_by_trigger(configs: Sequence[Config]) -> dict[tuple[int, int, float], list[Config]]:
    """Group configs sharing a trigger set (L, W, theta): their events differ only by exit."""
    out: dict[tuple[int, int, float], list[Config]] = {}
    for c in configs:
        out.setdefault((c.latency, c.window, c.theta), []).append(c)
    return out


# --- fast cell statistic (identical to intraday_study.daily_series + cell_t) ------------------

def fast_cell(d: np.ndarray, v: np.ndarray, n_days: int) -> tuple[float, float, int, int]:
    """(mean, NW t with 5 Bartlett lags, n_events, n_days) of the per-day mean series.

    Same value as `intraday_study.cell_t(daily_series(d, v))` for finite v, via bincount.
    """
    ok = np.isfinite(v)
    d, v = np.asarray(d)[ok], np.asarray(v, dtype=np.float64)[ok]
    if not len(v):
        return float("nan"), float("nan"), 0, 0
    cnt = np.bincount(d, minlength=n_days)
    sm = np.bincount(d, weights=v, minlength=n_days)
    has = cnt > 0
    x = sm[has] / cnt[has]
    n = x.size
    if n < 2:
        return float(x.mean()), float("nan"), len(v), n
    mean = float(x.mean())
    if np.ptp(x) == 0.0:
        t = float("nan") if mean == 0.0 else math.copysign(math.inf, mean)
        return mean, t, len(v), n
    dm = x - mean
    lags = min(st.NW_LAGS, n - 1)
    lrv = float(dm @ dm) / n
    for k in range(1, lags + 1):
        lrv += 2.0 * (1.0 - k / (lags + 1)) * float(dm[k:] @ dm[:-k]) / n
    if lrv <= 0.0:
        t = float("nan") if mean == 0.0 else math.copysign(math.inf, mean)
    else:
        t = mean / math.sqrt(lrv / n)
    return mean, t, len(v), n
