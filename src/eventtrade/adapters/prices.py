"""Daily price bars from free sources (Yahoo via yfinance, Stooq), plus cross-checks and sanity flags.

Output frames follow `schemas.PRICE_COLUMNS`: one row per (date, ticker), `date` a tz-naive
midnight Timestamp. What "adjusted" means per source:

- **yfinance** (`auto_adjust=False`): Yahoo's raw `Open/High/Low/Close` are split-adjusted but NOT
  dividend-adjusted; `Adj Close` is split- and dividend-adjusted. We keep open/high/low/close as
  Yahoo returns them, `adj_close = Adj Close`, and `adj_open = open * adj_close / close`, so
  `adj_open` and `adj_close` are on the same total-return scale. Volume is split-adjusted.
- **Stooq**: the CSV carries one OHLCV series with no separate adjusted close; its adjustment for
  dividends is undocumented. We set `adj_open = open`, `adj_close = close` — treat Stooq as a
  split-adjusted cross-check of price *returns*, not as a total-return source. As of 2026-10 the
  CSV endpoint answers non-browser clients with a JavaScript proof-of-work page; `fetch_stooq`
  detects that and raises `StooqError` rather than circumventing it.

Hold-out discipline is the caller's job: pass `end < config.HOLDOUT_START` outside the final
hold-out evaluation.
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

from eventtrade.schemas import PRICE_COLUMNS

_PRICE_FIELDS = ["open", "high", "low", "close", "adj_open", "adj_close"]
_YF_RENAME = {"Open": "open", "High": "high", "Low": "low", "Close": "close",
              "Adj Close": "adj_close", "Volume": "volume"}

MAX_GAP_TRADING_DAYS = 5
MAX_ABS_DAILY_RETURN = 0.40


class PriceFetchError(RuntimeError):
    """Some tickers could not be fetched; `.prices` holds the ones that were, `.failures` the rest."""

    def __init__(self, failures: dict[str, str], prices: pd.DataFrame):
        self.failures = failures
        self.prices = prices
        super().__init__(f"{len(failures)} ticker(s) failed: "
                         + "; ".join(f"{t}: {r}" for t, r in sorted(failures.items())))


class StooqError(RuntimeError):
    """Stooq returned something other than a price CSV (bot check, no data, quota)."""


# --- yfinance -------------------------------------------------------------------------------

def fetch_splits(tickers: list[str], end: date, ticker_factory: Callable[[str], object] | None = None
                 ) -> pd.DataFrame:
    """Yahoo split history (ticker, date, ratio) before `end`; tickers without splits have no rows.
    ratio = new shares per old share (2.0 for a 2-for-1)."""
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    rows = []
    for t in tickers:
        s = ticker_factory(t).splits  # type: ignore[attr-defined]
        for d, r in (s.items() if s is not None else []):
            ts = pd.Timestamp(d)
            ts = (ts.tz_localize(None) if ts.tzinfo else ts).normalize()
            if ts.date() < end and float(r) > 0:
                rows.append((t, ts.date().isoformat(), float(r)))
    return pd.DataFrame(rows, columns=["ticker", "date", "ratio"])


def fetch_yfinance(
    tickers: list[str],
    start: date,
    end: date,
    *,
    batch_size: int = 40,
    max_retries: int = 3,
    backoff_s: float = 2.0,
    download: Callable[..., pd.DataFrame] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """Fetch daily bars for `tickers` over [start, end] (both inclusive); raise PriceFetchError on any miss."""
    if download is None:
        import yfinance as yf
        download = yf.download
    wanted = list(dict.fromkeys(t.strip().upper() for t in tickers))
    frames: list[pd.DataFrame] = []
    failures: dict[str, str] = {}
    for i in range(0, len(wanted), batch_size):
        pending = wanted[i:i + batch_size]
        failed: dict[str, str] = {}
        for attempt in range(max_retries + 1):
            if attempt:
                sleep(backoff_s * 2 ** (attempt - 1))
            got, failed = _download_batch(download, pending, start, end)
            frames.extend(got)
            pending = [t for t in pending if t in failed]
            if not pending:
                break
        failures.update(failed)
    prices = _concat(frames)
    if failures:
        raise PriceFetchError(failures, prices)
    return prices


def _download_batch(download: Callable[..., pd.DataFrame], tickers: list[str], start: date,
                    end: date) -> tuple[list[pd.DataFrame], dict[str, str]]:
    """One yf.download call; returns per-ticker PRICE_COLUMNS frames and {ticker: reason} misses."""
    try:
        raw = download(tickers, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                       auto_adjust=False, actions=False, group_by="ticker", threads=True,
                       progress=False, multi_level_index=True)
    except Exception as e:  # network/HTTP errors: every ticker of the batch failed this attempt
        return [], {t: f"download raised {e!r}" for t in tickers}
    got, failed = [], {}
    for t in tickers:
        try:
            bars = _from_yf_frame(raw, t)
        except KeyError:
            failed[t] = "absent from download result"
            continue
        if bars.empty:
            failed[t] = "no rows (delisted, bad symbol, or rate-limited)"
        else:
            got.append(bars)
    return got, failed


def _from_yf_frame(raw: pd.DataFrame | None, ticker: str) -> pd.DataFrame:
    """Slice one ticker out of a group_by='ticker' yf.download frame into PRICE_COLUMNS."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=PRICE_COLUMNS)
    sub = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
    sub = sub.rename(columns=_YF_RENAME)
    missing = {"open", "high", "low", "close", "adj_close", "volume"} - set(sub.columns)
    if missing:
        raise KeyError(f"yfinance frame lacks {sorted(missing)}")
    sub = sub.dropna(subset=["close", "adj_close"])
    out = pd.DataFrame({
        "date": _naive_dates(sub.index),
        "ticker": ticker,
        "open": sub["open"].to_numpy(dtype=float),
        "high": sub["high"].to_numpy(dtype=float),
        "low": sub["low"].to_numpy(dtype=float),
        "close": sub["close"].to_numpy(dtype=float),
        "volume": sub["volume"].to_numpy(dtype=float),
        "adj_close": sub["adj_close"].to_numpy(dtype=float),
    })
    out["adj_open"] = out["open"] * out["adj_close"] / out["close"]
    return out[PRICE_COLUMNS].reset_index(drop=True)


# --- Stooq ----------------------------------------------------------------------------------

_STOOQ_URL = "https://stooq.com/q/d/l/"


def fetch_stooq(
    ticker: str,
    start: date,
    end: date,
    *,
    apikey: str | None = None,
    get: Callable[..., requests.Response] = requests.get,
    timeout: float = 30.0,
) -> pd.DataFrame:
    """Fetch daily bars for one US ticker from Stooq over [start, end]; raise StooqError if blocked/empty."""
    params = {"s": f"{ticker.lower().replace('-', '.')}.us", "i": "d",
              "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d")}
    if apikey:
        params["apikey"] = apikey
    resp = get(_STOOQ_URL, params=params, timeout=timeout,
               headers={"User-Agent": "eventtrade-research/0.1"})
    resp.raise_for_status()
    out = _parse_stooq_csv(resp.text, ticker.upper())
    return out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))] \
        .reset_index(drop=True)


def _parse_stooq_csv(text: str, ticker: str) -> pd.DataFrame:
    """Parse Stooq's `Date,Open,High,Low,Close,Volume` CSV into PRICE_COLUMNS (adj_* = raw)."""
    head = text.lstrip()[:200].lower()
    if head.startswith("<") or "javascript" in head:
        raise StooqError("Stooq returned an HTML/JavaScript page (browser verification), not CSV")
    if not head.startswith("date,"):
        raise StooqError(f"Stooq returned no CSV: {text.strip()[:120]!r}")
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().lower() for c in df.columns]
    if df.empty:
        raise StooqError(f"Stooq returned an empty CSV for {ticker}")
    if "volume" not in df.columns:
        df["volume"] = np.nan
    out = pd.DataFrame({
        "date": _naive_dates(pd.to_datetime(df["date"])),
        "ticker": ticker,
        "open": df["open"].astype(float), "high": df["high"].astype(float),
        "low": df["low"].astype(float), "close": df["close"].astype(float),
        "volume": df["volume"].astype(float),
    })
    out["adj_open"] = out["open"]
    out["adj_close"] = out["close"]
    return out[PRICE_COLUMNS].sort_values("date").reset_index(drop=True)


# --- checks (pure) --------------------------------------------------------------------------

def cross_check(a: pd.DataFrame, b: pd.DataFrame, *, col: str = "adj_close",
                tol: float = 0.005) -> pd.DataFrame:
    """Per-ticker agreement of daily returns of `col` between two PRICE_COLUMNS frames."""
    rows = []
    for t in sorted(set(a["ticker"]) | set(b["ticker"])):
        sa = a.loc[a["ticker"] == t].set_index("date")[col].sort_index()
        sb = b.loc[b["ticker"] == t].set_index("date")[col].sort_index()
        common = sa.index.intersection(sb.index)
        ra = sa.loc[common].pct_change().iloc[1:]
        rb = sb.loc[common].pct_change().iloc[1:]
        diff = (ra - rb).abs()
        rows.append({
            "ticker": t,
            "n_common_days": len(common),
            "n_only_a": len(sa.index.difference(sb.index)),
            "n_only_b": len(sb.index.difference(sa.index)),
            "median_abs_ret_diff": float(diff.median()) if len(diff) else np.nan,
            "max_abs_ret_diff": float(diff.max()) if len(diff) else np.nan,
            "n_mismatch_days": int((diff > tol).sum()),
        })
    return pd.DataFrame(rows, columns=["ticker", "n_common_days", "n_only_a", "n_only_b",
                                       "median_abs_ret_diff", "max_abs_ret_diff",
                                       "n_mismatch_days"])


def sanity_check(prices: pd.DataFrame, *, max_gap: int = MAX_GAP_TRADING_DAYS,
                 max_abs_ret: float = MAX_ABS_DAILY_RETURN) -> pd.DataFrame:
    """Flag non-positive prices, gaps of > max_gap missing weekdays, and |adj_close return| > max_abs_ret."""
    issues: list[dict] = []
    for t, g in prices.sort_values("date").groupby("ticker", sort=True):
        g = g.reset_index(drop=True)
        bad = (g[_PRICE_FIELDS] <= 0).any(axis=1)
        for _, r in g.loc[bad].iterrows():
            issues.append({"ticker": t, "date": r["date"], "issue": "nonpositive_price",
                           "value": float(r[_PRICE_FIELDS].min())})
        d = g["date"].to_numpy(dtype="datetime64[D]")
        if len(d) > 1:
            missing = np.busday_count(d[:-1], d[1:]) - 1
            for k in np.flatnonzero(missing > max_gap):
                issues.append({"ticker": t, "date": g["date"].iat[k + 1], "issue": "gap",
                               "value": float(missing[k])})
        ret = g["adj_close"].where(g["adj_close"] > 0).pct_change()
        for k in np.flatnonzero((ret.abs() > max_abs_ret).to_numpy()):
            issues.append({"ticker": t, "date": g["date"].iat[k], "issue": "extreme_return",
                           "value": float(ret.iat[k])})
    out = pd.DataFrame(issues, columns=["ticker", "date", "issue", "value"])
    return out.sort_values(["ticker", "date", "issue"]).reset_index(drop=True)


# --- helpers --------------------------------------------------------------------------------

def _naive_dates(idx) -> pd.DatetimeIndex:
    """Calendar dates as tz-naive midnight datetime64[ns]."""
    di = pd.DatetimeIndex(idx)
    if di.tz is not None:
        di = di.tz_localize(None)
    return di.normalize().astype("datetime64[ns]")


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate per-ticker frames, sorted by (date, ticker), deduplicated on the key."""
    if not frames:
        out = pd.DataFrame(columns=PRICE_COLUMNS)
        out["date"] = pd.to_datetime(out["date"]).astype("datetime64[ns]")
        return out
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(["date", "ticker"], keep="last")
    return out.sort_values(["date", "ticker"]).reset_index(drop=True)
