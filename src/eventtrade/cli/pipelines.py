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

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

from eventtrade.config import Config
from eventtrade.core import aliases as core_aliases
from eventtrade.core import cv, evaluate, features, labels, models, overfit
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


# --- Pre-registered feature families (ADR 0001) -----------------------------------------------

@dataclass(frozen=True)
class _Family:
    hypothesis: str
    spec: MetricSpec
    sign: float  # pre-registered expected sign; backtests trade this direction by default


_A1_ROOTS = ("root_14_protest", "root_17_coerce", "root_18_assault", "root_19_fight",
             "root_20_massviolence")

_FAMILIES: dict[str, list[_Family]] = {
    "A": [
        _Family("H0", MetricSpec("events", "tone", "mean", ("z",)), 1.0),
        _Family("H0", MetricSpec("events", "n_events", "share", ("z",)), 1.0),
        *(_Family("A1", MetricSpec("events", r, "sum_share", ("z", "lr"),
                                   denominator_metric="n_events"), -1.0) for r in _A1_ROOTS),
        _Family("A1", MetricSpec("events", "goldstein", "mean", ("z",)), 1.0),
        _Family("A2", MetricSpec("gkg", "theme_docs", "mean", ("z",)), 1.0),
        _Family("A2", MetricSpec("gkg", "theme_tone", "mean", ("z",)), 1.0),
        _Family("A3", MetricSpec("events", "tone", "mean", ("z",), agg_days=5), 1.0),
    ],
    "B": [
        _Family("B1", MetricSpec("gkg", "org_docs", "mean", ("z",)), 1.0),
        _Family("B1", MetricSpec("gkg", "org_tone", "mean", ("z", "level")), 1.0),
        _Family("B2", MetricSpec("gkg", "org_neg_docs", "mean", ("z",)), -1.0),
        _Family("B2", MetricSpec("gkg", "org_neg_docs", "sum", ("z", "lr")), -1.0),
    ],
}
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
    """Write the Track A ETF list and/or Track B index members (with GKG org aliases) to assets."""
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
    have = set(store.gdelt_session_dates(query_name, start, end + timedelta(days=1)))
    pending = [(a, b) for a, b in chunks if not (a in have and b in have)]
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


def hypothesis_of(track: Track, feature: str) -> tuple[str, float]:
    """Pre-registered hypothesis id and expected sign of a feature name ('?', +1 if unknown)."""
    if track == "B" and feature == _B1_PRODUCT_NAME:
        return "B1", 1.0
    for fam in _FAMILIES[track]:
        s = fam.spec
        names = {f"{s.column}_z.{s.window}", f"{s.column}_lr.{s.window}", f"{s.column}.0"}
        if feature in names:
            return fam.hypothesis, fam.sign
    return "?", 1.0


# --- signal study (gate 1) --------------------------------------------------------------------

def signal_study(cfg: Config, store: Store, track: Track, now: Clock, echo: Echo = print) -> int:
    """Evaluate every stored feature x horizon of a track, log each as a 'signal' trial, print gates."""
    assets = store.read_assets(track)
    tickers = {a.ticker for a in assets}
    feats = _for_tickers(store.read_features(cfg.start, cfg.holdout_start), tickers)
    labs = _for_tickers(store.read_labels(cfg.start, cfg.holdout_start), tickers)
    if feats.empty or labs.empty:
        echo(f"no features/labels for track {track}: run `build-dataset --track {track}`")
        return MISSING
    cost = cfg.cost_bps_etf if track == "A" else cfg.cost_bps_stock
    logged = {t.trial_id for t in store.read_trials(track, "signal")}
    horizons = list(cfg.horizons)
    d0, d1 = feats["date"].min().date(), feats["date"].max().date()
    n_pass = 0
    echo(f"{'feature':<52}{'h':>3}{'ic_mean':>9}{'nw_t':>7}  gate")
    for name, sub in feats.groupby("feature", sort=True):
        hyp, _ = hypothesis_of(track, str(name))
        for h in horizons:
            params = {"feature": str(name), "horizon": h, "label": "ret_oo"}
            try:
                rep = evaluate.evaluate_signal(
                    sub, labs, str(name), [h, *[x for x in horizons if x != h]], track)
                subs = evaluate.subperiod_signs(evaluate.daily_ic(sub, labs, h))
                ok, reason = evaluate.passes_gate(rep, subs, cost)
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


def _signal_study_vol(
    cfg: Config, store: Store, feats: pd.DataFrame, tickers: set[str], d0: date, d1: date,
    logged: set[str], now: Clock, echo: Echo,
) -> int:
    """A4: event-shock features vs future realised volatility (IC t and sub-period sign only)."""
    prices = store.read_prices(sorted(tickers), cfg.start, cfg.holdout_start)
    if prices.empty:
        return 0
    tdays = _trading_days(prices)
    a1 = [n for n in feats["feature"].unique() if hypothesis_of("A", str(n))[0] == "A1"]
    n_pass = 0
    for h in cfg.horizons:
        # VOL_LABEL_COLUMNS' `value` would collide with the feature's `value` in the IC join.
        vol = labels.realized_vol_label(prices, h, tdays).rename(columns={"value": "vol"})
        for name in sorted(a1):
            ic = evaluate.daily_ic(feats[feats["feature"] == name], vol, h, label_col="vol")
            t = evaluate.newey_west_t(ic, horizon=h)
            subs = evaluate.subperiod_signs(ic)
            sign = 1.0 if t > 0 else -1.0
            held = sum(1 for m in subs if math.isfinite(m) and m * sign > 0)
            ok = math.isfinite(t) and abs(t) >= evaluate.GATE_MIN_ABS_T and held >= 2
            reason = (f"{'pass' if ok else 'fail'}: NW t={t:.2f}, sign held {held}/3"
                      " (vol target: no spread condition)")
            n_pass += ok
            mean = float(ic.mean()) if len(ic) else float("nan")
            echo(f"{'[A4 vol] ' + str(name):<52}{h:>3}{mean:>9.4f}{t:>7.2f}  {reason}")
            params = {"feature": str(name), "horizon": h, "label": "realized_vol"}
            note = {"gate": reason, "subperiod_ic": subs, "ic_mean": mean, "ic_t": t,
                    "n_days": len(ic)}
            _log_trial(store, logged, "A", "signal", "A4", params, d0, d1, now, note)
    return n_pass


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
    params |= {"horizon": horizon, "rebalance": rebalance, "weights": "rank_long_short"}
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
    signal stage chose what got backtested. Hold-out trials are not counted.
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
