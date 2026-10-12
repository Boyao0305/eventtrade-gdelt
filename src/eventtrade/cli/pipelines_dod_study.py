"""ADR 0015 DoD contract awards (hypothesis family D, with the review amendments): once-only
study. Never names a concrete adapter.

Order of work (amendment 11): coverage, survivorship counts and power are computed and printed
before any outcome is read; trials are logged only after everything is computed."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.config import DEV_START, Config
from eventtrade.core import dod_study as ds
from eventtrade.core import eventstudy
from eventtrade.core import intraday as iu
from eventtrade.core import sec_events as sev
from eventtrade.core import small_insider as si
from eventtrade.core.screen import benjamini_hochberg, holm

HYPOTHESIS = "D"
N_BOOT = 2000
BH_Q = 0.10
HOLM_ALPHA = 0.05
TRACK_B_ROUND_TRIP = 2 * 0.0010 + 2 * 0.0001
STRESS_ROUND_TRIP = 2 * 0.0075 + 2 * 0.0001
DISCOVERY = (date(2016, 1, 4), date(2019, 12, 31))
CONFIRMATION = (date(2020, 1, 2), date(2025, 3, 31))
COVID = (pd.Timestamp("2020-02-20"), pd.Timestamp("2020-04-30"))
XLC_START = pd.Timestamp("2018-06-19")
LATE_CAPTURE_DAYS = 30
SMALL_HEDGE, DEFENSE_HEDGE = "IWM", "ITA"


def dod_study(cfg: Config, store: Any, prices: Any, dstore: Any, sstore: Any,
              map_rows: pd.DataFrame, now: Clock, echo: Echo = print, n_boot: int = N_BOOT) -> int:
    if CONFIRMATION[1] >= cfg.holdout_start:
        echo("refused: confirmation reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0015 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            n = json.loads(t.notes)
            echo(f"  {t.params.get('period')} {t.params.get('cell')}: mean {n.get('mean')}"
                 f" p {n.get('p')} {n.get('verdict', '')}")
        return OK
    awards = pd.DataFrame(dstore.awards())
    if awards.empty or map_rows.empty:
        echo("missing: DoD awards or the issuer map (ingest-dod, map-dod)")
        return MISSING
    rows = ds.award_rows(map_rows, awards)
    days = ds.issuer_days(rows)
    echo(f"awards: {len(awards):,} paragraphs; mapped sole awards with an action amount"
         f" {len(rows):,}; issuer-days {len(days):,} ({days['cik'].nunique()} issuers)")
    _coverage(dstore, days, echo)

    # --- survivorship (amendment 2): mapped issuers without a current listing --------------
    unlisted = days[days["ticker"].isna()]
    exits = _exits(sstore, sorted(set(unlisted["cik"])))
    f_fail: dict[str, float] = {}
    for pname, span in (("discovery", DISCOVERY), ("confirmation", CONFIRMATION)):
        inp = days[(days["d"] >= pd.Timestamp(span[0])) & (days["d"] <= pd.Timestamp(span[1]))]
        un = inp[inp["ticker"].isna()]
        fails = un["cik"].map(exits).eq("failure").sum()
        f_fail[pname] = float(fails / len(inp)) if len(inp) else 0.0
        echo(f"{pname}: unlisted mapped issuer-days {len(un)} of {len(inp)}"
             f" (${un['size'].sum() / 1e9:,.1f}bn of ${inp['size'].sum() / 1e9:,.1f}bn);"
             f" exits {dict(un['cik'].map(exits).value_counts())}; f_fail {f_fail[pname]:.4f}")

    # --- prices, market caps, events (no outcome yet) ---------------------------------------
    listed = days[days["ticker"].notna()].copy()
    foreign = sorted({t for t in listed["ticker"] if ds.foreign_otc(t)})
    echo(f"ADR / foreign ordinary tickers dropped (amendment 6): {foreign}")
    listed = listed[~listed["ticker"].isin(foreign)]
    tickers = sorted(set(listed["ticker"]))
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET, SMALL_HEDGE, DEFENSE_HEDGE})
    px = prices.read_prices(tickers + hedges, DEV_START, cfg.holdout_start)
    if px.empty:
        echo("missing: prices of mapped issuers (ingest-dod-prices)")
        return MISSING
    px["date"] = pd.to_datetime(px["date"])
    cal = pd.DatetimeIndex(sorted(px.loc[px["ticker"] == iu.HEDGE_MARKET, "date"].unique()))
    wide = {c: px.pivot(index="date", columns="ticker", values=c).reindex(cal)
            for c in ("adj_open", "close", "volume")}
    tickers = [t for t in tickers if t in wide["close"].columns]
    splits_all = pd.DataFrame(dstore.splits(), columns=["ticker", "date", "ratio"])
    splits = {t: g for t, g in splits_all.groupby("ticker")}
    close = wide["close"]
    raw = si.raw_close(close[tickers], splits)
    mdv = si.median_dollar_volume(close[tickers], wide["volume"][tickers])
    trad = si.tradable(raw, mdv)
    shares = sstore.read("shares_out")
    shares["val"] = shares["val"].astype(float)
    fs = ds.filing_shares(shares)
    cik_tk = dict(zip(listed["cik"], listed["ticker"], strict=True))
    bad = ds.float_outliers(shares, fs, close, splits, cik_tk)
    echo(f"float check: {len(bad)} issuer(s) dropped {sorted((cik_tk[c], round(r)) for c, r in bad.items())}")
    listed = listed[listed["ticker"].isin(tickers) & ~listed["cik"].isin(bad)].reset_index(drop=True)
    listed["mcap"] = ds.market_caps(listed, fs, close, splits).to_numpy()
    listed["ratio"] = listed["size"] / listed["mcap"]
    listed["cell"] = ds.cell_of(listed["ratio"].to_numpy())
    pos = cal.searchsorted(listed["d"].to_numpy(), "right") - 1
    listed["pos"] = pos
    ok = pos >= 0
    listed = listed[ok].copy()
    listed["date"] = cal[listed["pos"].to_numpy()]
    tpos = {t: i for i, t in enumerate(tickers)}
    tr = trad.to_numpy()
    listed["tradable"] = [bool(tr[p, tpos[t]]) for p, t in zip(listed["pos"], listed["ticker"],
                                                                strict=True)]
    mdv_np = mdv.to_numpy()
    listed["mdv"] = [mdv_np[p, tpos[t]] for p, t in zip(listed["pos"], listed["ticker"],
                                                         strict=True)]
    no_cap = int(listed["mcap"].isna().sum())
    echo(f"listed issuer-days {len(listed):,}: no market cap {no_cap}, not tradable"
         f" {int((~listed['tradable']).sum())}; ratio > 50%:"
         f" {sorted((str(d.date()), t, round(r, 2)) for d, t, r in zip(listed['d'], listed['ticker'], listed['ratio'], strict=True) if r > 0.5)}")
    ev_all = listed[listed["tradable"] & listed["cell"].ne("")].copy()
    track_b = {a.ticker: a for a in store.read_assets("B")}
    ev_all["track_b"] = ev_all["ticker"].isin(track_b)
    ev_all["stratum"] = ev_all["date"].dt.year
    ev_all["late"] = ev_all["d"].isin(_late_days(dstore))

    # strata: every tradable (date, ticker) of the issuer, stratum = calendar year
    tt = trad.stack()
    tt = tt[tt]
    strat_all = pd.DataFrame({"date": tt.index.get_level_values(0),
                              "ticker": tt.index.get_level_values(1).astype(object)})
    strat_all["stratum"] = strat_all["date"].dt.year

    # --- power before outcomes (amendment 11) ----------------------------------------------
    plan: dict[tuple[str, str, int], pd.DataFrame] = {}
    for h in ds.HORIZONS:
        for cell in ds.CELLS:
            e = ev_all[ev_all["cell"] == cell]
            keep = ds.decluster(e["pos"].to_numpy(), e["ticker"].to_numpy(), h)
            e = e[keep]
            for pname, span in (("discovery", DISCOVERY), ("confirmation", CONFIRMATION)):
                lo, hi = pd.Timestamp(span[0]), pd.Timestamp(span[1])
                last = int(np.flatnonzero((cal >= lo) & (cal <= hi)).max())
                sel = e[(e["date"] >= lo) & (e["date"] <= hi) & (e["pos"] + 1 + h <= last)]
                plan[(cell, pname, h)] = sel
                top = sel["ticker"].value_counts()
                echo(f"  power {pname:<12} {cell:<8} h{h:<2} events {len(sel):>4} issuers"
                     f" {sel['ticker'].nunique():>3} top {top.index[0] if len(top) else '-'}"
                     f" {top.iloc[0] / len(sel) if len(sel) else 0:.0%} MDE {ds.mde(h, len(sel)):.4f}"
                     f"{' UNDERPOWERED' if ds.underpowered(len(sel), sel['ticker'].nunique()) else ''}")

    # --- outcomes ---------------------------------------------------------------------------
    h_open = {t: _hedge(wide["adj_open"], t, track_b) for t in tickers}
    ita_open = {t: wide["adj_open"][DEFENSE_HEDGE] for t in tickers}
    award_pos = _award_positions(rows, cal)
    oc_by_h = {h: (sev.hedged_open_returns(wide["adj_open"][tickers], h_open, h),
                   sev.hedged_open_returns(wide["adj_open"][tickers], ita_open, h))
               for h in ds.HORIZONS}
    results: dict[tuple[str, str, int], dict[str, Any]] = {}

    def run(pname: str, span: tuple[date, date], cells: list[tuple[str, int]]) -> None:
        lo, hi = pd.Timestamp(span[0]), pd.Timestamp(span[1])
        last = int(np.flatnonzero((cal >= lo) & (cal <= hi)).max())
        st = strat_all[(strat_all["date"] >= lo) & (strat_all["date"] <= hi)]
        for h in sorted({h for _, h in cells}):
            oc_all, oc_ita = oc_by_h[h]
            pos_oc = cal.get_indexer(oc_all["date"])
            oc = oc_all[(oc_all["date"] >= lo) & (oc_all["date"] <= hi)
                        & (pos_oc + 1 + h <= last)]
            pool = oc.merge(st, on=["date", "ticker"], how="inner")
            for cell in [c for c, hh in cells if hh == h]:
                r = _cell(plan[(cell, pname, h)], oc, oc_ita, st, pool, h, cal, n_boot,
                          cfg.seed, pname, f_fail[pname], award_pos)
                results[(cell, pname, h)] = r
                echo(f"  {pname:<12} {cell:<8} h{h:<2} n {r['n_events']:>4}"
                     f" mean {_f(r['mean'], 5)} t {_f(r['t'])} p {_f(r['p'], 4)}"
                     f" ev_avg {_f(r['event_avg'], 5)} placebo {_f(r['placebo_avg'], 5)}"
                     f" net {_f(r['net'], 5)}")

    run("discovery", DISCOVERY, [(c, h) for h in ds.HORIZONS for c in ds.CELLS])
    disc = {f"{c}|h{h}": (v["p"] if v["p"] is not None else math.nan)
            for (c, _, h), v in results.items()}
    found = benjamini_hochberg(disc, BH_Q)
    for (c, _, h), v in results.items():
        v["discovered"] = f"{c}|h{h}" in found
    echo(f"discovery: {len(found)} of {len(disc)} cells discovered (BH q = {BH_Q})")
    todo = [(c, h) for (c, _, h), v in list(results.items()) if v["discovered"]]
    if todo:
        run("confirmation", CONFIRMATION, todo)
    conf = {k: v for k, v in results.items() if k[1] == "confirmation"}
    hp = holm({f"{c}|h{h}": (v["p"] if v["p"] is not None else math.nan)
               for (c, _, h), v in conf.items()}, HOLM_ALPHA)
    for (c, _, h), v in conf.items():
        ok = (f"{c}|h{h}" in hp and (v["mean"] or 0) > 0 and (v["net"] or -1) > 0
              and (v.get("mean_ex_covid") or 0) > 0 and (v.get("track_b_mean") or 0) > 0
              and (v.get("stressed_mean") or -1) > 0)
        v["holm"] = f"{c}|h{h}" in hp
        v["verdict"] = "CONFIRMED" if ok else "not confirmed"
        echo(f"  confirmation {c} h{h}: {v['verdict']} (net {v['net']})")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for (cell, pname, h), note in results.items():
        span = DISCOVERY if pname == "discovery" else CONFIRMATION
        params = {"adr": "0015", "cell": f"{cell}|h{h}", "period": pname, "n_boot": n_boot,
                  "seed": cfg.seed, "null": "matched_issuer_year"}
        _log_trial(store, logged, "B", "signal", HYPOTHESIS, params, *span, now, note)
    echo(f"{len(logged) - n0} trial(s) logged")
    return OK


def _cell(e: pd.DataFrame, oc: pd.DataFrame, oc_ita: pd.DataFrame, st: pd.DataFrame,
          pool: pd.DataFrame, h: int, cal: pd.DatetimeIndex, n_boot: int, seed: int, pname: str,
          f_fail: float, award_pos: dict[str, np.ndarray]) -> dict[str, Any]:
    ev = e[["date", "ticker", "stratum"]].assign(ticker=e["ticker"].astype(object))
    m = sev.matched_placebo(ev, oc, st, h, 1, cal, n_boot, seed)
    have, _ = eventstudy._events_with_outcome(ev, oc)
    have = have.merge(e[["date", "ticker", "stratum", "track_b", "mdv", "kinds",
                         "small_business", "late"]], on=["date", "ticker"], how="left")
    vals = have["value"].to_numpy(dtype=float)
    ev_avg = float(vals.mean()) if len(vals) else float("nan")
    plc = ds.placebo_expectation(have, pool)
    cost = np.where(have["track_b"].to_numpy(dtype=bool), TRACK_B_ROUND_TRIP,
                    si.cost_round_trip(have["mdv"].to_numpy(dtype=float)))
    stress = np.where(have["track_b"].to_numpy(dtype=bool), TRACK_B_ROUND_TRIP, STRESS_ROUND_TRIP)
    note: dict[str, Any] = {
        "mean": _num(m.mean), "t": _num(m.t), "p": _num(m.p), "n_events": m.n_events,
        "n_event_days": m.n_event_days, "n_matched": m.n_matched,
        "n_issuers": int(have["ticker"].nunique()) if len(have) else 0,
        "event_avg": _num(ev_avg), "placebo_avg": _num(plc),
        "cost_avg": _num(float(cost.mean()) if len(cost) else None),
        "net": _num(ev_avg - plc - float(cost.mean())) if len(cost) else None,
        "net_stress": _num(ev_avg - plc - float(stress.mean())) if len(stress) else None,
        "mde": _num(ds.mde(h, m.n_events)),
        "underpowered": ds.underpowered(m.n_events, int(have["ticker"].nunique()) if len(have) else 0),
        "adr": "0015",
    }
    if len(have):
        tb = have["track_b"].astype(bool)
        note["track_b_mean"] = _num(float(vals[tb].mean()) if tb.any() else None)
        note["small_mean"] = _num(float(vals[~tb].mean()) if (~tb).any() else None)
        note["n_track_b"] = int(tb.sum())
        note["by_kind"] = {k: _num(float(g["value"].mean())) for k, g in have.groupby("kinds")}
        note["by_small_business"] = {str(k): _num(float(g["value"].mean()))
                                     for k, g in have.groupby("small_business")}
        note["late_capture_n"] = int(have["late"].fillna(False).astype(bool).sum())
        ita, _ = eventstudy._events_with_outcome(ev, oc_ita)
        note["ita_hedged_avg"] = _num(float(ita["value"].mean()) if len(ita) else None)
        note["placebo_avg_no_award_pool"] = _num(ds.placebo_expectation(
            have, _no_award_pool(pool, award_pos, cal, h)))
        note["p_unmatched"] = _num(eventstudy.placebo_p(ev[["date", "ticker"]], oc, h, 1, cal,
                                                        n_boot, seed).p)
    if pname == "confirmation":
        ex = ev[~((ev["date"] >= COVID[0]) & (ev["date"] <= COVID[1]))]
        s_ex = eventstudy.calendar_time_series(ex, oc, h, cal)
        note["mean_ex_covid"] = _num(float(s_ex.mean()) if len(s_ex) else None)
        note["stressed_mean"] = _num(si.stressed_mean(m.mean, f_fail)
                                     if math.isfinite(m.mean) else None)
        note["f_fail"] = _num(f_fail)
    return note


def _hedge(adj_open: pd.DataFrame, tk: str, track_b: dict[str, Any]) -> pd.Series:
    """Track B: the sector SPDR (SPY before XLC exists); others: IWM (ADR 0012)."""
    if tk not in track_b:
        return adj_open[SMALL_HEDGE]
    hk = iu.SECTOR_SPDR.get(track_b[tk].screen_group or "", iu.HEDGE_MARKET)
    s = adj_open[hk].copy() if hk in adj_open.columns else adj_open[iu.HEDGE_MARKET].copy()
    if hk == "XLC":
        spy = adj_open[iu.HEDGE_MARKET]
        s[s.index < XLC_START] = np.nan
        k = s.first_valid_index()
        s = s.where(s.notna(), spy * (s[k] / spy[k])) if k is not None else spy
    return s


def _award_positions(rows: pd.DataFrame, cal: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """Per current ticker: trading-day positions (last day <= d) of every sole award."""
    r = rows[rows["ticker"].notna()]
    p = cal.searchsorted(r["d"].to_numpy(), "right") - 1
    return {t: np.unique(p[(r["ticker"] == t).to_numpy()]) for t in r["ticker"].unique()}


def _no_award_pool(pool: pd.DataFrame, award_pos: dict[str, np.ndarray], cal: pd.DatetimeIndex,
                   h: int) -> pd.DataFrame:
    """Amendment 9 (reported only): pool days farther than h trading days from any sole award."""
    pos = cal.get_indexer(pool["date"])
    keep = np.ones(len(pool), dtype=bool)
    for t, idx in pool.groupby("ticker").indices.items():
        a = award_pos.get(t)
        if a is None or not len(a):
            continue
        p = pos[idx]
        j = np.searchsorted(a, p)
        near = np.zeros(len(p), dtype=bool)
        for jj in (j - 1, j):
            ok = (jj >= 0) & (jj < len(a))
            near[ok] |= np.abs(a[jj[ok]] - p[ok]) <= h
        keep[idx] = ~near
    return pool[keep]


def _coverage(dstore: Any, days: pd.DataFrame, echo: Echo) -> None:
    """Amendment 5: articles per year against DoD release days (federal holidays excluded)."""
    from pandas.tseries.holiday import USFederalHolidayCalendar

    arts = pd.DataFrame(dstore.articles())
    ok = arts[arts["status"] == "ok"].copy()
    ok["d"] = pd.to_datetime(ok["release_date"])
    hol = USFederalHolidayCalendar().holidays("2015-01-01", "2025-03-31")
    bd = pd.bdate_range("2015-01-01", "2025-03-31").difference(hol)
    have = ok.groupby(ok["d"].dt.year).size()
    want = pd.Series(1, index=bd).groupby(bd.year).size()
    echo("coverage (articles / federal business days): " + json.dumps(
        {int(y): f"{int(have.get(y, 0))}/{int(n)}" for y, n in want.items()}))
    sept = days["d"].dt.month.eq(9).mean() if len(days) else float("nan")
    echo(f"September share of mapped issuer-days: {sept:.3f}")
    lag = _capture_lags(dstore)
    if len(lag):
        echo(f"capture lag (days, earliest capture): median {lag.median():.0f}, 90th pct"
             f" {lag.quantile(0.9):.0f}, > {LATE_CAPTURE_DAYS}: {int((lag > LATE_CAPTURE_DAYS).sum())}")


def _capture_lags(dstore: Any) -> pd.Series:
    arts = pd.DataFrame(dstore.articles())
    snaps = dstore.read_snapshots()
    ok = arts[arts["status"] == "ok"]
    out = {}
    for aid, rd in zip(ok["article_id"], ok["release_date"], strict=True):
        caps = snaps.get(int(aid), [])
        if not caps:
            continue
        first = min(pd.Timestamp(ts[:8]) for ts, _ in caps)
        out[pd.Timestamp(rd)] = (first - pd.Timestamp(rd)).days
    return pd.Series(out, dtype=float)


def _late_days(dstore: Any) -> set[pd.Timestamp]:
    lag = _capture_lags(dstore)
    return set(lag.index[lag > LATE_CAPTURE_DAYS])


def _exits(sstore: Any, ciks: list[str]) -> dict[str, str]:
    """merger / failure / other per unlisted CIK from its EDGAR profile (ADR 0012 amendment 1);
    'unknown' without a profile."""
    if not ciks:
        return {}
    f2 = sstore.read("company_filings2")
    f2["cik"] = f2["cik"].astype(str).str.zfill(10)
    out = {}
    for c in ciks:
        g = f2[f2["cik"] == c]
        out[c] = si.classify_exit(g) if len(g) else "unknown"
    return out


def _f(x: float | None, d: int = 2) -> str:
    return "nan" if x is None or not math.isfinite(x) else f"{x:.{d}f}"
