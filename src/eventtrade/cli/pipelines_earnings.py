"""ADR 0013 post-earnings drift (hypothesis P1): once-only study on Track B with the review
amendments (matched null on non-surprise earnings, timing check, coverage, survivorship guard,
declustered secondary null). Never names a concrete adapter."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.cli.pipelines_sec_study import cik_to_ticker
from eventtrade.config import DEV_START, Config
from eventtrade.core import earnings as er
from eventtrade.core import eventstudy
from eventtrade.core import intraday as iu
from eventtrade.core import sec_events as sev
from eventtrade.core.screen import benjamini_hochberg, holm

HYPOTHESIS = "P1"
HORIZONS = (5, 20, 60)
SIGNS = {"pos": 1, "neg": -1}
N_BOOT = 2000
BH_Q = 0.10
HOLM_ALPHA = 0.05
ROUND_TRIP = 0.0030
BORROW_ANNUAL = 0.0050
DISCOVERY = (date(2016, 1, 4), date(2019, 12, 31))
CONFIRMATION = (date(2020, 1, 2), date(2025, 3, 31))
COVERAGE_YEARS = {"discovery": range(2016, 2020), "confirmation": range(2020, 2025)}
COVID = (pd.Timestamp("2020-02-20"), pd.Timestamp("2020-04-30"))
XLC_START = pd.Timestamp("2018-06-19")
TIMING_LIMIT = 0.10


def earnings_study(cfg: Config, store: Any, sstore: Any, now: Clock, echo: Echo = print,
                   n_boot: int = N_BOOT) -> int:
    if CONFIRMATION[1] >= cfg.holdout_start:
        echo("refused: confirmation reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0013 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            n = json.loads(t.notes)
            echo(f"  {t.params.get('period')} {t.params.get('cell')}: mean {n.get('mean')}"
                 f" p {n.get('p')} {n.get('verdict', '')}")
        return OK
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
    op = px.pivot(index="date", columns="ticker", values="adj_open").reindex(cal)
    cl = px.pivot(index="date", columns="ticker", values="adj_close").reindex(cal)
    tickers = [t for t in tickers if t in op.columns]

    def hedge(frame: pd.DataFrame, tk: str) -> pd.Series:
        hk = iu.SECTOR_SPDR.get(assets[tk].screen_group or "", iu.HEDGE_MARKET)
        s = frame[hk].copy() if hk in frame.columns else frame[iu.HEDGE_MARKET].copy()
        if hk == "XLC":
            s[s.index < XLC_START] = np.nan
            spy = frame[iu.HEDGE_MARKET]
            k = s.first_valid_index()
            s = s.where(s.notna(), spy * (s[k] / spy[k])) if k is not None else spy
        return s

    h_open = {t: hedge(op, t) for t in tickers}
    h_close = {t: hedge(cl, t) for t in tickers}
    cf = sstore.read("company_filings")
    earn = er.earnings_days(cf, cmap, cal)
    earn_all = er.earnings_days(cf, cmap, cal, dedup=False, deal_filter=False)
    echo(f"earnings days: {len(earn):,} (all 2.02 days {len(earn_all):,}); tickers {earn['ticker'].nunique()}")

    # --- timing check (amendment 2), outcome-free --------------------------------------------
    share = er.pre_reaction_share(cl[tickers], h_close, earn)
    disc_share = float(share.loc[[y for y in share.index if 2016 <= y <= 2019]].mean())
    back = 2 if disc_share > TIMING_LIMIT else 1
    echo("timing: share of reactions before d by year " + json.dumps(
        {int(k): round(float(v), 3) for k, v in share.items()}) +
         f"; discovery mean {disc_share:.3f} -> window close(d-{back}) to close(d+1)")
    r3 = er.hedged_3day(cl[tickers], h_close, back=back)
    sz = er.surprise_z(r3, earn, exclude=earn_all)
    frac = float((sz["z"].abs() >= er.Z_EVENT).mean())
    echo(f"surprises: {len(sz):,} with z; fraction |z| >= 2: {frac:.3f}")
    ev_all = er.events(sz, cal)
    first_px = op[tickers].apply(lambda s: s.first_valid_index())
    late = {t for t, d in first_px.items() if d is None or d > pd.Timestamp(DEV_START)}

    results: dict[tuple[str, str, int], dict[str, Any]] = {}
    for pname, span in (("discovery", DISCOVERY), ("confirmation", CONFIRMATION)):
        lo, hi = pd.Timestamp(span[0]), pd.Timestamp(span[1])
        mask = np.asarray((cal >= lo) & (cal <= hi))
        last = int(np.flatnonzero(mask).max())
        cov = er.coverage_ok(earn_all, COVERAGE_YEARS[pname])
        echo(f"{pname}: coverage keeps {len(cov & set(tickers))}/{len(tickers)} tickers")
        # primary pool: the same stock's non-surprise earnings decision days (|z| < 2)
        calm = sz[sz["z"].abs() < er.Z_EVENT]
        cp = cal.get_indexer(calm["d"]) + 1
        okc = (cp > 0) & (cp < len(cal))
        pool = pd.DataFrame({"date": cal[cp[okc]], "ticker": calm["ticker"].to_numpy()[okc],
                             "stratum": "earn"})
        big = er.bigmove_strata(r3, earn_all, mask)
        for h in HORIZONS:
            oc_all = sev.hedged_open_returns(op[tickers], h_open, h)
            pos = cal.get_indexer(oc_all["date"])
            oc = oc_all[(pos >= 0) & mask[np.maximum(pos, 0)] & (pos + 1 + h <= last)]
            for side, sgn in SIGNS.items():
                key = (side, pname, h)
                if pname == "confirmation" and not results.get((side, "discovery", h), {}).get("discovered"):
                    continue
                e = ev_all[side]
                e = e[(e["date"] >= lo) & (e["date"] <= hi) & e["ticker"].isin(cov)]
                e_st = e.assign(stratum="earn")
                m1 = sev.matched_placebo(e, oc, e_st, h, sgn, cal, n_boot, cfg.seed, pool_strat=pool)
                m2 = sev.matched_placebo(e, oc, e.assign(stratum=side), h, sgn, cal, n_boot,
                                         cfg.seed, pool_strat=big)
                um = eventstudy.placebo_p(e, oc, h, sgn, cal, n_boot, cfg.seed)
                have, _ = eventstudy._events_with_outcome(e, oc)
                vals = have["value"].to_numpy()
                wins = (float(np.clip(vals, *np.nanquantile(vals, [0.01, 0.99])).mean())
                        if len(vals) else float("nan"))
                borrow = BORROW_ANNUAL * h / 252 if sgn < 0 else 0.0
                note = {"mean": _num(m1.mean), "t": _num(m1.t), "p": _num(m1.p),
                        "p_secondary_bigmove": _num(m2.p), "p_unmatched": _num(um.p),
                        "n_events": m1.n_events, "n_event_days": m1.n_event_days,
                        "n_matched": m1.n_matched, "winsorized_event_mean": _num(wins),
                        "net_directional": _num(sgn * m1.mean - ROUND_TRIP - borrow),
                        "window_back": back, "adr": "0013"}
                if pname == "confirmation":
                    ex = e[~((e["date"] >= COVID[0]) & (e["date"] <= COVID[1]))]
                    s_ex = eventstudy.calendar_time_series(ex, oc, h, cal)
                    note["mean_ex_covid"] = _num(float(s_ex.mean())) if len(s_ex) else None
                    s_old = eventstudy.calendar_time_series(e[~e["ticker"].isin(late)], oc, h, cal)
                    note["mean_excl_late_tickers"] = _num(float(s_old.mean())) if len(s_old) else None
                results[key] = note
                echo(f"  {pname:<12} {side} h{h:<3} n {m1.n_events:>5} mean {_f(m1.mean, 5)}"
                     f" t {_f(m1.t)} p {_f(m1.p, 4)} (big-move {_f(m2.p, 4)}, unmatched {_f(um.p, 4)})")
        if pname == "discovery":
            pv = {f"{k[0]}|h{k[2]}": (v["p"] if v["p"] is not None else math.nan)
                  for k, v in results.items() if k[1] == "discovery"}
            disc = benjamini_hochberg(pv, BH_Q)
            for k, v in results.items():
                if k[1] == "discovery":
                    v["discovered"] = f"{k[0]}|h{k[2]}" in disc
            echo(f"discovery: {len(disc)} of {len(pv)} cells discovered (BH q = {BH_Q})")
    conf = {k: v for k, v in results.items() if k[1] == "confirmation"}
    hp = holm({f"{k[0]}|h{k[2]}": (v["p"] if v["p"] is not None else math.nan)
               for k, v in conf.items()}, HOLM_ALPHA)
    for k, v in conf.items():
        sgn = SIGNS[k[0]]
        ok = (f"{k[0]}|h{k[2]}" in hp and sgn * (v["mean"] or 0) > 0
              and (v["net_directional"] or -1) > 0 and sgn * (v.get("mean_ex_covid") or 0) > 0)
        if k[0] == "pos":
            ok = ok and ((v.get("p_secondary_bigmove") or 1) <= 0.10
                         or (v.get("mean_excl_late_tickers") or 0) > 0)
        v["holm"] = f"{k[0]}|h{k[2]}" in hp
        v["verdict"] = "CONFIRMED" if ok else "not confirmed"
        echo(f"  confirmation {k[0]} h{k[2]}: {v['verdict']} (net {v['net_directional']})")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for (side, pname, h), note in results.items():
        span = DISCOVERY if pname == "discovery" else CONFIRMATION
        params = {"adr": "0013", "cell": f"{side}|h{h}", "period": pname, "n_boot": n_boot,
                  "seed": cfg.seed, "null": "matched_nonsurprise_earnings"}
        _log_trial(store, logged, "B", "signal", HYPOTHESIS, params, *span, now, note)
    echo(f"{len(logged) - n0} trial(s) logged")
    return OK


def _f(x: float | None, d: int = 2) -> str:
    return "nan" if x is None or not math.isfinite(x) else f"{x:.{d}f}"
