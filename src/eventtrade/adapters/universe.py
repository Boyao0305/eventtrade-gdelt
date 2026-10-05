"""Tradable universes: the Track A ETF list (package CSV) and Track B index members (Wikipedia).

Track B uses *current* constituents only — free data has no point-in-time membership, so it
carries survivorship bias (ADR 0001). Tickers are returned in Yahoo form (`BRK.B` -> `BRK-B`).
`org_aliases` are left empty here; alias generation is a separate component.
"""

from __future__ import annotations

import csv
import io
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
    assets = _parse_index_html(resp.text, index)
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
        ))
    return out


def _split(cell: str | None) -> list[str]:
    """'a; b;;c' -> ['a', 'b', 'c']."""
    return [p.strip() for p in (cell or "").split(";") if p.strip()]


def _parse_index_html(html: str, index: str) -> list[Asset]:
    """Parse the `#constituents` table (Symbol, Security columns) of a Wikipedia index page."""
    tables = pd.read_html(io.StringIO(html), attrs={"id": "constituents"}, flavor="lxml")
    table = next((t for t in tables if {"Symbol", "Security"} <= set(map(str, t.columns))), None)
    if table is None:
        raise ValueError(f"{index}: no constituents table with Symbol/Security columns")
    seen: dict[str, Asset] = {}
    for sym, name in zip(table["Symbol"], table["Security"], strict=True):
        if not isinstance(sym, str) or not sym.strip():
            continue
        ticker = sym.strip().upper().replace(".", "-")
        seen.setdefault(ticker, Asset(ticker=ticker, name=str(name).strip(), kind="stock",
                                      track="B", group=index))
    return sorted(seen.values(), key=lambda a: a.ticker)
