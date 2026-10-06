"""Orchestration for the eventtrade CLI: one public function per subcommand, each returning an exit code.

Exit codes: 0 ok; 1 partial failure (some tickers failed, the rest were stored); 2 refused
(hold-out guard, bad arguments); 3 BigQuery budget refused a query; 4 a prerequisite is missing
(store not initialised, no assets, no prices, no features, no trials).

Hold-out discipline (ADR 0001): every function here reads and writes development data only
(dates < cfg.holdout_start). Store reads are half-open, so `end=cfg.holdout_start` never returns
the first hold-out day. The single exception is `holdout`, which refuses to run unless the caller
confirms, evaluates one backtest trial once, and keeps hold-out features in memory only.

Adapters arrive as arguments typed by the Protocols below (declared here, in the consumer);
`cli/main.py` is the only module that names concrete adapters.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

from eventtrade.config import Config
from eventtrade.core import aliases as core_aliases
from eventtrade.core import (
    cv,
    evaluate,
    event_signals,
    eventstudy,
    features,
    labels,
    models,
    overfit,
    screen_features,
)
from eventtrade.core import screen as screen_stats
from eventtrade.core.features import MetricSpec
from eventtrade.schemas import (
    FEATURE_COLUMNS,
    SCORE_COLUMNS,
    TRIAL_RETURN_COLUMNS,
    Asset,
    BqUsage,
    Track,
    Trial,
)

Echo = Callable[[str], None]
Clock = Callable[[], datetime]
Dataset = Literal["events", "gkg-themes", "gkg-orgs"]

OK, PARTIAL, REFUSED, BUDGET, MISSING = 0, 1, 2, 3, 4


# --- Protocols (edges) ------------------------------------------------------------------------

class Store(Protocol):
    """What the pipelines need from storage (adapters/store.SqliteStore satisfies it)."""

    def init(self) -> None: ...
    def status(self, now: datetime | None = None) -> dict[str, Any]: ...
    def record(self, usage: BqUsage) -> None: ...
    def month_to_date_bytes(self, now: datetime) -> int: ...
    def upsert_gdelt_daily(self, df: pd.DataFrame, query_name: str) -> int: ...
    def read_gdelt_daily(self, source: str, start: date | None, end: date | None,
                         entities: Sequence[str] | None = None) -> pd.DataFrame: ...
    def gdelt_session_dates(self, query_name: str, start: date | None,
                            end: date | None) -> list[date]: ...
    def upsert_prices(self, df: pd.DataFrame, source: str) -> int: ...
    def read_prices(self, tickers: Sequence[str] | None, start: date | None,
                    end: date | None) -> pd.DataFrame: ...
    def upsert_assets(self, assets: Iterable[Asset]) -> int: ...
    def delete_assets(self, tickers: Sequence[str]) -> int: ...
    def read_assets(self, track: str | None = None) -> list[Asset]: ...
    def replace_features(self, df: pd.DataFrame) -> int: ...
    def read_features(self, start: date | None, end: date | None,
                      features: Sequence[str] | None = None) -> pd.DataFrame: ...
    def replace_labels(self, df: pd.DataFrame) -> int: ...
    def read_labels(self, start: date | None, end: date | None,
                    horizons: Sequence[int] | None = None) -> pd.DataFrame: ...
    def add_trial(self, trial: Trial, returns: pd.DataFrame | None = None) -> None: ...
    def read_trials(self, track: str | None = None, stage: str | None = None) -> list[Trial]: ...
    def read_trial_returns(self, trial_ids: Sequence[str]) -> pd.DataFrame: ...


class GdeltSource(Protocol):
    """Budget-guarded GDELT puller (adapters/gdelt_bq.GdeltBigQuery satisfies it)."""

    def estimate(self, query_name: str, start: date, end: date, **params: Any) -> int: ...
    def pull_events_daily(self, start: date, end: date, cutoff_et: str) -> pd.DataFrame: ...
    def pull_gkg_themes_daily(self, start: date, end: date, cutoff_et: str, themes: list[str],
                              by_country: bool) -> pd.DataFrame: ...
    def pull_gkg_orgs_daily(self, start: date, end: date, cutoff_et: str,
                            aliases: list[str]) -> pd.DataFrame: ...


# fetch(tickers, start, end_inclusive) -> PRICE_COLUMNS. On partial failure it raises an exception
# carrying `.failures` (ticker -> reason) and `.prices` (what did arrive).
FetchPrices = Callable[[list[str], date, date], pd.DataFrame]
SanityCheck = Callable[[pd.DataFrame], pd.DataFrame]
FetchIndex = Callable[[str], list[Asset]]
LoadEtfs = Callable[[], list[Asset]]


# --- Pre-registered feature families (ADR 0001, amended by ADR 0002) ---------------------------

@dataclass(frozen=True)
class _Family:
    hypothesis: str
    spec: MetricSpec
    # Pre-registered expected IC sign. None = no sign (H0): gate 1 accepts either (ADR 0002 item
    # 3), and a backtest needs an explicit --sign. Backtests trade this direction by default.
    sign: float | None


_A1_ROOTS = ("root_14_protest", "root_17_coerce", "root_18_assault", "root_19_fight",
             "root_20_massviolence")

# ADR 0002 item 1: every z family also gets the low-turnover `sm5` transform (5-day mean of z),
# evaluated at h = 5 and 20 only (`_SM5_HORIZONS`).
_FAMILIES: dict[str, list[_Family]] = {
    "A": [
        _Family("H0", MetricSpec("events", "tone", "mean", ("z", "sm5")), None),
        _Family("H0", MetricSpec("events", "n_events", "share", ("z", "sm5")), None),
        *(_Family("A1", MetricSpec("events", r, "sum_share", ("z", "lr", "sm5"),
                                   denominator_metric="n_events"), -1.0) for r in _A1_ROOTS),
        _Family("A1", MetricSpec("events", "goldstein", "mean", ("z", "sm5")), 1.0),
        _Family("A2", MetricSpec("gkg", "theme_docs", "mean", ("z", "sm5")), 1.0),
        _Family("A2", MetricSpec("gkg", "theme_tone", "mean", ("z", "sm5")), 1.0),
        _Family("A3", MetricSpec("events", "tone", "mean", ("z", "sm5"), agg_days=5), 1.0),
    ],
    "B": [
        _Family("B1", MetricSpec("gkg", "org_docs", "mean", ("z", "sm5")), 1.0),
        _Family("B1", MetricSpec("gkg", "org_tone", "mean", ("z", "sm5", "level")), 1.0),
        _Family("B2", MetricSpec("gkg", "org_neg_docs", "mean", ("z", "sm5")), -1.0),
        _Family("B2", MetricSpec("gkg", "org_neg_docs", "sum", ("z", "lr", "sm5")), -1.0),
    ],
}
_SM5_HORIZONS = (5, 20)
# A4 (ADR 0001/0002): event shocks raise future volatility, pre-registered sign positive in
# *shock* units. A1 registers each feature's return sign for a shock (conflict counts -1,
# Goldstein +1, since a shock is a LOW Goldstein), so a feature's shock orientation is minus its
# A1 sign, and its expected A4 IC sign is _A4_SIGN * (-A1 sign): +1 for counts, -1 for Goldstein.
_A4_SIGN = 1.0
_A4_LABEL = "vol_surprise_partial"
_A4_PLACEBO = "trailing_expanding_mean"
# B1: attention surprise x tone level, built after the base features.
_B1_PRODUCT = ("gkg.org_docs.mean_z.60", "gkg.org_tone.mean.0")
_B1_PRODUCT_NAME = "gkg.org_docs.mean_z.60*gkg.org_tone.mean.0"

_QUERY_OF: dict[str, str] = {"events": "events_daily", "gkg-themes": "gkg_themes_daily",
                             "gkg-orgs": "gkg_orgs_daily"}
_WF_TRAIN_DAYS, _WF_TEST_DAYS = 504, 63
_DEFAULT_MODEL_PARAMS: dict[str, dict[str, Any]] = {
    "ridge": {"alpha": 1.0},
    "logistic": {"C": 1.0},
    "lgbm": {"n_estimators": 200, "learning_rate": 0.05, "num_leaves": 15,
             "min_child_samples": 100, "subsample": 0.8, "subsample_freq": 1,
             "colsample_bytree": 0.8},
}


# --- status / init ----------------------------------------------------------------------------

def status(cfg: Config, store: Store, now: Clock, echo: Echo = print) -> int:
    """Print row counts and date ranges per table, BigQuery spend this month, and trial counts."""
    st = store.status(now())
    echo(f"store: {st['path']}")
    echo(f"hold-out starts {cfg.holdout_start} (never loaded outside `holdout`)")
    echo(f"{'table':<14}{'rows':>12}  {'min':<34}{'max':<34}")
    for name, t in st["tables"].items():
        echo(f"{name:<14}{t['rows']:>12,}  {t['min_date'] or '-'!s:<34}"
             f"{t['max_date'] or '-'!s:<34}")
    spent = int(st["bq_bytes_this_month"])
    echo(f"BigQuery billed this month: {_gb(spent)} of {_gb(cfg.bq_monthly_budget_bytes)}"
         f" (left {_gb(cfg.bq_monthly_budget_bytes - spent)})")
    counts: dict[tuple[str, str], int] = {}
    for t in store.read_trials():
        counts[(t.track, t.stage)] = counts.get((t.track, t.stage), 0) + 1
    for (track, stage), n in sorted(counts.items()):
        echo(f"trials track {track} stage {stage}: {n}")
    return OK


def init_db(cfg: Config, store: Store, echo: Echo = print) -> int:
    """Create the SQLite file and tables if missing (idempotent)."""
    store.init()
    echo(f"initialised {cfg.db_path}")
    return OK


# --- universe ---------------------------------------------------------------------------------

def load_universe(
    cfg: Config, store: Store, track: Track | None, load_etfs: LoadEtfs, fetch_index: FetchIndex,
    echo: Echo = print,
) -> int:
    """Write the Track A ETF list and/or Track B index members (with GKG org aliases) to assets.

    A reloaded track is replaced, not merged: its stored tickers missing from the new list are
    deleted (e.g. the GOOG/FOX/NWS second share classes dropped by ADR 0002 item 2), so they
    stop entering features, panels and backtests. Their prices and trials are kept.
    """
    assets: list[Asset] = []
    if track in (None, "A"):
        etfs = load_etfs()
        echo(f"track A: {len(etfs)} ETFs")
        assets += etfs
    if track in (None, "B"):
        stocks: dict[str, Asset] = {}
        for index in ("sp500", "sp400"):
            members = fetch_index(index)
            echo(f"track B: {index} {len(members)} members")
            for a in members:
                stocks.setdefault(a.ticker, a)
        with_aliases = [_with_aliases(a) for a in stocks.values()]
        n_none = sum(1 for a in with_aliases if not a.org_aliases)
        echo(f"track B: {len(with_aliases)} stocks, {n_none} with no unambiguous GKG alias")
        assets += with_aliases
    n = store.upsert_assets(assets)
    echo(f"upserted {n} assets")
    loaded = {a.ticker for a in assets}
    stale = sorted(a.ticker for t in (("A", "B") if track is None else (track,))
                   for a in store.read_assets(t) if a.ticker not in loaded)
    if stale:
        store.delete_assets(stale)
        shown = ", ".join(stale[:10]) + (", ..." if len(stale) > 10 else "")
        echo(f"removed {len(stale)} stale assets no longer in the universe: {shown}")
    return OK


def _with_aliases(a: Asset) -> Asset:
    keep = [x for x in core_aliases.make_aliases(a.name, a.ticker)
            if not core_aliases.is_ambiguous(x)]
    return a.model_copy(update={"org_aliases": keep})


# --- ingest: prices ---------------------------------------------------------------------------

def ingest_prices(
    cfg: Config, store: Store, fetch: FetchPrices, tickers: list[str] | None,
    track: Track | None, start: date | None, end: date | None,
    sanity: SanityCheck | None = None, chunk: int = 100, echo: Echo = print,
) -> int:
    """Fetch daily bars for the dev window (default: every stored asset) and upsert them."""
    window = _dev_window(cfg, start, end, echo)
    if window is None:
        return REFUSED
    return _ingest_prices_window(store, fetch, tickers, track, *window, sanity, chunk, echo)


def _ingest_prices_window(
    store: Store, fetch: FetchPrices, tickers: list[str] | None, track: Track | None,
    start: date, end: date, sanity: SanityCheck | None, chunk: int, echo: Echo,
) -> int:
    if not tickers:
        tickers = [a.ticker for a in store.read_assets(track)]
    if not tickers:
        echo("no tickers: run `load-universe` first or pass --tickers")
        return MISSING
    echo(f"prices {start}..{end} (inclusive) for {len(tickers)} tickers")
    failures: dict[str, str] = {}
    flags = 0
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        try:
            df = fetch(batch, start, end)
        except Exception as e:  # partial failure carries what did arrive
            if not hasattr(e, "failures"):
                raise
            failures |= dict(e.failures)
            df = e.prices
        n = store.upsert_prices(df, "yfinance") if len(df) else 0
        if sanity is not None and len(df):
            flags += len(sanity(df))
        echo(f"  tickers {i + 1}-{i + len(batch)}: {n:,} rows")
    if sanity is not None:
        echo(f"sanity flags: {flags} (see adapters/prices.sanity_check)")
    if failures:
        echo(f"{len(failures)} ticker(s) failed; rerun with --tickers {' '.join(sorted(failures))}")
        for t, reason in sorted(failures.items()):
            echo(f"  {t}: {reason}")
        return PARTIAL
    return OK


# --- ingest: GDELT ----------------------------------------------------------------------------

def ingest_gdelt(
    cfg: Config, store: Store, gdelt: GdeltSource, dataset: Dataset, start: date | None,
    end: date | None, execute: bool, by_country: bool, now: Clock,
    budget_error: type[Exception] = RuntimeError, echo: Echo = print,
) -> int:
    """Dry-run (default) or pull month by month, skipping months already stored; dev window only."""
    if by_country:
        # No feature reads per-country theme rows (themes_as_entities re-keys _GLOBAL only), and
        # the country query rewrites the _GLOBAL theme rows under its own query name, which made
        # the next plain gkg-themes ingest re-pull every month. Refused until a spec uses them.
        echo("refused: --by-country is disabled; no feature reads per-country theme rows yet")
        return REFUSED
    window = _dev_window(cfg, start, end, echo)
    if window is None:
        return REFUSED
    return _ingest_gdelt_window(cfg, store, gdelt, dataset, *window, execute, by_country, now,
                                budget_error, echo)


def _ingest_gdelt_window(
    cfg: Config, store: Store, gdelt: GdeltSource, dataset: Dataset, start: date, end: date,
    execute: bool, by_country: bool, now: Clock, budget_error: type[Exception], echo: Echo,
) -> int:
    plan = _gdelt_plan(store, dataset, by_country)
    if plan is None:
        echo(f"{dataset}: no themes/aliases in assets; run `load-universe` first")
        return MISSING
    query_name, params, pull = plan
    chunks = month_chunks(start, end)
    pending = pending_months(store, query_name, start, end)
    echo(f"{dataset} ({query_name}) sessions {start}..{end}: {len(chunks)} month(s),"
         f" {len(chunks) - len(pending)} already stored, {len(pending)} to pull")
    spent = store.month_to_date_bytes(now())
    left = cfg.bq_monthly_budget_bytes - spent
    if not execute:
        total = 0
        for a, b in pending:
            est = gdelt.estimate(query_name, a, b, cutoff_et=cfg.cutoff_et, **params)
            total += est
            echo(f"  {a}..{b}: {_gb(est)}")
        echo(f"estimate {_gb(total)}; budget left this month {_gb(left)}"
             f" (of {_gb(cfg.bq_monthly_budget_bytes)}); per-query cap"
             f" {_gb(cfg.bq_max_bytes_per_query)}")
        if total > left:
            echo("estimate exceeds what is left this month: --execute would stop part-way"
                 " and can be resumed next month")
        echo("dry run only; pass --execute to pull")
        return OK
    for a, b in pending:
        try:
            df = pull(gdelt, a, b, cfg.cutoff_et)
        except budget_error as e:
            echo(f"refused by budget guard at {a}..{b}: {e}")
            echo("already-pulled months are stored; rerun later to resume")
            return BUDGET
        n = store.upsert_gdelt_daily(df, query_name)
        echo(f"  {a}..{b}: {n:,} rows")
    return OK


def _gdelt_plan(
    store: Store, dataset: Dataset, by_country: bool
) -> tuple[str, dict[str, Any], Callable[[GdeltSource, date, date, str], pd.DataFrame]] | None:
    """(query name, extra query params, pull function) for a dataset, or None if inputs missing."""
    if dataset == "events":
        return _QUERY_OF["events"], {}, lambda g, a, b, c: g.pull_events_daily(a, b, c)
    if dataset == "gkg-themes":
        themes = sorted({t for a in store.read_assets("A") for t in a.gkg_themes})
        if not themes:
            return None
        name = "gkg_themes_country_daily" if by_country else _QUERY_OF["gkg-themes"]
        return name, {"themes": themes}, (
            lambda g, a, b, c: g.pull_gkg_themes_daily(a, b, c, themes, by_country))
    aliases = sorted({x.strip().lower() for a in store.read_assets("B") for x in a.org_aliases})
    if not aliases:
        return None
    return _QUERY_OF["gkg-orgs"], {"aliases": aliases}, (
        lambda g, a, b, c: g.pull_gkg_orgs_daily(a, b, c, aliases))


def pending_months(
    store: Store, query_name: str, start: date, end: date
) -> list[tuple[date, date]]:
    """Month chunks of [start, end] (inclusive) not yet stored under `query_name`.

    A month counts as stored when both its first and last session dates have rows written by
    that query (what `ingest-gdelt` skips). Reads session dates only, never values.
    """
    have = set(store.gdelt_session_dates(query_name, start, end + timedelta(days=1)))
    return [(a, b) for a, b in month_chunks(start, end) if not (a in have and b in have)]


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar-month pieces of [start, end], both inclusive, clipped to the window."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    out = []
    a = start
    while a <= end:
        nxt = date(a.year + (a.month == 12), a.month % 12 + 1, 1)
        b = min(nxt - timedelta(days=1), end)
        out.append((a, b))
        a = nxt
    return out


# --- dataset ----------------------------------------------------------------------------------

def build_dataset(cfg: Config, store: Store, track: Track, echo: Echo = print) -> int:
    """Build features and labels for one track over the dev period and replace them in the store."""
    assets = store.read_assets(track)
    if not assets:
        echo(f"no track {track} assets: run `load-universe --track {track}`")
        return MISSING
    tickers = [a.ticker for a in assets]
    prices = store.read_prices(tickers, cfg.start, cfg.holdout_start)
    if prices.empty:
        echo(f"no prices for track {track}: run `ingest-prices --track {track}`")
        return MISSING
    tdays = _trading_days(prices)
    daily = _read_gdelt(store, track, assets, cfg.start, cfg.holdout_start)
    echo(f"track {track}: {len(assets)} assets, {len(prices):,} price rows,"
         f" {len(tdays)} trading days, {len(daily):,} GDELT rows")
    feats = _track_features(track, daily, tdays, assets)
    if feats.empty:
        echo("no features built (no GDELT rows yet?): labels only")
    else:
        n = store.replace_features(feats)
        echo(f"features: {n:,} rows, {feats['feature'].nunique()} names")
    lab = labels.make_labels(prices, list(cfg.horizons), tdays)
    echo(f"labels: {store.replace_labels(lab):,} rows, horizons {list(cfg.horizons)}")
    return OK


def _read_gdelt(
    store: Store, track: Track, assets: list[Asset], start: date, end: date
) -> pd.DataFrame:
    if track == "A":
        ents = [features.GLOBAL, *sorted({c for a in assets for c in a.fips_countries})]
        parts = [store.read_gdelt_daily("events", start, end, ents),
                 store.read_gdelt_daily("gkg", start, end, ents)]
    else:
        ents = sorted({x for a in assets for x in a.org_aliases})
        parts = [store.read_gdelt_daily("gkg", start, end, ents)] if ents else []
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _track_features(
    track: Track, daily: pd.DataFrame, tdays: list[date], assets: list[Asset]
) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    if track == "A":
        daily = features.themes_as_entities(daily)
    specs = [f.spec for f in _FAMILIES[track]]
    out = features.build_features(daily, tdays, assets, specs)
    if track == "B":
        prod = features.product_feature(out, *_B1_PRODUCT, _B1_PRODUCT_NAME)
        out = pd.concat([out, prod], ignore_index=True)
    return out


def hypothesis_of(track: Track, feature: str) -> tuple[str, float | None]:
    """Pre-registered hypothesis id and expected sign of a feature name.

    The sign is None for H0 (no pre-registered sign) and for unknown names ('?').
    """
    fam = _family_of(track, feature)
    if fam is None:
        return "?", None
    return fam[0].hypothesis, fam[0].sign


def _family_of(track: Track, feature: str) -> tuple[_Family, str] | None:
    """(family, transform) a stored feature name was built from; None if it is not registered."""
    if track == "B" and feature == _B1_PRODUCT_NAME:
        return _Family("B1", MetricSpec("gkg", "org_docs", "mean"), 1.0), "product"
    for fam in _FAMILIES[track]:
        for tr in fam.spec.transforms:
            if feature == features.feature_name(fam.spec, tr):
                return fam, tr
    return None


def _horizons_for(track: Track, feature: str, horizons: Sequence[int]) -> list[int]:
    """Gate-1 horizons of a feature: sm5 (low-turnover) features only at h = 5 and 20."""
    fam = _family_of(track, feature)
    if fam is not None and fam[1] == "sm5":
        return [h for h in horizons if h in _SM5_HORIZONS]
    return list(horizons)


def _a4_sign(feature: str) -> float | None:
    """Expected A4 IC sign of an A1 feature (see `_A4_SIGN`); None if not an A1 feature."""
    fam = _family_of("A", feature)
    if fam is None or fam[0].hypothesis != "A1" or fam[0].sign is None:
        return None
    return _A4_SIGN * -fam[0].sign


# --- signal study (gate 1) --------------------------------------------------------------------

def signal_study(cfg: Config, store: Store, track: Track, now: Clock, echo: Echo = print) -> int:
    """Evaluate every stored feature x horizon of a track, log each as a 'signal' trial, print gates.

    Trial params carry `features_version` and the pre-registered sign (ADR 0002 item 5), so v2
    trials are new rows next to the v1 ones (which have no `features_version`) and nothing is
    re-logged; overfit_report's N counts both.
    """
    assets = store.read_assets(track)
    tickers = {a.ticker for a in assets}
    feats = _for_tickers(store.read_features(cfg.start, cfg.holdout_start), tickers)
    labs = _for_tickers(store.read_labels(cfg.start, cfg.holdout_start), tickers)
    if feats.empty or labs.empty:
        echo(f"no features/labels for track {track}: run `build-dataset --track {track}`")
        return MISSING
    if not _has_current_features(track, feats):
        echo(f"stored track {track} features predate features v{features.FEATURES_VERSION}"
             f" (no sm5 feature): run `build-dataset --track {track}` first; nothing logged")
        return MISSING
    cost = cfg.cost_bps_etf if track == "A" else cfg.cost_bps_stock
    logged = {t.trial_id for t in store.read_trials(track, "signal")}
    d0, d1 = feats["date"].min().date(), feats["date"].max().date()
    n_pass = 0
    echo(f"{'feature':<52}{'h':>3}{'ic_mean':>9}{'nw_t':>7}  gate")
    for name, sub in feats.groupby("feature", sort=True):
        hyp, sign = hypothesis_of(track, str(name))
        hs = _horizons_for(track, str(name), cfg.horizons)
        for h in hs:
            params = {"feature": str(name), "horizon": h, "label": "ret_oo",
                      "features_version": features.FEATURES_VERSION, "expected_sign": sign}
            try:
                rep = evaluate.evaluate_signal(sub, labs, str(name), [h, *[x for x in hs if x != h]],
                                               track)
                subs = evaluate.subperiod_signs(evaluate.daily_ic(sub, labs, h))
                ok, reason = evaluate.passes_gate(rep, subs, cost, expected_sign=sign)
                note = {"gate": reason, "subperiod_ic": subs, "report": rep.model_dump()}
                line = f"{rep.ic_mean:>9.4f}{rep.ic_t:>7.2f}"
            except ValueError as e:
                ok, reason = False, f"fail: {e}"
                note = {"gate": reason}
                line = f"{'nan':>9}{'nan':>7}"
            n_pass += ok
            echo(f"{name!s:<52}{h:>3}{line}  {reason}")
            _log_trial(store, logged, track, "signal", hyp, params, d0, d1, now, note)
    if track == "A":
        n_pass += _signal_study_vol(cfg, store, feats, tickers, d0, d1, logged, now, echo)
    echo(f"{n_pass} feature x horizon combination(s) passed gate 1")
    return OK


def _has_current_features(track: Track, feats: pd.DataFrame) -> bool:
    """True if the store holds at least one sm5 feature (new in features v2) for the track.

    The store does not record the features version, so this is the available proxy: a v1
    build has no sm5 names, and logging its rows under `features_version: 2` would be wrong.
    """
    sm5 = {features.feature_name(f.spec, "sm5") for f in _FAMILIES[track]
           if "sm5" in f.spec.transforms}
    return bool(sm5 & set(feats["feature"].unique()))


def _signal_study_vol(
    cfg: Config, store: Store, feats: pd.DataFrame, tickers: set[str], d0: date, d1: date,
    logged: set[str], now: Clock, echo: Echo,
) -> int:
    """A4 (ADR 0002 item 4): A1 event-shock features vs the vol surprise, partial IC + placebo.

    Label ln(fwd vol over h / trailing 22d vol); feature and label residualised per day on
    ln rv5, ln rv22, ln rv60 and ln long-run vol; the trailing-mean placebo must not pass.
    """
    prices = store.read_prices(sorted(tickers), cfg.start, cfg.holdout_start)
    if prices.empty:
        return 0
    tdays = _trading_days(prices)
    controls = labels.vol_controls(prices, tdays)
    a1 = sorted(str(n) for n in feats["feature"].unique() if _a4_sign(str(n)) is not None)
    n_pass = 0
    for h in cfg.horizons:
        label = labels.vol_surprise_label(prices, h, tdays)
        for name in a1:
            if h not in _horizons_for("A", name, cfg.horizons):
                continue
            sign = _a4_sign(name)
            res = evaluate.vol_gate(feats[feats["feature"] == name], label, controls, h,
                                    expected_sign=sign)
            n_pass += res.ok
            echo(f"{'[A4 vol] ' + name:<52}{h:>3}{res.ic_mean:>9.4f}{res.ic_t:>7.2f}  {res.reason}")
            params = {"feature": name, "horizon": h, "label": _A4_LABEL,
                      "controls": list(labels.VOL_CONTROL_NAMES), "placebo": _A4_PLACEBO,
                      "features_version": features.FEATURES_VERSION, "expected_sign": sign}
            _log_trial(store, logged, "A", "signal", "A4", params, d0, d1, now, res.notes())
    return n_pass


# --- sub-category screen (ADR 0003) -----------------------------------------------------------

# ADR 0003 "Statistics", step 1. Inclusive feature-date windows. Changing them is a new ADR.
SCREEN_DISCOVERY = (date(2015, 3, 2), date(2019, 12, 31))
SCREEN_CONFIRMATION = (date(2020, 1, 1), date(2025, 3, 31))
SCREEN_HORIZONS = (5, 20)
SCREEN_MIN_NAMES = 5
SCREEN_RECIPE = "sm5_z.60"
SCREEN_HYPOTHESIS = "S3"
SCREEN_BH_Q = 0.10
SCREEN_HOLM_ALPHA = 0.05
# Joint test (step 2), as settled in ADR 0003's implementation notes: directional statistics
# S = sum max(z, 0)^2 and M = max z (z = sign x t, |t| for two-sided cells); the track passes
# when min(p_S, p_M) < 5% / 2 (Bonferroni over the two statistics).
SCREEN_JOINT_ALPHA = 0.05
SCREEN_JOINT_RULE = "min(p_S, p_M) < alpha / 2"
# Stationary-bootstrap mean block length: 2 x the longest horizon (implementation notes).
SCREEN_MEAN_BLOCK = 2.0 * max(SCREEN_HORIZONS)
# Fewest bootstrap draws accepted (implementation notes, "Trial identity").
SCREEN_MIN_N_BOOT = 999
# Trial note of a cell with fewer than SCREEN_MIN_NAMES names on most of its discovery days.
SCREEN_THIN_NOTE = "insufficient names"

# Cell sets the screen can run (`screen --set`). 's3': ADR 0003 sub-category screen (both tracks).
# 'a2': ADR 0004 GKG theme screen (Track A only), same statistics, its own cells and hypothesis.
ScreenSet = Literal["s3", "a2"]
SCREEN_SETS: tuple[str, ...] = ("s3", "a2")
A2_HYPOTHESIS = "A2"
# ADR 0004 "Run condition": every dev month of this query must be stored before A2 runs.
A2_QUERY = _QUERY_OF["gkg-themes"]


@dataclass(frozen=True)
class _ScreenCell:
    signal: str  # signal name in core/screen_features, e.g. 'violence', 'attention'
    feature: str  # '<hypothesis>.<signal>', e.g. 'S3.violence', 'A2.tone'
    group: str  # S3 Track A 'dm' | 'em' | 'commodity'; Track B a GICS sector; A2 'commodity' | 'sector'
    horizon: int
    sign: float | None  # pre-registered IC sign; None = two-sided
    members: tuple[str, ...]
    hypothesis: str = SCREEN_HYPOTHESIS
    themes_hash: str | None = None  # A2 only: hash of the members' theme mapping

    def id(self, track: Track) -> str:
        return f"{track}|{self.signal}|{self.group}|h{self.horizon}"


def screen(
    cfg: Config, store: Store, track: Track, now: Clock, echo: Echo = print, n_boot: int = 2000,
    screen_set: ScreenSet = "s3",
) -> int:
    """Pre-registered screen of one track: discovery, joint test, EB, BH, confirmation.

    `screen_set` 's3' (default): ADR 0003 sub-category screen, hypothesis S3. Cells = the track's
    screen signals x asset groups (`Asset.screen_group`) x horizons 5, 20.
    `screen_set` 'a2': ADR 0004 GKG theme screen, Track A only, hypothesis A2. Cells = theme
    attention and tone x the pinned groups commodity / sector (`screen_features.
    THEME_GROUP_MEMBERS`) x horizons 5, 20 = 8 cells. MISSING unless every dev month of the
    `gkg_themes_daily` query is stored (checked on session dates, before any value is read) and
    every pinned member is a stored asset with themes; REFUSED on Track B. Trial params also
    carry `hypothesis` 'A2' and a hash of the members' theme mapping. Everything below is shared.

    Signals are built in memory (`core/screen_features`, sm5_z.60), never stored. Every
    pre-registered cell counts (ADR 0003 implementation notes): a cell whose discovery IC is
    defined (>= 5 names) on fewer than half of its discovery days is kept, left out of the joint
    test and EB, given p = NaN (counted in BH's m, never discovered) and logged with the note
    "insufficient names". Discovery labels are embargoed: a horizon-h cell uses feature dates
    whose label window ends by 2019-12-31. Every cell x period is logged as a `signal` trial
    (hypothesis S3) whose params include n_boot, the mean block, the seed and a hash of the
    cell's members, so re-running with the same settings logs nothing new and any other setting
    is a new trial. If the joint test is not significant the track stops after logging the
    discovery trials. REFUSED if n_boot < 999. Dev data only: every read ends at
    `cfg.holdout_start` (half-open).
    """
    if screen_set not in SCREEN_SETS:
        echo(f"refused: unknown screen set {screen_set!r} (one of {', '.join(SCREEN_SETS)})")
        return REFUSED
    a2 = screen_set == "a2"
    if a2 and track != "A":
        echo("refused: the a2 theme screen (ADR 0004) is Track A only")
        return REFUSED
    if n_boot < SCREEN_MIN_N_BOOT:
        echo(f"refused: n_boot {n_boot} < {SCREEN_MIN_N_BOOT} (ADR 0003 implementation notes)")
        return REFUSED
    if SCREEN_CONFIRMATION[1] >= cfg.holdout_start or SCREEN_DISCOVERY[0] < cfg.start:
        echo(f"refused: screen periods {SCREEN_DISCOVERY[0]}..{SCREEN_CONFIRMATION[1]} are not"
             f" inside the dev period {cfg.start}..{cfg.holdout_start} (ADR 0001)")
        return REFUSED
    if a2:
        last_dev = cfg.holdout_start - timedelta(days=1)
        pending = pending_months(store, A2_QUERY, cfg.start, last_dev)
        if pending:
            shown = ", ".join(f"{a:%Y-%m}" for a, _ in pending[:6])
            echo(f"refused to run A2: {len(pending)} dev month(s) of {A2_QUERY} not stored"
                 f" ({shown}{', ...' if len(pending) > 6 else ''}); ADR 0004 runs only when"
                 " `ingest-gdelt gkg-themes` has 0 months to pull for the whole dev period")
            return MISSING
    assets = store.read_assets(track)
    if not assets:
        echo(f"no track {track} assets: run `load-universe --track {track}`")
        return MISSING
    themes_hash: dict[str, str] = {}
    if a2:
        groups, absent = _theme_groups(assets)
        if absent:
            echo(f"A2 members missing from the stored assets or without gkg_themes:"
                 f" {', '.join(absent)}; run `load-universe --track A`")
            return MISSING
        by_ticker = {a.ticker: a for a in assets}
        themes_hash = {g: _themes_hash([by_ticker[t] for t in m]) for g, m in groups.items()}
    else:
        groups = _screen_groups(track, assets)
        if not groups:
            echo(f"no track {track} asset has a screen_group: run `load-universe --track"
                 f" {track}` with the current universe code (ADR 0003 groups)")
            return MISSING
    tickers = sorted({t for members in groups.values() for t in members})
    prices = store.read_prices(tickers, cfg.start, cfg.holdout_start)
    if prices.empty:
        echo(f"no prices for track {track}: run `ingest-prices --track {track}`")
        return MISSING
    tdays = _trading_days(prices)
    if a2:
        daily = store.read_gdelt_daily("gkg", cfg.start, cfg.holdout_start, [features.GLOBAL])
    else:
        daily = _read_screen_gdelt(store, track, assets, cfg.start, cfg.holdout_start)
    if daily.empty:
        echo(f"no GDELT rows for track {track}: run `ingest-gdelt`")
        return MISSING
    if a2:
        signals = screen_features.THEME_SIGNALS
        hypothesis = A2_HYPOTHESIS
        sig = screen_features.theme_signals(daily, tdays,
                                            [a for a in assets if a.ticker in set(tickers)])
    else:
        signals = screen_features.screen_families(track)
        hypothesis = SCREEN_HYPOTHESIS
        sig = screen_features.screen_signals(daily, tdays, assets, track)
    sig = sig[sig["ticker"].isin(tickers) & (sig["date"] < pd.Timestamp(cfg.holdout_start))]
    labs = _screen_labels(cfg, store, prices, tdays, tickers, echo)
    echo(f"track {track}: {len(tickers)} screened assets in {len(groups)} group(s),"
         f" {len(tdays)} trading days, {len(sig):,} signal rows, {len(labs):,} label rows"
         + (f" (hypothesis {hypothesis})" if a2 else ""))

    cells = [
        _ScreenCell(fam.name, fam.feature, g, h, fam.sign(g), tuple(sorted(members)),
                    hypothesis, themes_hash.get(g))
        for fam in signals
        for g, members in sorted(groups.items())
        for h in SCREEN_HORIZONS
    ]
    by_feature = {str(f): s for f, s in sig.groupby("feature", sort=False)}
    empty = pd.DataFrame(columns=FEATURE_COLUMNS)

    # Discovery: per-cell IC on embargoed discovery dates, then the joint test.
    disc_end = {h: _embargoed_end(tdays, SCREEN_DISCOVERY[1], h) for h in SCREEN_HORIZONS}
    n_disc_days = {h: sum(SCREEN_DISCOVERY[0] <= d <= disc_end[h] for d in tdays)
                   for h in SCREEN_HORIZONS}
    ids = {c: c.id(track) for c in cells}
    ic_disc = {ids[c]: screen_stats.cell_ic(by_feature.get(c.feature, empty), labs, c.members,
                                            c.horizon, SCREEN_DISCOVERY[0], disc_end[c.horizon],
                                            SCREEN_MIN_NAMES)
               for c in cells}
    horizon_of = {ids[c]: c.horizon for c in cells}
    signs = {ids[c]: c.sign for c in cells}
    thin = {ids[c] for c in cells
            if _is_thin(int(ic_disc[ids[c]].notna().sum()), n_disc_days[c.horizon])}
    evaluable = [ids[c] for c in cells if ids[c] not in thin]
    if thin:
        echo(f"{len(thin)} cell(s) with fewer than {SCREEN_MIN_NAMES} names on most discovery"
             f" days ({SCREEN_THIN_NOTE}): kept and logged with p = NaN, counted in BH's m,"
             f" never discovered: {', '.join(sorted(thin))}")

    joint: screen_stats.JointResult | None = None
    if evaluable:
        joint = screen_stats.joint_test({k: ic_disc[k] for k in evaluable},
                                        {k: horizon_of[k] for k in evaluable},
                                        {k: signs[k] for k in evaluable},
                                        n_boot, SCREEN_MEAN_BLOCK, cfg.seed)
        significant = _joint_significant(joint.p_s, joint.p_m)
        echo(f"joint test ({len(evaluable)} of {len(cells)} cells, {joint.n_dates} dates,"
             f" {n_boot} draws, mean block {SCREEN_MEAN_BLOCK:g}, seed {cfg.seed}):"
             f" S = {joint.s_stat:.2f} p_S = {joint.p_s:.4f},"
             f" M = {joint.m_stat:.2f} p_M = {joint.p_m:.4f} -> "
             f"{'significant' if significant else 'not significant'} ({SCREEN_JOINT_RULE},"
             f" alpha {SCREEN_JOINT_ALPHA})")
    else:
        significant = False
        echo("joint test not run: every cell has insufficient names")
    nan = float("nan")
    t_disc = {k: nan if joint is None else joint.t.get(k, nan) for k in ic_disc}
    z_disc = {k: nan if joint is None else joint.z.get(k, nan) for k in ic_disc}
    # BH's m is every cell of the track: thin cells enter with p = NaN.
    p_disc = {k: nan if joint is None else joint.p_cell.get(k, nan) for k in ic_disc}

    est = {k: screen_stats.cell_estimate(ic_disc[k], horizon_of[k]) for k in ic_disc}
    direction = {k: _screen_direction(signs[k], t_disc[k]) for k in ic_disc}
    eb: screen_stats.EbResult | None = None
    shrunk: dict[str, float] = {}
    discovered: set[str] = set()
    if significant:
        try:
            eb, shrunk = screen_stats.shrink_directional(
                {k: est[k][0] for k in evaluable}, {k: est[k][1] for k in evaluable},
                {k: signs[k] for k in evaluable}, t_disc)
        except ValueError as e:
            echo(f"EB shrinkage not computed: {e}")
        discovered = screen_stats.benjamini_hochberg(p_disc, SCREEN_BH_Q) - thin
        echo(f"Benjamini-Hochberg q = {SCREEN_BH_Q}: {len(discovered)} of {len(cells)} cell(s)"
             " discovered")
    else:
        echo(f"track {track} stops here (ADR 0003 step 2): no shrinkage, discovery or"
             " confirmation")

    logged = {t.trial_id for t in store.read_trials(track, "signal")}
    n_logged_before = len(logged)
    joint_note = {"s_stat": None if joint is None else _num(joint.s_stat),
                  "m_stat": None if joint is None else _num(joint.m_stat),
                  "p_s": None if joint is None else _num(joint.p_s),
                  "p_m": None if joint is None else _num(joint.p_m), "significant": significant,
                  "rule": SCREEN_JOINT_RULE, "alpha": SCREEN_JOINT_ALPHA, "n_boot": n_boot,
                  "mean_block": SCREEN_MEAN_BLOCK, "seed": cfg.seed, "n_cells": len(cells),
                  "n_tested": len(evaluable), "n_insufficient": len(thin),
                  "n_dates": 0 if joint is None else joint.n_dates}
    rows: dict[str, list[str]] = {}
    for c in cells:
        k = ids[c]
        mean, se = est[k]
        pooled = eb is not None and k in eb.posterior
        note = {"ic_mean": _num(mean), "se": _num(se), "n_days": int(ic_disc[k].notna().sum()),
                "n_period_days": n_disc_days[c.horizon],
                "t": _num(t_disc[k]), "z": _num(z_disc[k]), "p": _num(p_disc[k]),
                "direction": direction[k],
                "note": SCREEN_THIN_NOTE if k in thin else None,
                # shrunk_ic: EB posterior in the cell's direction (applied after shrinkage);
                # eb_posterior: the posterior of the pooled value (sign x IC, or IC if two-sided)
                "shrunk_ic": _num(shrunk[k]) if pooled else None,
                "eb_posterior": _num(eb.posterior[k]) if pooled else None,
                "shrinkage": _num(eb.shrinkage[k]) if pooled else None,
                "posterior_sd": _num(eb.posterior_sd[k]) if pooled else None,
                "eb": None if eb is None else {"tau2": _num(eb.tau2),
                                               "prior_mean": _num(eb.prior_mean),
                                               "n_cells": eb.n_cells},
                "bh_q": SCREEN_BH_Q, "discovered": k in discovered,
                "feature_dates": [SCREEN_DISCOVERY[0].isoformat(),
                                  disc_end[c.horizon].isoformat()],
                "min_names": SCREEN_MIN_NAMES, "joint": joint_note}
        _log_trial(store, logged, track, "signal", c.hypothesis,
                   _screen_params(c, k, "discovery", n_boot, cfg.seed), *SCREEN_DISCOVERY,
                   now, note)
        rows[k] = [f"{note['n_days']:>6}", _fmt(mean, 9, 4), _fmt(t_disc[k], 7, 2),
                   _fmt(p_disc[k], 8, 4), _fmt(shrunk[k] if pooled else nan, 9, 4),
                   "yes" if k in discovered else ("thin" if k in thin else "-")]

    # Confirmation: discovered cells only, each tested once (Holm across them).
    conf: screen_stats.ConfirmResult | None = None
    if discovered:
        conf, conf_days = _screen_confirm(cfg, track, [c for c in cells if ids[c] in discovered],
                                          ids, by_feature, labs, t_disc, empty)
        for c in cells:
            k = ids[c]
            if k not in discovered:
                continue
            v = conf.verdicts[k]
            note = {**v.notes(), "n_days": conf_days[k],
                    "discovery_trial": trial_id("signal", track,
                                                _screen_params(c, k, "discovery", n_boot,
                                                               cfg.seed),
                                                *SCREEN_DISCOVERY),
                    "shrunk_ic": _num(shrunk[k]) if k in shrunk else None,
                    "holm_alpha": SCREEN_HOLM_ALPHA, "cost_bps": conf.cost_bps,
                    "min_names": SCREEN_MIN_NAMES}
            _log_trial(store, logged, track, "signal", c.hypothesis,
                       _screen_params(c, k, "confirmation", n_boot, cfg.seed),
                       *SCREEN_CONFIRMATION, now, note)
            rows[k] += [_fmt(v.t, 7, 2), _fmt(v.p_one_sided, 8, 4),
                        "CONFIRMED" if v.confirmed else "no"]

    echo("ic, t: discovery mean IC and NW t (raw sign); p: bootstrap p in the registered"
         " direction (two-sided if none); shrunk: EB posterior IC in the cell's direction;"
         f" disc 'thin': {SCREEN_THIN_NOTE}; conf_p: one-sided, Holm across discovered cells")
    echo(f"{'cell':<44}{'n':>6}{'ic':>9}{'t':>7}{'p':>8}{'shrunk':>9}  {'disc':<5}"
         f"{'conf_t':>7}{'conf_p':>8}  confirmed")
    for c in cells:
        r = rows[ids[c]]
        tail = "".join(r[6:8]) + f"  {r[8]}" if len(r) > 6 else ""
        echo(f"{ids[c]:<44}{''.join(r[:5])}  {r[5]:<5}{tail}")
    n_conf = 0 if conf is None else len(conf.confirmed)
    n_new = len(logged) - n_logged_before
    echo(f"track {track}: {len(cells)} cells, {len(discovered)} discovered, {n_conf} confirmed;"
         f" {n_new} new trial(s) logged ({len(cells) + len(discovered) - n_new} already logged)")
    return OK


def _screen_confirm(
    cfg: Config, track: Track, cells: list[_ScreenCell], ids: dict[_ScreenCell, str],
    by_feature: dict[str, pd.DataFrame], labs: pd.DataFrame, discovery_t: dict[str, float],
    empty: pd.DataFrame,
) -> tuple[screen_stats.ConfirmResult, dict[str, int]]:
    """ADR 0003 step 5 on the confirmation period; also each cell's number of IC days."""
    lo, hi = (pd.Timestamp(d) for d in SCREEN_CONFIRMATION)
    ic_conf: dict[str, pd.Series] = {}
    spreads: dict[str, float] = {}
    turnovers: dict[str, float] = {}
    for c in cells:
        k = ids[c]
        f = by_feature.get(c.feature, empty)
        f = f[f["ticker"].isin(c.members) & (f["date"] >= lo) & (f["date"] <= hi)]
        ic_conf[k] = screen_stats.cell_ic(f, labs, c.members, c.horizon, *SCREEN_CONFIRMATION,
                                          SCREEN_MIN_NAMES)
        sp = evaluate.quantile_spread(f, labs, c.horizon, min_names=SCREEN_MIN_NAMES)
        spreads[k] = float(sp.mean()) if len(sp) else float("nan")
        turnovers[k] = evaluate.quantile_turnover(f, min_names=SCREEN_MIN_NAMES)
    cost = cfg.cost_bps_etf if track == "A" else cfg.cost_bps_stock
    res = screen_stats.confirm(
        [ids[c] for c in cells], ic_conf, {ids[c]: c.sign for c in cells}, spreads, turnovers,
        cost, {ids[c]: c.horizon for c in cells}, discovery_t, SCREEN_HOLM_ALPHA)
    return res, {k: int(s.notna().sum()) for k, s in ic_conf.items()}


def _screen_groups(track: Track, assets: list[Asset]) -> dict[str, list[str]]:
    """Asset group -> tickers. Track A: the ADR 0003 groups only; Track B: every GICS sector."""
    out: dict[str, list[str]] = {}
    for a in assets:
        g = a.screen_group
        if g is None or (track == "A" and g not in screen_features.GROUPS_A):
            continue
        out.setdefault(g, []).append(a.ticker)
    return out


def _read_screen_gdelt(
    store: Store, track: Track, assets: list[Asset], start: date, end: date
) -> pd.DataFrame:
    """The source the track's screen signals read: events by country (A), GKG by alias (B)."""
    if track == "A":
        ents = sorted({c for a in assets if a.screen_group for c in a.fips_countries})
        return store.read_gdelt_daily("events", start, end, ents) if ents else pd.DataFrame()
    ents = sorted({x for a in assets if a.screen_group for x in a.org_aliases})
    return store.read_gdelt_daily("gkg", start, end, ents) if ents else pd.DataFrame()


def _screen_labels(
    cfg: Config, store: Store, prices: pd.DataFrame, tdays: list[date], tickers: list[str],
    echo: Echo,
) -> pd.DataFrame:
    """Stored dev labels at h = 5, 20 if they cover every priced ticker, else built from prices."""
    hs = list(SCREEN_HORIZONS)
    stored = _for_tickers(store.read_labels(cfg.start, cfg.holdout_start, hs), set(tickers))
    priced = set(prices["ticker"].unique())
    if (not stored.empty and set(stored["horizon"].unique()) >= set(hs)
            and set(stored["ticker"].unique()) >= priced):
        echo("labels: stored dev labels reused")
        return stored
    echo("labels: built from dev prices (stored labels missing or incomplete)")
    return labels.make_labels(prices, hs, tdays)


def _is_thin(n_ic_days: int, n_period_days: int) -> bool:
    """Too few names on most days: the cell's IC (needs >= SCREEN_MIN_NAMES names with a signal
    and a label) is defined on fewer than half of its discovery trading days."""
    return n_period_days <= 0 or n_ic_days < 0.5 * n_period_days


def _embargoed_end(tdays: list[date], end: date, horizon: int) -> date:
    """Last feature date t whose label (adj_open t+1 -> t+1+h) ends on or before `end`."""
    days = [d for d in tdays if d <= end]
    if len(days) < horizon + 2:
        return days[0] - timedelta(days=1) if days else end
    return days[-(horizon + 2)]


def _joint_significant(p_s: float, p_m: float) -> bool:
    ps = [p for p in (p_s, p_m) if math.isfinite(p)]
    return bool(ps) and min(ps) < SCREEN_JOINT_ALPHA / 2


def _screen_direction(sign: float | None, t: float) -> float | None:
    """Registered sign, or the discovery-t sign of a two-sided cell (None if t is undefined)."""
    if sign is not None:
        return float(sign)
    if not math.isfinite(t) or t == 0.0:
        return None
    return 1.0 if t > 0 else -1.0


def _screen_params(
    c: _ScreenCell, cell_id: str, period: str, n_boot: int, seed: int
) -> dict[str, Any]:
    """Trial params, hence trial identity: any other bootstrap setting or membership is a new,
    counted trial (ADR 0003 implementation notes, "Trial identity").

    S3 params are exactly ADR 0003's (no extra key: existing trial ids are unchanged). Any other
    hypothesis adds `hypothesis` and, if set, `themes_hash` (A2: the members' theme mapping)."""
    params: dict[str, Any] = {
        "cell": cell_id, "signal": c.feature, "group": c.group, "horizon": c.horizon,
        "period": period, "features_version": features.FEATURES_VERSION,
        "expected_sign": c.sign, "recipe": SCREEN_RECIPE, "n_boot": int(n_boot),
        "mean_block": float(SCREEN_MEAN_BLOCK), "seed": int(seed),
        "members_hash": _members_hash(c.members)}
    if c.hypothesis != SCREEN_HYPOTHESIS:
        params["hypothesis"] = c.hypothesis
        if c.themes_hash is not None:
            params["themes_hash"] = c.themes_hash
    return params


def _members_hash(members: tuple[str, ...]) -> str:
    """First 16 hex digits of the sha256 of the cell's sorted tickers joined by ','."""
    return hashlib.sha256(",".join(sorted(members)).encode()).hexdigest()[:16]


def _theme_groups(assets: list[Asset]) -> tuple[dict[str, list[str]], list[str]]:
    """ADR 0004 group -> pinned tickers, and the pinned tickers absent from `assets` or without
    gkg_themes (the screen refuses if any)."""
    have = {a.ticker for a in assets if a.gkg_themes}
    groups = {g: list(screen_features.THEME_GROUP_MEMBERS[g]) for g in screen_features.GROUPS_A2}
    absent = [t for m in groups.values() for t in m if t not in have]
    return groups, absent


def _themes_hash(assets: Iterable[Asset]) -> str:
    """First 16 hex digits of the sha256 of 'TICKER:THEME;THEME' (themes sorted, unique) per
    asset, sorted and joined by ','. A changed theme mapping is a new trial."""
    items = sorted(f"{a.ticker}:{';'.join(sorted(set(a.gkg_themes)))}" for a in assets)
    return hashlib.sha256(",".join(items).encode()).hexdigest()[:16]


def _num(x: float | None) -> float | None:
    """JSON-safe number: NaN/inf -> None."""
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _fmt(x: float, width: int, digits: int) -> str:
    return f"{x:>{width}.{digits}f}" if math.isfinite(x) else f"{'nan':>{width}}"


# --- extreme-shock event study (ADR 0005) -----------------------------------------------------

# ADR 0005 "Nested optimization" and "Confirmation". Inclusive event-date windows; changing them
# is a new ADR. Discovery events are embargoed so that their outcome window ends by the last
# discovery day (`_embargoed_end`), confirmation events likewise by the last dev day.
EVENT_DISCOVERY = (date(2015, 3, 2), date(2019, 12, 31))
EVENT_CONFIRMATION = (date(2020, 1, 1), date(2025, 3, 31))
EVENT_NULL = "placebo_dates"  # ADR 0005 Amendment: discovery and confirmation null
EVENT_MIN_N_BOOT = 999
EVENT_BH_Q = 0.10
EVENT_HOLM_ALPHA = 0.05
EVENT_DECLUSTER_DAYS = 20
EVENT_INSUFFICIENT_NOTE = "insufficient events"
EVENT_HYPOTHESES = frozenset(c.hypothesis for c in event_signals.CELLS)
_OTHER_TRACK: dict[str, Track] = {"A": "B", "B": "A"}

# One configuration of the grid: (theta, W, h), as in event_signals.GRID.
EventConfig = tuple[float, int, int]


@dataclass(frozen=True)
class _EventEval:
    """One cell x configuration x period: calendar-time series and its statistics."""

    series: pd.Series
    events: pd.DataFrame  # the events dated in `event_dates`
    outcome: pd.DataFrame  # the outcome frame passed in (not copied; read inside the window)
    n_events: int
    n_event_days: int  # distinct event dates
    n_days: int  # active days: the calendar-time series' length
    n_no_outcome: int
    mean: float
    t: float
    z: float  # t in the registered direction
    event_dates: tuple[date, date]  # inclusive window the events were taken from

    @property
    def sufficient(self) -> bool:
        """ADR 0005 Amendment: >= 30 events and >= 30 distinct event dates."""
        return eventstudy.sufficient(self.n_events, self.n_event_days)


def event_study(
    cfg: Config, store: Store, track: Track, now: Clock, echo: Echo = print, n_boot: int = 2000,
) -> int:
    """ADR 0005 extreme-shock event study of one track: nested discovery, BH, confirmation.

    Shock scores (W = 60, 120) and outcomes (h = 1, 5, 10, 20) are built in memory from dev data
    only (every read ends at `cfg.holdout_start`, half-open) and never stored. For each of the
    track's cells (`event_signals.CELLS`) and each of the 32 configurations (theta, W, h):
    declustered events (`eventstudy.triggers`, 20 trading days, per ticker on the cell's
    trigger signal; E4/E4p split the declustered attention events by the same-day tone z), the
    calendar-time series of the discovery period and its NW t. Every cell x configuration is logged
    as a `signal` trial (hypothesis = the E-id); a configuration with fewer than 30 events or
    fewer than 30 distinct event dates (ADR 0005 Amendment) is logged with the note
    "insufficient events" and cannot be selected. Per cell the selected configuration is the
    largest directional t among the sufficient ones, and the cell's p is the placebo-date max-t
    p over them (`eventstudy.max_t_placebo`, ADR 0005 Amendment: n_boot draws, seed
    `cfg.seed`, one permutation of the discovery window's trading days per draw shared by
    every configuration); p = NaN when none is sufficient.

    Benjamini-Hochberg at q = 0.10 runs across ALL 47 cells (ADR 0005), so the other track's
    cell p-values are read from its logged discovery trials with the same settings; until that
    track has been run, this run stops after logging discovery. Discovered cells of this track
    are then confirmed once on 2020-01-01..2025-03-31 with their selected configuration
    (one-sided placebo-date p, `eventstudy.placebo_p`, n_boot draws, seed `cfg.seed`; Holm at
    5% across every discovered cell of both tracks, the other track's confirmation p-values
    read from its logged trials; net-of-cost mean in the registered direction for return
    cells, none for volatility cells), each logged as a trial. When both tracks have
    discovered cells, the first to confirm logs its confirmations with Holm pending; the
    second completes Holm and its notes' `holm_verdicts` carry the final verdict of every
    discovered cell of both tracks (`event_verdicts` resolves them). Re-running with the same
    settings logs nothing new. REFUSED if n_boot < 999 or a
    period reaches the hold-out; MISSING without grouped assets, prices or GDELT rows.
    """
    if n_boot < EVENT_MIN_N_BOOT:
        echo(f"refused: n_boot {n_boot} < {EVENT_MIN_N_BOOT} (ADR 0005)")
        return REFUSED
    if EVENT_CONFIRMATION[1] >= cfg.holdout_start or EVENT_DISCOVERY[0] < cfg.start:
        echo(f"refused: event-study periods {EVENT_DISCOVERY[0]}..{EVENT_CONFIRMATION[1]} are"
             f" not inside the dev period {cfg.start}..{cfg.holdout_start} (ADR 0001)")
        return REFUSED
    assets = store.read_assets(track)
    if not assets:
        echo(f"no track {track} assets: run `load-universe --track {track}`")
        return MISSING
    groups = event_signals.outcome_groups(assets, track)
    if not groups:
        echo(f"no track {track} asset has a screen_group: run `load-universe --track {track}`"
             " with the current universe code (ADR 0005 uses the ADR 0003 groups)")
        return MISSING
    cells = [c for c in event_signals.CELLS if c.track == track]
    grouped = [a for a in assets if a.ticker in groups]
    tickers = sorted(groups)
    prices = store.read_prices(tickers, cfg.start, cfg.holdout_start)
    if prices.empty:
        echo(f"no prices for track {track}: run `ingest-prices --track {track}`")
        return MISSING
    tdays = _trading_days(prices)
    daily = _read_event_gdelt(store, track, grouped, cfg.start, cfg.holdout_start)
    if daily.empty:
        echo(f"no GDELT rows for track {track}: run `ingest-gdelt`")
        return MISSING
    hold = pd.Timestamp(cfg.holdout_start)
    scores = {w: _before(event_signals.shock_scores(daily, tdays, grouped, track, w), hold)
              for w in event_signals.WINDOWS}
    outcome_of = _event_outcomes(prices, groups, tdays, {c.outcome for c in cells}, hold)
    members = {c.id: _event_members(c, groups) for c in cells}
    sliced: dict[tuple[str, str, int, tuple[date, date]], pd.DataFrame] = {}

    def outcome_for(c: event_signals.EventCell, h: int, period: tuple[date, date]) -> pd.DataFrame:
        """The cell's outcome frame restricted to its members and the period's event dates
        (cached: calendar_time_series' cost grows with the frame it merges against)."""
        key = (c.group, c.outcome, h, period)
        if key not in sliced:
            o = outcome_of[(c.outcome, h)]
            lo, hi = pd.Timestamp(period[0]), pd.Timestamp(_embargoed_end(tdays, period[1], h))
            sliced[key] = o[o["ticker"].isin(members[c.id]) & (o["date"] >= lo)
                            & (o["date"] <= hi)].reset_index(drop=True)
        return sliced[key]

    echo(f"track {track}: {len(tickers)} grouped assets, {len(tdays)} trading days,"
         f" {len(cells)} cells x {len(event_signals.GRID)} configurations,"
         f" {sum(len(s) for s in scores.values()):,} shock-score rows")

    settings = _event_settings(n_boot, cfg.seed)
    logged = {t.trial_id for t in store.read_trials(track, "signal")}
    n_logged_before = len(logged)
    nan = float("nan")

    # Discovery: every cell x configuration, then the search-corrected p per cell.
    events: dict[str, dict[tuple[int, float], pd.DataFrame]] = {}
    disc: dict[str, dict[EventConfig, _EventEval]] = {}
    p_cell: dict[str, float] = {}
    selected: dict[str, EventConfig | None] = {}
    for c in cells:
        events[c.id] = {}
        for w in event_signals.WINDOWS:
            base = event_signals.cell_scores(scores[w], dataclasses.replace(c, split=None))
            base = base[base["ticker"].isin(members[c.id])]
            for th in event_signals.THETAS:
                events[c.id][(w, th)] = _cell_events(base, scores[w], c, th, tdays)
        disc[c.id] = {g: _event_eval(events[c.id][(g[1], g[0])],
                                     outcome_for(c, g[2], EVENT_DISCOVERY), g[2], c.sign,
                                     EVENT_DISCOVERY, tdays)
                      for g in event_signals.GRID}
        res = _event_search(c.id, disc[c.id], c.sign, tdays, n_boot, cfg.seed)
        p_cell[c.id] = nan if res is None else float(res.p)
        selected[c.id] = None if res is None else res.best_config  # type: ignore[assignment]
        search = None if res is None else {
            "p": _num(res.p), "max_z": _num(res.max_z),
            "best_config": None if res.best_config is None else _cfg_label(res.best_config),
            "n_sufficient": len(res.t), "n_dates": res.n_dates, "test": "placebo_dates_max_t",
            "n_draws": res.n_draws, "null_max_mean": _num(res.null_max_mean)}
        for g, e in disc[c.id].items():
            note = {**_eval_note(e), "outcome": c.outcome, "trigger": c.trigger,
                    "split": c.split, "selected": g == selected[c.id], "cell_p": _num(p_cell[c.id]),
                    "search": search, "min_events": eventstudy.MIN_EVENTS,
                    "min_event_days": eventstudy.MIN_EVENT_DAYS,
                    "decluster_days": EVENT_DECLUSTER_DAYS}
            _log_trial(store, logged, track, "signal", c.hypothesis,
                       _event_params(c, g, "discovery", members[c.id], settings),
                       *EVENT_DISCOVERY, now, note)

    # Benjamini-Hochberg across all 47 cells: the other track's p-values come from its log.
    other = _OTHER_TRACK[track]
    other_ids = [c.id for c in event_signals.CELLS if c.track == other]
    other_p = _logged_event_notes(store, other, settings, "discovery", "cell_p")
    absent = [k for k in other_ids if k not in other_p]
    discovered: set[str] | None = None
    if absent:
        echo(f"Benjamini-Hochberg across the {len(event_signals.CELLS)} ADR 0005 cells needs"
             f" track {other}'s discovery p-values with the same settings ({len(absent)} of"
             f" {len(other_ids)} cells not logged): run `event-study --track {other}"
             f" --n-boot {n_boot}`, then rerun this track to confirm. Stopping after discovery.")
    else:
        all_p = {**p_cell, **{k: other_p[k] for k in other_ids}}
        discovered = screen_stats.benjamini_hochberg(all_p, EVENT_BH_Q)
        mine = discovered & set(p_cell)
        echo(f"Benjamini-Hochberg q = {EVENT_BH_Q} across {len(all_p)} cells:"
             f" {len(discovered)} discovered ({len(mine)} in track {track})")

    # Confirmation: discovered cells of this track, selected configuration, once each.
    conf: dict[str, _EventEval] = {}
    conf_p: dict[str, float] = {}
    conf_test: dict[str, dict[str, Any]] = {}
    if discovered:
        for c in cells:
            g = selected[c.id]
            if c.id not in discovered or g is None:
                continue
            oc = outcome_for(c, g[2], EVENT_CONFIRMATION)
            ev = events[c.id][(g[1], g[0])]
            e = _event_eval(ev, oc, g[2], c.sign, EVENT_CONFIRMATION, tdays)
            conf[c.id] = e
            if e.sufficient:
                pr = _event_confirm_test(e, ev, oc, g[2], c.sign, tdays, n_boot, cfg.seed)
                conf_p[c.id] = float(pr.p)
                conf_test[c.id] = pr.notes()
            else:
                conf_p[c.id] = nan
                conf_test[c.id] = {"test": "placebo_dates", "p": None}
        other_trials = _logged_event_trials(store, other, settings, "confirmation")
        other_conf = {k: _note_float(n, "p_one_sided") for k, (_, n) in other_trials.items()}
        other_disc = discovered - set(p_cell)
        holm = _event_holm(conf_p, other_disc, other_conf)
        cost_bps = cfg.cost_bps_etf if track == "A" else cfg.cost_bps_stock
        own: dict[str, tuple[str, float, float]] = {}  # cell -> (trial id, directional, net)
        for c in cells:
            if c.id not in conf:
                continue
            g = selected[c.id]
            assert g is not None
            e = conf[c.id]
            legs = _event_legs(c.outcome)
            directional = c.sign * e.mean
            net = eventstudy.net_mean(directional, legs, cost_bps) if math.isfinite(e.mean) else nan
            params = _event_params(c, g, "confirmation", members[c.id], settings)
            own[c.id] = (trial_id("signal", track, params, *EVENT_CONFIRMATION), directional, net)
        # With Holm complete, the note also records the verdict of every discovered cell of
        # BOTH tracks: the other track's confirmations were logged while Holm was pending, and
        # trials are append-only, so this is where their final verdict is written
        # (`event_verdicts` reads it back).
        verdicts = None if holm is None else _event_holm_verdicts(
            holm, conf_p, own, {k: other_trials[k] for k in other_disc})
        for c in cells:
            if c.id not in conf:
                continue
            g = selected[c.id]
            assert g is not None
            e = conf[c.id]
            _, directional, net = own[c.id]
            legs = _event_legs(c.outcome)
            reject = None if holm is None else c.id in holm
            note = {**_eval_note(e), "p_one_sided": _num(conf_p[c.id]),
                    "conf_test": conf_test[c.id], "holm_reject": reject,
                    "holm_alpha": EVENT_HOLM_ALPHA, "holm_complete": holm is not None,
                    "holm_verdicts": verdicts,
                    "directional_mean": _num(directional), "legs": legs,
                    "cost_bps": cost_bps if legs else 0.0, "net_mean": _num(net),
                    "confirmed": _event_confirmed(reject, directional, net),
                    "discovery_trial": trial_id("signal", track, _event_params(
                        c, g, "discovery", members[c.id], settings), *EVENT_DISCOVERY),
                    "discovery_p": _num(p_cell[c.id]), "bh_q": EVENT_BH_Q,
                    "outcome": c.outcome, "min_events": eventstudy.MIN_EVENTS,
                    "min_event_days": eventstudy.MIN_EVENT_DAYS}
            _log_trial(store, logged, track, "signal", c.hypothesis,
                       _event_params(c, g, "confirmation", members[c.id], settings),
                       *EVENT_CONFIRMATION, now, note)
        if holm is None:
            echo(f"Holm across discovered cells is pending: track {other} has discovered cells"
                 f" without a logged confirmation; run `event-study --track {other}"
                 f" --n-boot {n_boot}`: its confirmation notes will carry this track's verdicts")
    else:
        holm = None

    _event_table(echo, track, cells, disc, p_cell, selected, discovered, conf, conf_p, holm,
                 cfg, n_boot)
    n_new = len(logged) - n_logged_before
    n_disc = 0 if discovered is None else len(discovered & set(p_cell))
    echo(f"track {track}: {len(cells)} cells, {n_disc} discovered, {len(conf)} confirmation"
         f" test(s); {n_new} new trial(s) logged"
         f" ({len(cells) * len(event_signals.GRID) + len(conf) - n_new} already logged)")
    return OK


def _event_table(
    echo: Echo, track: Track, cells: list[event_signals.EventCell],
    disc: dict[str, dict[EventConfig, _EventEval]], p_cell: dict[str, float],
    selected: dict[str, EventConfig | None], discovered: set[str] | None,
    conf: dict[str, _EventEval], conf_p: dict[str, float], holm: set[str] | None, cfg: Config,
    n_boot: int,
) -> None:
    echo(f"suff: configurations with >= {eventstudy.MIN_EVENTS} discovery events and >="
         f" {eventstudy.MIN_EVENT_DAYS} event dates (others '{EVENT_INSUFFICIENT_NOTE}');"
         f" config theta/W/h selected by the largest t in the registered direction; n, mean,"
         f" t: discovery events, mean outcome (raw sign) and NW t of the selected config;"
         f" p: placebo-date max-t ({n_boot} draws, seed {cfg.seed});"
         f" disc: BH q = {EVENT_BH_Q} over 47 cells;"
         f" conf_p one-sided placebo-date p, Holm {EVENT_HOLM_ALPHA} across discovered cells")
    echo(f"{'cell':<26}{'sign':>5}{'suff':>6}  {'config':<12}{'n':>6}{'mean':>10}{'t':>7}"
         f"{'p':>8}  {'disc':<8}{'conf_n':>7}{'conf_mean':>10}{'conf_t':>7}{'conf_p':>8}"
         f"  confirmed")
    cost_bps = cfg.cost_bps_etf if track == "A" else cfg.cost_bps_stock
    for c in cells:
        g = selected[c.id]
        n_suff = sum(e.sufficient for e in disc[c.id].values())
        e = disc[c.id][g] if g is not None else None
        head = (f"{c.id:<26}{c.sign:>+5.0f}{n_suff:>3}/{len(disc[c.id]):<2}  "
                f"{_cfg_label(g) if g else '-':<12}"
                f"{e.n_events if e else 0:>6}{_fmt(e.mean if e else math.nan, 10, 5)}"
                f"{_fmt(e.t if e else math.nan, 7, 2)}{_fmt(p_cell[c.id], 8, 4)}  ")
        if discovered is None:
            flag = "pending" if n_suff else "insuff"
        else:
            flag = "yes" if c.id in discovered else ("-" if n_suff else "insuff")
        tail = ""
        if c.id in conf:
            ce = conf[c.id]
            directional = c.sign * ce.mean
            net = (eventstudy.net_mean(directional, _event_legs(c.outcome), cost_bps)
                   if math.isfinite(ce.mean) else math.nan)
            reject = None if holm is None else c.id in holm
            verdict = _event_confirmed(reject, directional, net)
            tail = (f"{ce.n_events:>7}{_fmt(ce.mean, 10, 5)}{_fmt(ce.t, 7, 2)}"
                    f"{_fmt(conf_p[c.id], 8, 4)}  "
                    f"{'pending' if verdict is None else ('CONFIRMED' if verdict else 'no')}")
        echo(head + f"{flag:<8}" + tail)


def _read_event_gdelt(
    store: Store, track: Track, assets: list[Asset], start: date, end: date
) -> pd.DataFrame:
    """Events by the grouped ETFs' countries (A, incl. the tone rows), GKG by alias (B)."""
    if track == "A":
        ents = sorted({c for a in assets for c in a.fips_countries})
        return store.read_gdelt_daily("events", start, end, ents) if ents else pd.DataFrame()
    ents = sorted({x for a in assets for x in a.org_aliases})
    return store.read_gdelt_daily("gkg", start, end, ents) if ents else pd.DataFrame()


def _before(df: pd.DataFrame, end: pd.Timestamp) -> pd.DataFrame:
    return df[pd.to_datetime(df["date"]) < end].reset_index(drop=True)


def _event_outcomes(
    prices: pd.DataFrame, groups: dict[str, str], tdays: list[date], kinds: set[str],
    end: pd.Timestamp,
) -> dict[tuple[str, int], pd.DataFrame]:
    """(outcome kind, h) -> [date, ticker, value], built per horizon to bound memory."""
    out: dict[tuple[str, int], pd.DataFrame] = {}
    for h in event_signals.HORIZONS:
        o = event_signals.outcomes(prices, groups, [h], tdays)
        o = o[o["date"] < end]
        for k in sorted(kinds):
            out[(k, h)] = o.loc[o["outcome"] == k, ["date", "ticker", "value"]].reset_index(
                drop=True)
    return out


def _event_members(cell: event_signals.EventCell, groups: Mapping[str, str]) -> tuple[str, ...]:
    """Tickers whose events a cell pools: its group (A), or every grouped stock (B 'all')."""
    if cell.group == event_signals.POOLED_B:
        return tuple(sorted(groups))
    return tuple(sorted(t for t, g in groups.items() if g == cell.group))


def _cell_events(
    base: pd.DataFrame, scores: pd.DataFrame, cell: event_signals.EventCell, theta: float,
    tdays: list[date],
) -> pd.DataFrame:
    """Declustered events [date, ticker] of a cell at theta.

    `base` is the cell's directional score WITHOUT the tone split (members only). Declustering
    is on the trigger signal of the (asset, signal) pair; a Track B split cell then keeps the
    declustered attention events whose same-day tone z is < 0 / >= 0 (ADR 0005: "the event is
    split"). So E4 and E4p partition E5's events (less those with undefined tone), and an
    attention spike blocks the next 20 days whatever its tone.
    """
    ev = eventstudy.triggers(base, theta, 1, EVENT_DECLUSTER_DAYS, tdays)
    if cell.split is None:
        return ev
    keep = event_signals.cell_scores(scores, cell)[["date", "ticker"]]
    keep = keep.assign(date=pd.to_datetime(keep["date"]))
    return ev.merge(keep, on=["date", "ticker"], how="inner").sort_values(
        ["date", "ticker"], ignore_index=True)


def _event_eval(
    events: pd.DataFrame, outcome: pd.DataFrame, h: int, sign: float,
    period: tuple[date, date], tdays: list[date],
) -> _EventEval:
    """Calendar-time series and statistics of the events dated in `period` (embargoed for h)."""
    lo = pd.Timestamp(period[0])
    hi = pd.Timestamp(_embargoed_end(tdays, period[1], h))
    ev = _in_window(events, lo, hi)
    oc = _in_window(outcome, lo, hi)
    s = eventstudy.calendar_time_series(ev, oc, h, tdays)
    attrs = dict(s.attrs)
    mean, t, z, _ = eventstudy.cell_stat(s, h, sign)
    return _EventEval(series=s, events=ev, outcome=outcome, n_events=int(attrs["n_events"]),
                      n_event_days=int(attrs["n_event_days"]), n_days=int(attrs["n_days"]),
                      n_no_outcome=int(attrs["n_events_no_outcome"]), mean=mean, t=t, z=z,
                      event_dates=(lo.date(), hi.date()))


def _in_window(df: pd.DataFrame, lo: pd.Timestamp, hi: pd.Timestamp) -> pd.DataFrame:
    return df[(df["date"] >= lo) & (df["date"] <= hi)]


def _event_confirm_test(
    e: _EventEval, events: pd.DataFrame, outcome: pd.DataFrame, h: int, sign: float,
    tdays: list[date], n_draws: int, seed: int,
) -> eventstudy.PlaceboResult:
    """One-sided placebo-date p of a confirmation (`eventstudy.placebo_p`): the events and the
    candidate placebo dates are those of `e`'s embargoed event window (distinct, not spaced)."""
    lo, hi = pd.Timestamp(e.event_dates[0]), pd.Timestamp(e.event_dates[1])
    return eventstudy.placebo_p(_in_window(events, lo, hi), _in_window(outcome, lo, hi), h, sign,
                                tdays, n_draws, seed)


def _event_search(
    cell_id: str, evals: Mapping[EventConfig, _EventEval], sign: float, tdays: list[date],
    n_boot: int, seed: int,
) -> eventstudy.MaxTPlaceboResult | None:
    """Placebo-date max-t over the cell's sufficient configurations (None if there are none).

    ADR 0005 Amendment: each configuration's events, outcome and embargoed event window are
    those of its discovery evaluation; the placebo window is their union (the h = 1 window).
    `cell_id` is unused by the statistic; it names the cell for logging and test doubles.
    """
    suff = {g: e for g, e in evals.items() if e.sufficient}
    if not suff:
        return None
    return eventstudy.max_t_placebo(
        {g: e.events for g, e in suff.items()}, {g: e.outcome for g, e in suff.items()},
        {g: g[2] for g in suff}, {g: e.event_dates for g, e in suff.items()}, sign, tdays,
        n_boot, seed)


def _eval_note(e: _EventEval) -> dict[str, Any]:
    return {"n_events": e.n_events, "n_event_days": e.n_event_days, "n_days": e.n_days,
            "n_events_no_outcome": e.n_no_outcome, "mean": _num(e.mean), "t": _num(e.t),
            "z": _num(e.z), "sufficient": e.sufficient,
            "note": None if e.sufficient else EVENT_INSUFFICIENT_NOTE,
            "event_dates": [e.event_dates[0].isoformat(), e.event_dates[1].isoformat()]}


def _event_params(
    cell: event_signals.EventCell, g: EventConfig, period: str, members: tuple[str, ...],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Trial params, hence identity: any other n_boot, null, seed or membership is a new trial."""
    return {"cell": cell.id, "theta": float(g[0]), "window": int(g[1]), "horizon": int(g[2]),
            "period": period, "features_version": settings["features_version"],
            "expected_sign": float(cell.sign), "n_boot": settings["n_boot"],
            "null": settings["null"], "seed": settings["seed"],
            "members_hash": _members_hash(members)}


def _event_settings(n_boot: int, seed: int) -> dict[str, Any]:
    """Settings in every event-study trial's params; logged trials are matched on them. `null`
    (ADR 0005 Amendment) keeps these ids apart from any trial of the bootstrap design."""
    return {"n_boot": int(n_boot), "null": EVENT_NULL, "seed": int(seed),
            "features_version": features.FEATURES_VERSION}


def _logged_event_notes(
    store: Store, track: Track, settings: Mapping[str, Any], period: str, key: str,
) -> dict[str, float]:
    """Cell id -> notes[key] (NaN for null) of a track's logged event-study trials of `period`
    with the same n_boot, null, seed and features version. When a cell was logged under
    several memberships, the most recently logged trial wins."""
    return {k: _note_float(n, key)
            for k, (_, n) in _logged_event_trials(store, track, settings, period).items()}


def _logged_event_trials(
    store: Store, track: Track, settings: Mapping[str, Any], period: str,
) -> dict[str, tuple[Trial, dict[str, Any]]]:
    """Cell id -> (trial, parsed notes) of a track's logged event-study trials of `period` with
    the same settings; the most recently logged trial of a cell wins."""
    out: dict[str, tuple[Trial, dict[str, Any]]] = {}
    for t in store.read_trials(track, "signal"):  # insertion-time order
        p = t.params
        if (t.hypothesis not in EVENT_HYPOTHESES or "theta" not in p
                or p.get("period") != period
                or any(p.get(k) != v for k, v in settings.items())):
            continue
        try:
            note = json.loads(t.notes)
        except (TypeError, ValueError):
            continue
        if isinstance(note, dict):
            out[str(p["cell"])] = (t, note)
    return out


def _note_float(note: Mapping[str, Any], key: str) -> float:
    v = note.get(key)
    return float("nan") if v is None else float(v)


def _event_holm_verdicts(
    holm: set[str], conf_p: Mapping[str, float], own: Mapping[str, tuple[str, float, float]],
    other: Mapping[str, tuple[Trial, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Final verdict of every discovered cell of both tracks once Holm is complete.

    `own`: cell -> (confirmation trial id, directional mean, net mean) of this run's cells;
    `other`: the other track's discovered cells' logged confirmation (trial, notes).
    """
    out: dict[str, dict[str, Any]] = {}
    for k, (tid, directional, net) in own.items():
        out[k] = {"trial_id": tid, "p": _num(conf_p[k]), "holm_reject": k in holm,
                  "confirmed": _event_confirmed(k in holm, directional, net)}
    for k, (t, note) in other.items():
        directional = _note_float(note, "directional_mean")
        net = _note_float(note, "net_mean")
        out[k] = {"trial_id": t.trial_id, "p": note.get("p_one_sided"), "holm_reject": k in holm,
                  "confirmed": _event_confirmed(k in holm, directional, net)}
    return dict(sorted(out.items()))


def event_verdicts(store: Store, n_boot: int, seed: int) -> dict[str, bool | None]:
    """Cell id -> final confirmation verdict of every logged ADR 0005 confirmation (both
    tracks, current features version, the given n_boot and seed; placebo-date null).

    A confirmation logged while Holm was pending has `confirmed` None in its own notes (trials
    are append-only); its verdict is then read from the `holm_verdicts` of the other track's
    confirmation that completed Holm. None: Holm has not completed yet.
    """
    settings = _event_settings(n_boot, seed)
    logged = {tr: _logged_event_trials(store, tr, settings, "confirmation")
              for tr in ("A", "B")}
    out: dict[str, bool | None] = {}
    for tr, trials in logged.items():
        for k, (t, note) in trials.items():
            verdict = note.get("confirmed")
            if verdict is None:
                for _, other_note in logged[_OTHER_TRACK[tr]].values():
                    v = (other_note.get("holm_verdicts") or {}).get(k)
                    if v is not None and v.get("trial_id") == t.trial_id:
                        verdict = v.get("confirmed")
            out[k] = None if verdict is None else bool(verdict)
    return out


def _event_holm(
    conf_p: Mapping[str, float], other_discovered: set[str], other_conf: Mapping[str, float],
) -> set[str] | None:
    """Holm rejections across every discovered cell (both tracks), or None while a discovered
    cell of the other track has no logged confirmation p. NaN p-values count in m."""
    if any(k not in other_conf for k in other_discovered):
        return None
    pvals = {**dict(conf_p), **{k: other_conf[k] for k in other_discovered}}
    return screen_stats.holm(pvals, EVENT_HOLM_ALPHA) if pvals else set()


def _event_confirmed(reject: bool | None, directional_mean: float, net: float) -> bool | None:
    """Confirmed: Holm rejects, the mean is in the registered direction and positive net of
    costs (volatility cells: no cost leg, so net == directional mean). None while Holm waits."""
    if reject is None:
        return None
    return bool(reject and math.isfinite(net) and directional_mean > 0 and net > 0)


def _event_legs(outcome: str) -> int:
    """ADR 0005 cost legs: abnormal return 4, raw return 2, volatility none."""
    return {"abnormal_return": eventstudy.LEGS_ABNORMAL,
            "raw_return": eventstudy.LEGS_RAW}.get(outcome, 0)


def _cfg_label(g: EventConfig | Any) -> str:
    theta, w, h = g
    return f"{float(theta):g}/{int(w)}/{int(h)}"


# --- pre-registered follow-up F1 (ADR 0006) ----------------------------------------------------

# Every setting is fixed by ADR 0006; changing any of them is a new ADR (and a new trial id).
F1_HYPOTHESIS = "F1"
F1_GROUP = "em"
F1_SIGNALS = ("quad3", "quad4", "root_14", "violence", "tone")
F1_THETA = 3.0
F1_W = 60
F1_H = 20
F1_OUTCOME = "abnormal_vol"
F1_SIGN = 1.0
F1_ALPHA = 0.05
F1_WINDOW = (date(2020, 1, 1), date(2025, 3, 31))  # inclusive event dates, embargoed for h
F1_N_BOOT = 2000  # ADR 0006: fixed, not a CLI option (another n_boot would be another draw)
FOLLOWUP_IDS = (F1_HYPOTHESIS,)  # `followup --id` choices


def followup_f1(cfg: Config, store: Store, now: Clock, echo: Echo = print) -> int:
    """ADR 0006 follow-up F1: EM conflict shocks -> 20-day abnormal volatility, one test.

    EM ETFs (`screen_group` 'em') fire on the composite event (`event_signals.composite_events`:
    quad3, quad4, root_14 or violence share z >= 3, or tone z <= -3, W = 60; one 20-day
    declustering per ETF over the union, computed over the whole dev period as in ADR 0005).
    Events dated in F1_WINDOW, embargoed so their outcome ends by its last day (`_event_eval`),
    give the calendar-time mean and NW t of ADR 0005 abnormal volatility at h = 20. With >= 30
    events and >= 30 event dates the one-sided (+) placebo-date p (`eventstudy.placebo_p`,
    F1_N_BOOT = 2,000 draws, seed `cfg.seed`; not a parameter) decides: "pass" iff p < 0.05 and
    mean > 0, else "fail"; otherwise "inconclusive" and no p. Exactly one `signal` trial
    (hypothesis F1) is ever logged: if ANY trial with hypothesis F1 exists, whatever its params
    (n_boot, seed, features version, members), nothing is read or computed or logged and the
    stored verdict is printed. REFUSED if F1_N_BOOT < 999 or the window reaches the hold-out;
    MISSING without EM assets, prices or GDELT rows. Every read ends at `cfg.holdout_start`
    (half-open).
    """
    n_boot = F1_N_BOOT
    if n_boot < EVENT_MIN_N_BOOT:
        echo(f"refused: n_boot {n_boot} < {EVENT_MIN_N_BOOT} (ADR 0006)")
        return REFUSED
    if F1_WINDOW[1] >= cfg.holdout_start or F1_WINDOW[0] < cfg.start:
        echo(f"refused: F1 window {F1_WINDOW[0]}..{F1_WINDOW[1]} is not inside the dev period"
             f" {cfg.start}..{cfg.holdout_start} (the hold-out is not used; ADR 0001, 0006)")
        return REFUSED
    # Once-only guard on the hypothesis, not the trial id: the id depends on n_boot, seed,
    # features version and members, so matching it would let another setting re-draw the test.
    prior = [t for t in store.read_trials() if t.hypothesis == F1_HYPOTHESIS]
    if prior:
        stored = prior[0]
        note = json.loads(stored.notes)
        sp = stored.params
        echo(f"F1: {note.get('verdict')} (already logged as {stored.trial_id}; nothing"
             " recomputed; ADR 0006 allows one F1 trial)")
        _f1_report(echo, note, sp.get("n_boot", "?"), sp.get("seed", "?"))
        if len(prior) > 1:
            echo(f"  warning: {len(prior)} F1 trials are logged; the first one"
                 f" ({stored.trial_id}) is the test")
        echo("0 new trials logged")
        return OK
    assets = store.read_assets("A")
    groups = {t: g for t, g in event_signals.outcome_groups(assets, "A").items()
              if g == F1_GROUP}
    if not groups:
        echo(f"no Track A asset with screen_group '{F1_GROUP}': run `load-universe --track A`")
        return MISSING
    members = tuple(sorted(groups))
    params = _f1_params(members, n_boot, cfg.seed)
    tid = trial_id("signal", "A", params, *F1_WINDOW)

    em_assets = [a for a in assets if a.ticker in groups]
    prices = store.read_prices(list(members), cfg.start, cfg.holdout_start)
    if prices.empty:
        echo("no prices for the EM ETFs: run `ingest-prices --track A`")
        return MISSING
    tdays = _trading_days(prices)
    daily = _read_event_gdelt(store, "A", em_assets, cfg.start, cfg.holdout_start)
    if daily.empty:
        echo("no GDELT event rows for the EM ETFs' countries: run `ingest-gdelt events`")
        return MISSING
    hold = pd.Timestamp(cfg.holdout_start)
    scores = _before(event_signals.shock_scores(daily, tdays, em_assets, "A", F1_W), hold)
    events = event_signals.composite_events(scores, list(members), F1_SIGNALS, F1_THETA,
                                            EVENT_DECLUSTER_DAYS, tdays)
    oc = event_signals.outcomes(prices, groups, [F1_H], tdays)
    oc = oc.loc[(oc["outcome"] == F1_OUTCOME) & (oc["date"] < hold),
                ["date", "ticker", "value"]].reset_index(drop=True)
    e = _event_eval(events, oc, F1_H, F1_SIGN, F1_WINDOW, tdays)
    p = math.nan
    conf_test: dict[str, Any] = {"test": EVENT_NULL, "p": None}
    if e.sufficient:
        pr = _event_confirm_test(e, events, oc, F1_H, F1_SIGN, tdays, n_boot, cfg.seed)
        p = float(pr.p)
        conf_test = pr.notes()
    verdict = _f1_verdict(e.sufficient, p, e.mean)
    note = {**_eval_note(e), "verdict": verdict, "p_one_sided": _num(p),
            "conf_test": conf_test, "alpha": F1_ALPHA, "outcome": F1_OUTCOME,
            "min_events": eventstudy.MIN_EVENTS, "min_event_days": eventstudy.MIN_EVENT_DAYS,
            "n_members": len(members), "adr": "0006"}
    logged = {t.trial_id for t in store.read_trials("A", "signal")}
    _log_trial(store, logged, "A", "signal", F1_HYPOTHESIS, params, *F1_WINDOW, now, note)
    echo(f"F1: {verdict} (trial {tid})")
    _f1_report(echo, note, n_boot, cfg.seed)
    echo("1 new trial logged")
    return OK


def _f1_params(members: tuple[str, ...], n_boot: int, seed: int) -> dict[str, Any]:
    """Trial params, hence identity: every fixed ADR 0006 setting plus n_boot, null, seed,
    features version and the members hash."""
    return {"hypothesis": F1_HYPOTHESIS, "group": F1_GROUP, "signals": list(F1_SIGNALS),
            "theta": F1_THETA, "window": F1_W, "horizon": F1_H,
            "decluster_days": EVENT_DECLUSTER_DAYS, "outcome": F1_OUTCOME,
            "expected_sign": F1_SIGN, "period": "confirmation", "alpha": F1_ALPHA,
            "min_events": eventstudy.MIN_EVENTS, "min_event_days": eventstudy.MIN_EVENT_DAYS,
            **_event_settings(n_boot, seed), "members_hash": _members_hash(members)}


def _f1_verdict(sufficient: bool, p: float, mean: float) -> str:
    """ADR 0006: 'inconclusive' below 30 events / 30 event dates; 'pass' iff p < 0.05 and
    mean > 0; otherwise 'fail' (an undefined p or mean fails)."""
    if not sufficient:
        return "inconclusive"
    ok = math.isfinite(p) and math.isfinite(mean) and p < F1_ALPHA and mean > 0
    return "pass" if ok else "fail"


def _f1_report(echo: Echo, note: Mapping[str, Any], n_boot: object, seed: object) -> None:
    def val(k: str) -> float:
        return _note_float(note, k)

    echo(f"  EM ETFs {note.get('n_members')}, signals {'/'.join(F1_SIGNALS)},"
         f" theta {F1_THETA:g}, W {F1_W}, h {F1_H}, events dated"
         f" {note['event_dates'][0]}..{note['event_dates'][1]}")
    echo(f"  events {note['n_events']} on {note['n_event_days']} dates"
         f" (need {eventstudy.MIN_EVENTS} and {eventstudy.MIN_EVENT_DAYS}),"
         f" mean abnormal vol {_fmt(val('mean'), 0, 4).strip()}, NW t {_fmt(val('t'), 0, 2).strip()},"
         f" one-sided placebo-date p {_fmt(val('p_one_sided'), 0, 4).strip()}"
         f" ({n_boot} draws, seed {seed}); pass iff p < {F1_ALPHA} and mean > 0")


# --- backtest ---------------------------------------------------------------------------------

def backtest(
    cfg: Config, store: Store, track: Track, now: Clock, feature: str | None = None,
    sign: float | None = None, model: Literal["ridge", "logistic", "lgbm"] | None = None,
    model_features: list[str] | None = None, horizon: int = 5, rebalance: int | None = None,
    echo: Echo = print,
) -> int:
    """Backtest one rule feature or one walk-forward model over the dev period; log a 'backtest' trial."""
    if (feature is None) == (model is None):
        echo("pass exactly one of --feature or --model")
        return REFUSED
    rebalance = rebalance or horizon
    assets = store.read_assets(track)
    tickers = {a.ticker for a in assets}
    prices = store.read_prices(sorted(tickers), cfg.start, cfg.holdout_start)
    if prices.empty:
        echo(f"no prices for track {track}")
        return MISSING
    if feature is not None:
        hyp, reg_sign = hypothesis_of(track, feature)
        sign = reg_sign if sign is None else sign
        if sign is None:
            # H0 (and unknown names) have no pre-registered direction; taking it from the
            # dev-period IC would be a choice made after seeing the data (ADR 0002 item 3).
            echo(f"{feature!r} ({hyp}) has no pre-registered sign: pass --sign +1 or -1")
            return REFUSED
        fs = _for_tickers(store.read_features(cfg.start, cfg.holdout_start, [feature]), tickers)
        if fs.empty:
            echo(f"feature {feature!r} not stored for track {track}")
            return MISSING
        scores = _rule_scores(fs, feature, sign)
        params: dict[str, Any] = {"kind": "rule", "feature": feature, "sign": sign}
        notes: dict[str, Any] = {}
    else:
        # Default inputs: every stored feature of the track, filtered per fold by the gate-1 IC
        # test on that fold's training dates. Taking the features that passed signal-study
        # instead would choose inputs with IC measured over every test block (look-ahead).
        select = model_features is None
        fs = _for_tickers(store.read_features(cfg.start, cfg.holdout_start, model_features),
                          tickers)
        labs = _for_tickers(store.read_labels(cfg.start, cfg.holdout_start, [horizon]), tickers)
        if fs.empty or labs.empty:
            echo("features or labels missing: run `build-dataset`")
            return MISSING
        names = sorted(str(n) for n in fs["feature"].unique())
        if not select:
            echo("explicit --features: if they were chosen from dev-period signal-study results,"
                 " this trial is not out-of-sample")
        scores, chosen = _model_scores(fs, labs, model, horizon, cfg.seed, select)
        if select:
            n_used = sum(1 for c in chosen if c)
            echo(f"per-fold feature selection: {n_used} of {len(chosen)} fold(s) had a feature"
                 " passing the gate-1 IC test on their training dates")
        hyp = "+".join(sorted({hypothesis_of(track, n)[0] for n in names}))
        params = {"kind": "model", "model": model, "features": names,
                  "feature_selection": "per_fold_train_ic" if select else "user",
                  "model_params": _DEFAULT_MODEL_PARAMS[model], "cv": "walk_forward",
                  "train_days": _WF_TRAIN_DAYS, "test_days": _WF_TEST_DAYS}
        notes = {"features_per_fold": chosen}
    # ADR 0002 item 5: the features version is part of the trial id, so a backtest over v2
    # features is a new row next to the same backtest over v1 features.
    params |= {"horizon": horizon, "rebalance": rebalance, "weights": "rank_long_short",
               "features_version": features.FEATURES_VERSION}
    weights = _weights(scores, prices, rebalance)
    if weights.empty:
        echo("no tradable weight dates")
        return MISSING
    cost = cfg.cost_bps_etf if track == "A" else cfg.cost_bps_stock
    ret, stats = _simulate(weights, prices, cost, cfg.borrow_bps_annual)
    d0, d1 = ret["date"].min().date(), ret["date"].max().date()
    tid = trial_id("backtest", track, params, d0, d1)
    echo(f"trial {tid} ({hyp}) {d0}..{d1}: sharpe {stats.sharpe:.2f}, ann_return"
         f" {stats.ann_return:.2%}, max_dd {stats.max_drawdown:.2%}, turnover"
         f" {stats.turnover:.3f}, cost_drag {stats.cost_drag:.2%}")
    if any(t.trial_id == tid for t in store.read_trials(track, "backtest")):
        echo("identical trial already logged; not logged again")
        return OK
    trial = Trial(trial_id=tid, created_at=now(), track=track, stage="backtest", hypothesis=hyp,
                  params=params, data_start=d0, data_end=d1, stats=stats,
                  notes=json.dumps(notes, default=_json_default))
    store.add_trial(trial, ret.assign(trial_id=tid)[TRIAL_RETURN_COLUMNS])
    return OK


def _rule_scores(fs: pd.DataFrame, feature: str, sign: float) -> pd.DataFrame:
    wide = fs.pivot_table(index=["date", "ticker"], columns="feature", values="value",
                          aggfunc="first").reset_index()
    return models.rule_score(wide, feature, sign)


def _model_scores(
    fs: pd.DataFrame, labs: pd.DataFrame, model: str, horizon: int, seed: int, select: bool
) -> tuple[pd.DataFrame, list[list[str]]]:
    """Walk-forward OOS scores, and the features each fold used.

    Fits on labelled rows only, but scores every (date, ticker) with features on a test date: a
    label needs prices at t+1 and t+1+h, so requiring one would build day t's cross-section from
    future data availability. With `select`, each fold keeps the features that pass the gate-1
    IC test on its own training dates (a fold with none scores nothing).
    """
    X, y = models.to_design(fs, labs, horizon, keep_unlabelled=True)
    y = y - y.groupby(X["date"]).transform("mean")  # rank within a day, not market direction
    dates = sorted(X["date"].unique())
    splits = cv.walk_forward_splits(dates, _WF_TRAIN_DAYS, _WF_TEST_DAYS, horizon, 0, True)
    p = _DEFAULT_MODEL_PARAMS[model]
    base: Callable[[pd.DataFrame, pd.Series], Any]
    if model == "ridge":
        base = lambda X_, y_: models.fit_ridge(X_, y_, p["alpha"])
    elif model == "logistic":
        base = lambda X_, y_: models.fit_logistic(X_, y_, p["C"])
    else:
        base = lambda X_, y_: models.fit_lgbm(X_, y_, p, seed)
    ic = _ic_table(fs, labs, horizon) if select else None
    chosen: list[list[str]] = []

    def fit(X_: pd.DataFrame, y_: pd.Series) -> Any:
        names = [str(c) for c in X_.columns if c not in ("date", "ticker")]
        if ic is not None:
            names = models.select_features(ic, X_["date"].unique(), horizon)
        chosen.append(names)
        return base(X_[["date", "ticker", *names]], y_) if names else None

    def predict(m: Any, X_: pd.DataFrame) -> pd.DataFrame:
        if m is None:
            return pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]"),
                                 "ticker": pd.Series(dtype=object),
                                 "score": pd.Series(dtype=float)})
        return models.predict(m, X_)

    oos = models.oos_predictions(X, y, splits, fit, predict)
    return oos[SCORE_COLUMNS], chosen


def _ic_table(fs: pd.DataFrame, labs: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Daily rank IC per feature (date x feature); a date's IC uses only that date's labels."""
    cols = {str(n): evaluate.daily_ic(sub, labs, horizon) for n, sub in fs.groupby("feature")}
    return pd.DataFrame(cols).sort_index()


def _weights(scores: pd.DataFrame, prices: pd.DataFrame, rebalance: int) -> pd.DataFrame:
    """Rank long-short weights on every `rebalance`-th score date strictly before the last price date.

    Only names with a price bar on the score date itself are ranked (known at t, never a later bar).
    """
    last = prices["date"].max()
    bars = prices[["date", "ticker"]].drop_duplicates()
    scores = scores.merge(bars, on=["date", "ticker"], how="inner")
    dates = sorted(d for d in scores["date"].unique() if d < last)[::rebalance]
    sc = scores[scores["date"].isin(dates)]
    return models.scores_to_weights(sc, "rank_long_short")


# --- overfitting (gate 3) ---------------------------------------------------------------------

def overfit_report(cfg: Config, store: Store, track: Track, echo: Echo = print) -> int:
    """DSR deflated over every logged dev trial of a track (all stages), PBO and bootstrap CI.

    Returns exist only for backtest trials, so they form the matrix for V[SR], PSR and PBO; but
    N in E[max SR] counts every signal, model and backtest trial (ADR 0001 gate 3), because the
    signal stage chose what got backtested. Hold-out trials are not counted. Trials of every
    features version count (ADR 0002 item 5: the v1 trials were evaluated, so they stay in N).
    """
    trials = store.read_trials(track, "backtest")
    if not trials:
        echo(f"no backtest trials for track {track}")
        return MISSING
    n_all = sum(1 for t in store.read_trials(track) if t.stage != "holdout")
    wide = store.read_trial_returns([t.trial_id for t in trials])
    rep = overfit.overfit_report(wide, cfg.seed, n_trials=max(n_all, wide.shape[1]))
    passed = rep.deflated_sharpe >= 0.95 and rep.pbo is not None and rep.pbo < 0.5
    pbo = "n/a (too few trials)" if rep.pbo is None else f"{rep.pbo:.3f}"
    echo(f"track {track}: {len(trials)} backtest trials with returns; DSR deflates over"
         f" {rep.n_trials} logged trials (all stages)")
    echo(f"best trial {rep.best_trial_id}: annualised Sharpe {rep.best_sharpe:.2f},"
         f" 95% CI [{rep.sharpe_ci95[0]:.2f}, {rep.sharpe_ci95[1]:.2f}]")
    echo(f"deflated Sharpe {rep.deflated_sharpe:.3f} (need >= 0.95); PBO {pbo} (need < 0.5)")
    echo(f"gate 3: {'pass' if passed else 'fail'}")
    return OK


# --- hold-out (gate 4) ------------------------------------------------------------------------

def holdout(
    cfg: Config, store: Store, track: Track, source_trial: str, confirmed: bool,
    gdelt: GdeltSource | None, fetch: FetchPrices, execute: bool, now: Clock,
    budget_error: type[Exception] = RuntimeError, echo: Echo = print,
) -> int:
    """Evaluate one rule backtest trial ONCE on the hold-out; needs explicit confirmation."""
    if not confirmed:
        echo("refused: the hold-out is evaluated once per strategy (ADR 0001)."
             " Pass --i-understand-this-is-final to run it.")
        return REFUSED
    src = next((t for t in store.read_trials(track, "backtest") if t.trial_id == source_trial),
               None)
    if src is None:
        echo(f"no backtest trial {source_trial!r} in track {track}")
        return MISSING
    if src.params.get("kind") != "rule":
        echo("only rule backtests can be evaluated on the hold-out so far")
        return MISSING
    if any(t.params.get("source_trial") == source_trial
           for t in store.read_trials(track, "holdout")):
        echo(f"refused: {source_trial} was already evaluated on the hold-out")
        return REFUSED
    h0, h1 = cfg.holdout_start, (cfg.end or now().date() - timedelta(days=1))
    if h1 < h0:
        echo("hold-out window is empty")
        return MISSING
    feature = src.params["feature"]
    datasets = _datasets_for(feature)
    if datasets and gdelt is None:
        echo("no BigQuery client: cannot ingest hold-out GDELT")
        return MISSING
    if not execute:
        # Dry run: report only. Nothing is fetched or written (hold-out prices included);
        # the GDELT step below only dry-runs its queries for byte estimates.
        n_tickers = len(store.read_assets(track))
        echo(f"dry run: --execute would fetch prices {h0}..{h1} for {n_tickers} track {track}"
             f" ticker(s), pull hold-out GDELT for {datasets or 'no dataset'}, and evaluate"
             f" {source_trial} once")
    else:
        rc = _ingest_prices_window(store, fetch, None, track, h0, h1, None, 100, echo)
        if rc not in (OK, PARTIAL):
            return rc
    if gdelt is not None:
        for ds in datasets:
            rc = _ingest_gdelt_window(cfg, store, gdelt, ds, h0, h1, execute, False, now,
                                      budget_error, echo)
            if rc != OK:
                return rc
    if not execute:
        return OK
    assets = store.read_assets(track)
    end_excl = h1 + timedelta(days=1)
    prices_all = store.read_prices([a.ticker for a in assets], cfg.start, end_excl)
    tdays = _trading_days(prices_all)
    daily = _read_gdelt(store, track, assets, cfg.start, end_excl)
    feats = _track_features(track, daily, tdays, assets)
    fs = feats[(feats["feature"] == feature) & (feats["date"] >= pd.Timestamp(h0))]
    prices = prices_all[prices_all["date"] >= pd.Timestamp(h0)]
    weights = _weights(_rule_scores(fs, feature, src.params["sign"]), prices,
                       int(src.params["rebalance"]))
    if weights.empty:
        echo("no hold-out weight dates")
        return MISSING
    cost = cfg.cost_bps_etf if track == "A" else cfg.cost_bps_stock
    ret, stats = _simulate(weights, prices, cost, cfg.borrow_bps_annual)
    lo, hi = overfit.stationary_bootstrap_sharpe_ci(ret["ret"], 2000, 10.0, cfg.seed)
    verdict = _gate4_verdict(lo)
    d0, d1 = ret["date"].min().date(), ret["date"].max().date()
    params = {**src.params, "source_trial": source_trial}
    tid = trial_id("holdout", track, params, d0, d1)
    note = json.dumps({"sharpe_ci95": [lo, hi], "gate4": verdict})
    store.add_trial(Trial(trial_id=tid, created_at=now(), track=track, stage="holdout",
                          hypothesis=src.hypothesis, params=params, data_start=d0, data_end=d1,
                          stats=stats, notes=note),
                    ret.assign(trial_id=tid)[TRIAL_RETURN_COLUMNS])
    echo(f"hold-out {d0}..{d1}: Sharpe {stats.sharpe:.2f}, 95% CI [{lo:.2f}, {hi:.2f}];"
         f" gate 4: {verdict}")
    return OK


def _gate4_verdict(ci_low: float) -> str:
    """Gate 4: the weights already carry the pre-registered sign, so only a CI above zero passes.

    A CI entirely below zero is a significant loss in the pre-registered direction, not a pass.
    """
    return "pass" if math.isfinite(ci_low) and ci_low > 0 else "fail"


def _datasets_for(feature: str) -> list[Dataset]:
    names = feature.split("*")
    out: list[Dataset] = []
    if any(n.startswith("events.") for n in names):
        out.append("events")
    if any(n.startswith("gkg.theme") for n in names):
        out.append("gkg-themes")
    if any(n.startswith("gkg.org") for n in names):
        out.append("gkg-orgs")
    return out


# --- shared helpers ---------------------------------------------------------------------------

def trial_id(stage: str, track: str, params: dict[str, Any], start: date, end: date) -> str:
    """Deterministic id of a configuration on a data window (re-running it is not a new trial)."""
    blob = json.dumps({"stage": stage, "track": track, "params": params,
                       "start": start.isoformat(), "end": end.isoformat()},
                      sort_keys=True, default=str)
    return f"{stage}-{track}-{hashlib.sha1(blob.encode()).hexdigest()[:12]}"


def _log_trial(
    store: Store, logged: set[str], track: Track, stage: str, hyp: str, params: dict[str, Any],
    d0: date, d1: date, now: Clock, note: dict[str, Any],
) -> None:
    tid = trial_id(stage, track, params, d0, d1)
    if tid in logged:
        return
    store.add_trial(Trial(trial_id=tid, created_at=now(), track=track, stage=stage,  # type: ignore[arg-type]
                          hypothesis=hyp, params=params, data_start=d0, data_end=d1,
                          notes=json.dumps(note, default=_json_default)))
    logged.add(tid)


def _json_default(x: Any) -> Any:
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    return str(x)


def _dev_window(
    cfg: Config, start: date | None, end: date | None, echo: Echo
) -> tuple[date, date] | None:
    """Inclusive [start, end] inside the dev period, or None (with a message) if it is not."""
    last_dev = cfg.holdout_start - timedelta(days=1)
    start = start or cfg.start
    end = end or last_dev
    if end >= cfg.holdout_start:
        echo(f"refused: end {end} is in the hold-out (>= {cfg.holdout_start}); ADR 0001")
        return None
    if end < start:
        echo(f"refused: end {end} is before start {start}")
        return None
    return start, end


def _simulate(weights: pd.DataFrame, prices: pd.DataFrame, cost: float,
              borrow: float) -> tuple[pd.DataFrame, Any]:
    # Imported here: vectorbt/numba take ~40 s to import, which every other command would pay.
    from eventtrade.core import simulate

    return simulate.simulate(weights, prices, cost, borrow)


def _trading_days(prices: pd.DataFrame) -> list[date]:
    return [d.date() for d in pd.DatetimeIndex(sorted(prices["date"].unique()))]


def _for_tickers(df: pd.DataFrame, tickers: set[str]) -> pd.DataFrame:
    return df[df["ticker"].isin(tickers)].reset_index(drop=True)


def _gb(n: float) -> str:
    return f"{n / 1e9:,.2f} GB"
