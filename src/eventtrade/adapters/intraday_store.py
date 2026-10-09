"""SQLite store for the intraday study (ADR 0007): `data/intraday.sqlite`, separate from the main
store so a multi-hour price backfill never holds the main file's write lock.

Tables (no schema.sql; convention §0.3). Timestamps are integer epoch seconds, UTC.
- intraday_bars(ticker, ts, open, high, low, close, volume, vwap, trade_count)
    PK (ticker, ts), WITHOUT ROWID. `ts` = bar start; regular session only.
- intraday_fetches(ticker, year, rows, fetched_at)      PK (ticker, year). Resume ledger: a
    (ticker, year) is done once written here, in the same transaction as its bars.
- events_15min(entity, batch_ts, n_events, n_conflict, n_articles, tone_x_articles)
    PK (entity, batch_ts). entity = FIPS country or '_GLOBAL'.
- orgs_15min(entity, batch_ts, docs, tone_sum)           PK (entity, batch_ts). entity = ticker
    or '_GLOBAL' (all GKG docs of the batch).
- events_15min_cat(entity, geo, batch_ts, n_events, q1..q4, r14, r16..r20, b163, n_articles,
    tone_x_articles)  PK (entity, geo, batch_ts), ADR 0008. geo 'action' (entity = ActionGeo FIPS
    country or '_GLOBAL') or 'actor' (Actor1/Actor2 country, CAMEO mapped to FIPS).
- gdelt_pulls(query_name, chunk_start, chunk_end, rows, bytes_billed, pulled_at)
    PK (query_name, chunk_start, chunk_end). Resume ledger for the 15-minute GDELT pulls.

Hold-out discipline is the caller's job (ranges passed in are never widened here).
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from eventtrade.schemas import INTRADAY_COLUMNS

EVENTS_15MIN_COLUMNS = ["entity", "batch_ts", "n_events", "n_conflict", "n_articles",
                        "tone_x_articles"]
ORGS_15MIN_COLUMNS = ["entity", "batch_ts", "docs", "tone_sum"]
EVENTS_15MIN_CAT_COUNTS = ["n_events", "q1", "q2", "q3", "q4", "r14", "r16", "r17", "r18", "r19",
                           "r20", "b163"]
EVENTS_15MIN_CAT_COLUMNS = ["entity", "geo", "batch_ts", *EVENTS_15MIN_CAT_COUNTS, "n_articles",
                            "tone_x_articles"]
_GDELT_TABLES = {"events_15min": EVENTS_15MIN_COLUMNS, "orgs_15min": ORGS_15MIN_COLUMNS,
                 "events_15min_cat": EVENTS_15MIN_CAT_COLUMNS}

_DDL = """
CREATE TABLE IF NOT EXISTS intraday_bars (
    ticker TEXT NOT NULL, ts INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL, vwap REAL, trade_count INTEGER NOT NULL,
    PRIMARY KEY (ticker, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS intraday_fetches (
    ticker TEXT NOT NULL, year INTEGER NOT NULL, rows INTEGER NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (ticker, year)
);
CREATE TABLE IF NOT EXISTS events_15min (
    entity TEXT NOT NULL, batch_ts INTEGER NOT NULL,
    n_events INTEGER NOT NULL, n_conflict INTEGER NOT NULL, n_articles REAL NOT NULL,
    tone_x_articles REAL NOT NULL,
    PRIMARY KEY (entity, batch_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS orgs_15min (
    entity TEXT NOT NULL, batch_ts INTEGER NOT NULL, docs INTEGER NOT NULL, tone_sum REAL NOT NULL,
    PRIMARY KEY (entity, batch_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events_15min_cat (
    entity TEXT NOT NULL, geo TEXT NOT NULL, batch_ts INTEGER NOT NULL,
    n_events INTEGER NOT NULL, q1 INTEGER NOT NULL, q2 INTEGER NOT NULL, q3 INTEGER NOT NULL,
    q4 INTEGER NOT NULL, r14 INTEGER NOT NULL, r16 INTEGER NOT NULL, r17 INTEGER NOT NULL,
    r18 INTEGER NOT NULL, r19 INTEGER NOT NULL, r20 INTEGER NOT NULL, b163 INTEGER NOT NULL,
    n_articles REAL NOT NULL, tone_x_articles REAL NOT NULL,
    PRIMARY KEY (entity, geo, batch_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gdelt_pulls (
    query_name TEXT NOT NULL, chunk_start TEXT NOT NULL, chunk_end TEXT NOT NULL,
    rows INTEGER NOT NULL, bytes_billed INTEGER NOT NULL, pulled_at TEXT NOT NULL,
    PRIMARY KEY (query_name, chunk_start, chunk_end)
);
"""


class IntradayStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def init(self) -> None:
        """Create the file and tables if missing; WAL mode. Idempotent."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_DDL)

    # --- bars ---------------------------------------------------------------------------------

    def fetched_years(self) -> set[tuple[str, int]]:
        with self._conn() as conn:
            return {(t, int(y)) for t, y in conn.execute("SELECT ticker, year FROM intraday_fetches")}

    def write_bars_year(self, ticker: str, year: int, bars: pd.DataFrame, now: datetime) -> int:
        """Replace `ticker`'s bars of calendar `year` (UTC) and mark the year fetched, atomically."""
        if not bars.empty and set(bars["ticker"]) != {ticker}:
            raise ValueError(f"bars for {sorted(set(bars['ticker']))}, expected only {ticker}")
        lo = int(datetime(year, 1, 1, tzinfo=UTC).timestamp())
        hi = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp())
        out = bars[(bars["ts"] >= lo) & (bars["ts"] < hi)] if not bars.empty else bars
        if len(out) != len(bars):
            raise ValueError(f"{len(bars) - len(out)} bar(s) of {ticker} outside year {year}")
        recs = [(ticker, int(r.ts), float(r.open), float(r.high), float(r.low), float(r.close),
                 float(r.volume), None if pd.isna(r.vwap) else float(r.vwap), int(r.trade_count))
                for r in out.itertuples(index=False)]
        with self._conn() as conn:
            conn.execute("DELETE FROM intraday_bars WHERE ticker = ? AND ts >= ? AND ts < ?",
                         (ticker, lo, hi))
            conn.executemany("INSERT INTO intraday_bars VALUES (?,?,?,?,?,?,?,?,?)", recs)
            conn.execute("INSERT OR REPLACE INTO intraday_fetches VALUES (?,?,?,?)",
                         (ticker, year, len(recs), _iso(now)))
        return len(recs)

    def read_bars(self, tickers: list[str] | None, start_ts: int | None, end_ts: int | None
                  ) -> pd.DataFrame:
        """Bars with start_ts <= ts < end_ts (half-open), sorted by (ticker, ts)."""
        q = f"SELECT {', '.join(INTRADAY_COLUMNS)} FROM intraday_bars WHERE 1=1"
        args: list = []
        if tickers is not None:
            q += f" AND ticker IN ({','.join('?' * len(tickers))})"
            args += list(tickers)
        if start_ts is not None:
            q += " AND ts >= ?"
            args.append(int(start_ts))
        if end_ts is not None:
            q += " AND ts < ?"
            args.append(int(end_ts))
        with self._conn() as conn:
            df = pd.read_sql_query(q + " ORDER BY ticker, ts", conn, params=args)
        return df[INTRADAY_COLUMNS]

    # --- 15-minute GDELT ----------------------------------------------------------------------

    def pulled_chunks(self, query_name: str) -> set[tuple[date, date]]:
        with self._conn() as conn:
            return {(date.fromisoformat(a), date.fromisoformat(b)) for a, b in conn.execute(
                "SELECT chunk_start, chunk_end FROM gdelt_pulls WHERE query_name = ?",
                (query_name,))}

    def write_gdelt_chunk(self, table: str, query_name: str, chunk: tuple[date, date],
                          rows: pd.DataFrame, bytes_billed: int, now: datetime) -> int:
        """Upsert one pulled chunk's rows and record the chunk as pulled, atomically."""
        cols = _GDELT_TABLES[table]
        if list(rows.columns) != cols:
            raise ValueError(f"{table} rows must have columns {cols}, got {list(rows.columns)}")
        recs = [tuple(_py(v) for v in r) for r in rows.itertuples(index=False)]
        with self._conn() as conn:
            conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES "
                             f"({','.join('?' * len(cols))})", recs)
            conn.execute("INSERT OR REPLACE INTO gdelt_pulls VALUES (?,?,?,?,?,?)",
                         (query_name, chunk[0].isoformat(), chunk[1].isoformat(), len(recs),
                          int(bytes_billed), _iso(now)))
        return len(recs)

    def read_gdelt(self, table: str, start_ts: int | None, end_ts: int | None) -> pd.DataFrame:
        cols = _GDELT_TABLES[table]
        q = f"SELECT {', '.join(cols)} FROM {table} WHERE 1=1"
        args: list = []
        if start_ts is not None:
            q += " AND batch_ts >= ?"
            args.append(int(start_ts))
        if end_ts is not None:
            q += " AND batch_ts < ?"
            args.append(int(end_ts))
        with self._conn() as conn:
            return pd.read_sql_query(f"{q} ORDER BY {', '.join(gdelt_key(cols))}", conn,
                                     params=args)[cols]

    def counts(self) -> dict[str, int]:
        with self._conn() as conn:
            return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                    for t in ("intraday_bars", "intraday_fetches", "events_15min", "orgs_15min",
                              "events_15min_cat", "gdelt_pulls")}

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=60)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-262144")
            with conn:
                yield conn
        finally:
            conn.close()


def gdelt_key(columns: list[str]) -> list[str]:
    """Primary key of a 15-minute GDELT table: its columns up to and including batch_ts."""
    return columns[: columns.index("batch_ts") + 1]


def _iso(t: datetime) -> str:
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def _py(v: object) -> object:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v.item() if hasattr(v, "item") else v
