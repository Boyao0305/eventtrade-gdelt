"""SQLite store for FINRA daily short-sale volume (ADR 0019): `data/shortvol.sqlite`.

Tables: short_volume(date, symbol, short, total, n_fac), shortvol_days(date, n_rows) — a
ledger of fetched days (also those without any facility file), so a rerun resumes. Days on or
after the hold-out are refused here."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pandas as pd

_DDL = """
CREATE TABLE IF NOT EXISTS short_volume (date TEXT NOT NULL, symbol TEXT NOT NULL,
    short REAL NOT NULL, total REAL NOT NULL, n_fac INTEGER NOT NULL, PRIMARY KEY (date, symbol));
CREATE TABLE IF NOT EXISTS shortvol_days (date TEXT PRIMARY KEY, n_rows INTEGER NOT NULL);
"""


class ShortVolStore:
    def __init__(self, path: Path, holdout_start: date):
        self.path = Path(path)
        self.holdout_start = holdout_start

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_DDL)

    def done(self) -> set[str]:
        with self._conn() as c:
            return {d for (d,) in c.execute("SELECT date FROM shortvol_days")}

    def write_day(self, d: date, df: pd.DataFrame) -> int:
        if d >= self.holdout_start:
            raise ValueError(f"{d} is in the hold-out")
        with self._conn() as c:
            c.execute("DELETE FROM short_volume WHERE date = ?", (d.isoformat(),))
            c.executemany("INSERT INTO short_volume VALUES (?,?,?,?,?)",
                          df[["date", "symbol", "short", "total", "n_fac"]]
                          .itertuples(index=False, name=None))
            c.execute("INSERT OR REPLACE INTO shortvol_days VALUES (?,?)", (d.isoformat(), len(df)))
        return len(df)

    def read(self, symbols: list[str] | None = None) -> pd.DataFrame:
        q = "SELECT date, symbol, short, total, n_fac FROM short_volume"
        with self._conn() as c:
            df = pd.read_sql_query(q, c)
        return df[df["symbol"].isin(symbols)] if symbols is not None else df

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=60)
        try:
            with conn:
                yield conn
        finally:
            conn.close()
