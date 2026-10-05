"""ADR 0003 screen signals: one recipe (v2 sm5_z.60) over the pre-registered cells."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.features import (
    GLOBAL,
    SM_DAYS,
    Z_CLIP,
    Z_STD_FLOOR,
    MetricSpec,
    build_features,
    entity_features,
    feature_name,
    map_to_tickers,
    to_trading_days,
)
from eventtrade.core.screen_features import (
    ANY_GROUP,
    GROUPS_A,
    SCREEN_FAMILIES_A,
    SCREEN_SIGNALS_B,
    SCREEN_WINDOW,
    screen_inputs,
    screen_signals,
    screen_specs,
)
from eventtrade.schemas import FEATURE_COLUMNS, GDELT_DAILY_COLUMNS, Asset

# --- case tables ------------------------------------------------------------------------------

# ADR 0003, "Cells — Track A", verbatim: cooperation (+) for countries and none (two-sided) for
# commodities; every conflict sign (-) for countries and flipped to (+) for commodities.
SIGN_TABLE_A = {
    #  family      dm     em    commodity
    "quad1":    (+1.0, +1.0, None),   # verbal cooperation
    "quad2":    (+1.0, +1.0, None),   # material cooperation
    "quad3":    (-1.0, -1.0, +1.0),   # verbal conflict
    "quad4":    (-1.0, -1.0, +1.0),   # material conflict
    "root_14":  (-1.0, -1.0, +1.0),   # protest
    "root_163": (-1.0, -1.0, +1.0),   # sanctions
    "violence": (-1.0, -1.0, +1.0),   # roots 18 + 19 + 20
}
# ADR 0003, "Cells — Track B": attention two-sided, negative attention (-), tone (+); the same
# sign in every GICS sector group.
SIGN_TABLE_B = {"org_docs": None, "org_neg_docs": -1.0, "org_tone": +1.0}

# Raw metrics behind each family (names as stored in gdelt_daily, source='events'/'gkg').
RECIPE_A = {
    "quad1": ("quad1",), "quad2": ("quad2",), "quad3": ("quad3",), "quad4": ("quad4",),
    "root_14": ("root_14_protest",), "root_163": ("root_163_sanctions",),
    "violence": ("root_18_assault", "root_19_fight", "root_20_massviolence"),
}
RECIPE_B = {"org_docs": ("org_docs",), "org_neg_docs": ("org_neg_docs",),
            "org_tone": ("org_tone",)}

EVENT_METRICS = ["quad1", "quad2", "quad3", "quad4", "root_14_protest", "root_163_sanctions",
                 "root_17_coerce", "root_18_assault", "root_19_fight", "root_20_massviolence"]

ASSETS = [
    Asset(ticker="EWJ", name="Japan", kind="etf", track="A", group="country",
          fips_countries=["JA"]),
    Asset(ticker="AAXJ", name="Asia", kind="etf", track="A", group="country",
          fips_countries=["JA", "KS"]),
    Asset(ticker="AAPL", name="Apple", kind="stock", track="B", group="sp500",
          org_aliases=["apple", "apple inc"]),
    Asset(ticker="MSFT", name="Microsoft", kind="stock", track="B", group="sp500",
          org_aliases=["microsoft"]),
]


def _daily(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS)


# --- sign table and recipe --------------------------------------------------------------------

def test_track_a_sign_table_matches_adr_0003_verbatim():
    assert GROUPS_A == ("dm", "em", "commodity")
    got = {f.name: tuple(f.sign(g) for g in GROUPS_A) for f in SCREEN_FAMILIES_A}
    assert got == SIGN_TABLE_A
    assert [f.name for f in SCREEN_FAMILIES_A] == list(SIGN_TABLE_A)   # 7 families, ADR order


def test_track_b_sign_table_matches_adr_0003_verbatim():
    got = {s.name: s.sign("Information Technology") for s in SCREEN_SIGNALS_B}
    assert got == SIGN_TABLE_B
    assert [s.name for s in SCREEN_SIGNALS_B] == list(SIGN_TABLE_B)
    assert all(set(s.signs) == {ANY_GROUP} for s in SCREEN_SIGNALS_B)


def test_cell_counts_match_adr_0003():
    assert len(SCREEN_FAMILIES_A) * len(GROUPS_A) * 2 == 42
    assert len(SCREEN_SIGNALS_B) * 11 * 2 == 66


def test_track_a_unknown_group_raises():
    with pytest.raises(KeyError):
        SCREEN_FAMILIES_A[0].sign("rates")


@pytest.mark.parametrize("fam", SCREEN_FAMILIES_A + SCREEN_SIGNALS_B, ids=lambda f: f.name)
def test_one_recipe_for_every_cell(fam):
    """Same stat (mean), transform (sm5 only), window 60, min_periods 20 for every family."""
    recipe = {**RECIPE_A, **RECIPE_B}
    assert fam.components == recipe[fam.name]
    assert fam.feature == f"S3.{fam.name}"
    assert fam.spec.stat == "mean"
    assert fam.spec.transforms == ("sm5",)
    assert fam.spec.window == SCREEN_WINDOW == 60
    assert fam.spec.min_periods == 20
    assert fam.spec.agg_days == 1 and fam.spec.denominator_metric is None
    assert fam.spec.source == ("events" if fam.track == "A" else "gkg")
    assert feature_name(fam.spec, "sm5").endswith("mean_sm5_z.60")


def test_screen_specs_by_track():
    assert [s.metric for s in screen_specs("A")] == list(RECIPE_A)
    assert [s.metric for s in screen_specs("B")] == list(RECIPE_B)


# --- violence: summed before the share is taken -----------------------------------------------

# One session day, two countries (JA, KS) behind AAXJ. Every event metric row has n = that
# country's n_events (as stored by the events query).
VIOLENCE_ROWS = [
    ("2024-01-03", "events", "JA", "root_18_assault", 3.0, 100),
    ("2024-01-03", "events", "JA", "root_19_fight", 5.0, 100),
    ("2024-01-03", "events", "JA", "root_20_massviolence", 1.0, 100),
    ("2024-01-03", "events", "JA", "root_17_coerce", 40.0, 100),     # not part of violence
    ("2024-01-03", "events", "KS", "root_18_assault", 2.0, 300),
    ("2024-01-03", "events", "KS", "root_19_fight", 0.0, 300),
    ("2024-01-03", "events", "KS", "root_20_massviolence", 4.0, 300),
    ("2024-01-03", "events", GLOBAL, "root_19_fight", 50.0, 10_000),
]
# JA: (3 + 5 + 1) / 100; AAXJ = JA + KS on raw sums: (9 + 6) / (100 + 300).
VIOLENCE_JA = 9 / 100
VIOLENCE_AAXJ = 15 / 400
VIOLENCE_AAXJ_WRONG_N_TRIPLED = 15 / 1200        # n summed over the three roots
VIOLENCE_AAXJ_WRONG_MEAN_OF_ROOTS = 15 / 400 / 3


def test_violence_rows_sum_counts_and_keep_n_events():
    rows = screen_inputs(_daily(VIOLENCE_ROWS), "A")
    assert list(rows.columns) == GDELT_DAILY_COLUMNS
    v = rows[rows["metric"] == "violence"].set_index("entity")
    assert v.loc["JA", "value_sum"] == 9.0 and v.loc["JA", "n"] == 100.0
    assert v.loc["KS", "value_sum"] == 6.0 and v.loc["KS", "n"] == 300.0
    assert v.loc[GLOBAL, "value_sum"] == 50.0 and v.loc[GLOBAL, "n"] == 10_000.0
    # coerce (root 17) is not a screen family and must not leak into violence
    assert "root_17_coerce" not in set(rows["metric"])


def test_violence_share_is_sum_then_share_per_ticker():
    rows = screen_inputs(_daily(VIOLENCE_ROWS), "A")
    tdays = [date(2024, 1, 3)]
    daily = map_to_tickers(to_trading_days(rows, tdays), ASSETS)
    spec = next(f.spec for f in SCREEN_FAMILIES_A if f.name == "violence")
    wide = entity_features(daily, [spec], tdays).set_index("entity")
    assert math.isclose(wide.loc["EWJ", spec.column], VIOLENCE_JA, rel_tol=1e-12)
    assert math.isclose(wide.loc["AAXJ", spec.column], VIOLENCE_AAXJ, rel_tol=1e-12)
    assert not math.isclose(wide.loc["AAXJ", spec.column], VIOLENCE_AAXJ_WRONG_N_TRIPLED)
    assert not math.isclose(wide.loc["AAXJ", spec.column], VIOLENCE_AAXJ_WRONG_MEAN_OF_ROOTS)


def test_violence_with_inconsistent_n_raises():
    bad = VIOLENCE_ROWS + [("2024-01-04", "events", "JA", "root_18_assault", 1.0, 100),
                           ("2024-01-04", "events", "JA", "root_19_fight", 1.0, 99)]
    with pytest.raises(ValueError, match="n_events"):
        screen_inputs(_daily(bad), "A")


def test_violence_missing_component_row_counts_as_zero():
    rows = [("2024-01-03", "events", "JA", "root_19_fight", 5.0, 100)]
    v = screen_inputs(_daily(rows), "A")
    v = v[v["metric"] == "violence"]
    assert v[["value_sum", "n"]].values.tolist() == [[5.0, 100.0]]


def test_existing_violence_metric_is_not_trusted():
    """A stray 'violence' row in the input is ignored; only the three roots build it."""
    rows = VIOLENCE_ROWS + [("2024-01-03", "events", "JA", "violence", 1e6, 1)]
    v = screen_inputs(_daily(rows), "A")
    v = v[(v["metric"] == "violence") & (v["entity"] == "JA")]
    assert v[["value_sum", "n"]].values.tolist() == [[9.0, 100.0]]


# --- end-to-end against a hand-written reference of the v2 sm5 recipe ------------------------

def _ref_sm5(x: list[float], window: int = 60, min_periods: int = 20) -> list[float]:
    """Plain-Python sm5_z: z_t vs rows t-window..t-1 (ddof=1, floor, clip), then 5-row mean."""
    z = []
    for t in range(len(x)):
        base = [v for v in x[max(0, t - window):t] if not math.isnan(v)]
        if math.isnan(x[t]) or len(base) < min_periods:
            z.append(math.nan)
            continue
        m = sum(base) / len(base)
        s = math.sqrt(sum((v - m) ** 2 for v in base) / (len(base) - 1))
        z.append(math.nan if s < Z_STD_FLOOR else max(-Z_CLIP, min(Z_CLIP, (x[t] - m) / s)))
    out = []
    for t in range(len(z)):
        w = z[t - SM_DAYS + 1:t + 1] if t >= SM_DAYS - 1 else []
        out.append(math.nan if len(w) < SM_DAYS or any(math.isnan(v) for v in w)
                   else sum(w) / SM_DAYS)
    return out


def _single_country_days(n_days: int, seed: int) -> tuple[pd.DataFrame, list[date], list[float]]:
    """One trading day per session (weekdays only, every session a trading day)."""
    rng = np.random.default_rng(seed)
    tdays = list(pd.bdate_range("2024-01-01", periods=n_days).date)
    rows, share = [], []
    for d in tdays:
        n = int(rng.integers(200, 400))
        r = [int(rng.integers(0, 20)) for _ in range(3)]
        for metric, c in zip(("root_18_assault", "root_19_fight", "root_20_massviolence"), r):
            rows.append((d, "events", "JA", metric, float(c), n))
        share.append(sum(r) / n)
    return _daily(rows), tdays, share


def test_violence_signal_equals_hand_reference():
    daily, tdays, share = _single_country_days(90, seed=5)
    out = screen_signals(daily, tdays, ASSETS, "A")
    got = out[(out["ticker"] == "EWJ") & (out["feature"] == "S3.violence")]
    ref = pd.Series(_ref_sm5(share), index=pd.to_datetime(tdays)).dropna()
    assert len(ref) == 90 - 20 - (SM_DAYS - 1)
    assert list(got["date"]) == list(ref.index)
    np.testing.assert_allclose(got["value"].to_numpy(), ref.to_numpy(), rtol=1e-10, atol=1e-12)


def test_violence_signal_is_not_a_combination_of_per_root_signals():
    """Summing before the share/z differs from summing the per-root sm5 signals."""
    daily, tdays, _ = _single_country_days(90, seed=5)
    out = screen_signals(daily, tdays, ASSETS, "A")
    v = out[(out["ticker"] == "EWJ") & (out["feature"] == "S3.violence")].set_index("date")
    per_root = build_features(
        daily, tdays, ASSETS,
        [MetricSpec("events", m, "mean", ("sm5",)) for m in RECIPE_A["violence"]])
    per_root = per_root[per_root["ticker"] == "EWJ"].groupby("date")["value"].agg(["sum", "mean"])
    joined = v.join(per_root, how="inner")
    assert len(joined) > 30
    assert not np.allclose(joined["value"], joined["sum"])
    assert not np.allclose(joined["value"], joined["mean"])


def test_single_metric_family_equals_v2_build_features_mean_sm5():
    """For a one-metric family the screen is exactly v2 build_features(mean, sm5), renamed."""
    daily, tdays = _random_daily(3)
    out = screen_signals(daily, tdays, ASSETS, "A")
    spec = MetricSpec("events", "quad4", "mean", ("sm5",))
    ref = build_features(daily, tdays, [a for a in ASSETS if a.track == "A"], [spec])
    got = out[out["feature"] == "S3.quad4"].reset_index(drop=True)
    pd.testing.assert_frame_equal(got.drop(columns="feature"),
                                  ref.drop(columns="feature").reset_index(drop=True))


def test_track_b_signals_equal_v2_build_features():
    daily, tdays = _random_daily(4)
    out = screen_signals(daily, tdays, ASSETS, "B")
    for sig in SCREEN_SIGNALS_B:
        ref = build_features(daily, tdays, [a for a in ASSETS if a.track == "B"], [sig.spec])
        got = out[out["feature"] == sig.feature].reset_index(drop=True)
        assert len(got) > 0, sig.name
        pd.testing.assert_frame_equal(got.drop(columns="feature"),
                                      ref.drop(columns="feature").reset_index(drop=True))


# --- contract on a random fixture -------------------------------------------------------------

def _random_daily(seed: int, start: str = "2023-01-01", periods: int = 160
                  ) -> tuple[pd.DataFrame, list[date]]:
    """Calendar days incl. weekends; trading days = weekdays minus two holidays."""
    rng = np.random.default_rng(seed)
    cal = pd.date_range(start, periods=periods, freq="D")
    rows = []
    for d in cal:
        rows.append((d.date(), "events", GLOBAL, "n_events", 5000.0, 5000))
        for e in ("JA", "KS", "FR"):
            if rng.random() < 0.9:
                n = int(rng.integers(50, 400))
                rows.append((d.date(), "events", e, "n_events", float(n), n))
                for m in EVENT_METRICS:
                    rows.append((d.date(), "events", e, m, float(rng.integers(0, n // 5)), n))
        total = int(rng.integers(200_000, 800_000))
        for alias in ("apple", "apple inc", "microsoft", "unmapped co"):
            if rng.random() < 0.7:
                k = int(rng.integers(1, 60))
                rows.append((d.date(), "gkg", alias, "org_docs", float(k), total))
                rows.append((d.date(), "gkg", alias, "org_neg_docs",
                             float(rng.integers(0, k + 1)), k))
                rows.append((d.date(), "gkg", alias, "org_tone", float(rng.normal(-1, 2) * k), k))
    holidays = {cal[i].date() for i in (40, 100) if i < len(cal)}
    tdays = [d.date() for d in cal if d.weekday() < 5 and d.date() not in holidays]
    return _daily(rows), tdays


@pytest.mark.parametrize("track,names,tickers", [
    ("A", {f"S3.{n}" for n in RECIPE_A}, {"EWJ", "AAXJ"}),
    ("B", {f"S3.{n}" for n in RECIPE_B}, {"AAPL", "MSFT"}),
])
def test_output_contract(track, names, tickers):
    daily, tdays = _random_daily(1)
    out = screen_signals(daily, tdays, ASSETS, track)
    assert list(out.columns) == FEATURE_COLUMNS
    assert set(out["feature"]) == names
    assert set(out["ticker"]) == tickers                     # other track's assets excluded
    assert not out.duplicated(["date", "ticker", "feature"]).any()
    assert np.isfinite(out["value"]).all()
    assert out["value"].abs().max() <= Z_CLIP
    assert set(out["date"]) <= {pd.Timestamp(d) for d in tdays}
    assert out["date"].dtype == "datetime64[ns]"
    assert out.equals(out.sort_values(["date", "ticker", "feature"], ignore_index=True))


def test_no_assets_of_track_gives_empty_frame():
    daily, tdays = _random_daily(1, periods=30)
    out = screen_signals(daily, tdays, [a for a in ASSETS if a.track == "B"], "A")
    assert list(out.columns) == FEATURE_COLUMNS and out.empty


def test_deterministic():
    daily, tdays = _random_daily(2)
    a = screen_signals(daily, tdays, ASSETS, "A")
    b = screen_signals(daily.sample(frac=1.0, random_state=0), tdays, ASSETS, "A")
    pd.testing.assert_frame_equal(a, b)


# --- no look-ahead ----------------------------------------------------------------------------

@pytest.mark.parametrize("track", ["A", "B"])
@pytest.mark.parametrize("cut_index", [35, 70, 100])
def test_no_future_row_changes_past_signals(track, cut_index):
    """Perturb every raw row with session_date after trading day t: signals <= t bit-identical."""
    daily, tdays = _random_daily(7)
    base = screen_signals(daily, tdays, ASSETS, track)
    t = pd.Timestamp(tdays[cut_index])
    rng = np.random.default_rng(99)
    pert = daily.copy()
    future = pd.to_datetime(pert["session_date"]) > t
    pert.loc[future, "value_sum"] = rng.normal(5, 10, future.sum())
    # new n per (day, source, entity): event metrics of one entity-day keep one common n_events
    key = pert["session_date"].astype(str) + "|" + pert["source"] + "|" + pert["entity"]
    new_n = {k: int(rng.integers(1, 5000)) for k in key[future].unique()}
    pert.loc[future, "n"] = key[future].map(new_n)
    # drop some future rows and add brand-new future rows
    drop = future & (rng.random(len(pert)) < 0.2)
    last = pd.to_datetime(pert["session_date"]) == pd.Timestamp(tdays[-1])
    pert = pert[~drop & ~(last & (pert["entity"] == "KS"))]
    extra = _daily([
        *((tdays[-1], "events", "KS", m, 1e3, 7) for m in RECIPE_A["violence"]),
        (tdays[-2], "gkg", "apple", "org_neg_docs", 1e5, 3),
        (tdays[-3], "gkg", "microsoft", "org_docs", 1e5, 10),
    ])
    pert = pd.concat([pert, extra], ignore_index=True)
    after = screen_signals(pert, tdays, ASSETS, track)
    past_b = base[base["date"] <= t].reset_index(drop=True)
    past_a = after[after["date"] <= t].reset_index(drop=True)
    pd.testing.assert_frame_equal(past_b, past_a, check_exact=True)
    assert len(past_b) > 0
    # not vacuous: later signals do change
    fut_b = base[base["date"] > t].reset_index(drop=True)
    fut_a = after[after["date"] > t].reset_index(drop=True)
    assert not fut_b.equals(fut_a)


def test_day_t_rows_reach_day_t_signal():
    """Not lagged by accident: changing only day t's violence rows changes day t's signal."""
    daily, tdays = _random_daily(3)
    base = screen_signals(daily, tdays, ASSETS, "A")
    has = set(base.loc[(base["ticker"] == "EWJ") & (base["feature"] == "S3.violence"), "date"])
    ja = set(pd.to_datetime(daily.loc[daily["entity"] == "JA", "session_date"]))
    t = next(d for d in tdays[90:] if pd.Timestamp(d) in has and pd.Timestamp(d) in ja)
    pert = daily.copy()
    hit = (pd.to_datetime(pert["session_date"]) == pd.Timestamp(t)) & (pert["entity"] == "JA") \
        & (pert["metric"] == "root_19_fight")
    assert hit.sum() == 1
    pert.loc[hit, "value_sum"] += 40
    after = screen_signals(pert, tdays, ASSETS, "A")

    def at(df):
        sel = (df["date"] == pd.Timestamp(t)) & (df["ticker"] == "EWJ") \
            & (df["feature"] == "S3.violence")
        return float(df.loc[sel, "value"].iloc[0])

    assert at(after) > at(base)
