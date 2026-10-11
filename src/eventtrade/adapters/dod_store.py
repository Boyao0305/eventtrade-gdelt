"""SQLite store for DoD daily contract announcements: `data/dod.sqlite`.

Tables:
- dod_snapshots(article_id, rank, timestamp, url)   Wayback captures from the CDX listing, best
                                                    first (rank 0); cached so a resumed run does
                                                    not list again
- dod_articles(article_id, snapshot, url, release_date, n_awards, status, fetched_at)
      one row per article id attempted. status: 'ok' (awards stored), 'out_of_range' (release
      date outside [start, holdout_start): no awards stored), 'not_contracts' (not a "Contracts
      For" article), 'no_date' (no release date found in any capture), 'fetch_failed'.
      'ok', 'out_of_range' and 'not_contracts' are final; the others are retried by a rerun.
- dod_awards(article_id, seq, release_date, agency, awardees, amount, kind, small_business,
      text)   awardees: JSON list of names; kind: contract / modification / order / other /
      correction; small_business 0/1.

Hold-out discipline is enforced here too: awards are written only for start <= release_date <
holdout_start, whatever the caller passes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

FINAL = ("ok", "out_of_range", "not_contracts")

_DDL = """
CREATE TABLE IF NOT EXISTS dod_snapshots (article_id INTEGER NOT NULL, rank INTEGER NOT NULL,
    timestamp TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY (article_id, rank));
CREATE TABLE IF NOT EXISTS dod_articles (article_id INTEGER PRIMARY KEY, snapshot TEXT,
    url TEXT, release_date TEXT, n_awards INTEGER, status TEXT NOT NULL, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS dod_awards (article_id INTEGER NOT NULL, seq INTEGER NOT NULL,
    release_date TEXT NOT NULL, agency TEXT, awardees TEXT NOT NULL, amount REAL, kind TEXT,
    small_business INTEGER, text TEXT, PRIMARY KEY (article_id, seq));
CREATE INDEX IF NOT EXISTS ix_dod_awards_date ON dod_awards(release_date);
"""


class DodStore:
    def __init__(self, path: Path, start: date, holdout_start: date):
        self.path = Path(path)
        self.start, self.holdout_start = start, holdout_start

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_DDL)

    def in_range(self, d: date | None) -> bool:
        return d is not None and self.start <= d < self.holdout_start

    def write_snapshots(self, snaps: dict[int, list[tuple[str, str]]]) -> int:
        """Replace the cached CDX listing."""
        rows = [(aid, k, ts, url) for aid, caps in snaps.items() for k, (ts, url) in
                enumerate(caps)]
        with self._conn() as c:
            c.execute("DELETE FROM dod_snapshots")
            c.executemany("INSERT INTO dod_snapshots VALUES (?,?,?,?)", rows)
        return len(snaps)

    def read_snapshots(self) -> dict[int, list[tuple[str, str]]]:
        out: dict[int, list[tuple[str, str]]] = {}
        with self._conn() as c:
            for aid, ts, url in c.execute(
                    "SELECT article_id, timestamp, url FROM dod_snapshots"
                    " ORDER BY article_id, rank"):
                out.setdefault(int(aid), []).append((ts, url))
        return out

    def done(self) -> set[int]:
        """Article ids with a final status (skipped by a resumed run)."""
        with self._conn() as c:
            q = f"SELECT article_id FROM dod_articles WHERE status IN ({','.join('?' * len(FINAL))})"
            return {int(a) for (a,) in c.execute(q, FINAL)}

    def write_article(self, article_id: int, snapshot: str | None, url: str | None,
                      release_date: date | None, status: str, awards: list[dict[str, Any]],
                      now: datetime) -> str:
        """Record one article and (re)write its awards atomically; returns the stored status.
        A release date outside [start, holdout_start) forces status 'out_of_range' and no
        awards."""
        if status == "ok" and not self.in_range(release_date):
            status = "out_of_range"
        if status != "ok":
            awards = []
        rd = release_date.isoformat() if release_date else None
        with self._conn() as c:
            c.execute("DELETE FROM dod_awards WHERE article_id = ?", (article_id,))
            c.executemany("INSERT INTO dod_awards VALUES (?,?,?,?,?,?,?,?,?)", [
                (article_id, int(a["seq"]), rd, a.get("agency"), json.dumps(a["awardees"]),
                 a.get("amount"), a.get("kind"), int(bool(a.get("small_business"))),
                 a.get("text")) for a in awards])
            c.execute("INSERT OR REPLACE INTO dod_articles VALUES (?,?,?,?,?,?,?)",
                      (article_id, snapshot, url, rd, len(awards), status, _iso(now)))
        return status

    def articles(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM dod_articles ORDER BY article_id")]

    def awards(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM dod_awards ORDER BY article_id, seq")]
        for r in rows:
            r["awardees"] = json.loads(r["awardees"])
        return rows

    def counts(self) -> dict[str, int]:
        with self._conn() as c:
            return {t: int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                    for t in ("dod_snapshots", "dod_articles", "dod_awards")}

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
