"""ADR 0011 SEC ingest: insider purchase data sets (quarterly) and 13D filings (monthly), resumable.
Never names a concrete adapter: the entry point passes the client functions and store in."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo
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


INITIAL_13D = frozenset({"SC 13D", "SCHEDULE 13D"})


def universe_ciks(assets: list[Any], cik_tickers: Any) -> dict[str, str]:
    """Track B ticker -> CIK, by exact ticker equality with the SEC map (Yahoo 'BRK-B' is SEC
    'BRK-B'). A ticker listed under several CIKs keeps the first listed."""
    first: dict[str, str] = {}
    for cik, ticker in zip(cik_tickers["cik"], cik_tickers["ticker"], strict=True):
        first.setdefault(str(ticker).upper(), str(cik).zfill(10))
    return {a.ticker: first[a.ticker.upper()] for a in assets if a.ticker.upper() in first}


def ingest_sec_extra(cfg: Config, sstore: Any, store: Any, submissions_fn: Callable[[str], Any],
                     subject_fn: Callable[[list[str], str], str | None], now: Clock,
                     echo: Echo = print) -> int:
    """ADR 0011 amendments 2 and 3 inputs, resumable: (a) EDGAR submissions (10-Q/10-K, 8-K,
    DEFM14A dates) of every Track B company mapped to a CIK; (b) the header subject CIK of every
    initial 13D filed before the hold-out that lists a universe CIK. Reads no prices."""
    sstore.init()
    assets = store.read_assets("B")
    tick = sstore.read("cik_tickers")
    if not assets or tick.empty:
        echo("missing: Track B assets or the CIK -> ticker map (run load-universe, ingest-sec)")
        return MISSING
    mapping = universe_ciks(assets, tick)
    ciks = sorted(set(mapping.values()))
    echo(f"universe: {len(mapping)}/{len(assets)} Track B tickers matched to a CIK"
         f" ({len(mapping) / len(assets):.1%}), {len(ciks)} distinct CIKs")
    unmatched = sorted(a.ticker for a in assets if a.ticker not in mapping)
    if unmatched:
        echo(f"  unmatched: {' '.join(unmatched)}")
    done = sstore.pulled("submissions")
    todo = [c for c in ciks if c not in done]
    echo(f"submissions: {len(todo)} CIK(s) to pull")
    for i, cik in enumerate(todo, 1):
        sstore.write_submissions(cik, submissions_fn(cik), now())
        if i % 100 == 0:
            echo(f"  {i}/{len(todo)}")
    holdout = cfg.holdout_start.isoformat()
    f13 = sstore.read("filings_13d")
    uni = set(ciks)
    f13["cik_list"] = [json.loads(x) for x in f13["ciks"]]
    keep = f13[f13["form"].isin(INITIAL_13D) & (f13["file_date"] < holdout)
               & f13["cik_list"].map(lambda xs: bool(uni.intersection(xs)))]
    done = sstore.pulled("subject13d")
    todo13 = keep[~keep["adsh"].isin(done)]
    echo(f"13D subjects: {len(keep)} initial 13D(s) list a universe CIK; {len(todo13)} to pull")
    for i, (adsh, cl) in enumerate(zip(todo13["adsh"], todo13["cik_list"], strict=True), 1):
        sstore.write_subject(adsh, subject_fn(cl, adsh), now())
        if i % 500 == 0:
            echo(f"  {i}/{len(todo13)}")
    subj = sstore.read("subject_13d").merge(keep[["adsh", "cik_list"]], on="adsh")
    found = subj[subj["subject_cik"].notna()]
    first = sum(s == cl[0] for s, cl in zip(found["subject_cik"], found["cik_list"], strict=True))
    echo(f"subjects: {len(found)}/{len(subj)} found; {found['subject_cik'].isin(uni).sum()} are"
         f" universe CIKs; subject = first listed CIK in {first}/{len(found)}")
    echo(f"done: company_filings {len(sstore.read('company_filings')):,} rows")
    return OK


def ingest_sec_shares(cfg: Config, sstore: Any, stores: list[Any],
                      shares_fn: Callable[[str], Any], now: Clock, echo: Echo = print,
                      ciks: list[str] | None = None) -> int:
    """ADR 0015 input, resumable: XBRL share counts (filed before the hold-out) of the given
    CIKs, or else of every company in the given price stores' assets (any track) that maps to a
    CIK. Reads no prices."""
    sstore.init()
    if ciks is None:
        tick = sstore.read("cik_tickers")
        assets = [a for s in stores for a in s.read_assets()]
        if not assets or tick.empty:
            echo("missing: assets or the CIK -> ticker map (run load-universe, ingest-sec)")
            return MISSING
        ciks = sorted(set(universe_ciks(assets, tick).values()))
    ciks = sorted({str(c).zfill(10) for c in ciks})
    done = sstore.pulled("shares")
    todo = [c for c in ciks if c not in done]
    echo(f"shares: {len(ciks)} CIKs, {len(todo)} to pull")
    empty = 0
    for i, cik in enumerate(todo, 1):
        empty += sstore.write_shares(cik, shares_fn(cik), now()) == 0
        if i % 200 == 0:
            echo(f"  {i}/{len(todo)} ({empty} without a share count)")
    echo(f"done: shares_out {len(sstore.read('shares_out')):,} rows; {empty} CIK(s) without"
         " a share count this run")
    return OK
