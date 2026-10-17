"""SQLite store for SEC filings (ADR 0011): `data/sec.sqlite`.

Tables (raw, as the SEC publishes them; columns lower-cased):
- insider_subs(accession_number, filing_date, document_type, issuercik, issuertradingsymbol)
- insider_owners(accession_number, rptownercik, rptownername, rptowner_relationship, rptowner_title)
- insider_trans(accession_number, trans_date, trans_code, trans_shares, trans_pricepershare,
                trans_acquired_disp_cd, security_title)       ('P' transactions only)
- filings_13d(adsh, form, file_date, ciks, names)               (ciks/names: JSON lists)
- cik_tickers(cik, ticker, title)
- company_filings(cik, form, filing_date, items, accession)   10-Q/10-K (+/A), 8-K (items
                                             '1.01,9.01') and DEFM14A of universe companies,
                                             2015-01-01..2025-03-31
- subject_13d(adsh, subject_cik)             header SUBJECT COMPANY CIK of initial 13Ds (NULL: none)
- company_meta(cik, name, sic, former_names) ADR 0012 issuer identity (former_names: JSON list of
                                             {name, from, to}; see sec.company_profile)
- company_filings2(cik, form, filing_date, items, accession)  ADR 0012 profile filings: forms
                                             sec.PROFILE_FORMS, 8-A12B/8-A12G at any date, others
                                             2015-01-01..2025-03-31
- ex21_hits(phrase, cik, file_date, adsh, doc)  ADR 0015 amendment 3: 10-K Exhibit 21
                                             documents that full-text search finds for a
                                             normalised awardee name (cik = filer)
- ex21_entries(adsh, doc, cik, file_date, entry_norm)  normalised subsidiary entries parsed
                                             from those documents
- sec_names(cik, name, valid_from, valid_to, last_filing)  ADR 0015 amendment 3: current and
                                             former EDGAR names with validity dates (None =
                                             open) and the CIK's last filing date
- sec_pulls(kind, period, rows, pulled_at)   resume ledger: kind 'insider' (period '2016q1'),
                                             '13d' (period '2016-03'), 'submissions' (period =
                                             cik), 'subject13d' (period = adsh), 'profile'
                                             (period = cik), 'shares' (period = cik), 'ex21'
                                             (period = phrase; 'ex21cap' too when EFTS's
                                             10,000-hit window cut the search short, rows =
                                             total hits), 'ex21doc' (period =
                                             'v<parser version>:adsh/doc') or 'names'
                                             (period = cik)
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
    "company_filings": ["cik", "form", "filing_date", "items", "accession"],
    "subject_13d": ["adsh", "subject_cik"],
    "company_meta": ["cik", "name", "sic", "former_names"],
    "company_filings2": ["cik", "form", "filing_date", "items", "accession"],
    "shares_out": ["cik", "tag", "end", "val", "filed", "form", "accn"],
    "xbrl_facts": ["cik", "tag", "start", "end", "val", "fy", "fp", "form", "filed", "accn"],
    "eightk_times": ["cik", "form", "filing_date", "report_date", "acceptance", "items",
                     "accession"],
    "ex21_hits": ["phrase", "cik", "file_date", "adsh", "doc"],
    "ex21_entries": ["adsh", "doc", "cik", "file_date", "entry_norm"],
    "sec_names": ["cik", "name", "valid_from", "valid_to", "last_filing"],
}
_DDL = "\n".join(f"CREATE TABLE IF NOT EXISTS {t} ({', '.join(c + ' TEXT' for c in cols)});"
                 for t, cols in _TABLES.items()) + """
CREATE INDEX IF NOT EXISTS ix_subs_acc ON insider_subs(accession_number);
CREATE INDEX IF NOT EXISTS ix_owners_acc ON insider_owners(accession_number);
CREATE INDEX IF NOT EXISTS ix_trans_acc ON insider_trans(accession_number);
CREATE UNIQUE INDEX IF NOT EXISTS ux_13d ON filings_13d(adsh);
CREATE INDEX IF NOT EXISTS ix_cf_cik ON company_filings(cik, filing_date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_subject ON subject_13d(adsh);
CREATE UNIQUE INDEX IF NOT EXISTS ux_meta ON company_meta(cik);
CREATE INDEX IF NOT EXISTS ix_cf2_cik ON company_filings2(cik, filing_date);
CREATE INDEX IF NOT EXISTS ix_shares_cik ON shares_out(cik, filed);
CREATE INDEX IF NOT EXISTS ix_facts_cik ON xbrl_facts(cik, tag, filed);
CREATE INDEX IF NOT EXISTS ix_ex21_phrase ON ex21_hits(phrase);
CREATE INDEX IF NOT EXISTS ix_names_cik ON sec_names(cik);
CREATE INDEX IF NOT EXISTS ix_ex21e_norm ON ex21_entries(entry_norm);
CREATE INDEX IF NOT EXISTS ix_ex21e_doc ON ex21_entries(adsh, doc);
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
            _migrate(c)
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

    def write_submissions(self, cik: str, df: pd.DataFrame, now: datetime) -> int:
        """Replace one company's filings and record its CIK in the ledger, atomically."""
        cols = _TABLES["company_filings"]
        with self._conn() as c:
            c.execute("DELETE FROM company_filings WHERE cik = ?", (cik,))
            c.executemany(f"INSERT INTO company_filings VALUES ({','.join('?' * len(cols))})",
                          df[cols].itertuples(index=False, name=None))
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("submissions", cik, len(df), _iso(now)))
        return len(df)

    def write_subject(self, adsh: str, subject_cik: str | None, now: datetime) -> None:
        """Store one 13D's subject CIK (None: not found) and record it in the ledger."""
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO subject_13d VALUES (?,?)", (adsh, subject_cik))
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("subject13d", adsh, int(subject_cik is not None), _iso(now)))

    def write_profile(self, meta: dict, df: pd.DataFrame, now: datetime) -> int:
        """Replace one company's meta row and profile filings and record its CIK in the ledger
        (kind 'profile'), atomically."""
        cik = meta["cik"]
        cols = _TABLES["company_filings2"]
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO company_meta VALUES (?,?,?,?)",
                      tuple(meta[k] for k in _TABLES["company_meta"]))
            c.execute("DELETE FROM company_filings2 WHERE cik = ?", (cik,))
            c.executemany(f"INSERT INTO company_filings2 VALUES ({','.join('?' * len(cols))})",
                          df[cols].itertuples(index=False, name=None))
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("profile", cik, len(df), _iso(now)))
        return len(df)

    def write_shares(self, cik: str, df: pd.DataFrame, now: datetime) -> int:
        """Replace one company's share counts and record its CIK (kind 'shares'), atomically."""
        cols = _TABLES["shares_out"]
        with self._conn() as c:
            c.execute("DELETE FROM shares_out WHERE cik = ?", (cik,))
            c.executemany(f"INSERT INTO shares_out VALUES ({','.join('?' * len(cols))})",
                          df[cols].itertuples(index=False, name=None))
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("shares", cik, len(df), _iso(now)))
        return len(df)

    def write_ex21(self, phrase: str, df: pd.DataFrame, now: datetime,
                   capped: bool = False, total: int | None = None) -> int:
        """Replace one phrase's Exhibit 21 hits and record it (kind 'ex21'; also 'ex21cap' if
        the search was cut short), atomically."""
        cols = _TABLES["ex21_hits"]
        with self._conn() as c:
            c.execute("DELETE FROM ex21_hits WHERE phrase = ?", (phrase,))
            c.executemany(f"INSERT INTO ex21_hits VALUES ({','.join('?' * len(cols))})",
                          df[cols].astype(object).where(df[cols].notna(), None)
                          .itertuples(index=False, name=None))
            c.execute("DELETE FROM sec_pulls WHERE kind = 'ex21cap' AND period = ?", (phrase,))
            if capped:
                c.execute("INSERT INTO sec_pulls VALUES (?,?,?,?)",
                          ("ex21cap", phrase, int(total or 0), _iso(now)))
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("ex21", phrase, len(df), _iso(now)))
        return len(df)

    def write_names(self, cik: str, names: list[tuple[str, str | None, str | None]],
                    now: datetime, last_filing: str | None = None) -> int:
        """Replace one CIK's (name, valid_from, valid_to) rows, with its last filing date, and
        record it (kind 'names')."""
        with self._conn() as c:
            c.execute("DELETE FROM sec_names WHERE cik = ?", (cik,))
            c.executemany("INSERT INTO sec_names VALUES (?,?,?,?,?)",
                          [(cik, n, a, b, last_filing) for n, a, b in names])
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("names", cik, len(names), _iso(now)))
        return len(names)

    def write_ex21_entries(self, adsh: str, doc: str, cik: str, file_date: str,
                           entries: set[str] | list[str], now: datetime,
                           version: int = 1) -> int:
        """Replace one Exhibit 21 document's normalised entries and record it (kind 'ex21doc',
        period 'v<version>:adsh/doc', version = the entry parser's), atomically. No entries
        (document gone or unparsable) is recorded too."""
        with self._conn() as c:
            c.execute("DELETE FROM ex21_entries WHERE adsh = ? AND doc = ?", (adsh, doc))
            c.executemany("INSERT INTO ex21_entries VALUES (?,?,?,?,?)",
                          [(adsh, doc, cik, file_date, e) for e in sorted(entries)])
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("ex21doc", f"v{version}:{adsh}/{doc}", len(entries), _iso(now)))
        return len(entries)

    def read_ex21_entries(self, norms: set[str] | list[str]) -> pd.DataFrame:
        """ex21_entries rows whose entry_norm is one of `norms`."""
        cols = _TABLES["ex21_entries"]
        with self._conn() as c:
            c.execute("CREATE TEMP TABLE want (n TEXT PRIMARY KEY)")
            c.executemany("INSERT OR IGNORE INTO want VALUES (?)", [(n,) for n in norms])
            return pd.read_sql_query(
                f"SELECT {', '.join('e.' + x for x in cols)} FROM ex21_entries e"
                " JOIN want w ON w.n = e.entry_norm", c)

    def write_facts(self, cik: str, df: pd.DataFrame, now: datetime) -> int:
        """Replace one company's XBRL facts and record its CIK (kind 'facts2': EPS, revenue,
        net income, diluted shares), atomically."""
        cols = _TABLES["xbrl_facts"]
        with self._conn() as c:
            c.execute("DELETE FROM xbrl_facts WHERE cik = ?", (cik,))
            c.executemany(f"INSERT INTO xbrl_facts VALUES ({','.join('?' * len(cols))})",
                          df[cols].astype(object).where(df[cols].notna(), None)
                          .itertuples(index=False, name=None))
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("facts2", cik, len(df), _iso(now)))
        return len(df)

    def write_8k_times(self, cik: str, df: pd.DataFrame, now: datetime) -> int:
        """Replace one company's 8-K report dates / acceptance times (kind '8ktimes')."""
        cols = _TABLES["eightk_times"]
        with self._conn() as c:
            c.execute("DELETE FROM eightk_times WHERE cik = ?", (cik,))
            c.executemany(f"INSERT INTO eightk_times VALUES ({','.join('?' * len(cols))})",
                          df[cols].itertuples(index=False, name=None))
            c.execute("INSERT OR REPLACE INTO sec_pulls VALUES (?,?,?,?)",
                      ("8ktimes", cik, len(df), _iso(now)))
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


def _migrate(c: sqlite3.Connection) -> None:
    """ADR 0015 caches created before their last column existed are dropped (and their ledger
    entries cleared) so they are rebuilt: ex21_hits without `doc`, sec_names without
    `last_filing`. Other tables are never touched."""
    for table, col, kinds in (("ex21_hits", "doc", ("ex21", "ex21cap")),
                              ("sec_names", "last_filing", ("names",))):
        have = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        if have and col not in have:
            c.execute(f"DROP TABLE {table}")
            c.executemany("DELETE FROM sec_pulls WHERE kind = ?", [(k,) for k in kinds])


def _iso(t: datetime) -> str:
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
