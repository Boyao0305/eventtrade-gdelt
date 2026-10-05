"""SqliteStore: round trips, upsert/replace semantics, append-only trials, BigQuery usage log."""

import os
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd
import pytest

from eventtrade.adapters.store import SqliteStore
from eventtrade.schemas import (
    FEATURE_COLUMNS,
    GDELT_DAILY_COLUMNS,
    LABEL_COLUMNS,
    PRICE_COLUMNS,
    Asset,
    BacktestStats,
    BqUsage,
    Trial,
)


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    s = SqliteStore(tmp_path / "sub" / "t.sqlite")
    s.init()
    return s


def _gdelt(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=GDELT_DAILY_COLUMNS)


def _prices(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=PRICE_COLUMNS)


def _trial(tid: str, track: str = "A", stage: str = "signal", created: datetime | None = None,
           stats: BacktestStats | None = None) -> Trial:
    return Trial(
        trial_id=tid,
        created_at=created or datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        track=track,
        stage=stage,
        hypothesis="A1",
        params={"window": 60, "start": "2015-03-02", "nested": {"k": [1, 2]}},
        data_start=date(2015, 3, 2),
        data_end=date(2025, 3, 31),
        stats=stats,
        notes="n",
    )


# --- lifecycle ---------------------------------------------------------------------------------

def test_init_is_idempotent_and_uses_wal(store: SqliteStore) -> None:
    store.init()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# --- gdelt_daily -------------------------------------------------------------------------------

def test_gdelt_round_trip_and_filters(store: SqliteStore) -> None:
    df = _gdelt([
        (date(2020, 1, 2), "events", "JA", "count", 10.0, 3),
        ("2020-01-03", "events", "GM", "count", 5.5, 2),
        (pd.Timestamp("2020-01-03"), "gkg", "JA", "tone", -1.25, 4),
    ])
    assert store.upsert_gdelt_daily(df, "q1", pulled_at=datetime(2026, 1, 1)) == 3
    out = store.read_gdelt_daily("events", date(2020, 1, 1), date(2020, 2, 1))
    assert list(out.columns) == GDELT_DAILY_COLUMNS
    assert out["entity"].tolist() == ["JA", "GM"]
    assert out["session_date"].tolist() == [pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-03")]
    assert out["n"].dtype == np.int64
    only = store.read_gdelt_daily("events", None, None, entities=["GM"])
    assert only["entity"].tolist() == ["GM"]


def test_gdelt_range_is_half_open(store: SqliteStore) -> None:
    store.upsert_gdelt_daily(_gdelt([
        ("2025-03-31", "events", "JA", "count", 1.0, 1),
        ("2025-04-01", "events", "JA", "count", 2.0, 1),
    ]), "q")
    out = store.read_gdelt_daily("events", date(2025, 3, 31), date(2025, 4, 1))
    assert out["session_date"].tolist() == [pd.Timestamp("2025-03-31")]


def test_gdelt_upsert_overwrites_and_restamps_vintage(store: SqliteStore) -> None:
    key = ("2020-01-02", "events", "JA", "count")
    store.upsert_gdelt_daily(_gdelt([(*key, 1.0, 1)]), "old", pulled_at=datetime(2026, 1, 1))
    store.upsert_gdelt_daily(_gdelt([(*key, 9.0, 7)]), "new", pulled_at=datetime(2026, 2, 1))
    out = store.read_gdelt_daily("events", None, None)
    assert out[["value_sum", "n"]].values.tolist() == [[9.0, 7]]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT query_name, pulled_at FROM gdelt_daily").fetchone() == (
            "new", "2026-02-01T00:00:00.000000+00:00")


@pytest.mark.parametrize("bad", [
    # extra column is a contract change
    lambda df: df.assign(extra=1),
    # missing column
    lambda df: df.drop(columns=["n"]),
])
def test_gdelt_rejects_column_contract_violations(store: SqliteStore, bad) -> None:
    df = _gdelt([("2020-01-02", "events", "JA", "count", 1.0, 1)])
    with pytest.raises(ValueError, match="column contract"):
        store.upsert_gdelt_daily(bad(df), "q")


def test_gdelt_rejects_unknown_source(store: SqliteStore) -> None:
    with pytest.raises(ValueError, match="source"):
        store.upsert_gdelt_daily(_gdelt([("2020-01-02", "mentions", "JA", "c", 1.0, 1)]), "q")


# --- prices ------------------------------------------------------------------------------------

def test_prices_round_trip_nan_and_ticker_filter(store: SqliteStore) -> None:
    df = _prices([
        ("2020-01-02", "EWJ", 1.0, 2.0, 0.5, 1.5, 1000, 0.9, 1.4),
        ("2020-01-02", "EWG", 1.0, 2.0, 0.5, 1.5, np.nan, 0.9, 1.4),
        ("2020-01-03", "EWJ", 1.1, 2.1, 0.6, 1.6, 2000, 1.0, 1.5),
    ])
    assert store.upsert_prices(df, "yfinance") == 3
    out = store.read_prices(None, None, None)
    assert list(out.columns) == PRICE_COLUMNS
    assert out["ticker"].tolist() == ["EWG", "EWJ", "EWJ"]  # sorted by date, ticker
    assert np.isnan(out.loc[0, "volume"])
    assert out["volume"].dtype == float
    ewj = store.read_prices(["EWJ"], date(2020, 1, 3), None)
    assert ewj["adj_open"].tolist() == [1.0]


def test_prices_upsert_overwrites_and_records_source(store: SqliteStore) -> None:
    row = ("2020-01-02", "EWJ", 1.0, 2.0, 0.5, 1.5, 1000, 0.9, 1.4)
    store.upsert_prices(_prices([row]), "yfinance")
    store.upsert_prices(_prices([(*row[:8], 9.9)]), "stooq")
    out = store.read_prices(["EWJ"], None, None)
    assert out["adj_close"].tolist() == [9.9]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT source FROM prices").fetchone() == ("stooq",)


def test_prices_reject_unknown_source(store: SqliteStore) -> None:
    with pytest.raises(ValueError, match="source"):
        store.upsert_prices(_prices([("2020-01-02", "X", 1, 1, 1, 1, 1, 1, 1)]), "bloomberg")


def test_prices_bulk_upsert(store: SqliteStore) -> None:
    days = pd.bdate_range("2015-01-01", periods=500)
    tickers = [f"T{i:03d}" for i in range(200)]
    idx = pd.MultiIndex.from_product([days, tickers], names=["date", "ticker"]).to_frame(index=False)
    rng = np.random.default_rng(0)
    for c in PRICE_COLUMNS[2:]:
        idx[c] = rng.random(len(idx))
    assert store.upsert_prices(idx, "yfinance") == 100_000
    assert store.status()["tables"]["prices"]["rows"] == 100_000


# --- assets ------------------------------------------------------------------------------------

def test_assets_round_trip_and_track_filter(store: SqliteStore) -> None:
    a = Asset(ticker="EWJ", name="Japan", kind="etf", track="A", group="country",
              fips_countries=["JA"], gkg_themes=["ECON_X"], first_trade=date(1996, 3, 18))
    b = Asset(ticker="AAPL", name="Apple", kind="stock", track="B", group="sp500",
              org_aliases=["apple inc", "apple"])
    assert store.upsert_assets([a, b]) == 2
    assert store.read_assets() == [b, a]
    assert store.read_assets("A") == [a]
    store.upsert_assets([a.model_copy(update={"name": "iShares Japan"})])
    assert store.read_assets("A")[0].name == "iShares Japan"


def test_assets_screen_group_round_trip_and_overwrite(store: SqliteStore) -> None:
    a = Asset(ticker="EWJ", name="Japan", kind="etf", track="A", group="country",
              screen_group="dm")
    b = Asset(ticker="AAPL", name="Apple", kind="stock", track="B", group="sp500",
              screen_group="Information Technology")
    c = Asset(ticker="SPY", name="SPDR", kind="etf", track="A", group="market")
    store.upsert_assets([a, b, c])
    assert {x.ticker: x.screen_group for x in store.read_assets()} == {
        "AAPL": "Information Technology", "EWJ": "dm", "SPY": None}
    # an upsert overwrites the group, including back to None
    store.upsert_assets([a.model_copy(update={"screen_group": "em"}),
                         b.model_copy(update={"screen_group": None})])
    assert {x.ticker: x.screen_group for x in store.read_assets()} == {
        "AAPL": None, "EWJ": "em", "SPY": None}


# The assets table as created before ADR 0003 (no screen_group column). The live db has it.
_OLD_ASSETS_DDL = """
CREATE TABLE assets (
    ticker         TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('etf', 'stock')),
    track          TEXT NOT NULL CHECK (track IN ('A', 'B')),
    grp            TEXT NOT NULL,
    fips_countries TEXT NOT NULL,
    gkg_themes     TEXT NOT NULL,
    org_aliases    TEXT NOT NULL,
    first_trade    TEXT
);
INSERT INTO assets VALUES ('EWJ', 'Japan', 'etf', 'A', 'country', '["JA"]', '[]', '[]',
                           '1996-03-18');
"""


def _asset_columns(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(assets)")]
    finally:
        conn.close()


def test_init_migrates_old_assets_table_keeping_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_ASSETS_DDL)
    conn.close()
    assert "screen_group" not in _asset_columns(path)

    store = SqliteStore(path)
    store.init()
    assert _asset_columns(path)[-1] == "screen_group"
    (a,) = store.read_assets()
    assert (a.ticker, a.fips_countries, a.first_trade, a.screen_group) == (
        "EWJ", ["JA"], date(1996, 3, 18), None)

    store.init()  # idempotent: no second ALTER, no error
    assert _asset_columns(path).count("screen_group") == 1
    store.upsert_assets([a.model_copy(update={"screen_group": "dm"})])
    assert store.read_assets()[0].screen_group == "dm"


def test_fresh_db_has_screen_group_column(store: SqliteStore) -> None:
    assert "screen_group" in _asset_columns(store.path)


def test_delete_assets_removes_only_named_tickers(store: SqliteStore) -> None:
    a = Asset(ticker="GOOG", name="Alphabet Inc. (Class C)", kind="stock", track="B",
              group="sp500")
    b = a.model_copy(update={"ticker": "GOOGL", "name": "Alphabet Inc. (Class A)"})
    store.upsert_assets([a, b])
    assert store.delete_assets(["GOOG", "NOPE"]) == 1
    assert [x.ticker for x in store.read_assets()] == ["GOOGL"]
    assert store.delete_assets([]) == 0


# --- features / labels: replace by feature name / horizon --------------------------------------

def test_replace_features_only_replaces_named_features(store: SqliteStore) -> None:
    f = lambda rows: pd.DataFrame(rows, columns=FEATURE_COLUMNS)  # noqa: E731
    store.replace_features(f([("2020-01-02", "EWJ", "a", 1.0), ("2020-01-02", "EWJ", "b", 2.0),
                              ("2020-01-03", "EWJ", "a", 3.0)]))
    store.replace_features(f([("2020-01-06", "EWJ", "a", 9.0)]))
    out = store.read_features(None, None)
    assert out[["feature", "value"]].values.tolist() == [["b", 2.0], ["a", 9.0]]
    only_b = store.read_features(date(2020, 1, 1), date(2020, 2, 1), features=["b"])
    assert list(only_b.columns) == FEATURE_COLUMNS and len(only_b) == 1


def test_replace_labels_only_replaces_given_horizons(store: SqliteStore) -> None:
    lab = lambda rows: pd.DataFrame(rows, columns=LABEL_COLUMNS)  # noqa: E731
    store.replace_labels(lab([("2020-01-02", "EWJ", 1, 0.01, 0.0, 0.002),
                              ("2020-01-02", "EWJ", 5, 0.05, 0.0, np.nan)]))
    store.replace_labels(lab([("2020-01-03", "EWJ", 1, 0.02, 0.001, 0.003)]))
    out = store.read_labels(None, None)
    assert list(out.columns) == LABEL_COLUMNS
    assert sorted(out["horizon"].tolist()) == [1, 5]
    assert out.loc[out.horizon == 1, "date"].tolist() == [pd.Timestamp("2020-01-03")]
    assert np.isnan(store.read_labels(None, None, horizons=[5])["ret_intraday"].iloc[0])


def test_replace_labels_is_scoped_by_ticker(store: SqliteStore) -> None:
    # Rebuilding Track B labels must not delete Track A rows of the same horizon.
    lab = lambda rows: pd.DataFrame(rows, columns=LABEL_COLUMNS)  # noqa: E731
    store.replace_labels(lab([("2020-01-02", "EWJ", 1, 0.01, 0.0, 0.0)]))
    store.replace_labels(lab([("2020-01-02", "AAPL", 1, 0.02, 0.0, 0.0)]))
    assert sorted(store.read_labels(None, None)["ticker"].tolist()) == ["AAPL", "EWJ"]
    store.replace_labels(lab([("2020-01-03", "AAPL", 1, 0.03, 0.0, 0.0)]))
    out = store.read_labels(None, None)
    assert out[["ticker", "ret_oo"]].values.tolist() == [["EWJ", 0.01], ["AAPL", 0.03]]


def test_gdelt_session_dates_by_query_name_half_open(store: SqliteStore) -> None:
    store.upsert_gdelt_daily(_gdelt([
        ("2020-01-01", "events", "_GLOBAL", "n_events", 1.0, 1),
        ("2020-01-01", "events", "JA", "n_events", 1.0, 1),
        ("2020-01-02", "events", "_GLOBAL", "n_events", 1.0, 1),
        ("2020-01-03", "events", "_GLOBAL", "n_events", 1.0, 1),
    ]), "events_daily")
    store.upsert_gdelt_daily(_gdelt([("2020-01-05", "gkg", "x", "org_docs", 1.0, 1)]),
                             "gkg_orgs_daily")
    assert store.gdelt_session_dates("events_daily", None, None) == [
        date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]
    assert store.gdelt_session_dates("events_daily", date(2020, 1, 2), date(2020, 1, 3)) == [
        date(2020, 1, 2)]
    assert store.gdelt_session_dates("gkg_themes_daily", None, None) == []


# --- trials: append-only -----------------------------------------------------------------------

def test_trial_round_trip_with_stats_and_returns(store: SqliteStore) -> None:
    stats = BacktestStats(n_days=10, ann_return=0.1, ann_vol=0.2, sharpe=0.5,
                          max_drawdown=-0.1, turnover=0.3, cost_drag=0.01)
    t1 = _trial("t1", stats=stats)
    rets = pd.DataFrame({"trial_id": ["t1", "t1"], "date": ["2020-01-02", "2020-01-03"],
                         "ret": [0.01, -0.02]})
    store.add_trial(t1, rets)
    store.add_trial(_trial("t2", track="B", stage="backtest",
                           created=datetime(2026, 10, 2, tzinfo=UTC)),
                    pd.DataFrame({"trial_id": ["t2"], "date": ["2020-01-03"], "ret": [0.5]}))
    assert store.read_trials()[0] == t1
    assert [t.trial_id for t in store.read_trials(track="B")] == ["t2"]
    assert [t.trial_id for t in store.read_trials(stage="signal")] == ["t1"]
    wide = store.read_trial_returns(["t2", "t1"])
    assert list(wide.columns) == ["t2", "t1"]
    assert wide.index.tolist() == [pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-03")]
    assert np.isnan(wide.loc["2020-01-02", "t2"]) and wide.loc["2020-01-03", "t1"] == -0.02


def test_trial_naive_created_at_is_read_back_as_utc(store: SqliteStore) -> None:
    store.add_trial(_trial("t", created=datetime(2026, 10, 1, 12, 0)))
    assert store.read_trials()[0].created_at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_trials_are_append_only(store: SqliteStore) -> None:
    store.add_trial(_trial("t1"), pd.DataFrame({"trial_id": ["t1"], "date": ["2020-01-02"],
                                                "ret": [0.1]}))
    # duplicate id is refused, never overwritten
    with pytest.raises(sqlite3.IntegrityError):
        store.add_trial(_trial("t1"))
    # triggers refuse UPDATE/DELETE even from a raw connection
    with sqlite3.connect(store.path) as conn:
        for sql in ["UPDATE trials SET notes='x'", "DELETE FROM trials",
                    "UPDATE trial_returns SET ret=0", "DELETE FROM trial_returns"]:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)


def test_failed_add_trial_writes_nothing(store: SqliteStore) -> None:
    # returns for another trial -> rejected before any write
    with pytest.raises(ValueError):
        store.add_trial(_trial("t1"), pd.DataFrame({"trial_id": ["zz"], "date": ["2020-01-02"],
                                                    "ret": [0.1]}))
    # duplicate return date -> transaction rolls back the trial row too
    dup = pd.DataFrame({"trial_id": ["t1", "t1"], "date": ["2020-01-02"] * 2, "ret": [0.1, 0.2]})
    with pytest.raises(sqlite3.IntegrityError):
        store.add_trial(_trial("t1"), dup)
    assert store.read_trials() == []


def test_read_trial_returns_empty(store: SqliteStore) -> None:
    assert store.read_trial_returns([]).empty


# --- bq_usage: UsageLog protocol ---------------------------------------------------------------

class _UsageLog(Protocol):
    def record(self, usage: BqUsage) -> None: ...
    def month_to_date_bytes(self, now: datetime) -> int: ...


def _usage(ran_at: datetime, billed: int, dry: bool = False) -> BqUsage:
    return BqUsage(query_name="q", ran_at=ran_at, bytes_processed=billed, bytes_billed=billed,
                   dry_run=dry)


@pytest.mark.parametrize("now, expected", [
    # counts only non-dry-run rows in now's UTC month
    (datetime(2026, 10, 15, tzinfo=UTC), 100 + 7),
    # rows from the previous month are excluded
    (datetime(2026, 9, 30, 12, tzinfo=UTC), 50 + 1000),
    # a non-UTC now is converted first: 2026-11-01 01:00 at +02:00 is still October in UTC
    (datetime(2026, 11, 1, 1, tzinfo=timezone(timedelta(hours=2))), 100 + 7),
    # empty month -> 0
    (datetime(2027, 1, 1, tzinfo=UTC), 0),
])
def test_month_to_date_bytes(store: SqliteStore, now: datetime, expected: int) -> None:
    log: _UsageLog = store
    log.record(_usage(datetime(2026, 9, 30, 23, 59, tzinfo=UTC), 50))
    log.record(_usage(datetime(2026, 10, 1, tzinfo=UTC), 100))
    log.record(_usage(datetime(2026, 10, 2), 7))  # naive -> UTC
    log.record(_usage(datetime(2026, 10, 3, tzinfo=UTC), 10**12, dry=True))
    # 2026-10-01 01:00 at +03:00 is 2026-09-30 22:00 UTC -> September
    log.record(_usage(datetime(2026, 10, 1, 1, tzinfo=timezone(timedelta(hours=3))), 1000))
    assert log.month_to_date_bytes(now) == expected


def test_status_reports_counts_dates_and_bytes(store: SqliteStore) -> None:
    store.upsert_prices(_prices([("2020-01-02", "A", 1, 1, 1, 1, 1, 1, 1),
                                 ("2021-05-03", "A", 1, 1, 1, 1, 1, 1, 1)]), "stooq")
    store.record(_usage(datetime(2026, 10, 2, tzinfo=UTC), 42))
    s = store.status(now=datetime(2026, 10, 5, tzinfo=UTC))
    assert s["tables"]["prices"] == {"rows": 2, "min_date": "2020-01-02", "max_date": "2021-05-03"}
    assert s["tables"]["trials"]["rows"] == 0
    assert set(s["tables"]) == {"gdelt_daily", "prices", "assets", "features", "labels", "trials",
                                "trial_returns", "bq_usage"}
    assert s["bq_bytes_this_month"] == 42


# --- real database on disk ---------------------------------------------------------------------

@pytest.mark.slow
def test_real_db_status() -> None:
    home = os.environ.get("EVENTTRADE_HOME")
    if not home:
        pytest.skip("EVENTTRADE_HOME not set")
    db = Path(home) / "data" / "eventtrade.sqlite"
    store = SqliteStore(db)
    store.init()
    s = store.status()
    assert "prices" in s["tables"] and s["bq_bytes_this_month"] >= 0
