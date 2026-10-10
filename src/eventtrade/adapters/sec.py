"""SEC EDGAR sources for ADR 0011: Insider Transactions Data Sets (Form 3/4/5, quarterly zips) and
the EDGAR full-text search index (13D filings). Free; the SEC requires a User-Agent that
identifies the requester and at most 10 requests per second.

Only open-market purchases are kept from the insider data (non-derivative TRANS_CODE 'P'), with
their submissions and reporting owners. Hold-out discipline is the caller's job.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import pandas as pd
import requests

INSIDER_URL = ("https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/"
               "{year}q{q}_form345.zip")
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
MIN_INTERVAL = 0.15  # seconds between requests (< 10/s)
MAX_RETRIES = 6
EFTS_PAGE = 100

SUB_COLS = ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERTRADINGSYMBOL"]
OWNER_COLS = ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP",
              "RPTOWNER_TITLE"]
TRANS_COLS = ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
              "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD", "SECURITY_TITLE"]


class SecError(RuntimeError):
    pass


class SecClient:
    def __init__(self, user_agent: str, session: Any = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        if "@" not in user_agent:
            raise SecError("the SEC requires a User-Agent with a contact email")
        self.headers = {"User-Agent": user_agent}
        self.http = session or requests.Session()
        self.sleep, self.clock = sleep, clock
        self._last = float("-inf")

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        err = ""
        for attempt in range(MAX_RETRIES):
            wait = MIN_INTERVAL - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            try:
                r = self.http.get(url, params=params, headers=self.headers, timeout=120)
            except requests.RequestException as e:
                err = repr(e)
            else:
                if r.status_code == 200:
                    return r
                err = f"HTTP {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise SecError(f"{url}: {err}")
            self.sleep(min(60.0, 2.0 ** attempt))
        raise SecError(f"{url}: gave up after {MAX_RETRIES} attempts ({err})")


def insider_quarter(client: SecClient, year: int, q: int) -> dict[str, pd.DataFrame]:
    """Submissions, reporting owners and open-market purchases ('P') of one quarterly data set."""
    r = client.get(INSIDER_URL.format(year=year, q=q))
    z = zipfile.ZipFile(io.BytesIO(r.content))
    trans = pd.read_csv(z.open("NONDERIV_TRANS.tsv"), sep="\t", dtype=str, usecols=TRANS_COLS)
    trans = trans[trans["TRANS_CODE"] == "P"]
    acc = set(trans["ACCESSION_NUMBER"])
    sub = pd.read_csv(z.open("SUBMISSION.tsv"), sep="\t", dtype=str, usecols=SUB_COLS)
    own = pd.read_csv(z.open("REPORTINGOWNER.tsv"), sep="\t", dtype=str, usecols=OWNER_COLS)
    return {"subs": sub[sub["ACCESSION_NUMBER"].isin(acc)].reset_index(drop=True),
            "owners": own[own["ACCESSION_NUMBER"].isin(acc)].reset_index(drop=True),
            "trans": trans.reset_index(drop=True)}


def filings_13d(client: SecClient, start: date, end: date) -> pd.DataFrame:
    """All filings of root form 13D filed start..end (inclusive), every page.

    Columns: adsh, form, file_date, ciks (JSON list, as listed), names (JSON list).
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        params = {"forms": "SC 13D,SCHEDULE 13D", "dateRange": "custom",
                  "startdt": start.isoformat(), "enddt": end.isoformat(), "from": offset}
        hits = client.get(EFTS_URL, params).json()["hits"]
        page = hits["hits"]
        for h in page:
            s = h["_source"]
            key = s.get("adsh") or h.get("_id")
            if key in seen:
                continue
            seen.add(key)
            rows.append({"adsh": key, "form": s.get("form") or s.get("file_type"),
                         "file_date": s.get("file_date"), "ciks": json.dumps(s.get("ciks") or []),
                         "names": json.dumps(s.get("display_names") or [])})
        offset += len(page)
        total = int(hits["total"]["value"])
        if not page or offset >= total:
            break
        if offset >= 10_000:
            raise SecError(f"{start}..{end}: more than 10,000 hits; query a shorter range")
    return pd.DataFrame(rows, columns=["adsh", "form", "file_date", "ciks", "names"])


def company_tickers(client: SecClient) -> pd.DataFrame:
    """Current CIK -> ticker map (cik as 10-digit string, ticker upper-case)."""
    d = client.get(TICKERS_URL).json()
    rows = [(str(v["cik_str"]).zfill(10), str(v["ticker"]).upper(), v.get("title", ""))
            for v in d.values()]
    return pd.DataFrame(rows, columns=["cik", "ticker", "title"])


def parse_sec_date(s: str) -> date:
    """'31-MAR-2016' (data sets) or '2016-03-31' (efts) -> date."""
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()  # noqa: DTZ007 (a calendar date)
        except ValueError:
            continue
    raise ValueError(f"unrecognised SEC date {s!r}")
