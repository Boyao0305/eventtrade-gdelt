"""Pipelines of the intraday study (ADR 0007). Like `pipelines`, never names a concrete adapter:
the entry point passes the intraday store and the bar fetcher in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, Protocol

import pandas as pd

from eventtrade.cli.pipelines import BUDGET, MISSING, OK, REFUSED, Clock, Echo, _dev_window, _gb
from eventtrade.config import Config
from eventtrade.core import intraday

# ADR 0007: intraday bars start on the first 2016 session.
INTRADAY_START = date(2016, 1, 4)

FetchBars = Callable[[list[str], date, date], pd.DataFrame]

# ADR 0007 "Range and pulls": GDELT ET days from 2015-12-01 (the baselines need the 20 trading
# days before 2016-01-04) to the last dev day.
GDELT_15MIN_START = date(2015, 12, 1)
Dataset15 = Literal["events", "orgs", "events_cat"]


@dataclass(frozen=True)
class _Plan15:
    query_name: str  # BigQuery query file and gdelt_pulls ledger key
    table: str  # intraday store table
    months_per_chunk: int  # 12 = calendar years; chosen by dry run to stay under the 50 GB cap


# Chunk sizes: the coarsest calendar period under the 50 GB per-query cap in the dry runs of
# 2026-10-07. Events: largest year 18.4 GB (2016); a year re-reads the 30-day lookback once, not
# twelve times. Orgs: largest year 39.3 GB (2016, 8,978 aliases); GKG has no lookback, so the
# chunk size changes the number of queries, not the bytes. Events by category (ADR 0008): the
# events rows plus four columns (root, base, two actor countries).
_PLANS: dict[str, _Plan15] = {
    "events": _Plan15("events_15min", "events_15min", 12),
    "orgs": _Plan15("gkg_orgs_15min", "orgs_15min", 12),
    "events_cat": _Plan15("events_15min_cat", "events_15min_cat", 12),
}


class Gdelt15Source(Protocol):
    """Budget-guarded 15-minute GDELT puller (adapters/gdelt_bq.GdeltBigQuery satisfies it).
    Pulls return (rows in the store's column order, bytes billed)."""

    def estimate(self, query_name: str, start: date, end: date, **params: Any) -> int: ...
    def pull_events_15min(self, start: date, end: date,
                          countries: list[str]) -> tuple[pd.DataFrame, int]: ...
    def pull_orgs_15min(self, start: date, end: date, aliases: list[str],
                        alias_tickers: list[str]) -> tuple[pd.DataFrame, int]: ...
    def pull_events_15min_cat(self, start: date, end: date, countries: list[str],
                              cameo_codes: list[str],
                              cameo_fips: list[str]) -> tuple[pd.DataFrame, int]: ...


def ingest_intraday_prices(
    cfg: Config, store: Any, istore: Any, fetch: FetchBars, tickers: list[str] | None,
    start: date | None, end: date | None, now: Clock, echo: Echo = print,
    max_tickers: int | None = None,
) -> int:
    """Fetch 15-minute bars per (ticker, calendar year) not yet in the ledger. Resumable."""
    window = _dev_window(cfg, start or INTRADAY_START, end, echo)
    if window is None:
        return REFUSED
    lo, hi = window
    if not tickers:
        tickers = intraday.fetch_order(store.read_assets(None))
        if len(tickers) == len(intraday.HEDGES):
            echo("no Track A/B assets with screen groups: run `load-universe` first")
            return MISSING
    istore.init()
    done = istore.fetched_years()
    todo = [(t, y) for t in tickers for y in range(lo.year, hi.year + 1) if (t, y) not in done]
    if max_tickers is not None:
        keep = list(dict.fromkeys(t for t, _ in todo))[:max_tickers]
        todo = [(t, y) for t, y in todo if t in keep]
    echo(f"intraday bars {lo}..{hi}: {len(tickers)} tickers, {len(todo)} ticker-year(s) to fetch")
    total = 0
    for i, (t, y) in enumerate(todo, 1):
        a, b = max(lo, date(y, 1, 1)), min(hi, date(y, 12, 31))
        bars = fetch([intraday.alpaca_symbol(t)], a, b)
        bars = bars.assign(ticker=t) if not bars.empty else bars
        n = istore.write_bars_year(t, y, bars, now())
        total += n
        if i % 25 == 0 or i == len(todo):
            echo(f"  {i}/{len(todo)} ticker-years, {total:,} bars (last {t} {y}: {n})")
    echo(f"done: {total:,} bars written")
    return OK


def intraday_status(istore: Any, echo: Echo = print) -> int:
    istore.init()
    for table, n in istore.counts().items():
        echo(f"{table:<18}{n:>14,}")
    return OK


def ingest_gdelt_15min(
    cfg: Config, store: Any, istore: Any, gdelt: Gdelt15Source, dataset: Dataset15,
    start: date | None, end: date | None, execute: bool, now: Clock,
    budget_error: type[Exception] = RuntimeError, echo: Echo = print,
    cameo_fips: list[tuple[str, str]] | None = None,
) -> int:
    """ADR 0007/0008: dry-run (default) or pull 15-minute GDELT counts chunk by chunk into the
    intraday store, skipping chunks already in its `gdelt_pulls` ledger. ET days, dev only.
    `cameo_fips` ((CAMEO code, FIPS) rows) is required for dataset 'events_cat'."""
    window = _dev_window(cfg, start or GDELT_15MIN_START, end, echo)
    if window is None:
        return REFUSED
    lo, hi = window
    plan = _PLANS[dataset]
    if dataset == "events_cat" and not cameo_fips:
        echo("events_cat: no CAMEO -> FIPS mapping given")
        return MISSING
    params = gdelt_15min_params(store.read_assets(None), dataset, cameo_fips)
    if params is None:
        what = "aliases (Track B)" if dataset == "orgs" else "FIPS countries (Track A)"
        echo(f"{dataset}: no {what} in assets with screen groups; run `load-universe` first")
        return MISSING
    if dataset == "events_cat":
        unmapped = sorted(set(params["countries"]) - set(params["cameo_fips"]))
        if unmapped:
            echo(f"events_cat: no CAMEO code for FIPS {', '.join(unmapped)}; add them to the"
                 " CAMEO -> FIPS table first")
            return MISSING
    istore.init()
    chunks = period_chunks(lo, hi, plan.months_per_chunk)
    done = istore.pulled_chunks(plan.query_name)
    pending = [c for c in chunks if c not in done]
    size = ", ".join(f"{len(v)} {k}" for k, v in params.items()
                     if k not in ("alias_tickers", "cameo_fips"))
    echo(f"{dataset} ({plan.query_name}) ET days {lo}..{hi}: {size}; {len(chunks)} chunk(s),"
         f" {len(chunks) - len(pending)} already pulled, {len(pending)} to pull")
    left = cfg.bq_monthly_budget_bytes - store.month_to_date_bytes(now())
    if not execute:
        total, worst = 0, 0
        for a, b in pending:
            est = gdelt.estimate(plan.query_name, a, b, **params)
            total, worst = total + est, max(worst, est)
            over = "  > per-query cap" if est > cfg.bq_max_bytes_per_query else ""
            echo(f"  {a}..{b}: {_gb(est)}{over}")
        echo(f"estimate {_gb(total)} (largest chunk {_gb(worst)}); budget left this month"
             f" {_gb(left)} (of {_gb(cfg.bq_monthly_budget_bytes)}); per-query cap"
             f" {_gb(cfg.bq_max_bytes_per_query)}")
        if total > 0 and total > left:
            echo("estimate exceeds what is left this month: --execute would stop part-way"
                 " and can be resumed later")
        echo("dry run only; pass --execute to pull")
        return OK
    rows_total = 0
    for a, b in pending:
        try:
            if dataset == "events":
                df, billed = gdelt.pull_events_15min(a, b, params["countries"])
            elif dataset == "events_cat":
                df, billed = gdelt.pull_events_15min_cat(a, b, params["countries"],
                                                         params["cameo_codes"],
                                                         params["cameo_fips"])
            else:
                df, billed = gdelt.pull_orgs_15min(a, b, params["aliases"],
                                                   params["alias_tickers"])
        except budget_error as e:
            echo(f"refused by budget guard at {a}..{b}: {e}")
            echo("already-pulled chunks are stored; rerun later to resume")
            return BUDGET
        n = istore.write_gdelt_chunk(plan.table, plan.query_name, (a, b), df, billed, now())
        rows_total += n
        echo(f"  {a}..{b}: {n:,} rows, {_gb(billed)} billed")
    echo(f"done: {rows_total:,} rows written")
    return OK


def gdelt_15min_params(
    assets: list[Any], dataset: Dataset15, cameo_fips: list[tuple[str, str]] | None = None,
) -> dict[str, list[str]] | None:
    """Query parameters from the stored universe, or None if there is nothing to ask for.

    events: the FIPS countries of the Track A trial assets (dm/em/commodity).
    events_cat: the same countries, plus the (CAMEO code, FIPS) rows of `cameo_fips` whose FIPS
    is one of them, as parallel arrays cameo_codes / cameo_fips sorted by CAMEO code.
    orgs: (alias, ticker) pairs of the Track B trial assets (GICS sector mapped to a SPDR),
    aliases lower-cased as the daily pull does; an alias shared by two tickers counts for both.
    """
    if dataset in ("events", "events_cat"):
        countries = sorted({c.strip() for a in intraday.track_a_assets(assets)
                            for c in a.fips_countries if c.strip()})
        if not countries:
            return None
        if dataset == "events":
            return {"countries": countries}
        if cameo_fips is None:
            raise ValueError("events_cat needs the CAMEO -> FIPS table")
        rows = sorted({(c, f) for c, f in cameo_fips if f in countries})
        return {"countries": countries, "cameo_codes": [c for c, _ in rows],
                "cameo_fips": [f for _, f in rows]}
    pairs = sorted({(x.strip().lower(), a.ticker) for a in intraday.track_b_assets(assets)
                    for x in a.org_aliases if x.strip()})
    if not pairs:
        return None
    return {"aliases": [x for x, _ in pairs], "alias_tickers": [t for _, t in pairs]}


def period_chunks(start: date, end: date, months: int) -> list[tuple[date, date]]:
    """Inclusive pieces of [start, end] on calendar boundaries of `months` months (12 = calendar
    years, 1 = calendar months), clipped to the window."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    if months < 1 or 12 % months:
        raise ValueError(f"months must divide 12, got {months}")
    out = []
    a = start
    while a <= end:
        m0 = (a.month - 1) // months * months  # 0-based first month of a's period
        y, m = a.year + (m0 + months) // 12, (m0 + months) % 12 + 1
        b = min(date(y, m, 1) - timedelta(days=1), end)
        out.append((a, b))
        a = b + timedelta(days=1)
    return out
