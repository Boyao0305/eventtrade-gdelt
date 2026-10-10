"""SQLite store for SEC filings (ADR 0011): `data/sec.sqlite`.

Tables (raw, as the SEC publishes them; columns lower-cased):
- insider_subs(accession_number, filing_date, document_type, issuercik, issuertradingsymbol)
- insider_owners(accession_number, rptownercik, rptownername, rptowner_relationship, rptowner_title)
- insider_trans(accession_number, trans_date, trans_code, trans_shares, trans_pricepershare,
                trans_acquired_disp_cd, security_title)       ('P' transactions only)
- filings_13d(adsh, form, file_date, ciks, names)               (ciks/names: JSON lists)
- cik_tickers(cik, ticker, title)
- sec_pulls(kind, period, rows, pulled_at)   resume ledger: kind 'insider' (period '2016q1') or
                                             '13d' (period '2016-03')
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

_TABLES = {
    "insider_subs": ["accession_number", "filing_date", "document_type", "issuercik",
                     "issuertradingsymbol"],
    "insider_owners": ["accession_number", "rptownercik", "rptownername", "rptowner_relationship",
                       "rptowner_title"],
    "insider_trans": ["accession_number", "trans_date", "trans_code", "trans_shares",
                      "trans_pricepershare", "trans_acquired_disp_cd", "security_title"],
    "filings_13d": ["adsh", "form", "file_date", "ciks", "names"],
    "cik_tickers": ["cik", "ticker", "title"],
}
_DDL = "\n".join(f"CREATE TABLE IF NOT EXISTS {t} ({', '.join(c + ' TEXT' for c in cols)});"
                 for t, cols in _TABLES.items()) + """
CREATE INDEX IF NOT EXISTS ix_subs_acc ON insider_subs(accession_number);
CREATE INDEX IF NOT EXISTS ix_owners_acc ON insider_owners(accession_number);
CREATE INDEX IF NOT EXISTS ix_trans_acc ON insider_trans(accession_number);
CREATE UNIQUE INDEX IF NOT EXISTS ux_13d ON filings_13d(adsh);
CREATE TABLE IF NOT EXISTS sec_pulls (kind TEXT NOT NULL, period TEXT NOT NULL,
    rows INTEGER NOT NULL, pulled_at TEXT NOT NULL, PRIMARY KEY (kind, period));
"""


class SecStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_DDL)

    def pulled(self, kind: str) -> set[str]:
        with self._conn() as c:
            return {p for (p,) in c.execute("SELECT period FROM sec_pulls WHERE kind = ?", (kind,))}

    def write_insider(self, period: str, parts: dict[str, pd.DataFrame], now: datetime) -> int:
        """Insert one quarter's tables and record the quarter, atomically (re-pull replaces)."""
        names = {"subs": "insider_subs", "owners": "insider_owners", "trans": "insider_trans"}
        with self._conn() as c:
            accs = list(parts["trans"]["ACCESSION_NUMBER"].unique())
            for tbl in names.values():
                c.executemany(f"DELETE FROM {tbl} WHERE accession_number = ?",
                              [(a,) for a in accs])
            for key, tbl in names.items():
                df = parts[key]
                cols = _TABLES[tbl]
                recs = df[[x.upper() for x in cols]].astype(object).where(df.notna(), None)
                c.executemany(f"INSERT INTO {tbl} VALUES ({','.join('?' * len(cols))})",
                              recs.itertuples(index=False, name=None))
            n = len(parts["trans"])
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("insider", period, n, _iso(now)))
        return n

    def write_13d(self, period: str, df: pd.DataFrame, now: datetime) -> int:
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO filings_13d VALUES (?,?,?,?,?)",
                          df[_TABLES["filings_13d"]].itertuples(index=False, name=None))
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("13d", period, len(df), _iso(now)))
        return len(df)

    def write_tickers(self, df: pd.DataFrame) -> int:
        with self._conn() as c:
            c.execute("DELETE FROM cik_tickers")
            c.executemany("INSERT INTO cik_tickers VALUES (?,?,?)",
                          df[_TABLES["cik_tickers"]].itertuples(index=False, name=None))
        return len(df)

    def read(self, table: str) -> pd.DataFrame:
        cols = _TABLES[table]
        with self._conn() as c:
            return pd.read_sql_query(f"SELECT {', '.join(cols)} FROM {table}", c)

    def counts(self) -> dict[str, int]:
        with self._conn() as c:
            return {t: int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                    for t in [*_TABLES, "sec_pulls"]}

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=60)
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def _iso(t: datetime) -> str:
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
