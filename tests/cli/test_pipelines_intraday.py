"""ADR 0007 intraday ingest: resumable per (ticker, year), hold-out refused, symbols mapped."""

from datetime import UTC, date, datetime

import pandas as pd

from eventtrade.adapters.intraday_store import IntradayStore
from eventtrade.cli import pipelines_intraday as pi
from eventtrade.config import Config
from eventtrade.core import intraday
from eventtrade.schemas import INTRADAY_COLUMNS, Asset

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _asset(t: str, track: str, group: str | None) -> Asset:
    return Asset(ticker=t, name=t, kind="etf" if track == "A" else "stock", track=track,
                 group="country" if track == "A" else "stock", screen_group=group)


class _Store:
    def __init__(self, assets):
        self.assets = assets

    def read_assets(self, track=None):
        return [a for a in self.assets if track is None or a.track == track]


def _fake_fetch(calls):
    def fetch(symbols, start, end):
        calls.append((tuple(symbols), start, end))
        ts = int(datetime(start.year, start.month, start.day, 15, 0, tzinfo=UTC).timestamp())
        return pd.DataFrame([[ts, symbols[0], 1, 1, 1, 1, 10, 1, 3]], columns=INTRADAY_COLUMNS)
    return fetch


def _cfg(tmp_path) -> Config:
    return Config(home=tmp_path, gcp_key_path=tmp_path / "k.json")


def test_resumes_and_maps_class_shares(tmp_path):
    istore = IntradayStore(tmp_path / "i.sqlite")
    calls: list = []
    out: list[str] = []
    rc = pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), istore, _fake_fetch(calls),
                                   ["BRK-B"], date(2016, 1, 4), date(2017, 6, 30), lambda: NOW,
                                   out.append)
    assert rc == pi.OK
    assert calls == [(("BRK.B",), date(2016, 1, 4), date(2016, 12, 31)),
                     (("BRK.B",), date(2017, 1, 1), date(2017, 6, 30))]
    assert istore.fetched_years() == {("BRK-B", 2016), ("BRK-B", 2017)}
    assert set(istore.read_bars(None, None, None)["ticker"]) == {"BRK-B"}
    calls.clear()
    pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), istore, _fake_fetch(calls), ["BRK-B"],
                              date(2016, 1, 4), date(2017, 6, 30), lambda: NOW, out.append)
    assert calls == []  # everything already in the ledger


def test_refuses_holdout(tmp_path):
    rc = pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), IntradayStore(tmp_path / "i.sqlite"),
                                   _fake_fetch([]), ["SPY"], None, date(2025, 4, 1),
                                   lambda: NOW, lambda s: None)
    assert rc == pi.REFUSED


def test_default_order_hedges_then_a_then_b_and_missing_universe(tmp_path):
    assets = [_asset("ZZZ", "B", "Energy"), _asset("EWJ", "A", "dm"), _asset("SPY", "A", None),
              _asset("NOPE", "B", None), _asset("USO", "A", "commodity")]
    order = intraday.fetch_order(assets)
    assert order[:len(intraday.HEDGES)] == list(intraday.HEDGES)
    assert order[len(intraday.HEDGES):] == ["EWJ", "USO", "ZZZ"]
    rc = pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), IntradayStore(tmp_path / "i.sqlite"),
                                   _fake_fetch([]), None, None, None, lambda: NOW, lambda s: None)
    assert rc == pi.MISSING


def test_max_tickers_limits_a_trial_run(tmp_path):
    calls: list = []
    pi.ingest_intraday_prices(_cfg(tmp_path), _Store([]), IntradayStore(tmp_path / "i.sqlite"),
                              _fake_fetch(calls), ["A", "B", "C"], date(2016, 1, 4),
                              date(2016, 2, 1), lambda: NOW, lambda s: None, max_tickers=2)
    assert [c[0] for c in calls] == [("A",), ("B",)]


# --- ADR 0007: 15-minute GDELT pulls --------------------------------------------------------

class _GStore(_Store):
    def __init__(self, assets, spent=0):
        super().__init__(assets)
        self.spent = spent

    def month_to_date_bytes(self, now):
        return self.spent


class _Budget(Exception):
    pass


class _FakeGdelt:
    def __init__(self, est=10**9, refuse_after=None):
        self.est = est
        self.refuse_after = refuse_after
        self.estimates: list = []
        self.pulls: list = []

    def estimate(self, query_name, start, end, **params):
        self.estimates.append((query_name, start, end, params))
        return self.est

    def _check(self):
        if self.refuse_after is not None and len(self.pulls) > self.refuse_after:
            raise _Budget("monthly budget")

    @staticmethod
    def _ts(d):
        return int(datetime(d.year, d.month, d.day, 14, 0, tzinfo=UTC).timestamp())

    def pull_events_15min(self, start, end, countries):
        from eventtrade.adapters.intraday_store import EVENTS_15MIN_COLUMNS

        self.pulls.append(("events", start, end, list(countries)))
        self._check()
        row = ["_GLOBAL", self._ts(start), 5, 1, 9.0, -3.0]
        return pd.DataFrame([row], columns=EVENTS_15MIN_COLUMNS), 7

    def pull_orgs_15min(self, start, end, aliases, alias_tickers):
        from eventtrade.adapters.intraday_store import ORGS_15MIN_COLUMNS

        self.pulls.append(("orgs", start, end, list(zip(aliases, alias_tickers, strict=True))))
        self._check()
        return pd.DataFrame([["AAPL", self._ts(start), 2, -1.5]], columns=ORGS_15MIN_COLUMNS), 11

    def pull_events_15min_cat(self, start, end, countries, cameo_codes, cameo_fips):
        from eventtrade.adapters.intraday_store import EVENTS_15MIN_CAT_COLUMNS

        self.pulls.append(("events_cat", start, end,
                           (list(countries), list(zip(cameo_codes, cameo_fips, strict=True)))))
        self._check()
        rows = [[e, g, self._ts(start), 4, 1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 9.0, -3.0]
                for e, g in (("JA", "action"), ("JA", "actor"), ("_GLOBAL", "action"))]
        return pd.DataFrame(rows, columns=EVENTS_15MIN_CAT_COLUMNS), 13


def _universe():
    ja = _asset("EWJ", "A", "dm").model_copy(update={"fips_countries": ["JA"]})
    uso = _asset("USO", "A", "commodity").model_copy(update={"fips_countries": ["SA", "US"]})
    xlb = _asset("XLB", "A", None).model_copy(update={"fips_countries": ["ZZ"]})  # not a trial
    aapl = _asset("AAPL", "B", "Information Technology").model_copy(
        update={"org_aliases": ["Apple Inc", "apple"]})
    goog = _asset("GOOG", "B", "Communication Services").model_copy(
        update={"org_aliases": ["alphabet"]})
    nope = _asset("NOPE", "B", None).model_copy(update={"org_aliases": ["nope"]})
    return [ja, uso, xlb, aapl, goog, nope]


# (CAMEO, FIPS) rows: JA/SA/US are the universe's countries; GM is not and is left out.
CAMEO = [("USA", "US"), ("JPN", "JA"), ("DEU", "GM"), ("SAU", "SA")]


def _run15(tmp_path, gdelt, dataset, start=None, end=None, execute=False, store=None, out=None,
           cameo=None):
    out = [] if out is None else out
    return pi.ingest_gdelt_15min(
        _cfg(tmp_path), store or _GStore(_universe()), IntradayStore(tmp_path / "i.sqlite"),
        gdelt, dataset, start, end, execute, lambda: NOW, _Budget, out.append, cameo_fips=cameo)


def test_period_chunks_years_and_months():
    assert pi.period_chunks(date(2015, 12, 1), date(2017, 3, 31), 12) == [
        (date(2015, 12, 1), date(2015, 12, 31)), (date(2016, 1, 1), date(2016, 12, 31)),
        (date(2017, 1, 1), date(2017, 3, 31))]
    assert pi.period_chunks(date(2016, 1, 15), date(2016, 3, 2), 1) == [
        (date(2016, 1, 15), date(2016, 1, 31)), (date(2016, 2, 1), date(2016, 2, 29)),
        (date(2016, 3, 1), date(2016, 3, 2))]
    assert pi.period_chunks(date(2016, 11, 5), date(2017, 1, 1), 3) == [
        (date(2016, 11, 5), date(2016, 12, 31)), (date(2017, 1, 1), date(2017, 1, 1))]


def test_15min_params_from_track_a_and_b_trial_assets():
    assets = _universe()
    assert pi.gdelt_15min_params(assets, "events") == {"countries": ["JA", "SA", "US"]}
    p = pi.gdelt_15min_params(assets, "orgs")
    assert list(zip(p["aliases"], p["alias_tickers"], strict=True)) == [
        ("alphabet", "GOOG"), ("apple", "AAPL"), ("apple inc", "AAPL")]
    assert pi.gdelt_15min_params([], "events") is None
    assert pi.gdelt_15min_params(assets[:3], "orgs") is None


def test_15min_dry_run_default_range_estimates_every_year(tmp_path):
    g, out = _FakeGdelt(est=2 * 10**9), []
    assert _run15(tmp_path, g, "events", out=out) == pi.OK
    spans = [(a, b) for _, a, b, _ in g.estimates]
    assert spans[0] == (date(2015, 12, 1), date(2015, 12, 31))
    assert spans[-1] == (date(2025, 1, 1), date(2025, 3, 31))  # never into the hold-out
    assert len(spans) == 11 and g.pulls == []
    assert {q for q, *_ in g.estimates} == {"events_15min"}
    assert g.estimates[0][3] == {"countries": ["JA", "SA", "US"]}
    assert any("estimate 22.00 GB" in line for line in out)
    assert IntradayStore(tmp_path / "i.sqlite").pulled_chunks("events_15min") == set()


def test_15min_execute_writes_chunks_then_resume_skips_them(tmp_path):
    g = _FakeGdelt()
    rc = _run15(tmp_path, g, "orgs", date(2016, 6, 1), date(2017, 2, 28), execute=True)
    assert rc == pi.OK
    assert [(a, b) for _, a, b, _ in g.pulls] == [(date(2016, 6, 1), date(2016, 12, 31)),
                                                  (date(2017, 1, 1), date(2017, 2, 28))]
    assert g.pulls[0][3] == [("alphabet", "GOOG"), ("apple", "AAPL"), ("apple inc", "AAPL")]
    istore = IntradayStore(tmp_path / "i.sqlite")
    assert istore.pulled_chunks("gkg_orgs_15min") == {(date(2016, 6, 1), date(2016, 12, 31)),
                                                      (date(2017, 1, 1), date(2017, 2, 28))}
    assert len(istore.read_gdelt("orgs_15min", None, None)) == 2
    g2 = _FakeGdelt()
    assert _run15(tmp_path, g2, "orgs", date(2016, 6, 1), date(2017, 2, 28),
                  execute=True) == pi.OK
    assert g2.pulls == []
    assert _run15(tmp_path, g2, "orgs", date(2016, 6, 1), date(2017, 2, 28)) == pi.OK
    assert g2.estimates == []  # the dry run skips pulled chunks too


def test_15min_budget_refusal_stops_cleanly_and_resumes(tmp_path):
    g, out = _FakeGdelt(refuse_after=1), []
    rc = _run15(tmp_path, g, "events", date(2016, 1, 1), date(2018, 12, 31), execute=True,
                out=out)
    assert rc == pi.BUDGET
    istore = IntradayStore(tmp_path / "i.sqlite")
    assert istore.pulled_chunks("events_15min") == {(date(2016, 1, 1), date(2016, 12, 31))}
    assert any("refused by budget guard at 2017-01-01..2017-12-31" in line for line in out)
    g2 = _FakeGdelt()
    assert _run15(tmp_path, g2, "events", date(2016, 1, 1), date(2018, 12, 31),
                  execute=True) == pi.OK
    assert [a.year for _, a, _, _ in g2.pulls] == [2017, 2018]
    rows = istore.read_gdelt("events_15min", None, None)
    assert len(rows) == 3 and set(rows["entity"]) == {"_GLOBAL"}


def test_15min_refuses_holdout_and_missing_universe(tmp_path):
    g = _FakeGdelt()
    assert _run15(tmp_path, g, "events", end=date(2025, 4, 1)) == pi.REFUSED
    assert _run15(tmp_path, g, "orgs", date(2016, 1, 1), date(2015, 1, 1)) == pi.REFUSED
    assert _run15(tmp_path, g, "orgs", store=_GStore([])) == pi.MISSING
    assert g.estimates == [] and g.pulls == []


# --- ADR 0008: events by category -------------------------------------------------------------

CAT_PARAMS = {"countries": ["JA", "SA", "US"], "cameo_codes": ["JPN", "SAU", "USA"],
              "cameo_fips": ["JA", "SA", "US"]}


def test_15min_params_events_cat_maps_only_universe_countries():
    assert pi.gdelt_15min_params(_universe(), "events_cat", CAMEO) == CAT_PARAMS
    assert pi.gdelt_15min_params([], "events_cat", CAMEO) is None


def test_15min_events_cat_dry_run_default_range(tmp_path):
    g, out = _FakeGdelt(est=3 * 10**9), []
    assert _run15(tmp_path, g, "events_cat", out=out, cameo=CAMEO) == pi.OK
    spans = [(a, b) for _, a, b, _ in g.estimates]
    assert spans[0] == (date(2015, 12, 1), date(2015, 12, 31))
    assert spans[-1] == (date(2025, 1, 1), date(2025, 3, 31))  # never into the hold-out
    assert len(spans) == 11 and g.pulls == []
    assert {q for q, *_ in g.estimates} == {"events_15min_cat"}
    assert g.estimates[0][3] == CAT_PARAMS  # the estimate binds what the pull would
    assert any("3 countries, 3 cameo_codes; 11 chunk(s)" in line for line in out)
    assert any("estimate 33.00 GB" in line for line in out)
    assert IntradayStore(tmp_path / "i.sqlite").pulled_chunks("events_15min_cat") == set()


def test_15min_events_cat_execute_resume_and_budget_refusal(tmp_path):
    g, out = _FakeGdelt(refuse_after=1), []
    rc = _run15(tmp_path, g, "events_cat", date(2016, 1, 1), date(2018, 12, 31), execute=True,
                out=out, cameo=CAMEO)
    assert rc == pi.BUDGET
    assert g.pulls[0][3] == (CAT_PARAMS["countries"], [("JPN", "JA"), ("SAU", "SA"),
                                                       ("USA", "US")])
    istore = IntradayStore(tmp_path / "i.sqlite")
    assert istore.pulled_chunks("events_15min_cat") == {(date(2016, 1, 1), date(2016, 12, 31))}
    assert istore.pulled_chunks("events_15min") == set()  # separate ledger key
    assert any("refused by budget guard at 2017-01-01..2017-12-31" in line for line in out)
    g2 = _FakeGdelt()
    assert _run15(tmp_path, g2, "events_cat", date(2016, 1, 1), date(2018, 12, 31),
                  execute=True, cameo=CAMEO) == pi.OK
    assert [a.year for _, a, _, _ in g2.pulls] == [2017, 2018]
    rows = istore.read_gdelt("events_15min_cat", None, None)
    assert len(rows) == 9 and set(zip(rows["entity"], rows["geo"], strict=True)) == {
        ("JA", "action"), ("JA", "actor"), ("_GLOBAL", "action")}
    assert istore.read_gdelt("events_15min", None, None).empty
    g3 = _FakeGdelt()
    assert _run15(tmp_path, g3, "events_cat", date(2016, 1, 1), date(2018, 12, 31),
                  cameo=CAMEO) == pi.OK
    assert g3.estimates == [] and g3.pulls == []  # everything pulled: nothing to estimate


def test_15min_events_cat_refuses_without_a_complete_mapping(tmp_path):
    g, out = _FakeGdelt(), []
    assert _run15(tmp_path, g, "events_cat", out=out) == pi.MISSING  # no table at all
    assert _run15(tmp_path, g, "events_cat", cameo=[("JPN", "JA"), ("USA", "US")],
                  out=out) == pi.MISSING  # SA has no CAMEO code
    assert any("no CAMEO code for FIPS SA" in line for line in out)
    assert _run15(tmp_path, g, "events_cat", end=date(2025, 4, 1), cameo=CAMEO) == pi.REFUSED
    assert _run15(tmp_path, g, "events_cat", store=_GStore([]), cameo=CAMEO) == pi.MISSING
    assert g.estimates == [] and g.pulls == []



def test_lookahead_and_holdout_trials_do_not_count_for_dsr():
    from eventtrade.cli.pipelines import counts_for_dsr
    from eventtrade.schemas import Trial

    def tr(stage, params):
        return Trial(trial_id="x", created_at=NOW, track="A", stage=stage, hypothesis="O1",
                     params=params, data_start=date(2016, 1, 4), data_end=date(2019, 12, 31))

    assert counts_for_dsr(tr("signal", {}))
    assert not counts_for_dsr(tr("signal", {"lookahead": True}))
    assert not counts_for_dsr(tr("holdout", {}))
