"""Intraday event study statistics (ADR 0007): clock, matrices, outages, signals, events, outcomes,
cell statistic and the local placebo-day null. Pure numpy/pandas; no I/O.

Grids
- Trading days: an ordered array of `datetime.date` (days with SPY regular-session bars). Every
  matrix below has one row per trading day, in that order.
- Bars: ET bar index k = 0..25 for [09:30 + 15k, 09:45 + 15k). `close[d, k]` is NaN when the bar
  has no trade (Alpaca omits it).
- GDELT: UTC slot j = 0..95 for the batch stamped 00:00 + 15j UTC of that day (stamps are already
  rounded up to the grid by the queries).
- Decision times: ET j_tau = 0..21 for tau = 09:45 + 15 j_tau. Entry is the close of bar
  k = j_tau. `utc_slot[d, j_tau]` is tau's UTC slot s; the signal window is slots s-5..s-2
  (batches tau-75..tau-30 min, available by tau-15).
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from eventtrade.core.evaluate import newey_west_t

NY = ZoneInfo("America/New_York")
N_BARS = 26
N_TAU = 22
N_SLOTS = 96
WINDOW = (5, 2)  # window = slots s-5 .. s-2 inclusive
BASELINE_DAYS = 20
BASELINE_MIN = 15
Z_THRESHOLD = 3.0
Z_CLIP = 10.0
TONE_SD_MIN = 0.5
MISSING_FRACTION = 0.25
H60_BARS = 4
NW_LAGS = 5
PLACEBO_RADIUS = 63
MIN_EVENTS = 30
MIN_EVENT_DAYS = 30
LIQUIDITY_MIN_TRADES = 20
TONE_SPLIT_MIN_DOCS = 20

Horizon = Literal["h60", "hC"]
HORIZONS: tuple[Horizon, ...] = ("h60", "hC")


# --- calendar and clock -----------------------------------------------------------------------

def bar_index(ts: np.ndarray | pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """(ET date, ET bar index k) of bar-start epoch seconds; k outside 0..25 for non-session bars."""
    et = pd.to_datetime(np.asarray(ts, dtype="int64"), unit="s", utc=True).tz_convert(NY)
    k = ((et.hour * 60 + et.minute) - 570) // 15
    return np.asarray(et.date), np.asarray(k)


def trading_calendar(spy_ts: np.ndarray | pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """(trading days, early_close flag) from SPY's regular-session bar starts.

    A trading day is a day with any SPY regular-session bar; it is an early close when SPY has
    no bar starting at 15:45 ET (k = 25).
    """
    days, k = bar_index(spy_ts)
    df = pd.DataFrame({"day": days, "k": k})
    df = df[(df["k"] >= 0) & (df["k"] < N_BARS)]
    last = df.groupby("day")["k"].max().sort_index()
    return np.asarray(last.index), np.asarray(last.values < N_BARS - 1)


def tau_utc_slots(days: Sequence[date]) -> np.ndarray:
    """utc_slot[d, j]: the UTC 15-minute slot of decision time tau_j = 09:45 + 15 j ET on day d."""
    out = np.empty((len(days), N_TAU), dtype=np.int64)
    for i, d in enumerate(days):
        for j in range(N_TAU):
            m = 585 + 15 * j
            et = datetime.combine(d, time(m // 60, m % 60), NY)
            u = et.astimezone(ZoneInfo("UTC"))
            out[i, j] = (u.hour * 60 + u.minute) // 15
    return out


# --- bars -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BarMatrix:
    """One ticker's bars on the trading-day grid; NaN = no bar (no trade)."""
    open: np.ndarray    # [nd, 26]
    close: np.ndarray   # [nd, 26]
    high: np.ndarray
    low: np.ndarray
    trades: np.ndarray  # [nd, 26], 0 where no bar


def bar_matrix(bars: pd.DataFrame, days: Sequence[date]) -> BarMatrix:
    """Place one ticker's regular-session bars (INTRADAY_COLUMNS) on the [day, k] grid."""
    nd = len(days)
    mats = {c: np.full((nd, N_BARS), np.nan) for c in ("open", "close", "high", "low")}
    trades = np.zeros((nd, N_BARS))
    if len(bars):
        bdays, k = bar_index(bars["ts"].to_numpy())
        pos = {d: i for i, d in enumerate(days)}
        di = np.array([pos.get(d, -1) for d in bdays])
        ok = (di >= 0) & (k >= 0) & (k < N_BARS)
        for c, mat in mats.items():
            mat[di[ok], k[ok]] = bars[c].to_numpy(dtype=float)[ok]
        trades[di[ok], k[ok]] = bars["trade_count"].to_numpy(dtype=float)[ok]
    return BarMatrix(mats["open"], mats["close"], mats["high"], mats["low"], trades)


def median_trades(m: BarMatrix, day_mask: np.ndarray) -> float:
    """Median trade count per 15-minute bar over the masked days, missing bars counted as 0."""
    x = m.trades[day_mask]
    return float(np.median(x)) if x.size else float("nan")


def abdi_ranaldo_spread(m: BarMatrix, day_mask: np.ndarray) -> float:
    """Median over days of the mean two-bar Abdi-Ranaldo (2017) spread estimate within the day.

    s^2 = 4 (c_k - eta_k)(c_k - eta_{k+1}), eta = (ln h + ln l)/2, c = ln close; negative
    products are set to 0 before the square root (the usual convention).
    """
    c = np.log(m.close[day_mask])
    eta = (np.log(m.high[day_mask]) + np.log(m.low[day_mask])) / 2
    prod = 4 * (c[:, :-1] - eta[:, :-1]) * (c[:, :-1] - eta[:, 1:])
    s = np.sqrt(np.clip(prod, 0, None))
    with np.errstate(all="ignore"):
        daily = np.nanmean(np.where(np.isnan(prod), np.nan, s), axis=1)
    daily = daily[np.isfinite(daily)]
    return float(np.median(daily)) if daily.size else float("nan")


# --- GDELT matrices, outages, windows -----------------------------------------------------------

def slot_matrix(rows: pd.DataFrame, value: str, days: Sequence[date]) -> np.ndarray:
    """[nd, 96] matrix of one entity's `value` per (trading day, UTC slot); 0 where no row.

    `rows` has `batch_ts` (epoch s, UTC, on the 15-minute grid) and `value`. Batches on days that
    are not trading days are dropped.
    """
    out = np.zeros((len(days), N_SLOTS))
    if len(rows):
        u = pd.to_datetime(rows["batch_ts"].to_numpy(dtype="int64"), unit="s", utc=True)
        pos = {d: i for i, d in enumerate(days)}
        di = np.array([pos.get(d, -1) for d in u.date])
        sl = np.asarray((u.hour * 60 + u.minute) // 15)
        ok = di >= 0
        np.add.at(out, (di[ok], sl[ok]), rows[value].to_numpy(dtype=float)[ok])
    return out


def missing_batches(global_counts: np.ndarray, pulled: np.ndarray) -> np.ndarray:
    """ADR 0007 outages: a pulled batch is missing when its `_GLOBAL` count is 0 or below 25% of
    the median at the same UTC slot over the previous 20 trading days. Slots outside `pulled`
    (a [96] bool mask) are never missing (and never used)."""
    nd = global_counts.shape[0]
    miss = np.zeros_like(global_counts, dtype=bool)
    for d in range(nd):
        lo = max(0, d - BASELINE_DAYS)
        if d > lo:
            med = np.median(global_counts[lo:d], axis=0)
            low = global_counts[d] < MISSING_FRACTION * med
        else:
            low = np.zeros(N_SLOTS, dtype=bool)
        miss[d] = pulled & ((global_counts[d] <= 0) | low)
    return miss


def window_valid(missing: np.ndarray) -> np.ndarray:
    """valid[d, s]: the window ending at tau slot s (slots s-5..s-2) has no missing batch and no
    batch that is one of the two right after a missing one (slots s-7..s-2 all present)."""
    nd = missing.shape[0]
    valid = np.zeros((nd, N_SLOTS), dtype=bool)
    lo_off, hi_off = WINDOW[0] + 2, WINDOW[1]
    for s in range(lo_off, N_SLOTS):
        valid[:, s] = ~missing[:, s - lo_off:s - hi_off + 1].any(axis=1)
    return valid


def window_sums(m: np.ndarray) -> np.ndarray:
    """W[d, s] = sum of slots s-5..s-2 of day d (NaN where s < 5)."""
    nd = m.shape[0]
    cs = np.concatenate([np.zeros((nd, 1)), np.cumsum(m, axis=1)], axis=1)
    out = np.full((nd, N_SLOTS), np.nan)
    a, b = WINDOW
    for s in range(a, N_SLOTS):
        out[:, s] = cs[:, s - b + 1] - cs[:, s - a]
    return out


def baseline_z(x: np.ndarray, den: np.ndarray, valid: np.ndarray,
               sd_min: Literal["share", "tone"]) -> np.ndarray:
    """z[d, s] of x against the same UTC slot on the previous 20 trading days (ADR 0007).

    A baseline value counts when its window is valid and its denominator > 0; at least 15 are
    needed. sd_eff = max(sample sd, sd_min): for shares 1 / mean(baseline denominators), for tone
    0.5 tone points. z is clipped to +-10; NaN where the current window is invalid or den <= 0.
    """
    nd = x.shape[0]
    ok = valid & (den > 0) & np.isfinite(x)
    z = np.full(x.shape, np.nan)
    for d in range(1, nd):
        lo = max(0, d - BASELINE_DAYS)
        okb = ok[lo:d]
        n = okb.sum(axis=0)
        xb = np.where(okb, x[lo:d], np.nan)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN / 1-value slots -> NaN
            mean = np.nanmean(xb, axis=0)
            sd = np.nanstd(xb, axis=0, ddof=1)
            if sd_min == "share":
                floor = 1.0 / np.nanmean(np.where(okb, den[lo:d], np.nan), axis=0)
            else:
                floor = np.full(N_SLOTS, TONE_SD_MIN)
            sd_eff = np.fmax(sd, floor)
            zz = (x[d] - mean) / sd_eff
        use = ok[d] & (n >= BASELINE_MIN) & np.isfinite(zz)
        z[d, use] = np.clip(zz[use], -Z_CLIP, Z_CLIP)
    return z


def at_tau(m: np.ndarray, utc_slot: np.ndarray) -> np.ndarray:
    """Read a [nd, 96] slot matrix at each decision time: [nd, 22]."""
    return np.take_along_axis(m, utc_slot, axis=1)


# --- events -----------------------------------------------------------------------------------

def first_firing(fires: np.ndarray) -> np.ndarray:
    """[nd, 22] bool -> j_tau of the first firing per day (-1 if none). Declustering (ADR 0007)."""
    any_ = fires.any(axis=1)
    return np.where(any_, fires.argmax(axis=1), -1)


@dataclass(frozen=True)
class Panel:
    """Stacked prices of the assets and hedges a study needs, vectorized for outcome lookups.

    - close_ff[a, d, k]: close of bar k, forward-filled within the day (NaN before the first bar).
      An exit at bar k_exit therefore reads the last bar at or before k_exit; when no bar traded
      after the entry bar, it reads the entry close itself (return 0), as ADR 0007 requires.
    - entry[a, d, j]: close of the entry bar j itself, NaN if it has no trade.
    - pre[a, d, j]: price at tau-75 (close of bar j-5, or the session's first open before
      11:00; implementation note in ADR 0007).
    - hedge[a]: row of a's hedge in the panel, or -1 for raw-return assets (and for hedges).
    """
    close_ff: np.ndarray
    entry: np.ndarray
    pre: np.ndarray
    hedge: np.ndarray


def empty_panel(n_assets: int, n_days: int, hedge: Sequence[int]) -> Panel:
    """Allocate a float32 panel (filled row by row with `fill_panel_row`)."""
    if len(hedge) != n_assets:
        raise ValueError("one hedge entry per asset")
    return Panel(np.full((n_assets, n_days, N_BARS), np.nan, dtype=np.float32),
                 np.full((n_assets, n_days, N_TAU), np.nan, dtype=np.float32),
                 np.full((n_assets, n_days, N_TAU), np.nan, dtype=np.float32),
                 np.asarray(hedge, dtype=int))


def fill_panel_row(p: Panel, i: int, m: BarMatrix) -> None:
    p.close_ff[i] = pd.DataFrame(m.close.T).ffill().to_numpy().T
    p.entry[i] = m.close[:, :N_TAU]
    first_open = pd.DataFrame(m.open.T).bfill().to_numpy().T[:, 0]
    for j in range(N_TAU):
        p.pre[i, :, j] = m.close[:, j - 5] if j >= 5 else first_open


def build_panel(mats: Sequence[BarMatrix], hedge: Sequence[int]) -> Panel:
    nd = mats[0].close.shape[0] if mats else 0
    p = empty_panel(len(mats), nd, hedge)
    for i, m in enumerate(mats):
        fill_panel_row(p, i, m)
    return p


def _ret(p: Panel, a: np.ndarray, d: np.ndarray, j: np.ndarray, horizon: Horizon) -> np.ndarray:
    k_exit = np.minimum(j + H60_BARS, N_BARS - 1) if horizon == "h60" else np.full(len(j), N_BARS - 1)
    e = p.entry[a, d, j].astype(np.float64)
    return p.close_ff[a, d, k_exit].astype(np.float64) / e - 1.0


def outcome(p: Panel, a: np.ndarray, d: np.ndarray, j: np.ndarray, horizon: Horizon) -> np.ndarray:
    """Abnormal return r(asset) - r(hedge), or raw r(asset) when the asset has no hedge.

    NaN when the asset (or its hedge) has no trade in the entry bar: not eligible (ADR 0007).
    """
    a = np.asarray(a, dtype=int)
    d = np.asarray(d, dtype=int)
    j = np.asarray(j, dtype=int)
    r = _ret(p, a, d, j, horizon)
    h = p.hedge[a]
    has = h >= 0
    if has.any():
        rh = _ret(p, h[has], d[has], j[has], horizon)
        r = r.copy()
        r[has] = r[has] - rh
    return r


def outcome_pre(p: Panel, a: np.ndarray, d: np.ndarray, j: np.ndarray) -> np.ndarray:
    """The same outcome over [tau-75, tau): the move before GDELT had the window's news."""
    a = np.asarray(a, dtype=int)
    d = np.asarray(d, dtype=int)
    j = np.asarray(j, dtype=int)
    r = p.entry[a, d, j].astype(np.float64) / p.pre[a, d, j] - 1.0
    h = p.hedge[a]
    has = h >= 0
    if has.any():
        r = r.copy()
        hh, dd, jj = h[has], d[has], j[has]
        r[has] = r[has] - (p.entry[hh, dd, jj].astype(np.float64) / p.pre[hh, dd, jj] - 1.0)
    return r


def eligible(p: Panel, a: np.ndarray, d: np.ndarray, j: np.ndarray) -> np.ndarray:
    """Entry bar traded for the asset and (if any) its hedge."""
    a = np.asarray(a, dtype=int)
    ok = np.isfinite(p.entry[a, d, j])
    h = p.hedge[a]
    has = h >= 0
    ok[has] &= np.isfinite(p.entry[h[has], np.asarray(d)[has], np.asarray(j)[has]])
    return ok


# --- cell statistic and placebo null -------------------------------------------------------------

def daily_series(day: np.ndarray, value: np.ndarray) -> pd.Series:
    """Mean outcome per event day, ordered by day (positions on the trading-day grid)."""
    s = pd.Series(np.asarray(value, dtype=float), index=np.asarray(day, dtype=int))
    s = s[np.isfinite(s.to_numpy())]
    return s.groupby(level=0).mean().sort_index()


def cell_t(series: pd.Series) -> tuple[float, float]:
    """(mean, Newey-West t with 5 lags) of a daily series."""
    if len(series) < 2:
        return float("nan"), float("nan")
    return float(series.mean()), float(newey_west_t(series, lags=NW_LAGS))


def sufficient(n_events: int, n_days: int) -> bool:
    return n_events >= MIN_EVENTS and n_days >= MIN_EVENT_DAYS


def placebo_days(rng: np.random.Generator, orig_days: np.ndarray, years: np.ndarray,
                 pool: np.ndarray) -> np.ndarray:
    """One draw: a distinct placebo day for each original event day (ADR 0007).

    `pool`: sorted trading-day positions a placebo may use (the period's decision days: no
    early closes). Uniform over pool days within +-63 trading-day positions of the original,
    distinct across the original days; if none is left, pool days of the same calendar year;
    then any unused pool day. `years[i]` = calendar year of trading-day position i.
    """
    pool = np.asarray(pool, dtype=int)
    used = np.zeros(int(max(pool.max(initial=0), np.max(orig_days, initial=0))) + 1, dtype=bool)
    out = np.empty(len(orig_days), dtype=int)
    same_year: dict[int, np.ndarray] = {}
    for idx in rng.permutation(len(orig_days)):
        o = int(orig_days[idx])
        a = np.searchsorted(pool, o - PLACEBO_RADIUS, "left")
        b = np.searchsorted(pool, o + PLACEBO_RADIUS, "right")
        pick = _pick(rng, pool[a:b], used)
        if pick is None:
            y = int(years[o])
            if y not in same_year:
                same_year[y] = pool[years[pool] == y]
            pick = _pick(rng, same_year[y], used)
        if pick is None:
            pick = _pick(rng, pool, used)
        if pick is None:
            raise ValueError("more event days than placebo pool days")
        used[pick] = True
        out[idx] = pick
    return out


def _pick(rng: np.random.Generator, cand: np.ndarray, used: np.ndarray) -> int | None:
    cand = cand[~used[cand]]
    return int(cand[rng.integers(len(cand))]) if len(cand) else None


@dataclass(frozen=True)
class PlaceboResult:
    mean: float
    t: float
    p: float
    n_events: int
    n_days: int
    n_valid_draws: int


def placebo_test(
    events: pd.DataFrame,
    outcome_at: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray],
    years: np.ndarray,
    pool: np.ndarray,
    direction: Literal[1, -1, 0],
    n_boot: int,
    seed: int,
) -> PlaceboResult:
    """Observed cell t and its placebo-day p (ADR 0007 §Inference).

    `events`: columns day (trading-day position), asset (key), j (tau index).
    `outcome_at(day, asset, j) -> np.ndarray`: outcomes for arrays of positions, NaN where the
    event is not eligible there (no entry bar for asset or hedge); NaN events drop out.
    direction: +1 / -1 one-sided in that direction; 0 = two-sided on |t|.
    p = (1 + #{t_placebo >= t_obs}) / (1 + #valid draws) on the directional statistic.
    """
    if direction not in (1, -1, 0):
        raise ValueError(f"direction must be 1, -1 or 0, got {direction!r}")
    day = events["day"].to_numpy(dtype=int)
    asset = events["asset"].to_numpy()
    j = events["j"].to_numpy(dtype=int)
    obs_val = outcome_at(day, asset, j)
    ok = np.isfinite(obs_val)
    series = daily_series(day[ok], obs_val[ok])
    n_events, n_days = int(ok.sum()), len(series)
    mean, t = cell_t(series)

    def stat(tt: float) -> float:
        return abs(tt) if direction == 0 else direction * tt

    if not sufficient(n_events, n_days) or not math.isfinite(t):
        return PlaceboResult(mean, t, float("nan"), n_events, n_days, 0)
    t_obs = stat(t)
    uniq = np.unique(day)
    rng = np.random.default_rng(seed)
    exceed = 0
    valid = 0
    for _ in range(n_boot):
        mapped = placebo_days(rng, uniq, years, pool)
        lookup = dict(zip(uniq.tolist(), mapped.tolist(), strict=True))
        pd_day = np.array([lookup[x] for x in day])
        v = outcome_at(pd_day, asset, j)
        fin = np.isfinite(v)
        s = daily_series(pd_day[fin], v[fin])
        _, tp = cell_t(s)
        if math.isfinite(tp):  # a draw with < 2 eligible days has no t and is not counted
            valid += 1
            exceed += stat(tp) >= t_obs
    p = (1 + exceed) / (1 + valid)
    return PlaceboResult(mean, t, p, n_events, n_days, valid)
