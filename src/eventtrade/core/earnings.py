"""ADR 0013 post-earnings drift: earnings days from 8-K Item 2.02, the 3-day hedged reaction and
its standardized surprise, events, and the non-earnings big-move strata of the secondary null.
Pure: no I/O. Wide frames are indexed by trading day, columns = tickers."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

Z_EVENT = 2.0
SIGMA_DAYS = 250
SIGMA_MIN = 150
DEDUP_DAYS = 5
SIGMA_EXCLUDE = 2
PLACEBO_EXCLUDE = 5


def earnings_days(company_filings: pd.DataFrame, cik_ticker: Mapping[str, str],
                  cal: pd.DatetimeIndex, dedup: bool = True, deal_filter: bool = True
                  ) -> pd.DataFrame:
    """[ticker, d]: first trading day on/after each 8-K (not 8-K/A) with Item 2.02.

    dedup: filings of a ticker within 5 trading days of the previous kept one are merged into it.
    deal_filter: 2.02 8-Ks that also carry Item 1.01 or 2.01 are dropped (ADR 0013 am. 6)."""
    f = company_filings[company_filings["form"] == "8-K"].copy()
    items = f["items"].fillna("").astype(str)
    f = f[items.str.contains(r"\b2\.02\b")]
    if deal_filter:
        it = f["items"].fillna("").astype(str)
        f = f[~it.str.contains(r"\b(?:1\.01|2\.01)\b")]
    f["ticker"] = f["cik"].astype(str).str.zfill(10).map(cik_ticker)
    f = f.dropna(subset=["ticker"])
    pos = cal.searchsorted(pd.to_datetime(f["filing_date"]).to_numpy(), side="left")
    f = f[pos < len(cal)].assign(p=pos[pos < len(cal)])
    rows = []
    for tk, g in f.sort_values("p").groupby("ticker"):
        last = -10**9
        for p in g["p"]:
            if not dedup or p - last > DEDUP_DAYS:
                rows.append((tk, int(p)))
                last = p
    return pd.DataFrame([(t, cal[p]) for t, p in rows], columns=["ticker", "d"])


def hedged_3day(close: pd.DataFrame, hedge_close: Mapping[str, pd.Series], back: int = 1
                ) -> pd.DataFrame:
    """R[t] = close(t+1)/close(t-back) - 1 minus the hedge's (back=1: 3-day window centred on t;
    back=2: the 4-day window of ADR 0013 amendment 2)."""
    out = {}
    for tk in close.columns:
        h = hedge_close[tk]
        out[tk] = ((close[tk].shift(-1) / close[tk].shift(back) - 1)
                   - (h.shift(-1) / h.shift(back) - 1))
    return pd.DataFrame(out, index=close.index)


def pre_reaction_share(close: pd.DataFrame, hedge_close: Mapping[str, pd.Series],
                       earn: pd.DataFrame) -> pd.Series:
    """Per year: share of earnings days whose largest |1-day hedged return| in [d-4, d+1] falls
    before d (ADR 0013 amendment 2; outcome-free)."""
    cal = close.index
    rows = []
    for tk, g in earn.groupby("ticker"):
        if tk not in close.columns:
            continue
        h = hedge_close[tk]
        r1 = ((close[tk] / close[tk].shift(1) - 1) - (h / h.shift(1) - 1)).to_numpy()
        for d, p in zip(g["d"], cal.get_indexer(g["d"]), strict=True):
            if p < 5 or p + 1 >= len(cal):
                continue
            w = r1[p - 4:p + 2]
            if np.isfinite(w).sum() < 4:
                continue
            k = int(np.nanargmax(np.abs(w)))
            rows.append((d.year, k < 4))
    df = pd.DataFrame(rows, columns=["year", "before"])
    return df.groupby("year")["before"].mean()


def coverage_ok(earn_all: pd.DataFrame, years: range) -> set[str]:
    """Tickers with >= 3 Item 2.02 days in every listed calendar year (ADR 0013 amendment 3)."""
    e = earn_all.assign(year=pd.to_datetime(earn_all["d"]).dt.year)
    cnt = e.groupby(["ticker", "year"]).size().unstack(fill_value=0)
    cnt = cnt.reindex(columns=list(years), fill_value=0)
    return set(cnt.index[(cnt >= 3).all(axis=1)])


def surprise_z(r3: pd.DataFrame, earn: pd.DataFrame, exclude: pd.DataFrame | None = None
               ) -> pd.DataFrame:
    """[ticker, d, r, z]: reaction r = R3[d]; z = r / sd of R3 over the 250 trading days ending at
    d-2 (window ends at d-1 close: known before d), excluding days within +-2 of an earnings day;
    needs >= 150 values."""
    cal = r3.index
    rows = []
    excl = earn if exclude is None else exclude
    ex_by = {tk: cal.get_indexer(g["d"]) for tk, g in excl.groupby("ticker")}
    for tk, g in earn.groupby("ticker"):
        if tk not in r3.columns:
            continue
        x = r3[tk].to_numpy(dtype=float).copy()
        epos = cal.get_indexer(g["d"])
        mask = np.zeros(len(cal), dtype=bool)
        for p in ex_by.get(tk, epos):
            if p < 0:
                continue
            mask[max(0, p - SIGMA_EXCLUDE):p + SIGMA_EXCLUDE + 1] = True
        xs = np.where(mask, np.nan, x)
        for d, p in zip(g["d"], epos, strict=True):
            if p < 2:
                continue
            w = xs[max(0, p - 2 - SIGMA_DAYS + 1):p - 1]  # R3 at <= d-2 uses closes <= d-1
            w = w[np.isfinite(w)]
            r = x[p]
            if len(w) >= SIGMA_MIN and np.isfinite(r):
                sd = w.std(ddof=1)
                rows.append((tk, d, r, r / sd if sd > 0 else np.nan))
    return pd.DataFrame(rows, columns=["ticker", "d", "r", "z"])


def events(sz: pd.DataFrame, cal: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """pos / neg events [date, ticker] with date = d+1 (decision at the close of d+1)."""
    out = {}
    for name, m in (("pos", sz["z"] >= Z_EVENT), ("neg", sz["z"] <= -Z_EVENT)):
        e = sz[m]
        p = cal.get_indexer(e["d"]) + 1
        ok = (p > 0) & (p < len(cal))
        out[name] = pd.DataFrame({"date": cal[p[ok]], "ticker": e["ticker"].to_numpy()[ok]})
    return out


def bigmove_strata(r3: pd.DataFrame, earn: pd.DataFrame, period_mask: np.ndarray,
                   decluster: int = 10, before: int = 5, after: int = 20) -> pd.DataFrame:
    """Secondary null: [date, ticker, stratum] for non-earnings days whose 3-day hedged move has
    |z| >= 2 against the stock's trailing 250-day sd (same estimator); stratum 'pos' / 'neg'.
    `date` is the decision day t = (centre day)+1, as for events. Days within +-5 trading days of
    an earnings day are excluded."""
    cal = r3.index
    rows = []
    epos_by = {tk: cal.get_indexer(g["d"]) for tk, g in earn.groupby("ticker")}
    for tk in r3.columns:
        x = r3[tk].to_numpy(dtype=float)
        excl = np.zeros(len(cal), dtype=bool)
        for p in epos_by.get(tk, np.array([], dtype=int)):
            if p >= 0:
                excl[max(0, p - before):p + after + 1] = True
        s = pd.Series(np.where(excl, np.nan, x))
        sd = s.shift(2).rolling(SIGMA_DAYS, min_periods=SIGMA_MIN).std().to_numpy()
        z = x / sd
        big = np.isfinite(z) & (np.abs(z) >= Z_EVENT) & ~excl
        idx = np.flatnonzero(big)
        kept, last = [], -10**9
        for i in idx:  # first qualifying centre day per `decluster` trading days
            if i - last > decluster:
                kept.append(i)
                last = i
        idx = np.asarray(kept, dtype=int)
        idx = idx[(idx + 1 < len(cal))]
        keep = period_mask[idx + 1]
        idx = idx[keep]
        rows.append(pd.DataFrame({"date": cal[idx + 1], "ticker": tk,
                                  "stratum": np.where(z[idx] > 0, "pos", "neg")}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["date", "ticker", "stratum"])
