"""SQLite store for every table the project keeps on disk (stdlib sqlite3, no ORM).

Conventions shared by all methods:
- Dates are stored as ISO text ``YYYY-MM-DD``; timestamps as ISO-8601 UTC text
  ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``. Naive datetimes are taken to be UTC.
- Date ranges passed to ``read_*`` are half-open: ``start <= date < end``. ``None`` means unbounded.
  Half-open so ``config.HOLDOUT_START`` can be passed as ``end`` without leaking its first day.
- DataFrames read back use the contract columns from ``schemas.py`` with date columns as
  ``datetime64`` (midnight), sorted by primary key.
- ``trials`` and ``trial_returns`` are append-only; triggers abort any UPDATE or DELETE.

Tables (no schema.sql; convention §0.3):
- gdelt_daily(session_date, source, entity, metric, value_sum, n, pulled_at, query_name)
    PK (session_date, source, entity, metric). pulled_at/query_name = vintage of the last pull.
- prices(date, ticker, open, high, low, close, volume, adj_open, adj_close, source, fetched_at)
    PK (date, ticker). source in ('yfinance', 'stooq').
- assets(ticker, name, kind, track, grp, fips_countries, gkg_themes, org_aliases, first_trade,
         screen_group)
    PK ticker. List fields are JSON arrays. ``grp`` holds Asset.group (GROUP is an SQL keyword).
    screen_group (nullable, ADR 0003) was added later; ``init`` adds it to older files.
- features(date, ticker, feature, value)                         PK (date, ticker, feature)
- labels(date, ticker, horizon, ret_oo, ret_overnight, ret_intraday)  PK (date, ticker, horizon)
- trials(trial_id, created_at, track, stage, hypothesis, params, data_start, data_end, stats, notes)
    PK trial_id. params and stats are JSON (stats NULL when absent). Append-only.
- trial_returns(trial_id, date, ret)                             PK (trial_id, date). Append-only.
- bq_usage(id, query_name, ran_at, bytes_processed, bytes_billed, dry_run)  id autoincrement.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from eventtrade.schemas import (
    FEATURE_COLUMNS,
    GDELT_DAILY_COLUMNS,
    LABEL_COLUMNS,
    PRICE_COLUMNS,
    TRIAL_RETURN_COLUMNS,
    Asset,
    BqUsage,
    Trial,
)

_PRICE_SOURCES = ("yfinance", "stooq")

_DDL = """
CREATE TABLE IF NOT EXISTS gdelt_daily (
    session_date TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('events', 'gkg')),
    entity       TEXT NOT NULL,
    metric       TEXT NOT NULL,
    value_sum    REAL,
    n            INTEGER,
    pulled_at    TEXT NOT NULL,
    query_name   TEXT NOT NULL,
    PRIMARY KEY (session_date, source, entity, metric)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS gdelt_daily_source_date ON gdelt_daily (source, session_date);

CREATE TABLE IF NOT EXISTS prices (
    date       TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    adj_open   REAL,
    adj_close  REAL,
    source     TEXT NOT NULL CHECK (source IN ('yfinance', 'stooq')),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, ticker)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS prices_ticker_date ON prices (ticker, date);

CREATE TABLE IF NOT EXISTS assets (
    ticker         TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('etf', 'stock')),
    track          TEXT NOT NULL CHECK (track IN ('A', 'B')),
    grp            TEXT NOT NULL,
    fips_countries TEXT NOT NULL,
    gkg_themes     TEXT NOT NULL,
    org_aliases    TEXT NOT NULL,
    first_trade    TEXT,
    screen_group   TEXT
);

CREATE TABLE IF NOT EXISTS features (
    date    TEXT NOT NULL,
    ticker  TEXT NOT NULL,
    feature TEXT NOT NULL,
    value   REAL,
    PRIMARY KEY (date, ticker, feature)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS features_feature_date ON features (feature, date);

CREATE TABLE IF NOT EXISTS labels (
    date          TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    horizon       INTEGER NOT NULL,
    ret_oo        REAL,
    ret_overnight REAL,
    ret_intraday  REAL,
    PRIMARY KEY (date, ticker, horizon)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS labels_horizon_date ON labels (horizon, date);

CREATE TABLE IF NOT EXISTS trials (
    trial_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    track      TEXT NOT NULL CHECK (track IN ('A', 'B')),
    stage      TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    params     TEXT NOT NULL,
    data_start TEXT NOT NULL,
    data_end   TEXT NOT NULL,
    stats      TEXT,
    notes      TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trials_no_update BEFORE UPDATE ON trials
BEGIN SELECT RAISE(ABORT, 'trials is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trials_no_delete BEFORE DELETE ON trials
BEGIN SELECT RAISE(ABORT, 'trials is append-only'); END;

CREATE TABLE IF NOT EXISTS trial_returns (
    trial_id TEXT NOT NULL REFERENCES trials (trial_id),
    date     TEXT NOT NULL,
    ret      REAL,
    PRIMARY KEY (trial_id, date)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS trial_returns_no_update BEFORE UPDATE ON trial_returns
BEGIN SELECT RAISE(ABORT, 'trial_returns is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trial_returns_no_delete BEFORE DELETE ON trial_returns
BEGIN SELECT RAISE(ABORT, 'trial_returns is append-only'); END;

CREATE TABLE IF NOT EXISTS bq_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query_name      TEXT NOT NULL,
    ran_at          TEXT NOT NULL,
    bytes_processed INTEGER NOT NULL,
    bytes_billed    INTEGER NOT NULL,
    dry_run         INTEGER NOT NULL CHECK (dry_run IN (0, 1))
);
CREATE INDEX IF NOT EXISTS bq_usage_ran_at ON bq_usage (ran_at);
"""

# Columns added after the first release, as (table, column, declaration). Files created earlier
# get them via ALTER TABLE ... ADD COLUMN in init(); fresh files already have them from _DDL.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("assets", "screen_group", "TEXT"),  # ADR 0003
)

# table -> (date column for min/max in status(), or None)
_STATUS_TABLES: dict[str, str | None] = {
    "gdelt_daily": "session_date",
    "prices": "date",
    "assets": None,
    "features": "date",
    "labels": "date",
    "trials": "data_start",
    "trial_returns": "date",
    "bq_usage": "ran_at",
}


class SqliteStore:
    """SQLite adapter; satisfies the UsageLog protocol (record, month_to_date_bytes)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # --- lifecycle --------------------------------------------------------------------------

    def init(self) -> None:
        """Create the file, tables, indexes and triggers if missing; switch to WAL; add columns
        introduced after a file was created (_ADDED_COLUMNS). Idempotent."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_DDL)
            for table, column, decl in _ADDED_COLUMNS:
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # --- gdelt_daily ------------------------------------------------------------------------

    def upsert_gdelt_daily(
        self, df: pd.DataFrame, query_name: str, pulled_at: datetime | None = None
    ) -> int:
        """Insert or overwrite GDELT_DAILY_COLUMNS rows, stamping the pull vintage; returns row count."""
        _check_columns(df, GDELT_DAILY_COLUMNS)
        bad = set(df["source"].unique()) - {"events", "gkg"}
        if bad:
            raise ValueError(f"unknown gdelt source(s): {sorted(bad)}")
        stamp = _iso_ts(pulled_at or datetime.now(UTC))
        rows = _rows(
            [
                _iso_dates(df["session_date"]),
                df["source"],
                df["entity"],
                df["metric"],
                df["value_sum"].astype(float),
                df["n"],
            ],
            extra=(stamp, query_name),
        )
        sql = (
            "INSERT INTO gdelt_daily (session_date, source, entity, metric, value_sum, n,"
            " pulled_at, query_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (session_date, source, entity, metric) DO UPDATE SET"
            " value_sum = excluded.value_sum, n = excluded.n,"
            " pulled_at = excluded.pulled_at, query_name = excluded.query_name"
        )
        return self._executemany(sql, rows, len(df))

    def read_gdelt_daily(
        self,
        source: str,
        start: date | None,
        end: date | None,
        entities: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """GDELT_DAILY_COLUMNS rows of one source with start <= session_date < end."""
        where, params = _range_clause("session_date", start, end)
        where.insert(0, "source = ?")
        params.insert(0, source)
        _add_in(where, params, "entity", entities)
        df = self._query(
            f"SELECT {', '.join(GDELT_DAILY_COLUMNS)} FROM gdelt_daily"
            f" WHERE {' AND '.join(where)} ORDER BY session_date, source, entity, metric",
            params,
        )
        df["session_date"] = _to_dt(df["session_date"])
        df["value_sum"] = df["value_sum"].astype(float)
        df["n"] = df["n"].astype("int64")
        return df

    def gdelt_session_dates(
        self, query_name: str, start: date | None, end: date | None
    ) -> list[date]:
        """Distinct session dates written by `query_name` with start <= session_date < end."""
        where, params = _range_clause("session_date", start, end)
        where.insert(0, "query_name = ?")
        params.insert(0, query_name)
        with self._conn() as conn:
            cur = conn.execute(
                f"SELECT DISTINCT session_date FROM gdelt_daily WHERE {' AND '.join(where)}"
                " ORDER BY session_date",
                params,
            )
            return [date.fromisoformat(r[0]) for r in cur.fetchall()]

    # --- prices -----------------------------------------------------------------------------

    def upsert_prices(
        self, df: pd.DataFrame, source: str, fetched_at: datetime | None = None
    ) -> int:
        """Insert or overwrite PRICE_COLUMNS rows from one source ('yfinance'|'stooq'); returns row count."""
        _check_columns(df, PRICE_COLUMNS)
        if source not in _PRICE_SOURCES:
            raise ValueError(f"price source must be one of {_PRICE_SOURCES}, got {source!r}")
        stamp = _iso_ts(fetched_at or datetime.now(UTC))
        num = PRICE_COLUMNS[2:]
        rows = _rows(
            [_iso_dates(df["date"]), df["ticker"], *(df[c].astype(float) for c in num)],
            extra=(source, stamp),
        )
        cols = ", ".join(PRICE_COLUMNS)
        sets = ", ".join(f"{c} = excluded.{c}" for c in [*num, "source", "fetched_at"])
        sql = (
            f"INSERT INTO prices ({cols}, source, fetched_at) VALUES ({_qs(len(PRICE_COLUMNS) + 2)})"
            f" ON CONFLICT (date, ticker) DO UPDATE SET {sets}"
        )
        return self._executemany(sql, rows, len(df))

    def read_prices(
        self, tickers: Sequence[str] | None, start: date | None, end: date | None
    ) -> pd.DataFrame:
        """PRICE_COLUMNS rows with start <= date < end, all tickers when tickers is None."""
        where, params = _range_clause("date", start, end)
        _add_in(where, params, "ticker", tickers)
        df = self._query(
            f"SELECT {', '.join(PRICE_COLUMNS)} FROM prices{_where(where)} ORDER BY date, ticker",
            params,
        )
        df["date"] = _to_dt(df["date"])
        for c in PRICE_COLUMNS[2:]:
            df[c] = df[c].astype(float)
        return df

    # --- assets -----------------------------------------------------------------------------

    def upsert_assets(self, assets: Iterable[Asset]) -> int:
        """Insert or overwrite assets by ticker; returns the number written."""
        rows = [
            (
                a.ticker,
                a.name,
                a.kind,
                a.track,
                a.group,
                json.dumps(a.fips_countries),
                json.dumps(a.gkg_themes),
                json.dumps(a.org_aliases),
                a.first_trade.isoformat() if a.first_trade else None,
                a.screen_group,
            )
            for a in assets
        ]
        sql = (
            "INSERT INTO assets (ticker, name, kind, track, grp, fips_countries, gkg_themes,"
            " org_aliases, first_trade, screen_group) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (ticker) DO UPDATE SET name = excluded.name, kind = excluded.kind,"
            " track = excluded.track, grp = excluded.grp,"
            " fips_countries = excluded.fips_countries, gkg_themes = excluded.gkg_themes,"
            " org_aliases = excluded.org_aliases, first_trade = excluded.first_trade,"
            " screen_group = excluded.screen_group"
        )
        return self._executemany(sql, rows, len(rows))

    def delete_assets(self, tickers: Sequence[str]) -> int:
        """Remove assets by ticker (prices, features and trials are kept); returns rows deleted."""
        if not tickers:
            return 0
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM assets WHERE ticker IN (SELECT value FROM json_each(?))",
                (json.dumps(list(tickers)),),
            )
            return cur.rowcount

    def read_assets(self, track: str | None = None) -> list[Asset]:
        """All assets (or one track's), ordered by ticker."""
        where, params = ([], []) if track is None else (["track = ?"], [track])
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT ticker, name, kind, track, grp, fips_countries, gkg_themes, org_aliases,"
                f" first_trade, screen_group FROM assets{_where(where)} ORDER BY ticker",
                params,
            )
            return [
                Asset(
                    ticker=r[0],
                    name=r[1],
                    kind=r[2],
                    track=r[3],
                    group=r[4],
                    fips_countries=json.loads(r[5]),
                    gkg_themes=json.loads(r[6]),
                    org_aliases=json.loads(r[7]),
                    first_trade=date.fromisoformat(r[8]) if r[8] else None,
                    screen_group=r[9],
                )
                for r in cur.fetchall()
            ]

    # --- features / labels (derived, rebuildable) -------------------------------------------

    def replace_features(self, df: pd.DataFrame) -> int:
        """Delete every stored row of each feature name in df, then insert df; returns row count."""
        _check_columns(df, FEATURE_COLUMNS)
        rows = _rows([_iso_dates(df["date"]), df["ticker"], df["feature"], df["value"].astype(float)])
        names = json.dumps(sorted(set(df["feature"].tolist())))
        return self._replace(
            "DELETE FROM features WHERE feature IN (SELECT value FROM json_each(?))",
            names,
            "INSERT INTO features (date, ticker, feature, value) VALUES (?, ?, ?, ?)",
            rows,
            len(df),
        )

    def read_features(
        self, start: date | None, end: date | None, features: Sequence[str] | None = None
    ) -> pd.DataFrame:
        """FEATURE_COLUMNS rows with start <= date < end, optionally only the named features."""
        where, params = _range_clause("date", start, end)
        _add_in(where, params, "feature", features)
        df = self._query(
            f"SELECT {', '.join(FEATURE_COLUMNS)} FROM features{_where(where)}"
            " ORDER BY date, ticker, feature",
            params,
        )
        df["date"] = _to_dt(df["date"])
        df["value"] = df["value"].astype(float)
        return df

    def replace_labels(self, df: pd.DataFrame) -> int:
        """Delete stored rows of each (horizon, ticker) in df, then insert df; returns row count.

        Scoped by ticker so rebuilding one track's labels leaves the other track's rows alone.
        """
        _check_columns(df, LABEL_COLUMNS)
        rets = LABEL_COLUMNS[3:]
        rows = _rows(
            [_iso_dates(df["date"]), df["ticker"], df["horizon"].astype("int64"),
             *(df[c].astype(float) for c in rets)]
        )
        scope = json.dumps({
            "h": sorted({int(h) for h in df["horizon"].tolist()}),
            "t": sorted({str(t) for t in df["ticker"].tolist()}),
        })
        return self._replace(
            "DELETE FROM labels WHERE horizon IN (SELECT value FROM json_each(?1, '$.h'))"
            " AND ticker IN (SELECT value FROM json_each(?1, '$.t'))",
            scope,
            f"INSERT INTO labels ({', '.join(LABEL_COLUMNS)}) VALUES ({_qs(len(LABEL_COLUMNS))})",
            rows,
            len(df),
        )

    def read_labels(
        self, start: date | None, end: date | None, horizons: Sequence[int] | None = None
    ) -> pd.DataFrame:
        """LABEL_COLUMNS rows with start <= date < end, optionally only the given horizons."""
        where, params = _range_clause("date", start, end)
        _add_in(where, params, "horizon", None if horizons is None else [int(h) for h in horizons])
        df = self._query(
            f"SELECT {', '.join(LABEL_COLUMNS)} FROM labels{_where(where)}"
            " ORDER BY date, ticker, horizon",
            params,
        )
        df["date"] = _to_dt(df["date"])
        df["horizon"] = df["horizon"].astype("int64")
        for c in LABEL_COLUMNS[3:]:
            df[c] = df[c].astype(float)
        return df

    # --- trials (append-only) ---------------------------------------------------------------

    def add_trial(self, trial: Trial, returns: pd.DataFrame | None = None) -> None:
        """Append one trial and, optionally, its TRIAL_RETURN_COLUMNS rows in one transaction."""
        d = trial.model_dump(mode="json")
        row = (
            trial.trial_id,
            _iso_ts(trial.created_at),
            trial.track,
            trial.stage,
            trial.hypothesis,
            json.dumps(d["params"], sort_keys=True),
            trial.data_start.isoformat(),
            trial.data_end.isoformat(),
            None if d["stats"] is None else json.dumps(d["stats"], sort_keys=True),
            trial.notes,
        )
        ret_rows: list[tuple[Any, ...]] = []
        if returns is not None:
            _check_columns(returns, TRIAL_RETURN_COLUMNS)
            if (returns["trial_id"].astype(str) != trial.trial_id).any():
                raise ValueError("returns.trial_id must all equal trial.trial_id")
            ret_rows = list(
                _rows([returns["trial_id"], _iso_dates(returns["date"]), returns["ret"].astype(float)])
            )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO trials (trial_id, created_at, track, stage, hypothesis, params,"
                " data_start, data_end, stats, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            if ret_rows:
                conn.executemany(
                    "INSERT INTO trial_returns (trial_id, date, ret) VALUES (?, ?, ?)", ret_rows
                )

    def read_trials(self, track: str | None = None, stage: str | None = None) -> list[Trial]:
        """Logged trials, optionally filtered by track and stage, in insertion-time order."""
        where: list[str] = []
        params: list[Any] = []
        if track is not None:
            where.append("track = ?")
            params.append(track)
        if stage is not None:
            where.append("stage = ?")
            params.append(stage)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT trial_id, created_at, track, stage, hypothesis, params, data_start,"
                f" data_end, stats, notes FROM trials{_where(where)}"
                " ORDER BY created_at, trial_id",
                params,
            )
            return [
                Trial(
                    trial_id=r[0],
                    created_at=datetime.fromisoformat(r[1]),
                    track=r[2],
                    stage=r[3],
                    hypothesis=r[4],
                    params=json.loads(r[5]),
                    data_start=date.fromisoformat(r[6]),
                    data_end=date.fromisoformat(r[7]),
                    stats=None if r[8] is None else json.loads(r[8]),
                    notes=r[9],
                )
                for r in cur.fetchall()
            ]

    def read_trial_returns(self, trial_ids: Sequence[str]) -> pd.DataFrame:
        """Wide frame of daily returns: DatetimeIndex 'date' x one column per trial_id (NaN where absent)."""
        ids = list(dict.fromkeys(str(t) for t in trial_ids))
        long = self._query(
            "SELECT trial_id, date, ret FROM trial_returns"
            " WHERE trial_id IN (SELECT value FROM json_each(?)) ORDER BY date",
            [json.dumps(ids)],
        )
        long["date"] = _to_dt(long["date"])
        long["ret"] = long["ret"].astype(float)
        wide = long.pivot(index="date", columns="trial_id", values="ret")
        wide = wide.reindex(columns=ids)
        wide.columns.name = "trial_id"
        wide.index.name = "date"
        return wide.astype(float)

    # --- bq_usage (UsageLog protocol) -------------------------------------------------------

    def record(self, usage: BqUsage) -> None:
        """Append one BigQuery usage row (dry runs included, flagged)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bq_usage (query_name, ran_at, bytes_processed, bytes_billed, dry_run)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    usage.query_name,
                    _iso_ts(usage.ran_at),
                    int(usage.bytes_processed),
                    int(usage.bytes_billed),
                    int(usage.dry_run),
                ),
            )

    def month_to_date_bytes(self, now: datetime) -> int:
        """Sum of bytes_billed over non-dry-run rows in now's UTC calendar month."""
        month = _to_utc(now).strftime("%Y-%m")
        with self._conn() as conn:
            (total,) = conn.execute(
                "SELECT COALESCE(SUM(bytes_billed), 0) FROM bq_usage"
                " WHERE dry_run = 0 AND substr(ran_at, 1, 7) = ?",
                (month,),
            ).fetchone()
        return int(total)

    # --- status -----------------------------------------------------------------------------

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        """Row count and min/max date per table, plus BigQuery bytes billed in now's UTC month."""
        out: dict[str, Any] = {"path": str(self.path), "tables": {}}
        with self._conn() as conn:
            for table, col in _STATUS_TABLES.items():
                if col is None:
                    (n,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    out["tables"][table] = {"rows": int(n), "min_date": None, "max_date": None}
                    continue
                n, lo, hi = conn.execute(
                    f"SELECT COUNT(*), MIN({col}), MAX({col}) FROM {table}"
                ).fetchone()
                if table == "trials":
                    (hi,) = conn.execute("SELECT MAX(data_end) FROM trials").fetchone()
                out["tables"][table] = {"rows": int(n), "min_date": lo, "max_date": hi}
        out["bq_bytes_this_month"] = self.month_to_date_bytes(now or datetime.now(UTC))
        return out

    # --- internals --------------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=60)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Big page cache: bulk upserts into the secondary indexes are random-access.
            conn.execute("PRAGMA cache_size=-262144")  # KiB -> 256 MB
            conn.execute("PRAGMA temp_store=MEMORY")
            with conn:  # commit on success, rollback on exception
                yield conn
        finally:
            conn.close()

    def _executemany(self, sql: str, rows: Iterable[tuple[Any, ...]], n: int) -> int:
        with self._conn() as conn:
            conn.executemany(sql, rows)
        return n

    def _replace(
        self, delete_sql: str, delete_arg: str, insert_sql: str,
        rows: Iterable[tuple[Any, ...]], n: int,
    ) -> int:
        with self._conn() as conn:
            conn.execute(delete_sql, (delete_arg,))
            conn.executemany(insert_sql, rows)
        return n

    def _query(self, sql: str, params: Sequence[Any]) -> pd.DataFrame:
        with self._conn() as conn:
            cur = conn.execute(sql, list(params))
            cols = [d[0] for d in cur.description]
            return pd.DataFrame.from_records(cur.fetchall(), columns=cols)


# --- module helpers -------------------------------------------------------------------------

def _check_columns(df: pd.DataFrame, expected: list[str]) -> None:
    got = list(df.columns)
    missing = [c for c in expected if c not in got]
    extra = [c for c in got if c not in expected]
    if missing or extra:
        raise ValueError(f"column contract violated: missing={missing} extra={extra}")


def _iso_dates(s: pd.Series) -> pd.Series:
    if s.isna().any():
        raise ValueError(f"null dates in column {s.name!r}")
    dt = pd.to_datetime(s)
    if getattr(dt.dt, "tz", None) is not None:
        dt = dt.dt.tz_localize(None)
    return dt.dt.strftime("%Y-%m-%d")


def _to_dt(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="%Y-%m-%d") if len(s) else s.astype("datetime64[ns]")


def _to_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _iso_ts(ts: datetime) -> str:
    return _to_utc(ts).isoformat(timespec="microseconds")


def _plain(s: pd.Series) -> list[Any]:
    """Python scalars with every missing value as None (sqlite3 cannot bind numpy or pd.NA)."""
    if pd.api.types.is_float_dtype(s.dtype):
        return [None if v != v else v for v in s.tolist()]
    return s.astype(object).where(s.notna(), None).tolist()


def _rows(cols: list[pd.Series], extra: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    lists = [_plain(c) for c in cols]
    if extra:
        return (row + extra for row in zip(*lists))
    return zip(*lists)


def _qs(n: int) -> str:
    return ", ".join(["?"] * n)


def _where(clauses: list[str]) -> str:
    return f" WHERE {' AND '.join(clauses)}" if clauses else ""


def _range_clause(col: str, start: date | None, end: date | None) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if start is not None:
        where.append(f"{col} >= ?")
        params.append(_iso_day(start))
    if end is not None:
        where.append(f"{col} < ?")
        params.append(_iso_day(end))
    return where, params


def _iso_day(d: date | str) -> str:
    return pd.Timestamp(d).strftime("%Y-%m-%d")


def _add_in(where: list[str], params: list[Any], col: str, values: Sequence[Any] | None) -> None:
    if values is None:
        return
    where.append(f"{col} IN (SELECT value FROM json_each(?))")
    params.append(json.dumps(list(values)))
