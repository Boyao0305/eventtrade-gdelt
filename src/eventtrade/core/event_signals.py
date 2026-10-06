"""ADR 0005 extreme-shock event study: pre-registered cells, grid, shock scores and outcomes. Pure.

Shock scores (`shock_scores`) are the v2 z-score (`features.surprise`: floored at
`Z_STD_FLOOR`, clipped to +-`Z_CLIP`) of one daily statistic against the ticker's own trailing
baseline of W trading days that EXCLUDES day t, with no smoothing. Statistics, all built with
`features.build_features` on raw sums (roll-forward to trading days, ticker mapping, zero-fill on
observed days and the look-ahead rules exactly as v2):

- Track A families (ADR 0003, `screen_features.SCREEN_FAMILIES_A`): family events / the mapped
  countries' n_events, i.e. the share within the country's events, combined over an ETF's
  countries as sum(counts) / sum(n_events). For a commodity ETF the mapped countries are its
  producer countries, so this is ADR 0005's "pooled family share over those countries" (E2).
  `violence` = roots 18 + 19 + 20 summed before dividing (`screen_features.screen_inputs`).
- Track A `tone`: events AvgTone weighted by NumArticles (value_sum = sum tone x articles,
  n = articles), pooled over the mapped countries.
- Track B `org_docs`: docs mentioning the stock's aliases / the day's GKG docs (attention share,
  day total counted once); `org_tone`: mean document tone of mentioning docs, used only to split
  attention spikes into negative / positive (same-day z, same W).

Cells (`CELLS`, 47, ADR 0005 verbatim) name a trigger signal and its direction: 'up' fires when
z >= theta, 'down' (tone, "low tone") when z <= -theta. `cell_scores` turns scores into the
directional value v that fires when v >= theta, applying the Track B tone split. Declustering
(20 days), the calendar-time portfolio and the statistics live elsewhere.

Outcomes (`outcomes`, entered at t+1 open, as everywhere), per (date t, ticker, horizon h):

- `raw_return`      = ret_oo(h) = adj_open[t+1+h] / adj_open[t+1] - 1 (`labels.make_labels`).
- `abnormal_return` = ret_oo(h) minus the equal-weighted mean ret_oo(h) of the OTHER members of
  the ticker's group with a return that day; undefined (no row) without such a member.
- `abnormal_vol`    = ln(forward vol / trailing vol) minus the group's same-day median of it
  (the ticker included, as the ADR says "the group's median"); needs >= 2 members with a value.
  Forward vol = RMS of the h open-to-open log returns over opens t+1..t+1+h (from entry, as
  everywhere); trailing vol = RMS of the 22 close-to-close log returns t-21..t
  (`labels.vol_surprise_label`'s trailing leg). That label's forward leg starts at the t close
  and would credit the close t -> open t+1 gap, which happens before entry, to the event.

Price window of the outcome at t (pinned by tests): opens t+1..t+1+h and closes t-22..t of the
ticker and its group members; nothing after t+1+h. Filter prices to dates before
`config.HOLDOUT_START` (and labels then end there automatically).

Groups: Track A screen_group dm / em / commodity; Track B the GICS sector (`outcome_groups`).
Track B cells pool all stocks (cell group 'all'), each stock measured against its own sector.

JUDGEMENT CALLS (not fixed by ADR 0005; chosen before any event-study value was computed):

| Question | Choice |
|---|---|
| min_periods of the W-day baseline | W // 3 (20 for 60, 40 for 120): v2's 20-of-60 ratio |
| tone z undefined on an attention spike | in neither E4 nor E4p; still an E5 spike |
| tone z exactly 0 | positive (ADR: "tone z >= 0") |
| abnormal vol median | includes the ticker itself; NaN if < 2 members have a value |
| abnormal return peers | members with a defined ret_oo(h) that day, excluding the ticker |
| forward leg of abnormal vol | open-to-open over opens t+1..t+1+h (no pre-entry overnight gap) |
| trailing leg of abnormal vol | close-to-close t-21..t, known at t (A4 v2 label's leg) |
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from itertools import product
from typing import Literal

import numpy as np
import pandas as pd

from eventtrade.core.features import MetricSpec, build_features, feature_name
from eventtrade.core.labels import SURPRISE_TRAILING_DAYS, make_labels
from eventtrade.core.labels import _ln_rms as ln_rms
from eventtrade.core.labels import _wide as labels_wide
from eventtrade.core.screen_features import GROUPS_A, screen_inputs
from eventtrade.schemas import GDELT_DAILY_COLUMNS, Asset, Track

__all__ = [
    "CELLS", "GRID", "HORIZONS", "MIN_PERIODS", "OUTCOMES", "OUTCOME_COLUMNS", "SCORE_COLUMNS",
    "SIGNALS", "THETAS", "WINDOWS", "EventCell", "cell_scores", "outcome_groups", "outcomes",
    "shock_scores",
]

# --- grid (ADR 0005 "Nested optimization") -------------------------------------------------------

THETAS: tuple[float, ...] = (2.0, 2.5, 3.0, 4.0)
WINDOWS: tuple[int, ...] = (60, 120)
HORIZONS: tuple[int, ...] = (1, 5, 10, 20)
# (theta, W, h), theta-major: 4 x 2 x 4 = 32 configurations per cell.
GRID: tuple[tuple[float, int, int], ...] = tuple(product(THETAS, WINDOWS, HORIZONS))
# Judgement call (module docstring): the v2 ratio 20 of 60, kept for W = 120.
MIN_PERIODS: Mapping[int, int] = {w: w // 3 for w in WINDOWS}

SCORE_COLUMNS = ["date", "ticker", "signal", "value"]
OUTCOME_COLUMNS = ["date", "ticker", "horizon", "outcome", "value"]

Outcome = Literal["abnormal_return", "raw_return", "abnormal_vol"]
OUTCOMES: tuple[str, ...] = ("abnormal_return", "raw_return", "abnormal_vol")
Trigger = Literal["up", "down"]
Split = Literal["negative", "positive"]

# Signals of each track: (name, gdelt source, raw metric after screen_inputs).
_FAMILIES = ("quad1", "quad2", "quad3", "quad4", "root_14", "root_163", "violence")
TONE_A = "tone"
ATTENTION_B = "org_docs"
TONE_B = "org_tone"
SIGNALS: Mapping[str, tuple[str, ...]] = {"A": (*_FAMILIES, TONE_A), "B": (ATTENTION_B, TONE_B)}
_SOURCE = {"A": "events", "B": "gkg"}
POOLED_B = "all"


# --- cells (ADR 0005 "Hypotheses and pre-registered signs") --------------------------------------

@dataclass(frozen=True)
class EventCell:
    """One pre-registered cell: trigger signal and direction, asset group, outcome, sign."""

    hypothesis: str
    track: Track
    signal: str          # trigger signal (SIGNALS[track])
    group: str           # dm | em | commodity (A); 'all' = every stock pooled (B)
    outcome: Outcome
    trigger: Trigger     # 'up': z >= theta; 'down': z <= -theta
    sign: float          # registered sign of the mean outcome
    split: Split | None = None   # Track B: same-day tone z < 0 ('negative') or >= 0

    @property
    def id(self) -> str:
        return f"{self.hypothesis}|{self.signal}|{self.group}"


def _trigger(signal: str) -> Trigger:
    return "down" if signal == TONE_A else "up"


def _cells() -> tuple[EventCell, ...]:
    out: list[EventCell] = []
    # E1: conflict families (quad3, quad4, protest, sanctions, violence) and low tone -> abnormal
    # return of the country ETF, sign -. E1c: cooperation (quad1, quad2), sign +. DM and EM.
    for hyp, sigs, sign in (("E1", ("quad3", "quad4", "root_14", "root_163", "violence", TONE_A),
                             -1.0),
                            ("E1c", ("quad1", "quad2"), +1.0)):
        out += [EventCell(hyp, "A", s, g, "abnormal_return", _trigger(s), sign)
                for s in sigs for g in ("dm", "em")]
    # E2: conflict (quad4, violence, sanctions, protest) in producer countries -> raw return of
    # commodity ETFs, sign +.
    out += [EventCell("E2", "A", s, "commodity", "raw_return", "up", +1.0)
            for s in ("quad4", "violence", "root_163", "root_14")]
    # E3: any family shock (7 families + low tone) -> abnormal volatility, DM, EM, commodity, +.
    out += [EventCell("E3", "A", s, g, "abnormal_vol", _trigger(s), +1.0)
            for s in SIGNALS["A"] for g in GROUPS_A]
    # E4 / E4p: negative / positive attention spike -> abnormal return (-, +); E5: any spike ->
    # abnormal vol (+). All stocks pooled.
    out += [
        EventCell("E4", "B", ATTENTION_B, POOLED_B, "abnormal_return", "up", -1.0, "negative"),
        EventCell("E4p", "B", ATTENTION_B, POOLED_B, "abnormal_return", "up", +1.0, "positive"),
        EventCell("E5", "B", ATTENTION_B, POOLED_B, "abnormal_vol", "up", +1.0),
    ]
    return tuple(out)


CELLS: tuple[EventCell, ...] = _cells()


# --- shock scores --------------------------------------------------------------------------------

def _spec(track: str, signal: str, window: int) -> MetricSpec:
    return MetricSpec(_SOURCE[track], signal, "mean", ("z",), window=window,
                      min_periods=MIN_PERIODS[window])


def _inputs(gdelt_daily: pd.DataFrame, track: Track) -> pd.DataFrame:
    """GDELT_DAILY_COLUMNS rows of the track's signals (families combined, tone as stored)."""
    rows = screen_inputs(gdelt_daily, track)
    if track == "A":
        tone = gdelt_daily.loc[(gdelt_daily["source"] == "events")
                               & (gdelt_daily["metric"] == TONE_A), GDELT_DAILY_COLUMNS].copy()
        tone["session_date"] = pd.to_datetime(tone["session_date"]).astype("datetime64[ns]")
        rows = pd.concat([rows, tone.astype({"value_sum": "float64", "n": "float64"})],
                         ignore_index=True)
    return rows[rows["metric"].isin(SIGNALS[track])].reset_index(drop=True)


def shock_scores(
    gdelt_daily: pd.DataFrame, trading_days: list[date], assets: list[Asset], track: Track,
    window: int,
) -> pd.DataFrame:
    """SCORE_COLUMNS: z of every signal of `track` (SIGNALS) vs a trailing W-day baseline.

    Only the track's assets are mapped. Value at t uses GDELT rows rolled into trading days <= t
    only; NaN/inf (short or constant baseline, no denominator) are dropped. Filter inputs to
    dates before `config.HOLDOUT_START` before calling.
    """
    if track not in SIGNALS:
        raise ValueError(f"unknown track {track!r}")
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS} (ADR 0005), got {window!r}")
    track_assets = [a for a in assets if a.track == track]
    rows = _inputs(gdelt_daily, track)
    if not track_assets or rows.empty:
        return pd.DataFrame(columns=SCORE_COLUMNS)
    specs = [_spec(track, s, window) for s in SIGNALS[track]]
    feats = build_features(rows, trading_days, track_assets, specs)
    rename = {feature_name(sp, "z"): sp.metric for sp in specs}
    feats = feats[feats["feature"].isin(rename)]
    out = feats.assign(signal=feats["feature"].map(rename))[SCORE_COLUMNS]
    return out.sort_values(["date", "ticker", "signal"], ignore_index=True)


def cell_scores(scores: pd.DataFrame, cell: EventCell) -> pd.DataFrame:
    """[date, ticker, value]: the cell's directional shock; an event candidate when value >= theta.

    'up' -> z of the trigger signal; 'down' -> -z (low tone). Track B split cells keep only rows
    whose same-day (same ticker) tone z exists and is < 0 ('negative') or >= 0 ('positive').
    """
    s = scores[scores["signal"] == cell.signal][["date", "ticker", "value"]]
    if cell.trigger == "down":
        s = s.assign(value=-s["value"])
    if cell.split is not None:
        tone = scores[scores["signal"] == TONE_B][["date", "ticker", "value"]]
        s = s.merge(tone.rename(columns={"value": "tone"}), on=["date", "ticker"], how="inner")
        keep = s["tone"] < 0 if cell.split == "negative" else s["tone"] >= 0
        s = s[keep][["date", "ticker", "value"]]
    return s.sort_values(["date", "ticker"], ignore_index=True)


# --- outcomes ------------------------------------------------------------------------------------

def outcome_groups(assets: list[Asset], track: Track) -> dict[str, str]:
    """Ticker -> outcome group: A dm / em / commodity (screen_group), B the GICS sector."""
    out = {}
    for a in assets:
        g = a.screen_group
        if a.track != track or g is None or (track == "A" and g not in GROUPS_A):
            continue
        out[a.ticker] = g
    return out


def outcomes(
    prices: pd.DataFrame, groups: Mapping[str, str], horizons: list[int] | tuple[int, ...] = HORIZONS,
    trading_days: list[date] | None = None,
) -> pd.DataFrame:
    """OUTCOME_COLUMNS for every priced ticker (raw) and every grouped ticker (abnormal).

    Labels are built here from `prices` (`labels.make_labels`, `_vol_ratio`) on the shared
    calendar (`trading_days` if given); stored labels lack h = 10. Only finite values
    are returned. `groups` maps ticker -> group; tickers absent from it get raw returns only.
    """
    hs = sorted({int(h) for h in horizons})
    labs = make_labels(prices, hs, trading_days)
    parts = []
    for h in hs:
        ret = labs[labs["horizon"] == h].pivot(index="date", columns="ticker", values="ret_oo")
        vol = _vol_ratio(prices, h, trading_days)
        parts.append(_long(ret, h, "raw_return"))
        abn_r, abn_v = [], []
        for g in sorted(set(groups.values())):
            members = sorted(t for t, gg in groups.items() if gg == g)
            r = ret.reindex(columns=[m for m in members if m in ret.columns])
            v = vol.reindex(columns=[m for m in members if m in vol.columns])
            n_other = r.notna().sum(axis=1).to_numpy()[:, None] - 1
            peer = (r.sum(axis=1, min_count=1).to_numpy()[:, None] - r) / np.where(
                n_other >= 1, n_other, np.nan)
            abn_r.append(r - peer)
            med = v.median(axis=1).where(v.notna().sum(axis=1) >= 2)
            abn_v.append(v.sub(med, axis=0))
        if abn_r:
            parts.append(_long(pd.concat(abn_r, axis=1), h, "abnormal_return"))
            parts.append(_long(pd.concat(abn_v, axis=1), h, "abnormal_vol"))
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=OUTCOME_COLUMNS)
    out = out[np.isfinite(out["value"].astype("float64"))]
    return out[OUTCOME_COLUMNS].sort_values(["date", "ticker", "horizon", "outcome"],
                                            ignore_index=True)


def _vol_ratio(prices: pd.DataFrame, h: int, trading_days: list[date] | None) -> pd.DataFrame:
    """date x ticker: ln(forward RMS open-to-open log return over opens t+1..t+1+h / trailing
    22-day RMS close-to-close log return to t). NaN on a zero vol or any missing price.

    The forward leg starts at the t+1 open, the entry (ADR 0005), so the close t -> open t+1
    gap, which happens before entry, is not in it (`labels.vol_surprise_label` starts at the t
    close and is not used here). The trailing leg is vol_surprise_label's.
    """
    o, c, _ = labels_wide(prices, ["adj_open", "adj_close"], trading_days)
    r2_oo = np.log(o / o.shift(1)) ** 2  # row s: ln(O_s / O_{s-1})^2
    fwd = ln_rms(r2_oo, h).shift(-(h + 1))  # opens t+1..t+1+h
    trail = ln_rms(np.log(c / c.shift(1)) ** 2, SURPRISE_TRAILING_DAYS)
    out = fwd - trail
    out.index = pd.DatetimeIndex(out.index, name="date")
    out.columns.name = "ticker"
    return out


def _long(wide: pd.DataFrame, h: int, kind: str) -> pd.DataFrame:
    s = wide.stack().rename("value").reset_index()
    s.columns = ["date", "ticker", "value"]
    return s.assign(horizon=np.int64(h), outcome=kind)[OUTCOME_COLUMNS]
