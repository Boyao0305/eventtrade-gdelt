"""ADR 0015 amendment 3: awardee name normalisation and dated, mechanical CIK resolution."""

from datetime import date

import pytest

from eventtrade.core import dod_issuers as di


@pytest.mark.parametrize("raw, norm", [
    ("The Boeing Co.", "boeing"),
    ("The Boeing Company", "boeing"),
    ("Lockheed Martin Corporation", "lockheed martin"),
    ("Kellogg Brown & Root Services, Inc.", "kellogg brown and root services"),
    ("Kellogg Brown and Root Services Inc.", "kellogg brown and root services"),
    ("Huntington-Ingalls Industries", "huntington ingalls industries"),
    ("Defense Engineering Inc.*", "defense engineering"),
    ("Sparton L.L.C.", "sparton"),
    ("BAE Systems Land and Armaments LP", "bae systems land and armaments"),
    ("Acme Co. Inc.", "acme"),
    ("Labatt Institutional Supply Co., doing business as Labatt Food Service Corp.",
     "labatt institutional supply"),
    ("USA Waste of California Inc., dba Waste Management", "usa waste of california"),
    ("Science Applications International Corp. (SAIC)", "science applications international"),
    ("Martin's Point Health Care Inc.", "martins point health care"),
    ("Holdings Inc.", "holdings"),
    ("Co.", ""),
    ("ELI LILLY & Co", "eli lilly"),
    ("Eli Lilly and Co.", "eli lilly"),
    ("UNITED TECHNOLOGIES CORP /DE/", "united technologies"),
    ("RAYTHEON CO/", "raytheon"),
    ("LEIDOS HOLDINGS, INC./NEW/", "leidos holdings"),
    ("L-3 Communications EO/IR Inc.", "l 3 communications eo ir"),
])
def test_normalize(raw, norm):
    assert di.normalize(raw) == norm


def test_head():
    assert di.head("Lockheed Martin Corp., Lockheed Martin Aeronautics Co.") == "lockheed martin"
    assert di.head("United Technologies Corp., Pratt & Whitney") == "united technologies"
    assert di.head("Huntington-Ingalls Industries, Ingalls Shipbuilding") is None  # no suffix
    assert di.head("Boeing Co.") is None
    assert di.head("Labatt Supply Co., doing business as Labatt Food") is None


@pytest.mark.parametrize("norm, ok", [
    ("boeing", True), ("pfizer", True), ("kbr", False), ("3m", False),
    ("general electric", True), ("lockheed martin", True),
    ("services", False), ("systems", False), ("global services", False),
    ("technology solutions", False), ("federal logistics", False), ("", False),
    ("bae systems", True), ("aerospace", False), ("general dynamics", True),
])
def test_searchable(norm, ok):
    assert di.searchable(norm) is ok


def test_jv_and_query():
    assert di.is_jv("alberici mortenson jv")
    assert di.is_jv("x and y a joint venture")
    assert not di.is_jv("jvc kenwood")
    assert di.ex21_query("electric boat") == '"electric boat"'
    assert di.ex21_query("pratt and whitney") == '"pratt and whitney" OR "pratt whitney"'


RTX, UTX_OLD, RAY, GD, LHX, LLL = "0000101829", "0000999999", "0001047122", "0000040533", \
    "0000202058", "0001056239"
TITLES = di.build_titles([
    (RTX, "UNITED TECHNOLOGIES CORP /DE/", None, "2020-04-03"),
    (RTX, "RAYTHEON TECHNOLOGIES CORP", "2020-04-03", "2023-07-17"),
    (RTX, "RTX Corp", "2023-07-17", None),
    (RAY, "RAYTHEON CO/", None, None),
    (GD, "GENERAL DYNAMICS CORP", None, None),
    ("0000000001", "KBR, Inc.", None, None),
    ("0000000002", "Twin Co", None, "2018-01-01"),
    ("0000000003", "Twin Corp", "2016-01-01", None),
    ("0000000004", "Services Inc", None, None),  # generic: never a title
    ("0000049196", "HUNTINGTON BANCSHARES INC/MD", None, None),
    ("0000000005", "ACME CO /PA/ /NEW/", None, None),
])


def test_build_titles_skips_generic_and_parses_dates():
    assert "services" not in TITLES
    assert "huntington bancshares" in TITLES and "acme" in TITLES  # EDGAR state tags dropped
    assert TITLES["raytheon technologies"] == [(RTX, date(2020, 4, 3), date(2023, 7, 17))]


def test_title_match_is_dated():
    d1, d2 = date(2019, 6, 1), date(2021, 6, 1)
    assert di.resolve("United Technologies Corp.", d1, TITLES, {}) == (RTX, "title")
    assert di.resolve("United Technologies Corp.", d2, TITLES, {}) == (None, "none")
    assert di.resolve("Raytheon Technologies", d2, TITLES, {}) == (RTX, "title")
    # before the rename, the distinctive prefix 'raytheon' (Raytheon Co.) owns the name
    assert di.resolve("Raytheon Technologies", d1, TITLES, {}) == (RAY, "title_prefix")
    assert di.resolve("RTX Corp.", date(2023, 7, 17), TITLES, {}) == (RTX, "title")
    assert di.resolve("RTX Corp.", date(2023, 7, 16), TITLES, {})[1] == "unsearchable"
    assert di.resolve("KBR Inc.", d1, TITLES, {}) == ("0000000001", "title")  # short but exact


def test_title_ambiguity_and_head():
    assert di.resolve("Twin Co.", date(2017, 1, 1), TITLES, {}) == (None, "ambiguous")
    assert di.resolve("Twin Co.", date(2019, 1, 1), TITLES, {}) == ("0000000003", "title")
    assert di.resolve("United Technologies Corp., Pratt & Whitney", date(2019, 1, 1), TITLES,
                      {}) == (RTX, "title_head")
    assert di.resolve("General Dynamics Corp., Electric Boat", date(2019, 1, 1), TITLES,
                      {}) == (GD, "title_head")


def test_ex21_window_and_ambiguity():
    ex21 = di.build_ex21([
        ("electric boat", GD, "2018-02-12"),
        ("electric boat", GD, "2019-02-13"),
        ("l 3 communications", LLL, "2019-02-20"),
        ("l 3 communications", LHX, "2019-08-26"),
    ])
    assert di.resolve("Electric Boat Corp.", date(2018, 6, 1), TITLES, ex21) == (GD, "ex21")
    # filed on d is not before d; a filing more than 400 days old has expired
    assert di.resolve("Electric Boat Corp.", date(2018, 2, 12), TITLES, ex21) == (None, "none")
    assert di.resolve("Electric Boat Corp.", date(2020, 4, 1), TITLES, ex21) == (None, "none")
    assert di.resolve("L-3 Communications Corp.", date(2019, 6, 1), TITLES, ex21) == \
        (LLL, "ex21")
    assert di.resolve("L-3 Communications Corp.", date(2019, 10, 1), TITLES, ex21) == \
        (None, "ambiguous")
    assert di.resolve("L-3 Communications Corp.", date(2020, 6, 1), TITLES, ex21) == \
        (LHX, "ex21")


def test_jv_and_unsearchable_rules():
    ex21 = di.build_ex21([("alberici mortenson jv", GD, "2018-01-01")])
    assert di.resolve("Alberici-Mortenson JV", date(2018, 6, 1), TITLES, ex21) == (None, "jv")
    assert di.resolve("ABC Inc.", date(2018, 6, 1), TITLES, {}) == (None, "unsearchable")
    assert di.resolve("Global Services LLC", date(2018, 6, 1), TITLES, {}) == \
        (None, "unsearchable")
    assert di.resolve("Inc.", date(2018, 6, 1), TITLES, {}) == (None, "unsearchable")


LMT = "0000936468"
TITLES2 = di.build_titles([
    (RAY, "RAYTHEON CO/", None, None, "2020-04-03"),  # stopped filing after the merger
    (RTX, "RAYTHEON TECHNOLOGIES CORP", "2020-04-03", "2023-07-17", "2026-10-09"),
    (RTX, "RTX Corp", "2023-07-17", None, "2026-10-09"),
    (LMT, "LOCKHEED MARTIN CORP", None, None, "2026-10-09"),
    (GD, "GENERAL DYNAMICS CORP", None, None, None),
    ("0000000007", "GENERAL DYNAMICS LAND CORP", None, None, None),
    ("0000000008", "Harper Inc", None, None, None),
    ("0000000009", "Pacific Inc", None, None, None),
    ("0000000010", "KBR Inc", None, None, None),
    ("0000000011", "Twin Peaks Corp", None, None, None),
    ("0000000012", "Twin Peaks Co", None, None, None),
    ("0000000013", "Gone Corp", "2016-01-01", None, "2015-06-30"),  # ends before it starts
])


def test_last_filing_ends_a_title():
    assert TITLES2["raytheon"] == [(RAY, None, date(2020, 4, 4))]
    assert "gone" not in TITLES2
    assert di.resolve("Raytheon Co.", date(2020, 4, 3), TITLES2, {}) == (RAY, "title")
    ex21 = di.build_ex21([("raytheon", RTX, "2021-02-08")])
    assert di.resolve("Raytheon Co.", date(2021, 6, 1), TITLES2, ex21) == (RTX, "ex21")
    assert di.resolve("Raytheon Co.", date(2021, 6, 1), TITLES2, {}) == (None, "none")
    # the head falls through to Exhibit 21 as well
    assert di.resolve("Raytheon Co., Integrated Defense Systems", date(2021, 6, 1), TITLES2,
                      ex21) == (RTX, "ex21")
    assert di.resolve("Raytheon Co., Integrated Defense Systems", date(2019, 6, 1), TITLES2,
                      ex21) == (RAY, "title_head")


def test_ex21_phrases():
    d = date(2021, 6, 1)
    assert di.ex21_phrases("Lockheed Martin Corp.", d, TITLES2) == []
    assert di.ex21_phrases("Raytheon Co., Integrated Defense Systems", d, TITLES2) == [
        "raytheon co integrated defense systems", "raytheon"]
    assert di.ex21_phrases("Alberici-Mortenson JV", d, TITLES2) == []
    assert di.ex21_phrases("ABC Inc.", d, TITLES2) == []


def test_title_prefix():
    d = date(2021, 6, 1)
    assert di.resolve("Lockheed Martin Rotary and Mission Systems", d, TITLES2, {}) == \
        (LMT, "title_prefix")
    assert di.resolve("General Dynamics Land Systems", d, TITLES2, {}) == \
        ("0000000007", "title_prefix")  # the longest title wins
    assert di.resolve("General Dynamics Electric Boat", d, TITLES2, {}) == (GD, "title_prefix")
    assert di.resolve("Harper Construction Co.", d, TITLES2, {}) == ("0000000008", "title_prefix")
    assert di.resolve("Pacific Architects and Engineers", d, TITLES2, {}) == (None, "none")
    assert di.resolve("KBR Wyle Services", d, TITLES2, {}) == (None, "none")  # 3 letters
    assert di.resolve("Twin Peaks Logistics", d, TITLES2, {}) == (None, "ambiguous")
    assert di.resolve("Lockheedmartin Space", d, TITLES2, {}) == (None, "none")  # word boundary
    # Exhibit 21 agreeing with the prefix stands; conflicting with it, the name is ambiguous
    ex21 = di.build_ex21([("general dynamics electric boat", GD, "2021-02-08"),
                          ("general dynamics land systems", GD, "2021-02-08")])
    assert di.resolve("General Dynamics Electric Boat", d, TITLES2, ex21) == (GD, "ex21")
    assert di.resolve("General Dynamics Land Systems", d, TITLES2, ex21) == (None, "ambiguous")
    # a dead CIK's title is no prefix after its last filing
    assert di.resolve("Raytheon Missile Systems", date(2019, 6, 1), TITLES2, {}) == \
        (RAY, "title_prefix")
    assert di.resolve("Raytheon Missile Systems", d, TITLES2, {}) == (None, "none")
    assert not di.prefix_qualifies("american") and not di.prefix_qualifies("global services")
    assert di.prefix_qualifies("boeing") and di.prefix_qualifies("american water")


EX21_HTML = """<html><body><p>SUBSIDIARIES OF GENERAL DYNAMICS</p><table>
<tr><td><b>Name</b></td><td>Jurisdiction</td></tr>
<tr><td><font>Bath Iron Works Corporation</font></td><td>Maine</td></tr>
<tr><td>Electric Boat Corporation*</td><td>Delaware</td></tr>
<tr><td>National Steel and Shipbuilding Company (Nevada)</td><td>100%</td></tr>
<tr><td>Gulfstream Aerospace Corporation, a Georgia corporation</td><td></td></tr>
<tr><td>General Dynamics Land Systems Inc.<br/>General Dynamics OTS, Inc.</td><td></td></tr>
<tr><td>&#160;&#160;Jet Aviation AG (1)</td><td>Switzerland</td></tr>
</table><div>Rolls-Royce Motor Cars Sterling LLC</div></body></html>"""

EX21_TEXT = """EXHIBIT 21
1. Insitu, Inc.                     Washington
2. Aviall Services, Inc. ......... Delaware
   Boeing Capital Corporation - Delaware
   Spirit Holdings Corp. Delaware
"""


def test_ex21_entries_html():
    e = di.ex21_entries(EX21_HTML)
    for name in ("bath iron works", "electric boat", "national steel and shipbuilding",
                 "gulfstream aerospace", "general dynamics land systems", "general dynamics ots",
                 "jet aviation ag", "rolls royce motor cars sterling"):
        assert name in e, name
    assert "rolls royce" not in e and "bath iron" not in e  # only whole entries


def test_ex21_entries_never_split_names_off_prose_or_later_columns():
    e = di.ex21_entries("<table><tr><td>KW Fund VI - Raytheon LLC</td><td>Delaware</td></tr>"
                        "<tr><td>Harris, Rothenberg International Inc.</td></tr></table>"
                        "<p>Winchester Defense, LLC owns 49% of this Joint Venture and\n"
                        "BAE Systems Ordnance Systems, Inc. owns 51% of the joint venture</p>"
                        "<p>Raytheon Company, a Delaware corporation</p>"
                        "<p>Acme Corp., Delaware</p>")
    assert "kw fund vi raytheon" in e and "harris rothenberg international" in e
    assert "raytheon company" not in e and "raytheon" in e  # ', a Delaware corporation' cut
    assert "acme" in e
    assert "harris" not in e and "bae systems ordnance systems" not in e
    assert not any(x == "raytheon" for x in di.ex21_entries("KW Fund VI - Raytheon LLC"))


def test_ex21_entries_text():
    e = di.ex21_entries(EX21_TEXT)
    for name in ("insitu", "aviall services", "boeing capital", "spirit holdings"):
        assert name in e, name
    assert di.ex21_entries("") == set()


def test_ex21_conflicting_with_a_title_prefix_is_ambiguous():
    from datetime import date as _d

    from eventtrade.core import dod_issuers as di

    titles = di.build_titles([("0000000001", "BAE Systems PLC", None, None)])
    ex21 = di.build_ex21([("bae systems ordnance systems", "0000000002", "2017-02-01")])
    assert di.resolve("BAE Systems Ordnance Systems Inc.", _d(2017, 6, 1), titles, ex21) == \
        (None, "ambiguous")
