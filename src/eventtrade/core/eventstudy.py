"""Statistics for the pre-registered extreme-shock event study (ADR 0005).

Pure: no I/O, no clock. Thresholds, calendars, seeds and the configuration grid are parameters;
the only randomness is the placebo draws in `max_t_placebo` (discovery) and `placebo_p`
(confirmation), both seeded by the caller.

Conventions pinned by tests/core/test_eventstudy.py:

- `triggers` (ADR 0005 "Events"). An (asset, signal) pair fires on trading day t when its shock
  score is >= theta (direction +1) or <= -theta (direction -1); NaN never fires. Declustering is
  per ticker, on trading-day POSITIONS in `trading_days` (not calendar days), and first-come on
  accepted events: after an event at position p the ticker cannot fire at p+1 .. p+decluster_days
  (ADR: "cannot trigger again for 20 trading days"); the first day at or beyond theta at
  distance > decluster_days fires and starts a new block. A day inside a block is discarded and
  does not extend it. Deterministic: a ticker has one score per day.

- `calendar_time_series` is ADR 0005's calendar-time portfolio. An event dated t (decided at t's
  cutoff, entered at the t+1 open, exited at the t+1+h open) is ACTIVE on the h trading days
  t+1 .. t+h (positions p+1 .. p+h of `trading_days`), the sessions over which it is held. The
  series' value on trading day d is the equal-weighted mean, over the events active on d, of
  their h-day outcome (ADR: "the daily mean of the outcome across active events"). Days with no
  active event are absent; days past the end of `trading_days` are dropped. Because the whole
  h-day outcome is averaged (not a daily return), the same form serves every ADR 0005 outcome,
  abnormal volatility included. Each event appears on h consecutive days and nearby events'
  windows overlap, so the series is autocorrelated up to ~2h-1 lags; `cell_stat` uses
  `newey_west_t(..., horizon=h)`: the larger of Bartlett with max(auto, 2h) lags (ADR "lags >=
  h") and Hansen-Hodrick with h-1 uniform lags. The returned Series carries `attrs` n_events
  (events with an outcome), n_event_days (distinct event dates with an outcome), n_days (active
  days = its length), n_events_no_outcome (dropped: e.g. past the embargo) and horizon. The
  ADR 0005 Amendment's sufficiency rule, `sufficient(n_events, n_event_days)` (at least
  `MIN_EVENTS` events AND `MIN_EVENT_DAYS` distinct event dates), is applied by the caller on
  those attrs.

- `cell_stat`: (mean, NW t with horizon h, directional t = sign * t or |t| when sign is None,
  number of observations = active days).

- `max_t_placebo` (ADR 0005 Amendment: the discovery search correction). Observed statistic
  M = max over the cell's (sufficient) configurations of the directional t of the exact
  calendar-time series (configs whose t is undefined are left out). Null: each draw takes ONE
  uniformly random permutation of the trading days of the cell's window (the union of the
  configurations' embargoed event windows: the h = 1 window in the pipeline) and moves every
  event date of every configuration, with all the tickers that fired on it, to its image. So
  the dates of a configuration stay distinct, each lands on a uniformly random window date, the
  h's of a (theta, W) trigger set share the mapping (as the Amendment requires) and so do the
  trigger sets, which keeps the correlation between the configurations the max is taken over
  (independent mappings per trigger set would inflate the null max and make the test
  conservative). A configuration's outcome is read only inside its own embargoed window: an
  event moved past it (a longer-h configuration, at most h_max - h_min window days) has no
  outcome and drops out of that draw, as in `placebo_p`. The units moved are the events with an
  outcome on their own date. Each configuration's calendar-time series and NW t (own h) are
  recomputed; M* = max over configurations; p = (1 + #{M* >= M}) / (n_draws + 1). Vectorised:
  per configuration, the sum and count of its tickers' outcomes for every (event date, window
  date) pair are tabulated once, so a draw is a gather plus h shifted additions (pinned
  against a loop over `calendar_time_series` in the tests).

- `placebo_p` (confirmation test of ONE configuration; ADR 0005 says "one-sided p < 0.05" and
  does not fix the test). Null: the event dates are no different from random dates. Each draw
  moves every distinct event date, with all the tickers that fired on it, to a placebo date
  drawn uniformly (without replacement) from the dates where any of those tickers has an
  outcome (the caller restricts `outcome` to the period and embargo), the dates of a draw
  distinct but not spaced (as in `max_t_placebo`); it then recomputes the calendar-time series and its directional NW t (an event whose ticker has no
  outcome on its placebo date drops out of that draw). p = (1 + #{z* >= z}) / (n_draws + 1).
  Moving whole dates keeps same-day clusters (e.g. commodity ETFs sharing producer countries)
  as clusters, and using the same tickers' outcomes carries their drift and the HAC t's
  small-sample spread into the null. The one-sided normal p of the HAC t has neither.

- `net_mean(mean, legs, cost_bps)` = mean - legs * cost_bps / 1e4, where `mean` is already in
  the registered direction (sign * mean). ADR 0005: abnormal-return trades pay `LEGS_ABNORMAL`
  = 4 legs, raw-return trades `LEGS_RAW` = 2; ETF 5 bp, stock 10 bp per leg; volatility cells
  have no cost leg.

JUDGEMENT CALLS (not fixed by ADR 0005; chosen before any event-study value was computed):

| Question | Choice |
|---|---|
| "crosses a threshold": level or upward crossing? | level: every day with score >= theta is a candidate; the 20-day declustering is what limits a persistent shock (a score held above theta re-fires every 21 trading days, while its W-day baseline absorbs it) |
| first day after the block | position p + decluster_days + 1 |
| days an event is active | t+1 .. t+h (held from the t+1 open to the t+1+h open) |
| value of an active event on a day | its whole h-day outcome (ADR wording), not a daily return |
| confirmation p of one configuration | placebo dates (`placebo_p`), not the normal p of the HAC t |
| placebo unit | a distinct event date with all its tickers (clusters kept) |
| discovery placebo: one mapping per trigger set, or one for the whole cell? | one permutation per draw for the whole cell (shared by every trigger set and h) |
| placebo spacing (discovery and confirmation) | none: dates distinct only (the Amendment's "same placebo-date construction"). Declustering is per ticker and signal, so a cell's pooled event dates are not spaced; spacing the confirmation's placebo dates 21 apart rejected 8.5% at 5% (h = 20, 2 signals x 3 ETFs, 58 dates; docs/agent/log.md) |
| discovery placebo window when the h's embargo differently | the union (h = 1) window; events moved past a longer h's window drop out |
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.core.evaluate import newey_west_t
from eventtrade.core.screen import _boot_p, _nw_t_rows

__all__ = [
    "LEGS_ABNORMAL",
    "LEGS_RAW",
    "MIN_EVENTS",
    "MIN_EVENT_DAYS",
    "MaxTPlaceboResult",
    "PlaceboResult",
    "calendar_time_series",
    "cell_stat",
    "max_t_placebo",
    "net_mean",
    "placebo_p",
    "sufficient",
    "triggers",
]

Config = Hashable
Sign = int | float | None

MIN_EVENTS = 30  # ADR 0005: a cell with fewer events is "insufficient events"
MIN_EVENT_DAYS = 30  # ADR 0005 Amendment: ... or fewer distinct event dates
LEGS_ABNORMAL = 4  # long the asset, short the group, in and out
LEGS_RAW = 2
_PLACEBO_CHUNK = 4_000_000  # expanded (draw x event x day) entries per placebo chunk


# --- events -----------------------------------------------------------------------------------

def triggers(
    score: pd.DataFrame,
    theta: float,
    direction: int,
    decluster_days: int = 20,
    trading_days: Sequence[Any] | pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """Declustered events [date, ticker] of one shock score (see module docstring).

    `score` has columns date, ticker, value (one row per date and ticker). `trading_days` is the
    calendar the 20-day block is counted on; every score date must be in it.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction!r}")
    if decluster_days < 0:
        raise ValueError("decluster_days must be >= 0")
    if trading_days is None:
        raise ValueError("trading_days is required")
    cal = _calendar(trading_days)
    df = score[["date", "ticker", "value"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    if df.duplicated(["date", "ticker"]).any():
        raise ValueError("score has duplicate (date, ticker) rows")
    df["pos"] = _positions(cal, df["date"], "score")
    v = df["value"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        hit = v >= theta if direction == 1 else v <= -theta
    cross = df[hit].sort_values(["ticker", "pos"])

    keep: list[tuple[pd.Timestamp, Any]] = []
    for ticker, grp in cross.groupby("ticker", sort=True):
        last = None
        for p, d in zip(grp["pos"].to_numpy(), grp["date"]):
            if last is None or p - last > decluster_days:
                keep.append((d, ticker))
                last = p
    out = pd.DataFrame(keep, columns=["date", "ticker"])
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values(["date", "ticker"], ignore_index=True)


# --- calendar-time series ---------------------------------------------------------------------

def calendar_time_series(
    events: pd.DataFrame, outcome: pd.DataFrame, h: int,
    trading_days: Sequence[Any] | pd.DatetimeIndex,
) -> pd.Series:
    """Calendar-time portfolio: per trading day, the mean h-day outcome of the active events.

    `outcome` has columns date, ticker, value: the h-day outcome of a position decided at
    `date` (entered at the next open). Every event date must be in `trading_days`. See the
    module docstring for the active window and the `attrs` it carries.
    """
    if h < 1:
        raise ValueError("h must be >= 1")
    cal = _calendar(trading_days)
    have, n_no = _events_with_outcome(events, outcome)
    return _series_of(have, n_no, h, cal)


def _series_of(have: pd.DataFrame, n_no: int, h: int, cal: pd.DatetimeIndex) -> pd.Series:
    """`calendar_time_series` of events already joined to their outcome (`_events_with_outcome`)."""
    pos = _positions(cal, have["date"], "event")
    s = _active_mean(pos, have["value"].to_numpy(dtype=float), h, len(cal))
    s.index = pd.DatetimeIndex(cal[s.index.to_numpy(dtype=int)], name="date")
    s.name = "calendar_time_mean"
    s.attrs = {
        "n_events": len(have),
        "n_event_days": int(have["date"].nunique()),
        "n_days": len(s),
        "n_events_no_outcome": n_no,
        "horizon": int(h),
    }
    return s


def _events_with_outcome(events: pd.DataFrame, outcome: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Events joined to their outcome value; (rows with a finite value, number dropped)."""
    ev = events[["date", "ticker"]].copy()
    ev["date"] = pd.to_datetime(ev["date"])
    oc = outcome[["date", "ticker", "value"]].copy()
    oc["date"] = pd.to_datetime(oc["date"])
    ev["ticker"] = ev["ticker"].astype(object)
    oc["ticker"] = oc["ticker"].astype(object)
    if oc.duplicated(["date", "ticker"]).any():
        raise ValueError("outcome has duplicate (date, ticker) rows")
    merged = ev.merge(oc, on=["date", "ticker"], how="left")
    merged["value"] = pd.to_numeric(merged["value"], errors="coerce").astype(float)
    have = merged[np.isfinite(merged["value"].to_numpy())].reset_index(drop=True)
    return have, int(len(merged) - len(have))


def _active_mean(pos: np.ndarray, values: np.ndarray, h: int, n_cal: int) -> pd.Series:
    """Series indexed by calendar position: mean of `values` over events active there."""
    if pos.size == 0:
        return pd.Series(dtype=float)
    days = (pos[:, None] + np.arange(1, h + 1)[None, :]).ravel()
    vals = np.repeat(values, h)
    ok = days < n_cal
    days, vals = days[ok], vals[ok]
    if days.size == 0:
        return pd.Series(dtype=float)
    sums = np.bincount(days, weights=vals, minlength=n_cal)
    cnt = np.bincount(days, minlength=n_cal)
    live = np.flatnonzero(cnt)
    return pd.Series(sums[live] / cnt[live], index=live, dtype=float)


# --- cell statistic ---------------------------------------------------------------------------

def cell_stat(series: pd.Series, h: int, sign: Sign) -> tuple[float, float, float, int]:
    """(mean, NW t with horizon h, directional t, number of observations)."""
    sgn = _check_sign(sign)
    s = pd.Series(series, dtype=float).dropna()
    mean = float(s.mean()) if len(s) else float("nan")
    t = newey_west_t(s, horizon=int(h))
    return mean, float(t), _z(float(t), sgn), len(s)


# --- search-corrected p: placebo-date max-t (ADR 0005 Amendment) -------------------------------

def sufficient(n_events: int, n_event_days: int) -> bool:
    """ADR 0005 Amendment: at least MIN_EVENTS events and MIN_EVENT_DAYS distinct event dates."""
    return int(n_events) >= MIN_EVENTS and int(n_event_days) >= MIN_EVENT_DAYS


@dataclass(frozen=True)
class MaxTPlaceboResult:
    """Placebo-date null of the maximum directional t over one cell's configurations."""

    p: float
    max_z: float
    best_config: Config | None
    t: dict[Config, float]
    z: dict[Config, float]
    n_obs: dict[Config, int]  # active days of the observed calendar-time series
    n_events: dict[Config, int]
    n_event_days: dict[Config, int]
    sign: Sign
    n_draws: int
    seed: int
    n_dates: int  # trading days a placebo date is drawn from (the cell's window)
    null_max_mean: float  # mean of the null maxima (drift / search-size diagnostic)

    def notes(self) -> dict[str, Any]:
        return {
            "test": "placebo_dates_max_t",
            "p": _f(self.p), "max_z": _f(self.max_z),
            "best_config": None if self.best_config is None else str(self.best_config),
            "sign": self.sign, "n_draws": self.n_draws, "seed": self.seed,
            "n_dates": self.n_dates, "n_configs": len(self.t),
            "null_max_mean": _f(self.null_max_mean),
            "t": {str(c): _f(v) for c, v in self.t.items()},
            "z": {str(c): _f(v) for c, v in self.z.items()},
            "n_obs": {str(c): n for c, n in self.n_obs.items()},
            "n_events": {str(c): n for c, n in self.n_events.items()},
            "n_event_days": {str(c): n for c, n in self.n_event_days.items()},
        }


def max_t_placebo(
    events_by_config: Mapping[Config, pd.DataFrame],
    outcome_by_config: Mapping[Config, pd.DataFrame],
    horizons: Mapping[Config, int],
    windows: Mapping[Config, tuple[Any, Any]],
    sign: Sign,
    trading_days: Sequence[Any] | pd.DatetimeIndex,
    n_draws: int,
    seed: int,
) -> MaxTPlaceboResult:
    """Search-corrected p of the best configuration against placebo dates (module docstring).

    Per configuration: its events [date, ticker], its h-day outcome [date, ticker, value], its
    holding period h and its inclusive embargoed event window (lo, hi); every observed event
    must be dated inside its window, and its outcome is only read inside it. `sign` is the
    cell's registered sign (+1/-1), or None for |t|.
    """
    cfgs = list(events_by_config)
    if not cfgs:
        raise ValueError("events_by_config is empty")
    for what, m in (("horizon", horizons), ("outcome", outcome_by_config), ("window", windows)):
        missing = [c for c in cfgs if c not in m]
        if missing:
            raise ValueError(f"configs without a {what}: {missing[:5]}")
    sgn = _check_sign(sign)
    if n_draws < 1:
        raise ValueError("n_draws must be >= 1")
    cal = _calendar(trading_days)
    n_cal = len(cal)
    win = {c: (cal.searchsorted(pd.Timestamp(windows[c][0]), side="left"),
               cal.searchsorted(pd.Timestamp(windows[c][1]), side="right") - 1) for c in cfgs}
    lo = min(w[0] for w in win.values())
    hi = max(w[1] for w in win.values())
    n_c = max(0, hi - lo + 1)

    # Observed: the exact calendar-time series of every configuration (as the trials log it).
    t_obs: dict[Config, float] = {}
    z_obs: dict[Config, float] = {}
    n_obs: dict[Config, int] = {}
    n_ev: dict[Config, int] = {}
    n_ev_days: dict[Config, int] = {}
    units: dict[Config, pd.DataFrame] = {}
    for c in cfgs:
        h = int(horizons[c])
        oc = _window_rows(outcome_by_config[c], cal[win[c][0]] if win[c][0] < n_cal else None,
                          cal[win[c][1]] if win[c][1] >= 0 else None)
        have, n_no = _events_with_outcome(events_by_config[c], oc)
        ev_pos = _positions(cal, pd.to_datetime(events_by_config[c]["date"]), "event")
        if ev_pos.size and (ev_pos.min() < win[c][0] or ev_pos.max() > win[c][1]):
            raise ValueError(f"config {c!r} has events outside its window {windows[c]}")
        s = _series_of(have, n_no, h, cal)
        t_obs[c] = float(newey_west_t(s, horizon=h))
        z_obs[c] = _z(t_obs[c], sgn)
        n_obs[c] = len(s)
        n_ev[c] = int(s.attrs["n_events"])
        n_ev_days[c] = int(s.attrs["n_event_days"])
        units[c] = have
    live = [i for i, c in enumerate(cfgs) if not math.isnan(z_obs[c])]
    nan = float("nan")
    if not live:
        return MaxTPlaceboResult(nan, nan, None, t_obs, z_obs, n_obs, n_ev, n_ev_days, sign,
                                 int(n_draws), int(seed), int(n_c), nan)
    best_i = max(live, key=lambda i: (z_obs[cfgs[i]], -i))  # first config wins a tie
    m_obs = z_obs[cfgs[best_i]]

    # Null: one permutation of the window's trading days per draw, shared by every config.
    perms = _placebo_permutations(np.random.default_rng(seed), n_c, int(n_draws))
    wide_cache: dict[int, tuple[np.ndarray, np.ndarray, dict[Any, int]]] = {}
    m_null = np.full(int(n_draws), -np.inf)
    for i in live:
        c = cfgs[i]
        s_mat, c_mat, day_idx = _placebo_tables(units[c], outcome_by_config[c], cal, lo, n_c,
                                                (win[c][0] - lo, win[c][1] - lo), wide_cache)
        h = int(horizons[c])
        step = max(1, _PLACEBO_CHUNK // max(1, (n_c + h) * 4))
        for a in range(0, int(n_draws), step):
            b = min(int(n_draws), a + step)
            img = perms[a:b][:, day_idx]  # (draws, event dates): placebo index in the window
            mat = _relocated_means(s_mat, c_mat, img, n_c, h, min(n_c + h, n_cal - lo))
            z_b = _z_vec(_nw_t_rows(mat, h), sgn)
            m_null[a:b] = np.fmax(m_null[a:b], np.where(np.isnan(z_b), -np.inf, z_b))
    finite = m_null[np.isfinite(m_null)]
    return MaxTPlaceboResult(
        p=_boot_p(m_null, m_obs), max_z=float(m_obs), best_config=cfgs[best_i], t=t_obs,
        z=z_obs, n_obs=n_obs, n_events=n_ev, n_event_days=n_ev_days, sign=sign,
        n_draws=int(n_draws), seed=int(seed), n_dates=int(n_c),
        null_max_mean=float(finite.mean()) if finite.size else nan,
    )


def _placebo_permutations(rng: np.random.Generator, n: int, n_draws: int) -> np.ndarray:
    """(n_draws, n): row b maps window date i to window date perms[b, i]; a uniformly random
    permutation per row, so the dates of any configuration stay distinct."""
    return np.argsort(rng.random((n_draws, n)), axis=1, kind="stable")


def _window_rows(df: pd.DataFrame, lo: pd.Timestamp | None, hi: pd.Timestamp | None
                 ) -> pd.DataFrame:
    if lo is None or hi is None:
        return df.iloc[:0]
    d = pd.to_datetime(df["date"])
    return df[(d >= lo) & (d <= hi)]


def _placebo_tables(
    have: pd.DataFrame, outcome: pd.DataFrame, cal: pd.DatetimeIndex, lo: int, n_c: int,
    own: tuple[int, int], cache: dict[int, tuple[np.ndarray, np.ndarray, dict[Any, int]]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per event date i and window date j: the sum (S) and count (C) of the finite outcomes,
    at j, of the tickers that fired on i; zero outside the config's own window `own`
    (indices into the window). Also each event date's own window index."""
    key = id(outcome)
    if key not in cache:  # outcome frames are shared by the configs of one h: build once
        oc = outcome[["date", "ticker", "value"]].copy()
        oc["date"] = pd.to_datetime(oc["date"])
        oc["value"] = pd.to_numeric(oc["value"], errors="coerce").astype(float)
        idx = cal.get_indexer(oc["date"]) - lo
        oc = oc[(idx >= 0) & (idx < n_c) & np.isfinite(oc["value"].to_numpy())]
        idx = cal.get_indexer(oc["date"]) - lo
        tickers = sorted(set(oc["ticker"]), key=str)
        col = {tk: k for k, tk in enumerate(tickers)}
        vals = np.zeros((len(tickers), n_c))
        fin = np.zeros((len(tickers), n_c))
        cols = oc["ticker"].map(col).to_numpy(dtype=np.int64)
        vals[cols, idx] = oc["value"].to_numpy()
        fin[cols, idx] = 1.0
        cache[key] = (vals, fin, col)
    vals, fin, col = cache[key]
    ev_days, day_of = np.unique(cal.get_indexer(have["date"]) - lo, return_inverse=True)
    tk = have["ticker"].map(col)
    ok = tk.notna().to_numpy()
    used = np.unique(tk[ok].to_numpy(dtype=np.int64))
    ind = np.zeros((len(ev_days), len(used)))
    np.add.at(ind, (day_of[ok], np.searchsorted(used, tk[ok].to_numpy(dtype=np.int64))), 1.0)
    s_mat = ind @ vals[used]
    c_mat = ind @ fin[used]
    outside = np.ones(n_c, dtype=bool)
    outside[own[0]: own[1] + 1] = False
    s_mat[:, outside] = 0.0
    c_mat[:, outside] = 0.0
    return s_mat, c_mat, ev_days.astype(np.int64)


def _relocated_means(
    s_mat: np.ndarray, c_mat: np.ndarray, img: np.ndarray, n_c: int, h: int, n_out: int,
) -> np.ndarray:
    """Calendar-time series (rows = draws, columns = window positions lo .. lo+n_out-1) when
    event date i is moved to window date img[b, i]: an event entered at j is active on
    j+1 .. j+h; NaN where no event is active. Distinct images per row: no collisions."""
    rows, k = img.shape
    r = np.arange(rows)[:, None]
    e = np.arange(k)[None, :]
    entry_s = np.zeros((rows, n_c))
    entry_c = np.zeros((rows, n_c))
    entry_s[r, img] = s_mat[e, img]
    entry_c[r, img] = c_mat[e, img]
    # window sum over entries j-h .. j-1 = difference of cumulative sums (counts are exact)
    cs_s = np.zeros((rows, n_c + h + 1))
    cs_c = np.zeros((rows, n_c + h + 1))
    np.cumsum(entry_s, axis=1, out=cs_s[:, 1: n_c + 1])
    np.cumsum(entry_c, axis=1, out=cs_c[:, 1: n_c + 1])
    cs_s[:, n_c + 1:] = cs_s[:, n_c: n_c + 1]
    cs_c[:, n_c + 1:] = cs_c[:, n_c: n_c + 1]
    j = np.arange(n_out)  # active day j holds entries j-h .. j-1
    sums = cs_s[:, j] - cs_s[:, np.maximum(j - h, 0)]
    cnt = cs_c[:, j] - cs_c[:, np.maximum(j - h, 0)]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, sums / np.maximum(cnt, 1.0), np.nan)


# --- placebo-date p of one configuration --------------------------------------------------------

@dataclass(frozen=True)
class PlaceboResult:
    """Placebo-date test of one configuration's calendar-time mean (see module docstring)."""

    p: float
    t: float
    z: float
    n_events: int
    n_draws: int
    seed: int
    null_z_mean: float  # mean directional t over the placebo draws (drift / skew diagnostic)

    def notes(self) -> dict[str, Any]:
        return {"test": "placebo_dates", "p": _f(self.p), "t": _f(self.t), "z": _f(self.z),
                "n_events": self.n_events, "n_draws": self.n_draws, "seed": self.seed,
                "placebo_spacing": 0, "null_z_mean": _f(self.null_z_mean)}


def placebo_p(
    events: pd.DataFrame,
    outcome: pd.DataFrame,
    h: int,
    sign: Sign,
    trading_days: Sequence[Any] | pd.DatetimeIndex,
    n_draws: int,
    seed: int,
) -> PlaceboResult:
    """One-sided p of the events' directional NW t against placebo dates of the same tickers.

    `outcome` must already be restricted to the dates an event could have had (period and
    embargo): placebo dates are drawn among its dates with a finite value for an event ticker.
    """
    sgn = _check_sign(sign)
    if n_draws < 1:
        raise ValueError("n_draws must be >= 1")
    cal = _calendar(trading_days)
    have, _ = _events_with_outcome(events, outcome)
    s = calendar_time_series(events, outcome, h, cal)
    t_obs = float(newey_west_t(s, horizon=int(h)))
    z_obs = _z(t_obs, sgn)
    nan = float("nan")
    if have.empty or math.isnan(z_obs):
        return PlaceboResult(nan, t_obs, z_obs, len(have), int(n_draws), int(seed), nan)

    oc = outcome[["date", "ticker", "value"]].copy()
    oc["date"] = pd.to_datetime(oc["date"])
    oc["ticker"] = oc["ticker"].astype(object)
    oc["value"] = pd.to_numeric(oc["value"], errors="coerce").astype(float)
    oc = oc[np.isfinite(oc["value"].to_numpy()) & oc["ticker"].isin(set(have["ticker"]))]
    tickers = sorted(set(have["ticker"]), key=str)
    col = {tk: i for i, tk in enumerate(tickers)}
    wide = np.full((len(cal), len(tickers)), np.nan)
    wide[_positions(cal, oc["date"], "outcome"), oc["ticker"].map(col).to_numpy()] = (
        oc["value"].to_numpy())
    elig = np.flatnonzero(np.isfinite(wide).any(axis=1))  # dates with any event ticker's outcome
    ev_days = np.sort(have["date"].unique())
    day_of = np.searchsorted(ev_days, have["date"].to_numpy())  # event -> its event-date index
    ev_col = have["ticker"].map(col).to_numpy()

    # Each draw moves every event DATE (with all the tickers that fired on it) to a placebo
    # date, so same-day clusters stay clusters; the dates of a draw are distinct, not spaced (the
    # pooled observed dates are not: declustering is per ticker and signal). An event whose
    # ticker has no outcome on the placebo date drops out of that draw.
    rng = np.random.default_rng(seed)
    pick = _distinct_draws(rng, len(elig), len(ev_days), int(n_draws))
    pos = elig[pick][:, day_of]  # (n_draws, n_events)
    val = wide[pos, ev_col[None, :]]

    z_null = np.empty(n_draws)
    n_cal = len(cal)
    step = max(1, _PLACEBO_CHUNK // max(1, pos.shape[1] * h))
    for lo in range(0, n_draws, step):
        hi = min(n_draws, lo + step)
        mat = _active_mean_rows(pos[lo:hi], val[lo:hi], h, n_cal)
        z_null[lo:hi] = _z_vec(_nw_t_rows(mat, int(h)), sgn)
    finite = z_null[np.isfinite(z_null)]
    return PlaceboResult(
        p=_boot_p(z_null, z_obs), t=t_obs, z=z_obs, n_events=len(have), n_draws=int(n_draws),
        seed=int(seed), null_z_mean=float(finite.mean()) if finite.size else nan,
    )


def _distinct_draws(rng: np.random.Generator, n: int, k: int, n_draws: int) -> np.ndarray:
    """(n_draws, k) sorted distinct indices into range(n); each row a uniform k-subset.

    Each row is the first k of a uniformly random permutation of range(n), so there is no
    rejection loop (redrawing a row until its k integers are distinct never ends once k is
    around sqrt(n) or more). Chunked so the (rows, n) key matrix stays small.
    """
    if k > n:
        raise ValueError(f"cannot draw {k} placebo dates from {n}")
    out = np.empty((n_draws, k), dtype=np.int64)
    step = max(1, _PLACEBO_CHUNK // max(1, n))
    for a in range(0, n_draws, step):
        b = min(n_draws, a + step)
        out[a:b] = np.sort(np.argsort(rng.random((b - a, n)), axis=1, kind="stable")[:, :k],
                           axis=1)
    return out


def _active_mean_rows(pos: np.ndarray, val: np.ndarray, h: int, n_cal: int) -> np.ndarray:
    """Row-wise `_active_mean` on the full calendar: (rows, n_cal), NaN where nothing is active."""
    rows = pos.shape[0]
    days = (pos[:, :, None] + np.arange(1, h + 1)[None, None, :]).reshape(rows, -1)
    vals = np.repeat(val, h, axis=1)
    ok = (days < n_cal) & np.isfinite(vals)
    flat = (np.arange(rows)[:, None] * n_cal + days)[ok]
    sums = np.bincount(flat, weights=vals[ok], minlength=rows * n_cal).reshape(rows, n_cal)
    cnt = np.bincount(flat, minlength=rows * n_cal).reshape(rows, n_cal)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, sums / np.maximum(cnt, 1), np.nan)


# --- costs ------------------------------------------------------------------------------------

def net_mean(mean: float, legs: int, cost_bps: float) -> float:
    """Directional mean outcome net of `legs` * `cost_bps` (basis points per leg)."""
    if legs < 0 or cost_bps < 0:
        raise ValueError("legs and cost_bps must be >= 0")
    return float(mean) - legs * cost_bps / 1e4


# --- helpers ----------------------------------------------------------------------------------

def _calendar(trading_days: Sequence[Any] | pd.DatetimeIndex) -> pd.DatetimeIndex:
    if (isinstance(trading_days, pd.DatetimeIndex) and trading_days.tz is None
            and trading_days.is_monotonic_increasing and trading_days.is_unique):
        return trading_days  # already a calendar: skip the element-wise conversion
    return pd.DatetimeIndex(pd.to_datetime(list(trading_days))).sort_values().unique()


def _positions(cal: pd.DatetimeIndex, dates: pd.Series, what: str) -> np.ndarray:
    pos = cal.get_indexer(pd.to_datetime(dates))
    if (pos < 0).any():
        bad = pd.to_datetime(pd.Series(dates)).iloc[int(np.flatnonzero(pos < 0)[0])]
        raise ValueError(f"{what} date {bad:%Y-%m-%d} is not a trading day")
    return pos.astype(np.int64)


def _check_sign(sign: Sign) -> float | None:
    if sign is None:
        return None
    if sign not in (1, -1):
        raise ValueError(f"sign must be +1, -1 or None, got {sign!r}")
    return float(sign)


def _z(t: float, sign: float | None) -> float:
    if math.isnan(t):
        return float("nan")
    return abs(t) if sign is None else sign * t


def _z_vec(t: np.ndarray, sign: float | None) -> np.ndarray:
    return np.abs(t) if sign is None else sign * t


def _f(v: float) -> float | None:
    v = float(v)
    return None if math.isnan(v) else v
