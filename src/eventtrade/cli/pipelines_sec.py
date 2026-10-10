"""ADR 0011 SEC ingest: insider purchase data sets (quarterly) and 13D filings (monthly), resumable.
Never names a concrete adapter: the entry point passes the client functions and store in."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from eventtrade.cli.pipelines import OK, REFUSED, Clock, Echo
from eventtrade.config import Config

SEC_START = date(2015, 10, 1)  # 2015Q4: Form 4s filed late in 2015 inform early-2016 clusters


def quarters(start: date, end: date) -> list[tuple[int, int]]:
    out, y, q = [], start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        out.append((y, q))
        y, q = (y, q + 1) if q < 4 else (y + 1, 1)
    return out


def months(start: date, end: date) -> list[tuple[date, date]]:
    out, d = [], date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        out.append((max(d, start), min(nxt - timedelta(days=1), end)))
        d = nxt
    return out


def ingest_sec(cfg: Config, sstore: Any, insider_quarter: Callable[[int, int], dict],
               filings_13d: Callable[[date, date], Any], tickers: Callable[[], Any],
               now: Clock, echo: Echo = print) -> int:
    """Pull every missing insider quarter and 13D month from 2015Q4 to the day before the
    hold-out, then refresh the CIK -> ticker map. Data sets of quarters reaching into the
    hold-out are not pulled (2025Q1 ends 2025-03-31)."""
    end = cfg.holdout_start - timedelta(days=1)
    if end < SEC_START:
        echo("refused: nothing before the hold-out")
        return REFUSED
    sstore.init()
    done_q = sstore.pulled("insider")
    todo_q = [(y, q) for y, q in quarters(SEC_START, end) if f"{y}q{q}" not in done_q]
    echo(f"insider data sets: {len(todo_q)} quarter(s) to pull")
    for y, q in todo_q:
        n = sstore.write_insider(f"{y}q{q}", insider_quarter(y, q), now())
        echo(f"  {y}q{q}: {n:,} purchase transactions")
    done_m = sstore.pulled("13d")
    todo_m = [(a, b) for a, b in months(SEC_START, end) if a.strftime("%Y-%m") not in done_m]
    echo(f"13D filings: {len(todo_m)} month(s) to pull")
    for a, b in todo_m:
        n = sstore.write_13d(a.strftime("%Y-%m"), filings_13d(a, b), now())
        if a.month in (1, 7):
            echo(f"  {a:%Y-%m}: {n} filings")
    echo(f"CIK -> ticker map: {sstore.write_tickers(tickers()):,} rows")
    echo(f"done: {sstore.counts()}")
    return OK
