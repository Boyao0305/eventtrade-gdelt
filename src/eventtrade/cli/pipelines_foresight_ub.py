"""ADR 0016 value of foresight for earnings, macro releases, DoD awards and insider buys
(hypothesis family U, deliberate look-ahead diagnostic, with the review amendments). Once only;
trials carry params.lookahead = true. Never names a concrete adapter."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.cli.pipelines_sec_study import cik_to_ticker
from eventtrade.config import DEV_START, Config
from eventtrade.core import dod_study as ds
from eventtrade.core import earnings as er
from eventtrade.core import foresight_ub as fu
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_study as st
from eventtrade.core import macro as mc
from eventtrade.core import macro_calendar as mcal
from eventtrade.core import sec_events as sev
from eventtrade.core import small_insider as si

HYPOTHESIS = "U"
PERIODS = {"A": (date(2016, 1, 4), date(2019, 12, 31)), "B": (date(2020, 1, 2), date(2025, 3, 31))}
COST_EARN = 0.0030
COST_MACRO = 0.0002
COST_INSIDER = 0.0030
COST_DOD_TRACK_B = 2 * 0.0010 + 2 * 0.0001
XLC_START = pd.Timestamp("2018-06-19")
EARN_EXCL = 5
LATE_FILING_BDAYS = 5
SIGNED = ("U-E-lead", "U-M1-lead", "U-D-material", "U-D-moderate", "U-D-lead-material",
          "U-D-lead-moderate", "U-I-all", "U-I-cxo")
ORACLE = ("U-E", "U-M1", "U-M2")


def foresight_ub(cfg: Config, store: Any, sstore: Any, istore: Any, dstore: Any, dodcap: Any,
                 map_rows: pd.DataFrame, exclusions: pd.DataFrame | None, now: Clock,
                 echo: Echo = print, n_boot: int = fu.N_BOOT) -> int:
    if PERIODS["B"][1] >= cfg.holdout_start:
        echo("refused: period B reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0016 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            n = json.loads(t.notes)
            echo(f"  {t.params.get('period')} {t.params.get('cell')}: {n.get('summary', '')}")
        return OK
    res: dict[tuple[str, str], dict[str, Any]] = {}
    for part in (_earnings, _macro, _dod, _insider):
        rc = part(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, res, echo,
                  n_boot)
        if rc != OK:
            return rc
    for cell in SIGNED:
        lo = {p: res[(cell, p)].get("D_lo", float("nan")) for p in PERIODS if (cell, p) in res}
        hi = {p: res[(cell, p)].get("D_hi", float("nan")) for p in PERIODS if (cell, p) in res}
        v = fu.verdict(lo, hi)
        for p in PERIODS:
            if (cell, p) in res:
                res[(cell, p)]["verdict"] = v
        echo(f"verdict {cell}: {v}")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for (cell, p), note in res.items():
        params = {"adr": "0016", "cell": cell, "period": p, "lookahead": True, "n_boot": n_boot,
                  "seed": cfg.seed}
        _log_trial(store, logged, "B", "signal", HYPOTHESIS, params, *PERIODS[p], now, note)
    echo(f"{len(logged) - n0} trial(s) logged (lookahead)")
    return OK


# --- helpers -------------------------------------------------------------------------------

def _sector_hedge(frame: pd.DataFrame, tk: str, assets: dict[str, Any], fallback: str) -> pd.Series:
    if tk not in assets:
        return frame[fallback]
    hk = iu.SECTOR_SPDR.get(assets[tk].screen_group or "", iu.HEDGE_MARKET)
    s = frame[hk].copy() if hk in frame.columns else frame[iu.HEDGE_MARKET].copy()
    if hk == "XLC":
        spy = frame[iu.HEDGE_MARKET]
        s[s.index < XLC_START] = np.nan
        k = s.first_valid_index()
        s = s.where(s.notna(), spy * (s[k] / spy[k])) if k is not None else spy
    return s


def _signed_cell(cell: str, p: str, dates: Any, vals: np.ndarray, plc: np.ndarray, cost: Any,
                 n_boot: int, seed: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Amendment 3: x_i = value_i - own placebo pool mean; D = mean(x) - cost; month-block CI."""
    vals, plc = np.asarray(vals, float), np.asarray(plc, float)
    cost_arr = np.broadcast_to(np.asarray(cost, float), vals.shape)
    ok = np.isfinite(vals) & np.isfinite(plc)
    d = np.asarray(list(dates), dtype=object)[ok]
    x = vals[ok] - plc[ok] - cost_arr[ok]
    lo, hi = fu.month_block_ci(d, x, n_boot, seed)
    note = {"n": int(ok.sum()), "event_mean": _num(float(vals[ok].mean()) if ok.any() else None),
            "placebo_mean": _num(float(plc[ok].mean()) if ok.any() else None),
            "cost": _num(float(cost_arr[ok].mean()) if ok.any() else None),
            "D": _num(float(x.mean()) if len(x) else None), "D_lo": _num(lo), "D_hi": _num(hi),
            "adr": "0016", **(extra or {})}
    note["summary"] = (f"n {note['n']} event {_f(note['event_mean'], 5)} placebo"
                       f" {_f(note['placebo_mean'], 5)} cost {_f(note['cost'], 4)} D"
                       f" {_f(note['D'], 5)} [{_f(lo, 5)}, {_f(hi, 5)}]")
    return note


def _oracle_cell(p: str, dates: Any, absv: np.ndarray, plc: np.ndarray, pool_abs: np.ndarray,
                 cost: float, n_boot: int, seed: int, extra: dict[str, Any] | None = None
                 ) -> dict[str, Any]:
    """Amendment 1: descriptive sizing; no verdict."""
    absv, plc = np.asarray(absv, float), np.asarray(plc, float)
    ok = np.isfinite(absv) & np.isfinite(plc)
    d = np.asarray(list(dates), dtype=object)[ok]
    x = absv[ok] - plc[ok]
    lo, hi = fu.month_block_ci(d, x, n_boot, seed)
    ev_m = float(absv[ok].mean()) if ok.any() else float("nan")
    pl_m = float(plc[ok].mean()) if ok.any() else float("nan")
    qe = fu.q_star(absv[ok], cost)
    qp = fu.q_star(pool_abs, cost)
    note = {"n": int(ok.sum()), "event_abs_mean": _num(ev_m), "placebo_abs_mean": _num(pl_m),
            "ratio": _num(ev_m / pl_m if pl_m else None), "delta": _num(float(x.mean())
                                                                         if len(x) else None),
            "delta_lo": _num(lo), "delta_hi": _num(hi), "cost": cost,
            "event_net_oracle": _num(ev_m - cost),
            **{f"event_{k}": _num(v) for k, v in qe.items()},
            **{f"placebo_{k}": _num(v) for k, v in qp.items()}, "adr": "0016", **(extra or {})}
    note["summary"] = (f"n {note['n']} E|R| {_f(ev_m, 5)} vs placebo {_f(pl_m, 5)}"
                       f" (x{_f(note['ratio'])}), delta {_f(note['delta'], 5)} [{_f(lo, 5)},"
                       f" {_f(hi, 5)}]; q* {_f(qe['q_mean'], 3)} (median {_f(qe['q_median'], 3)},"
                       f" wins {_f(qe['q_wins'], 3)}); ordinary-day q* {_f(qp['q_mean'], 3)}")
    return note


def _pool_means(keys: list[tuple], pool: pd.DataFrame, col: str = "value") -> np.ndarray:
    pm = pool.groupby(["ticker", "stratum"])[col].mean()
    return np.array([pm.get(k, np.nan) for k in keys], dtype=float)


# --- U-E: earnings ----------------------------------------------------------------------------

def _earnings(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, res, echo,
              n_boot) -> int:
    assets = {a.ticker: a for a in store.read_assets("B")}
    cmap = cik_to_ticker(list(assets.values()), sstore.read("cik_tickers"))
    tickers = sorted(set(cmap.values()))
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET})
    px = store.read_prices(tickers + hedges, DEV_START, cfg.holdout_start)
    if px.empty:
        echo("no daily prices")
        return MISSING
    px["date"] = pd.to_datetime(px["date"])
    cal = pd.DatetimeIndex(sorted(px.loc[px["ticker"] == iu.HEDGE_MARKET, "date"].unique()))
    cl = px.pivot(index="date", columns="ticker", values="adj_close").reindex(cal)
    tickers = [t for t in tickers if t in cl.columns]
    hc = {t: _sector_hedge(cl, t, assets, iu.HEDGE_MARKET) for t in tickers}
    cf = sstore.read("company_filings")
    earn = er.earnings_days(cf, cmap, cal)
    earn_all = er.earnings_days(cf, cmap, cal, dedup=False, deal_filter=False)
    r3 = er.hedged_3day(cl[tickers], hc, back=2)
    lead = pd.DataFrame({t: (cl[t].shift(2) / cl[t].shift(5) - 1) - (hc[t].shift(2) / hc[t].shift(5) - 1)
                         for t in tickers}, index=cal)
    r1 = pd.DataFrame({t: (cl[t] / cl[t].shift(1) - 1) - (hc[t] / hc[t].shift(1) - 1)
                       for t in tickers}, index=cal)
    near = np.zeros((len(cal), len(tickers)), dtype=bool)
    tix = {t: i for i, t in enumerate(tickers)}
    for t, d in zip(earn_all["ticker"], earn_all["d"], strict=True):
        if t in tix:
            pp = cal.get_indexer([pd.Timestamp(d)])[0]
            if pp >= 0:
                near[max(0, pp - EARN_EXCL):pp + EARN_EXCL + 1, tix[t]] = True
    R, L, R1 = r3.to_numpy(), lead.to_numpy(), r1.to_numpy()
    year = cal.year.to_numpy()
    for p, (a, b) in PERIODS.items():
        lo_, hi_ = pd.Timestamp(a), pd.Timestamp(b)
        cov = er.coverage_ok(earn_all, range(a.year, min(b.year, 2024) + 1)) & set(tickers)
        e = earn[(pd.to_datetime(earn["d"]) >= lo_) & (pd.to_datetime(earn["d"]) <= hi_)
                 & earn["ticker"].isin(cov)]
        pos = cal.get_indexer(pd.to_datetime(e["d"]))
        ti = np.array([tix[t] for t in e["ticker"]])
        okp = pos >= 0
        pos, ti, dts = pos[okp], ti[okp], pd.to_datetime(e["d"]).to_numpy()[okp]
        rv = R[pos, ti]
        lv = np.sign(rv) * L[pos, ti]
        # placebo pool: ordinary days of covered tickers in the period (amendment 5)
        inper = (cal >= lo_) & (cal <= hi_)
        rows = []
        for t in cov:
            j = tix[t]
            m = inper & ~near[:, j] & np.isfinite(R[:, j])
            idx = np.flatnonzero(m)
            rows.append(pd.DataFrame({"ticker": t, "stratum": year[idx], "abs": np.abs(R[idx, j]),
                                      "lead": np.sign(R[idx, j]) * L[idx, j]}))
        pool = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
            columns=["ticker", "stratum", "abs", "lead"])
        keys = list(zip([tickers[i] for i in ti], year[pos], strict=True))
        pm_abs = _pool_means(keys, pool, "abs")
        pm_lead = _pool_means(keys, pool, "lead")
        # amendment 5: share of events whose largest 1-day move in [d-5, d+1] is in [d-5, d-3]
        early = []
        for pp, j in zip(pos, ti, strict=True):
            w = R1[max(0, pp - 5):pp + 2, j]
            early.append(np.nanargmax(np.abs(w)) <= 2 if np.isfinite(w).any() and pp >= 5 else np.nan)
        early_by_year = pd.Series(early, index=year[pos], dtype=float).groupby(level=0).mean()
        res[("U-E", p)] = _oracle_cell(p, dts, np.abs(rv), pm_abs, pool["abs"].to_numpy(),
                                       COST_EARN, n_boot, cfg.seed,
                                       {"early_peak_share_by_year": {
                                           int(k): _num(v) for k, v in early_by_year.items()},
                                        "n_tickers": len(cov)})
        res[("U-E-lead", p)] = _signed_cell("U-E-lead", p, dts, lv, pm_lead, COST_EARN, n_boot,
                                            cfg.seed)
        for c in ("U-E", "U-E-lead"):
            echo(f"  {p} {c:<18} {res[(c, p)]['summary']}")
    return OK


# --- U-M: macro releases ----------------------------------------------------------------------

def _macro(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, res, echo,
           n_boot) -> int:
    daily = store.read_prices([iu.HEDGE_MARKET], date(2016, 1, 4), cfg.holdout_start)
    days = np.array(sorted({pd.Timestamp(x).date() for x in daily["date"]}), dtype=object)
    days = days[days < cfg.holdout_start]
    hold = int(datetime(cfg.holdout_start.year, cfg.holdout_start.month, cfg.holdout_start.day,
                        tzinfo=UTC).timestamp())
    spy = istore.read_bars([iu.HEDGE_MARKET], None, hold)
    if spy.empty or not len(days):
        echo("missing SPY bars / daily prices")
        return MISSING
    m = st.bar_matrix(spy, list(days))
    early, bad = mc.session_flags(m, list(days), mcal.NYSE_EARLY_CLOSES)
    drop = early | bad
    close = mc.last_close(m)
    close[bad] = np.nan
    prev = np.concatenate([[np.nan], close[:-1]])
    prev2 = np.concatenate([[np.nan, np.nan], close[:-2]])
    gap = m.open[:, 0] / prev - 1
    post = close / m.close[:, mc.K_1345] - 1
    lead = np.sign(gap) * (prev / prev2 - 1)
    for v in (gap, post, lead):
        v[drop] = np.nan
    es = mc.event_sets(list(days), drop, mcal.FOMC_SCHEDULED, mcal.FOMC_CANCELLED,
                       [d for d, _ in mcal.FOMC_UNSCHEDULED], mcal.CPI_RELEASES,
                       mcal.NFP_RELEASES, mcal.FOMC_PRESS_CONFERENCE)
    weekday = np.array([d.weekday() for d in days])
    year = np.array([d.year for d in days])
    for p, (a, b) in PERIODS.items():
        mask = (days >= a) & (days <= b)
        pool = es.pool[mask[es.pool]]
        for cell, vals, evs, oracle in (("U-M1", np.abs(gap), es.events["M4"], True),
                                        ("U-M2", np.abs(post), es.events["M5"], True),
                                        ("U-M1-lead", lead, es.events["M4"], False)):
            ev = evs[mask[evs]]
            ev = ev[np.isfinite(vals[ev])]
            r = mc.placebo_test(vals, ev, pool, weekday, year, n_boot, cfg.seed, 1)
            plc = np.full(len(ev), r.placebo_mean)
            dts = [days[i] for i in ev]
            if oracle:
                pv = vals[pool]
                note = _oracle_cell(p, dts, vals[ev], plc, pv[np.isfinite(pv)], COST_MACRO,
                                    n_boot, cfg.seed, {"placebo_p": _num(r.p)})
            else:
                note = _signed_cell(cell, p, dts, vals[ev], plc, COST_MACRO, n_boot, cfg.seed,
                                    {"placebo_p": _num(r.p)})
            res[(cell, p)] = note
            echo(f"  {p} {cell:<18} {note['summary']}")
    return OK


# --- U-D: DoD awards ----------------------------------------------------------------------------

def _dod(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, res, echo,
         n_boot) -> int:
    awards = pd.DataFrame(dstore.awards())
    rows = ds.award_rows(map_rows, awards, exclusions)
    days = ds.issuer_days(rows)
    listed = days[days["ticker"].notna()].copy()
    listed = listed[~listed["ticker"].map(ds.foreign_otc)]
    tickers = sorted(set(listed["ticker"]))
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET, "IWM"})
    px = dodcap.read_prices(tickers + hedges, DEV_START, cfg.holdout_start)
    if px.empty:
        echo("missing DoD prices")
        return MISSING
    px["date"] = pd.to_datetime(px["date"])
    cal = pd.DatetimeIndex(sorted(px.loc[px["ticker"] == iu.HEDGE_MARKET, "date"].unique()))
    wide = {c: px.pivot(index="date", columns="ticker", values=c).reindex(cal)
            for c in ("adj_open", "adj_close", "close", "volume")}
    tickers = [t for t in tickers if t in wide["close"].columns]
    splits_all = pd.DataFrame(dstore.splits(), columns=["ticker", "date", "ratio"])
    splits = {t: g for t, g in splits_all.groupby("ticker")}
    raw = si.raw_close(wide["close"][tickers], splits)
    mdv = si.median_dollar_volume(wide["close"][tickers], wide["volume"][tickers])
    trad = si.tradable(raw, mdv).to_numpy()
    sh = sstore.read("shares_out")
    sh["val"] = sh["val"].astype(float)
    fs = ds.filing_shares(sh)
    cik_tk = dict(zip(listed["cik"], listed["ticker"], strict=True))
    bad = ds.float_outliers(sh, fs, wide["close"], splits, cik_tk)
    listed = listed[listed["ticker"].isin(tickers) & ~listed["cik"].isin(bad)].reset_index(drop=True)
    listed["mcap"] = ds.market_caps(listed, fs, wide["close"], splits).to_numpy()
    listed["cell"] = ds.cell_of((listed["size"] / listed["mcap"]).to_numpy())
    listed["pos"] = cal.searchsorted(listed["d"].to_numpy(), "right") - 1
    listed = listed[listed["pos"] >= 1]
    tix = {t: i for i, t in enumerate(tickers)}
    j = np.array([tix[t] for t in listed["ticker"]])
    listed = listed[trad[listed["pos"].to_numpy(), j] & listed["cell"].ne("")].copy()
    assets = {a.ticker: a for a in store.read_assets("B")}
    aot = wide["adj_open"][tickers].to_numpy()
    act = wide["adj_close"][tickers].to_numpy()
    ho = np.column_stack([_sector_hedge(wide["adj_open"], t, assets, "IWM").to_numpy()
                          for t in tickers])
    hcl = np.column_stack([_sector_hedge(wide["adj_close"], t, assets, "IWM").to_numpy()
                           for t in tickers])
    with np.errstate(invalid="ignore", divide="ignore"):
        on = np.vstack([aot[1:] / act[:-1] - ho[1:] / hcl[:-1], np.full((1, len(tickers)), np.nan)])
        prior = np.vstack([np.full((1, len(tickers)), np.nan), act[1:] / act[:-1] - hcl[1:] / hcl[:-1]])
    mdv_np = mdv.to_numpy()
    award_pos: dict[int, set[int]] = {}
    for t, d in zip(rows["ticker"], rows["d"], strict=True):
        if isinstance(t, str) and t in tix:
            award_pos.setdefault(tix[t], set()).add(int(cal.searchsorted(d, "right") - 1))
    yr = cal.year.to_numpy()
    for p, (a, b) in PERIODS.items():
        lo_, hi_ = pd.Timestamp(a), pd.Timestamp(b)
        inper = (cal >= lo_) & (cal <= hi_)
        for cell in ds.CELLS:
            e = listed[listed["cell"] == cell]
            keep = ds.decluster(e["pos"].to_numpy(), e["ticker"].to_numpy(), 1)
            e = e[keep]
            e = e[inper[e["pos"].to_numpy()]]
            pos = e["pos"].to_numpy()
            jj = np.array([tix[t] for t in e["ticker"]], dtype=int)
            vals, lead = on[pos, jj], prior[pos, jj]
            pm, pm_lead, pm_ex = [], [], []
            for pp, k in zip(pos, jj, strict=True):
                m = inper & (yr == yr[pp]) & trad[:, k]
                idx = np.flatnonzero(m)
                pm.append(np.nanmean(on[idx, k]) if len(idx) else np.nan)
                pm_lead.append(np.nanmean(prior[idx, k]) if len(idx) else np.nan)
                aw = np.array(sorted(award_pos.get(k, set())))
                if len(aw):
                    dist = np.min(np.abs(idx[:, None] - aw[None, :]), axis=1)
                    idx2 = idx[dist > 1]
                else:
                    idx2 = idx
                pm_ex.append(np.nanmean(on[idx2, k]) if len(idx2) else np.nan)
            tb = e["ticker"].isin(assets).to_numpy()
            cost = np.where(tb, COST_DOD_TRACK_B, si.cost_round_trip(mdv_np[pos, jj]))
            dts = cal[pos]
            res[(f"U-D-{cell}", p)] = _signed_cell(
                f"U-D-{cell}", p, dts, vals, np.array(pm), cost, n_boot, cfg.seed,
                {"placebo_mean_award_excluded": _num(float(np.nanmean(pm_ex)) if pm_ex else None),
                 "n_track_b": int(tb.sum()), "label": "pre-release value (16:00 -> next open)"})
            res[(f"U-D-lead-{cell}", p)] = _signed_cell(
                f"U-D-lead-{cell}", p, dts, lead, np.array(pm_lead), cost, n_boot, cfg.seed)
            for c in (f"U-D-{cell}", f"U-D-lead-{cell}"):
                echo(f"  {p} {c:<18} {res[(c, p)]['summary']}")
    return OK


# --- U-I: insider purchases --------------------------------------------------------------------

def _insider(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, res, echo,
             n_boot) -> int:
    assets = {a.ticker: a for a in store.read_assets("B")}
    cmap = cik_to_ticker(list(assets.values()), sstore.read("cik_tickers"))
    spy = store.read_prices([iu.HEDGE_MARKET], date(2015, 10, 1), cfg.holdout_start)
    cal = pd.DatetimeIndex(sorted(pd.to_datetime(spy["date"]).unique()))
    subs, owners, trans = (sstore.read("insider_subs"), sstore.read("insider_owners"),
                           sstore.read("insider_trans"))
    acc = sev.qualifying_accessions(subs, owners, trans, cmap)
    ev = sev.insider_events(acc, cal)
    _late_report(subs, trans, cmap, echo)
    tickers = sorted(set(acc["ticker"]))
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET})
    px = store.read_prices(tickers + hedges, DEV_START, cfg.holdout_start)
    px["date"] = pd.to_datetime(px["date"])
    op = px.pivot(index="date", columns="ticker", values="adj_open").reindex(cal)
    cl = px.pivot(index="date", columns="ticker", values="adj_close").reindex(cal)
    tickers = [t for t in tickers if t in cl.columns]
    hc = {t: _sector_hedge(cl, t, assets, iu.HEDGE_MARKET) for t in tickers}
    ho = {t: _sector_hedge(op, t, assets, iu.HEDGE_MARKET) for t in tickers}
    cf = sstore.read("company_filings")
    cf["filing_date"] = pd.to_datetime(cf["filing_date"])
    periodic = {}
    for cik, g in cf[cf["form"].isin(["10-Q", "10-K"])].groupby(
            cf["cik"].astype(str).str.zfill(10)):
        if cik in cmap:
            periodic[cmap[cik]] = list(g["filing_date"])
    acc = acc.assign(event_day=sev.event_day(acc["filing_date"], cal))
    yr = cal.year.to_numpy()
    for p, (a, b) in PERIODS.items():
        lo_, hi_ = pd.Timestamp(a), pd.Timestamp(b)
        inper = np.asarray((cal >= lo_) & (cal <= hi_))
        last = int(np.flatnonzero(inper).max())
        strat = sev.strata(cl[tickers], hc, periodic, cal, inper)
        sp = {(t, s): np.sort(cal.get_indexer(g["date"]))
              for (t, s), g in strat.groupby(["ticker", "stratum"])}
        sday = {(t, d): s for t, d, s in zip(strat["ticker"], strat["date"], strat["stratum"],
                                              strict=True)}
        for kind in ("ins_all", "ins_cxo"):
            e = ev[kind]
            e = e[(e["date"] >= lo_) & (e["date"] <= hi_) & e["ticker"].isin(tickers)]
            sub = acc if kind == "ins_all" else acc[acc["cxo"]]
            vals, plc, plc_un, vw, filing_seg, dts = [], [], [], [], [], []
            n_zero = 0
            for d, t in zip(e["date"], e["ticker"], strict=True):
                g = sub[(sub["ticker"] == t) & (sub["event_day"] == d)]
                if g.empty:
                    continue
                c, h = cl[t].to_numpy(), hc[t].to_numpy()
                fpos = cal.get_indexer([d])[0]
                end = fpos - 1
                tpos = int(cal.searchsorted(g["trade_date"].min(), "right") - 1)
                length = end - tpos
                if length <= 0 or tpos < 0:
                    n_zero += 1
                    continue
                vals.append(fu.hedged_window(c, h, np.array([tpos]), np.array([end]))[0])
                s = sday.get((t, cal[tpos]))
                starts = sp.get((t, s), np.array([], dtype=int))
                starts = starts[(starts + length <= last) & (starts >= 0)]
                plc.append(fu.pool_mean_window(c, h, starts, length))
                un = np.flatnonzero(inper & (yr == yr[tpos]))
                un = un[un + length <= last]
                plc_un.append(fu.pool_mean_window(c, h, un, length))
                w = []
                for td, v in zip(g["trade_date"], g["value"], strict=True):
                    tp = int(cal.searchsorted(td, "right") - 1)
                    if tp < end:
                        w.append((v, fu.hedged_window(c, h, np.array([tp]), np.array([end]))[0]))
                vw.append(sum(v * r for v, r in w) / sum(v for v, _ in w) if w else np.nan)
                o, hoo = op[t].to_numpy(), ho[t].to_numpy()
                filing_seg.append((o[fpos + 1] / c[end] - hoo[fpos + 1] / h[end])
                                  if fpos + 1 < len(cal) else np.nan)
                dts.append(d)
            cell = "U-I-all" if kind == "ins_all" else "U-I-cxo"
            res[(cell, p)] = _signed_cell(
                cell, p, dts, np.array(vals), np.array(plc), COST_INSIDER, n_boot, cfg.seed,
                {"placebo_mean_unmatched": _num(float(np.nanmean(plc_un)) if plc_un else None),
                 "value_weighted_mean": _num(float(np.nanmean(vw)) if vw else None),
                 "filing_segment_mean": _num(float(np.nanmean(filing_seg)) if filing_seg
                                             else None),
                 "n_zero_length": n_zero})
            echo(f"  {p} {cell:<18} {res[(cell, p)]['summary']}")
    return OK


def _late_report(subs: pd.DataFrame, trans: pd.DataFrame, cmap: dict[str, str], echo: Echo) -> None:
    """Amendment 4: purchases dropped by ADR 0011's 5-business-day filing rule, by gap."""
    s = subs[subs["document_type"].astype(str) == "4"].copy()
    s["cik"] = s["issuercik"].astype(str).str.zfill(10)
    s = s[s["cik"].isin(cmap)]
    t = trans[(trans["trans_code"] == "P") & (trans["trans_acquired_disp_cd"] == "A")]
    t = t.merge(s[["accession_number", "filing_date"]], on="accession_number")
    fd = sev._dates(t["filing_date"])
    td = sev._dates(t["trans_date"])
    ok = fd.notna() & td.notna()
    lag = np.busday_count(td[ok].to_numpy().astype("datetime64[D]"),
                          fd[ok].to_numpy().astype("datetime64[D]"))
    bins = pd.cut(pd.Series(lag), [-10**6, -1, 5, 10, 30, 10**6],
                  labels=["negative", "0-5 (kept)", "6-10", "11-30", ">30"])
    echo("insider purchases by trade-to-filing lag (business days): "
         + json.dumps({str(k): int(v) for k, v in bins.value_counts().sort_index().items()}))


def _f(x: float | None, d: int = 2) -> str:
    return "nan" if x is None or not isinstance(x, (int, float)) or not math.isfinite(x) \
        else f"{x:.{d}f}"
