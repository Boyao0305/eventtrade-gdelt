"""ADR 0005 extreme-shock event study: cells, grid, shock scores and outcomes."""

import itertools
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.event_signals import (
    CELLS,
    GRID,
    HORIZONS,
    MIN_PERIODS,
    OUTCOME_COLUMNS,
    OUTCOMES,
    SCORE_COLUMNS,
    THETAS,
    WINDOWS,
    EventCell,
    cell_scores,
    outcome_groups,
    outcomes,
    shock_scores,
)
from eventtrade.core.features import Z_CLIP
from eventtrade.core.labels import make_labels, vol_surprise_label
from eventtrade.schemas import GDELT_DAILY_COLUMNS, Asset

ADR = Path(__file__).resolve().parents[2] / "docs" / "adr" / "0005-extreme-shock-event-study.md"

# --- case tables (ADR 0005, transcribed by hand) -----------------------------------------------

CONFLICT_E1 = ("quad3", "quad4", "root_14", "root_163", "violence")   # ADR order; + low tone
COOP = ("quad1", "quad2")
CONFLICT_E2 = ("quad4", "violence", "root_163", "root_14")             # ADR order
FAMILIES = ("quad1", "quad2", "quad3", "quad4", "root_14", "root_163", "violence")


def _cells_by_hand() -> list[tuple]:
    """(id, hypothesis, track, signal, group, outcome, trigger, split, sign), ADR order."""
    rows = []
    for s in (*CONFLICT_E1, "tone"):                     # E1: 6 signals x dm, em
        for g in ("dm", "em"):
            rows.append((f"E1|{s}|{g}", "E1", "A", s, g, "abnormal_return",
                         "down" if s == "tone" else "up", None, -1.0))
    for s in COOP:                                       # E1c: 2 x dm, em
        for g in ("dm", "em"):
            rows.append((f"E1c|{s}|{g}", "E1c", "A", s, g, "abnormal_return", "up", None, +1.0))
    for s in CONFLICT_E2:                                # E2: 4 x commodity, raw return
        rows.append((f"E2|{s}|commodity", "E2", "A", s, "commodity", "raw_return", "up", None,
                     +1.0))
    for s in (*FAMILIES, "tone"):                        # E3: 8 x dm, em, commodity
        for g in ("dm", "em", "commodity"):
            rows.append((f"E3|{s}|{g}", "E3", "A", s, g, "abnormal_vol",
                         "down" if s == "tone" else "up", None, +1.0))
    rows.append(("E4|org_docs|all", "E4", "B", "org_docs", "all", "abnormal_return", "up",
                 "negative", -1.0))
    rows.append(("E4p|org_docs|all", "E4p", "B", "org_docs", "all", "abnormal_return", "up",
                 "positive", +1.0))
    rows.append(("E5|org_docs|all", "E5", "B", "org_docs", "all", "abnormal_vol", "up", None,
                 +1.0))
    return rows


def _as_row(c: EventCell) -> tuple:
    return (c.id, c.hypothesis, c.track, c.signal, c.group, c.outcome, c.trigger, c.split, c.sign)


# --- cells and grid -------------------------------------------------------------------------------

def test_cells_match_adr_0005_verbatim():
    assert [_as_row(c) for c in CELLS] == _cells_by_hand()
    assert len(CELLS) == 47
    assert len({c.id for c in CELLS}) == 47


def test_cell_counts_per_hypothesis_match_adr():
    n = pd.Series([c.hypothesis for c in CELLS]).value_counts().to_dict()
    assert n == {"E1": 12, "E1c": 4, "E2": 4, "E3": 24, "E4": 1, "E4p": 1, "E5": 1}
    assert n["E1"] + n["E1c"] == 16      # "E1/E1c 8 signals x 2 groups = 16"


def test_hypothesis_signs_and_tracks_match_the_adr_table():
    """Parse the ADR's '| Id | Track | Event -> outcome | Sign |' table from the file itself."""
    text = ADR.read_text(encoding="utf-8")
    signs = {"−": -1.0, "-": -1.0, "+": +1.0}
    table = {
        m.group(1): (m.group(2), signs[m.group(3)])
        for m in re.finditer(r"^\| (E\w+) \| ([AB]) \| .+ \| ([−+-]) \|$", text, re.MULTILINE)
    }
    assert set(table) == {"E1", "E1c", "E2", "E3", "E4", "E4p", "E5"}
    for c in CELLS:
        assert (c.track, c.sign) == table[c.hypothesis], c.id
    assert "**47 cells.**" in text


def test_outcome_kinds_are_the_three_adr_outcomes():
    assert OUTCOMES == ("abnormal_return", "raw_return", "abnormal_vol")
    assert {c.outcome for c in CELLS} == set(OUTCOMES)
    assert {c.hypothesis for c in CELLS if c.outcome == "raw_return"} == {"E2"}


def test_tone_trigger_direction_is_negative():
    tone = [c for c in CELLS if c.signal == "tone"]
    assert len(tone) == 2 + 3        # E1 x {dm, em}, E3 x {dm, em, commodity}
    assert all(c.trigger == "down" for c in tone)
    assert all(c.trigger == "up" for c in CELLS if c.signal != "tone")


def test_grid_is_32_configurations_in_registered_order():
    assert THETAS == (2.0, 2.5, 3.0, 4.0)
    assert WINDOWS == (60, 120)
    assert HORIZONS == (1, 5, 10, 20)
    assert GRID == tuple(itertools.product(THETAS, WINDOWS, HORIZONS))
    assert len(GRID) == 32
    assert len(CELLS) * len(GRID) == 1504


# --- cell_scores: trigger direction and the tone split ------------------------------------------

def _scores(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=SCORE_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _cell(cid: str) -> EventCell:
    return next(c for c in CELLS if c.id == cid)


def test_cell_scores_tone_is_negated_so_low_tone_triggers():
    s = _scores([("2016-01-04", "EWJ", "tone", -3.0), ("2016-01-04", "EWG", "tone", 3.0),
                 ("2016-01-04", "EWJ", "quad3", 2.5)])
    out = cell_scores(s, _cell("E1|tone|dm")).set_index("ticker")["value"]
    assert out.to_dict() == {"EWG": -3.0, "EWJ": 3.0}   # z = -3 -> 3 >= theta: an event
    up = cell_scores(s, _cell("E1|quad3|dm"))
    assert up["value"].tolist() == [2.5] and up["ticker"].tolist() == ["EWJ"]


# Track B split (ADR 0005 "Events"): negative = tone z < 0, positive = tone z >= 0. A spike whose
# tone z is undefined (too short a tone baseline) is in neither split but is still an E5 spike.
SPLIT_CASES = [
    #  attention z, tone z,  in E4,  in E4p, in E5
    (3.0,          -0.5,    True,   False,  True),
    (3.0,           0.0,    False,  True,   True),    # tone z >= 0 is positive
    (3.0,           1.2,    False,  True,   True),
    (3.0,           np.nan, False,  False,  True),    # unsplittable
]


@pytest.mark.parametrize("att, tone, e4, e4p, e5", SPLIT_CASES)
def test_track_b_attention_spike_split_by_same_day_tone(att, tone, e4, e4p, e5):
    rows = [("2016-01-04", "AAPL", "org_docs", att)]
    if not np.isnan(tone):
        rows.append(("2016-01-04", "AAPL", "org_tone", tone))
    s = _scores(rows)
    got = {cid: not cell_scores(s, _cell(cid)).empty
           for cid in ("E4|org_docs|all", "E4p|org_docs|all", "E5|org_docs|all")}
    assert got == {"E4|org_docs|all": e4, "E4p|org_docs|all": e4p, "E5|org_docs|all": e5}
    for cid, hit in got.items():
        if hit:
            assert cell_scores(s, _cell(cid))["value"].tolist() == [att]


def test_cell_scores_track_b_ignores_tone_of_other_days_and_tickers():
    s = _scores([("2016-01-04", "AAPL", "org_docs", 3.0), ("2016-01-05", "AAPL", "org_tone", -1),
                 ("2016-01-04", "MSFT", "org_tone", -1.0)])
    assert cell_scores(s, _cell("E4|org_docs|all")).empty


# --- shock_scores ---------------------------------------------------------------------------------

N_DAYS = 200
TDAYS = list(pd.bdate_range("2015-03-02", periods=N_DAYS).date)


def _a_assets() -> list[Asset]:
    return [
        Asset(ticker="EWJ", name="Japan", kind="etf", track="A", group="country",
              fips_countries=["JA"], screen_group="dm"),
        Asset(ticker="USO", name="Oil", kind="etf", track="A", group="commodity",
              fips_countries=["SA", "IZ"], gkg_themes=["ENV_OIL"], screen_group="commodity"),
        Asset(ticker="AAPL", name="Apple", kind="stock", track="B", group="sp500",
              org_aliases=["apple inc"], screen_group="Information Technology"),
    ]


def _events_daily(seed: int = 0) -> pd.DataFrame:
    """Events rows per (trading day, country): n_events, every family metric, tone."""
    rng = np.random.default_rng(seed)
    rows = []
    metrics = ["quad1", "quad2", "quad3", "quad4", "root_14_protest", "root_163_sanctions",
               "root_18_assault", "root_19_fight", "root_20_massviolence"]
    for d in TDAYS:
        for e in ("JA", "SA", "IZ", "GM"):
            n = float(rng.integers(50, 150))
            rows.append((d, "events", e, "n_events", n, n))
            for m in metrics:
                rows.append((d, "events", e, m, float(rng.integers(0, 20)), n))
            arts = float(rng.integers(100, 400))
            rows.append((d, "events", e, "tone", float(rng.normal(-2, 1)) * arts, arts))
            rows.append((d, "events", e, "goldstein", float(rng.normal()) * n, n))
    return pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS)


def _expected_z(x: pd.Series, i: int, window: int) -> float:
    base = x.iloc[max(0, i - window):i].dropna()
    if len(base) < MIN_PERIODS[window]:
        return np.nan
    sd = base.std(ddof=1)
    return float(np.clip((x.iloc[i] - base.mean()) / sd, -Z_CLIP, Z_CLIP)) if sd >= 1e-9 \
        else np.nan


def _series(daily, ents, metrics, den="n") -> pd.Series:
    """Pooled per-day statistic sum(value_sum of metrics) / sum(n of the first metric)."""
    d = daily[daily["entity"].isin(ents)]
    num = d[d["metric"].isin(metrics)].groupby("session_date")["value_sum"].sum()
    n = d[d["metric"] == metrics[0]].groupby("session_date")["n"].sum()
    s = (num / n).reindex(pd.to_datetime(TDAYS).date)
    return s.reset_index(drop=True)


def _score(out, ticker, signal, day) -> float:
    hit = out[(out["ticker"] == ticker) & (out["signal"] == signal)
              & (out["date"] == pd.Timestamp(day))]
    return float(hit["value"].iloc[0]) if len(hit) else np.nan


@pytest.mark.parametrize("window", WINDOWS)
def test_track_a_family_share_z_pooled_over_producer_countries(window):
    daily = _events_daily()
    out = shock_scores(daily, TDAYS, _a_assets(), "A", window)
    assert list(out.columns) == SCORE_COLUMNS
    assert set(out["ticker"]) == {"EWJ", "USO"}
    assert set(out["signal"]) == {*FAMILIES, "tone"}
    i = N_DAYS - 3
    # USO pools SA + IZ: share = (count SA + count IZ) / (n_events SA + n_events IZ).
    x = _series(daily, ["SA", "IZ"], ["quad4"])
    assert _score(out, "USO", "quad4", TDAYS[i]) == pytest.approx(_expected_z(x, i, window))
    # violence = roots 18 + 19 + 20 summed before dividing by n_events.
    x = _series(daily, ["JA"], ["root_18_assault", "root_19_fight", "root_20_massviolence"])
    assert _score(out, "EWJ", "violence", TDAYS[i]) == pytest.approx(_expected_z(x, i, window))
    # tone: article-weighted over the mapped countries.
    x = _series(daily, ["SA", "IZ"], ["tone"])
    assert _score(out, "USO", "tone", TDAYS[i]) == pytest.approx(_expected_z(x, i, window))


@pytest.mark.parametrize("window", WINDOWS)
def test_scores_start_after_min_periods(window):
    out = shock_scores(_events_daily(), TDAYS, _a_assets(), "A", window)
    first = out.groupby("signal")["date"].min()
    assert (first == pd.Timestamp(TDAYS[MIN_PERIODS[window]])).all()


def test_z_is_clipped_and_floored():
    daily = _events_daily()
    # constant JA quad1 share -> std 0 -> no score; a huge quad3 spike on the last day -> +10.
    m = (daily["entity"] == "JA") & (daily["metric"] == "quad1")
    daily.loc[m, "value_sum"] = daily.loc[m, "n"] * 0.1
    m = ((daily["entity"] == "JA") & (daily["metric"] == "quad3")
         & (daily["session_date"] == TDAYS[-1]))
    daily.loc[m, "value_sum"] = daily.loc[m, "n"]
    out = shock_scores(daily, TDAYS, _a_assets(), "A", 60)
    assert out[(out["ticker"] == "EWJ") & (out["signal"] == "quad1")].empty
    assert _score(out, "EWJ", "quad3", TDAYS[-1]) == Z_CLIP


def test_unknown_window_or_track_raises():
    with pytest.raises(ValueError):
        shock_scores(_events_daily(), TDAYS, _a_assets(), "A", 90)
    with pytest.raises(ValueError):
        shock_scores(_events_daily(), TDAYS, _a_assets(), "C", 60)


def _gkg_daily(seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for d in TDAYS:
        total = float(rng.integers(90_000, 110_000))
        for alias in ("apple inc", "apple"):
            docs = float(rng.integers(0, 30))
            rows.append((d, "gkg", alias, "org_docs", docs, total))
            if docs:
                rows.append((d, "gkg", alias, "org_tone", float(rng.normal(-1, 2)) * docs, docs))
                rows.append((d, "gkg", alias, "org_neg_docs", float(rng.integers(0, docs + 1)),
                             docs))
    return pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS)


def test_track_b_attention_share_and_tone():
    daily = _gkg_daily()
    assets = [a.model_copy(update={"org_aliases": ["apple inc", "apple"]})
              if a.ticker == "AAPL" else a for a in _a_assets()]
    out = shock_scores(daily, TDAYS, assets, "B", 60)
    assert set(out["ticker"]) == {"AAPL"}
    assert set(out["signal"]) == {"org_docs", "org_tone"}
    i = N_DAYS - 1
    docs = daily[daily["metric"] == "org_docs"]
    # attention = docs over all aliases / the day's total (counted once, not per alias)
    att = (docs.groupby("session_date")["value_sum"].sum()
           / docs.groupby("session_date")["n"].max()).reset_index(drop=True)
    assert _score(out, "AAPL", "org_docs", TDAYS[i]) == pytest.approx(_expected_z(att, i, 60))
    tone = _series(daily, ["apple inc", "apple"], ["org_tone"])
    assert _score(out, "AAPL", "org_tone", TDAYS[i]) == pytest.approx(_expected_z(tone, i, 60))


@pytest.mark.parametrize("track", ["A", "B"])
def test_no_future_rows_perturbing_gdelt_after_t_leaves_scores_to_t_unchanged(track):
    daily = _events_daily() if track == "A" else _gkg_daily()
    assets = _a_assets()
    if track == "B":
        assets = [a.model_copy(update={"org_aliases": ["apple inc", "apple"]})
                  if a.ticker == "AAPL" else a for a in assets]
    t = pd.Timestamp(TDAYS[150])
    base = shock_scores(daily, TDAYS, assets, track, 60)
    pert = daily.copy()
    after = pd.to_datetime(pert["session_date"]) > t
    rng = np.random.default_rng(7)
    pert.loc[after, "value_sum"] = pert.loc[after, "value_sum"] * rng.uniform(0, 5, after.sum())
    pert = pert[~(after & (rng.uniform(size=len(pert)) < 0.2))]   # and drop some future rows
    got = shock_scores(pert, TDAYS, assets, track, 60)
    a = base[base["date"] <= t].reset_index(drop=True)
    b = got[got["date"] <= t].reset_index(drop=True)
    assert len(a) > 0
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    assert not base[base["date"] > t].reset_index(drop=True).equals(
        got[got["date"] > t].reset_index(drop=True))


# --- outcomes -------------------------------------------------------------------------------------

P_DAYS = list(pd.bdate_range("2016-01-04", periods=80).date)
P_TICKERS = {"EWJ": "dm", "EWG": "dm", "EWU": "dm", "FXI": "em", "EWZ": "em", "SOLO": "x"}


def _prices(seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for tk in P_TICKERS:
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(P_DAYS))))
        o = c * np.exp(rng.normal(0, 0.003, len(P_DAYS)))
        for d, oo, cc in zip(P_DAYS, o, c):
            rows.append((pd.Timestamp(d), tk, oo, cc))
    return pd.DataFrame(rows, columns=["date", "ticker", "adj_open", "adj_close"])


def _wide_outcome(out, kind, h) -> pd.DataFrame:
    sub = out[(out["outcome"] == kind) & (out["horizon"] == h)]
    return sub.pivot(index="date", columns="ticker", values="value")


def test_outcome_columns_and_horizons():
    out = outcomes(_prices(), P_TICKERS, HORIZONS, P_DAYS)
    assert list(out.columns) == OUTCOME_COLUMNS
    assert set(out["horizon"]) == set(HORIZONS)       # h = 10 built from prices
    assert set(out["outcome"]) == set(OUTCOMES)


@pytest.mark.parametrize("h", HORIZONS)
def test_abnormal_return_excludes_the_asset_itself_from_its_group_mean(h):
    prices = _prices()
    out = outcomes(prices, P_TICKERS, [h], P_DAYS)
    lab = make_labels(prices, [h], P_DAYS).pivot(index="date", columns="ticker",
                                                 values="ret_oo")
    abn = _wide_outcome(out, "abnormal_return", h)
    raw = _wide_outcome(out, "raw_return", h)
    t = lab.dropna().index[5]
    r = lab.loc[t]
    assert abn.loc[t, "EWJ"] == pytest.approx(r["EWJ"] - (r["EWG"] + r["EWU"]) / 2)
    assert abn.loc[t, "FXI"] == pytest.approx(r["FXI"] - r["EWZ"])
    assert "SOLO" not in abn.columns          # no other member -> undefined
    pd.testing.assert_series_equal(raw.loc[t], r.rename(t), check_names=False)
    assert raw.loc[t, "SOLO"] == pytest.approx(r["SOLO"])


def test_abnormal_return_uses_only_members_with_a_return():
    prices = _prices()
    t = P_DAYS[10]
    h = 5
    # drop EWU's bar at t+1: its ret_oo(t) is NaN, so EWJ's peer mean is EWG alone.
    prices = prices[~((prices["ticker"] == "EWU") & (prices["date"] == pd.Timestamp(P_DAYS[11])))]
    out = outcomes(prices, P_TICKERS, [h], P_DAYS)
    lab = make_labels(prices, [h], P_DAYS).pivot(index="date", columns="ticker", values="ret_oo")
    abn = _wide_outcome(out, "abnormal_return", h)
    r = lab.loc[pd.Timestamp(t)]
    assert np.isnan(r["EWU"])
    assert abn.loc[pd.Timestamp(t), "EWJ"] == pytest.approx(r["EWJ"] - r["EWG"])


def _vol_ratio_by_hand(prices: pd.DataFrame, h: int) -> pd.DataFrame:
    """ln(RMS open-to-open log return over opens t+1..t+1+h / RMS close-to-close over t-21..t)."""
    o = prices.pivot(index="date", columns="ticker", values="adj_open")
    c = prices.pivot(index="date", columns="ticker", values="adj_close")
    out = pd.DataFrame(np.nan, index=o.index, columns=o.columns)
    for i in range(22, len(o) - h - 1):
        ro = np.log(o.iloc[i + 2: i + 2 + h].to_numpy() / o.iloc[i + 1: i + 1 + h].to_numpy())
        rc = np.log(c.iloc[i - 21: i + 1].to_numpy() / c.iloc[i - 22: i].to_numpy())
        out.iloc[i] = 0.5 * np.log((ro ** 2).mean(axis=0)) - 0.5 * np.log((rc ** 2).mean(axis=0))
    return out


@pytest.mark.parametrize("h", HORIZONS)
def test_abnormal_vol_is_vol_ratio_minus_group_median(h):
    # forward leg open-to-open from the t+1 open (entry), trailing leg close-to-close to t
    prices = _prices()
    out = outcomes(prices, P_TICKERS, [h], P_DAYS)
    v = _vol_ratio_by_hand(prices, h)
    av = _wide_outcome(out, "abnormal_vol", h)
    t = v.dropna().index[3]
    dm = v.loc[t, ["EWJ", "EWG", "EWU"]]
    assert av.loc[t, "EWU"] == pytest.approx(v.loc[t, "EWU"] - dm.median())
    em = v.loc[t, ["FXI", "EWZ"]]
    assert av.loc[t, "EWZ"] == pytest.approx(v.loc[t, "EWZ"] - em.median())
    assert "SOLO" not in av.columns          # a group of one has no abnormal vol


@pytest.mark.parametrize("h", HORIZONS)
def test_pre_entry_overnight_gap_does_not_enter_abnormal_vol(h):
    """A jump between the close at t and the open at t+1 happens before entry (ADR 0005:
    entered at t+1 open): neither abnormal return nor abnormal volatility at t may move."""
    prices = _prices()
    i = 30
    t = pd.Timestamp(P_DAYS[i])
    gap = prices.copy()
    after = (gap["ticker"] == "EWJ") & (gap["date"] >= pd.Timestamp(P_DAYS[i + 1]))
    gap.loc[after, ["adj_open", "adj_close"]] *= 1.05  # +5% overnight, then the same path
    base = outcomes(prices, P_TICKERS, [h], P_DAYS)
    got = outcomes(gap, P_TICKERS, [h], P_DAYS)
    for kind in ("abnormal_return", "abnormal_vol"):
        a = base[(base["date"] == t) & (base["outcome"] == kind)].reset_index(drop=True)
        b = got[(got["date"] == t) & (got["outcome"] == kind)].reset_index(drop=True)
        assert len(a) == 5
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-9, atol=1e-12)
    # the old close-to-close forward leg would have moved (the leak this test pins)
    old = vol_surprise_label(gap, h, P_DAYS).pivot(index="date", columns="ticker", values="value")
    ref = vol_surprise_label(prices, h, P_DAYS).pivot(index="date", columns="ticker",
                                                       values="value")
    assert abs(old.loc[t, "EWJ"] - ref.loc[t, "EWJ"]) > 0.1


@pytest.mark.parametrize("h", HORIZONS)
def test_outcome_at_t_ignores_prices_after_t_plus_h_plus_1(h):
    """Documented window: opens t+1..t+1+h (returns and forward vol), closes t-22..t (trailing
    vol), all members."""
    prices = _prices()
    i = 30
    t = pd.Timestamp(P_DAYS[i])
    base = outcomes(prices, P_TICKERS, [h], P_DAYS)
    pert = prices.copy()
    later = pert["date"] > pd.Timestamp(P_DAYS[i + h + 1])
    pert.loc[later, ["adj_open", "adj_close"]] *= 1.7
    got = outcomes(pert, P_TICKERS, [h], P_DAYS)
    a = base[base["date"] == t].reset_index(drop=True)
    b = got[got["date"] == t].reset_index(drop=True)
    assert len(a) > 0
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    # ... and the open at t+1+h does matter (the window is not shorter than documented).
    pert2 = prices.copy()
    edge = pert2["date"] == pd.Timestamp(P_DAYS[i + h + 1])
    pert2.loc[edge, "adj_open"] *= 1.7
    got2 = outcomes(pert2, P_TICKERS, [h], P_DAYS)
    c = got2[(got2["date"] == t) & (got2["outcome"] == "raw_return")]
    d = base[(base["date"] == t) & (base["outcome"] == "raw_return")]
    assert not np.allclose(c["value"].to_numpy(), d["value"].to_numpy())


def test_outcome_groups_from_assets():
    assets = [
        *_a_assets(),
        Asset(ticker="SPY", name="SPY", kind="etf", track="A", group="market",
              fips_countries=["US"]),
        Asset(ticker="XLF", name="Fin", kind="etf", track="A", group="sector",
              screen_group=None),
        Asset(ticker="JPM", name="JPM", kind="stock", track="B", group="sp500",
              screen_group="Financials"),
    ]
    assert outcome_groups(assets, "A") == {"EWJ": "dm", "USO": "commodity"}
    assert outcome_groups(assets, "B") == {"AAPL": "Information Technology",
                                           "JPM": "Financials"}
