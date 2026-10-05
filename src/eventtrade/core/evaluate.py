"""Univariate signal evaluation: the signal gate of ADR 0001 (gate 1).

Pure. Inputs are long-format frames (`FEATURE_COLUMNS`, `LABEL_COLUMNS`); nothing here reads
the clock, the disk or the hold-out boundary — the caller must pass development-period data only.

Conventions pinned by tests/core/test_evaluate.py:
- Daily IC is cross-sectional: one correlation per date over the names present in both frames
  (NaNs dropped first). Dates with fewer than `min_names` names, or where either side is
  constant, are skipped (absent from the series), never zero-filled.
- Quantiles come from average ranks: q = ceil(rank * n_quantiles / n). Tied names always share a
  quantile, so the assignment does not depend on row order; a large tie block can leave the top
  or bottom quantile empty, and that date then has no spread.
- The HAC t-stat treats the IC series as consecutive observations (gaps ignored). Overlapping
  h-day labels make the IC series MA(h-1), so the default long-run variance is the larger of
  Hansen-Hodrick (uniform, h-1 lags) and Bartlett (max(auto, 2h) lags); a null simulation in the
  tests pins its size.
- Gate 1 trades the signal in the direction of its IC: a negative-IC feature passes when the
  top-minus-bottom spread is sufficiently *negative*. Cost per holding period is
  4 * cost_bps * min(1, horizon * daily_turnover): the spread is $1 long plus $1 short, and
  replacing a fraction tau of both legs trades 4*tau of notional (cost_bps is one-way per unit
  traded). The daily turnover is scaled to the horizon and capped at full replacement.
"""

import math
from collections.abc import Sequence
from typing import Literal

import numpy as np
import pandas as pd

from eventtrade.schemas import SignalReport, Track

IcMethod = Literal["spearman", "pearson"]

GATE_MIN_ABS_T = 3.0
_LABEL_LEGS = ("ret_oo", "ret_overnight", "ret_intraday")


# --- IC ---------------------------------------------------------------------------------------

def daily_ic(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    horizon: int,
    method: IcMethod = "spearman",
    min_names: int = 5,
    label_col: str = "ret_oo",
) -> pd.Series:
    """Cross-sectional IC per date between one feature and the `label_col` return at `horizon`."""
    df = _join(features, labels, horizon, label_col)
    df = _keep_dates_with(df, min_names)
    if df.empty:
        return _empty_series("ic")
    g = df.groupby("date", sort=True)
    if method == "spearman":
        x = g["value"].rank(method="average")
        y = g["label"].rank(method="average")
    elif method == "pearson":
        x, y = df["value"], df["label"]
    else:
        raise ValueError(f"unknown method {method!r}")
    xd = x - x.groupby(df["date"]).transform("mean")
    yd = y - y.groupby(df["date"]).transform("mean")
    parts = pd.DataFrame({"date": df["date"], "xy": xd * yd, "xx": xd * xd, "yy": yd * yd})
    s = parts.groupby("date", sort=True)[["xy", "xx", "yy"]].sum()
    denom = np.sqrt(s["xx"] * s["yy"])
    # Constant feature or constant label on a date: correlation undefined, skip the date.
    ok = denom > 1e-12 * np.maximum(1.0, np.sqrt(s["xx"] + s["yy"]))
    ic = (s["xy"][ok] / denom[ok]).clip(-1.0, 1.0)
    ic.name = "ic"
    ic.index.name = "date"
    return ic


def newey_west_t(series: pd.Series | Sequence[float], lags: int | None = None, horizon: int = 1) -> float:
    """HAC t-stat of the mean: Bartlett with `lags` if given, else the overlap-safe default below.

    Default (lags=None): the larger of two long-run variances, Bartlett with
    max(auto, 2*horizon) lags (auto = floor(4*(T/100)^(2/9)); 2*horizon only when horizon > 1)
    and, for horizon > 1, Hansen-Hodrick (uniform weights over horizon-1 lags). Overlapping
    h-day labels make the IC series MA(h-1); Bartlett down-weights exactly those autocovariances,
    and with lags = h-1 its |t| >= 3 rate under the null was ~6x nominal at h=20.
    """
    x = np.asarray(pd.Series(series).dropna(), dtype=float)
    t_obs = x.size
    if t_obs < 2:
        return float("nan")
    mean = float(x.mean())
    if np.ptp(x) == 0.0:  # constant series: exact zero variance (avoids rounding residue)
        return float("nan") if mean == 0.0 else math.copysign(math.inf, mean)
    d = x - mean
    if lags is not None:
        lrv = _long_run_var(d, int(lags), "bartlett")
    else:
        auto = math.floor(4 * (t_obs / 100) ** (2 / 9))
        lrv = _long_run_var(d, max(auto, 2 * horizon) if horizon > 1 else auto, "bartlett")
        if horizon > 1:
            hh = _long_run_var(d, horizon - 1, "uniform")
            lrv = max(lrv, hh)  # Hansen-Hodrick can go non-positive; then Bartlett stands
    if lrv <= 0.0:
        return float("nan") if mean == 0.0 else math.copysign(math.inf, mean)
    return mean / math.sqrt(lrv / t_obs)


def _long_run_var(d: np.ndarray, lags: int, kernel: Literal["bartlett", "uniform"]) -> float:
    """Kernel-weighted sum of autocovariances (1/T normalisation) of a demeaned series."""
    t_obs = d.size
    lags = min(max(lags, 0), t_obs - 1)
    lrv = float(d @ d) / t_obs
    for k in range(1, lags + 1):
        w = 1.0 if kernel == "uniform" else 1.0 - k / (lags + 1)
        lrv += 2.0 * w * float(d[k:] @ d[:-k]) / t_obs
    return lrv


def subperiod_signs(ic_series: pd.Series, n_periods: int = 3) -> list[float]:
    """Mean IC in each of `n_periods` consecutive, equal-count sub-periods (date order)."""
    s = pd.Series(ic_series).dropna().sort_index()
    if len(s) < n_periods:
        return [float("nan")] * n_periods
    return [float(chunk.mean()) for chunk in np.array_split(s.to_numpy(dtype=float), n_periods)]


def ic_by_return_leg(
    features: pd.DataFrame, labels: pd.DataFrame, horizon: int, min_names: int = 5
) -> dict[str, float]:
    """Mean daily rank IC against ret_oo, ret_overnight and ret_intraday (stale-pricing diagnostic).

    ret_overnight (close t -> open t+1) is NOT tradable from a signal formed at t's cutoff; a
    feature whose IC lives there and not in ret_oo is detecting stale prices (e.g. country ETFs
    whose underlying market closed before the US session), not a tradable edge.
    """
    out: dict[str, float] = {}
    for leg in _LABEL_LEGS:
        ic = daily_ic(features, labels, horizon, "spearman", min_names, label_col=leg)
        out[leg] = float(ic.mean()) if len(ic) else float("nan")
    return out


# --- Quantiles --------------------------------------------------------------------------------

def assign_quantiles(values: pd.Series, n_quantiles: int = 5) -> pd.Series:
    """Quantile 1..n_quantiles from average ranks; tied values share a quantile, NaN stays NaN."""
    v = pd.Series(values, dtype=float)
    n = int(v.notna().sum())
    if n == 0:
        return pd.Series(np.nan, index=v.index)
    r = v.rank(method="average")
    q = np.ceil(r * n_quantiles / n - 1e-9).clip(1, n_quantiles)
    return q


def quantile_spread(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    horizon: int,
    n_quantiles: int = 5,
    min_names: int = 5,
) -> pd.Series:
    """Per date: mean ret_oo of the top feature quantile minus mean ret_oo of the bottom one."""
    df = _join(features, labels, horizon, "ret_oo")
    df = _keep_dates_with(df, max(min_names, n_quantiles))
    if df.empty:
        return _empty_series("q_spread")
    df = df.assign(q=_quantiles_by_date(df, n_quantiles))
    top = df[df["q"] == n_quantiles].groupby("date")["label"].mean()
    bottom = df[df["q"] == 1].groupby("date")["label"].mean()
    spread = (top - bottom).dropna().sort_index()
    spread.name = "q_spread"
    spread.index.name = "date"
    return spread


def quantile_turnover(features: pd.DataFrame, n_quantiles: int = 5, min_names: int = 5) -> float:
    """Mean fraction of the top and bottom quantile sets replaced between consecutive dates."""
    df = _one_feature(features).dropna(subset=["value"])
    df = _keep_dates_with(df, max(min_names, n_quantiles))
    if df.empty:
        return float("nan")
    df = df.assign(q=_quantiles_by_date(df, n_quantiles))
    rates: list[float] = []
    for q in (1, n_quantiles):
        sets = df[df["q"] == q].groupby("date", sort=True)["ticker"].agg(frozenset)
        prev: frozenset | None = None
        for cur in sets.reindex(sorted(df["date"].unique())):
            if isinstance(cur, frozenset) and isinstance(prev, frozenset) and cur:
                rates.append(1.0 - len(cur & prev) / len(cur))
            prev = cur if isinstance(cur, frozenset) else None
    return float(np.mean(rates)) if rates else float("nan")


# --- Report and gate --------------------------------------------------------------------------

def evaluate_signal(
    feature_df: pd.DataFrame,
    labels: pd.DataFrame,
    feature_name: str,
    horizons: Sequence[int],
    track: Track,
    n_quantiles: int = 5,
    min_names: int = 5,
    nw_lags: int | None = None,
) -> SignalReport:
    """SignalReport for `feature_name` at horizons[0]; decay holds the mean IC at every horizon."""
    if not horizons:
        raise ValueError("horizons must be non-empty")
    feat = feature_df[feature_df["feature"] == feature_name]
    if feat.empty:
        raise ValueError(f"feature {feature_name!r} not in feature_df")
    decay: dict[int, float] = {}
    ics: dict[int, pd.Series] = {}
    for h in horizons:
        ics[h] = daily_ic(feat, labels, h, "spearman", min_names)
        decay[int(h)] = float(ics[h].mean()) if len(ics[h]) else float("nan")
    h0 = int(horizons[0])
    ic = ics[h0]
    spread = quantile_spread(feat, labels, h0, n_quantiles, min_names)
    return SignalReport(
        feature=feature_name,
        horizon=h0,
        track=track,
        n_days=int(len(ic)),
        ic_mean=decay[h0],
        ic_std=float(ic.std(ddof=1)) if len(ic) > 1 else float("nan"),
        ic_t=newey_west_t(ic, nw_lags, h0),
        ic_hit_rate=float((ic > 0).mean()) if len(ic) else float("nan"),
        q_spread_mean=float(spread.mean()) if len(spread) else float("nan"),
        turnover=quantile_turnover(feat, n_quantiles, min_names),
        decay=decay,
    )


def passes_gate(report: SignalReport, subperiod_ic_means: Sequence[float], cost_bps: float) -> tuple[bool, str]:
    """ADR 0001 gate 1: |NW t| >= 3, sign held in >= 2 of 3 sub-periods, directional spread > costs."""
    t = report.ic_t
    if math.isnan(t):
        return False, "fail: NW t is undefined"
    reasons: list[str] = []
    if abs(t) < GATE_MIN_ABS_T:
        reasons.append(f"|NW t|={abs(t):.2f} < {GATE_MIN_ABS_T}")
    sign = 1.0 if t > 0 else -1.0
    n_sub = len(subperiod_ic_means)
    needed = math.ceil(2 * n_sub / 3)
    same = sum(1 for m in subperiod_ic_means if math.isfinite(m) and m * sign > 0)
    if n_sub == 0 or same < needed:
        reasons.append(f"sign held in {same}/{n_sub} sub-periods (< {needed})")
    period_turnover = min(1.0, report.horizon * report.turnover) if math.isfinite(report.turnover) else 1.0
    # The spread is $1 long + $1 short; replacing a fraction tau of both legs trades 4*tau notional.
    cost = 4.0 * cost_bps * 1e-4 * period_turnover
    net = sign * report.q_spread_mean - cost
    if not math.isfinite(net) or net <= 0:
        reasons.append(
            f"directional spread {sign * report.q_spread_mean:.5f} - cost {cost:.5f} = {net:.5f} <= 0"
        )
    if reasons:
        return False, "fail: " + "; ".join(reasons)
    return True, f"pass: NW t={t:.2f}, sign held {same}/{n_sub}, net spread {net:.5f}"


# --- helpers ----------------------------------------------------------------------------------

def _one_feature(features: pd.DataFrame) -> pd.DataFrame:
    names = features["feature"].unique()
    if len(names) > 1:
        raise ValueError(f"expected one feature, got {len(names)}: {list(names)[:5]}")
    return features[["date", "ticker", "value"]]


def _join(features: pd.DataFrame, labels: pd.DataFrame, horizon: int, label_col: str) -> pd.DataFrame:
    feat = _one_feature(features)
    lab = labels.loc[labels["horizon"] == horizon, ["date", "ticker", label_col]]
    df = feat.merge(lab, on=["date", "ticker"], how="inner").rename(columns={label_col: "label"})
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["value", "label"])
    return df.reset_index(drop=True)


def _keep_dates_with(df: pd.DataFrame, min_names: int) -> pd.DataFrame:
    counts = df.groupby("date")["ticker"].transform("size")
    return df[counts >= min_names].reset_index(drop=True)


def _quantiles_by_date(df: pd.DataFrame, n_quantiles: int) -> pd.Series:
    r = df.groupby("date")["value"].rank(method="average")
    n = df.groupby("date")["value"].transform("count")
    return np.ceil(r * n_quantiles / n - 1e-9).clip(1, n_quantiles)


def _empty_series(name: str) -> pd.Series:
    s = pd.Series(dtype=float, name=name)
    s.index.name = "date"
    return s
