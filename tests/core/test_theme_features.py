"""ADR 0004 theme screen (hypothesis A2): signals, groups and signs. Synthetic fixtures only.

Written before any theme value was read: no fixture here is derived from stored GKG data.
"""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.universe import load_etf_universe
from eventtrade.core.features import (
    GLOBAL,
    SM_DAYS,
    Z_CLIP,
    entity_features,
    feature_name,
    to_trading_days,
)
from eventtrade.core.screen_features import (
    GROUPS_A2,
    SCREEN_FAMILIES_A,
    SCREEN_WINDOW,
    THEME_GROUP_MEMBERS,
    THEME_SIGNALS,
    theme_inputs,
    theme_signals,
)
from eventtrade.schemas import FEATURE_COLUMNS, GDELT_DAILY_COLUMNS, Asset

# --- the pre-registration, verbatim -----------------------------------------------------------

# ADR 0004, "Cells": attention two-sided in both groups; tone (+) for sectors, two-sided for
# commodities (bad news about supply can raise prices).
SIGN_TABLE_A2 = {
    #  signal        commodity  sector
    "attention":   (None,      None),
    "tone":        (None,      +1.0),
}
# ADR 0004, "Universe": commodities + XLE (7); sectors + defence, XLE excluded (11).
MEMBERS_A2 = {
    "commodity": {"USO", "UNG", "GLD", "SLV", "CPER", "DBA", "XLE"},
    "sector": {"XLB", "XLC", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY", "ITA"},
}
# The theme mapping committed in universe_etf.csv when ADR 0004 was written. ADR 0004: "it is
# not changed after seeing theme data" - a failure here means the mapping was edited.
THEMES_AT_ADR_0004 = {
    "XLE": "ENV_OIL;ECON_OILPRICE;ENV_NATURALGAS;WB_1768_OIL_AND_GAS_PIPELINE",
    "XLF": "WB_1234_BANKING_INSTITUTIONS;ECON_INTEREST_RATES;ECON_CENTRALBANK;ECON_DEBT;"
           "ECON_BANKRUPTCY",
    "XLK": "WB_133_INFORMATION_AND_COMMUNICATION_TECHNOLOGIES;CYBER_ATTACK",
    "XLV": "WB_1350_PHARMACEUTICALS;HEALTH_PANDEMIC;HEALTH_VACCINATION;MEDICAL",
    "XLI": "WB_135_TRANSPORT;ECON_TRADE_DISPUTE;ECON_FREETRADE",
    "XLB": "ENV_MINING;ENV_METALS;WB_1699_METAL_ORE_MINING;WB_2934_COPPER",
    "XLP": "ECON_INFLATION;ECON_COST_OF_LIVING;FOOD_SECURITY",
    "XLY": "UNEMPLOYMENT;ECON_COST_OF_LIVING;TAX_FNCACT_RETAILER",
    "XLU": "ECON_ELECTRICALPRICE;ECON_ELECTRICALGRID;ECON_ELECTRICALGENERATION;"
           "WB_519_UTILITIES_GOVERNANCE_AND_PERFORMANCE",
    "XLRE": "ECON_HOUSING_PRICES;ECON_INTEREST_RATES",
    "XLC": "WB_133_INFORMATION_AND_COMMUNICATION_TECHNOLOGIES;ECON_MONOPOLY",
    "ITA": "MILITARY;ARMEDCONFLICT;TAX_WEAPONS;MIL_SELF_IDENTIFIED_ARMS_DEAL;"
           "SLFID_MILITARY_SPENDING;MIL_WEAPONS_PROLIFERATION",
    "USO": "ENV_OIL;ECON_OILPRICE;WB_1768_OIL_AND_GAS_PIPELINE;WB_2290_OIL_AND_GAS_EXPORT",
    "UNG": "ENV_NATURALGAS;ECON_NATGASPRICE;WB_551_GAS_TRANSPORTATION_STORAGE_AND_DISTRIBUTION",
    "GLD": "WB_2936_GOLD;ECON_GOLDPRICE;ECON_CENTRALBANK;ECON_INFLATION",
    "SLV": "WB_2937_SILVER;ENV_METALS",
    "CPER": "WB_2934_COPPER;ENV_MINING;WB_1699_METAL_ORE_MINING",
    "DBA": "WB_435_AGRICULTURE_AND_FOOD_SECURITY;FOOD_SECURITY;TAX_FOODSTAPLES_WHEAT;"
           "TAX_FOODSTAPLES_CORN;TAX_FOODSTAPLES_SOYBEAN;NATURAL_DISASTER_DROUGHT",
}


def test_sign_table_matches_adr_0004_verbatim():
    assert GROUPS_A2 == ("commodity", "sector")
    got = {s.name: tuple(s.sign(g) for g in GROUPS_A2) for s in THEME_SIGNALS}
    assert got == SIGN_TABLE_A2
    assert [s.name for s in THEME_SIGNALS] == list(SIGN_TABLE_A2)


def test_eight_cells():
    assert len(THEME_SIGNALS) * len(GROUPS_A2) * 2 == 8


def test_groups_match_adr_0004():
    assert {g: set(m) for g, m in THEME_GROUP_MEMBERS.items()} == MEMBERS_A2
    assert all(len(m) == len(set(m)) for m in THEME_GROUP_MEMBERS.values())
    assert not MEMBERS_A2["commodity"] & MEMBERS_A2["sector"]   # XLE only with commodities


def test_groups_agree_with_universe_csv():
    """Commodity = the ADR 0003 commodity screen_group; sector = the other theme-mapped
    'sector' ETFs (rates ETFs TLT/IEF/SHY have themes but are not in A2)."""
    etfs = {a.ticker: a for a in load_etf_universe()}
    commodity = {t for t, a in etfs.items() if a.screen_group == "commodity"}
    sector = {t for t, a in etfs.items()
              if a.group == "sector" and a.screen_group is None and a.gkg_themes}
    assert commodity == MEMBERS_A2["commodity"]
    assert sector == MEMBERS_A2["sector"]


def test_theme_mapping_unchanged_since_adr_0004():
    etfs = {a.ticker: a for a in load_etf_universe()}
    got = {t: ";".join(etfs[t].gkg_themes) for t in THEMES_AT_ADR_0004}
    assert got == THEMES_AT_ADR_0004


@pytest.mark.parametrize("sig", THEME_SIGNALS, ids=lambda s: s.name)
def test_same_recipe_as_adr_0003(sig):
    s3 = SCREEN_FAMILIES_A[0].spec
    assert sig.feature == f"A2.{sig.name}"
    assert sig.spec.source == "gkg" and sig.track == "A"
    assert (sig.spec.stat, sig.spec.transforms, sig.spec.window, sig.spec.min_periods,
            sig.spec.agg_days, sig.spec.denominator_metric) == (
        s3.stat, s3.transforms, s3.window, s3.min_periods, s3.agg_days, s3.denominator_metric)
    assert sig.spec.window == SCREEN_WINDOW == 60
    assert feature_name(sig.spec, "sm5").endswith("mean_sm5_z.60")


# --- theme_inputs: the case table -------------------------------------------------------------

E1 = Asset(ticker="E1", name="E1", kind="etf", track="A", group="sector",
           gkg_themes=["T1", "T2"])
E2 = Asset(ticker="E2", name="E2", kind="etf", track="A", group="sector",
           gkg_themes=["T2", "T3", "T2"])           # T2 listed twice: counts once
STOCK = Asset(ticker="S1", name="S1", kind="stock", track="B", group="sp500",
              gkg_themes=["T1"], org_aliases=["s1"])  # not an ETF: ignored


def _daily(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS)


def _g(day, theme, kind, value_sum, n):
    return (day, "gkg", GLOBAL, f"theme_{theme}_{kind}", float(value_sum), n)


D1, D2, D3 = "2024-01-03", "2024-01-04", "2024-01-05"
# Day total 1000 on D1, 800 on D2, 900 on D3 (n of every docs row of the day).
CASE_ROWS = [
    _g(D1, "T1", "docs", 10, 1000), _g(D1, "T1", "tone", -20.0, 10),
    _g(D1, "T2", "docs", 5, 1000), _g(D1, "T2", "tone", 5.0, 5),
    _g(D1, "T9", "docs", 70, 1000), _g(D1, "T9", "tone", 700.0, 70),   # unmapped theme
    _g(D2, "T9", "docs", 4, 800), _g(D2, "T9", "tone", 1.0, 4),        # no mapped theme hit
    _g(D3, "T3", "docs", 6, 900), _g(D3, "T3", "tone", -3.0, 6),
    _g(D3, "T1", "tone", 99.0, 0),                                     # tone row with n = 0
    # never read: country theme rows, event rows, org rows
    (D1, "gkg", "US", "theme_T1_docs", 500.0, 9000),
    (D1, "events", GLOBAL, "theme_T1_docs", 500.0, 9000),
    (D1, "gkg", "s1", "org_docs", 3.0, 1000),
    (D1, "gkg", GLOBAL, "org_docs", 1000.0, 1000),
]
# (day, ticker, metric) -> (value_sum, n). Rules, one group each:
CASES = {
    # attention: mentions of mapped themes summed; the day total counted ONCE (never x themes)
    (D1, "E1", "attention"): (15.0, 1000.0),
    (D1, "E2", "attention"): (5.0, 1000.0),
    # attention on a day with a known total but no mapped theme row: a true zero
    (D2, "E1", "attention"): (0.0, 800.0),
    (D2, "E2", "attention"): (0.0, 800.0),
    (D3, "E1", "attention"): (0.0, 900.0),
    (D3, "E2", "attention"): (6.0, 900.0),
    # tone: summed tone over summed mentions (mention-weighted mean); no row without a mention
    (D1, "E1", "tone"): (-15.0, 15.0),
    (D1, "E2", "tone"): (5.0, 5.0),
    (D3, "E2", "tone"): (-3.0, 6.0),
}
ABSENT = [(D2, "E1", "tone"), (D2, "E2", "tone"), (D3, "E1", "tone")]


def _as_dict(rows: pd.DataFrame) -> dict:
    return {(r.session_date.strftime("%Y-%m-%d"), r.entity.removeprefix("a2:"), r.metric):
            (r.value_sum, r.n) for r in rows.itertuples()}


def test_theme_inputs_case_table():
    rows = theme_inputs(_daily(CASE_ROWS), [E1, E2, STOCK])
    assert list(rows.columns) == GDELT_DAILY_COLUMNS
    assert set(rows["source"]) == {"gkg"}
    got = _as_dict(rows)
    assert got == CASES
    for key in ABSENT:
        assert key not in got


def test_attention_counts_mentions_not_distinct_documents():
    """A document carrying both T1 and T2 is in both per-theme counts: attention counts it
    twice (documented definitional choice, ADR 0004 implementation notes)."""
    rows = [_g(D1, "T1", "docs", 1, 100), _g(D1, "T2", "docs", 1, 100)]   # possibly 1 doc
    got = _as_dict(theme_inputs(_daily(rows), [E1]))
    assert got[(D1, "E1", "attention")] == (2.0, 100.0)


def test_disagreeing_day_totals_raise():
    rows = [_g(D1, "T1", "docs", 1, 100), _g(D1, "T2", "docs", 1, 101)]
    with pytest.raises(ValueError, match="day total"):
        theme_inputs(_daily(rows), [E1])


def test_no_theme_rows_or_no_etf_gives_empty():
    assert theme_inputs(_daily([(D1, "gkg", "s1", "org_docs", 3.0, 10)]), [E1]).empty
    assert theme_inputs(_daily(CASE_ROWS), [STOCK]).empty
    out = theme_signals(_daily([]), [date(2024, 1, 3)], [E1])
    assert list(out.columns) == FEATURE_COLUMNS and out.empty


def test_weekend_sessions_roll_into_monday_on_raw_sums():
    """Sat/Sun/Mon sessions -> Monday: attention = all mentions / all day totals of the three
    sessions (a session without a mapped mention still adds its total)."""
    rows = [_g("2024-01-06", "T1", "docs", 4, 100),          # Saturday
            _g("2024-01-07", "T9", "docs", 9, 50),           # Sunday: E1 has no mention
            _g("2024-01-08", "T2", "docs", 6, 250),          # Monday
            _g("2024-01-06", "T1", "tone", -8.0, 4), _g("2024-01-08", "T2", "tone", 3.0, 6)]
    tdays = [date(2024, 1, 5), date(2024, 1, 8)]
    daily = to_trading_days(theme_inputs(_daily(rows), [E1]), tdays)
    sig = {s.name: s.spec for s in THEME_SIGNALS}
    wide = entity_features(daily, list(sig.values()), tdays).set_index(["date", "entity"])
    mon = (pd.Timestamp("2024-01-08"), "a2:E1")
    assert math.isclose(wide.loc[mon, sig["attention"].column], 10 / 400, rel_tol=1e-12)
    assert math.isclose(wide.loc[mon, sig["tone"].column], -5.0 / 10, rel_tol=1e-12)


# --- end to end: theme_signals against a hand-written reference -------------------------------

def _ref_sm5(x: list[float], window: int = 60, min_periods: int = 20) -> list[float]:
    """Plain-Python sm5_z (as in test_screen_features): z vs t-window..t-1, then 5-row mean."""
    z = []
    for t in range(len(x)):
        base = [v for v in x[max(0, t - window):t] if not math.isnan(v)]
        if math.isnan(x[t]) or len(base) < min_periods:
            z.append(math.nan)
            continue
        m = sum(base) / len(base)
        s = math.sqrt(sum((v - m) ** 2 for v in base) / (len(base) - 1))
        z.append(math.nan if s < 1e-9 else max(-Z_CLIP, min(Z_CLIP, (x[t] - m) / s)))
    out = []
    for t in range(len(z)):
        w = z[t - SM_DAYS + 1:t + 1] if t >= SM_DAYS - 1 else []
        out.append(math.nan if len(w) < SM_DAYS or any(math.isnan(v) for v in w)
                   else sum(w) / SM_DAYS)
    return out


def _weekday_fixture(n_days: int, seed: int):
    """Every session a trading day; E1 = T1 + T2, both hit every day."""
    rng = np.random.default_rng(seed)
    tdays = list(pd.bdate_range("2024-01-01", periods=n_days).date)
    rows, att, tone = [], [], []
    for d in tdays:
        total = int(rng.integers(50_000, 90_000))
        k1, k2 = int(rng.integers(50, 500)), int(rng.integers(50, 500))
        s1, s2 = float(rng.normal(-1, 2) * k1), float(rng.normal(-1, 2) * k2)
        rows += [_g(d, "T1", "docs", k1, total), _g(d, "T1", "tone", s1, k1),
                 _g(d, "T2", "docs", k2, total), _g(d, "T2", "tone", s2, k2),
                 _g(d, "T9", "docs", 1000, total), _g(d, "T9", "tone", 1.0, 1000)]
        att.append((k1 + k2) / total)
        tone.append((s1 + s2) / (k1 + k2))
    return _daily(rows), tdays, att, tone


@pytest.mark.parametrize("name", ["attention", "tone"])
def test_signal_equals_hand_reference(name):
    daily, tdays, att, tone = _weekday_fixture(90, seed=11)
    out = theme_signals(daily, tdays, [E1])
    got = out[(out["ticker"] == "E1") & (out["feature"] == f"A2.{name}")]
    ref = pd.Series(_ref_sm5(att if name == "attention" else tone),
                    index=pd.to_datetime(tdays)).dropna()
    assert len(ref) == 90 - 20 - (SM_DAYS - 1)
    assert list(got["date"]) == list(ref.index)
    np.testing.assert_allclose(got["value"].to_numpy(), ref.to_numpy(), rtol=1e-10, atol=1e-12)


# --- contract and look-ahead on a random calendar fixture -------------------------------------

THEMES = ["T1", "T2", "T3", "T9"]
ETFS = [E1, E2, Asset(ticker="E3", name="E3", kind="etf", track="A", group="commodity",
                      gkg_themes=["T3"], fips_countries=["JA"])]


def _random_daily(seed: int, periods: int = 160) -> tuple[pd.DataFrame, list[date]]:
    """Calendar days incl. weekends; theme rows missing at random; trading days = weekdays
    minus two holidays. Every docs row of a day shares that day's total."""
    rng = np.random.default_rng(seed)
    cal = pd.date_range("2023-01-01", periods=periods, freq="D")
    rows = []
    for d in cal:
        total = int(rng.integers(100_000, 300_000))
        for t in THEMES:
            if rng.random() < 0.8:
                k = int(rng.integers(1, 400))
                rows += [_g(d.date(), t, "docs", k, total),
                         _g(d.date(), t, "tone", float(rng.normal(-1, 2) * k), k)]
        rows.append((d.date(), "events", "JA", "quad4", float(rng.integers(0, 9)), 50))
    holidays = {cal[i].date() for i in (40, 100)}
    tdays = [d.date() for d in cal if d.weekday() < 5 and d.date() not in holidays]
    return _daily(rows), tdays


def test_output_contract():
    daily, tdays = _random_daily(1)
    out = theme_signals(daily, tdays, ETFS)
    assert list(out.columns) == FEATURE_COLUMNS
    assert set(out["feature"]) == {"A2.attention", "A2.tone"}
    assert set(out["ticker"]) == {"E1", "E2", "E3"}
    assert not out.duplicated(["date", "ticker", "feature"]).any()
    assert np.isfinite(out["value"]).all() and out["value"].abs().max() <= Z_CLIP
    assert set(out["date"]) <= {pd.Timestamp(d) for d in tdays}
    assert out["date"].dtype == "datetime64[ns]"
    assert out.equals(out.sort_values(["date", "ticker", "feature"], ignore_index=True))


def test_fips_countries_never_leak_into_theme_signals():
    daily, tdays = _random_daily(1)
    a = theme_signals(daily, tdays, ETFS)
    b = theme_signals(daily, tdays, [*ETFS[:2], ETFS[2].model_copy(update={"fips_countries": []})])
    pd.testing.assert_frame_equal(a, b)


def test_deterministic_and_order_free():
    daily, tdays = _random_daily(2)
    a = theme_signals(daily, tdays, ETFS)
    b = theme_signals(daily.sample(frac=1.0, random_state=0), tdays, ETFS)
    pd.testing.assert_frame_equal(a, b)


@pytest.mark.parametrize("cut_index", [35, 70, 100])
def test_no_future_row_changes_past_signals(cut_index):
    """Perturb, drop and add raw rows with session_date after trading day t: signals <= t are
    bit-identical."""
    daily, tdays = _random_daily(7)
    base = theme_signals(daily, tdays, ETFS)
    t = pd.Timestamp(tdays[cut_index])
    rng = np.random.default_rng(99)
    pert = daily.copy()
    sd = pd.to_datetime(pert["session_date"])
    future = sd > t
    pert.loc[future, "value_sum"] = rng.normal(50, 100, future.sum()).round()
    # new day totals per future day (shared by that day's docs rows), new tone counts
    is_docs = pert["metric"].str.endswith("_docs")
    new_total = {d: int(rng.integers(1_000, 5_000_000)) for d in sd[future].unique()}
    pert.loc[future & is_docs, "n"] = sd[future & is_docs].map(new_total)
    pert.loc[future & ~is_docs, "n"] = rng.integers(1, 900, (future & ~is_docs).sum())
    drop = future & (rng.random(len(pert)) < 0.2) & ~is_docs
    pert = pert[~drop]
    last = tdays[-1]
    extra = _daily([_g(last, "T5", "docs", 9, new_total.get(pd.Timestamp(last), 10**6)),
                    _g(tdays[-2], "T3", "tone", 1e4, 3)])
    pert = pd.concat([pert[~((pd.to_datetime(pert["session_date"]) == pd.Timestamp(tdays[-2]))
                             & (pert["metric"] == "theme_T3_tone"))], extra], ignore_index=True)
    after = theme_signals(pert, tdays, ETFS)
    past_b = base[base["date"] <= t].reset_index(drop=True)
    past_a = after[after["date"] <= t].reset_index(drop=True)
    assert len(past_b) > 0
    pd.testing.assert_frame_equal(past_b, past_a, check_exact=True)
    fut_b = base[base["date"] > t].reset_index(drop=True)
    fut_a = after[after["date"] > t].reset_index(drop=True)
    assert not fut_b.equals(fut_a)       # not vacuous: later signals do change


def test_day_t_rows_reach_day_t_signal():
    """Not lagged by accident: raising only day t's T1 mentions raises E1's day-t attention."""
    daily, tdays = _random_daily(3)
    base = theme_signals(daily, tdays, ETFS)
    sel = (base["ticker"] == "E1") & (base["feature"] == "A2.attention")
    has = set(base.loc[sel, "date"])
    t1 = set(pd.to_datetime(daily.loc[daily["metric"] == "theme_T1_docs", "session_date"]))
    t = next(d for d in tdays[90:] if pd.Timestamp(d) in has and pd.Timestamp(d) in t1)
    pert = daily.copy()
    hit = (pd.to_datetime(pert["session_date"]) == pd.Timestamp(t)) \
        & (pert["metric"] == "theme_T1_docs")
    pert.loc[hit, "value_sum"] += 5_000
    after = theme_signals(pert, tdays, ETFS)

    def at(df):
        s = (df["date"] == pd.Timestamp(t)) & (df["ticker"] == "E1") \
            & (df["feature"] == "A2.attention")
        return float(df.loc[s, "value"].iloc[0])

    assert at(after) > at(base)
