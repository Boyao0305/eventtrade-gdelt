"""map-dod pipeline with fake SEC fetchers: sole-award selection, title / Exhibit 21 / JV rules,
resumable caches, --limit-names, --no-fetch, the CSV and summary; and the CLI wiring."""

import csv
from datetime import UTC, date, datetime

import pandas as pd

from eventtrade.adapters import sec
from eventtrade.adapters.dod_store import DodStore
from eventtrade.adapters.sec_store import SecStore
from eventtrade.cli import main as cli_main
from eventtrade.cli import pipelines_dod as pdod
from eventtrade.cli import pipelines_dod_map as pm
from eventtrade.config import Config

NOW = datetime(2026, 10, 11, tzinfo=UTC)
GD, BA, RAY = "0000040533", "0000012927", "0001047122"

NAMES = {
    GD: [("GENERAL DYNAMICS CORP", None, None)],
    BA: [("BOEING CO", None, None)],
    RAY: [("RAYTHEON CO/", None, None)],  # delisted parent: known only through Exhibit 21
}
LAST = {RAY: "2020-04-03"}
# phrase -> documents found by full-text search: (cik, file_date, doc)
EX21 = {"electric boat": [(GD, "2018-02-12", "gd.htm")],
        "raytheon missile systems": [(RAY, "2018-02-14", "ray.htm")],
        "bath iron works": [(GD, "2018-02-12", "gd.htm"), (BA, "2018-02-10", "ba.htm")],
        "unknown widgets": [(BA, "2018-02-10", "ba.htm")]}  # words found, entry is not equal
DOCS = {"gd.htm": "<table><tr><td>Electric Boat Corporation</td><td>Delaware</td></tr>"
                  "<tr><td>Bath Iron Works Corporation</td><td>Maine</td></tr></table>",
        "ray.htm": "Raytheon Missile Systems Company    Delaware\n",
        "ba.htm": "Bath Iron Works Holdings\nUnknown Widgets Europe Ltd\n"}


def _award(seq, names, amount, text="x"):
    return {"seq": seq, "awardees": names, "amount": amount, "kind": "contract",
            "small_business": False, "agency": "NAVY", "text": text}


def _stores(tmp_path):
    cfg = Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")
    ds = DodStore(cfg.dod_db_path, pdod.DOD_START, cfg.holdout_start)
    ds.init()
    ds.write_article(1, "t", "u", date(2018, 6, 1), "ok", [
        _award(0, ["The Boeing Co."], 5e9),
        _award(1, ["Electric Boat Corp."], 2e9),
        _award(2, ["Raytheon Missile Systems"], 1e9),
        _award(3, ["Bath Iron Works Corp."], 3e8),
        _award(4, ["Alberici-Mortenson JV"], 2e8),
        _award(5, ["Global Services LLC"], 1e8),
        _award(6, ["Unknown Widgets Inc."], 5e7),
        _award(7, ["Boeing Co.", "General Dynamics Corp."], 9e9),  # multiple award: excluded
        _award(8, ["General Dynamics Corp."], None),  # no amount: excluded
    ], NOW)
    ss = SecStore(cfg.sec_db_path)
    ss.init()
    ss.write_tickers(pd.DataFrame([(GD, "GD", "GENERAL DYNAMICS CORP"),
                                   (BA, "BA", "BOEING CO")], columns=["cik", "ticker", "title"]))
    return cfg, ds, ss


def _fakes(calls):
    def names_fn(cik):
        calls.append(("names", cik))
        return NAMES.get(cik, []), LAST.get(cik, "2026-10-09")

    def ex21_fn(phrase, query):
        calls.append(("ex21", phrase, query))
        rows = [(phrase, c, fd, f"{c}-{fd}", doc) for c, fd, doc in EX21.get(phrase, [])]
        df = pd.DataFrame(rows, columns=sec.EX21_COLS)
        df.attrs.update(total=len(rows), capped=False)
        return df

    def doc_fn(cik, adsh, doc):
        calls.append(("doc", doc))
        return DOCS[doc]

    return names_fn, ex21_fn, doc_fn


def _read(path):
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_map_dod_rules_csv_and_resume(tmp_path):
    cfg, ds, ss = _stores(tmp_path)
    calls, lines = [], []
    names_fn, ex21_fn, doc_fn = _fakes(calls)
    out = tmp_path / "docs" / "data" / "map.csv"
    assert pm.map_dod(cfg, ss, ds, names_fn, ex21_fn, doc_fn, lambda: NOW, out, echo=lines.append) == 0
    rows = _read(out)
    assert list(rows[0]) == pm.CSV_COLS
    got = {r["awardee"]: (r["cik"], r["rule"], r["ticker_now"]) for r in rows}
    assert got == {
        "The Boeing Co.": (BA, "title", "BA"),
        "Electric Boat Corp.": (GD, "ex21", "GD"),
        "Raytheon Missile Systems": (RAY, "ex21", ""),
        "Bath Iron Works Corp.": (GD, "ex21", "GD"),  # BA's document has no equal entry
        "Alberici-Mortenson JV": ("", "jv", ""),
        "Global Services LLC": ("", "unsearchable", ""),
        "Unknown Widgets Inc.": ("", "none", ""),
    }
    assert [r["seq"] for r in rows] == [str(i) for i in range(7)]
    searched = {c[1] for c in calls if c[0] == "ex21"}
    assert searched == {"electric boat", "raytheon missile systems", "bath iron works",
                        "unknown widgets"}  # not boeing (title), JV or unsearchable names
    assert ("names", RAY) in calls  # the Exhibit 21 parent's names are fetched too
    assert sorted(c[1] for c in calls if c[0] == "doc") == ["ba.htm", "gd.htm", "ray.htm"]
    text = "\n".join(lines)
    assert "mapped: 4/7 paragraphs" in text and "RAYTHEON CO/" in text
    assert "mapped CIKs with no current ticker: 1/3" in text
    # rerun: everything cached, no request
    calls.clear()
    assert pm.map_dod(cfg, ss, ds, names_fn, ex21_fn, doc_fn, lambda: NOW, out, echo=lines.append) == 0
    assert calls == [] and _read(out) == rows


def test_no_fetch_and_limit_names(tmp_path):
    cfg, ds, ss = _stores(tmp_path)
    calls, lines = [], []
    names_fn, ex21_fn, doc_fn = _fakes(calls)
    out = tmp_path / "m.csv"
    assert pm.map_dod(cfg, ss, ds, names_fn, ex21_fn, doc_fn, lambda: NOW, out, echo=lines.append,
                      fetch=False) == 0
    assert calls == []
    assert {r["rule"] for r in _read(out)} <= {"none", "jv", "unsearchable"}
    assert pm.map_dod(cfg, ss, ds, names_fn, ex21_fn, doc_fn, lambda: NOW, out, echo=lines.append,
                      limit_names=2) == 0
    assert [r["awardee"] for r in _read(out)] == ["The Boeing Co.", "Electric Boat Corp."]
    # smoke: names fetched only for listed CIKs sharing a first word with a kept name
    assert {c[1] for c in calls if c[0] == "names"} == {BA, GD}


def test_missing_inputs(tmp_path):
    cfg = Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")
    ds = DodStore(cfg.dod_db_path, pdod.DOD_START, cfg.holdout_start)
    ds.init()
    ss = SecStore(cfg.sec_db_path)
    names_fn, ex21_fn, doc_fn = _fakes([])
    lines = []
    assert pm.map_dod(cfg, ss, ds, names_fn, ex21_fn, doc_fn, lambda: NOW, tmp_path / "m.csv",
                      echo=lines.append) == 4
    assert "missing" in lines[-1]


def test_cli_map_dod_wiring(tmp_path, monkeypatch):
    _stores(tmp_path)
    calls = []
    names_fn, ex21_fn, doc_fn = _fakes(calls)
    monkeypatch.setattr(sec, "former_names", lambda client, cik: names_fn(cik))
    monkeypatch.setattr(sec, "ex21_parents", lambda client, phrase, q: ex21_fn(phrase, q))
    monkeypatch.setattr(sec, "ex21_document", lambda client, c, a, d: doc_fn(c, a, d))
    assert cli_main.main(["--home", str(tmp_path), "init-db"]) == 0
    out = tmp_path / "o.csv"
    assert cli_main.main(["--home", str(tmp_path), "map-dod", "--out", str(out),
                          "--limit-names", "3"]) == 0
    assert len(_read(out)) == 3 and calls
    args = cli_main._parser().parse_args(["map-dod", "--no-fetch"])
    assert args.no_fetch and args.limit_names is None
    assert str(args.out).replace("\\", "/") == "docs/data/dod_issuer_map.csv"
