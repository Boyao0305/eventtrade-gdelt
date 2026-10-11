"""SEC EDGAR sources for ADR 0011: Insider Transactions Data Sets (Form 3/4/5, quarterly zips) and
the EDGAR full-text search index (13D filings), the submissions API (10-Q/10-K/8-K/DEFM14A dates)
and 13D header pages (subject company). Free; the SEC requires a User-Agent that
identifies the requester and at most 10 requests per second.

Only open-market purchases are kept from the insider data (non-derivative TRANS_CODE 'P'), with
their submissions and reporting owners. Hold-out discipline is the caller's job.
"""

from __future__ import annotations

import io
import json
import re
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


SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
HEADERS_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{adsh}-index-headers.html"
SUBMISSION_FORMS = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A", "8-K", "DEFM14A"})
SUBMISSIONS_FROM, SUBMISSIONS_TO = "2015-01-01", "2025-03-31"
SUBMISSION_COLS = ["cik", "form", "filing_date", "items", "accession"]


def company_submissions(client: SecClient, cik: str) -> pd.DataFrame:
    """One company's 10-Q/10-K (and amendments), 8-K and DEFM14A filings dated 2015-01-01 ..
    2025-03-31, from the EDGAR submissions API: `filings.recent` plus every older page listed in
    `filings.files` whose date span reaches into that window (pages entirely before it are not
    fetched). `items` is the 8-K item string ('1.01,9.01'); filings on/after the hold-out are
    dropped here, before anything is stored."""
    cik = str(cik).zfill(10)
    d = client.get(SUBMISSIONS_URL.format(name=f"CIK{cik}.json")).json()
    filings = d.get("filings") or {}
    pages = [filings.get("recent") or {}]
    for f in filings.get("files") or []:
        if (f.get("filingTo") or "9999") < SUBMISSIONS_FROM or \
                (f.get("filingFrom") or "") > SUBMISSIONS_TO:
            continue
        pages.append(client.get(SUBMISSIONS_URL.format(name=f["name"])).json())
    rows = []
    for p in pages:
        forms, dates, accs = p.get("form") or [], p.get("filingDate") or [], \
            p.get("accessionNumber") or []
        items = p.get("items") or [""] * len(forms)
        for form, fd, it, acc in zip(forms, dates, items, accs, strict=False):
            if form in SUBMISSION_FORMS and SUBMISSIONS_FROM <= fd <= SUBMISSIONS_TO:
                rows.append((cik, form, fd, it or "", acc))
    df = pd.DataFrame(rows, columns=SUBMISSION_COLS)
    return df.drop_duplicates("accession").sort_values(["filing_date", "accession"]) \
        .reset_index(drop=True)


PROFILE_FORMS = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A", "8-K", "DEFM14A", "S-4",
                           "8-A12B", "8-A12G", "25", "25-NSE", "15-12B", "15-12G", "15-15D"})
PROFILE_ANY_DATE = frozenset({"8-A12B", "8-A12G"})  # registrations: kept whatever their date


def company_profile(client: SecClient, cik: str) -> tuple[dict[str, Any], pd.DataFrame]:
    """One company's identity and listing history for ADR 0012 (amendments 1 and 3), from the
    EDGAR submissions API (`filings.recent` and every older page in `filings.files`).

    meta: cik (10 digits), name (current, i.e. 2026 information), sic, former_names (JSON list
    of {name, from, to}, dates 'YYYY-MM-DD' or None). Hold-out discipline: a former name taken
    after 2025-03-31 is dropped, and a `to` after 2025-03-31 is stored as None (the name was
    still in use at the end of the window).
    filings: columns SUBMISSION_COLS; forms PROFILE_FORMS. 8-A12B/8-A12G are kept at any date
    (a registration before 2015 tells when the issuer listed); every other form only
    2015-01-01..2025-03-31, so nothing on/after the hold-out is returned.
    """
    cik = str(cik).zfill(10)
    d = client.get(SUBMISSIONS_URL.format(name=f"CIK{cik}.json")).json()
    filings = d.get("filings") or {}
    pages = [filings.get("recent") or {}]
    pages += [client.get(SUBMISSIONS_URL.format(name=f["name"])).json()
              for f in filings.get("files") or []]
    rows = []
    for p in pages:
        forms, dates, accs = p.get("form") or [], p.get("filingDate") or [], \
            p.get("accessionNumber") or []
        items = p.get("items") or [""] * len(forms)
        for form, fd, it, acc in zip(forms, dates, items, accs, strict=False):
            if form in PROFILE_ANY_DATE or \
                    (form in PROFILE_FORMS and SUBMISSIONS_FROM <= fd <= SUBMISSIONS_TO):
                rows.append((cik, form, fd, it or "", acc))
    df = pd.DataFrame(rows, columns=SUBMISSION_COLS)
    df = df.drop_duplicates("accession").sort_values(["filing_date", "accession"]) \
        .reset_index(drop=True)
    former = []
    for f in d.get("formerNames") or []:
        start, end = (f.get("from") or "")[:10] or None, (f.get("to") or "")[:10] or None
        if start and start > SUBMISSIONS_TO:
            continue  # a name taken on/after the hold-out start
        former.append({"name": f.get("name"), "from": start,
                       "to": end if end and end <= SUBMISSIONS_TO else None})
    meta = {"cik": cik, "name": d.get("name"), "sic": str(d.get("sic") or "") or None,
            "former_names": json.dumps(former)}
    return meta, df


_SUBJECT_TEXT = re.compile(r"SUBJECT COMPANY:(.*?)(?:FILED BY:|</PRE>|$)", re.DOTALL)
_SUBJECT_SGML = re.compile(r"<SUBJECT-COMPANY>(.*?)</SUBJECT-COMPANY>", re.DOTALL)
_CIK_TEXT = re.compile(r"CENTRAL INDEX KEY:\s*(\d+)")
_CIK_SGML = re.compile(r"<CIK>\s*(\d+)")


def parse_subject_cik(html: str) -> str | None:
    """SUBJECT COMPANY's CENTRAL INDEX KEY of an `-index-headers.html` page (10 digits)."""
    for block, key in ((_SUBJECT_TEXT, _CIK_TEXT), (_SUBJECT_SGML, _CIK_SGML)):
        m = block.search(html)
        if m and (k := key.search(m.group(1))):
            return k.group(1).zfill(10)
    return None


def filing_subject(client: SecClient, ciks: str | list[str], adsh: str) -> str | None:
    """Subject company CIK of one filing, from its header page. The filing's folder lives under
    each of its CIKs in principle but not always, so each listed CIK is tried in order until one
    answers; a CIK whose folder is missing (HTTP 4xx) is skipped. None if no folder answers or
    the header has no SUBJECT COMPANY. Server errors that outlast the retries are raised."""
    for cik in [ciks] if isinstance(ciks, str) else ciks:
        url = HEADERS_URL.format(cik=int(cik), nodash=adsh.replace("-", ""), adsh=adsh)
        try:
            r = client.get(url)
        except SecError as e:
            if "gave up" in str(e):
                raise
            continue
        return parse_subject_cik(r.text)
    return None


def parse_sec_date(s: str) -> date:
    """'31-MAR-2016' (data sets) or '2016-03-31' (efts) -> date."""
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()  # noqa: DTZ007 (a calendar date)
        except ValueError:
            continue
    raise ValueError(f"unrecognised SEC date {s!r}")


CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{tax}/{tag}.json"
SHARES_TAGS = (("dei", "EntityCommonStockSharesOutstanding"),
               ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"))
SHARES_COLS = ["cik", "tag", "end", "val", "filed", "form", "accn"]


def shares_outstanding(client: SecClient, cik: str) -> pd.DataFrame:
    """ADR 0015: one company's share counts from XBRL company concepts, filed before the hold-out.

    The cover-page dei:EntityCommonStockSharesOutstanding first; when it has no
    (non-dimensional) fact, as for most multi-class issuers, us-gaap basic weighted-average
    shares instead. Columns SHARES_COLS; empty if neither concept exists (HTTP 404)."""
    cik = str(cik).zfill(10)
    for tax, tag in SHARES_TAGS:
        try:
            d = client.get(CONCEPT_URL.format(cik=cik, tax=tax, tag=tag)).json()
        except SecError as e:
            if "HTTP 404" in str(e):
                continue
            raise
        rows = [(cik, tag, f.get("end"), float(f["val"]), f.get("filed"), f.get("form"),
                 f.get("accn"))
                for f in (d.get("units") or {}).get("shares", [])
                if f.get("val") is not None and (f.get("filed") or "9999") <= SUBMISSIONS_TO]
        if rows:
            return pd.DataFrame(rows, columns=SHARES_COLS).drop_duplicates() \
                .sort_values(["filed", "end"]).reset_index(drop=True)
    return pd.DataFrame(columns=SHARES_COLS)


EX21_FORMS = "10-K,10-K405,10-KT"  # EFTS matches root forms, so amendments (10-K/A) come too
EX21_FROM, EX21_TO = "2014-01-01", SUBMISSIONS_TO
EFTS_CAP = 10_000
EX21_COLS = ["phrase", "cik", "file_date", "adsh", "doc"]
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{doc}"


def ex21_parents(client: SecClient, phrase: str, query: str | None = None) -> pd.DataFrame:
    """ADR 0015 amendment 3: Exhibit 21 documents of 10-Ks filed 2014-01-01..2025-03-31 that
    EDGAR full-text search finds for `phrase`.

    `query` is the EFTS q string (default: the quoted phrase). Every page is read (100 hits a
    page) up to EFTS's 10,000-hit window; only documents whose file_type starts with 'EX-21'
    are kept; cik is the first listed CIK (the filer) and doc the document's file name. Columns
    EX21_COLS, one row per (cik, adsh, doc). attrs: total (EFTS hit count) and capped (True if
    the window was hit and the remaining pages could not be read). A hit only says the words
    occur in the exhibit; `ex21_document` + `core.dod_issuers.ex21_entries` verify the entry."""
    rows: dict[tuple[str, str, str], tuple[str, str, str, str, str]] = {}
    offset, total, capped = 0, 0, False
    while True:
        params = {"q": query or f'"{phrase}"', "forms": EX21_FORMS, "dateRange": "custom",
                  "startdt": EX21_FROM, "enddt": EX21_TO, "from": offset}
        hits = client.get(EFTS_URL, params).json()["hits"]
        page = hits["hits"]
        total = int(hits["total"]["value"])
        for h in page:
            s = h.get("_source") or {}
            if not str(s.get("file_type") or "").upper().startswith("EX-21"):
                continue
            ciks = s.get("ciks") or []
            _id = str(h.get("_id", ""))
            if not ciks or ":" not in _id:
                continue
            cik = str(ciks[0]).zfill(10)
            adsh = s.get("adsh") or _id.split(":")[0]
            doc = _id.split(":", 1)[1]
            rows.setdefault((cik, adsh, doc), (phrase, cik, s.get("file_date"), adsh, doc))
        offset += len(page)
        if not page or offset >= total:
            break
        if offset >= EFTS_CAP:
            capped = True
            break
    df = pd.DataFrame(list(rows.values()), columns=EX21_COLS)         .sort_values(["file_date", "cik", "adsh", "doc"]).reset_index(drop=True)
    df.attrs.update(total=total, capped=capped)
    return df


def ex21_document(client: SecClient, cik: str, adsh: str, doc: str) -> str:
    """Text of one Exhibit 21 document from the EDGAR archive ('' if it is gone: HTTP 404)."""
    url = ARCHIVE_URL.format(cik=int(cik), nodash=adsh.replace("-", ""), doc=doc)
    try:
        return client.get(url).text
    except SecError as e:
        if "HTTP 404" in str(e):
            return ""
        raise


Names = list[tuple[str, str | None, str | None]]


def former_names(client: SecClient, cik: str) -> tuple[Names, str | None]:
    """ADR 0015 amendment 3: a CIK's EDGAR names with validity dates ('YYYY-MM-DD' or None =
    open), and its last filing date, from the submissions API: every `formerNames` entry (from,
    to), plus the current `name`, valid from the latest former 'to' (None if no former name)
    onwards; last filing = the latest `filings.recent.filingDate` (None if none). An unknown CIK
    (HTTP 404) has no names."""
    cik = str(cik).zfill(10)
    try:
        d = client.get(SUBMISSIONS_URL.format(name=f"CIK{cik}.json")).json()
    except SecError as e:
        if "HTTP 404" in str(e):
            return [], None
        raise
    out: Names = []
    ends: list[str] = []
    for f in d.get("formerNames") or []:
        if not f.get("name"):
            continue
        start, end = (f.get("from") or "")[:10] or None, (f.get("to") or "")[:10] or None
        out.append((str(f["name"]), start, end))
        if end:
            ends.append(end)
    if d.get("name"):
        out.append((str(d["name"]), max(ends) if ends else None, None))
    dates = [x for x in ((d.get("filings") or {}).get("recent") or {}).get("filingDate") or []
             if x]
    return out, (max(dates)[:10] if dates else None)
