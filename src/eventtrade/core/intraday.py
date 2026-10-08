"""Intraday event study (ADR 0007): universe, hedges and (later) clock, signals, outcomes, null.

Pure functions only: no I/O (import-linter contract "Core does no I/O").
"""

from __future__ import annotations

from collections.abc import Iterable

from eventtrade.schemas import Asset

# Hedge ETFs (ADR 0007 §Data): fetched for outcomes, never trial assets.
HEDGE_DM = "EFA"
HEDGE_EM = "EEM"
HEDGE_MARKET = "SPY"
SECTOR_SPDR = {
    "Communication Services": "XLC", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Energy": "XLE", "Financials": "XLF", "Health Care": "XLV", "Industrials": "XLI",
    "Information Technology": "XLK", "Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU",
}
HEDGES: tuple[str, ...] = (HEDGE_MARKET, HEDGE_DM, HEDGE_EM, *sorted(set(SECTOR_SPDR.values())))
TRACK_A_GROUPS = ("dm", "em", "commodity")


def track_a_assets(assets: Iterable[Asset]) -> list[Asset]:
    """Track A trial assets: country ETFs (dm/em) and the commodity group, sorted by ticker."""
    return sorted((a for a in assets if a.track == "A" and a.screen_group in TRACK_A_GROUPS),
                  key=lambda a: a.ticker)


def track_b_assets(assets: Iterable[Asset]) -> list[Asset]:
    """Track B trial assets: stocks with a GICS sector that maps to a SPDR, sorted by ticker."""
    return sorted((a for a in assets if a.track == "B" and a.screen_group in SECTOR_SPDR),
                  key=lambda a: a.ticker)


def fetch_order(assets: Iterable[Asset]) -> list[str]:
    """Tickers to fetch: hedges first, then Track A, then Track B (so a partial pull is usable)."""
    assets = list(assets)
    out: list[str] = list(HEDGES)
    for a in track_a_assets(assets) + track_b_assets(assets):
        if a.ticker not in out:
            out.append(a.ticker)
    return out


def alpaca_symbol(ticker: str) -> str:
    """Yahoo-style class shares (BRK-B) to Alpaca's (BRK.B)."""
    return ticker.replace("-", ".")
