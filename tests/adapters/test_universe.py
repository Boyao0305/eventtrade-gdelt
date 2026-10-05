"""adapters/universe.py — packaged ETF universe and Wikipedia index-member parsing.

Fixtures: `universe_fips_country.txt` is GDELT's FIPS lookup
(https://www.gdeltproject.org/data/lookups/FIPS.country.txt, fetched 2026-10-05);
`universe_sp500.html` / `universe_sp400.html` are the live Wikipedia tables cut to a few rows.
"""

from datetime import date
from pathlib import Path

import pytest

from eventtrade.adapters import universe as universe_mod
from eventtrade.adapters.universe import (
    _one_share_class,
    _parse_index_html,
    _parse_universe_csv,
    fetch_index_members,
    load_etf_universe,
)
from eventtrade.schemas import Asset

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


def test_rates_etfs_have_no_country_and_only_spy_is_plain_us():
    # ADR 0002 item 2 / experiment 0003: SPY, TLT, IEF and SHY all mapped to ['US'] and so had
    # identical event features. Rates are not a country-news bet: they lose the mapping.
    by = {a.ticker: a for a in load_etf_universe()}
    for t in ("TLT", "IEF", "SHY"):
        assert by[t].group == "rates" and by[t].fips_countries == [], t
    assert by["SPY"].fips_countries == ["US"]
    assert [a.ticker for a in by.values() if a.fips_countries == ["US"]] == ["SPY"]


def test_no_two_etfs_share_a_country_list():
    seen: dict[tuple[str, ...], str] = {}
    for a in load_etf_universe():
        if a.fips_countries:
            key = tuple(sorted(a.fips_countries))
            assert key not in seen, (a.ticker, seen.get(key))
            seen[key] = a.ticker


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


# --- ADR 0003 screen groups (Track A) ---------------------------------------------------------

# MSCI market classification as of 2015-03-02, the start of the dev period (ADR 0003
# implementation notes), not at each fund's inception; 'em' also holds markets that were
# Frontier or Standalone on that date (no third country group in the pre-registered screen).
# Sources: MSCI annual market classification reviews; reclassification history in the CSV's
# msci_note column. Every ticker is listed so a CSV edit cannot silently move one.
SCREEN_GROUP_CASES: dict[str, str | None] = {
    # developed on 2015-03-02
    "EWJ": "dm", "EWG": "dm", "EWU": "dm", "EWQ": "dm", "EWI": "dm", "EWP": "dm", "EWL": "dm",
    "EWN": "dm", "EWD": "dm", "EWC": "dm", "EWA": "dm", "EWH": "dm", "EWS": "dm", "NORW": "dm",
    "EIRL": "dm", "EWO": "dm", "EWK": "dm", "EDEN": "dm", "ENZL": "dm",
    # Israel: MSCI moved it EM -> DM effective 2010-06, so DM on 2015-03-02
    "EIS": "dm",
    # emerging on 2015-03-02
    "FXI": "em", "EWY": "em", "EWT": "em", "INDA": "em", "EWZ": "em", "EWW": "em", "EZA": "em",
    "TUR": "em", "EIDO": "em", "THD": "em", "EWM": "em", "EPHE": "em", "EPOL": "em", "ECH": "em",
    "EPU": "em",
    # Greece: MSCI moved it DM -> EM effective 2013-11-27, so EM on 2015-03-02
    "GREK": "em",
    # Qatar and UAE: FM -> EM effective 2014-06-02, so EM on 2015-03-02
    "QAT": "em", "UAE": "em",
    # not DM, not EM on 2015-03-02 -> 'em' bucket: Argentina FM (2009-06 .. 2019-05),
    # Vietnam FM, Saudi Arabia Standalone (EM from 2019)
    "ARGT": "em", "VNM": "em", "KSA": "em",
    # commodities and the energy sector ETF
    "USO": "commodity", "UNG": "commodity", "GLD": "commodity", "SLV": "commodity",
    "CPER": "commodity", "DBA": "commodity", "XLE": "commodity",
    # not in any Track A screen group
    "SPY": None, "TLT": None, "IEF": None, "SHY": None, "ITA": None, "XLF": None, "XLK": None,
    "XLV": None, "XLI": None, "XLB": None, "XLP": None, "XLY": None, "XLU": None, "XLRE": None,
    "XLC": None,
}


def test_screen_group_table_covers_the_universe():
    assert {a.ticker for a in load_etf_universe()} == set(SCREEN_GROUP_CASES)


@pytest.mark.parametrize(("ticker", "expected"), sorted(SCREEN_GROUP_CASES.items()))
def test_etf_screen_group(ticker, expected):
    by = {a.ticker: a for a in load_etf_universe()}
    assert by[ticker].screen_group == expected


def test_screen_groups_follow_asset_group():
    for a in load_etf_universe():
        if a.group == "country":
            assert a.screen_group in {"dm", "em"}, a.ticker
        elif a.group == "commodity" or a.ticker == "XLE":
            assert a.screen_group == "commodity", a.ticker
        else:
            assert a.screen_group is None, a.ticker


def test_screen_groups_have_at_least_five_names():
    # ADR 0003: within-group daily rank IC needs >= 5 names per day.
    counts: dict[str, int] = {}
    for a in load_etf_universe():
        if a.screen_group:
            counts[a.screen_group] = counts.get(a.screen_group, 0) + 1
    assert counts == {"dm": 20, "em": 21, "commodity": 7}


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
    assert a.screen_group is None  # no screen_group column -> None


@pytest.mark.parametrize(("cell", "expected"), [
    ("dm", "dm"),
    (" em ", "em"),          # whitespace stripped
    ("commodity", "commodity"),
    ("", None),              # empty -> None
    ("  ", None),
])
def test_parse_universe_csv_screen_group(cell, expected):
    header = ("ticker,name,kind,track,group,fips_countries,gkg_themes,org_aliases,first_trade,"
              "screen_group,msci_note\n")
    (a,) = _parse_universe_csv(header + f'X,n,etf,A,country,JA,,,,{cell},"free text; ignored"\n')
    assert a.screen_group == expected


def test_parse_universe_csv_rejects_unknown_screen_group():
    header = "ticker,name,kind,track,group,fips_countries,gkg_themes,org_aliases,first_trade,screen_group\n"
    with pytest.raises(ValueError, match="screen_group"):
        _parse_universe_csv(header + "X,n,etf,A,country,JA,,,,DM\n")


# --- Wikipedia index pages -------------------------------------------------------------------

def test_parse_sp500_fixture():
    assets = _parse_index_html((FIX / "universe_sp500.html").read_text(encoding="utf-8"), "sp500")
    by = {a.ticker: a for a in assets}
    # dotted share classes become Yahoo's dashed form
    assert set(by) == {"MMM", "AOS", "BRK-B", "BF-B"}
    assert by["MMM"].name == "3M"
    assert all(a.kind == "stock" and a.track == "B" and a.group == "sp500" for a in assets)
    assert all(a.org_aliases == [] and a.fips_countries == [] for a in assets)
    # ADR 0003 Track B groups: the 'GICS Sector' column
    assert {t: a.screen_group for t, a in by.items()} == {
        "MMM": "Industrials", "AOS": "Industrials", "BRK-B": "Financials",
        "BF-B": "Consumer Staples"}


def test_parse_sp400_uses_constituents_not_changes_table():
    assets = _parse_index_html((FIX / "universe_sp400.html").read_text(encoding="utf-8"), "sp400")
    assert [a.ticker for a in assets] == ["AA", "AAL", "AAON"]
    assert {a.group for a in assets} == {"sp400"}
    assert [a.screen_group for a in assets] == ["Materials", "Industrials", "Industrials"]


# The 11 GICS sectors as Wikipedia spells them (live pages, 2026-10-05: sp500 and sp400 both
# show exactly these 11). A value outside the set means GICS or the page changed: fail loudly
# rather than invent a 12th screen group.
@pytest.mark.parametrize(("sector", "expected"), [
    ("Information Technology", "Information Technology"),
    ("Communication Services", "Communication Services"),
    ("Real Estate", "Real Estate"),
    (" Health Care ", "Health Care"),        # whitespace stripped
    ("Telecommunication Services", ValueError),  # pre-2018 GICS name
    ("Tech", ValueError),
    ("", ValueError),
])
def test_gics_sector_validation(sector, expected):
    html = _constituents_html([("AAPL", "Apple Inc.", sector)])
    if expected is ValueError:
        with pytest.raises(ValueError, match="GICS"):
            _parse_index_html(html, "sp500")
    else:
        (a,) = _parse_index_html(html, "sp500")
        assert a.screen_group == expected


def test_missing_gics_sector_column_is_an_error():
    html = ('<html><body><table id="constituents"><tr><th>Symbol</th><th>Security</th></tr>'
            "<tr><td>AAPL</td><td>Apple</td></tr></table></body></html>")
    with pytest.raises(ValueError, match="GICS Sector"):
        _parse_index_html(html, "sp500")


def test_share_class_dedup_keeps_sector():
    rows = [("GOOGL", "Alphabet Inc. (Class A)", "Communication Services"),
            ("GOOG", "Alphabet Inc. (Class C)", "Communication Services")]
    (a,) = _one_share_class(_parse_index_html(_constituents_html(rows), "sp500"))
    assert (a.ticker, a.screen_group) == ("GOOGL", "Communication Services")


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_fetch_index_members_rejects_truncated_page():
    html = (FIX / "universe_sp500.html").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="layout changed"):
        fetch_index_members("sp500", get=lambda url, **kw: _Resp(html))


# --- one share class per company (ADR 0002 item 2) --------------------------------------------

# (listings as (ticker, Wikipedia Security name), tickers kept). Experiment 0003: GOOG/GOOGL,
# FOX/FOXA and NWS/NWSA produced identical GKG features, double-counting one company.
# Same company = same name once the class marker ("(Class A)", "Class B") is removed. The
# preferred class is listed explicitly; otherwise the alphabetically first ticker is kept.
SHARE_CLASS_CASES: list[tuple[list[tuple[str, str]], set[str]]] = [
    ([("GOOGL", "Alphabet Inc. (Class A)"), ("GOOG", "Alphabet Inc. (Class C)")], {"GOOGL"}),
    ([("GOOG", "Alphabet Inc. (Class C)"), ("GOOGL", "Alphabet Inc. (Class A)")], {"GOOGL"}),
    ([("FOX", "Fox Corporation (Class B)"), ("FOXA", "Fox Corporation (Class A)")], {"FOXA"}),
    ([("NWS", "News Corp (Class B)"), ("NWSA", "News Corp (Class A)")], {"NWSA"}),
    # marker with or without parentheses, any case
    ([("NWS", "News Corp class B"), ("NWSA", "News Corp (Class A)")], {"NWSA"}),
    # a pair not in the preference list: alphabetically first ticker
    ([("UAA", "Under Armour (Class A)"), ("UA", "Under Armour (Class C)")], {"UA"}),
    # different companies sharing a word are not merged
    ([("FOXA", "Fox Corporation (Class A)"), ("FOXF", "Fox Factory")], {"FOXA", "FOXF"}),
    # a lone class listing is kept as is
    ([("BRK-B", "Berkshire Hathaway"), ("BF-B", "Brown–Forman")], {"BRK-B", "BF-B"}),
]


def _stock(ticker: str, name: str) -> Asset:
    return Asset(ticker=ticker, name=name, kind="stock", track="B", group="sp500")


@pytest.mark.parametrize(("listings", "kept"), SHARE_CLASS_CASES)
def test_one_share_class(listings, kept):
    out = _one_share_class([_stock(t, n) for t, n in listings])
    assert {a.ticker for a in out} == kept
    assert [a.ticker for a in out] == sorted(a.ticker for a in out)


def _constituents_html(rows: list[tuple[str, ...]]) -> str:
    """(ticker, name[, sector]) rows; sector defaults to Industrials."""
    body = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2] if len(r) > 2 else 'Industrials'}</td></tr>"
        for r in rows)
    return ('<html><body><table id="constituents"><tr><th>Symbol</th><th>Security</th>'
            f"<th>GICS Sector</th></tr>{body}</table></body></html>")


def test_fetch_index_members_keeps_one_share_class(monkeypatch):
    rows = [("GOOGL", "Alphabet Inc. (Class A)"), ("GOOG", "Alphabet Inc. (Class C)"),
            ("FOXA", "Fox Corporation (Class A)"), ("FOX", "Fox Corporation (Class B)"),
            ("NWSA", "News Corp (Class A)"), ("NWS", "News Corp (Class B)"), ("MMM", "3M")]
    monkeypatch.setitem(universe_mod._MIN_MEMBERS, "sp500", 1)
    out = fetch_index_members("sp500", get=lambda url, **kw: _Resp(_constituents_html(rows)))
    assert [a.ticker for a in out] == ["FOXA", "GOOGL", "MMM", "NWSA"]


def test_fetch_index_members_unknown_index():
    with pytest.raises(ValueError, match="unknown index"):
        fetch_index_members("nasdaq100")  # type: ignore[arg-type]


# --- live (slow) ----------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("index, lo, hi", [("sp500", 495, 510), ("sp400", 395, 405)])
def test_fetch_index_members_live(index, lo, hi):
    assets = fetch_index_members(index)
    assert lo <= len(assets) <= hi
    assert len({a.screen_group for a in assets}) == 11


@pytest.mark.slow
def test_gkg_themes_exist_in_live_lookup():
    import requests
    r = requests.get("https://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT", timeout=60)
    r.raise_for_status()
    known = {line.split("\t")[0] for line in r.text.splitlines() if "\t" in line}
    bad = [(a.ticker, t) for a in load_etf_universe() for t in a.gkg_themes if t not in known]
    assert bad == []
