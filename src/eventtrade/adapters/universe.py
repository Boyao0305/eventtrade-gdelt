"""Tradable universes: the Track A ETF list (package CSV) and Track B index members (Wikipedia).

Track B uses *current* constituents only — free data has no point-in-time membership, so it
carries survivorship bias (ADR 0001). Tickers are returned in Yahoo form (`BRK.B` -> `BRK-B`).
`org_aliases` are left empty here; alias generation is a separate component.

`screen_group` (ADR 0003 cells): Track A from the CSV's screen_group column, the country's MSCI
class as of 2015-03-02 (ADR 0003 implementation notes; its msci_note column records that class
and the reclassification history); Track B is the
'GICS Sector' column of the Wikipedia table, checked against the 11 GICS sectors.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Callable
from datetime import date
from importlib import resources
from typing import Literal

import pandas as pd
import requests

from eventtrade.schemas import Asset

_INDEX_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
}
# Live counts are ~503 and ~400; anything far below means the page layout changed.
_MIN_MEMBERS = {"sp500": 480, "sp400": 380}
_USER_AGENT = "eventtrade-research/0.1 (index membership; contact via repo owner)"
# One listing per company (ADR 0002): share classes get identical GKG features (experiment 0003)
# and would double-count the company in a cross-section. Keep the class listed here, else the
# alphabetically first ticker. Class A: voting shares and the longer listing history.
_PREFERRED_SHARE_CLASS = frozenset({"GOOGL", "FOXA", "NWSA"})
_CLASS_MARKER = re.compile(r"\(?\s*\bclass\s+[a-z]\b\s*\)?", re.IGNORECASE)
# Track A screen groups (ADR 0003): MSCI developed / emerging country ETFs, commodities + XLE.
_ETF_SCREEN_GROUPS = frozenset({"dm", "em", "commodity"})
# The 11 GICS sectors as Wikipedia spells them (since 2018, when Telecommunication Services
# became Communication Services). Anything else means the classification or the page changed.
GICS_SECTORS = frozenset({
    "Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy",
    "Financials", "Health Care", "Industrials", "Information Technology", "Materials",
    "Real Estate", "Utilities",
})


def load_etf_universe() -> list[Asset]:
    """Track A ETF universe from the packaged universe_etf.csv."""
    text = resources.files("eventtrade").joinpath("universe_etf.csv").read_text(encoding="utf-8")
    return _parse_universe_csv(text)


def fetch_index_members(
    index: Literal["sp500", "sp400"],
    *,
    get: Callable[..., requests.Response] = requests.get,
    timeout: float = 30.0,
) -> list[Asset]:
    """Current constituents of the S&P 500 or 400 from Wikipedia, as Track B stock Assets."""
    if index not in _INDEX_URLS:
        raise ValueError(f"unknown index {index!r}; expected one of {sorted(_INDEX_URLS)}")
    resp = get(_INDEX_URLS[index], timeout=timeout, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    assets = _one_share_class(_parse_index_html(resp.text, index))
    if len(assets) < _MIN_MEMBERS[index]:
        raise ValueError(f"{index}: parsed only {len(assets)} members; Wikipedia layout changed?")
    return assets


def _parse_universe_csv(text: str) -> list[Asset]:
    """CSV rows -> Assets; list columns are ';'-separated, empty first_trade -> None."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        out.append(Asset(
            ticker=row["ticker"].strip(),
            name=row["name"].strip(),
            kind=row["kind"].strip(),
            track=row["track"].strip(),
            group=row["group"].strip(),
            fips_countries=_split(row.get("fips_countries", "")),
            gkg_themes=_split(row.get("gkg_themes", "")),
            org_aliases=_split(row.get("org_aliases", "")),
            first_trade=date.fromisoformat(row["first_trade"]) if row.get("first_trade") else None,
            screen_group=_etf_screen_group(row),
        ))
    return out


def _etf_screen_group(row: dict[str, str]) -> str | None:
    """CSV screen_group cell -> 'dm' | 'em' | 'commodity' | None (empty cell or no column)."""
    cell = (row.get("screen_group") or "").strip()
    if not cell:
        return None
    if cell not in _ETF_SCREEN_GROUPS:
        raise ValueError(f"{row['ticker']}: unknown screen_group {cell!r}; "
                         f"expected one of {sorted(_ETF_SCREEN_GROUPS)}")
    return cell


def _split(cell: str | None) -> list[str]:
    """'a; b;;c' -> ['a', 'b', 'c']."""
    return [p.strip() for p in (cell or "").split(";") if p.strip()]


def _parse_index_html(html: str, index: str) -> list[Asset]:
    """Parse the `#constituents` table (Symbol, Security, GICS Sector) of a Wikipedia index page."""
    tables = pd.read_html(io.StringIO(html), attrs={"id": "constituents"}, flavor="lxml")
    table = next((t for t in tables if {"Symbol", "Security"} <= set(map(str, t.columns))), None)
    if table is None:
        raise ValueError(f"{index}: no constituents table with Symbol/Security columns")
    if "GICS Sector" not in set(map(str, table.columns)):
        raise ValueError(f"{index}: constituents table has no 'GICS Sector' column")
    seen: dict[str, Asset] = {}
    for sym, name, sector in zip(table["Symbol"], table["Security"], table["GICS Sector"],
                                 strict=True):
        if not isinstance(sym, str) or not sym.strip():
            continue
        ticker = sym.strip().upper().replace(".", "-")
        seen.setdefault(ticker, Asset(ticker=ticker, name=str(name).strip(), kind="stock",
                                      track="B", group=index,
                                      screen_group=_gics_sector(sector, index, ticker)))
    return sorted(seen.values(), key=lambda a: a.ticker)


def _gics_sector(cell: object, index: str, ticker: str) -> str:
    """Validated GICS sector name; anything outside the 11 sectors (or empty) is an error."""
    sector = cell.strip() if isinstance(cell, str) else ""
    if sector not in GICS_SECTORS:
        raise ValueError(f"{index} {ticker}: GICS sector {cell!r} is not one of the 11 sectors")
    return sector


def _one_share_class(assets: list[Asset]) -> list[Asset]:
    """One Asset per company (same name modulo class marker), sorted by ticker."""
    by_company: dict[str, list[Asset]] = {}
    for a in assets:
        by_company.setdefault(_company_key(a.name), []).append(a)
    kept = [min(group, key=lambda a: (a.ticker not in _PREFERRED_SHARE_CLASS, a.ticker))
            for group in by_company.values()]
    return sorted(kept, key=lambda a: a.ticker)


def _company_key(name: str) -> str:
    """'Alphabet Inc. (Class C)' -> 'alphabet inc': class marker and punctuation removed."""
    s = _CLASS_MARKER.sub(" ", unicodedata.normalize("NFKC", name)).lower()
    return " ".join(re.sub(r"[^\w]+", " ", s).split())
