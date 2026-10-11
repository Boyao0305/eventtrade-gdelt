"""ADR 0014: scheduled macro announcements on SPY (hypothesis family M).

Pure functions on SPY's regular-session bar grid (`intraday_study.BarMatrix`: trading days x 26
bars of 15 minutes, bar k starting 09:30 + 15k ET). `p(d, hh:mm)` is the close of the bar ending
at hh:mm.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from eventtrade.core.intraday_study import BarMatrix

K_0945 = 0    # bar 09:30-09:45: its close is p(d, 09:45)
K_1345 = 16   # bar 13:30-13:45: its close is p(d, 13:45), before the 14:00 statement
K_1430 = 19   # bar 14:15-14:30: its close is p(d, 14:30), when the press conference starts
K_EARLY_LAST = 13  # bar 12:45-13:00: the last bar of a 13:00 early close
COST_SIDE = 0.0001  # SPY, 1 bp per side
SHIFTS = tuple(k for k in range(-14, 15) if k != 0 and k % 5 != 0)  # M2 placebo (amendment 7)
NW_LAGS = 5
EVEN_WEEKS = ((-1, 3), (9, 13), (19, 23), (29, 33))  # CMVJ even weeks, in cycle days
CELLS = ("M1", "M2", "M3", "M4", "M5")


def positions(days: Sequence[date], dates: Iterable[str | date]) -> np.ndarray:
    """Trading-day positions of `dates`, each moved to the first trading day on or after it.

    Dates after the last trading day are dropped. Sorted, unique.
    """
    d = np.asarray(days, dtype="datetime64[D]")
    q = np.asarray([np.datetime64(x, "D") for x in dates], dtype="datetime64[D]")
    pos = np.searchsorted(d, q, "left")
    return np.unique(pos[pos < len(d)])


def exact_positions(days: Sequence[date], dates: Iterable[str | date]) -> np.ndarray:
    """Trading-day positions of the `dates` that are trading days (amendment 4). Sorted, unique."""
    d = np.asarray(days, dtype="datetime64[D]")
    q = np.asarray([np.datetime64(x, "D") for x in dates], dtype="datetime64[D]")
    pos = np.searchsorted(d, q, "left")
    ok = pos < len(d)
    ok[ok] = d[pos[ok]] == q[ok]
    return np.unique(pos[ok])


def last_bar(m: BarMatrix) -> np.ndarray:
    """Index of the last bar with a close on each day; -1 if the day has no bar."""
    ok = np.isfinite(m.close)
    return np.where(ok.any(axis=1), ok.shape[1] - 1 - np.argmax(ok[:, ::-1], axis=1), -1)


def session_flags(m: BarMatrix, days: Sequence[date], listed_early: Iterable[str | date]
                  ) -> tuple[np.ndarray, np.ndarray]:
    """(early, bad) per day (amendment 6).

    early: in the NYSE early-close list and the last bar starts at or before 12:45 (k <= 13).
    bad: no bar at all, or the list and the bars disagree (listed early but bars after 13:00, or
    not listed but no bar after 13:00).
    """
    lb = last_bar(m)
    listed = np.zeros(len(days), dtype=bool)
    listed[exact_positions(days, listed_early)] = True
    short = (lb >= 0) & (lb <= K_EARLY_LAST)
    early = listed & short
    bad = (lb < 0) | (listed != short)
    return early, bad


@dataclass(frozen=True)
class EventSets:
    fomc: np.ndarray        # scheduled statement days that happened, trading days
    overlap: np.ndarray     # fomc days that also carry an 08:30 CPI/NFP release
    events: dict[str, np.ndarray]  # M1, M3, M4, M5 event positions
    pool: np.ndarray        # placebo pool positions (all periods)
    press: np.ndarray       # fomc days with a press conference


def event_sets(days: Sequence[date], drop: np.ndarray, fomc_scheduled: Iterable[str],
               fomc_cancelled: Iterable[str], fomc_unscheduled: Iterable[str],
               cpi: Iterable[str], nfp: Iterable[str], press: Iterable[str]) -> EventSets:
    """Event days per cell and the placebo pool (ADR 0014 with amendments 3-5, 11).

    Events are announcement dates that are trading days. Every announcement date (including
    cancelled and unscheduled FOMC dates and releases on non-trading days) moves to its first
    trading day on or after it; that day and its neighbours leave the pool, as do `drop` days.
    """
    fomc_scheduled, fomc_cancelled = list(fomc_scheduled), list(fomc_cancelled)
    cpi, nfp, unsched = list(cpi), list(nfp), list(fomc_unscheduled)
    fomc = np.setdiff1d(exact_positions(days, fomc_scheduled),
                        exact_positions(days, fomc_cancelled))
    rel = np.union1d(exact_positions(days, cpi), exact_positions(days, nfp))
    overlap = np.intersect1d(fomc, rel)
    events = {"M1": np.setdiff1d(fomc, overlap), "M3": np.union1d(fomc, rel),
              "M4": np.setdiff1d(rel, overlap), "M5": np.setdiff1d(fomc, overlap)}
    ann = positions(days, fomc_scheduled + fomc_cancelled + unsched + cpi + nfp)
    near = np.unique(np.concatenate([ann - 1, ann, ann + 1]))
    pool = np.setdiff1d(np.flatnonzero(~np.asarray(drop, dtype=bool)), near)
    return EventSets(fomc, overlap, events, pool,
                     np.intersect1d(fomc, exact_positions(days, press)))


def last_close(m: BarMatrix) -> np.ndarray:
    """close(d): the close of the last bar with a value on each day (NaN if none)."""
    c = m.close
    last = last_bar(m)
    out = np.full(c.shape[0], np.nan)
    has = last >= 0
    out[has] = c[np.flatnonzero(has), last[has]]
    return out


def _sgn(x: np.ndarray) -> np.ndarray:
    s = np.sign(x)
    return np.where(s == 0, np.nan, s)


def day_values(m: BarMatrix, drop: np.ndarray) -> dict[str, np.ndarray]:
    """Per-day values of M1, M3, M4, M5 on every trading day (NaN where undefined).

    `drop` days (early closes, bad sessions) are NaN in every cell. The previous close is the
    prior trading day's.
    """
    c = last_close(m)
    prev = np.concatenate([[np.nan], c[:-1]])
    p0945 = m.close[:, K_0945]
    p1345 = m.close[:, K_1345]
    p1430 = m.close[:, K_1430]
    out = {
        "M1": p1345 / prev - 1,
        "M3": c / prev - 1,
        "M4": _sgn(m.open[:, 0] / prev - 1) * (c / p0945 - 1),
        "M5": _sgn(p1430 / p1345 - 1) * (c / p1430 - 1),
    }
    bad = np.asarray(drop, dtype=bool)
    for v in out.values():
        v[bad] = np.nan
    return out


def cycle_day(n_days: int, statements: np.ndarray) -> np.ndarray:
    """CMVJ FOMC-cycle day of every trading-day position (NaN before the first statement).

    Day 0 is a scheduled statement day. The day before the next statement is day -1 of the next
    cycle; every other day counts from the most recent statement.
    """
    s = np.unique(np.asarray(statements, dtype=int))
    t = np.arange(n_days)
    out = np.full(n_days, np.nan)
    i_prev = np.searchsorted(s, t, "right") - 1
    has = i_prev >= 0
    out[has] = t[has] - s[i_prev[has]]
    i_next = np.searchsorted(s, t, "left")
    nxt = i_next < len(s)
    eve = np.zeros(n_days, dtype=bool)
    eve[nxt] = s[i_next[nxt]] - t[nxt] == 1
    out[eve] = -1
    return out


def even_week(cday: np.ndarray) -> np.ndarray:
    """1.0 on CMVJ even-week days, 0.0 otherwise, NaN where the cycle day is unknown."""
    e = np.zeros(len(cday))
    for lo, hi in EVEN_WEEKS:
        e[(cday >= lo) & (cday <= hi)] = 1.0
    e[~np.isfinite(cday)] = np.nan
    return e


def nw_slope(y: np.ndarray, x: np.ndarray, lags: int = NW_LAGS) -> tuple[float, float]:
    """(b, HAC t of b) in y = a + b x + e, Bartlett kernel; rows with NaN drop out."""
    ok = np.isfinite(y) & np.isfinite(x)
    y, x = np.asarray(y, float)[ok], np.asarray(x, float)[ok]
    n = len(y)
    if n < 3 or np.ptp(x) == 0:
        return float("nan"), float("nan")
    xm = np.column_stack([np.ones(n), x])
    xtx_inv = np.linalg.inv(xm.T @ xm)
    beta = xtx_inv @ xm.T @ y
    g = xm * (y - xm @ beta)[:, None]
    s = g.T @ g
    for k in range(1, min(lags, n - 1) + 1):
        w = 1.0 - k / (lags + 1)
        c = g[k:].T @ g[:-k]
        s += w * (c + c.T)
    v = xtx_inv @ s @ xtx_inv
    return float(beta[1]), float(beta[1] / np.sqrt(v[1, 1]))


def blocks(flag: np.ndarray) -> int:
    """Number of maximal runs of consecutive 1s (positions) in a 0/1 array."""
    f = np.nan_to_num(np.asarray(flag, float)) > 0
    return int(np.sum(f & ~np.concatenate([[False], f[:-1]])))


@dataclass(frozen=True)
class PlaceboResult:
    mean: float
    n: int
    p: float
    p_up: float
    p_down: float
    placebo_mean: float


def placebo_test(values: np.ndarray, events: np.ndarray, pool: np.ndarray, weekday: np.ndarray,
                 year: np.ndarray, n_boot: int, seed: int, direction: int) -> PlaceboResult:
    """Event-day mean of `values` against same-(year, weekday) placebo days (ADR 0014 nulls).

    `events`, `pool`: trading-day positions. Events whose value is NaN are dropped first. Each
    remaining event is replaced, per draw, by a distinct day with the same year and weekday drawn
    from the pool days plus the events' own days (a label permutation: without the event days the
    null is too narrow by sqrt(1 - n/N) when events are a large share of a year-weekday, e.g. NFP
    Fridays). A year-weekday with fewer pool days than events fails the run (amendment 11). A
    placebo mean averages its finite values. direction +1/-1: one-sided; 0: two-sided,
    2 x min(p_up, p_down) capped at 1.
    """
    if direction not in (1, -1, 0):
        raise ValueError(f"direction must be 1, -1 or 0, got {direction!r}")
    v = np.asarray(values, float)
    ev = np.asarray(events, int)
    ev = ev[np.isfinite(v[ev])]
    pool = np.asarray(pool, int)
    obs = float(v[ev].mean()) if len(ev) else float("nan")
    if not len(ev):
        nan = float("nan")
        return PlaceboResult(nan, 0, nan, nan, nan, nan)
    rng = np.random.default_rng(seed)
    sums = np.zeros(n_boot)
    cnts = np.zeros(n_boot)
    keys = year * 7 + weekday
    for key in np.unique(keys[ev]):
        n = int(np.sum(keys[ev] == key))
        own = ev[keys[ev] == key]
        cand = np.union1d(pool[keys[pool] == key], own)  # permutation: event days stay eligible
        if len(cand) < 2 * n:
            raise ValueError(f"placebo pool too small for year {key // 7}, weekday {key % 7}:"
                             f" {len(cand) - n} pool days for {n} events")
        pick = np.argsort(rng.random((n_boot, len(cand))), axis=1)[:, :n]
        pv = v[cand][pick]
        sums += np.nansum(pv, axis=1)
        cnts += np.isfinite(pv).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        null = sums / cnts
    null = null[np.isfinite(null)]
    b = len(null)
    p_up = (1 + np.sum(null >= obs)) / (1 + b)
    p_down = (1 + np.sum(null <= obs)) / (1 + b)
    if direction == 1:
        p = p_up
    elif direction == -1:
        p = p_down
    else:
        p = min(1.0, 2 * min(p_up, p_down))
    return PlaceboResult(obs, len(ev), float(p), float(p_up), float(p_down),
                         float(null.mean()) if b else float("nan"))


def shift_p(y: np.ndarray, statements: np.ndarray, b_obs: float, n_shift: int, seed: int) -> float:
    """Reported only (amendment 7): share of whole-schedule shifts k in SHIFTS whose M2 slope is
    >= the observed one; k drawn uniformly, n_shift draws."""
    if not np.isfinite(b_obs):
        return float("nan")
    n = len(y)
    ks = np.random.default_rng(seed).choice(SHIFTS, size=n_shift)
    slope: dict[int, float] = {}
    for k in np.unique(ks):
        s = statements + int(k)
        slope[int(k)] = nw_slope(y, even_week(cycle_day(n, s[(s >= 0) & (s < n)])))[0]
    hits = sum(slope[int(k)] >= b_obs for k in ks)
    return (1 + hits) / (1 + n_shift)
