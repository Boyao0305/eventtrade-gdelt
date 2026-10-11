"""DoD contract announcement parser on trimmed Wayback captures (tests/fixtures/dod/)."""

from datetime import date
from pathlib import Path

import pytest

from eventtrade.core.dod_parse import parse_award, parse_dod_article, parse_dod_header

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "dod"


def _load(name):
    return parse_dod_article((FIX / name).read_text(encoding="utf-8"))


def test_single_awards_agencies_and_small_business():
    d, aws = _load("contracts_2015-02-18.html")
    assert d == date(2015, 2, 18)
    assert [a["seq"] for a in aws] == list(range(6))
    assert [a["agency"] for a in aws] == ["ARMY"] * 3 + ["AIR FORCE", "NAVY",
                                                         "DEFENSE LOGISTICS AGENCY"]
    first = aws[0]
    assert first["awardees"] == ["DynCorp International"]
    assert first["amount"] == 68_961_578.0 and first["kind"] == "contract"
    assert not first["small_business"]
    assert first["text"].startswith("DynCorp International, Fort Worth, Texas, was awarded")
    assert aws[1]["kind"] == "modification"
    small = aws[2]  # "Defense Engineering Inc.,* Alexandria, Virginia, ..."
    assert small["awardees"] == ["Defense Engineering Inc."] and small["small_business"]
    # a comma inside the name is kept; only the location is dropped
    assert aws[3]["awardees"] == ["Strategic Professional Resources, Inc."]
    assert aws[5]["awardees"] == ["US Foods Lexington"] and aws[5]["amount"] == 61_500_000.0
    # headings, blank paragraphs and the '* Small Business' note are not awards
    assert not any("Small Business" in a["text"][:20] for a in aws)


def test_multiple_award_paragraphs_and_orders():
    d, aws = _load("contracts_2017-08-28.html")
    assert d == date(2017, 8, 28)
    by_first = {a["awardees"][0]: a for a in aws}
    multi = by_first["Mark G. Miller Inc."]
    assert multi["awardees"] == ["Mark G. Miller Inc.", "Falcona Intuitive Integrated Solutions",
                                 "Strategic Enterprise Solutions Corp.",
                                 "Ki Ho Military Acquisition Consulting Inc."]
    assert multi["amount"] == 9_750_000.0 and multi["kind"] == "contract"
    assert multi["agency"] == "AIR FORCE" and not multi["small_business"]
    pair = by_first["Lyon Shipyard Inc."]  # "...; and Tecnico Corp.,* ..., each have been awarded"
    assert pair["awardees"] == ["Lyon Shipyard Inc.", "Tecnico Corp."]
    assert pair["small_business"] and pair["amount"] == 7_200_000.0
    assert by_first["The Boeing Co."]["kind"] == "order"  # "$323,500,000 ... delivery order"
    keo = by_first["L3 Technologies Inc., doing business as L3 KEO"]
    assert keo["kind"] == "order" and keo["amount"] == 7_020_131.0
    assert by_first["Raytheon Missile Systems Co."]["kind"] == "modification"


def test_missing_amount_and_corrections():
    d, aws = _load("contracts_2018-12-14.html")
    assert d == date(2018, 12, 14)
    austal = next(a for a in aws if a["awardees"] == ["Austal USA"])
    assert austal["amount"] is None and austal["kind"] == "modification"
    assert austal["agency"] == "NAVY"
    notes = [a for a in aws if a["kind"] == "correction"]
    assert [a["text"].split(":")[0] for a in notes] == ["UPDATE", "CORRECTION"]
    assert all(a["awardees"] == [] for a in notes)
    assert next(a for a in aws if a["awardees"] == ["Gary Kubiak & Son Electric Inc."])[
        "small_business"]


def test_header_reads_date_without_awards():
    html = (FIX / "contracts_2017-08-28.html").read_text(encoding="utf-8")
    assert parse_dod_header(html) == (date(2017, 8, 28), True)
    news = ('<html><head><meta itemprop="datePublished" content="2009-09-30T14:04:30"/></head>'
            '<body><h1>Supercomputer hopes</h1><div class="body"><p>Lockheed Martin Corp., '
            'Bethesda, Maryland, is awarded a $5 contract.</p></div></body></html>')
    assert parse_dod_header(news) == (date(2009, 9, 30), False)
    assert parse_dod_article(news) == (date(2009, 9, 30), [])  # not a 'Contracts For' article
    assert parse_dod_header("<html><body><p>nothing</p></body></html>") == (None, False)


def test_title_variants():
    for title in ("Contracts For Sept. 2, 2016", "Contracts for September 2, 2016",
                  "CONTRACTS FOR SEP 2, 2016"):
        html = f"<html><body><h1>{title}</h1><div class='body'></div></body></html>"
        assert parse_dod_header(html)[0] == date(2016, 9, 2)


CASES = [
    (("Lockheed Martin Corp., Fort Worth, Texas, is awarded a $1,234,567,890 modification "
      "(P00012) to a previously awarded contract."),
     ["Lockheed Martin Corp."], 1_234_567_890.0, "modification"),
    (("Physical Optics Corp., Torrance, California,* has been awarded a not-to-exceed "
      "$24,500,000 indefinite-delivery/indefinite-quantity contract."),
     ["Physical Optics Corp."], 24_500_000.0, "contract"),
    (("Skidmore, Owings & Merrill LLP, Washington, D.C. (W912ER-23-F-0070); and Hellmuth, Obata "
      "& Kassabaum PC, Washington, D.C. (W912ER-23-F-0069), will compete for each order of the "
      "$700,000,000 firm-fixed-price contract."),
     ["Skidmore, Owings & Merrill LLP", "Hellmuth, Obata & Kassabaum PC"], 700_000_000.0,
     "contract"),
    ("Parker Hannifin Corp., Irvine California, was awarded a $1,000,000 delivery order.",
     ["Parker Hannifin Corp."], 1_000_000.0, "order"),
    (("Chemring Australia Pty Ltd., Lara, Victoria, Australia, is awarded a $19,689,472 "
      "modification (P00001)."),
     ["Chemring Australia Pty Ltd."], 19_689_472.0, "modification"),
    (("Iridium Satellite LLC, Tempe, Arizona, is awarded a $9,378,867 cost-plus-fixed-fee option "
      "to support services."),
     ["Iridium Satellite LLC"], 9_378_867.0, "modification"),
    (("Equilon Enterprises, Houston, Texas (SP0600-15-D-0461), $1,011,111,314; World Fuel "
      "Services Inc., Miami, Florida (SP0600-15-D-0478), $9,028,665, have each been awarded a "
      "fixed-price contract."),
     ["Equilon Enterprises", "World Fuel Services Inc."], 1_011_111_314.0, "contract"),
]


@pytest.mark.parametrize(("text", "names", "amount", "kind"), CASES)
def test_award_paragraphs(text, names, amount, kind):
    a = parse_award(text)
    assert a["awardees"] == names and a["amount"] == amount and a["kind"] == kind


def test_non_award_paragraph_is_skipped():
    assert parse_award("Work will be performed in Norfolk, Virginia.") is None
