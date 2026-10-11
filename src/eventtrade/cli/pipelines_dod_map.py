"""ADR 0015 amendment 3: map DoD sole awardees to issuer CIKs, mechanically and dated.

Never names a concrete adapter: the entry point passes the SEC fetch functions and the stores.
Reads the DoD awards (read-only) and SEC names / Exhibit 21 documents; writes only the
`sec_names`, `ex21_hits` and `ex21_entries` caches (data/sec.sqlite) and the mapping CSV. Reads
no prices.
"""

from __future__ import annotations

import csv
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from eventtrade.cli.pipelines import MISSING, OK, Clock, Echo
from eventtrade.config import Config
from eventtrade.core import dod_issuers as di

PROGRESS_EVERY = 500
TOP_PARENTS = 40
CSV_COLS = ["article_id", "seq", "release_date", "awardee", "norm", "cik", "rule", "ticker_now"]
MAPPED_RULES = ("title", "title_head", "ex21", "title_prefix")

Names = list[tuple[str, str | None, str | None]]
NamesFn = Callable[[str], tuple[Names, str | None]]  # cik -> (names, last filing date)
Ex21Fn = Callable[[str, str], Any]  # (phrase, EFTS query) -> DataFrame with attrs total/capped
DocFn = Callable[[str, str, str], str]  # (cik, adsh, doc) -> document text ('' if gone)


class SecStorePort(Protocol):
    def init(self) -> None: ...
    def pulled(self, kind: str) -> set[str]: ...
    def read(self, table: str) -> Any: ...
    def write_names(self, cik: str, names: Names, now: Any,
                    last_filing: str | None = None) -> int: ...
    def write_ex21(self, phrase: str, df: Any, now: Any, capped: bool = False,
                   total: int | None = None) -> int: ...
    def write_ex21_entries(self, adsh: str, doc: str, cik: str, file_date: str,
                           entries: set[str], now: Any, version: int = 1) -> int: ...
    def read_ex21_entries(self, norms: set[str]) -> Any: ...


class DodAwardsPort(Protocol):
    def awards(self) -> list[dict[str, Any]]: ...


def sole_awards(awards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Paragraphs with exactly one awardee and a parsed amount."""
    return [{"article_id": a["article_id"], "seq": a["seq"], "release_date": a["release_date"],
             "awardee": a["awardees"][0], "amount": float(a["amount"])}
            for a in awards if len(a["awardees"]) == 1 and a.get("amount") is not None]


def map_dod(cfg: Config, sstore: SecStorePort, dstore: DodAwardsPort, names_fn: NamesFn,
            ex21_fn: Ex21Fn, doc_fn: DocFn, now: Clock, out: Path, echo: Echo = print,
            limit_names: int | None = None, fetch: bool = True,
            clock: Callable[[], float] = time.monotonic) -> int:
    """(a) sole-award paragraphs and their normalised names; (b) EDGAR names and last filing
    date of every CIK in cik_tickers (resumable); (c) Exhibit 21 full-text search for every
    searchable name (and head) a title does not resolve on one of its dates, then each hit
    document fetched once and parsed into normalised entries, then the names of the filers
    whose entries equal a searched name (all resumable); (d) resolve every paragraph, write the
    CSV and print the summary.

    `limit_names` (smoke runs): keep the N searchable names with the most award dollars, and
    fetch names only for CIKs whose current title shares a first word with one of them."""
    sstore.init()
    paras = [p for p in sole_awards(dstore.awards())
             if p["release_date"] and date.fromisoformat(p["release_date"]) < cfg.holdout_start]
    tick = sstore.read("cik_tickers")
    if not paras or tick.empty:
        echo("missing: DoD awards (ingest-dod) or the CIK -> ticker map (ingest-sec)")
        return MISSING
    for p in paras:
        p["norm"] = di.normalize(p["awardee"])
        p["d"] = date.fromisoformat(p["release_date"])
    dollars: Counter[str] = Counter()
    for p in paras:
        dollars[p["norm"]] += p["amount"]
    names = sorted({p["norm"] for p in paras})
    srch = [n for n in names if di.searchable(n) and not di.is_jv(n)]
    echo(f"(a) {len(paras):,} sole-award paragraphs, {len(names):,} distinct normalised names,"
         f" {len(srch):,} searchable")
    if limit_names is not None:
        keep = set(sorted(srch, key=lambda n: (-dollars[n], n))[:limit_names])
        paras = [p for p in paras if p["norm"] in keep]
        echo(f"  --limit-names {limit_names}: {len(paras):,} paragraphs kept")

    # (b) title universe: EDGAR names of current listings
    first_ticker: dict[str, str] = {}
    for cik, t in zip(tick["cik"], tick["ticker"], strict=True):
        first_ticker.setdefault(str(cik).zfill(10), str(t))
    universe = sorted(first_ticker)
    if limit_names is not None:
        words = {n.split()[0] for p in paras for n in (p["norm"], di.head(p["awardee"]) or "")
                 if n}
        universe = sorted({str(c).zfill(10) for c, ti in zip(tick["cik"], tick["title"],
                                                            strict=True)
                           if (di.normalize(ti) or "-").split()[0] in words})
    _fetch_names(sstore, universe, names_fn, now, echo, fetch, clock, "(b) names of listed CIKs")

    # (c) Exhibit 21: full-text search, then exact entries of each hit document
    titles = _titles(sstore)
    need = sorted({ph for p in paras for ph in di.ex21_phrases(p["awardee"], p["d"], titles)})
    done = sstore.pulled("ex21")
    todo = [n for n in need if n not in done]
    echo(f"(c) Exhibit 21 search: {len(need):,} names need it, {len(todo):,} not cached"
         + ("" if fetch else " (--no-fetch: skipped)"))
    if fetch:
        t0, n_req = clock(), 0
        for i, n in enumerate(todo, 1):
            df = ex21_fn(n, di.ex21_query(n))
            attrs = getattr(df, "attrs", {}) or {}
            sstore.write_ex21(n, df, now(), capped=bool(attrs.get("capped")),
                              total=attrs.get("total"))
            n_req += max(1, -(-int(attrs.get("total") or 0) // 100))
            if i % PROGRESS_EVERY == 0 or i == len(todo):
                rate = (clock() - t0) / i
                echo(f"  {i:,}/{len(todo):,} (~{n_req:,} requests, {rate:.1f} s/name,"
                     f" ~{rate * (len(todo) - i) / 60:.0f} min left)")
    hits_all = sstore.read("ex21_hits")
    hits_all = hits_all[hits_all["phrase"].isin(set(need))]
    hits = hits_all.drop_duplicates(["adsh", "doc"])
    done = sstore.pulled("ex21doc")
    docs = [(a, dc, c, fd) for a, dc, c, fd in zip(hits["adsh"], hits["doc"], hits["cik"],
                                                   hits["file_date"], strict=True)
            if f"v{di.EX21_PARSER_VERSION}:{a}/{dc}" not in done]
    echo(f"    Exhibit 21 documents: {len(hits):,} hit, {len(docs):,} not cached"
         + ("" if fetch else " (--no-fetch: skipped)"))
    if fetch:
        t0 = clock()
        for i, (a, dc, c, fd) in enumerate(docs, 1):
            sstore.write_ex21_entries(a, dc, c, fd, di.ex21_entries(doc_fn(c, a, dc)), now(),
                                      version=di.EX21_PARSER_VERSION)
            if i % PROGRESS_EVERY == 0 or i == len(docs):
                rate = (clock() - t0) / i
                echo(f"  {i:,}/{len(docs):,} ({rate:.2f} s/document,"
                     f" ~{rate * (len(docs) - i) / 60:.0f} min left)")
    # an entry counts only in a document that the search for that same name returned
    ent = sstore.read_ex21_entries(set(need)).merge(
        hits_all[["phrase", "adsh", "doc"]].rename(columns={"phrase": "entry_norm"}),
        on=["entry_norm", "adsh", "doc"]).drop_duplicates(["entry_norm", "adsh", "doc"])
    # names of every filer the search found (verified or not): delisted companies such as
    # Raytheon Co. enter the title universe this way
    parents = sorted(set(hits["cik"]))
    _fetch_names(sstore, parents, names_fn, now, echo, fetch, clock,
                 f"    names of {len(parents):,} Exhibit 21 filer CIK(s)")

    # (d) resolve
    titles = _titles(sstore)
    ex21 = di.build_ex21(zip(ent["entry_norm"], ent["cik"], ent["file_date"], strict=True))
    rows = []
    for p in paras:
        cik, rule = di.resolve(p["awardee"], p["d"], titles, ex21)
        rows.append({**p, "cik": cik or "", "rule": rule,
                     "ticker_now": first_ticker.get(cik or "", "")})
    rows.sort(key=lambda r: (r["release_date"], int(r["article_id"]), int(r["seq"])))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CSV_COLS)
        w.writerows([r[c] for c in CSV_COLS] for r in rows)
    echo(f"(d) wrote {len(rows):,} rows to {out}")
    _summary(rows, sstore, first_ticker, echo)
    capped = sstore.pulled("ex21cap") & set(need)
    if capped:
        echo(f"Exhibit 21 searches cut at 10,000 hits: {len(capped)}: {sorted(capped)[:20]}")
    return OK


def _titles(sstore: SecStorePort) -> di.Titles:
    nm = sstore.read("sec_names")
    return di.build_titles(zip(nm["cik"], nm["name"], nm["valid_from"], nm["valid_to"],
                               nm["last_filing"], strict=True))


def _fetch_names(sstore: SecStorePort, ciks: list[str], names_fn: NamesFn, now: Clock,
                 echo: Echo, fetch: bool, clock: Callable[[], float], label: str) -> None:
    done = sstore.pulled("names")
    todo = [c for c in ciks if c not in done]
    echo(f"{label}: {len(ciks):,} CIK(s), {len(todo):,} not cached"
         + ("" if fetch else " (--no-fetch: skipped)"))
    if not fetch:
        return
    t0 = clock()
    for i, cik in enumerate(todo, 1):
        names, last = names_fn(cik)
        sstore.write_names(cik, names, now(), last_filing=last)
        if i % PROGRESS_EVERY == 0 or i == len(todo):
            rate = (clock() - t0) / i
            echo(f"  {i:,}/{len(todo):,} ({rate:.2f} s/CIK, ~{rate * (len(todo) - i) / 60:.0f}"
                 " min left)")


def _summary(rows: list[dict[str, Any]], sstore: SecStorePort, first_ticker: dict[str, str],
             echo: Echo) -> None:
    """Rule counts and dollars, share of dollars mapped, top parents by mapped dollars (names
    only), and mapped CIKs without a current listing. No prices."""
    total = sum(r["amount"] for r in rows) or 1.0
    by_rule: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for r in rows:
        by_rule[r["rule"]][0] += 1
        by_rule[r["rule"]][1] += r["amount"]
    echo("rule            paragraphs    $bn   share of $")
    for rule, (n, amt) in sorted(by_rule.items(), key=lambda kv: -kv[1][1]):
        echo(f"  {rule:<13} {int(n):>10,} {amt / 1e9:>6.1f}   {amt / total:>6.1%}")
    mapped = [r for r in rows if r["rule"] in MAPPED_RULES]
    m_amt = sum(r["amount"] for r in mapped)
    echo(f"mapped: {len(mapped):,}/{len(rows):,} paragraphs, {m_amt / total:.1%} of award"
         " dollars")
    nm = sstore.read("sec_names")
    current = {str(c): str(n) for c, n, b in zip(nm["cik"], nm["name"], nm["valid_to"],
                                                  strict=True)
               if b is None or isinstance(b, float)}  # NULL: the name in use now
    by_cik: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for r in mapped:
        by_cik[r["cik"]][0] += 1
        by_cik[r["cik"]][1] += r["amount"]
    echo(f"top {TOP_PARENTS} parent CIKs by mapped award dollars:")
    for cik, (n, amt) in sorted(by_cik.items(), key=lambda kv: -kv[1][1])[:TOP_PARENTS]:
        echo(f"  {cik} {first_ticker.get(cik, '-'):<6} {current.get(cik, '?')[:45]:<45}"
             f" {int(n):>5} ${amt / 1e9:>6.2f}bn")
    unl = {c: v for c, v in by_cik.items() if c not in first_ticker}
    echo(f"mapped CIKs with no current ticker: {len(unl):,}/{len(by_cik):,}"
         f" ({sum(int(v[0]) for v in unl.values()):,} paragraphs,"
         f" {sum(v[1] for v in unl.values()) / total:.1%} of award dollars)")
