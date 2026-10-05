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
- Gate 1 direction (ADR 0002 item 3): the observed IC sign must equal the pre-registered sign.
  H0 has none (`expected_sign=None`): either sign, traded in the direction of its IC. The
  sub-period and spread conditions are measured in that gate direction, so a feature registered
  negative passes only when its top-minus-bottom spread is sufficiently *negative*.
- Gate 1 cost (ADR 0002 item 3): 4 x one-way cost x turnover per holding period, i.e.
  4 * cost_bps * min(1, horizon * daily_turnover). A $1 long + $1 short book trades four legs
  when both sides are replaced; ADR 0001's "one-way" wording meant this per-leg rate. The daily
  turnover is scaled to the horizon and capped at full replacement.
- A4 volatility gate (ADR 0002 item 4, `vol_gate`): IC of the residuals of both feature and
  label after a per-day cross-sectional OLS on the vol controls, with the same |NW t|, registered
  sign (positive) and sub-period conditions and no spread condition. A placebo (each ticker's
  trailing mean of the feature) runs alongside; if the placebo also passes, the feature fails.
"""

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from eventtrade.schemas import FEATURE_COLUMNS, SignalReport, Track

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
        n_days=len(ic),
        ic_mean=decay[h0],
        ic_std=float(ic.std(ddof=1)) if len(ic) > 1 else float("nan"),
        ic_t=newey_west_t(ic, nw_lags, h0),
        ic_hit_rate=float((ic > 0).mean()) if len(ic) else float("nan"),
        q_spread_mean=float(spread.mean()) if len(spread) else float("nan"),
        turnover=quantile_turnover(feat, n_quantiles, min_names),
        decay=decay,
    )


def passes_gate(
    report: SignalReport, subperiod_ic_means: Sequence[float], cost_bps: float, *,
    expected_sign: float | None,
) -> tuple[bool, str]:
    """Gate 1 (ADR 0001, amended by ADR 0002 item 3).

    Pass = |NW t| >= 3, the observed IC sign equals `expected_sign` (the pre-registered sign;
    None = H0, either sign), the gate direction held in >= 2 of 3 sub-periods, and the spread in
    the gate direction exceeds 4 x one-way cost x turnover (a $1 long + $1 short book trades four
    legs). `expected_sign` is keyword-only and required: forgetting it must not silently fall back
    to taking the sign from the data.
    """
    expected = _check_sign(expected_sign)
    t = report.ic_t
    if math.isnan(t):
        return False, "fail: NW t is undefined"
    direction, same, n_sub, reasons = _ic_conditions(t, subperiod_ic_means, expected)
    period_turnover = min(1.0, report.horizon * report.turnover) if math.isfinite(report.turnover) else 1.0
    cost = 4.0 * cost_bps * 1e-4 * period_turnover
    net = direction * report.q_spread_mean - cost
    if not math.isfinite(net) or net <= 0:
        reasons.append(
            f"directional spread {direction * report.q_spread_mean:.5f} - cost {cost:.5f}"
            f" = {net:.5f} <= 0"
        )
    if reasons:
        return False, "fail: " + "; ".join(reasons)
    return True, f"pass: NW t={t:.2f}, sign held {same}/{n_sub}, net spread {net:.5f}"


def _check_sign(expected_sign: float | None) -> float | None:
    if expected_sign is None:
        return None
    s = float(expected_sign)
    if s not in (1.0, -1.0):
        raise ValueError(f"expected_sign must be +1, -1 or None, got {expected_sign!r}")
    return s


def _ic_conditions(
    t: float, subperiod_ic_means: Sequence[float], expected: float | None
) -> tuple[float, int, int, list[str]]:
    """IC leg shared by gate 1 and the A4 gate: (gate direction, sub-periods held, n, failures).

    The gate direction is the pre-registered sign, or the observed sign of t when there is none.
    """
    reasons: list[str] = []
    if abs(t) < GATE_MIN_ABS_T:
        reasons.append(f"|NW t|={abs(t):.2f} < {GATE_MIN_ABS_T}")
    observed = 1.0 if t > 0 else -1.0
    direction = observed if expected is None else expected
    if expected is not None and observed != expected:
        reasons.append(f"observed IC sign {observed:+.0f} != pre-registered sign {expected:+.0f}")
    n_sub = len(subperiod_ic_means)
    needed = math.ceil(2 * n_sub / 3)
    same = sum(1 for m in subperiod_ic_means if math.isfinite(m) and m * direction > 0)
    if n_sub == 0 or same < needed:
        reasons.append(f"sign held in {same}/{n_sub} sub-periods (< {needed})")
    return direction, same, n_sub, reasons


# --- A4 volatility gate (ADR 0002 item 4) -----------------------------------------------------

def partial_ic(
    feature: pd.DataFrame, label: pd.DataFrame, controls: pd.DataFrame, horizon: int,
    min_names: int = 5, label_col: str = "value",
) -> pd.Series:
    """Daily Spearman IC of feature and label residuals after a per-day cross-sectional OLS.

    `feature` is one feature (FEATURE_COLUMNS), `label` has `horizon` and `label_col`
    (VOL_LABEL_COLUMNS by default), `controls` is date, ticker and one column per regressor. On
    each date, rows with any NaN/inf are dropped; the date needs at least
    max(min_names, n_regressors + 1 + 3) names. Both the feature and the label are regressed on
    [1, controls] and the IC is the rank correlation of the two residual vectors. A residual that
    is numerically zero (the side is an exact linear function of the controls) leaves the
    correlation undefined and the date is skipped, as in `daily_ic`.
    """
    feat = _one_feature(feature)
    lab = label.loc[label["horizon"] == horizon, ["date", "ticker", label_col]].rename(
        columns={label_col: "label"})
    ctl_cols = [c for c in controls.columns if c not in ("date", "ticker")]
    df = (feat.merge(lab, on=["date", "ticker"], how="inner")
          .merge(controls[["date", "ticker", *ctl_cols]], on=["date", "ticker"], how="inner"))
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["value", "label", *ctl_cols])
    df = _keep_dates_with(df.reset_index(drop=True), max(min_names, len(ctl_cols) + 4))
    out: dict[Any, float] = {}
    for d, g in df.groupby("date", sort=True):
        X = np.column_stack([np.ones(len(g)), g[ctl_cols].to_numpy(dtype=float)])
        rx = _ols_residual(X, g["value"].to_numpy(dtype=float))
        ry = _ols_residual(X, g["label"].to_numpy(dtype=float))
        if rx is None or ry is None:
            continue
        ic = _rank_corr(rx, ry)
        if ic is not None:
            out[d] = ic
    s = pd.Series(out, dtype=float, name="ic")
    s.index.name = "date"
    return s.sort_index()


def placebo_feature(feature: pd.DataFrame) -> pd.DataFrame:
    """Static placebo: each ticker's mean of its own earlier feature values (expanding, to t-1).

    Defined only where the feature itself is observed, so the placebo and the feature are tested
    on the same rows. It carries the per-ticker level and none of the day-t news.
    """
    df = feature[["date", "ticker", "feature", "value"]].sort_values(["ticker", "date"],
                                                                    ignore_index=True)
    _one_feature(df)
    v = df["value"].replace([np.inf, -np.inf], np.nan)
    g = df["ticker"]
    # Shift before summing so day t's own value never enters, not even as float residue.
    s = v.fillna(0.0).groupby(g).shift(1, fill_value=0.0).groupby(g).cumsum()
    n = v.notna().astype(float).groupby(g).shift(1, fill_value=0.0).groupby(g).cumsum()
    df["value"] = (s / n.where(n > 0)).where(v.notna())
    return df[FEATURE_COLUMNS].sort_values(["date", "ticker"], ignore_index=True)


@dataclass(frozen=True)
class VolGateResult:
    """Outcome of the A4 gate for one feature x horizon; `notes()` is what the trial logs."""

    ok: bool
    reason: str
    n_days: int
    ic_mean: float
    ic_t: float
    subperiod_ic: list[float] = field(default_factory=list)
    placebo_ic_mean: float = float("nan")
    placebo_t: float = float("nan")
    placebo_subperiod_ic: list[float] = field(default_factory=list)
    placebo_pass: bool = False

    def notes(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("ok")
        return {"gate": d.pop("reason"), **d}


def vol_gate(
    feature: pd.DataFrame, label: pd.DataFrame, controls: pd.DataFrame, horizon: int, *,
    expected_sign: float = 1.0, min_names: int = 5,
) -> VolGateResult:
    """A4 gate: partial IC passes (|NW t| >= 3, registered sign, 2 of 3 sub-periods) AND the
    trailing-mean placebo, put through the same test, does not pass."""
    expected = _check_sign(expected_sign)
    ic = partial_ic(feature, label, controls, horizon, min_names)
    ok_f, t, subs, same, why = _vol_ic_leg(ic, horizon, expected)
    pic = partial_ic(placebo_feature(feature), label, controls, horizon, min_names)
    ok_p, pt, psubs, _, _ = _vol_ic_leg(pic, horizon, expected)
    reasons = list(why)
    if ok_p:
        reasons.append(f"placebo (trailing per-ticker mean) also passes, NW t={pt:.2f}:"
                       " the IC is a static per-ticker level")
    ok = ok_f and not ok_p
    reason = (f"pass: partial NW t={t:.2f}, sign held {same}/{len(subs)}; placebo NW t={pt:.2f}"
              if ok else "fail: " + "; ".join(reasons))
    return VolGateResult(
        ok=ok, reason=reason, n_days=len(ic),
        ic_mean=float(ic.mean()) if len(ic) else float("nan"), ic_t=t, subperiod_ic=subs,
        placebo_ic_mean=float(pic.mean()) if len(pic) else float("nan"), placebo_t=pt,
        placebo_subperiod_ic=psubs, placebo_pass=ok_p,
    )


def _vol_ic_leg(
    ic: pd.Series, horizon: int, expected: float | None
) -> tuple[bool, float, list[float], int, list[str]]:
    t = newey_west_t(ic, horizon=horizon)
    subs = subperiod_signs(ic)
    if math.isnan(t):
        return False, t, subs, 0, [f"partial NW t is undefined ({len(ic)} dates)"]
    _, same, _, reasons = _ic_conditions(t, subs, expected)
    return not reasons, t, subs, same, reasons


def _ols_residual(X: np.ndarray, y: np.ndarray) -> np.ndarray | None:
    """y minus its least-squares fit on X; None if the residual is numerically zero."""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    r = y - X @ beta
    scale = float(np.linalg.norm(y - y.mean()))
    if scale == 0.0 or float(np.linalg.norm(r)) <= 1e-9 * scale:
        return None
    return r


def _rank_corr(x: np.ndarray, y: np.ndarray) -> float | None:
    rx = pd.Series(x).rank(method="average").to_numpy()
    ry = pd.Series(y).rank(method="average").to_numpy()
    rx, ry = rx - rx.mean(), ry - ry.mean()
    den = math.sqrt(float(rx @ rx) * float(ry @ ry))
    if den <= 0.0:
        return None
    return max(-1.0, min(1.0, float(rx @ ry) / den))


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
