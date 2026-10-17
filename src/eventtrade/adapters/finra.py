"""FINRA Reg SHO daily short-sale volume files (ADR 0019): one pipe-delimited file per facility
and trading day at cdn.finra.org. The consolidated NMS file (CNMS) only exists from 2018-08, so
the series is built for every day as the sum of the NMS facility files (FNSQ, FNQC, FNYX, FNRA)
that exist that day; the OTC file (FORF) is excluded. No User-Agent email (not SEC)."""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd
import requests

URL = "https://cdn.finra.org/equity/regsho/daily/{fac}shvol{ymd}.txt"
FACILITIES = ("FNSQ", "FNQC", "FNYX", "FNRA")
UA = {"User-Agent": "Mozilla/5.0 (eventtrade research)"}
MIN_INTERVAL = 0.1


class FinraClient:
    def __init__(self, session: Any = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.http = session or requests.Session()
        self.sleep, self.clock = sleep, clock
        self._last = float("-inf")

    def get(self, url: str) -> str | None:
        """Text of the file, None if it does not exist (403/404); retries other failures."""
        err = ""
        for attempt in range(5):
            wait = MIN_INTERVAL - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            try:
                r = self.http.get(url, headers=UA, timeout=60)
            except requests.RequestException as e:
                err = repr(e)
            else:
                if r.status_code == 200:
                    return r.text
                if r.status_code in (403, 404):
                    return None
                err = f"HTTP {r.status_code}"
            self.sleep(min(30.0, 2.0 ** attempt))
        raise RuntimeError(f"{url}: gave up ({err})")


def parse_file(text: str) -> pd.DataFrame:
    """[symbol, short, total] from one facility file (header row, trailing footer lines)."""
    df = pd.read_csv(io.StringIO(text), sep="|", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df[df["Symbol"].notna() & df["Date"].str.fullmatch(r"\d{8}", na=False)]
    return pd.DataFrame({"symbol": df["Symbol"].str.strip(),
                         "short": pd.to_numeric(df["ShortVolume"], errors="coerce"),
                         "total": pd.to_numeric(df["TotalVolume"], errors="coerce")}).dropna()


def day_short_volume(client: FinraClient, d: date, symbols: set[str] | None) -> pd.DataFrame:
    """[date, symbol, short, total, n_fac]: the NMS facilities summed for day d (empty if none)."""
    parts = []
    for fac in FACILITIES:
        txt = client.get(URL.format(fac=fac, ymd=d.strftime("%Y%m%d")))
        if txt:
            p = parse_file(txt)
            if symbols is not None:
                p = p[p["symbol"].isin(symbols)]
            parts.append(p.assign(fac=fac))
    if not parts:
        return pd.DataFrame(columns=["date", "symbol", "short", "total", "n_fac"])
    df = pd.concat(parts)
    g = df.groupby("symbol").agg(short=("short", "sum"), total=("total", "sum"),
                                 n_fac=("fac", "nunique")).reset_index()
    return g.assign(date=d.isoformat())[["date", "symbol", "short", "total", "n_fac"]]
