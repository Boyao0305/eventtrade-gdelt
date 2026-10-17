"""ADR 0019 (pre-earnings informed-trading footprints, hypothesis family X): per-day intraday
order flow and late-day pressure from 15-minute bars, and window-vs-baseline aggregation around
an announcement day. Pure: no I/O.

Positions index the shared trading-day calendar. For an announcement at position a:
W = [a-12, a-3], B = [a-72, a-13] minus days within [e-3, e+10] of any earlier 8-K 2.02 day e
of the stock (amendment 6). a-2 (the decision day) is never used by these features.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
N_BARS = 26
K_1500, K_1200, K_1300 = 22, 10, 14
W_LO, W_HI = 12, 3
B_LO, B_HI = 72, 13
MIN_W_DAYS, MIN_B_DAYS = 8, 40


def bar_arrays(bars: pd.DataFrame, days: Sequence[date]) -> dict[str, np.ndarray]:
    """One ticker's regular-session 15-minute bars on the [day, k] grid: open, close, volume
    (NaN where no bar)."""
    nd = len(days)
    out = {c: np.full((nd, N_BARS), np.nan) for c in ("open", "close", "volume")}
    if len(bars):
        et = pd.to_datetime(bars["ts"].to_numpy(dtype="int64"), unit="s", utc=True).tz_convert(NY)
        k = ((et.hour * 60 + et.minute) - 570) // 15
        pos = {d: i for i, d in enumerate(days)}
        di = np.array([pos.get(d, -1) for d in et.date])
        ok = (di >= 0) & (k >= 0) & (k < N_BARS)
        for c, arr in out.items():
            arr[di[ok], np.asarray(k)[ok]] = bars[c].to_numpy(dtype=float)[ok]
    return out


def daily_flow(asset: dict[str, np.ndarray], hedge: dict[str, np.ndarray],
               early: np.ndarray) -> dict[str, np.ndarray]:
    """Per day: up volume, down volume (bars whose return beats / trails the same bar of the
    hedge), total volume of classified bars, and the late-day hedged return (15:00 bar open to
    the last bar close; 12:00 on early-close days, whose bars from 13:00 are dropped)."""
    o, c, v = asset["open"].copy(), asset["close"].copy(), asset["volume"].copy()
    ho, hc = hedge["open"], hedge["close"]
    early = np.asarray(early, dtype=bool)
    for arr in (o, c, v):
        arr[early, K_1300:] = np.nan
    with np.errstate(invalid="ignore", divide="ignore"):
        rel = (c / o - 1) - (hc / ho - 1)
    up = np.where(rel > 0, v, 0.0)
    dn = np.where(rel < 0, v, 0.0)
    cls = np.where(np.isfinite(rel) & np.isfinite(v), v, 0.0)
    n_bars = np.isfinite(rel).sum(axis=1)
    last = np.where(np.isfinite(c), np.arange(N_BARS)[None, :], -1).max(axis=1)
    idx = np.arange(len(c))
    start = np.where(early, K_1200, K_1500)
    with np.errstate(invalid="ignore", divide="ignore"):
        late = (c[idx, np.maximum(last, 0)] / o[idx, start] - 1) \
            - (hc[idx, np.maximum(last, 0)] / ho[idx, start] - 1)
    late[last < start] = np.nan
    return {"up": up.sum(axis=1), "down": dn.sum(axis=1), "vol": cls.sum(axis=1),
            "has": n_bars > 0, "late": late}


def baseline_mask(a: int, n: int, earn_pos: np.ndarray) -> np.ndarray:
    """Boolean mask of B positions for an announcement at a (amendment 6)."""
    m = np.zeros(n, dtype=bool)
    lo, hi = max(0, a - B_LO), a - B_HI
    if hi < lo:
        return m
    m[lo:hi + 1] = True
    for e in earn_pos[earn_pos < a]:
        m[max(0, e - 3):min(n, e + 11)] = False
    return m


def window_mask(a: int, n: int) -> np.ndarray:
    m = np.zeros(n, dtype=bool)
    lo, hi = max(0, a - W_LO), a - W_HI
    if hi >= lo:
        m[lo:hi + 1] = True
    return m


def flow_features(flow: dict[str, np.ndarray], a: int, earn_pos: np.ndarray) -> dict[str, float]:
    """X2 (hedged order-flow imbalance, W minus B) and X6 (sum of late-day hedged returns over W).
    NaN unless bars exist on >= 8 W days and >= 40 B days (amendment 8)."""
    n = len(flow["up"])
    w, b = window_mask(a, n), baseline_mask(a, n, earn_pos)
    w &= flow["has"]
    b &= flow["has"]
    nan = float("nan")
    if w.sum() < MIN_W_DAYS or b.sum() < MIN_B_DAYS:
        return {"X2": nan, "X6": nan}

    def imb(m: np.ndarray) -> float:
        tot = flow["vol"][m].sum()
        return float((flow["up"][m].sum() - flow["down"][m].sum()) / tot) if tot > 0 else nan

    lt = flow["late"][w]
    return {"X2": imb(w) - imb(b), "X6": float(np.nansum(lt)) if np.isfinite(lt).any() else nan}


def volume_ratio(v: np.ndarray, a: int, earn_pos: np.ndarray) -> float:
    """log(mean daily volume over W / mean over B) for one stock (B with exclusions)."""
    n = len(v)
    w, b = window_mask(a, n), baseline_mask(a, n, earn_pos)
    vw, vb = v[w], v[b]
    vw, vb = vw[np.isfinite(vw) & (vw > 0)], vb[np.isfinite(vb) & (vb > 0)]
    if len(vw) < MIN_W_DAYS or len(vb) < MIN_B_DAYS:
        return float("nan")
    return float(np.log(vw.mean() / vb.mean()))


def market_log_ratio(vol: np.ndarray) -> np.ndarray:
    """Per position a: the cross-sectional median over stocks of log(mean V[a-12..a-3] /
    mean V[a-72..a-13]) (no exclusions; the calendar term of X1)."""
    v = pd.DataFrame(np.where(vol > 0, vol, np.nan))
    mw = v.rolling(W_LO - W_HI + 1, min_periods=MIN_W_DAYS).mean().shift(W_HI)
    mb = v.rolling(B_LO - B_HI + 1, min_periods=MIN_B_DAYS).mean().shift(B_HI)
    lr = np.log(mw / mb)
    return lr.median(axis=1, skipna=True).to_numpy()


def short_feature(ratio: np.ndarray, a: int, earn_pos: np.ndarray) -> float:
    """X3: mean daily FINRA short ratio over W minus over B (NaN where too few days)."""
    n = len(ratio)
    w, b = window_mask(a, n), baseline_mask(a, n, earn_pos)
    rw, rb = ratio[w], ratio[b]
    rw, rb = rw[np.isfinite(rw)], rb[np.isfinite(rb)]
    if len(rw) < MIN_W_DAYS or len(rb) < MIN_B_DAYS:
        return float("nan")
    return float(rw.mean() - rb.mean())


def announcement_day(filing_date: date, report_date: date | None, acceptance: str | None,
                     cal: pd.DatetimeIndex) -> pd.Timestamp | None:
    """Amendment 1: first trading day on or after min(reportDate, filing date); one day later
    when the 8-K was accepted after 16:00 ET and reportDate is not before the filing date."""
    base = filing_date if report_date is None else min(report_date, filing_date)
    p = int(cal.searchsorted(pd.Timestamp(base), "left"))
    late = False
    if acceptance and (report_date is None or report_date >= filing_date):
        try:
            t = datetime.fromisoformat(acceptance).astimezone(NY)
            late = t.date() == filing_date and t.time() >= time(16, 0)
        except ValueError:
            late = False
    p += int(late)
    return cal[p] if p < len(cal) else None
