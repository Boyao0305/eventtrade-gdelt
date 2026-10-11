"""DoD daily contract announcements ingest (Wayback Machine captures of defense.gov), resumable.
Never names a concrete adapter: the entry point passes the listing/fetch functions and the store.

Hold-out discipline: an article's release date is read first (`parse_dod_header`); awards are
parsed and stored only for 2015-01-01 <= release date < config.holdout_start. Reads no prices.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

from eventtrade.cli.pipelines import MISSING, OK, PARTIAL, Clock, Echo
from eventtrade.config import Config
from eventtrade.core.dod_parse import parse_dod_article, parse_dod_header

DOD_START = date(2015, 1, 1)
MAX_CAPTURES = 3  # captures tried per article before giving up
PROGRESS_EVERY = 100

Snapshots = dict[int, list[tuple[str, str]]]


class DodStorePort(Protocol):
    def init(self) -> None: ...
    def in_range(self, d: date | None) -> bool: ...
    def write_snapshots(self, snaps: Snapshots) -> int: ...
    def read_snapshots(self) -> Snapshots: ...
    def done(self) -> set[int]: ...
    def write_article(self, article_id: int, snapshot: str | None, url: str | None,
                      release_date: date | None, status: str, awards: list[dict[str, Any]],
                      now: Any) -> str: ...
    def articles(self) -> list[dict[str, Any]]: ...
    def awards(self) -> list[dict[str, Any]]: ...


def spread(ids: list[int]) -> list[int]:
    """Ids in bit-reversed index order: any prefix samples the whole id range evenly, so a
    `--limit` trial run and the progress lines are representative of every year."""
    ids = sorted(ids)
    bits = max(1, (len(ids) - 1).bit_length())
    order = sorted(range(len(ids)), key=lambda i: int(f"{i:0{bits}b}"[::-1], 2))
    return [ids[i] for i in order]


def ingest_dod(cfg: Config, dstore: DodStorePort, list_fn: Callable[[], Snapshots],
               fetch_fn: Callable[[str, str], str], now: Clock, echo: Echo = print,
               limit: int | None = None, relist: bool = False,
               fetch_errors: tuple[type[BaseException], ...] = (RuntimeError,),
               clock: Callable[[], float] = time.monotonic) -> int:
    """List archived contract articles (cached after the first run unless `relist`), then fetch
    and parse every article without a final status, best capture first (up to MAX_CAPTURES)."""
    dstore.init()
    snaps = {} if relist else dstore.read_snapshots()
    if not snaps:
        echo("listing Wayback captures (CDX) ...")
        snaps = list_fn()
        dstore.write_snapshots(snaps)
    if not snaps:
        echo("missing: the CDX listing returned no article")
        return MISSING
    done = dstore.done()
    todo = [a for a in spread(list(snaps)) if a not in done]
    echo(f"{len(snaps):,} article ids listed (ids {min(snaps)}..{max(snaps)}); {len(done):,} done,"
         f" {len(todo):,} to fetch")
    if limit is not None:
        todo = todo[:limit]
    tally: Counter[str] = Counter()
    n_awards = 0
    t0 = clock()
    for i, aid in enumerate(todo, 1):
        status, n = _one(dstore, aid, snaps[aid], fetch_fn, now, fetch_errors)
        tally[status] += 1
        n_awards += n
        if i % PROGRESS_EVERY == 0 or i == len(todo):
            rate = (clock() - t0) / i
            echo(f"  {i:,}/{len(todo):,} {dict(tally)} awards {n_awards:,}"
                 f" ({rate:.1f} s/article, ~{rate * (len(todo) - i) / 3600:.1f} h left)")
    _summary(dstore, echo)
    failed = sum(v for k, v in tally.items() if k not in ("ok", "out_of_range", "not_contracts"))
    return PARTIAL if failed else OK


_FAIL_RANK = {"fetch_failed": 0, "no_date": 1, "no_awards": 2}


def _one(dstore: DodStorePort, aid: int, caps: list[tuple[str, str]],
         fetch_fn: Callable[[str, str], str], now: Clock,
         fetch_errors: tuple[type[BaseException], ...]) -> tuple[str, int]:
    """Fetch and store one article; a failed capture falls through to the next one. With no
    success, the most informative failure is recorded (no_awards > no_date > fetch_failed)."""
    fail: tuple[str, str | None, str | None, date | None] = ("fetch_failed", None, None, None)
    for ts, url in caps[:MAX_CAPTURES]:
        try:
            html = fetch_fn(ts, url)
        except fetch_errors:
            outcome: tuple[str, str | None, str | None, date | None] = \
                ("fetch_failed", ts, url, None)
        else:
            rd, is_contracts = parse_dod_header(html)
            if rd is not None and not dstore.in_range(rd):
                return dstore.write_article(aid, ts, url, rd, "out_of_range", [], now()), 0
            if rd is not None and not is_contracts:
                return dstore.write_article(aid, ts, url, rd, "not_contracts", [], now()), 0
            awards = parse_dod_article(html)[1] if rd is not None else []
            if awards:
                return dstore.write_article(aid, ts, url, rd, "ok", awards, now()), len(awards)
            outcome = ("no_awards" if rd is not None else "no_date", ts, url, rd)
        if fail[1] is None or _FAIL_RANK[outcome[0]] >= _FAIL_RANK[fail[0]]:
            fail = outcome
    return dstore.write_article(aid, fail[1], fail[2], fail[3], fail[0], [], now()), 0


def _summary(dstore: DodStorePort, echo: Echo) -> None:
    arts = dstore.articles()
    by_year: dict[str, Counter[str]] = {}
    for a in arts:
        y = (a["release_date"] or "none")[:4]
        by_year.setdefault(y, Counter())[a["status"]] += 1
    echo("articles by release year (status counts):")
    for y in sorted(by_year):
        echo(f"  {y}: {dict(sorted(by_year[y].items()))}")
    aw = dstore.awards()
    if aw:
        real = [a for a in aw if a["kind"] != "correction"]
        n = max(len(real), 1)
        kinds = Counter(a["kind"] for a in aw)
        echo(f"awards stored: {len(aw):,} ({len(aw) - len(real)} corrections); with amount"
             f" {sum(a['amount'] is not None for a in real) / n:.1%}, with >= 1 awardee"
             f" {sum(bool(a['awardees']) for a in real) / n:.1%}; kinds {dict(kinds)}")
    else:
        echo("awards stored: 0")
    fails = Counter(a["status"] for a in arts if a["status"] not in
                    ("ok", "out_of_range", "not_contracts"))
    echo(f"parse/fetch failures: {dict(fails) or 0}")
