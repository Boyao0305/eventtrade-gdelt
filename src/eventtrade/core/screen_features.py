"""ADR 0003 sub-category screen: the pre-registered cell signals, built with one recipe. Pure.

Every screen signal is the v2 (`features.FEATURES_VERSION` 2) `sm5_z.60` transform of one
statistic: the 5-trading-day mean of the floored/clipped z-score against the ticker's own
60-day baseline that excludes day t (`features.surprise`, min_periods 20). Nothing is tuned per
cell: same stat, transform, window and min_periods for every family (ADR 0003, "One recipe").

Statistic = `mean` (value_sum / n) of a family metric, after the usual roll-forward and ticker
mapping on raw sums (`features.build_features`):

- Track A families (events). A family row stores value_sum = matching events and n = the
  entity's `n_events`, so the mean is "the family's share of the country's events", combined
  over an ETF's countries as sum(counts) / sum(n_events). This is ADR 0003's "v2 share of that
  family in the country's events"; it is *not* `features`' `share` stat (n / n(_GLOBAL)) nor the
  A1 `sum_share` (count / global n_events).
  `violence` = roots 18 + 19 + 20: the three counts are summed per raw row key *before* the
  share is taken (n stays the entity's n_events, never tripled).
- Track B signals (GKG orgs), the v2 B1/B2 means: `org_docs` = docs mentioning the ticker's
  aliases / the day's total docs (attention share); `org_neg_docs` = negative (tone < -2) /
  mentioning docs; `org_tone` = mean document tone of mentioning docs.

Feature names: 'S3.<name>' (hypothesis S3 = this screen). Pre-registered signs per asset group
are in `SCREEN_FAMILIES_A` / `SCREEN_SIGNALS_B` (ADR 0003 verbatim). Filter inputs to dates
before `config.HOLDOUT_START` before calling, as for `build_features`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

import pandas as pd

from eventtrade.core.features import (
    GLOBAL,
    MetricSpec,
    build_features,
    feature_name,
)
from eventtrade.schemas import FEATURE_COLUMNS, GDELT_DAILY_COLUMNS, Asset, Track

FEATURE_PREFIX = "S3."
SCREEN_WINDOW = 60
SCREEN_MIN_PERIODS = 20
# Track A asset groups (ADR 0003): developed-market country ETFs, emerging-market country ETFs,
# commodity ETFs + XLE. Which ticker is in which group is decided outside this module.
GROUPS_A: tuple[str, ...] = ("dm", "em", "commodity")
# Track B signs are the same in every GICS sector group.
ANY_GROUP = "*"


@dataclass(frozen=True)
class ScreenSignal:
    """One screen family: name, the raw metrics summed into it, its recipe and its signs."""

    name: str
    track: Track
    components: tuple[str, ...]   # raw gdelt_daily metrics; >1 = summed before the stat
    signs: Mapping[str, float | None]   # asset group (or ANY_GROUP) -> pre-registered IC sign

    @property
    def spec(self) -> MetricSpec:
        """The one recipe: mean of the (combined) metric, sm5 over a 60-day z baseline."""
        return MetricSpec("events" if self.track == "A" else "gkg", self.name, "mean", ("sm5",),
                          window=SCREEN_WINDOW, min_periods=SCREEN_MIN_PERIODS)

    @property
    def feature(self) -> str:
        return FEATURE_PREFIX + self.name

    def sign(self, group: str) -> float | None:
        """Pre-registered sign for an asset group; None = two-sided. Unknown group: KeyError."""
        if group in self.signs:
            return self.signs[group]
        if ANY_GROUP in self.signs:
            return self.signs[ANY_GROUP]
        raise KeyError(f"{self.name}: no pre-registered sign for group {group!r}")


def _a(name: str, components: tuple[str, ...], dm: float | None, em: float | None,
       commodity: float | None) -> ScreenSignal:
    return ScreenSignal(name, "A", components,
                        MappingProxyType({"dm": dm, "em": em, "commodity": commodity}))


def _b(name: str, sign: float | None) -> ScreenSignal:
    return ScreenSignal(name, "B", (name,), MappingProxyType({ANY_GROUP: sign}))


# ADR 0003, Track A: cooperation (+) for countries, none (two-sided) for commodities; conflict
# (-) for countries, flipped to (+) for commodities (supply disruption raises prices).
SCREEN_FAMILIES_A: tuple[ScreenSignal, ...] = (
    _a("quad1", ("quad1",), +1.0, +1.0, None),                       # verbal cooperation
    _a("quad2", ("quad2",), +1.0, +1.0, None),                       # material cooperation
    _a("quad3", ("quad3",), -1.0, -1.0, +1.0),                       # verbal conflict
    _a("quad4", ("quad4",), -1.0, -1.0, +1.0),                       # material conflict
    _a("root_14", ("root_14_protest",), -1.0, -1.0, +1.0),           # protest
    _a("root_163", ("root_163_sanctions",), -1.0, -1.0, +1.0),       # sanctions
    _a("violence", ("root_18_assault", "root_19_fight", "root_20_massviolence"),
       -1.0, -1.0, +1.0),                                            # roots 18 + 19 + 20
)

# ADR 0003, Track B: attention two-sided, negative attention (-), tone (+).
SCREEN_SIGNALS_B: tuple[ScreenSignal, ...] = (
    _b("org_docs", None),
    _b("org_neg_docs", -1.0),
    _b("org_tone", +1.0),
)

_BY_TRACK: dict[str, tuple[ScreenSignal, ...]] = {"A": SCREEN_FAMILIES_A, "B": SCREEN_SIGNALS_B}


def screen_families(track: Track) -> tuple[ScreenSignal, ...]:
    """The pre-registered families (A) or signals (B) of a track."""
    if track not in _BY_TRACK:
        raise ValueError(f"unknown track {track!r}")
    return _BY_TRACK[track]


def screen_specs(track: Track) -> list[MetricSpec]:
    """The MetricSpecs `screen_signals` feeds to `build_features` for a track."""
    return [f.spec for f in screen_families(track)]


def screen_inputs(gdelt_daily: pd.DataFrame, track: Track) -> pd.DataFrame:
    """GDELT_DAILY_COLUMNS rows of the track's families, multi-metric families combined.

    Keeps only the components' rows of the track's source. A family of one metric is renamed
    to the family name; a family of several (violence) gets, per (session_date, entity), the
    sum of its components' value_sum and their common n (the entity's n_events). The
    components must agree on n for a key (ValueError otherwise); a missing component row
    counts as 0 events. Any input row already named like a combined family is ignored.
    """
    missing = [c for c in GDELT_DAILY_COLUMNS if c not in gdelt_daily.columns]
    if missing:
        raise ValueError(f"gdelt_daily is missing columns {missing}")
    fams = screen_families(track)
    source = "events" if track == "A" else "gkg"
    df = gdelt_daily.loc[gdelt_daily["source"] == source, GDELT_DAILY_COLUMNS].copy()
    df["session_date"] = pd.to_datetime(df["session_date"]).astype("datetime64[ns]")
    df["value_sum"] = df["value_sum"].astype("float64")
    df["n"] = df["n"].astype("float64")
    parts = []
    for fam in fams:
        sub = df[df["metric"].isin(fam.components)]
        if len(fam.components) == 1:
            parts.append(sub.assign(metric=fam.name))
            continue
        keys = ["session_date", "source", "entity"]
        g = sub.groupby(keys, sort=False)
        n_lo, n_hi = g["n"].min(), g["n"].max()
        bad = n_lo != n_hi
        if bad.any():
            k = bad[bad].index[0]
            raise ValueError(f"{fam.name}: components disagree on n (n_events) at {k}: "
                             f"{n_lo[k]} vs {n_hi[k]}")
        comb = g["value_sum"].sum().to_frame().join(n_lo).reset_index()
        parts.append(comb.assign(metric=fam.name)[GDELT_DAILY_COLUMNS])
    if not parts:
        return pd.DataFrame(columns=GDELT_DAILY_COLUMNS)
    out = pd.concat(parts, ignore_index=True)[GDELT_DAILY_COLUMNS]
    return out.sort_values(["session_date", "source", "entity", "metric"], ignore_index=True)


def screen_signals(
    gdelt_daily: pd.DataFrame, trading_days: list[date], assets: list[Asset], track: Track,
) -> pd.DataFrame:
    """FEATURE_COLUMNS of every ADR 0003 screen signal of a track ('S3.<name>', sm5_z.60).

    Only the track's assets are mapped (others are ignored). Composes `screen_inputs` with
    `features.build_features`, so roll-forward, ticker mapping on raw sums, zero-fill and the
    look-ahead rules are exactly v2's. NaN/inf values are dropped.
    """
    fams = screen_families(track)
    track_assets = [a for a in assets if a.track == track]
    if not track_assets:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    rows = screen_inputs(gdelt_daily, track)
    feats = build_features(rows, trading_days, track_assets, [f.spec for f in fams])
    rename = {feature_name(f.spec, "sm5"): f.feature for f in fams}
    feats = feats[feats["feature"].isin(rename) & (feats["ticker"] != GLOBAL)]
    feats = feats.assign(feature=feats["feature"].map(rename))
    return feats[FEATURE_COLUMNS].sort_values(["date", "ticker", "feature"], ignore_index=True)
