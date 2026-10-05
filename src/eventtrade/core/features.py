"""GDELT daily aggregates -> per-ticker features on trading days. Pure.

Pipeline (`build_features`):

1. `to_trading_days`  session_date -> first trading day >= session_date; `value_sum`, `n` summed.
   Weekend/holiday news therefore lands on the next session (Fri 16:00 ET row -> Saturday
   session_date [adapter cutoff, see `session_date_of`] -> Monday).
2. `map_to_tickers`   raw re-keying entity -> ticker, summing `value_sum` and `n` over the asset's
   entities (ETF: fips_countries + gkg_themes; stock: org_aliases). Because the combination is
   done on the additive raw sums, every downstream statistic is exact: a ticker's `mean` is the
   n-weighted (article/event-weighted) mean over its entities, its `count` the sum, its `share`
   the sum of shares. An article matching two of a ticker's entities counts twice. `_GLOBAL`
   rows pass through unchanged.
3. `entity_features`  per (date, entity) statistics, wide: mean = value_sum/n, count = n,
   sum = value_sum, share = n / n(_GLOBAL), sum_share = value_sum / value_sum(_GLOBAL), optionally
   over a trailing `agg_days` sum of the raw columns (weekly variant, hypothesis A3).
4. `surprise`         z-score and log-ratio vs the entity's own trailing baseline that EXCLUDES
   the current aggregation period: baseline = rows t-shift-window+1 .. t-shift.

Leakage rules: every step uses only rows at or before t (positional on the trading-day grid);
no forward-fill; a missing day is NaN. Count-like stats are 0 (a true zero) on a day the source
was observed (any row of that source that day) once the entity has appeared at least once;
before its first appearance, and on unobserved days, they are NaN.

Feature names: '<source>.<metric>.<stat>[<agg>d]_<transform>.<window>', e.g.
'events.root_19_fight.share_z.60', 'gkg.tone.mean5d_z.60'. Untransformed levels use window 0:
'events.root_19_fight.share.0'. Dates come back as `datetime64[ns]`.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from eventtrade.schemas import FEATURE_COLUMNS, GDELT_DAILY_COLUMNS, Asset

GLOBAL = "_GLOBAL"
_NY = ZoneInfo("America/New_York")
_THEME_METRIC = re.compile(r"theme_(?P<theme>.+)_(?P<kind>docs|tone)")

Stat = Literal["mean", "count", "sum", "share", "sum_share"]
Transform = Literal["level", "z", "lr"]


@dataclass(frozen=True)
class MetricSpec:
    """One feature family: which raw metric, which statistic, and how it is surprised."""

    source: str
    metric: str
    stat: Stat
    transforms: tuple[Transform, ...] = ("z",)
    window: int = 60
    min_periods: int = 20
    agg_days: int = 1
    denominator_metric: str | None = None  # share denominator: _GLOBAL of this metric (default: same)

    @property
    def column(self) -> str:
        """Name of the untransformed statistic column in the wide entity frame."""
        agg = f"{self.agg_days}d" if self.agg_days > 1 else ""
        den = f"/{self.denominator_metric}" if self.denominator_metric else ""
        return f"{self.source}.{self.metric}{den}.{self.stat}{agg}"


def session_date_of(added_utc: datetime, cutoff_et: time = time(15, 30)) -> date:
    """New York session date of a GDELT DATEADDED (UTC): rows after the cutoff go to the next day."""
    if added_utc.tzinfo is None:
        added_utc = added_utc.replace(tzinfo=ZoneInfo("UTC"))
    local = added_utc.astimezone(_NY)
    d = local.date()
    return d if local.time() <= cutoff_et else d + timedelta(days=1)


def to_trading_days(gdelt_daily: pd.DataFrame, trading_days: list[date]) -> pd.DataFrame:
    """Roll session_date forward to the first trading day >= it and re-sum value_sum and n.

    Rows after the last trading day, or before the first one (their session is unknowable
    without the previous trading day), are dropped.
    """
    df = _check_daily(gdelt_daily)
    cal = _calendar(trading_days)
    sd = df["session_date"].to_numpy()
    idx = np.searchsorted(cal.to_numpy(), sd, side="left")
    keep = (idx < len(cal)) & (sd >= cal[0].to_datetime64())
    df = df.loc[keep].copy()
    df["session_date"] = cal[idx[keep]]
    out = df.groupby(["session_date", "source", "entity", "metric"], as_index=False, sort=True)[
        ["value_sum", "n"]
    ].sum()
    return out[GDELT_DAILY_COLUMNS]


def map_to_tickers(daily: pd.DataFrame, assets: list[Asset]) -> pd.DataFrame:
    """Re-key raw rows from GDELT entities to tickers (sum of value_sum and n); keep _GLOBAL rows."""
    df = _check_daily(daily)
    pairs = [
        (e, a.ticker)
        for a in assets
        for e in dict.fromkeys(
            (a.fips_countries + a.gkg_themes) if a.kind == "etf" else a.org_aliases
        )
    ]
    pairs.append((GLOBAL, GLOBAL))
    m = pd.DataFrame(pairs, columns=["entity", "ticker"])
    out = df.merge(m, on="entity", how="inner")
    out = out.groupby(["session_date", "source", "ticker", "metric"], as_index=False, sort=True)[
        ["value_sum", "n"]
    ].sum()
    return out.rename(columns={"ticker": "entity"})[GDELT_DAILY_COLUMNS]


def entity_features(
    daily: pd.DataFrame, metric_specs: list[MetricSpec], trading_days: list[date] | None = None
) -> pd.DataFrame:
    """Wide frame [date, entity, <spec.column>...] of untransformed statistics on a full date grid."""
    df = _check_daily(daily)
    grid = (
        _calendar(trading_days) if trading_days is not None
        else pd.DatetimeIndex(sorted(df["session_date"].unique()), name="date")
    )
    observed = {
        src: pd.Series(grid.isin(g["session_date"].unique()), index=grid)
        for src, g in df.groupby("source")
    }
    cache: dict[tuple[str, str], tuple[pd.DataFrame, pd.DataFrame]] = {}

    def raw(source: str, metric: str, agg: int) -> tuple[pd.DataFrame, pd.DataFrame]:
        if (source, metric) not in cache:
            cache[(source, metric)] = _raw_wide(df, source, metric, grid, observed.get(source))
        vs, n = cache[(source, metric)]
        if agg > 1:
            vs = vs.rolling(agg, min_periods=agg).sum()
            n = n.rolling(agg, min_periods=agg).sum()
        return vs, n

    cols: dict[str, pd.DataFrame] = {}
    for spec in _dedupe(metric_specs):
        _check_spec(spec)
        vs, n = raw(spec.source, spec.metric, spec.agg_days)
        if spec.stat == "mean":
            val = vs / n.where(n > 0)
        elif spec.stat == "count":
            val = n
        elif spec.stat == "sum":
            val = vs
        else:
            gvs, gn = raw(spec.source, spec.denominator_metric or spec.metric, spec.agg_days)
            num, den = (n, gn) if spec.stat == "share" else (vs, gvs)
            g = den[GLOBAL] if GLOBAL in den.columns else pd.Series(np.nan, index=grid)
            val = num.div(g.where(g != 0), axis=0)
        cols[spec.column] = val
    if not cols:
        return pd.DataFrame(columns=["date", "entity"])
    entities = sorted(set().union(*(c.columns for c in cols.values())))
    long = {
        name: w.reindex(columns=entities).stack().rename(name) for name, w in cols.items()
    }
    out = pd.concat(long.values(), axis=1)
    out.index.names = ["date", "entity"]
    return out.reset_index().sort_values(["date", "entity"], ignore_index=True)


def surprise(
    series_frame: pd.DataFrame, window: int, min_periods: int, shift: int = 1,
    transforms: tuple[Transform, ...] = ("z", "lr"),
) -> pd.DataFrame:
    """Per-entity z-score and log-ratio of each column vs its trailing baseline excluding t.

    Baseline for row t = the entity's rows t-shift-window+1 .. t-shift on the frame's date grid
    (shift=1: exclude day t; use shift=agg_days for agg_days-summed columns so the baseline does
    not overlap the current period). z = (x - mean) / std (ddof=1), NaN if std is 0.
    lr = ln(x / mean), NaN unless both are > 0. Output columns '<col>_z.<window>', '<col>_lr.<window>'.
    """
    if shift < 1:
        raise ValueError("shift must be >= 1: the baseline may never contain day t")
    if not 2 <= min_periods <= window:
        raise ValueError("need 2 <= min_periods <= window")
    df = series_frame.copy()
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    grid = pd.DatetimeIndex(sorted(df["date"].unique()), name="date")
    value_cols = [c for c in df.columns if c not in ("date", "entity")]
    out_cols: dict[str, pd.Series] = {}
    for c in value_cols:
        x = df.pivot(index="date", columns="entity", values=c).reindex(grid).astype("float64")
        base = x.shift(shift).rolling(window, min_periods=min_periods)
        m, s = base.mean(), base.std(ddof=1)
        if "z" in transforms:
            out_cols[f"{c}_z.{window}"] = ((x - m) / s.where(s > 0)).stack()
        if "lr" in transforms:
            ok = (x > 0) & (m > 0)
            out_cols[f"{c}_lr.{window}"] = np.log(x.where(ok) / m.where(ok)).stack()
        if "level" in transforms:
            out_cols[f"{c}.0"] = x.stack()
    if not out_cols:
        return df[["date", "entity"]].copy()
    out = pd.concat(out_cols, axis=1)
    out.index.names = ["date", "entity"]
    out = out.reset_index()
    return df[["date", "entity"]].merge(out, on=["date", "entity"], how="left")


def themes_as_entities(gdelt_daily: pd.DataFrame) -> pd.DataFrame:
    """Re-key global GKG theme rows so the theme is the entity: (_GLOBAL, theme_<T>_docs) -> (T, theme_docs).

    The global theme query writes one metric per theme on entity _GLOBAL; `map_to_tickers` maps
    entities, so an ETF's `gkg_themes` only match after this. Country theme rows (entity = FIPS)
    and every other row pass through unchanged.
    """
    df = _check_daily(gdelt_daily)
    m = df["metric"].str.fullmatch(_THEME_METRIC.pattern)
    hit = (df["source"] == "gkg") & (df["entity"] == GLOBAL) & m.fillna(False).astype(bool)
    parts = df.loc[hit, "metric"].str.extract(_THEME_METRIC.pattern)
    df.loc[hit, "entity"] = parts["theme"]
    df.loc[hit, "metric"] = "theme_" + parts["kind"]
    return df[GDELT_DAILY_COLUMNS].reset_index(drop=True)


def product_feature(features: pd.DataFrame, a: str, b: str, name: str) -> pd.DataFrame:
    """FEATURE_COLUMNS rows of a*b where both features exist for the same (date, ticker)."""
    fa = features.loc[features["feature"] == a, ["date", "ticker", "value"]]
    fb = features.loc[features["feature"] == b, ["date", "ticker", "value"]]
    out = fa.merge(fb, on=["date", "ticker"], suffixes=("_a", "_b"))
    out = out.assign(feature=name, value=out["value_a"] * out["value_b"])
    out = out[np.isfinite(out["value"].astype("float64"))]
    return out[FEATURE_COLUMNS].sort_values(["date", "ticker"], ignore_index=True)


def build_features(
    gdelt_daily: pd.DataFrame, trading_days: list[date], assets: list[Asset],
    specs: list[MetricSpec],
) -> pd.DataFrame:
    """Compose roll-forward, ticker mapping, statistics and surprise into FEATURE_COLUMNS.

    NaN/inf values are dropped from the long output. Filter inputs to dates before
    `config.HOLDOUT_START` before calling, except in the hold-out evaluation.
    """
    daily = map_to_tickers(to_trading_days(gdelt_daily, trading_days), assets)
    specs = _dedupe(specs)
    wide = entity_features(daily, specs, trading_days)
    parts = []
    for spec in specs:
        sub = wide[["date", "entity", spec.column]]
        sur = surprise(sub, spec.window, spec.min_periods, shift=spec.agg_days,
                       transforms=spec.transforms)
        parts.append(sur.melt(id_vars=["date", "entity"], var_name="feature", value_name="value"))
    if not parts:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    out = pd.concat(parts, ignore_index=True).rename(columns={"entity": "ticker"})
    out = out[(out["ticker"] != GLOBAL) & np.isfinite(out["value"].astype("float64"))]
    out = out.drop_duplicates(["date", "ticker", "feature"])
    return out[FEATURE_COLUMNS].sort_values(["date", "ticker", "feature"], ignore_index=True)


def _check_daily(daily: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in GDELT_DAILY_COLUMNS if c not in daily.columns]
    if missing:
        raise ValueError(f"gdelt_daily is missing columns {missing}")
    df = daily[GDELT_DAILY_COLUMNS].copy()
    df["session_date"] = pd.to_datetime(df["session_date"]).astype("datetime64[ns]")
    df["value_sum"] = df["value_sum"].astype("float64")
    df["n"] = df["n"].astype("float64")
    return df


def _calendar(trading_days: list[date]) -> pd.DatetimeIndex:
    cal = pd.to_datetime(pd.Series(list(trading_days))).astype("datetime64[ns]")
    if cal.empty:
        raise ValueError("trading_days is empty")
    return pd.DatetimeIndex(sorted(cal.unique()), name="date")


def _raw_wide(
    df: pd.DataFrame, source: str, metric: str, grid: pd.DatetimeIndex,
    observed: pd.Series | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """value_sum and n as date x entity on `grid`; 0 on observed days after first appearance."""
    sub = df[(df["source"] == source) & (df["metric"] == metric)]
    vs = sub.pivot_table(index="session_date", columns="entity", values="value_sum",
                         aggfunc="sum").reindex(grid)
    n = sub.pivot_table(index="session_date", columns="entity", values="n",
                        aggfunc="sum").reindex(grid)
    if observed is None:
        observed = pd.Series(False, index=grid)
    fill = n.notna().cummax() & np.asarray(observed, dtype=bool)[:, None]
    return vs.mask(vs.isna() & fill, 0.0), n.mask(n.isna() & fill, 0.0)


def _dedupe(specs: list[MetricSpec]) -> list[MetricSpec]:
    return list(dict.fromkeys(specs))


def _check_spec(spec: MetricSpec) -> None:
    for part in (spec.source, spec.metric, spec.denominator_metric or ""):
        if "." in part:
            raise ValueError(f"'.' is reserved in feature names: {part!r}")
    if spec.agg_days < 1:
        raise ValueError("agg_days must be >= 1")
    if spec.denominator_metric and spec.stat not in ("share", "sum_share"):
        raise ValueError("denominator_metric only applies to share stats")
