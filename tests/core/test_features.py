import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from feature_cases import (
    AGG2_CASES,
    MAP_ETF_MEAN,
    MAP_ETF_MEAN_OF_MEANS,
    MAP_EXPECTED,
    MAP_ROWS,
    REAGG_EXPECTED,
    REAGG_ROWS,
    REAGG_WRONG_MEAN_OF_MEANS,
    ROLL_FORWARD_CASES,
    SESSION_DATE_CASES,
    STAT_CASES,
    STAT_DAYS,
    STAT_ROWS,
    SURPRISE_CASES,
    TRADING_DAYS,
)

from eventtrade.core.features import (
    GLOBAL,
    MetricSpec,
    build_features,
    entity_features,
    map_to_tickers,
    product_feature,
    session_date_of,
    surprise,
    themes_as_entities,
    to_trading_days,
)
from eventtrade.schemas import FEATURE_COLUMNS, GDELT_DAILY_COLUMNS, Asset


def _daily(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS)


def _same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)


ASSETS = [
    Asset(ticker="AAPL", name="Apple", kind="stock", track="B", group="sp500",
          org_aliases=["apple", "apple inc"]),
    Asset(ticker="EWJ", name="Japan", kind="etf", track="A", group="country", fips_countries=["JA"]),
    Asset(ticker="AAXJ", name="Asia", kind="etf", track="A", group="country",
          fips_countries=["JA", "KS"]),
    Asset(ticker="XLE", name="Energy", kind="etf", track="A", group="sector",
          gkg_themes=["ENV_OIL"]),
]


# --- cutoff and roll-forward -----------------------------------------------------------------

@pytest.mark.parametrize("added_utc,expected", SESSION_DATE_CASES)
def test_session_date_of(added_utc, expected):
    assert session_date_of(added_utc) == expected


@pytest.mark.parametrize("session,expected", ROLL_FORWARD_CASES)
def test_roll_forward(session, expected):
    out = to_trading_days(_daily([(session, "events", "JA", "m", 1.0, 1)]), TRADING_DAYS)
    if expected is None:
        assert out.empty
    else:
        assert out["session_date"].tolist() == [pd.Timestamp(expected)]


def test_friday_after_cutoff_lands_on_monday():
    """The whole chain: Fri 16:00 ET -> Saturday session_date -> Monday trading day."""
    sd = session_date_of(pd.Timestamp("2024-07-05 20:00").to_pydatetime())
    out = to_trading_days(_daily([(sd, "events", "JA", "m", 1.0, 1)]), TRADING_DAYS)
    assert out["session_date"].iloc[0] == pd.Timestamp(date(2024, 7, 8))


# --- re-aggregation exactness ------------------------------------------------------------------

def test_reaggregation_is_exact():
    rows = [(d, "gkg", "JA", "tone", v, n) for d, v, n in REAGG_ROWS]
    out = to_trading_days(_daily(rows), TRADING_DAYS)
    day, vs, n, mean = REAGG_EXPECTED
    assert len(out) == 1 and list(out.columns) == GDELT_DAILY_COLUMNS
    assert out["session_date"].iloc[0] == pd.Timestamp(day)
    assert (out["value_sum"].iloc[0], out["n"].iloc[0]) == (vs, n)
    wide = entity_features(out, [MetricSpec("gkg", "tone", "mean")], TRADING_DAYS)
    got = wide.loc[wide["date"] == pd.Timestamp(day), "gkg.tone.mean"].iloc[0]
    assert got == mean and not _same(got, REAGG_WRONG_MEAN_OF_MEANS)


def test_reaggregation_keeps_keys_apart():
    rows = [(date(2024, 7, 6), "events", "JA", "m", 1.0, 1),
            (date(2024, 7, 6), "events", "KS", "m", 2.0, 2),
            (date(2024, 7, 6), "gkg", "JA", "m", 4.0, 4),
            (date(2024, 7, 7), "events", "JA", "m2", 8.0, 8)]
    out = to_trading_days(_daily(rows), TRADING_DAYS)
    assert len(out) == 4 and out["value_sum"].sum() == 15.0


# --- statistics --------------------------------------------------------------------------------

@pytest.mark.parametrize("stat,entity,expected", STAT_CASES)
def test_stat_cases(stat, entity, expected):
    rows = [(STAT_DAYS[i], "events", e, "m", v, n) for i, e, v, n in STAT_ROWS]
    spec = MetricSpec("events", "m", stat)
    wide = entity_features(_daily(rows), [spec], STAT_DAYS)
    got = wide[wide["entity"] == entity].sort_values("date")[spec.column].tolist()
    assert len(got) == len(expected)
    assert all(_same(g, e) for g, e in zip(got, expected)), (got, expected)


@pytest.mark.parametrize("stat,entity,expected", AGG2_CASES)
def test_agg_days_sums_raw_first(stat, entity, expected):
    rows = [(STAT_DAYS[i], "events", e, "m", v, n) for i, e, v, n in STAT_ROWS]
    spec = MetricSpec("events", "m", stat, agg_days=2)
    wide = entity_features(_daily(rows), [spec], STAT_DAYS)
    got = wide[wide["entity"] == entity].sort_values("date")[spec.column].tolist()
    assert all(_same(g, e) for g, e in zip(got, expected)), (got, expected)


def test_share_with_other_denominator():
    d = STAT_DAYS[0]
    rows = [(d, "events", GLOBAL, "all", 0.0, 1000), (d, "events", GLOBAL, "fight", 0.0, 10),
            (d, "events", "JA", "fight", 0.0, 5)]
    spec = MetricSpec("events", "fight", "share", denominator_metric="all")
    wide = entity_features(_daily(rows), [spec], [d])
    assert wide.loc[wide["entity"] == "JA", spec.column].iloc[0] == 5 / 1000


# --- surprise ----------------------------------------------------------------------------------

@pytest.mark.parametrize("values,window,minp,shift,z,lr", SURPRISE_CASES)
def test_surprise_cases(values, window, minp, shift, z, lr):
    days = pd.bdate_range("2024-01-01", periods=len(values))
    frame = pd.DataFrame({"date": days, "entity": "JA", "x": values})
    out = surprise(frame, window, minp, shift=shift)
    assert all(_same(g, e) for g, e in zip(out[f"x_z.{window}"], z)), out[f"x_z.{window}"].tolist()
    assert all(_same(g, e) for g, e in zip(out[f"x_lr.{window}"], lr))


def test_surprise_baseline_is_per_entity():
    days = pd.bdate_range("2024-01-01", periods=4)
    frame = pd.DataFrame({"date": list(days) * 2, "entity": ["A"] * 4 + ["B"] * 4,
                          "x": [1.0, 2.0, 3.0, 10.0, 100.0, 200.0, 300.0, 400.0]})
    out = surprise(frame, 3, 3)
    assert out.loc[(out["entity"] == "A") & (out["date"] == days[3]), "x_z.3"].iloc[0] == 8.0
    assert out.loc[(out["entity"] == "B") & (out["date"] == days[3]), "x_z.3"].iloc[0] == 2.0


def test_surprise_rejects_baseline_containing_t():
    frame = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=3), "entity": "A",
                          "x": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError):
        surprise(frame, 2, 2, shift=0)


# --- mapping to tickers ------------------------------------------------------------------------

def test_map_to_tickers_sums_raw():
    d = STAT_DAYS[0]
    rows = [(d, "gkg", e, "tone", v, n) for e, v, n in MAP_ROWS] + [
        (d, "gkg", GLOBAL, "tone", 1.0, 100)]
    out = map_to_tickers(_daily(rows), ASSETS)
    assert list(out.columns) == GDELT_DAILY_COLUMNS
    got = {r.entity: (r.value_sum, r.n) for r in out.itertuples() if r.entity != GLOBAL}
    assert got == MAP_EXPECTED
    assert out.loc[out["entity"] == GLOBAL, "n"].iloc[0] == 100
    wide = entity_features(out, [MetricSpec("gkg", "tone", "mean")], [d])
    aaxj = wide.loc[wide["entity"] == "AAXJ", "gkg.tone.mean"].iloc[0]
    assert aaxj == MAP_ETF_MEAN and aaxj != MAP_ETF_MEAN_OF_MEANS


def test_map_ignores_duplicate_entities_in_one_asset():
    d = STAT_DAYS[0]
    a = Asset(ticker="DUP", name="d", kind="etf", track="A", group="country",
              fips_countries=["JA", "JA"])
    out = map_to_tickers(_daily([(d, "events", "JA", "m", 1.0, 1)]), [a])
    assert out["n"].tolist() == [1]


# --- build_features: naming, contract, no look-ahead -------------------------------------------

SPECS = [
    MetricSpec("events", "root_19_fight", "share", transforms=("z", "lr", "level"), window=20,
               min_periods=10),
    MetricSpec("gkg", "tone", "mean", window=20, min_periods=10),
    MetricSpec("gkg", "tone", "mean", window=20, min_periods=10, agg_days=5),
    MetricSpec("gkg", "tone", "count", transforms=("lr",), window=20, min_periods=10),
]


def _random_daily(seed: int, start="2023-01-01", periods=200) -> tuple[pd.DataFrame, list[date]]:
    """Calendar days incl. weekends; trading days = weekdays minus a few holidays."""
    rng = np.random.default_rng(seed)
    cal = pd.date_range(start, periods=periods, freq="D")
    rows = []
    for d in cal:
        for src, metric in (("events", "root_19_fight"), ("gkg", "tone")):
            g_n = int(rng.integers(500, 1000))
            rows.append((d.date(), src, GLOBAL, metric, float(rng.normal(-1, 1) * g_n), g_n))
            for e in ("JA", "KS", "apple", "apple inc", "ENV_OIL"):
                if rng.random() < 0.8:
                    n = int(rng.integers(1, 30))
                    rows.append((d.date(), src, e, metric, float(rng.normal(-1, 2) * n), n))
    holidays = {cal[40].date(), cal[100].date()}
    tdays = [d.date() for d in cal if d.weekday() < 5 and d.date() not in holidays]
    return _daily(rows), tdays


@pytest.fixture(scope="module")
def random_case():
    daily, tdays = _random_daily(1)
    return daily, tdays, build_features(daily, tdays, ASSETS, SPECS)


def test_build_contract_and_names(random_case):
    _, tdays, feats = random_case
    assert list(feats.columns) == FEATURE_COLUMNS
    assert not feats.duplicated(["date", "ticker", "feature"]).any()
    assert np.isfinite(feats["value"]).all()
    assert set(feats["ticker"]) == {"AAPL", "EWJ", "AAXJ", "XLE"}
    assert set(feats["date"]) <= {pd.Timestamp(d) for d in tdays}
    assert {"events.root_19_fight.share_z.20", "events.root_19_fight.share_lr.20",
            "events.root_19_fight.share.0", "gkg.tone.mean_z.20", "gkg.tone.mean5d_z.20",
            "gkg.tone.count_lr.20"} == set(feats["feature"])


@pytest.mark.parametrize("cut_index", [30, 60, 120])
def test_no_future_row_changes_past_features(cut_index):
    """Perturb every raw row whose session_date is after trading day t: features <= t unchanged."""
    daily, tdays = _random_daily(7)
    base = build_features(daily, tdays, ASSETS, SPECS)
    t = pd.Timestamp(tdays[cut_index])
    rng = np.random.default_rng(99)
    pert = daily.copy()
    future = pd.to_datetime(pert["session_date"]) > t
    pert.loc[future, "value_sum"] = rng.normal(5, 10, future.sum())
    pert.loc[future, "n"] = rng.integers(1, 5000, future.sum())
    # also add brand-new rows for an existing entity in the future
    extra = _daily([(tdays[-1], "events", "JA", "root_19_fight", 1e6, 1)])
    pert = pd.concat([pert, extra], ignore_index=True)
    after = build_features(pert, tdays, ASSETS, SPECS)
    past_b = base[base["date"] <= t].reset_index(drop=True)
    past_a = after[after["date"] <= t].reset_index(drop=True)
    pd.testing.assert_frame_equal(past_b, past_a)
    # the perturbation is not vacuous: later features do change
    fut_b = base[base["date"] > t].reset_index(drop=True)
    fut_a = after[after["date"] > t].reset_index(drop=True)
    assert not fut_b.equals(fut_a)


def test_day_t_rows_reach_day_t_features():
    """Not lagged by accident: changing only day t's rows changes day t's surprise."""
    daily, tdays = _random_daily(3)
    t = pd.Timestamp(tdays[80])
    base = build_features(daily, tdays, ASSETS, SPECS)
    pert = daily.copy()
    on_t = (pd.to_datetime(pert["session_date"]) == t) & (pert["entity"] == "JA")
    pert.loc[on_t, "n"] *= 10
    after = build_features(pert, tdays, ASSETS, SPECS)
    key = (base["date"] == t) & (base["ticker"] == "EWJ") & (
        base["feature"] == "events.root_19_fight.share_z.20")
    key_a = (after["date"] == t) & (after["ticker"] == "EWJ") & (
        after["feature"] == "events.root_19_fight.share_z.20")
    assert after.loc[key_a, "value"].iloc[0] > base.loc[key, "value"].iloc[0]


def test_build_empty_specs():
    daily, tdays = _random_daily(2, periods=120)
    assert list(build_features(daily, tdays, ASSETS, []).columns) == FEATURE_COLUMNS


# Global GKG theme rows become theme entities; country theme rows and other metrics are untouched.
THEME_REKEY_CASES = [
    # (source, entity, metric) in -> (entity, metric) out
    (("gkg", GLOBAL, "theme_ENV_OIL_docs"), ("ENV_OIL", "theme_docs")),
    (("gkg", GLOBAL, "theme_ENV_OIL_tone"), ("ENV_OIL", "theme_tone")),
    (("gkg", GLOBAL, "theme_TAX_FNCACT_RETAILER_docs"), ("TAX_FNCACT_RETAILER", "theme_docs")),
    # by-country rows keep their FIPS entity and per-theme metric
    (("gkg", "SA", "theme_ENV_OIL_docs"), ("SA", "theme_ENV_OIL_docs")),
    # not a theme metric, or not gkg: unchanged
    (("gkg", "apple inc", "org_docs"), ("apple inc", "org_docs")),
    (("events", GLOBAL, "theme_X_docs"), (GLOBAL, "theme_X_docs")),
    (("gkg", GLOBAL, "theme__docs_extra"), (GLOBAL, "theme__docs_extra")),
]


@pytest.mark.parametrize("key,expected", THEME_REKEY_CASES)
def test_themes_as_entities(key, expected):
    src, ent, met = key
    out = themes_as_entities(_daily([("2020-01-02", src, ent, met, 3.0, 10)]))
    assert list(out.columns) == GDELT_DAILY_COLUMNS
    assert (out["entity"].iloc[0], out["metric"].iloc[0]) == expected
    assert (out["value_sum"].iloc[0], out["n"].iloc[0]) == (3.0, 10)


def test_themes_as_entities_lets_etf_themes_map():
    daily = _daily([("2020-01-02", "gkg", GLOBAL, "theme_ENV_OIL_docs", 5.0, 100),
                    ("2020-01-02", "gkg", GLOBAL, "theme_ENV_NATURALGAS_docs", 3.0, 100)])
    etf = Asset(ticker="XLE", name="x", kind="etf", track="A", group="sector",
                gkg_themes=["ENV_OIL", "ENV_NATURALGAS"])
    out = map_to_tickers(themes_as_entities(daily), [etf])
    assert out[["entity", "metric", "value_sum", "n"]].values.tolist() == [
        ["XLE", "theme_docs", 8.0, 200.0]]


# Interaction: product only where both sides exist; non-finite products are dropped.
def test_product_feature():
    f = pd.DataFrame([
        ("2020-01-02", "A", "x", 2.0), ("2020-01-02", "A", "y", 3.0),
        ("2020-01-02", "B", "x", 1.0),                                  # no y for B -> no row
        ("2020-01-03", "A", "x", np.inf), ("2020-01-03", "A", "y", 1.0),  # inf -> dropped
    ], columns=FEATURE_COLUMNS)
    out = product_feature(f, "x", "y", "x*y")
    assert list(out.columns) == FEATURE_COLUMNS
    assert out[["ticker", "feature", "value"]].values.tolist() == [["A", "x*y", 6.0]]
