"""US Department of Defense daily contract announcements, through the Internet Archive.

defense.gov answers direct fetches with an Akamai 403, so the articles are read from the Wayback
Machine: the CDX API lists every archived URL under the contract-article prefixes, and
`/web/<timestamp>id_/<url>` returns the page as captured (no Wayback toolbar). Free; we stay at
or below 1 request per second and identify ourselves with a plain User-Agent (no contact data).

An article id has captures under several URL forms over the years (/News/Contracts/Contract/
Article/<id>/, /Newsroom/Contracts/..., /News/Contracts/Contract-View/Article/<id>, with slugs,
query strings or a doubled slash). `list_snapshots` keeps every 200 capture per id, best first.
Parsing is `eventtrade.core.dod_parse`; hold-out discipline is the caller's job.
"""

from __future__ import annotations

import gzip
import re
import time
from collections.abc import Callable
from typing import Any

import requests

CDX_URL = "https://web.archive.org/cdx/search/cdx"
RAW_URL = "https://web.archive.org/web/{timestamp}id_/{url}"
PREFIXES = ("defense.gov/News/Contracts/Contract/Article/",
            "defense.gov/Newsroom/Contracts/Contract/Article/",
            "defense.gov/News/Contracts/Contract-View/Article/")
USER_AGENT = "eventtrade-research/0.1"
MIN_INTERVAL = 1.0  # seconds between requests (<= 1 request/s)
MAX_RETRIES = 6
RETRY_STATUS = frozenset({429, 500, 502, 503, 504, 520, 522, 524})
TIMEOUT = 120

_ID = re.compile(r"/Article/(\d+)(?=/|$|\?|#)")


class DodError(RuntimeError):
    pass


class WaybackClient:
    """GET with a 1 request/s floor and exponential backoff on 429, 5xx, timeouts and connection
    errors. Other HTTP errors raise DodError at once."""

    def __init__(self, session: Any = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 min_interval: float = MIN_INTERVAL):
        self.headers = {"User-Agent": USER_AGENT}
        self.http = session or requests.Session()
        self.sleep, self.clock, self.min_interval = sleep, clock, min_interval
        self._last = float("-inf")

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        err = ""
        for attempt in range(MAX_RETRIES):
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            try:
                r = self.http.get(url, params=params, headers=self.headers, timeout=TIMEOUT)
            except requests.RequestException as e:
                err = repr(e)
            else:
                if r.status_code == 200:
                    return r
                err = f"HTTP {r.status_code}"
                if r.status_code not in RETRY_STATUS:
                    raise DodError(f"{url}: {err}")
            self.sleep(min(120.0, 5.0 * 2.0 ** attempt))
        raise DodError(f"{url}: gave up after {MAX_RETRIES} attempts ({err})")


def article_id(url: str) -> int | None:
    """Numeric article id of a contract-article URL ('/Article/1000857/', '.../1611035/dei08/',
    '...//?ref=x'); None for the listing page or 'Article/?2399096=...'."""
    m = _ID.search(url)
    return int(m.group(1)) if m else None


def _rank(url: str) -> tuple[int, ...]:
    """Lower is better: canonical path, no query string, no slug, https, newest URL scheme."""
    path = url.split("://", 1)[-1]
    clean = re.search(r"/Article/\d+/?$", path) is not None
    prefix = next((i for i, p in enumerate(PREFIXES) if p.lower() in path.lower()), len(PREFIXES))
    return (int("?" in path), int(not clean), int("//" in path.split("/Article/")[-1]),
            prefix, int(not url.startswith("https")))


def list_snapshots(client: WaybackClient, prefixes: tuple[str, ...] = PREFIXES,
                   echo: Callable[[str], None] = lambda s: None
                   ) -> dict[int, list[tuple[str, str]]]:
    """Every article id archived under the prefixes with an HTTP 200 capture, mapped to its
    captures [(timestamp, original url), ...], best URL form first (ties: oldest first)."""
    caps: dict[int, set[tuple[str, str]]] = {}
    for prefix in prefixes:
        base = {"url": prefix, "matchType": "prefix"}
        n_pages = int(client.get(CDX_URL, {**base, "showNumPages": "true"}).text.strip() or 0)
        for page in range(n_pages):
            r = client.get(CDX_URL, {**base, "filter": "statuscode:200", "collapse": "urlkey",
                                     "fl": "timestamp,original", "page": page})
            for line in r.text.splitlines():
                ts, _, url = line.strip().partition(" ")
                if ts.isdigit() and url and (aid := article_id(url)) is not None:
                    caps.setdefault(aid, set()).add((ts, url))
            echo(f"  CDX {prefix} page {page + 1}/{n_pages}: {len(caps)} ids so far")
    return {aid: sorted(v, key=lambda c: (_rank(c[1]), c[0])) for aid, v in sorted(caps.items())}


def fetch_article(client: WaybackClient, timestamp: str, url: str) -> str:
    """Raw HTML of one capture (gzip bodies served without a Content-Encoding are inflated)."""
    content = client.get(RAW_URL.format(timestamp=timestamp, url=url)).content
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content.decode("utf-8", errors="replace")
