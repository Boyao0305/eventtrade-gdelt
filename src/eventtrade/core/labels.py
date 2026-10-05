"""Forward-return and forward-volatility labels. Pure.

Alignment rules (pinned by tests/core/test_labels.py):

- A label row exists for every (date, ticker) bar in `prices`; the signal is formed at that day's
  cutoff and entered at the next session's `adj_open`.
- "Next session" is positional on a shared trading calendar: the sorted union of all dates in
  `prices`, or `trading_days` if given. A ticker missing a bar on a calendar day therefore gives
  NaN for every label that needs that bar — it is never skipped over (which would silently
  lengthen the horizon) and never forward-filled. Pass the exchange calendar as `trading_days`
  when `prices` holds a single ticker, otherwise its own gaps are invisible.
- Bars beyond the end of `prices` are missing, so truncating `prices` before
  `config.HOLDOUT_START` is enough to keep hold-out prices out of every label.
- Dates come back as `datetime64[ns]` (midnight).
- A4 (ADR 0002 item 4) uses `vol_surprise_label`, a vol *shock* (forward / trailing vol), with
  `vol_controls` as regressors; `realized_vol_label` is the vol *level* that experiment 0003
  showed ranks with any static per-ticker offset, kept as a diagnostic only.
"""

from datetime import date

import numpy as np
import pandas as pd

from eventtrade.schemas import (
    LABEL_COLUMNS,
    PRICE_COLUMNS,
    VOL_CONTROL_COLUMNS,
    VOL_LABEL_COLUMNS,
)

__all__ = [
    "VOL_CONTROL_COLUMNS",
    "VOL_CONTROL_NAMES",
    "VOL_LABEL_COLUMNS",
    "make_labels",
    "realized_vol_label",
    "vol_controls",
    "vol_surprise_label",
]

# A4 v2 (ADR 0002 item 4): trailing-vol controls, all ending at t. Columns of `vol_controls` are
# VOL_CONTROL_COLUMNS == ["date", "ticker", *VOL_CONTROL_NAMES].
VOL_CONTROL_NAMES = VOL_CONTROL_COLUMNS[2:]
SURPRISE_TRAILING_DAYS = 22
_CONTROL_WINDOWS = {"ln_rv5": 5, "ln_rv22": 22, "ln_rv60": 60}
_LONG_RUN_MIN_RETURNS = 60
# Mean squared daily log return treated as zero (rolling sums can leave ~1e-20 residue after a
# run of identical closes); 1e-14 is a daily vol of 1e-7, far below any traded asset.
_ZERO_VAR = 1e-14


def make_labels(
    prices: pd.DataFrame, horizons: list[int], trading_days: list[date] | None = None
) -> pd.DataFrame:
    """Forward open-to-open, overnight and first-day intraday returns per (date, ticker, horizon)."""
    hs = _check_horizons(horizons)
    o, c, keys = _wide(prices, ["adj_open", "adj_close"], trading_days)
    open1 = o.shift(-1)
    overnight = _stack(open1 / c - 1.0, keys)
    intraday = _stack(c.shift(-1) / open1 - 1.0, keys)
    parts = []
    for h in hs:
        part = keys.copy()
        part["horizon"] = h
        part["ret_oo"] = _stack(o.shift(-(1 + h)) / open1 - 1.0, keys)
        part["ret_overnight"] = overnight
        part["ret_intraday"] = intraday
        parts.append(part)
    out = pd.concat(parts, ignore_index=True)[LABEL_COLUMNS]
    out["horizon"] = out["horizon"].astype("int64")
    return out.sort_values(["date", "ticker", "horizon"], ignore_index=True)


def realized_vol_label(
    prices: pd.DataFrame, horizon: int, trading_days: list[date] | None = None,
    demean: bool = False,
) -> pd.DataFrame:
    """Realised volatility of daily close-to-close log returns over t+1..t+h (VOL_LABEL_COLUMNS).

    Returns r_s = ln(adj_close[s] / adj_close[s-1]) for s in t+1..t+h (h returns, the first one
    spans t close -> t+1 close). Default (`demean=False`) is the zero-mean realised vol
    sqrt(mean(r_s^2)), defined for h=1 as |r_{t+1}|. `demean=True` gives the sample std
    (ddof=1), NaN for h=1. Daily units, not annualised. Any missing close in t..t+h -> NaN.
    """
    (h,) = _check_horizons([horizon])
    c, keys = _wide(prices, ["adj_close"], trading_days)
    r = np.log(c / c.shift(1))
    if demean:
        vol = r.rolling(h, min_periods=h).std(ddof=1)
    else:
        vol = np.sqrt((r * r).rolling(h, min_periods=h).mean())
    out = keys.copy()
    out["horizon"] = h
    out["value"] = _stack(vol.shift(-h), keys)
    out["horizon"] = out["horizon"].astype("int64")
    return out[VOL_LABEL_COLUMNS].sort_values(["date", "ticker"], ignore_index=True)


def vol_surprise_label(
    prices: pd.DataFrame, horizon: int, trading_days: list[date] | None = None
) -> pd.DataFrame:
    """A4 v2 label: ln(forward realised vol over t+1..t+h / trailing 22-day realised vol to t).

    Both vols are zero-mean RMS of close-to-close log returns (as `realized_vol_label`). The
    forward leg uses r_{t+1}..r_{t+h}; the trailing leg r_{t-21}..r_t, so it needs closes
    t-22..t. A zero vol on either side, or any missing close, gives NaN (never +/-inf).
    """
    (h,) = _check_horizons([horizon])
    c, keys = _wide(prices, ["adj_close"], trading_days)
    r2 = np.log(c / c.shift(1)) ** 2
    fwd = _ln_rms(r2, h).shift(-h)
    trail = _ln_rms(r2, SURPRISE_TRAILING_DAYS)
    out = keys.copy()
    out["horizon"] = h
    out["value"] = _stack(fwd - trail, keys)
    out["horizon"] = out["horizon"].astype("int64")
    return out[VOL_LABEL_COLUMNS].sort_values(["date", "ticker"], ignore_index=True)


def vol_controls(prices: pd.DataFrame, trading_days: list[date] | None = None) -> pd.DataFrame:
    """Per (date, ticker): ln rv5, ln rv22, ln rv60 and ln expanding long-run vol, all ending at t.

    Each is ln sqrt(mean r_s^2) of close-to-close log returns over the window ending with r_t
    (= ln(C_t / C_{t-1})); the long-run vol uses every return up to t and needs at least 60. They
    use t's close, after the 15:30 decision cutoff: they are a statistical baseline for the A4 gate,
    not a trading input, and including r_t absorbs more volatility clustering (the conservative
    choice). Zero vol or any missing close in a rolling window -> NaN; the long-run vol averages
    the returns it has.
    """
    c, keys = _wide(prices, ["adj_close"], trading_days)
    r2 = np.log(c / c.shift(1)) ** 2
    out = keys.copy()
    for name, w in _CONTROL_WINDOWS.items():
        out[name] = _stack(_ln_rms(r2, w), keys)
    lr = r2.expanding(min_periods=_LONG_RUN_MIN_RETURNS).mean()
    out["ln_rv_lr"] = _stack(0.5 * np.log(lr.where(lr > _ZERO_VAR)), keys)
    return out[["date", "ticker", *VOL_CONTROL_NAMES]].sort_values(["date", "ticker"],
                                                                    ignore_index=True)


def _ln_rms(r2: pd.DataFrame, window: int) -> pd.DataFrame:
    """ln sqrt(mean of `window` squared returns ending at each row); NaN if incomplete or zero."""
    m = r2.rolling(window, min_periods=window).mean()
    return 0.5 * np.log(m.where(m > _ZERO_VAR))


def _check_horizons(horizons: list[int]) -> list[int]:
    hs = sorted({int(h) for h in horizons})
    if not hs or hs[0] < 1:
        raise ValueError(f"horizons must be positive integers, got {horizons!r}")
    return hs


def _wide(
    prices: pd.DataFrame, cols: list[str], trading_days: list[date] | None
) -> tuple:
    """Pivot `cols` to date x ticker on the shared calendar; also return the (date, ticker) keys."""
    missing = [c for c in PRICE_COLUMNS[:2] + cols if c not in prices.columns]
    if missing:
        raise ValueError(f"prices is missing columns {missing}")
    df = prices[["date", "ticker", *cols]].copy()
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    if df.duplicated(["date", "ticker"]).any():
        raise ValueError("prices has duplicate (date, ticker) rows")
    cal = df["date"].unique() if trading_days is None else pd.to_datetime(
        pd.Series(list(trading_days))).astype("datetime64[ns]").unique()
    cal = pd.DatetimeIndex(sorted(set(cal) | set(df["date"].unique())), name="date")
    wides = [
        df.pivot(index="date", columns="ticker", values=c).reindex(cal).astype("float64")
        for c in cols
    ]
    keys = df[["date", "ticker"]].sort_values(["date", "ticker"], ignore_index=True)
    return (*wides, keys)


def _stack(wide: pd.DataFrame, keys: pd.DataFrame) -> np.ndarray:
    """Read `wide` at each (date, ticker) of `keys`, in key order."""
    return wide.stack().reindex(pd.MultiIndex.from_frame(keys)).to_numpy(dtype="float64")
