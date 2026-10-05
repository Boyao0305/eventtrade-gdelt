"""adapters/universe.py — packaged ETF universe and Wikipedia index-member parsing.

Fixtures: `universe_fips_country.txt` is GDELT's FIPS lookup
(https://www.gdeltproject.org/data/lookups/FIPS.country.txt, fetched 2026-10-05);
`universe_sp500.html` / `universe_sp400.html` are the live Wikipedia tables cut to a few rows.
"""

from datetime import date
from pathlib import Path

import pytest

from eventtrade.adapters.universe import (
    _parse_index_html,
    _parse_universe_csv,
    fetch_index_members,
    load_etf_universe,
)

FIX = Path(__file__).parent / "fixtures"
FIPS = {line.split("\t")[0] for line in (FIX / "universe_fips_country.txt")
        .read_text(encoding="latin-1").splitlines() if "\t" in line}
GROUPS = {"country", "sector", "commodity", "rates", "market"}


# --- the packaged ETF universe ---------------------------------------------------------------

def test_etf_universe_shape():
    u = load_etf_universe()
    assert 50 <= len(u) <= 70
    assert len({a.ticker for a in u}) == len(u)
    assert all(a.kind == "etf" and a.track == "A" and a.group in GROUPS for a in u)
    assert all(a.org_aliases == [] for a in u)
    assert [a.ticker for a in u if a.group == "market"] == ["SPY"]


def test_etf_universe_fips_codes_exist_in_gdelt_lookup():
    bad = [(a.ticker, c) for a in load_etf_universe() for c in a.fips_countries if c not in FIPS]
    assert bad == []


def test_country_etfs_have_one_country_and_no_themes():
    for a in load_etf_universe():
        if a.group == "country":
            assert len(a.fips_countries) == 1 and a.gkg_themes == [], a.ticker


def test_sector_commodity_rates_have_themes():
    for a in load_etf_universe():
        if a.group in {"sector", "commodity", "rates"}:
            assert a.gkg_themes, a.ticker


def test_first_trade_known_and_mostly_before_dev_start():
    u = load_etf_universe()
    assert all(a.first_trade is not None for a in u)
    late = sorted(a.ticker for a in u if a.first_trade > date(2015, 3, 2))
    # launched after the dev period starts: short history, enter the cross-section late
    assert late == ["KSA", "XLC", "XLRE"]


# FIPS 10-4, not ISO: the codes most often gotten wrong.
@pytest.mark.parametrize("ticker, fips", [
    ("EWJ", "JA"), ("EWG", "GM"), ("FXI", "CH"), ("EWY", "KS"), ("EZA", "SF"), ("TUR", "TU"),
    ("EWU", "UK"), ("EWP", "SP"), ("EWL", "SZ"), ("EWD", "SW"), ("EWA", "AS"), ("EWS", "SN"),
    ("EPHE", "RP"), ("ECH", "CI"), ("VNM", "VM"), ("EWO", "AU"), ("EIRL", "EI"), ("EDEN", "DA"),
    ("UAE", "AE"), ("KSA", "SA"), ("EIS", "IS"),
])
def test_country_fips_codes(ticker, fips):
    by = {a.ticker: a for a in load_etf_universe()}
    assert by[ticker].fips_countries == [fips]


# --- CSV parsing rules -----------------------------------------------------------------------

@pytest.mark.parametrize("row, field, expected", [
    # ';'-separated lists, whitespace and empty items dropped
    ("X,n,etf,A,commodity, SA ;IZ;;,ENV_OIL,,2006-04-10", "fips_countries", ["SA", "IZ"]),
    ("X,n,etf,A,commodity,,ENV_OIL;ECON_OILPRICE,,", "gkg_themes", ["ENV_OIL", "ECON_OILPRICE"]),
    # empty list cell -> []
    ("X,n,etf,A,sector,,,,", "fips_countries", []),
    # first_trade: ISO date or empty -> None
    ("X,n,etf,A,sector,,,,2006-04-10", "first_trade", date(2006, 4, 10)),
    ("X,n,etf,A,sector,,,,", "first_trade", None),
])
def test_parse_universe_csv_rules(row, field, expected):
    header = "ticker,name,kind,track,group,fips_countries,gkg_themes,org_aliases,first_trade\n"
    (a,) = _parse_universe_csv(header + row + "\n")
    assert getattr(a, field) == expected


# --- Wikipedia index pages -------------------------------------------------------------------

def test_parse_sp500_fixture():
    assets = _parse_index_html((FIX / "universe_sp500.html").read_text(encoding="utf-8"), "sp500")
    by = {a.ticker: a for a in assets}
    # dotted share classes become Yahoo's dashed form
    assert set(by) == {"MMM", "AOS", "BRK-B", "BF-B"}
    assert by["MMM"].name == "3M"
    assert all(a.kind == "stock" and a.track == "B" and a.group == "sp500" for a in assets)
    assert all(a.org_aliases == [] and a.fips_countries == [] for a in assets)


def test_parse_sp400_uses_constituents_not_changes_table():
    assets = _parse_index_html((FIX / "universe_sp400.html").read_text(encoding="utf-8"), "sp400")
    assert [a.ticker for a in assets] == ["AA", "AAL", "AAON"]
    assert {a.group for a in assets} == {"sp400"}


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_fetch_index_members_rejects_truncated_page():
    html = (FIX / "universe_sp500.html").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="layout changed"):
        fetch_index_members("sp500", get=lambda url, **kw: _Resp(html))


def test_fetch_index_members_unknown_index():
    with pytest.raises(ValueError, match="unknown index"):
        fetch_index_members("nasdaq100")  # type: ignore[arg-type]


# --- live (slow) ----------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("index, lo, hi", [("sp500", 495, 510), ("sp400", 395, 405)])
def test_fetch_index_members_live(index, lo, hi):
    assets = fetch_index_members(index)
    assert lo <= len(assets) <= hi


@pytest.mark.slow
def test_gkg_themes_exist_in_live_lookup():
    import requests
    r = requests.get("https://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT", timeout=60)
    r.raise_for_status()
    known = {line.split("\t")[0] for line in r.text.splitlines() if "\t" in line}
    bad = [(a.ticker, t) for a in load_etf_universe() for t in a.gkg_themes if t not in known]
    assert bad == []
