"""GDELT 2.0 on BigQuery -> daily aggregates in GDELT_DAILY_COLUMNS, behind a byte budget.

Every query goes: dry run (recorded) -> refuse if the estimate breaks the per-query cap or the
month-to-date budget -> run with ``maximum_bytes_billed`` -> record actual bytes. There is no other
path to BigQuery in this module; SQL lives in ``queries/*.sql`` and Python only binds parameters.

Timing (docs/adr/0001): rows are placed by ``DATEADDED`` (events) or ``DATE`` (GKG), both UTC
``YYYYMMDDHHMMSS`` of the 15-minute batch. ``session_date`` is the America/New_York calendar date
of that timestamp, moved to the next calendar day when the local time is after ``cutoff_et``
(a row stamped exactly at the cutoff stays on the same day). The SQL expression lives between
``-- session_date:begin/end`` markers in each query and must match ``session_date_of``.

Partitions (experiment 0001): ``gkg_partitioned`` is partitioned by the UTC day of ``DATE``, so a
session window maps to the UTC days its cutoff-to-cutoff interval spans. ``events_partitioned`` is
partitioned by ``SQLDATE`` (the event's own date), not by ``DATEADDED``: rows added on one day sit
in that day's partition and in earlier ones. Event pulls therefore scan ``lookback_days`` extra
partitions, and the SQL drops any event dated more than ``lookback_days`` before its session, per
row, so results do not depend on the width of the pull window.

15-minute queries (ADR 0007): ``events_15min`` and ``gkg_orgs_15min`` take America/New_York
calendar days, not cutoff-rolled sessions, and stamp rows by 15-minute batch (epoch seconds UTC).
They use the same guard but no session roll: GKG partitions are the UTC days start..end (every
kept batch, 12:00-19:45 UTC, lies on its ET day), events partitions reach back ``lookback_days``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from eventtrade.adapters.intraday_store import EVENTS_15MIN_COLUMNS, ORGS_15MIN_COLUMNS
from eventtrade.schemas import GDELT_DAILY_COLUMNS, BqUsage

QUERIES_DIR = Path(__file__).parent / "queries"
_NY = ZoneInfo("America/New_York")
DEFAULT_LOOKBACK_DAYS = 30  # events dated > 30 days before being added: ~0.5% of rows (exp. 0001)
_MIN_BILLED_BYTES = 10 * 1024**2  # BigQuery bills at least 10 MiB per query that reads a table
_RESULT_TIMEOUT_S = 600.0
EVENTS_15MIN = "events_15min"
ORGS_15MIN = "gkg_orgs_15min"
_15MIN_QUERIES = (EVENTS_15MIN, ORGS_15MIN)


class UsageLog(Protocol):
    """Where BigQuery usage is recorded and month-to-date billed bytes are read back."""

    def record(self, usage: BqUsage) -> None: ...

    def month_to_date_bytes(self, now: datetime) -> int: ...


class BudgetExceeded(RuntimeError):
    """A query was refused because its dry-run estimate breaks a byte budget."""


def session_date_of(dateadded: int, cutoff_et: str) -> date:
    """New York session date of a UTC YYYYMMDDHHMMSS stamp; after cutoff_et rolls to next day."""
    utc = datetime.strptime(str(dateadded), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    local = utc.astimezone(_NY)
    day = local.date()
    return day + timedelta(days=1) if local.time() > _parse_cutoff(cutoff_et) else day


def partition_window(start: date, end: date, cutoff_et: str) -> tuple[date, date]:
    """First and last UTC days whose timestamps can fall in sessions start..end."""
    cut = _parse_cutoff(cutoff_et)
    first = datetime.combine(start - timedelta(days=1), cut, tzinfo=_NY).astimezone(UTC)
    last = datetime.combine(end, cut, tzinfo=_NY).astimezone(UTC)
    return first.date(), last.date()


class GdeltBigQuery:
    """Budget-guarded GDELT puller. ``client`` is a google.cloud.bigquery.Client (or a fake)."""

    def __init__(
        self,
        client: Any,
        usage: UsageLog,
        max_bytes_per_query: int,
        monthly_budget_bytes: int,
        now: Callable[[], datetime],
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        queries_dir: Path = QUERIES_DIR,
    ) -> None:
        if lookback_days < 0:
            raise ValueError("lookback_days must be >= 0")
        self._client = client
        self._usage = usage
        self._max_bytes = int(max_bytes_per_query)
        self._budget = int(monthly_budget_bytes)
        self._now = now
        self._lookback = int(lookback_days)
        self._queries_dir = Path(queries_dir)

    # --- public -------------------------------------------------------------------------------

    def estimate(self, query_name: str, start: date, end: date, **params: Any) -> int:
        """Bytes the named query would process over sessions start..end (dry run only, recorded)."""
        return self._dry_run(query_name, self._bind(start, end, params, query_name))

    def query(self, query_name: str, start: date, end: date, **params: Any) -> pd.DataFrame:
        """Run the named query under the budget guard and return BigQuery's rows unchanged."""
        return self._query_billed(query_name, start, end, params)[0]

    def pull_events_daily(self, start: date, end: date, cutoff_et: str) -> pd.DataFrame:
        """Event aggregates per ActionGeo FIPS country and '_GLOBAL' for sessions start..end."""
        df = self.query("events_daily", start, end, cutoff_et=cutoff_et)
        return _to_daily(df, "events")

    def pull_gkg_themes_daily(
        self, start: date, end: date, cutoff_et: str, themes: list[str], by_country: bool
    ) -> pd.DataFrame:
        """theme_<T>_docs / theme_<T>_tone per day, '_GLOBAL' (and per FIPS country if by_country)."""
        name = "gkg_themes_country_daily" if by_country else "gkg_themes_daily"
        df = self.query(name, start, end, cutoff_et=cutoff_et, themes=_nonempty(themes, "themes"))
        return _to_daily(df, "gkg")

    def pull_gkg_orgs_daily(
        self, start: date, end: date, cutoff_et: str, aliases: list[str]
    ) -> pd.DataFrame:
        """org_docs / org_tone / org_neg_docs per day with entity = lowercase org alias."""
        clean = [a.strip().lower() for a in _nonempty(aliases, "aliases")]
        df = self.query("gkg_orgs_daily", start, end, cutoff_et=cutoff_et, aliases=clean)
        return _to_daily(df, "gkg")

    def pull_events_15min(
        self, start: date, end: date, countries: list[str]
    ) -> tuple[pd.DataFrame, int]:
        """ADR 0007 event counts per (FIPS country or '_GLOBAL', 15-minute batch) for ET days
        start..end, and the bytes billed. Columns: intraday_store.EVENTS_15MIN_COLUMNS."""
        clean = sorted({c.strip() for c in _nonempty(countries, "countries")})
        df, billed = self._query_billed(EVENTS_15MIN, start, end, {"countries": clean})
        return _to_15min(df, EVENTS_15MIN_COLUMNS), billed

    def pull_orgs_15min(
        self, start: date, end: date, aliases: list[str], alias_tickers: list[str]
    ) -> tuple[pd.DataFrame, int]:
        """ADR 0007 GKG docs per (ticker or '_GLOBAL', 15-minute batch) for ET days start..end,
        and the bytes billed. aliases[i] belongs to alias_tickers[i]; aliases are lower-cased.
        Columns: intraday_store.ORGS_15MIN_COLUMNS."""
        if len(aliases) != len(alias_tickers):
            raise ValueError(f"{len(aliases)} aliases but {len(alias_tickers)} alias_tickers")
        pairs = sorted({(a.strip().lower(), t.strip()) for a, t in zip(aliases, alias_tickers,
                                                                      strict=True)
                        if a.strip() and t.strip()})
        if not pairs:
            raise ValueError("aliases must be a non-empty list of strings")
        params = {"aliases": [a for a, _ in pairs], "alias_tickers": [t for _, t in pairs]}
        df, billed = self._query_billed(ORGS_15MIN, start, end, params)
        return _to_15min(df, ORGS_15MIN_COLUMNS), billed

    # --- internals ----------------------------------------------------------------------------

    def _query_billed(
        self, query_name: str, start: date, end: date, params: dict[str, Any]
    ) -> tuple[pd.DataFrame, int]:
        bound = self._bind(start, end, params, query_name)
        est = self._dry_run(query_name, bound)
        cap = self._check_budget(query_name, est)
        return self._run(query_name, bound, cap)

    def _sql(self, query_name: str) -> str:
        path = self._queries_dir / f"{query_name}.sql"
        if not path.is_file():
            raise ValueError(f"unknown query {query_name!r} (no {path})")
        return path.read_text(encoding="utf-8")

    def _bind(
        self, start: date, end: date, params: dict[str, Any], query_name: str = ""
    ) -> dict[str, Any]:
        """Named parameters for one query; partition windows when a cutoff is given (daily
        queries) or for the 15-minute queries (ET days, no session roll)."""
        if end < start:
            raise ValueError(f"end {end} is before start {start}")
        bound: dict[str, Any] = {"start": start, "end": end}
        if query_name in _15MIN_QUERIES:
            if query_name == ORGS_15MIN and (
                len(params.get("aliases", [])) != len(params.get("alias_tickers", []))
            ):
                raise ValueError("aliases and alias_tickers must have the same length")
            bound |= {
                "gkg_part_start": start,
                "gkg_part_end": end,
                "ev_part_start": start - timedelta(days=self._lookback),
                "ev_part_end": end,
                "lookback_days": self._lookback,
            }
        elif "cutoff_et" in params:
            first, last = partition_window(start, end, params["cutoff_et"])
            bound |= {
                "gkg_part_start": first,
                "gkg_part_end": last,
                "ev_part_start": start - timedelta(days=self._lookback),
                "ev_part_end": last,
                "lookback_days": self._lookback,
            }
        return bound | params

    def _job_config(self, bound: dict[str, Any], **kwargs: Any) -> Any:
        from google.cloud import bigquery

        return bigquery.QueryJobConfig(query_parameters=_query_parameters(bound), **kwargs)

    def _dry_run(self, query_name: str, bound: dict[str, Any]) -> int:
        cfg = self._job_config(bound, dry_run=True, use_query_cache=False)
        job = self._client.query(self._sql(query_name), job_config=cfg)
        est = int(job.total_bytes_processed or 0)
        self._usage.record(
            BqUsage(
                query_name=query_name,
                ran_at=self._now(),
                bytes_processed=est,
                bytes_billed=0,
                dry_run=True,
            )
        )
        return est

    def _check_budget(self, query_name: str, est: int) -> int:
        """Refuse or return the maximum_bytes_billed to run with."""
        billable = 0 if est == 0 else max(est, _MIN_BILLED_BYTES)
        if est > self._max_bytes:
            raise BudgetExceeded(
                f"{query_name}: estimate {est:,} B > per-query cap {self._max_bytes:,} B"
            )
        spent = int(self._usage.month_to_date_bytes(self._now()))
        if spent + billable > self._budget:
            raise BudgetExceeded(
                f"{query_name}: month-to-date {spent:,} B + estimate {billable:,} B"
                f" > monthly budget {self._budget:,} B"
            )
        return max(min(self._max_bytes, self._budget - spent), _MIN_BILLED_BYTES)

    def _run(
        self, query_name: str, bound: dict[str, Any], cap: int
    ) -> tuple[pd.DataFrame, int]:
        """Rows and bytes billed of the real (non-dry) job; usage is recorded even on failure."""
        cfg = self._job_config(bound, maximum_bytes_billed=cap)
        job = self._client.query(self._sql(query_name), job_config=cfg)
        try:
            job.result(timeout=_RESULT_TIMEOUT_S)
            return job.to_dataframe(), int(job.total_bytes_billed or 0)
        finally:
            self._usage.record(
                BqUsage(
                    query_name=query_name,
                    ran_at=self._now(),
                    bytes_processed=int(job.total_bytes_processed or 0),
                    bytes_billed=int(job.total_bytes_billed or 0),
                    dry_run=False,
                )
            )


def _parse_cutoff(cutoff_et: str) -> time:
    m = re.fullmatch(r"(\d{2}):(\d{2})", cutoff_et) if isinstance(cutoff_et, str) else None
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError(f"cutoff_et must be 'HH:MM', got {cutoff_et!r}")
    return time(int(m[1]), int(m[2]))


def _nonempty(values: Sequence[str], what: str) -> list[str]:
    out = [v for v in values if v and v.strip()]
    if not out:
        raise ValueError(f"{what} must be a non-empty list of strings")
    return list(out)


def _query_parameters(bound: dict[str, Any]) -> list[Any]:
    from google.cloud import bigquery

    out: list[Any] = []
    for name, value in bound.items():
        if isinstance(value, (list, tuple)):
            elem = _bq_type(value[0]) if value else "STRING"
            out.append(bigquery.ArrayQueryParameter(name, elem, list(value)))
        else:
            out.append(bigquery.ScalarQueryParameter(name, _bq_type(value), value))
    return out


def _bq_type(value: Any) -> str:
    # bool before int (bool is an int subclass); datetime before date (same reason).
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    if isinstance(value, str):
        return "STRING"
    if isinstance(value, datetime):
        return "TIMESTAMP"
    if isinstance(value, date):
        return "DATE"
    raise TypeError(f"cannot bind {type(value).__name__} as a BigQuery parameter")


def _to_daily(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """BigQuery rows -> GDELT_DAILY_COLUMNS: datetime64 dates, float value_sum, int64 n.

    Rows whose value_sum is NULL (e.g. tone of documents with no parsable V2Tone) are dropped
    rather than written as 0.
    """
    if df.empty:
        return pd.DataFrame(
            {
                "session_date": pd.Series(dtype="datetime64[ns]"),
                "source": pd.Series(dtype=object),
                "entity": pd.Series(dtype=object),
                "metric": pd.Series(dtype=object),
                "value_sum": pd.Series(dtype=float),
                "n": pd.Series(dtype="int64"),
            }
        )[GDELT_DAILY_COLUMNS]
    out = pd.DataFrame(
        {
            "session_date": pd.to_datetime(df["session_date"].astype(str), format="%Y-%m-%d"),
            "source": source,
            "entity": df["entity"].astype(str),
            "metric": df["metric"].astype(str),
            "value_sum": pd.to_numeric(df["value_sum"], errors="coerce").astype(float),
            "n": pd.to_numeric(df["n"], errors="coerce"),
        }
    )
    out = out[out["value_sum"].notna() & out["n"].notna()]
    out["n"] = out["n"].astype("int64")
    out = out.sort_values(["session_date", "entity", "metric"], kind="stable")
    return out[GDELT_DAILY_COLUMNS].reset_index(drop=True)


def _to_15min(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """BigQuery rows -> the intraday store's 15-minute columns: str entity, int64 batch_ts and
    counts, float sums; sorted by (entity, batch_ts)."""
    ints = {"batch_ts", "n_events", "n_conflict", "docs"}
    out = pd.DataFrame({
        c: (df[c].astype(str) if c == "entity"
            else pd.to_numeric(df[c]).astype("int64" if c in ints else float))
        for c in columns
    }) if not df.empty else pd.DataFrame({
        c: pd.Series(dtype=object if c == "entity" else "int64" if c in ints else float)
        for c in columns
    })
    out = out.sort_values(["entity", "batch_ts"], kind="stable")
    return out[columns].reset_index(drop=True)
