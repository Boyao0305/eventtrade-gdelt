"""gdelt_bq: session-date rule, partition windows, budget guard, parameter binding, output shape.

Fast tests use a fake BigQuery client and an in-memory UsageLog. Tests marked slow talk to the
real BigQuery API but only run queries that read no table (0 bytes) or dry runs (free).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from eventtrade.adapters.gdelt_bq import (
    QUERIES_DIR,
    BudgetExceeded,
    GdeltBigQuery,
    partition_window,
    session_date_of,
)
from eventtrade.schemas import GDELT_DAILY_COLUMNS, BqUsage

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
MB = 10**6
MIB10 = 10 * 1024**2

# --- session_date_of: case table ------------------------------------------------------------

SESSION_CASES = [
    # Rule 1: at or before the cutoff (New York local time) stays on the same calendar day.
    #   2025-03-10 is EDT (UTC-4): 15:30 ET = 19:30 UTC.
    (20250310193000, "15:30", date(2025, 3, 10)),
    (20250310120000, "15:30", date(2025, 3, 10)),
    (20250310040000, "15:30", date(2025, 3, 10)),  # 00:00 ET
    # Rule 2: strictly after the cutoff rolls to the next calendar day (weekends not skipped).
    (20250310193100, "15:30", date(2025, 3, 11)),
    (20250310200000, "15:30", date(2025, 3, 11)),
    (20250314200000, "15:30", date(2025, 3, 15)),  # Friday evening -> Saturday
    # Rule 3: the UTC date is not the New York date; late UTC evening is still the same NY day,
    #   early UTC morning belongs to the previous NY evening (then rolls forward past the cutoff).
    (20250311030000, "15:30", date(2025, 3, 11)),  # 23:00 ET on 3-10 -> after cutoff -> 3-11
    (20250311035800, "23:59", date(2025, 3, 10)),  # 23:58 ET on 3-10 with a 23:59 cutoff
    # Rule 4: winter (EST, UTC-5): 15:30 ET = 20:30 UTC.
    (20250102203000, "15:30", date(2025, 1, 2)),
    (20250102203100, "15:30", date(2025, 1, 3)),
    (20250102193100, "15:30", date(2025, 1, 2)),  # would be after cutoff under EDT; not in winter
    # Rule 5: DST change days use the offset in force at that instant.
    (20250309193000, "15:30", date(2025, 3, 9)),  # spring-forward day, already EDT
    (20241103203000, "15:30", date(2024, 11, 3)),  # fall-back day, already EST
    (20241103193100, "15:30", date(2024, 11, 3)),
    # Rule 6: other cutoffs; the comparison includes seconds.
    (20250310130000, "09:00", date(2025, 3, 10)),  # 09:00:00 ET exactly stays
    (20250310130001, "09:00", date(2025, 3, 11)),  # one second later rolls
    (20250310125959, "09:00", date(2025, 3, 10)),
    (20250311035959, "23:59", date(2025, 3, 11)),  # 23:59:59 ET is after 23:59:00
    (20250310040000, "00:00", date(2025, 3, 10)),  # midnight cutoff: 00:00:00 stays
    (20250310040100, "00:00", date(2025, 3, 11)),
]


@pytest.mark.parametrize(("dateadded", "cutoff", "expected"), SESSION_CASES)
def test_session_date_of(dateadded: int, cutoff: str, expected: date) -> None:
    assert session_date_of(dateadded, cutoff) == expected


# --- partition_window: case table -----------------------------------------------------------

WINDOW_CASES = [
    # Rule: first = UTC day of the previous day's cutoff; last = UTC day of the end day's cutoff.
    (date(2025, 3, 10), date(2025, 3, 10), "15:30", date(2025, 3, 9), date(2025, 3, 10)),
    (date(2025, 1, 2), date(2025, 1, 31), "15:30", date(2025, 1, 1), date(2025, 1, 31)),
    # A cutoff late enough that its UTC instant is past midnight pushes `last` to the next day.
    (date(2025, 3, 10), date(2025, 3, 10), "21:00", date(2025, 3, 10), date(2025, 3, 11)),
    # End 2025-03-31 with the default cutoff never touches the hold-out partition 2025-04-01.
    (date(2015, 3, 2), date(2025, 3, 31), "15:30", date(2015, 3, 1), date(2025, 3, 31)),
]


@pytest.mark.parametrize(("start", "end", "cutoff", "first", "last"), WINDOW_CASES)
def test_partition_window(start: date, end: date, cutoff: str, first: date, last: date) -> None:
    assert partition_window(start, end, cutoff) == (first, last)


# --- fakes ----------------------------------------------------------------------------------


class FakeUsage:
    def __init__(self, spent: int = 0) -> None:
        self.rows: list[BqUsage] = []
        self.spent = spent

    def record(self, usage: BqUsage) -> None:
        self.rows.append(usage)

    def month_to_date_bytes(self, now: datetime) -> int:
        return self.spent + sum(r.bytes_billed for r in self.rows if not r.dry_run)


class FakeJob:
    def __init__(self, processed: int, billed: int, df: pd.DataFrame) -> None:
        self.total_bytes_processed = processed
        self.total_bytes_billed = billed
        self._df = df

    def result(self, timeout: float | None = None) -> None:
        return None

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


class FakeClient:
    """Answers dry runs with `estimate` bytes and real runs with `rows`."""

    def __init__(self, estimate: int, rows: pd.DataFrame | None = None) -> None:
        self.estimate = estimate
        self.rows = rows if rows is not None else pd.DataFrame()
        self.calls: list[tuple[str, object]] = []

    def query(self, sql: str, job_config: object = None) -> FakeJob:
        self.calls.append((sql, job_config))
        if getattr(job_config, "dry_run", False):
            return FakeJob(self.estimate, 0, pd.DataFrame())
        billed = max(self.estimate, MIB10)
        return FakeJob(self.estimate, billed, self.rows)


def _bq(
    client: FakeClient,
    usage: FakeUsage,
    cap: int = 50 * 10**9,
    budget: int = 900 * 10**9,
    lookback: int = 30,
) -> GdeltBigQuery:
    return GdeltBigQuery(client, usage, cap, budget, lambda: NOW, lookback_days=lookback)


def _params(job_config: object) -> dict[str, object]:
    out: dict[str, object] = {}
    for p in job_config.query_parameters:  # type: ignore[attr-defined]
        out[p.name] = list(p.values) if hasattr(p, "values") else p.value
    return out


def _types(job_config: object) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in job_config.query_parameters:  # type: ignore[attr-defined]
        out[p.name] = p.array_type if hasattr(p, "array_type") else p.type_
    return out


def _bq_rows(*rows: tuple) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["session_date", "entity", "metric", "value_sum", "n"])


D = date(2025, 3, 10)

# --- budget guard ---------------------------------------------------------------------------


def test_refuses_when_estimate_exceeds_per_query_cap() -> None:
    client, usage = FakeClient(estimate=60 * 10**9), FakeUsage()
    with pytest.raises(BudgetExceeded, match="per-query cap"):
        _bq(client, usage).pull_events_daily(D, D, "15:30")
    assert len(client.calls) == 1 and client.calls[0][1].dry_run  # never ran for real
    assert [(u.dry_run, u.bytes_processed, u.bytes_billed) for u in usage.rows] == [
        (True, 60 * 10**9, 0)
    ]


def test_refuses_when_month_to_date_plus_estimate_exceeds_budget() -> None:
    client, usage = FakeClient(estimate=20 * 10**9), FakeUsage(spent=890 * 10**9)
    with pytest.raises(BudgetExceeded, match="monthly budget"):
        _bq(client, usage).pull_events_daily(D, D, "15:30")
    assert len(client.calls) == 1


def test_runs_with_maximum_bytes_billed_and_records_both_jobs() -> None:
    client, usage = FakeClient(estimate=1 * 10**9), FakeUsage(spent=100 * 10**9)
    _bq(client, usage, cap=50 * 10**9).pull_events_daily(D, D, "15:30")
    dry_cfg, real_cfg = client.calls[0][1], client.calls[1][1]
    assert dry_cfg.dry_run and not real_cfg.dry_run
    assert dry_cfg.use_query_cache is False  # estimates are never cache-shortened
    assert real_cfg.maximum_bytes_billed == 50 * 10**9  # min(cap, budget - spent)
    assert [(u.query_name, u.dry_run, u.bytes_billed) for u in usage.rows] == [
        ("events_daily", True, 0),
        ("events_daily", False, 1 * 10**9),
    ]
    assert all(u.ran_at == NOW for u in usage.rows)


def test_maximum_bytes_billed_is_capped_by_remaining_budget() -> None:
    client, usage = FakeClient(estimate=1 * 10**9), FakeUsage(spent=895 * 10**9)
    _bq(client, usage).pull_events_daily(D, D, "15:30")
    assert client.calls[1][1].maximum_bytes_billed == 5 * 10**9


def test_tiny_query_is_checked_against_the_10mib_minimum_bill() -> None:
    # 1 MB estimate is billed as 10 MiB; with only 5 MB left it must be refused.
    client, usage = FakeClient(estimate=1 * MB), FakeUsage(spent=900 * 10**9 - 5 * MB)
    with pytest.raises(BudgetExceeded):
        _bq(client, usage).pull_events_daily(D, D, "15:30")


def test_estimate_is_dry_run_only_and_recorded() -> None:
    client, usage = FakeClient(estimate=123), FakeUsage()
    assert (
        _bq(client, usage).estimate("gkg_orgs_daily", D, D, cutoff_et="15:30", aliases=["x"]) == 123
    )
    assert len(client.calls) == 1 and client.calls[0][1].dry_run
    assert [(u.dry_run, u.bytes_processed) for u in usage.rows] == [(True, 123)]


def test_failed_run_is_still_recorded() -> None:
    class Boom(FakeJob):
        def result(self, timeout: float | None = None) -> None:
            raise RuntimeError("bytes billed limit exceeded")

    class Client(FakeClient):
        def query(self, sql: str, job_config: object = None) -> FakeJob:
            job = super().query(sql, job_config)
            return job if job_config.dry_run else Boom(5, 0, pd.DataFrame())

    usage = FakeUsage()
    with pytest.raises(RuntimeError):
        _bq(Client(estimate=5), usage).pull_events_daily(D, D, "15:30")
    assert [u.dry_run for u in usage.rows] == [True, False]


def test_unknown_query_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown query"):
        _bq(FakeClient(1), FakeUsage()).estimate("nope", D, D)


# --- parameter binding ----------------------------------------------------------------------


def test_events_binding_dates_cutoff_and_partition_windows() -> None:
    client = FakeClient(estimate=1)
    _bq(client, FakeUsage(), lookback=30).pull_events_daily(date(2025, 3, 1), D, "15:30")
    p, t = _params(client.calls[1][1]), _types(client.calls[1][1])
    assert p["start"] == date(2025, 3, 1) and p["end"] == D and p["cutoff_et"] == "15:30"
    assert p["lookback_days"] == 30 and t["lookback_days"] == "INT64"
    assert p["ev_part_start"] == date(
        2025, 1, 30
    )  # start - lookback (events are SQLDATE-partitioned)
    assert p["ev_part_end"] == D
    assert (p["gkg_part_start"], p["gkg_part_end"]) == (date(2025, 2, 28), D)
    assert t["start"] == "DATE" and t["cutoff_et"] == "STRING"


def test_orgs_binding_lowercases_aliases_as_string_array() -> None:
    client = FakeClient(estimate=1)
    _bq(client, FakeUsage()).pull_gkg_orgs_daily(D, D, "15:30", ["Apple Inc", " reuters "])
    p, t = _params(client.calls[1][1]), _types(client.calls[1][1])
    assert p["aliases"] == ["apple inc", "reuters"] and t["aliases"] == "STRING"


def test_themes_binding_picks_query_by_by_country() -> None:
    client = FakeClient(estimate=1)
    bq = _bq(client, FakeUsage())
    bq.pull_gkg_themes_daily(D, D, "15:30", ["ENV_OIL"], by_country=False)
    bq.pull_gkg_themes_daily(D, D, "15:30", ["ENV_OIL"], by_country=True)
    sqls = [sql for sql, cfg in client.calls if not cfg.dry_run]
    assert sqls[0] == (QUERIES_DIR / "gkg_themes_daily.sql").read_text(encoding="utf-8")
    assert sqls[1] == (QUERIES_DIR / "gkg_themes_country_daily.sql").read_text(encoding="utf-8")
    assert _params(client.calls[1][1])["themes"] == ["ENV_OIL"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"cutoff_et": "3:30pm"}, "cutoff_et"),
        ({"cutoff_et": "25:00"}, "cutoff_et"),
    ],
)
def test_bad_cutoff_is_rejected_before_any_query(kwargs: dict, match: str) -> None:
    client = FakeClient(estimate=1)
    with pytest.raises(ValueError, match=match):
        _bq(client, FakeUsage()).pull_events_daily(D, D, **kwargs)
    assert client.calls == []


def test_empty_lists_and_reversed_dates_are_rejected() -> None:
    bq = _bq(FakeClient(estimate=1), FakeUsage())
    with pytest.raises(ValueError):
        bq.pull_gkg_orgs_daily(D, D, "15:30", [])
    with pytest.raises(ValueError):
        bq.pull_gkg_themes_daily(D, D, "15:30", ["  "], by_country=False)
    with pytest.raises(ValueError):
        bq.pull_events_daily(D, date(2025, 3, 9), "15:30")


# Every @param a pull query references is bound by its pull method (BigQuery rejects unbound ones).
PULL_QUERIES = {
    "events_daily": {"cutoff_et": "15:30"},
    "gkg_themes_daily": {"cutoff_et": "15:30", "themes": ["A"]},
    "gkg_themes_country_daily": {"cutoff_et": "15:30", "themes": ["A"]},
    "gkg_orgs_daily": {"cutoff_et": "15:30", "aliases": ["a"]},
    "events_15min": {"countries": ["JA"]},
    "gkg_orgs_15min": {"aliases": ["a"], "alias_tickers": ["A"]},
    "events_15min_cat": {"countries": ["JA"], "cameo_codes": ["JPN"], "cameo_fips": ["JA"]},
    "session_date_check": {"cutoff_et": "15:30", "dateadded": [20250310193000]},
    "partition_probe": {},
    "lag_sample": {},
    "dup_sample": {},
}


@pytest.mark.parametrize("name", sorted(PULL_QUERIES))
def test_every_sql_parameter_is_bound(name: str) -> None:
    sql = (QUERIES_DIR / f"{name}.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--")[0] for line in sql.splitlines())
    used = set(re.findall(r"@(\w+)", code))
    client = FakeClient(estimate=1)
    _bq(client, FakeUsage()).estimate(name, D, D, **PULL_QUERIES[name])
    assert used <= set(_params(client.calls[0][1]))


def test_every_query_file_is_covered() -> None:
    assert {p.stem for p in QUERIES_DIR.glob("*.sql")} == set(PULL_QUERIES)


# --- the session_date SQL expression is the same everywhere ----------------------------------


def _session_block(path: Path) -> str:
    sql = path.read_text(encoding="utf-8")
    m = re.search(r"-- session_date:begin(.*?)-- session_date:end", sql, re.DOTALL)
    assert m, f"{path.name} lacks session_date markers"
    body = re.sub(r"\bDATE(ADDED)?\b(?= AS STRING)", "TS", m.group(1))
    return " ".join(body.split()).rstrip(",")  # a trailing select-list comma is not semantic


def test_session_date_expression_identical_in_every_daily_query() -> None:
    names = [
        "events_daily",
        "gkg_themes_daily",
        "gkg_themes_country_daily",
        "gkg_orgs_daily",
        "session_date_check",
    ]
    blocks = {n: _session_block(QUERIES_DIR / f"{n}.sql") for n in names}
    assert len(set(blocks.values())) == 1, blocks


# --- output shape ---------------------------------------------------------------------------


def test_events_output_follows_contract() -> None:
    rows = _bq_rows(
        (date(2025, 3, 10), "_GLOBAL", "n_events", 10.0, 10),
        (date(2025, 3, 10), "US", "tone", -5.5, 40),
        (date(2025, 3, 10), "US", "goldstein", None, 3),  # NULL sum -> dropped, not 0
    )
    df = _bq(FakeClient(1, rows), FakeUsage()).pull_events_daily(D, D, "15:30")
    assert list(df.columns) == GDELT_DAILY_COLUMNS
    assert len(df) == 2 and set(df["source"]) == {"events"}
    assert str(df["session_date"].dtype).startswith("datetime64")
    assert df["value_sum"].dtype == float and df["n"].dtype == "int64"
    assert df.iloc[0]["session_date"] == pd.Timestamp("2025-03-10")


def test_gkg_output_source_and_empty_result() -> None:
    rows = _bq_rows((date(2025, 3, 10), "apple inc", "org_docs", 27.0, 351905))
    df = _bq(FakeClient(1, rows), FakeUsage()).pull_gkg_orgs_daily(D, D, "15:30", ["apple inc"])
    assert list(df["source"]) == ["gkg"] and list(df["entity"]) == ["apple inc"]
    empty = _bq(FakeClient(1, _bq_rows()), FakeUsage()).pull_gkg_orgs_daily(D, D, "15:30", ["x"])
    assert list(empty.columns) == GDELT_DAILY_COLUMNS and empty.empty


# --- seam: the SQLite store satisfies UsageLog ----------------------------------------------


def test_sqlite_store_works_as_usage_log(tmp_path: Path) -> None:
    store_mod = pytest.importorskip("eventtrade.adapters.store")
    store = store_mod.SqliteStore(tmp_path / "t.sqlite")
    store.init()
    client = FakeClient(estimate=2 * 10**9)
    _bq(client, store).pull_events_daily(D, D, "15:30")
    assert store.month_to_date_bytes(NOW) == 2 * 10**9  # dry runs do not count


# --- slow: real BigQuery, zero-byte queries and dry runs only ---------------------------------


def _real_bq() -> GdeltBigQuery:
    home = Path(os.environ.get("EVENTTRADE_HOME", ""))
    keys = sorted((home / "secrets").glob("*.json")) if home.name else []
    if not keys:
        pytest.skip("EVENTTRADE_HOME with secrets/*.json not set")
    from google.cloud import bigquery
    from google.oauth2 import service_account

    cred = service_account.Credentials.from_service_account_file(str(keys[0]))
    client = bigquery.Client(project=cred.project_id, credentials=cred)
    return GdeltBigQuery(client, FakeUsage(), 1 * 10**9, 900 * 10**9, lambda: datetime.now(UTC))


@pytest.mark.slow
def test_bigquery_session_date_matches_python_rule() -> None:
    values = [case[0] for case in SESSION_CASES]
    for cutoff in ("15:30", "09:00", "00:00", "23:59"):
        df = _real_bq().query("session_date_check", D, D, dateadded=values, cutoff_et=cutoff)
        got = {int(r.dateadded): str(r.session_date) for r in df.itertuples()}
        assert got == {v: str(session_date_of(v, cutoff)) for v in values}


@pytest.mark.slow
def test_bigquery_dry_runs_compile_and_scale() -> None:
    bq = _real_bq()
    one = bq.estimate("gkg_orgs_daily", D, D, cutoff_et="15:30", aliases=["apple inc"])
    month = bq.estimate(
        "gkg_orgs_daily",
        date(2025, 2, 1),
        date(2025, 2, 28),
        cutoff_et="15:30",
        aliases=["apple inc"],
    )
    assert 0 < one < month
    assert bq.estimate("events_daily", D, D, cutoff_et="15:30") > 0


@pytest.mark.slow
def test_bigquery_15min_dry_runs_compile() -> None:
    bq = _real_bq()
    assert bq.estimate("events_15min", D, D, countries=["JA"]) > 0
    assert bq.estimate("gkg_orgs_15min", D, D, aliases=["apple inc"], alias_tickers=["AAPL"]) > 0
    assert bq.estimate("events_15min_cat", D, D, countries=["JA"], cameo_codes=["JPN"],
                       cameo_fips=["JA"]) > 0


# --- ADR 0007: 15-minute queries ------------------------------------------------------------


def _code(name: str) -> str:
    """SQL text without comments, whitespace collapsed."""
    sql = (QUERIES_DIR / f"{name}.sql").read_text(encoding="utf-8")
    return " ".join(" ".join(line.split("--")[0] for line in sql.splitlines()).split())


def test_events_15min_binding_has_lookback_partitions_and_no_cutoff() -> None:
    client = FakeClient(estimate=1)
    _, billed = _bq(client, FakeUsage(), lookback=30).pull_events_15min(
        date(2016, 1, 1), date(2016, 12, 31), ["JA", " GM", "JA"]
    )
    p, t = _params(client.calls[1][1]), _types(client.calls[1][1])
    assert p["countries"] == ["GM", "JA"] and t["countries"] == "STRING"
    assert (p["start"], p["end"]) == (date(2016, 1, 1), date(2016, 12, 31))
    # SQLDATE partitions: reach back the lookback; never past the last ET day (hold-out safe).
    assert (p["ev_part_start"], p["ev_part_end"]) == (date(2015, 12, 2), date(2016, 12, 31))
    assert p["lookback_days"] == 30 and t["lookback_days"] == "INT64"
    assert "cutoff_et" not in p
    assert billed == MIB10  # the fake bills at least 10 MiB
    assert client.calls[1][0] == (QUERIES_DIR / "events_15min.sql").read_text(encoding="utf-8")


def test_events_15min_partition_end_stays_before_holdout() -> None:
    client = FakeClient(estimate=1)
    _bq(client, FakeUsage()).estimate(
        "events_15min", date(2025, 1, 1), date(2025, 3, 31), countries=["JA"]
    )
    assert _params(client.calls[0][1])["ev_part_end"] == date(2025, 3, 31)


def test_orgs_15min_binding_parallel_arrays_and_utc_day_partitions() -> None:
    client = FakeClient(estimate=1)
    _bq(client, FakeUsage()).pull_orgs_15min(
        D,
        D,
        ["Apple Inc", "apple", "alphabet", "alphabet", "apple"],
        ["AAPL", "AAPL", "GOOG", "GOOGL", "AAPL"],
    )
    p, t = _params(client.calls[1][1]), _types(client.calls[1][1])
    pairs = list(zip(p["aliases"], p["alias_tickers"], strict=True))
    # lower-cased, duplicate pairs dropped, still aligned; a shared alias counts for both tickers
    assert pairs == [
        ("alphabet", "GOOG"),
        ("alphabet", "GOOGL"),
        ("apple", "AAPL"),
        ("apple inc", "AAPL"),
    ]
    assert t["aliases"] == "STRING" and t["alias_tickers"] == "STRING"
    assert (p["gkg_part_start"], p["gkg_part_end"]) == (D, D)


def test_orgs_15min_rejects_mismatched_or_empty_arrays_before_any_query() -> None:
    client = FakeClient(estimate=1)
    bq = _bq(client, FakeUsage())
    with pytest.raises(ValueError, match="alias_tickers"):
        bq.pull_orgs_15min(D, D, ["a", "b"], ["A"])
    with pytest.raises(ValueError, match="same length"):
        bq.estimate("gkg_orgs_15min", D, D, aliases=["a", "b"], alias_tickers=["A"])
    with pytest.raises(ValueError):
        bq.pull_orgs_15min(D, D, [" "], ["A"])
    with pytest.raises(ValueError):
        bq.pull_events_15min(D, D, [])
    assert client.calls == []


def test_15min_pulls_go_through_the_budget_guard() -> None:
    client, usage = FakeClient(estimate=60 * 10**9), FakeUsage()
    with pytest.raises(BudgetExceeded, match="per-query cap"):
        _bq(client, usage).pull_orgs_15min(D, D, ["a"], ["A"])
    assert len(client.calls) == 1 and client.calls[0][1].dry_run
    client, usage = FakeClient(estimate=1 * 10**9), FakeUsage(spent=895 * 10**9)
    _bq(client, usage).pull_events_15min(D, D, ["JA"])
    assert client.calls[1][1].maximum_bytes_billed == 5 * 10**9
    assert [(u.query_name, u.dry_run) for u in usage.rows] == [
        ("events_15min", True),
        ("events_15min", False),
    ]


def test_15min_output_columns_and_dtypes() -> None:
    from eventtrade.adapters.intraday_store import EVENTS_15MIN_COLUMNS, ORGS_15MIN_COLUMNS

    rows = pd.DataFrame(
        [["JA", 1457010000, 3, 1, 7, -2.5], ["_GLOBAL", 1457010000, 9, 2, 20, 1.0]],
        columns=EVENTS_15MIN_COLUMNS,
    )
    df, _ = _bq(FakeClient(1, rows), FakeUsage()).pull_events_15min(D, D, ["JA"])
    assert list(df.columns) == EVENTS_15MIN_COLUMNS
    assert list(df["entity"]) == ["JA", "_GLOBAL"]
    assert df["batch_ts"].dtype == "int64" and df["n_events"].dtype == "int64"
    assert df["n_articles"].dtype == float and df["tone_x_articles"].dtype == float
    empty, _ = _bq(FakeClient(1, pd.DataFrame()), FakeUsage()).pull_orgs_15min(D, D, ["a"], ["A"])
    assert list(empty.columns) == ORGS_15MIN_COLUMNS and empty.empty
    assert empty["docs"].dtype == "int64"


def test_events_15min_sql_dedups_whole_et_day_and_keeps_earliest_row() -> None:
    code = _code("events_15min")
    assert "SQLDATE" not in code  # timing by DATEADDED only
    assert "DATE(added, 'America/New_York') AS et_day" in code
    assert (
        "GROUP BY et_day, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode" in code
    )
    assert "ORDER BY DATEADDED, GLOBALEVENTID LIMIT 1 )[OFFSET(0)]" in code
    assert "event_day >= DATE_SUB(et_day, INTERVAL @lookback_days DAY)" in code
    # the time filter comes after the dedup (kept is built from the deduplicated rows)
    assert code.index("GROUP BY et_day") < code.index("MOD(batch_ts, 86400)")
    # '_GLOBAL' takes every row (also rows with no country); a country only when requested
    assert "UNNEST(IF(country IN UNNEST(@countries), [country, '_GLOBAL'], ['_GLOBAL']))" in code
    assert "COUNTIF(quad IN (3, 4)) AS n_conflict" in code


@pytest.mark.parametrize("name", ["events_15min", "gkg_orgs_15min"])
def test_15min_sql_batch_rounding_and_filter(name: str) -> None:
    code = _code(name)
    assert "+ 899, 900) * 900" in code  # round UP to the 15-minute grid
    assert "EXTRACT(DAYOFWEEK FROM TIMESTAMP_SECONDS(batch_ts)) BETWEEN 2 AND 6" in code
    assert "MOD(batch_ts, 86400) BETWEEN 43200 AND 71100" in code
    assert (43200, 71100) == (12 * 3600, 19 * 3600 + 45 * 60)  # 12:00 and 19:45 UTC
    assert "session_date" not in code and "@cutoff_et" not in code


def test_orgs_15min_sql_counts_a_document_once_per_ticker() -> None:
    code = _code("gkg_orgs_15min")
    assert "SELECT DISTINCT @alias_tickers[OFFSET(i)]" in code
    assert "JOIN UNNEST(@aliases) AS a WITH OFFSET AS i ON LOWER(o) = a" in code
    assert "SELECT '_GLOBAL' AS entity, batch_ts, COUNT(*) AS docs" in code
    assert "SPLIT(V2Tone, ',')[SAFE_OFFSET(0)]" in code


# --- ADR 0008: events by category (events_15min_cat) ----------------------------------------

# The FIPS 10-4 countries of the Track A trial assets (store, 2026-10-09), and the CAMEO actor
# country code (ISO-3166 alpha-3 style) of each. FIPS traps: AS Australia / AU Austria, SF South
# Africa, SZ Switzerland, SW Sweden, SN Singapore, RP Philippines, KS South Korea, UP Ukraine,
# ZA Zambia, CG DR Congo, EI Ireland, IZ Iraq, NI Nigeria, KU Kuwait, VM Vietnam, CI Chile,
# CH China, AG Algeria, GM Germany, DA Denmark, SP Spain, TU Turkey.
CAMEO_FIPS = {
    "ARE": "AE", "DZA": "AG", "ARG": "AR", "AUS": "AS", "AUT": "AU", "BEL": "BE", "BRA": "BR",
    "CAN": "CA", "COD": "CG", "CHN": "CH", "CHL": "CI", "DNK": "DA", "IRL": "EI", "FRA": "FR",
    "DEU": "GM", "GRC": "GR", "HKG": "HK", "IDN": "ID", "IND": "IN", "IRN": "IR", "ISR": "IS",
    "ITA": "IT", "IRQ": "IZ", "JPN": "JA", "KOR": "KS", "KWT": "KU", "KAZ": "KZ", "LBY": "LY",
    "MEX": "MX", "MYS": "MY", "NGA": "NI", "NLD": "NL", "NOR": "NO", "NZL": "NZ", "PER": "PE",
    "POL": "PL", "QAT": "QA", "PHL": "RP", "RUS": "RS", "SAU": "SA", "ZAF": "SF", "SGP": "SN",
    "ESP": "SP", "SWE": "SW", "CHE": "SZ", "THA": "TH", "TUR": "TU", "TWN": "TW", "GBR": "UK",
    "UKR": "UP", "USA": "US", "VEN": "VE", "VNM": "VM", "ZMB": "ZA",
}
TRACK_A_FIPS = [
    "AE", "AG", "AR", "AS", "AU", "BE", "BR", "CA", "CG", "CH", "CI", "DA", "EI", "FR", "GM",
    "GR", "HK", "ID", "IN", "IR", "IS", "IT", "IZ", "JA", "KS", "KU", "KZ", "LY", "MX", "MY",
    "NI", "NL", "NO", "NZ", "PE", "PL", "QA", "RP", "RS", "SA", "SF", "SN", "SP", "SW", "SZ",
    "TH", "TU", "TW", "UK", "UP", "US", "VE", "VM", "ZA",
]


def test_cameo_fips_table_is_pinned_and_covers_every_track_a_country_once() -> None:
    from eventtrade.adapters.gdelt_bq import cameo_fips_table

    rows = cameo_fips_table()
    assert dict(rows) == CAMEO_FIPS and len(rows) == len(CAMEO_FIPS)
    fips = [f for _, f in rows]
    assert len(TRACK_A_FIPS) == 54
    assert sorted(fips) == sorted(TRACK_A_FIPS)  # each of the 54 exactly once, nothing else


def test_cameo_fips_table_rejects_malformed_files(tmp_path: Path) -> None:
    from eventtrade.adapters.gdelt_bq import cameo_fips_table

    for body in ("code,fips\nJPN,JA\n", "cameo_code,fips\nJP,JA\n",
                 "cameo_code,fips\njpn,JA\n", "cameo_code,fips\nJPN,JA\nJPN,KS\n"):
        path = tmp_path / "m.csv"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError):
            cameo_fips_table(path)


def test_events_15min_cat_binding_parallel_arrays_and_lookback_partitions() -> None:
    client = FakeClient(estimate=1)
    _, billed = _bq(client, FakeUsage(), lookback=30).pull_events_15min_cat(
        date(2016, 1, 1), date(2016, 12, 31), ["JA", " GM", "JA"],
        ["JPN", "DEU", "JPN", " USA"], ["JA", "GM", "JA", "US"],
    )
    p, t = _params(client.calls[1][1]), _types(client.calls[1][1])
    assert p["countries"] == ["GM", "JA"]
    # duplicate pairs dropped, stripped, still aligned; sorted by CAMEO code
    assert list(zip(p["cameo_codes"], p["cameo_fips"], strict=True)) == [
        ("DEU", "GM"), ("JPN", "JA"), ("USA", "US")]
    assert t["cameo_codes"] == "STRING" and t["cameo_fips"] == "STRING"
    assert (p["ev_part_start"], p["ev_part_end"]) == (date(2015, 12, 2), date(2016, 12, 31))
    assert p["lookback_days"] == 30 and "cutoff_et" not in p
    assert billed == MIB10
    assert client.calls[1][0] == (QUERIES_DIR / "events_15min_cat.sql").read_text(
        encoding="utf-8")


def test_events_15min_cat_rejects_bad_arrays_before_any_query() -> None:
    client = FakeClient(estimate=1)
    bq = _bq(client, FakeUsage())
    with pytest.raises(ValueError, match="cameo_fips"):
        bq.pull_events_15min_cat(D, D, ["JA"], ["JPN", "DEU"], ["JA"])
    with pytest.raises(ValueError, match="same length"):
        bq.estimate("events_15min_cat", D, D, countries=["JA"], cameo_codes=["JPN", "DEU"],
                    cameo_fips=["JA"])
    with pytest.raises(ValueError):
        bq.pull_events_15min_cat(D, D, ["JA"], [" "], ["JA"])
    with pytest.raises(ValueError):
        bq.pull_events_15min_cat(D, D, [], ["JPN"], ["JA"])
    assert client.calls == []


def test_events_15min_cat_goes_through_the_budget_guard() -> None:
    client, usage = FakeClient(estimate=60 * 10**9), FakeUsage()
    with pytest.raises(BudgetExceeded, match="per-query cap"):
        _bq(client, usage).pull_events_15min_cat(D, D, ["JA"], ["JPN"], ["JA"])
    assert len(client.calls) == 1 and client.calls[0][1].dry_run
    client, usage = FakeClient(estimate=1 * 10**9), FakeUsage(spent=895 * 10**9)
    _bq(client, usage).pull_events_15min_cat(D, D, ["JA"], ["JPN"], ["JA"])
    assert client.calls[1][1].maximum_bytes_billed == 5 * 10**9
    assert [(u.query_name, u.dry_run) for u in usage.rows] == [
        ("events_15min_cat", True),
        ("events_15min_cat", False),
    ]


def test_events_15min_cat_output_columns_dtypes_and_order() -> None:
    from eventtrade.adapters.intraday_store import EVENTS_15MIN_CAT_COLUMNS

    def row(entity: str, geo: str) -> list:
        return [entity, geo, 1457010000, 3, 1, 0, 1, 1, 0, 0, 0, 1, 0, 0, 0, 7, -2.5]

    rows = pd.DataFrame([row("JA", "actor"), row("_GLOBAL", "action"), row("JA", "action")],
                        columns=EVENTS_15MIN_CAT_COLUMNS)
    df, _ = _bq(FakeClient(1, rows), FakeUsage()).pull_events_15min_cat(D, D, ["JA"], ["JPN"],
                                                                       ["JA"])
    assert list(df.columns) == EVENTS_15MIN_CAT_COLUMNS
    assert list(zip(df["entity"], df["geo"], strict=True)) == [
        ("JA", "action"), ("JA", "actor"), ("_GLOBAL", "action")]
    for c in ["batch_ts", "n_events", "q1", "q2", "q3", "q4", "r14", "r16", "r17", "r18", "r19",
              "r20", "b163"]:
        assert df[c].dtype == "int64", c
    assert df["n_articles"].dtype == float and df["tone_x_articles"].dtype == float
    empty, _ = _bq(FakeClient(1, pd.DataFrame()), FakeUsage()).pull_events_15min_cat(
        D, D, ["JA"], ["JPN"], ["JA"])
    assert list(empty.columns) == EVENTS_15MIN_CAT_COLUMNS and empty.empty
    assert empty["q1"].dtype == "int64"


def test_events_15min_cat_sql_keeps_events_15min_rows_and_dedup() -> None:
    code, base = _code("events_15min_cat"), _code("events_15min")
    assert "SQLDATE" not in code  # timing by DATEADDED only
    for rule in (
        "DATE(added, 'America/New_York') AS et_day",
        "GROUP BY et_day, SOURCEURL, EventCode, ActionGeo_FeatureID, ActionGeo_CountryCode",
        "ORDER BY DATEADDED, GLOBALEVENTID LIMIT 1 )[OFFSET(0)]",
        "WHERE et_day BETWEEN @start AND @end",
        "event_day >= DATE_SUB(et_day, INTERVAL @lookback_days DAY)",
        "event_day <= DATE(added)",
        "DIV(UNIX_SECONDS(first.added) + 899, 900) * 900 AS batch_ts",
        "UNNEST(IF(country IN UNNEST(@countries), [country, '_GLOBAL'], ['_GLOBAL']))",
    ):
        assert rule in code and rule in base, rule
    # every measure comes from the earliest row of the dedup group, for both geos
    struct = ("STRUCT(added, QuadClass AS quad, EventRootCode AS root, EventBaseCode AS base,"
              " NumArticles AS n_articles, AvgTone AS tone) ORDER BY DATEADDED, GLOBALEVENTID"
              " LIMIT 1 )[OFFSET(0)] AS first")
    assert code.count(struct) == 2
    # both dedups read the eligible rows (ET day range, lookback, SQLDATE <= UTC day of DATEADDED)
    assert code.index("eligible AS (") < code.index("event_day <= DATE(added)") < code.index(
        "action_ev AS (")
    assert "FROM eligible GROUP BY et_day, SOURCEURL, EventCode, ActionGeo_FeatureID" in code
    assert "FROM eligible AS e JOIN UNNEST(@cameo_codes)" in code


def test_events_15min_cat_sql_keeps_every_batch() -> None:
    code = _code("events_15min_cat")
    assert "+ 899, 900) * 900" in code  # round UP to the 15-minute grid
    # ADR 0008: weekends and all 96 UTC slots are kept (no weekday or 12:00-19:45 UTC filter)
    assert "DAYOFWEEK" not in code and "MOD(batch_ts" not in code and "86400" not in code
    assert "session_date" not in code and "@cutoff_et" not in code


def test_events_15min_cat_sql_categories_and_actor_attribution() -> None:
    code = _code("events_15min_cat")
    for q in (1, 2, 3, 4):
        assert f"COUNTIF(quad = {q}) AS q{q}" in code
    for r in (14, 16, 17, 18, 19, 20):
        assert f"COUNTIF(root = '{r}') AS r{r}" in code
    assert "COUNTIF(base = '163') AS b163" in code
    assert "IFNULL(SUM(tone * n_articles), 0) AS tone_x_articles" in code
    # actor geo: Actor1/Actor2CountryCode only (no ActorGeo, no Actor*Code prefix), mapped
    # through the parallel arrays; unmapped codes drop out of the inner join
    assert "Actor1Geo" not in code and "Actor2Geo" not in code and "Actor1Code" not in code
    assert ("SELECT e.*, @cameo_fips[OFFSET(i)] AS fips FROM eligible AS e JOIN"
            " UNNEST(@cameo_codes) AS k WITH OFFSET AS i ON k IN (e.Actor1CountryCode,"
            " e.Actor2CountryCode)") in code
    # dedup per (ET day, SOURCEURL, EventCode, actor country): once per distinct actor country
    assert ("FROM actor_mapped WHERE fips IN UNNEST(@countries)"
            " GROUP BY et_day, SOURCEURL, EventCode, fips") in code
    assert "SELECT fips AS entity, 'actor' AS geo, first FROM actor_ev" in code
    assert "SELECT entity, 'action' AS geo, first FROM action_ev" in code
    assert "GROUP BY entity, geo, batch_ts" in code
