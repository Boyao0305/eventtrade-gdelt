"""ADR 0018 forecasting the direction of the earnings reaction (hypothesis family W, with the
review amendments): features known at close(d-2), yearly walk-forward models, one primary test
of W-L on the pooled out-of-sample predictions 2018-01 -> 2025-03. Once only. Never names a
concrete adapter."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.cli.pipelines_foresight_ub import _sector_hedge
from eventtrade.cli.pipelines_sec_study import cik_to_ticker
from eventtrade.config import DEV_START, Config
from eventtrade.core import earnings as er
from eventtrade.core import earnings_forecast as ef
from eventtrade.core import intraday as iu
from eventtrade.core import sec_events as sev
from eventtrade.core import small_insider as si
from eventtrade.core import xbrl_features as xf

HYPOTHESIS = "W"
YEARS = tuple(range(2018, 2026))
OOS = (date(2018, 1, 1), date(2025, 3, 31))
SUB = {"2018-2019": (date(2018, 1, 1), date(2019, 12, 31)),
       "2020-2025Q1": (date(2020, 1, 1), date(2025, 3, 31))}
COVID = (pd.Timestamp("2020-02-20"), pd.Timestamp("2020-04-30"))
N_EXTRA_DSR = 20 + 136
MODELS = ("W-L", "W-G", "W-R0", "W-R1", "W-R2", "W-R3")
SECTORS = sorted(set(iu.SECTOR_SPDR))
TIMING_COLS = ("timing_lag",)
MAX_ABS_R = 1.0   # implementation note: |R| > 100% over 4 days = price error, dropped and counted


def earn_forecast(cfg: Config, store: Any, sstore: Any, dstore: Any, now: Clock,
                  echo: Echo = print, n_boot: int = 10_000) -> int:
    if OOS[1] >= cfg.holdout_start:
        echo("refused: the evaluation reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0018 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            echo(f"  {t.params.get('model')}: {json.loads(t.notes).get('summary', '')}")
        return OK
    ev = build_events(cfg, store, sstore, dstore, echo)
    if ev is None:
        return MISSING
    feat_cols = [c for c in ev.columns if c.startswith("f_")]
    echo(f"events: {len(ev):,} ({int(ev['deal_ok'].sum()):,} pass the deal filter), features"
         f" {len(feat_cols)}; years {sorted(ev['year'].unique().tolist())}")
    main = ev[ev["deal_ok"]].reset_index(drop=True)
    preds = _walk_forward(main, feat_cols, cfg.seed, echo)
    preds_nt = _walk_forward(main, [c for c in feat_cols if c[2:] not in TIMING_COLS], cfg.seed,
                             echo, only="L")
    preds_nd = _walk_forward(ev.reset_index(drop=True), feat_cols, cfg.seed, echo, only="L")
    r = main["R"].to_numpy()
    lo_w, hi_w = np.nanquantile(np.concatenate([main["R"].to_numpy()]), [0.01, 0.99])
    rw = np.clip(r, lo_w, hi_w)
    oos = _in(main["decision"], OOS) & preds["W-L"].notna().to_numpy()
    base = ef.net_pnl(preds["W-R0"].to_numpy(dtype=float), r)
    results = {}
    for mdl in MODELS:
        pr = preds[mdl].to_numpy(dtype=float)
        note = _stats(main, pr, r, rw, oos, base, n_boot, cfg.seed)
        if mdl == "W-L":
            note["subsets"] = _subsets(main, pr, r, oos, n_boot, cfg.seed)
            note["subsets"]["no_timing_feature"] = _brief(
                main, preds_nt["W-L"].to_numpy(dtype=float), r, oos, n_boot, cfg.seed)
            m_nd = _in(ev["decision"], OOS) & preds_nd["W-L"].notna().to_numpy()
            note["subsets"]["no_deal_filter"] = _brief(
                ev.reset_index(drop=True), preds_nd["W-L"].to_numpy(dtype=float),
                ev["R"].to_numpy(), m_nd, n_boot, cfg.seed)
            note["verdict"] = _verdict(note)
        results[mdl] = note
        echo(f"  {mdl}: {note['summary']}")
    echo(f"W-L verdict: {results['W-L']['verdict']}")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for mdl, note in results.items():
        params = {"adr": "0018", "model": mdl, "period": "2018-2025Q1", "n_boot": n_boot,
                  "seed": cfg.seed, "n_extra_dsr": N_EXTRA_DSR, "primary": mdl == "W-L"}
        _log_trial(store, logged, "B", "signal", HYPOTHESIS, params, *OOS, now, note)
    echo(f"{len(logged) - n0} trial(s) logged")
    return OK


# --- events and features ------------------------------------------------------------------------

def build_events(cfg: Config, store: Any, sstore: Any, dstore: Any, echo: Echo) -> pd.DataFrame | None:
    assets = {a.ticker: a for a in store.read_assets("B")}
    cmap = cik_to_ticker(list(assets.values()), sstore.read("cik_tickers"))
    tk_cik = {}
    for c, t in cmap.items():
        tk_cik.setdefault(t, c)
    tickers = sorted(set(cmap.values()))
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET})
    px = store.read_prices(tickers + hedges, DEV_START, cfg.holdout_start)
    if px.empty:
        echo("no daily prices")
        return None
    px["date"] = pd.to_datetime(px["date"])
    cal = pd.DatetimeIndex(sorted(px.loc[px["ticker"] == iu.HEDGE_MARKET, "date"].unique()))
    wide = {k: px.pivot(index="date", columns="ticker", values=k).reindex(cal)
            for k in ("adj_close", "close", "volume")}
    tickers = [t for t in tickers if t in wide["adj_close"].columns]
    cl, n_err = si.clean_opens(wide["adj_close"][tickers], wide["volume"][tickers])  # amendment 7
    cl_all = wide["adj_close"].copy()
    cl_all[tickers] = cl
    sp = pd.DataFrame(dstore.splits(), columns=["ticker", "date", "ratio"])
    raw = si.raw_close(wide["close"][tickers], {t: g for t, g in sp.groupby("ticker")})
    hc = {t: _sector_hedge(cl_all, t, assets, iu.HEDGE_MARKET) for t in tickers}
    echo(f"prices: {len(tickers)} tickers, {n_err} reverting price spikes screened")
    cf = sstore.read("company_filings")
    earn_deal = er.earnings_days(cf, cmap, cal)
    earn_nod = er.earnings_days(cf, cmap, cal, deal_filter=False)
    earn_all = er.earnings_days(cf, cmap, cal, dedup=False, deal_filter=False)
    r3 = er.hedged_3day(cl[tickers], hc, back=2)
    sz = er.surprise_z(r3, earn_nod, exclude=earn_all)
    deal_keys = set(zip(earn_deal["ticker"], pd.to_datetime(earn_deal["d"]), strict=True))
    acc = sev.qualifying_accessions(sstore.read("insider_subs"), sstore.read("insider_owners"),
                                    sstore.read("insider_trans"), cmap)
    acc_pos = cal.searchsorted(pd.to_datetime(acc["filing_date"]).to_numpy(), "left")
    ins = pd.DataFrame({"ticker": acc["ticker"], "pos": acc_pos, "value": acc["value"]})
    ea = earn_all.assign(year=pd.to_datetime(earn_all["d"]).dt.year)
    cov_cnt = ea.groupby(["ticker", "year"]).size()

    R, C, RAW = r3.to_numpy(), cl.to_numpy(), raw.to_numpy()
    H = np.column_stack([hc[t].to_numpy() for t in tickers])
    with np.errstate(invalid="ignore", divide="ignore"):
        r1 = C[1:] / C[:-1] - 1 - (H[1:] / H[:-1] - 1)
    r1 = np.vstack([np.full((1, len(tickers)), np.nan), r1])
    sig60 = pd.DataFrame(r1).rolling(60, min_periods=40).std().to_numpy()
    tix = {t: i for i, t in enumerate(tickers)}
    rows = []
    n_bad = 0
    sz_by = {t: g.sort_values("d") for t, g in sz.groupby("ticker")}
    earn_pos_by = {t: np.sort(cal.get_indexer(pd.to_datetime(g["d"])))
                   for t, g in earn_all.groupby("ticker")}
    ins_by = {t: g for t, g in ins.groupby("ticker")}
    for t, d in zip(earn_nod["ticker"], pd.to_datetime(earn_nod["d"]), strict=True):
        if t not in tix:
            continue
        j = tix[t]
        p = cal.get_indexer([d])[0]
        if p < 260 or p + 1 >= len(cal):
            continue
        rv = R[p, j]
        if not np.isfinite(rv) or rv == 0:
            continue
        if abs(rv) > MAX_ABS_R:   # one-way price errors (e.g. CHRD 2020-11) are not reverting
            n_bad += 1
            continue
        dec = cal[p - 2]
        yr = dec.year
        if cov_cnt.get((t, yr - 1), 0) < 3:          # amendment 8: coverage on the prior year
            continue
        f: dict[str, Any] = {"ticker": t, "d": d, "decision": dec, "label": cal[p + 1],
                             "year": yr, "R": rv, "deal_ok": (t, d) in deal_keys}
        g = sz_by.get(t)
        prev = g[cal.get_indexer(pd.to_datetime(g["d"])) + 1 <= p - 3].tail(4) if g is not None \
            else pd.DataFrame(columns=["r", "z"])
        zs = prev["z"].to_numpy()[::-1]
        rs = prev["r"].to_numpy()[::-1]
        for k in range(4):
            f[f"prev_sign_{k + 1}"] = float(np.sign(rs[k])) if k < len(rs) else np.nan
            f[f"f_prev_z_{k + 1}"] = float(zs[k]) if k < len(zs) else np.nan
        f["f_prev_z_mean"] = float(np.nanmean(zs)) if len(zs) else np.nan
        for k in range(4):
            f[f"f_prev_sign_{k + 1}"] = f[f"prev_sign_{k + 1}"]
        for name, back in (("ret_5", 7), ("ret_20", 22), ("ret_63", 63), ("ret_252", 252)):
            a, b = p - back, p - 2
            val = (C[b, j] / C[a, j] - 1) - (H[b, j] / H[a, j] - 1) if a >= 0 else np.nan
            f[f"f_{name}"] = val
            if name == "ret_63":
                f["ret_63"] = val
        f["f_vol60"] = sig60[p - 2, j]
        zwin = np.abs(r1[p - 4:p - 1, j] / sig60[p - 5, j]) if sig60[p - 5, j] > 0 else np.array([])
        f["pre_move"] = bool(np.nanmax(zwin) >= 2) if len(zwin) and np.isfinite(zwin).any() else False
        ig = ins_by.get(t)
        if ig is not None:
            m = (ig["pos"] >= p - 92) & (ig["pos"] <= p - 3)
            f["f_ins_n"] = float(m.sum())
            f["f_ins_val"] = float(np.log1p(ig.loc[m, "value"].sum()))
        else:
            f["f_ins_n"], f["f_ins_val"] = 0.0, 0.0
        ep = earn_pos_by.get(t, np.array([], dtype=int))
        cand = ep[(ep >= p - 262) & (ep <= p - 242)]
        f["f_timing_lag"] = float(cand[np.argmin(np.abs(cand - (p - 252)))] * -1 + p - 252) \
            if len(cand) else np.nan
        f["predictable"] = bool(len(cand) and abs(f["f_timing_lag"]) <= 5)
        sec = assets[t].screen_group if t in assets else None
        for s in SECTORS:
            f[f"f_sec_{s}"] = float(sec == s)
        f["cik"] = tk_cik.get(t)
        f["raw_close"] = RAW[p - 2, j]
        rows.append(f)
    ev = pd.DataFrame(rows)
    echo(f"events dropped as price errors (|R| > {MAX_ABS_R:.0%}): {n_bad}")
    facts = sstore.read("xbrl_facts")
    for col in ("val",):
        facts[col] = pd.to_numeric(facts[col], errors="coerce")
    fx = xf.fundamental_features(facts, ev[["cik", "d", "decision"]])
    with np.errstate(invalid="ignore", divide="ignore"):
        ev["f_eps_growth"] = fx["eps_growth_raw"].to_numpy() / ev["raw_close"].to_numpy()
    for c in ("sue_q", "sue_q1", "sue_q2", "sue_q3", "rev_growth", "q_end_age_days"):
        ev[f"f_{c}"] = fx[c].to_numpy()
    ev = ev[ev["label"] < pd.Timestamp(cfg.holdout_start)].reset_index(drop=True)
    return ev


def _walk_forward(ev: pd.DataFrame, cols: list[str], seed: int, echo: Echo,
                  only: str | None = None) -> pd.DataFrame:
    out = pd.DataFrame(index=ev.index, columns=list(MODELS), dtype=float)
    x = ev[cols].to_numpy(dtype=float)
    y = (ev["R"].to_numpy() > 0).astype(int)
    for yr, tr, te in ef.yearly_splits(ev["decision"], ev["label"], YEARS):
        w = ef.weights(ev["R"].to_numpy()[tr])
        for mdl in ("L", "G"):
            if only is not None and mdl != only:
                continue
            out.loc[te, f"W-{mdl}"] = ef.fit_predict(mdl, x[tr], y[tr], w, x[te], seed)
        if only is None:
            rules = ef.rule_predictions(ev.iloc[te].reset_index(drop=True))
            for k, v in rules.items():
                out.loc[te, k] = v
        echo(f"    walk-forward {yr}: train {len(tr):,}, test {len(te):,}")
    return out


# --- statistics ---------------------------------------------------------------------------------

def _in(dates: pd.Series, span: tuple[date, date]) -> np.ndarray:
    d = pd.to_datetime(dates)
    return ((d >= pd.Timestamp(span[0])) & (d <= pd.Timestamp(span[1]))).to_numpy()


def _brief(ev: pd.DataFrame, pred: np.ndarray, r: np.ndarray, mask: np.ndarray, n_boot: int,
           seed: int) -> dict[str, Any]:
    m = mask & np.isfinite(pred) & np.isfinite(r)
    pnl = ef.net_pnl(pred[m], r[m])
    lo, p = ef.week_block_lower(ev["decision"][m], pnl, n_boot, seed)
    return {"n": int(m.sum()), "mean_net": _num(float(pnl.mean()) if m.any() else None),
            "lower95": _num(lo), "p": _num(p)}


def _stats(ev: pd.DataFrame, pred: np.ndarray, r: np.ndarray, rw: np.ndarray, oos: np.ndarray,
           base: np.ndarray, n_boot: int, seed: int) -> dict[str, Any]:
    m = oos & np.isfinite(pred)
    d = ev["decision"][m]
    pnl = ef.net_pnl(pred[m], r[m])
    pnl_w = ef.net_pnl(pred[m], rw[m])
    lo, p = ef.week_block_lower(d, pnl, n_boot, seed)
    lo_w, _ = ef.week_block_lower(d, pnl_w, n_boot, seed)
    diff = pnl - base[m]
    lo_d, p_d = ef.week_block_lower(d, diff, n_boot, seed)
    short = pred[m] < 0
    abs_r = float(np.abs(r[m]).mean()) if m.any() else float("nan")
    note: dict[str, Any] = {
        "adr": "0018", "n": int(m.sum()), "n_short": int(short.sum()),
        "hit": _num(float((np.sign(r[m]) == pred[m]).mean()) if m.any() else None),
        "weighted_hit": _num(ef.weighted_hit(pred[m], r[m])),
        "q_star": _num((1 + ef.COST / abs_r) / 2 if abs_r > 0 else None),
        "mean_net": _num(float(pnl.mean()) if m.any() else None), "lower95": _num(lo), "p": _num(p),
        "mean_net_wins": _num(float(pnl_w.mean()) if m.any() else None),
        "lower95_wins": _num(lo_w), "calendar_t": _num(ef.calendar_t(d, pnl)),
        "diff_vs_R0": _num(float(diff.mean()) if m.any() else None), "diff_lower95": _num(lo_d),
        "diff_p": _num(p_d),
        "short_mean_net": _num(float(pnl[short].mean()) if short.any() else None),
    }
    for name, span in SUB.items():
        s = _in(d, span)
        note[f"mean_net_{name}"] = _num(float(pnl[s].mean()) if s.any() else None)
    q = pd.to_datetime(d).dt.to_period("Q").astype(str).to_numpy()
    note["quarters"] = len(set(q))
    note["summary"] = (f"n {note['n']} hit {_f(note['hit'], 3)} w-hit {_f(note['weighted_hit'], 3)}"
                       f" (q* {_f(note['q_star'], 3)}) net {_f(note['mean_net'], 5)} lo"
                       f" {_f(lo, 5)} p {_f(p, 4)} | wins {_f(note['mean_net_wins'], 5)} | vs R0"
                       f" {_f(note['diff_vs_R0'], 5)} lo {_f(lo_d, 5)} | short"
                       f" {_f(note['short_mean_net'], 5)} | cal t {_f(note['calendar_t'])}")
    return note


def _subsets(ev: pd.DataFrame, pred: np.ndarray, r: np.ndarray, oos: np.ndarray, n_boot: int,
             seed: int) -> dict[str, Any]:
    d = pd.to_datetime(ev["decision"])
    out = {"predictable_dates": _brief(ev, pred, r, oos & ev["predictable"].to_numpy(bool),
                                       n_boot, seed),
           "no_pre_move": _brief(ev, pred, r, oos & ~ev["pre_move"].to_numpy(bool), n_boot, seed),
           "ex_covid": _brief(ev, pred, r, oos & ~((d >= COVID[0]) & (d <= COVID[1])).to_numpy(),
                              n_boot, seed)}
    return out


def _verdict(n: dict[str, Any]) -> str:
    pos = lambda x: x is not None and x > 0
    sub = n["subsets"]
    ok = (pos(n["mean_net"]) and pos(n["lower95"]) and pos(n["mean_net_wins"])
          and pos(n["lower95_wins"]) and (n["calendar_t"] or 0) > 1.645 and pos(n["diff_lower95"])
          and pos(n["short_mean_net"]) and pos(n["mean_net_2018-2019"])
          and pos(n["mean_net_2020-2025Q1"])
          and all(pos(sub[k]["mean_net"]) for k in ("predictable_dates", "no_pre_move", "ex_covid",
                                                     "no_timing_feature", "no_deal_filter")))
    return "CONFIRMED" if ok else "not confirmed"


def _f(x: Any, dd: int = 2) -> str:
    return "nan" if x is None or not isinstance(x, (int, float)) or not math.isfinite(x) \
        else f"{x:.{dd}f}"
