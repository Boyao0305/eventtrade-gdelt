"""ingest-dod pipeline with a fake Wayback: resume, capture fallback, hold-out, summary; and the
CLI wiring (argument parsing) of `ingest-dod`."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from eventtrade.adapters.dod_store import DodStore
from eventtrade.cli import main as cli_main
from eventtrade.cli import pipelines_dod as pd
from eventtrade.config import Config

NOW = datetime(2026, 10, 11, tzinfo=UTC)
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "dod"
HOLDOUT_PAGE = ("<html><body><h1>Contracts For April 14, 2025</h1><div class='body'>"
                "<p style='text-align: center;'>NAVY</p><p>Secret Co., Norfolk, Virginia, is "
                "awarded a $1 contract.</p></div></body></html>")
NEWS_PAGE = ('<html><head><meta itemprop="datePublished" content="2016-03-01T10:00:00"/></head>'
             "<body><h1>Some news</h1></body></html>")


class FetchError(RuntimeError):
    pass


def _cfg(tmp_path):
    return Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")


def _store(tmp_path):
    return DodStore(tmp_path / "data" / "dod.sqlite", pd.DOD_START, date(2025, 4, 1))


def _pages():
    return {"a1": (FIX / "contracts_2015-02-18.html").read_text(encoding="utf-8"),
            "b2": (FIX / "contracts_2017-08-28.html").read_text(encoding="utf-8"),
            "h3": HOLDOUT_PAGE, "n4": NEWS_PAGE}


SNAPS = {1: [("t", "a1")],
         2: [("t", "bad"), ("t", "b2")],  # first capture fails: fall back to the second
         3: [("t", "h3")],
         4: [("t", "n4")],
         5: [("t", "bad"), ("t", "bad")]}


def _run(tmp_path, fetched, lines, **kw):
    pages = _pages()
    listed = []

    def list_fn():
        listed.append(1)
        return SNAPS

    def fetch(ts, url):
        fetched.append(url)
        if url not in pages:
            raise FetchError(url)
        return pages[url]

    rc = pd.ingest_dod(_cfg(tmp_path), _store(tmp_path), list_fn, fetch, lambda: NOW,
                       lines.append, fetch_errors=(FetchError,), **kw)
    return rc, listed


def test_ingest_resume_fallback_and_holdout(tmp_path):
    fetched, lines = [], []
    rc, _ = _run(tmp_path, fetched, lines)
    assert rc == pd.PARTIAL  # article 5 failed
    st = _store(tmp_path)
    arts = {a["article_id"]: a for a in st.articles()}
    assert {k: a["status"] for k, a in arts.items()} == {
        1: "ok", 2: "ok", 3: "out_of_range", 4: "not_contracts", 5: "fetch_failed"}
    assert arts[2]["url"] == "b2" and arts[3]["release_date"] == "2025-04-14"
    aw = st.awards()
    assert {a["article_id"] for a in aw} == {1, 2}  # nothing from the hold-out article
    assert not any("Secret Co." in a["text"] for a in aw)
    assert arts[1]["n_awards"] == 6 and arts[2]["n_awards"] == 5
    assert any(s.startswith("articles by release year") for s in lines)
    assert any("2025: {'out_of_range': 1}" in s for s in lines)
    assert any("parse/fetch failures: {'fetch_failed': 1}" in s for s in lines)
    # rerun: the listing is cached and only the failed article is tried again
    fetched2, lines2 = [], []
    rc2, listed2 = _run(tmp_path, fetched2, lines2)
    assert listed2 == [] and fetched2 == ["bad", "bad"] and rc2 == pd.PARTIAL
    assert len(_store(tmp_path).awards()) == len(aw)


def test_limit_and_relist(tmp_path):
    fetched, lines = [], []
    _, listed = _run(tmp_path, fetched, lines, limit=2)
    assert len(_store(tmp_path).articles()) == 2 and listed == [1]
    _, listed2 = _run(tmp_path, [], [], limit=0, relist=True)
    assert listed2 == [1]


def test_empty_listing_is_missing(tmp_path):
    rc = pd.ingest_dod(_cfg(tmp_path), _store(tmp_path), dict, lambda ts, u: "", lambda: NOW,
                       lambda s: None)
    assert rc == pd.MISSING


def test_spread_order_covers_the_range():
    ids = list(range(100, 116))
    out = pd.spread(ids)
    assert sorted(out) == ids and out[:4] == [100, 108, 104, 112]


def test_cli_parses_ingest_dod_flags():
    args = cli_main._parser().parse_args(["ingest-dod", "--limit", "30", "--relist"])
    assert args.command == "ingest-dod" and args.limit == 30 and args.relist
    args = cli_main._parser().parse_args(["ingest-dod"])
    assert args.limit is None and not args.relist


def test_config_has_dod_store_path(tmp_path):
    assert _cfg(tmp_path).dod_db_path == tmp_path / "data" / "dod.sqlite"


@pytest.mark.parametrize("bad", [["ingest-dod", "--limit", "x"]])
def test_cli_rejects_bad_limit(bad):
    with pytest.raises(SystemExit):
        cli_main._parser().parse_args(bad)
