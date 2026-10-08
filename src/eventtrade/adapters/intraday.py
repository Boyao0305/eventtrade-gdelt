"""Intraday bars from Alpaca market data (ADR 0007): 15-minute SIP bars, regular session only.

The free Basic plan serves historical SIP bars from 2016 for anything older than 15 minutes, at
200 requests per minute. The IEX feed is not used: it has no 2016 bars and ~2.5% of volume.
Credentials come from `<home>/secrets/alpaca.env` (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`).

Output frames follow `schemas.INTRADAY_COLUMNS`. Hold-out discipline is the caller's job: pass
`end < config.HOLDOUT_START` outside the final hold-out evaluation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from eventtrade.schemas import INTRADAY_COLUMNS

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
PAGE_LIMIT = 10_000
MIN_SECONDS_BETWEEN_REQUESTS = 60.0 / 180  # stay under the 200/min plan limit
MAX_RETRIES = 6
NY = ZoneInfo("America/New_York")
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 0)


class AlpacaError(RuntimeError):
    """Alpaca refused or kept failing a request."""


def load_credentials(path: Path) -> tuple[str, str]:
    """Read `KEY=VALUE` lines; return (key id, secret). Never logs the values."""
    if not path.is_file():
        raise AlpacaError(f"no Alpaca credentials at {path}")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    try:
        return env["APCA_API_KEY_ID"], env["APCA_API_SECRET_KEY"]
    except KeyError as e:
        raise AlpacaError(f"{path} lacks {e.args[0]}") from None


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep bars whose start lies in [09:30, 16:00) New York time."""
    if df.empty:
        return df
    et = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(NY)
    minutes = et.dt.hour * 60 + et.dt.minute
    lo = SESSION_OPEN[0] * 60 + SESSION_OPEN[1]
    hi = SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1]
    return df[(minutes >= lo) & (minutes < hi)].reset_index(drop=True)


def fetch_alpaca_bars(
    tickers: list[str],
    start: date,
    end: date,
    key: tuple[str, str],
    session: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> pd.DataFrame:
    """15-minute SIP bars for `tickers`, sessions `start`..`end` inclusive, regular session only.

    Pages through `next_page_token`; retries 429 and 5xx with exponential backoff. Tickers with
    no bars are simply absent from the result.
    """
    http = session or requests.Session()
    headers = {"APCA-API-KEY-ID": key[0], "APCA-API-SECRET-KEY": key[1]}
    t0 = datetime(start.year, start.month, start.day, tzinfo=NY)
    t1 = datetime(end.year, end.month, end.day, tzinfo=NY) + timedelta(days=1)
    params: dict[str, Any] = {
        "symbols": ",".join(tickers), "timeframe": "15Min", "feed": "sip", "adjustment": "all",
        "start": t0.isoformat(), "end": t1.isoformat(), "limit": PAGE_LIMIT, "sort": "asc",
    }
    rows: list[tuple] = []
    last = [float("-inf")]
    while True:
        payload = _get(http, headers, params, sleep, clock, last)
        for sym, bars in (payload.get("bars") or {}).items():
            for b in bars:
                ts = int(pd.Timestamp(b["t"]).timestamp())
                rows.append((ts, sym, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                             float(b["v"]), float(b.get("vw", "nan")), int(b.get("n", 0))))
        token = payload.get("next_page_token")
        if not token:
            break
        params = {**params, "page_token": token}
    df = pd.DataFrame(rows, columns=INTRADAY_COLUMNS)
    df = regular_session(df)
    return df.drop_duplicates(["ts", "ticker"], keep="last").sort_values(
        ["ticker", "ts"]).reset_index(drop=True)


def _get(http: Any, headers: dict[str, str], params: dict[str, Any],
         sleep: Callable[[float], None], clock: Callable[[], float], last: list[float]) -> Any:
    for attempt in range(MAX_RETRIES):
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (clock() - last[0])
        if wait > 0:
            sleep(wait)
        last[0] = clock()
        try:
            r = http.get(BARS_URL, headers=headers, params=params, timeout=60)
        except requests.RequestException as e:
            err = f"request raised {e!r}"
        else:
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                raise AlpacaError(err)
        sleep(min(60.0, 2.0 ** attempt))
    raise AlpacaError(f"gave up after {MAX_RETRIES} attempts: {err}")
