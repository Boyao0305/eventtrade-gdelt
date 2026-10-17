"""ADR 0019 pre-earnings informed-trading footprints (hypothesis family X, with the review
amendments): announcement days from 8-K report dates, footprint features X1-X6, X-L (W-L plus
X1-X6) against W-L on the same events, the X2 decile rule against its return placebo. Once only.
A positive verdict is "candidate" (hold-out pending). Never names a concrete adapter."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli import pipelines_earn_forecast as pef
from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.cli.pipelines_foresight_ub import _sector_hedge
from eventtrade.cli.pipelines_sec_study import cik_to_ticker
from eventtrade.config import DEV_START, Config
from eventtrade.core import earnings as er
from eventtrade.core import earnings_forecast as ef
from eventtrade.core import informed as inf
from eventtrade.core import intraday as iu
from eventtrade.core import macro_calendar as mcal
from eventtrade.core.screen import holm

HYPOTHESIS = "X"
XCOLS = ("f_X1", "f_X2", "f_X3", "f_X4", "f_X5", "f_X6")
OTHER_EARN = 15
HOLM_ALPHA = 0.05


def informed_study(cfg: Config, store: Any, sstore: Any, dstore: Any, istore: Any, svstore: Any,
                   now: Clock, echo: Echo = print, n_boot: int = 10_000) -> int:
    if pef.OOS[1] >= cfg.holdout_start:
        echo("refused: the evaluation reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0019 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            echo(f"  {t.params.get('model')}: {json.loads(t.notes).get('summary', '')}")
        return OK
    assets = {a.ticker: a for a in store.read_assets("B")}
    cmap = cik_to_ticker(list(assets.values()), sstore.read("cik_tickers"))
    spy = store.read_prices([iu.HEDGE_MARKET], DEV_START, cfg.holdout_start)
    cal = pd.DatetimeIndex(sorted(pd.to_datetime(spy["date"]).unique()))
    day_map, n_moved = _day_map(sstore.read("eightk_times"), cmap, cal)
    echo(f"announcement days: {len(day_map):,} 2.02 8-Ks mapped; {n_moved:,} moved before the"
         f" 8-K filing day by reportDate/acceptance")
    ev = pef.build_events(cfg, store, sstore, dstore, echo, day_map)
    if ev is None:
        return MISSING
    ev = ev[ev["deal_ok"]].reset_index(drop=True)
    cf = sstore.read("company_filings")
    earn_all = er.earnings_days(cf, cmap, cal, dedup=False, deal_filter=False)
    earn_pos = {t: np.sort(cal.get_indexer(pd.to_datetime(g["d"]))) for t, g in
                earn_all.groupby("ticker")}
    empty = np.array([], dtype=int)
    other = np.array([bool(((earn_pos.get(t, empty) >= p - OTHER_EARN)
                            & (earn_pos.get(t, empty) <= p - 1)).any())
                      for t, p in zip(ev["ticker"], ev["pos"], strict=True)], dtype=bool)
    ev = ev[~other].reset_index(drop=True)
    echo(f"events: {len(ev):,} after dropping {int(other.sum())} with another 2.02 8-K in"
         f" [a-{OTHER_EARN}, a-1]")
    _x_features(cfg, store, istore, svstore, assets, ev, earn_pos, cal, echo)
    cols35 = [c for c in ev.columns if c.startswith("f_") and c not in XCOLS]
    colsx = cols35 + list(XCOLS)
    preds_x = pef._walk_forward(ev, colsx, cfg.seed, echo)
    preds_w = pef._walk_forward(ev, cols35, cfg.seed, echo, only="L")
    r = ev["R"].to_numpy()
    lo_w, hi_w = np.nanquantile(r, [0.01, 0.99])
    rw = np.clip(r, lo_w, hi_w)
    oos = pef._in(ev["decision"], pef.OOS)
    base_r0 = ef.net_pnl(np.ones(len(r)), r)
    pr_xl = preds_x["W-L"].to_numpy(dtype=float)
    pr_wl = preds_w["W-L"].to_numpy(dtype=float)
    pr_xg = preds_x["W-G"].to_numpy(dtype=float)
    xr, xrp = _decile_rules(ev, oos)
    res: dict[str, dict[str, Any]] = {}
    # X-L
    n_xl = pef._stats(ev, pr_xl, r, rw, oos, base_r0, n_boot, cfg.seed)
    m = oos & np.isfinite(pr_xl) & np.isfinite(pr_wl)
    d_w = ef.net_pnl(pr_xl[m], r[m]) - ef.net_pnl(pr_wl[m], r[m])
    lo_b, p_b = ef.week_block_lower(ev["decision"][m], d_w, n_boot, cfg.seed)
    n_xl.update(diff_vs_WL=_num(float(d_w.mean())), diff_vs_WL_lower95=_num(lo_b),
                diff_vs_WL_p=_num(p_b), n_disagree=int((pr_xl[m] != pr_wl[m]).sum()),
                predictable=pef._brief(ev, pr_xl, r, oos & ev["predictable"].to_numpy(bool),
                                       n_boot, cfg.seed),
                subsets=pef._subsets(ev, pr_xl, r, oos, n_boot, cfg.seed),
                WL_same_events=pef._stats(ev, pr_wl, r, rw, oos, base_r0, n_boot, cfg.seed))
    res["X-L"] = n_xl
    # X-R vs its return placebo X-R'
    traded = oos & np.isfinite(xr)
    n_xr = pef._stats(ev, xr, r, rw, traded, base_r0, n_boot, cfg.seed)
    pnl_xr = np.where(np.isfinite(xr), ef.net_pnl(np.nan_to_num(xr, nan=1.0), r), 0.0)
    pnl_xp = np.where(np.isfinite(xrp), ef.net_pnl(np.nan_to_num(xrp, nan=1.0), r), 0.0)
    lo_p, p_p = ef.week_block_lower(ev["decision"][oos], (pnl_xr - pnl_xp)[oos], n_boot, cfg.seed)
    tp = oos & np.isfinite(xrp)
    n_xr.update(diff_vs_placebo=_num(float((pnl_xr - pnl_xp)[oos].mean())),
                diff_vs_placebo_lower95=_num(lo_p), diff_vs_placebo_p=_num(p_p),
                placebo=pef._stats(ev, xrp, r, rw, tp, base_r0, n_boot, cfg.seed),
                predictable=pef._brief(ev, xr, r, traded & ev["predictable"].to_numpy(bool),
                                       n_boot, cfg.seed))
    res["X-R"] = n_xr
    res["X-G"] = pef._stats(ev, pr_xg, r, rw, oos, base_r0, n_boot, cfg.seed)
    hp = holm({"X-L": res["X-L"]["diff_vs_WL_p"] or math.nan, "X-R": res["X-R"]["p"] or math.nan},
              HOLM_ALPHA)
    res["X-L"]["verdict"] = _verdict_xl(res["X-L"], "X-L" in hp)
    res["X-R"]["verdict"] = _verdict_xr(res["X-R"], "X-R" in hp)
    for k in ("X-L", "X-R", "X-G"):
        echo(f"  {k}: {res[k]['summary']}" + (f" | verdict {res[k].get('verdict')}"
                                             if k != "X-G" else ""))
    for col in ("f_X1", "f_X2", "f_X3", "f_X6"):
        res[f"deciles-{col[2:]}"] = _decile_table(ev, col, oos)
        echo(f"  deciles {col[2:]}: {res[f'deciles-{col[2:]}']['summary']}")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for k, note in res.items():
        params = {"adr": "0019", "model": k, "period": "2018-2025Q1", "n_boot": n_boot,
                  "seed": cfg.seed, "reported_only": k.startswith("deciles") or k == "X-G"}
        _log_trial(store, logged, "B", "signal", HYPOTHESIS, params, *pef.OOS, now, note)
    echo(f"{len(logged) - n0} trial(s) logged")
    return OK


def _day_map(eightk: pd.DataFrame, cmap: dict[str, str], cal: pd.DatetimeIndex
             ) -> tuple[dict, int]:
    e = eightk[eightk["items"].fillna("").str.contains(r"\b2\.02\b")].copy()
    e["ticker"] = e["cik"].astype(str).str.zfill(10).map(cmap)
    e = e.dropna(subset=["ticker"])
    out: dict[tuple[str, pd.Timestamp], pd.Timestamp] = {}
    moved = 0
    for t, fd, rd, acc in zip(e["ticker"], e["filing_date"], e["report_date"], e["acceptance"],
                              strict=True):
        f = date.fromisoformat(fd)
        rdd = date.fromisoformat(rd) if rd else None
        d = cal[int(cal.searchsorted(pd.Timestamp(f), "left"))] if pd.Timestamp(f) <= cal[-1] \
            else None
        a = inf.announcement_day(f, rdd, acc, cal)
        if d is None or a is None:
            continue
        key = (t, d)
        if key not in out or a < out[key]:
            out[key] = a
        moved += a < d
    return out, moved


def _x_features(cfg: Config, store: Any, istore: Any, svstore: Any, assets: dict, ev: pd.DataFrame,
                earn_pos: dict, cal: pd.DatetimeIndex, echo: Echo) -> None:
    tickers = sorted(set(ev["ticker"]))
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET})
    px = store.read_prices(tickers + hedges, DEV_START, cfg.holdout_start)
    px["date"] = pd.to_datetime(px["date"])
    vol = px.pivot(index="date", columns="ticker", values="volume").reindex(cal)
    cl = px.pivot(index="date", columns="ticker", values="adj_close").reindex(cal)
    mlr = inf.market_log_ratio(vol[[t for t in tickers if t in vol.columns]].to_numpy())
    sv = svstore.read()
    sv["ratio"] = sv["short"] / sv["total"].where(sv["total"] > 0)
    srat = sv.pivot(index="date", columns="symbol", values="ratio")
    srat.index = pd.to_datetime(srat.index)
    srat = srat.reindex(cal)
    days = [d.date() for d in cal]
    early = np.zeros(len(cal), dtype=bool)
    early[cal.get_indexer(pd.to_datetime(list(mcal.NYSE_EARLY_CLOSES)))[
        cal.get_indexer(pd.to_datetime(list(mcal.NYSE_EARLY_CLOSES))) >= 0]] = True
    hold = int(pd.Timestamp(cfg.holdout_start, tz="UTC").timestamp())
    hbars = {h: inf.bar_arrays(istore.read_bars([h], None, hold), days) for h in hedges}
    feats = {c: np.full(len(ev), np.nan) for c in XCOLS}
    ret_w = np.full(len(ev), np.nan)
    n_no_bars = n_no_finra = 0
    for t, idx in ev.groupby("ticker").indices.items():
        sub = ev.iloc[idx]
        ep = earn_pos.get(t, np.array([], dtype=int))
        hk = iu.SECTOR_SPDR.get(assets[t].screen_group or "", iu.HEDGE_MARKET) \
            if t in assets else iu.HEDGE_MARKET
        hk = hk if hk in hbars else iu.HEDGE_MARKET
        bars = istore.read_bars([t], None, hold)
        flow = inf.daily_flow(inf.bar_arrays(bars, days), hbars[hk], early) if len(bars) else None
        v = vol[t].to_numpy() if t in vol.columns else np.full(len(cal), np.nan)
        sym = t.replace("-", ".")
        sr = srat[sym].to_numpy() if sym in srat.columns else np.full(len(cal), np.nan)
        hc = _sector_hedge(cl, t, assets, iu.HEDGE_MARKET).to_numpy()
        c = cl[t].to_numpy() if t in cl.columns else np.full(len(cal), np.nan)
        for i, p in zip(idx, sub["pos"], strict=True):
            x1 = inf.volume_ratio(v, p, ep) - mlr[p]
            ff = inf.flow_features(flow, p, ep) if flow is not None else {"X2": np.nan,
                                                                           "X6": np.nan}
            x3 = inf.short_feature(sr, p, ep)
            feats["f_X1"][i], feats["f_X2"][i], feats["f_X3"][i] = x1, ff["X2"], x3
            feats["f_X4"][i] = x1 * np.sign(ff["X2"]) if np.isfinite(ff["X2"]) else np.nan
            feats["f_X5"][i] = x1 * x3
            feats["f_X6"][i] = ff["X6"]
            n_no_bars += not np.isfinite(ff["X2"])
            n_no_finra += not np.isfinite(x3)
            a0, a1 = p - 13, p - 3
            ret_w[i] = (c[a1] / c[a0] - 1) - (hc[a1] / hc[a0] - 1) if a0 >= 0 else np.nan
    for k, vals in feats.items():
        ev[k] = vals
    ev["ret_w"] = ret_w
    ok = np.isfinite(ev["f_X2"]) & np.isfinite(ret_w)
    corr = float(np.corrcoef(ev.loc[ok, "f_X2"], ret_w[ok])[0, 1]) if ok.sum() > 2 else math.nan
    echo(f"X features: X2 missing {n_no_bars:,}, X3 missing {n_no_finra:,} of {len(ev):,};"
         f" corr(X2, hedged W return) {corr:.3f}")


def _decile_rules(ev: pd.DataFrame, oos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """X-R (X2 deciles) and X-R' (hedged W-return deciles): cut-offs from each walk-forward
    step's training events; long at/above the 90th, short at/below the 10th, else no trade."""
    xr = np.full(len(ev), np.nan)
    xp = np.full(len(ev), np.nan)
    for _, tr, te in ef.yearly_splits(ev["decision"], ev["label"], pef.YEARS):
        for col, out in (("f_X2", xr), ("ret_w", xp)):
            v_tr = ev[col].to_numpy()[tr]
            v_tr = v_tr[np.isfinite(v_tr)]
            if len(v_tr) < 100:
                continue
            lo, hi = np.quantile(v_tr, [0.1, 0.9])
            v = ev[col].to_numpy()[te]
            out[te] = np.where(v >= hi, 1.0, np.where(v <= lo, -1.0, np.nan))
    return xr, xp


def _decile_table(ev: pd.DataFrame, col: str, oos: np.ndarray) -> dict[str, Any]:
    """Reported only (amendment 9): mean R by training-cut-off decile of one feature."""
    dec = np.full(len(ev), np.nan)
    for _, tr, te in ef.yearly_splits(ev["decision"], ev["label"], pef.YEARS):
        v_tr = ev[col].to_numpy()[tr]
        v_tr = v_tr[np.isfinite(v_tr)]
        if len(v_tr) < 100:
            continue
        edges = np.quantile(v_tr, np.linspace(0.1, 0.9, 9))
        v = ev[col].to_numpy()[te]
        dec[te] = np.where(np.isfinite(v), np.searchsorted(edges, v, side="right"), np.nan)
    r = ev["R"].to_numpy()
    means = [_num(float(r[oos & (dec == k)].mean())) if (oos & (dec == k)).any() else None
             for k in range(10)]
    ns = [int((oos & (dec == k)).sum()) for k in range(10)]
    return {"adr": "0019", "feature": col, "mean_R_by_decile": means, "n_by_decile": ns,
            "summary": " ".join(f"{m * 100:.2f}" if m is not None else "nan" for m in means)}


def _verdict_xl(n: dict[str, Any], holm_ok: bool) -> str:
    pos = lambda x: x is not None and x > 0
    ok = (holm_ok and pos(n["mean_net"]) and pos(n["lower95"]) and pos(n["mean_net_wins"])
          and pos(n["lower95_wins"]) and (n["calendar_t"] or 0) > 1.645
          and pos(n["diff_vs_WL_lower95"]) and pos(n["diff_lower95"]) and pos(n["short_mean_net"])
          and pos(n["mean_net_2018-2019"]) and pos(n["mean_net_2020-2025Q1"])
          and pos(n["predictable"]["mean_net"]))
    return "candidate (hold-out pending)" if ok else "not a candidate"


def _verdict_xr(n: dict[str, Any], holm_ok: bool) -> str:
    pos = lambda x: x is not None and x > 0
    ok = (holm_ok and pos(n["mean_net"]) and pos(n["lower95"]) and pos(n["mean_net_wins"])
          and pos(n["lower95_wins"]) and (n["calendar_t"] or 0) > 1.645
          and pos(n["short_mean_net"]) and pos(n["mean_net_2018-2019"])
          and pos(n["mean_net_2020-2025Q1"]) and pos(n["diff_vs_placebo_lower95"])
          and pos(n["predictable"]["mean_net"]))
    return "candidate (hold-out pending)" if ok else "not a candidate"
