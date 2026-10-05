"""ADR 0003 sub-category screen and ADR 0004 theme screen: the pre-registered cell signals,
built with one recipe. Pure.

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

ADR 0004 theme screen (hypothesis A2; `THEME_SIGNALS`, `theme_signals`): the same recipe on two
GKG theme statistics per ETF, over the ETF's mapped `gkg_themes`, read from the global
`gkg_themes_daily` rows (entity '_GLOBAL'; `theme_<T>_docs`: value_sum = docs mentioning T,
n = all GKG docs that session day; `theme_<T>_tone`: value_sum = summed document tone,
n = docs mentioning T):

- `attention` = sum over mapped themes T of docs(T) / all GKG docs that day;
- `tone` = sum over mapped themes of tone_sum(T) / sum over mapped themes of docs(T).

DEFINITIONAL CHOICE (ADR 0004 implementation notes). The query stores per-theme counts only, so
"documents mentioning any mapped theme" cannot be computed exactly: a document carrying two of
an ETF's themes is counted twice. `attention` therefore counts theme MENTIONS (document-theme
pairs) per GKG document of the day, not distinct documents, and `tone` is the mention-weighted
mean tone (a document with k mapped themes weighs k). The day total is counted once. Days roll
forward to trading days on the raw sums (mentions and day totals summed over the sessions rolled
into one trading day), like every v2 statistic.
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
    # gdelt_daily source of the inputs; None = the track's (events for A, gkg for B).
    source: str | None = None
    prefix: str = FEATURE_PREFIX   # feature-name prefix = the hypothesis ('S3.', 'A2.')

    @property
    def spec(self) -> MetricSpec:
        """The one recipe: mean of the (combined) metric, sm5 over a 60-day z baseline."""
        source = self.source or ("events" if self.track == "A" else "gkg")
        return MetricSpec(source, self.name, "mean", ("sm5",),
                          window=SCREEN_WINDOW, min_periods=SCREEN_MIN_PERIODS)

    @property
    def feature(self) -> str:
        return self.prefix + self.name

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
    return _renamed(feats, fams)


def _renamed(feats: pd.DataFrame, signals: tuple[ScreenSignal, ...]) -> pd.DataFrame:
    """Keep the signals' sm5 columns, renamed to '<prefix><name>', in FEATURE_COLUMNS order."""
    rename = {feature_name(f.spec, "sm5"): f.feature for f in signals}
    feats = feats[feats["feature"].isin(rename) & (feats["ticker"] != GLOBAL)]
    feats = feats.assign(feature=feats["feature"].map(rename))
    return feats[FEATURE_COLUMNS].sort_values(["date", "ticker", "feature"], ignore_index=True)


# --- ADR 0004: GKG theme screen (hypothesis A2) -----------------------------------------------

THEME_PREFIX = "A2."
# ADR 0004 groups, pinned verbatim: commodity ETFs + XLE, and the sector SPDRs + ITA (XLE is
# screened with the commodities only). Membership is fixed here, not derived from data.
GROUPS_A2: tuple[str, ...] = ("commodity", "sector")
THEME_GROUP_MEMBERS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "commodity": ("USO", "UNG", "GLD", "SLV", "CPER", "DBA", "XLE"),
    "sector": ("XLB", "XLC", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY", "ITA"),
})
_THEME_ROW = r"theme_(?P<theme>.+)_(?P<kind>docs|tone)"
# Entity under which one ETF's combined theme rows are keyed before `build_features`, so the
# ticker mapping is the identity and can never pick up a FIPS code or a single theme.
_THEME_ENTITY = "a2:"


def _theme(name: str, raw: str, commodity: float | None, sector: float | None) -> ScreenSignal:
    return ScreenSignal(name, "A", (raw,),
                        MappingProxyType({"commodity": commodity, "sector": sector}),
                        source="gkg", prefix=THEME_PREFIX)


# ADR 0004: attention two-sided in both groups; tone (+) for sectors, two-sided for commodities
# (bad news about supply can raise prices).
THEME_SIGNALS: tuple[ScreenSignal, ...] = (
    _theme("attention", "theme_<T>_docs", None, None),
    _theme("tone", "theme_<T>_tone", None, +1.0),
)


def theme_inputs(gdelt_daily: pd.DataFrame, assets: list[Asset]) -> pd.DataFrame:
    """GDELT_DAILY_COLUMNS rows of each ETF's combined theme statistics (module docstring).

    Reads only source 'gkg', entity '_GLOBAL', metrics `theme_<T>_docs` / `theme_<T>_tone`;
    every other row is ignored. For each ETF in `assets` with `gkg_themes` (a theme listed twice
    counts once), entity 'a2:<ticker>':

    - metric 'attention' on every session day with a known day total (any theme docs row that
      day): value_sum = sum of docs(T) over the mapped themes (0 if none has a row), n = the day
      total. The docs rows of one day must agree on n (ValueError otherwise).
    - metric 'tone' on session days where a mapped theme has a tone row with n > 0:
      value_sum = sum of tone_sum(T), n = sum of docs(T), over those rows.
    """
    missing = [c for c in GDELT_DAILY_COLUMNS if c not in gdelt_daily.columns]
    if missing:
        raise ValueError(f"gdelt_daily is missing columns {missing}")
    empty = pd.DataFrame(columns=GDELT_DAILY_COLUMNS)
    df = gdelt_daily.loc[(gdelt_daily["source"] == "gkg") & (gdelt_daily["entity"] == GLOBAL),
                         GDELT_DAILY_COLUMNS]
    ext = df["metric"].astype(str).str.extract(f"^{_THEME_ROW}$")
    df = df.assign(theme=ext["theme"], kind=ext["kind"])[ext["kind"].notna()]
    pairs = [(t, a.ticker) for a in assets if a.kind == "etf"
             for t in dict.fromkeys(a.gkg_themes)]
    if df.empty or not pairs:
        return empty
    df = df.assign(session_date=pd.to_datetime(df["session_date"]).astype("datetime64[ns]"),
                   value_sum=df["value_sum"].astype("float64"), n=df["n"].astype("float64"))
    mapping = pd.DataFrame(pairs, columns=["theme", "ticker"])

    docs = df[df["kind"] == "docs"]
    by_day = docs.groupby("session_date")["n"]
    lo, hi = by_day.min(), by_day.max()
    bad = lo != hi
    if bad.any():
        d = bad[bad].index[0]
        raise ValueError(f"theme docs rows disagree on the day total (n) at {d.date()}:"
                         f" {lo[d]} vs {hi[d]}")
    totals = lo.rename("n").reset_index()
    mentions = (docs.merge(mapping, on="theme")
                .groupby(["session_date", "ticker"], as_index=False)["value_sum"].sum())
    grid = totals.merge(pd.DataFrame({"ticker": sorted(mapping["ticker"].unique())}),
                        how="cross")
    att = grid.merge(mentions, on=["session_date", "ticker"], how="left")
    att = att.assign(value_sum=att["value_sum"].fillna(0.0), metric="attention")

    tone = df[(df["kind"] == "tone") & (df["n"] > 0)].merge(mapping, on="theme")
    tone = (tone.groupby(["session_date", "ticker"], as_index=False)[["value_sum", "n"]].sum()
            .assign(metric="tone"))

    out = pd.concat([att, tone], ignore_index=True)
    out = out.assign(source="gkg", entity=_THEME_ENTITY + out["ticker"])[GDELT_DAILY_COLUMNS]
    return out.sort_values(["session_date", "source", "entity", "metric"], ignore_index=True)


def theme_signals(
    gdelt_daily: pd.DataFrame, trading_days: list[date], assets: list[Asset],
) -> pd.DataFrame:
    """FEATURE_COLUMNS of the ADR 0004 theme signals ('A2.attention', 'A2.tone'; sm5_z.60).

    Every ETF in `assets` with `gkg_themes` gets both signals (the caller picks the group
    members). Composes `theme_inputs` with `features.build_features`, so roll-forward, zero-fill
    on observed days and the look-ahead rules are exactly v2's. NaN/inf values are dropped.
    """
    etfs = [a for a in assets if a.kind == "etf" and a.gkg_themes]
    rows = theme_inputs(gdelt_daily, etfs)
    if rows.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    keyed = [a.model_copy(update={"fips_countries": [], "gkg_themes": [_THEME_ENTITY + a.ticker]})
             for a in etfs]
    feats = build_features(rows, trading_days, keyed, [s.spec for s in THEME_SIGNALS])
    return _renamed(feats, THEME_SIGNALS)
