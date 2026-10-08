"""Pipelines of the intraday study (ADR 0007). Like `pipelines`, never names a concrete adapter:
the entry point passes the intraday store and the bar fetcher in.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _dev_window
from eventtrade.config import Config
from eventtrade.core import intraday

# ADR 0007: intraday bars start on the first 2016 session.
INTRADAY_START = date(2016, 1, 4)

FetchBars = Callable[[list[str], date, date], pd.DataFrame]


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

