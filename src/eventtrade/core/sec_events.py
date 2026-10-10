"""ADR 0011 SEC event study: insider-purchase and 13D events, sector-hedged open-to-open outcomes,
matching strata and the matched placebo test. Pure: no I/O.

Conventions
- Event date t = first trading day on or after the filing date; entry at the t+1 open, exit at
  the t+1+h open (`eventstudy.calendar_time_series` conventions).
- Events are DataFrames [date (Timestamp), ticker]; outcomes [date, ticker, value].
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.core import eventstudy
from eventtrade.core.evaluate import newey_west_t

MIN_VALUE = 10_000.0
MAX_FILING_LAG_BDAYS = 5
CLUSTER_OWNERS = 3
CLUSTER_WINDOW_DAYS = 30
DECLUSTER_DAYS = 20
DEAL_WINDOW_DAYS = 30
EVENT_TYPES = ("ins_all", "ins_cxo", "ins_cluster", "act_13d")
_CXO = re.compile(r"\b(ceo|chief executive|cfo|chief financial|president)\b", re.IGNORECASE)
_CXO_EXCLUDE = re.compile(r"vice|division", re.IGNORECASE)
INITIAL_13D = ("SC 13D", "SCHEDULE 13D")


def is_cxo(title: Any) -> bool:
    t = "" if title is None or (isinstance(title, float) and math.isnan(title)) else str(title)
    return bool(_CXO.search(t)) and not _CXO_EXCLUDE.search(t)


def _dates(s: pd.Series) -> pd.Series:
    """'31-MAR-2016' or '2016-03-31' -> Timestamp (NaT if unparsable)."""
    out = pd.to_datetime(s, format="%d-%b-%Y", errors="coerce")
    alt = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
    return out.fillna(alt)


def event_day(dates: pd.Series, cal: pd.DatetimeIndex) -> pd.Series:
    """First trading day on or after each date (NaT past the calendar)."""
    pos = cal.searchsorted(pd.to_datetime(dates).to_numpy(), side="left")
    ok = pos < len(cal)
    out = pd.Series(pd.NaT, index=dates.index, dtype="datetime64[ns]")
    out[ok] = cal[pos[ok]]
    return out


def qualifying_accessions(subs: pd.DataFrame, owners: pd.DataFrame, trans: pd.DataFrame,
                          cik_ticker: Mapping[str, str]) -> pd.DataFrame:
    """One row per qualifying Form 4 accession (ADR 0011 + amendments 1, 4, 5).

    Columns: accession, ticker, filing_date, value, cxo (bool), owner_key (co-filers of one
    accession are one owner: the sorted tuple of their CIKs, as a string).
    """
    s = subs[subs["document_type"].astype(str) == "4"].copy()
    s["filing_date"] = _dates(s["filing_date"])
    s["cik"] = s["issuercik"].astype(str).str.zfill(10)
    s["ticker"] = s["cik"].map(cik_ticker)
    s = s.dropna(subset=["ticker", "filing_date"])
    t = trans.copy()
    t = t[(t["trans_code"] == "P") & (t["trans_acquired_disp_cd"] == "A")]
    t["shares"] = pd.to_numeric(t["trans_shares"], errors="coerce")
    t["price"] = pd.to_numeric(t["trans_pricepershare"], errors="coerce")
    t["trans_date"] = _dates(t["trans_date"])
    t = t[(t["price"] > 0) & (t["shares"] > 0) & t["trans_date"].notna()]
    t = t.merge(s[["accession_number", "cik", "ticker", "filing_date"]], on="accession_number")
    lag = np.busday_count(t["trans_date"].to_numpy().astype("datetime64[D]"),
                          t["filing_date"].to_numpy().astype("datetime64[D]"))
    t = t[(lag >= 0) & (lag <= MAX_FILING_LAG_BDAYS)]
    # identical purchases reported in several accessions (affiliates) count once: earliest filing
    t = t.sort_values(["filing_date", "accession_number"])
    t = t.drop_duplicates(["cik", "trans_date", "shares", "price"], keep="first")
    t["value"] = t["shares"] * t["price"]
    acc = t.groupby("accession_number").agg(ticker=("ticker", "first"),
                                            filing_date=("filing_date", "first"),
                                            value=("value", "sum")).reset_index()
    o = owners[owners["accession_number"].isin(acc["accession_number"])].copy()
    rel = o["rptowner_relationship"].fillna("").astype(str)
    o["insider"] = rel.str.contains("Director") | rel.str.contains("Officer")
    o["cxo"] = o["insider"] & o["rptowner_title"].map(is_cxo)
    g = o.groupby("accession_number")
    flags = pd.DataFrame({"insider": g["insider"].any(), "cxo": g["cxo"].any(),
                          "owner_key": g["rptownercik"].agg(
                              lambda x: ",".join(sorted(map(str, x))))}).reset_index()
    acc = acc.merge(flags, on="accession_number")
    acc = acc[acc["insider"]].drop(columns="insider")
    return acc.rename(columns={"accession_number": "accession"}).reset_index(drop=True)


def decluster(ev: pd.DataFrame, cal: pd.DatetimeIndex, days: int = DECLUSTER_DAYS) -> pd.DataFrame:
    """Per ticker, keep an event only if > `days` trading days after the last kept one."""
    if ev.empty:
        return ev
    ev = ev.sort_values(["ticker", "date"]).reset_index(drop=True)
    pos = cal.get_indexer(ev["date"])
    keep = np.zeros(len(ev), dtype=bool)
    last: dict[str, int] = {}
    for i, (tk, p) in enumerate(zip(ev["ticker"], pos, strict=True)):
        if p < 0:
            continue
        if tk not in last or p - last[tk] > days:
            keep[i] = True
            last[tk] = p
    return ev[keep].reset_index(drop=True)


def insider_events(acc: pd.DataFrame, cal: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """ins_all, ins_cxo, ins_cluster events [date, ticker] (declustered, 20 trading days)."""
    a = acc.copy()
    a["date"] = event_day(a["filing_date"], cal)
    a = a.dropna(subset=["date"])
    out: dict[str, pd.DataFrame] = {}
    for name, sub in (("ins_all", a), ("ins_cxo", a[a["cxo"]])):
        v = sub.groupby(["ticker", "date"])["value"].sum().reset_index()
        out[name] = decluster(v[v["value"] >= MIN_VALUE][["date", "ticker"]], cal)
    rows = []
    for tk, g in a.sort_values("filing_date").groupby("ticker"):
        armed = True
        fdates = g["filing_date"].to_numpy()
        keys = g["owner_key"].to_numpy()
        for d_ev in sorted(set(g["date"])):
            # distinct owners with filings in the 30 calendar days ending on the event day
            lo = d_ev - pd.Timedelta(days=CLUSTER_WINDOW_DAYS - 1)
            m = (fdates >= lo.to_datetime64()) & (fdates <= d_ev.to_datetime64())
            n = len(set(keys[m]))
            if n >= CLUSTER_OWNERS and armed:
                rows.append((d_ev, tk))
                armed = False
            elif n < CLUSTER_OWNERS:
                armed = True
    cl = pd.DataFrame(rows, columns=["date", "ticker"])
    out["ins_cluster"] = decluster(cl, cal) if len(cl) else cl
    return out


def events_13d(filings: pd.DataFrame, subjects: pd.DataFrame, company_filings: pd.DataFrame,
               cik_ticker: Mapping[str, str], cal: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(act_13d events [date, ticker], audit table) — initial 13Ds, subject from the header,
    deal-related filings dropped (8-K items 1.01/2.01 or DEFM14A in the 30 days up to filing)."""
    f = filings[filings["form"].isin(INITIAL_13D)].drop_duplicates("adsh")
    f = f.merge(subjects, on="adsh", how="inner")
    f["subject_cik"] = f["subject_cik"].astype(str).str.zfill(10)
    f["ticker"] = f["subject_cik"].map(cik_ticker)
    f["file_date"] = _dates(f["file_date"])
    f = f.dropna(subset=["ticker", "file_date"])
    cf = company_filings.copy()
    cf["cik"] = cf["cik"].astype(str).str.zfill(10)
    cf["filing_date"] = _dates(cf["filing_date"])
    items = cf["items"].fillna("").astype(str)
    deal = cf[((cf["form"] == "8-K") & items.str.contains(r"\b(?:1\.01|2\.01)\b"))
              | (cf["form"] == "DEFM14A")]
    flagged = []
    for r in f.itertuples(index=False):
        d = deal[(deal["cik"] == r.subject_cik)
                 & (deal["filing_date"] >= r.file_date - pd.Timedelta(days=DEAL_WINDOW_DAYS))
                 & (deal["filing_date"] <= r.file_date)]
        flagged.append(bool(len(d)))
    f["deal"] = flagged
    audit = f[["adsh", "form", "file_date", "ticker", "names", "deal"]].reset_index(drop=True)
    keep = f[~f["deal"]].copy()
    keep["date"] = event_day(keep["file_date"], cal)
    ev = keep.dropna(subset=["date"])[["date", "ticker"]].drop_duplicates()
    return decluster(ev, cal), audit


# --- outcomes and strata ------------------------------------------------------------------------

def hedged_open_returns(adj_open: pd.DataFrame, hedge: Mapping[str, pd.Series], h: int
                        ) -> pd.DataFrame:
    """Long [date, ticker, value]: open(t+1+h)/open(t+1) - 1 minus the hedge's, per decision t.

    `adj_open`: wide (index = trading days, columns = tickers). `hedge[ticker]`: the hedge's
    adj_open on the same index (already switched XLC/SPY by date)."""
    fwd = adj_open.shift(-(h + 1)) / adj_open.shift(-1) - 1.0
    out = []
    for tk in adj_open.columns:
        hs = hedge[tk]
        hr = hs.shift(-(h + 1)) / hs.shift(-1) - 1.0
        v = fwd[tk] - hr
        out.append(pd.DataFrame({"date": adj_open.index, "ticker": tk, "value": v.to_numpy()}))
    df = pd.concat(out, ignore_index=True)
    return df[np.isfinite(df["value"].to_numpy())].reset_index(drop=True)


def strata(adj_close: pd.DataFrame, hedge_close: Mapping[str, pd.Series],
           periodic_filings: Mapping[str, Sequence[pd.Timestamp]], cal: pd.DatetimeIndex,
           period_mask: np.ndarray) -> pd.DataFrame:
    """[date, ticker, stratum] for every (trading day, ticker) of the period (amendment 2).

    stratum = earnings-proximity bin (trading days since the last 10-Q/10-K filing day: 0-10,
    11-30, 31+, or 'none') x tercile of the 20-day sector-relative close-to-close return up to t
    (tercile cut points from that ticker's own period values)."""
    rows = []
    for tk in adj_close.columns:
        hc = hedge_close[tk]
        rel = (adj_close[tk] / adj_close[tk].shift(20) - 1.0) - (hc / hc.shift(20) - 1.0)
        rel = rel.reindex(cal)
        fpos = np.sort(cal.searchsorted(pd.DatetimeIndex(periodic_filings.get(tk, [])),
                                        side="left"))
        idx = np.arange(len(cal))
        k = np.searchsorted(fpos, idx, side="right") - 1
        if fpos.size:
            since = np.where(k >= 0, idx - fpos[np.maximum(k, 0)], -1)
        else:  # no 10-Q/10-K on record (e.g. foreign or bank-regulator filers): stratum 'none'
            since = np.full(len(cal), -1)
        prox = np.select([since < 0, since <= 10, since <= 30], ["none", "0-10", "11-30"], "31+")
        r = rel.to_numpy()
        pm = period_mask & np.isfinite(r)
        if pm.sum() >= 3:
            q1, q2 = np.quantile(r[pm], [1 / 3, 2 / 3])
            terc = np.where(r <= q1, "lo", np.where(r <= q2, "mid", "hi"))
        else:
            terc = np.full(len(cal), "mid")
        st = np.char.add(np.char.add(prox.astype(str), "|"), terc.astype(str))
        ok = period_mask & np.isfinite(r)
        rows.append(pd.DataFrame({"date": cal[ok], "ticker": tk, "stratum": st[ok]}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["date", "ticker", "stratum"])


@dataclass(frozen=True)
class MatchedResult:
    mean: float
    t: float
    p: float
    n_events: int
    n_event_days: int
    n_matched: int
    null_t_mean: float


def matched_placebo(events: pd.DataFrame, outcome: pd.DataFrame, strat: pd.DataFrame, h: int,
                    sign: int, cal: pd.DatetimeIndex, n_draws: int, seed: int) -> MatchedResult:
    """One-sided p of the events' NW t (horizon h) against matched placebo dates.

    `outcome` and `strat` must be restricted to the period's eligible dates (embargo applied).
    In a draw, each event moves to a date drawn uniformly (with replacement) from the same
    ticker's dates in its own stratum that have an outcome; an event whose stratum pool is empty
    keeps no placebo (dropped from the draw). p = (1 + #{t_placebo * sign >= t_obs * sign}) /
    (1 + valid draws)."""
    have, _ = eventstudy._events_with_outcome(events, outcome)
    have = have.merge(strat, on=["date", "ticker"], how="inner")
    s = eventstudy._series_of(have[["date", "ticker", "value"]], 0, h, cal)
    mean = float(s.mean()) if len(s) else float("nan")
    t_obs = float(newey_west_t(s, horizon=h)) if len(s) > 1 else float("nan")
    n_ev, n_days = len(have), int(have["date"].nunique()) if len(have) else 0
    if not eventstudy.sufficient(n_ev, n_days) or not math.isfinite(t_obs):
        return MatchedResult(mean, t_obs, float("nan"), n_ev, n_days, 0, float("nan"))
    pool = outcome.merge(strat, on=["date", "ticker"], how="inner")
    pool["pos"] = cal.get_indexer(pool["date"])
    groups = {k: (g["pos"].to_numpy(), g["value"].to_numpy(dtype=float))
              for k, g in pool.groupby(["ticker", "stratum"])}
    keys = list(zip(have["ticker"], have["stratum"], strict=True))
    rng = np.random.default_rng(seed)
    exceed = valid = 0
    tsum = 0.0
    n_matched = sum(1 for k in keys if k in groups)
    for _ in range(n_draws):
        pos, val = [], []
        for k in keys:
            g = groups.get(k)
            if g is None:
                continue
            i = rng.integers(len(g[0]))
            pos.append(g[0][i])
            val.append(g[1][i])
        if len(pos) < 2:
            continue
        ser = eventstudy._active_mean(np.asarray(pos), np.asarray(val), h, len(cal))
        tp = float(newey_west_t(ser, horizon=h)) if len(ser) > 1 else float("nan")
        if math.isfinite(tp):
            valid += 1
            tsum += tp
            exceed += sign * tp >= sign * t_obs
    p = (1 + exceed) / (1 + valid) if valid else float("nan")
    return MatchedResult(mean, t_obs, p, n_ev, n_days, n_matched,
                         tsum / valid if valid else float("nan"))
