"""ADR 0012 (insider purchases on fresh small/mid caps): raw prices, tradability, price-error
screen, robust IWM-hedged outcomes, ticker and issuer identity, survivorship classification.
Pure: no I/O. Wide frames are indexed by trading day (Timestamp), columns = tickers."""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

MIN_RAW_PRICE = 5.0
MIN_DOLLAR_VOLUME = 1_000_000.0
DV_WINDOW = 60
COST_TIER_DV = 5_000_000.0
COST_LEG_THIN, COST_LEG_THICK, COST_LEG_HEDGE, COST_LEG_STRESS = 0.0050, 0.0025, 0.0001, 0.0075
ERR_UP, ERR_DOWN, ERR_REVERT, ERR_DAYS = 3.0, -0.75, 2.0 / 3.0, 5
EXIT_GRACE = 5
MISMATCH_LO, MISMATCH_HI, MISMATCH_SHARE = 0.67, 1.5, 0.20
FAIL_RETURN = -0.30
LISTING_FORMS = ("8-A12B",)
PERIODIC = ("10-Q", "10-K", "10-Q/A", "10-K/A")


def split_factor(index: pd.DatetimeIndex, splits: pd.DataFrame) -> pd.Series:
    """Product of split ratios dated strictly after each day: raw = split-adjusted x factor."""
    f = pd.Series(1.0, index=index)
    for d, r in zip(pd.to_datetime(splits["date"]), splits["ratio"].astype(float), strict=True):
        if r > 0 and math.isfinite(r):
            f[index < d.normalize()] *= r
    return f


def raw_close(close: pd.DataFrame, splits: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    out = close.copy()
    for t in close.columns:
        s = splits.get(t)
        if s is not None and len(s):
            out[t] = close[t] * split_factor(close.index, s)
    return out


def median_dollar_volume(close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    """Median close x volume over the 60 trading days ending at t (split-neutral)."""
    return (close * volume).rolling(DV_WINDOW, min_periods=DV_WINDOW // 2).median()


def tradable(raw: pd.DataFrame, mdv: pd.DataFrame) -> pd.DataFrame:
    return (raw >= MIN_RAW_PRICE) & (mdv >= MIN_DOLLAR_VOLUME)


def clean_opens(open_: pd.DataFrame, volume: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Opens with zero-volume days and reverting spikes set to NaN; (opens, n_errors).

    A spike: open-to-open return > +300% or < -75% that comes back by >= 2/3 within 5 days."""
    o = open_.where(volume > 0)
    n_err = 0
    arr = o.to_numpy(dtype=float, copy=True)
    for c in range(arr.shape[1]):
        col = arr[:, c]
        idx = np.flatnonzero(np.isfinite(col))
        for a, b in itertools.pairwise(idx):
            r = col[b] / col[a] - 1.0
            if r > ERR_UP or r < ERR_DOWN:
                fut = col[b + 1:b + 1 + ERR_DAYS]
                fut = fut[np.isfinite(fut)]
                if fut.size and np.any(np.abs(fut / col[a] - 1.0) <= abs(r) * (1 - ERR_REVERT)):
                    col[b] = np.nan
                    n_err += 1
    return pd.DataFrame(arr, index=o.index, columns=o.columns), n_err


def _next_valid(col: np.ndarray) -> np.ndarray:
    """nxt[i] = smallest j >= i with finite col[j], else -1."""
    n = len(col)
    nxt = np.full(n, -1)
    last = -1
    for i in range(n - 1, -1, -1):
        if np.isfinite(col[i]):
            last = i
        nxt[i] = last
    return nxt


def hedged_outcomes(open_: pd.DataFrame, hedge_open: pd.Series, h: int) -> tuple[pd.DataFrame, int]:
    """Long [date, ticker, value]: open(exit)/open(t+1) - 1 minus the hedge's over the same days.

    Entry needs a valid open at t+1. Exit = first valid open at or after t+1+h, within 5 days of
    it; else no outcome. Returns (outcomes, number of delayed exits)."""
    hv = hedge_open.to_numpy(dtype=float)
    n = len(open_.index)
    rows = []
    delayed = 0
    for t in open_.columns:
        col = open_[t].to_numpy(dtype=float)
        nxt = _next_valid(col)
        for i in range(n - 1 - h):
            e = i + 1
            if not np.isfinite(col[e]) or not np.isfinite(hv[e]):
                continue
            target = e + h
            x = nxt[target] if target < n else -1
            if x < 0 or x - target > EXIT_GRACE or not np.isfinite(hv[x]):
                continue
            delayed += x > target
            rows.append((open_.index[i], t, (col[x] / col[e] - 1.0) - (hv[x] / hv[e] - 1.0)))
    return pd.DataFrame(rows, columns=["date", "ticker", "value"]), delayed


def ticker_mismatch(trans: pd.DataFrame, raw: pd.DataFrame) -> dict[str, float]:
    """Per ticker, the share of Form 4 purchases whose price/raw close (on or before the
    transaction date) lies outside [0.67, 1.5]. `trans`: ticker, trans_date, price."""
    out: dict[str, float] = {}
    for t, g in trans.groupby("ticker"):
        if t not in raw.columns:
            continue
        s = raw[t].dropna()
        if s.empty:
            continue
        pos = s.index.searchsorted(pd.to_datetime(g["trans_date"]).to_numpy(), side="right") - 1
        ok = pos >= 0
        ratio = g["price"].to_numpy(dtype=float)[ok] / s.to_numpy()[pos[ok]]
        bad = (ratio < MISMATCH_LO) | (ratio > MISMATCH_HI)
        out[t] = float(bad.mean()) if len(ratio) else float("nan")
    return out


def issuer_ok(cik_meta: Mapping[str, Any], filings: pd.DataFrame, dates: pd.Series) -> np.ndarray:
    """ADR 0012 amendment 3 for one issuer: which event/placebo dates are admissible.

    Excluded: SIC 6770 (blank checks); no 10-Q/10-K at all; dates before a name change away from
    an '... Acquisition ...' name; dates before the first 8-A12B (if none on file: dates
    without a 10-Q/10-K in the previous 180 days)."""
    d = pd.to_datetime(dates).to_numpy()
    if str(cik_meta.get("sic") or "") == "6770":
        return np.zeros(len(d), dtype=bool)
    f = filings.copy()
    f["filing_date"] = pd.to_datetime(f["filing_date"])
    per = np.sort(f[f["form"].isin(PERIODIC)]["filing_date"].to_numpy())
    if per.size == 0:
        return np.zeros(len(d), dtype=bool)
    ok = np.ones(len(d), dtype=bool)
    for fn in json.loads(cik_meta.get("former_names") or "[]"):
        if "acquisition" in str(fn.get("name", "")).lower() and fn.get("to"):
            ok &= d >= np.datetime64(pd.Timestamp(fn["to"]).normalize())
    listing = f[f["form"].isin(LISTING_FORMS)]["filing_date"]
    if len(listing):
        ok &= d >= listing.min().to_datetime64()
    else:
        k = np.searchsorted(per, d, side="right") - 1
        last = np.where(k >= 0, per[np.maximum(k, 0)], np.datetime64("NaT", "ns"))
        recent = (k >= 0) & ((d - last) <= np.timedelta64(180, "D"))
        ok &= recent
    return ok


def classify_exit(filings: pd.DataFrame) -> str:
    """'failure' | 'merger' | 'other' for an issuer that is no longer listed (amendment 1)."""
    f = filings.copy()
    f["filing_date"] = pd.to_datetime(f["filing_date"])
    items = f["items"].fillna("").astype(str)
    if ((f["form"] == "8-K") & items.str.contains(r"\b1\.03\b")).any():
        return "failure"
    dereg = f[f["form"].isin(["25", "25-NSE", "15-12B", "15-12G", "15-15D"])]["filing_date"]
    end = dereg.max() if len(dereg) else f["filing_date"].max()
    lo = end - pd.Timedelta(days=365)
    merger = f[(f["filing_date"] >= lo) & (f["filing_date"] <= end)
               & ((f["form"].isin(["DEFM14A", "S-4"]))
                  | ((f["form"] == "8-K") & items.str.contains(r"\b2\.01\b")))]
    if len(merger):
        return "merger"
    if len(dereg):
        return "failure"
    return "other"


def stressed_mean(mean: float, f_fail: float) -> float:
    return (1.0 - f_fail) * mean + f_fail * FAIL_RETURN


def cost_round_trip(mdv_at_t: np.ndarray, stress: bool = False) -> np.ndarray:
    leg = np.where(mdv_at_t < COST_TIER_DV, COST_LEG_THIN, COST_LEG_THICK)
    if stress:
        leg = np.full(len(mdv_at_t), COST_LEG_STRESS)
    return 2 * leg + 2 * COST_LEG_HEDGE


def strata3(rel20: pd.DataFrame, rel250: pd.DataFrame, periodic: Mapping[str, Sequence[pd.Timestamp]],
            cal: pd.DatetimeIndex, mask: np.ndarray, two_dim: bool = False) -> pd.DataFrame:
    """[date, ticker, stratum]: earnings proximity x tercile of 20-day x tercile of 250-day
    IWM-relative return (within stock). two_dim=True drops the 250-day dimension."""
    rows = []
    idx = np.arange(len(cal))
    for tk in rel20.columns:
        fpos = np.sort(cal.searchsorted(pd.DatetimeIndex(periodic.get(tk, [])), side="left"))
        k = np.searchsorted(fpos, idx, side="right") - 1
        since = np.where(k >= 0, idx - fpos[np.maximum(k, 0)], -1) if fpos.size else np.full(len(cal), -1)
        prox = np.select([since < 0, since <= 10, since <= 30], ["none", "0-10", "11-30"], "31+")
        parts = [prox.astype(str)]
        dims = [rel20[tk].to_numpy()] + ([] if two_dim else [rel250[tk].to_numpy()])
        ok = mask.copy()
        for r in dims:
            ok &= np.isfinite(r)
        for r in dims:
            if ok.sum() >= 3:
                q1, q2 = np.quantile(r[ok], [1 / 3, 2 / 3])
                parts.append(np.where(r <= q1, "lo", np.where(r <= q2, "mid", "hi")))
            else:
                parts.append(np.full(len(cal), "mid"))
        st = parts[0]
        for p in parts[1:]:
            st = np.char.add(np.char.add(st, "|"), p.astype(str))
        rows.append(pd.DataFrame({"date": cal[ok], "ticker": tk, "stratum": st[ok]}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["date", "ticker", "stratum"])
