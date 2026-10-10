"""ADR 0011 SEC event study (insider purchases, initial 13Ds): audit step (no prices) and the
once-only study (12 cells, matched placebo, BH, Holm + costs + March-2020 robustness).
Never names a concrete adapter: the entry point passes the stores in."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.config import Config
from eventtrade.core import eventstudy
from eventtrade.core import intraday as iu
from eventtrade.core import sec_events as sev
from eventtrade.core.screen import benjamini_hochberg, holm

HYPOTHESIS = "S1"
HORIZONS = (5, 20, 60)
N_BOOT = 2000
BH_Q = 0.10
HOLM_ALPHA = 0.05
ROUND_TRIP = 0.0030  # 2 x 10 bp stock + 2 x 5 bp sector ETF (amendment 8)
DISCOVERY = (date(2016, 1, 4), date(2019, 12, 31))
CONFIRMATION = (date(2020, 1, 2), date(2025, 3, 31))
COVID = (pd.Timestamp("2020-02-20"), pd.Timestamp("2020-04-30"))
XLC_START = pd.Timestamp("2018-06-19")


def cik_to_ticker(assets: list[Any], cik_tickers: pd.DataFrame) -> dict[str, str]:
    """One universe ticker per CIK: the first listed in company_tickers.json (amendment 4)."""
    uni = {a.ticker.upper(): a.ticker for a in assets}
    out: dict[str, str] = {}
    for cik, tk in zip(cik_tickers["cik"], cik_tickers["ticker"], strict=True):
        t = uni.get(str(tk).upper())
        c = str(cik).zfill(10)
        if t is not None and c not in out:
            out[c] = t
    return out


def _calendar(store: Any, cfg: Config) -> pd.DatetimeIndex:
    spy = store.read_prices([iu.HEDGE_MARKET], date(2015, 10, 1), cfg.holdout_start)
    return pd.DatetimeIndex(sorted(pd.to_datetime(spy["date"]).unique()))


def _all_events(store: Any, sstore: Any, cfg: Config) -> tuple[dict[str, pd.DataFrame], pd.DataFrame,
                                                               dict[str, str], pd.DatetimeIndex]:
    assets = store.read_assets("B")
    cmap = cik_to_ticker(assets, sstore.read("cik_tickers"))
    cal = _calendar(store, cfg)
    acc = sev.qualifying_accessions(sstore.read("insider_subs"), sstore.read("insider_owners"),
                                    sstore.read("insider_trans"), cmap)
    ev = sev.insider_events(acc, cal)
    e13, audit = sev.events_13d(sstore.read("filings_13d"), sstore.read("subject_13d"),
                                sstore.read("company_filings"), cmap, cal)
    ev["act_13d"] = e13
    return ev, audit, cmap, cal


def sec_audit(cfg: Config, store: Any, sstore: Any, echo: Echo = print, n: int = 50,
              seed: int | None = None) -> int:
    """Amendment 3: 50 random initial 13D events with names and deal flags. Reads no prices."""
    ev, audit, cmap, _cal = _all_events(store, sstore, cfg)
    if audit.empty:
        echo("no 13D events: run ingest-sec and ingest-sec-extra")
        return MISSING
    echo(f"CIK map: {len(cmap)} universe CIKs; events: "
         + ", ".join(f"{k} {len(v)}" for k, v in ev.items()))
    sample = audit.sample(min(n, len(audit)), random_state=seed if seed is not None else cfg.seed)
    for r in sample.sort_values("file_date").itertuples(index=False):
        names = " | ".join(json.loads(r.names))[:150]
        echo(f"{r.file_date:%Y-%m-%d} {r.ticker:<6} {'DEAL' if r.deal else '    '} {names}")
    echo(f"deal-flagged: {int(audit['deal'].sum())}/{len(audit)}")
    return OK


def sec_study(cfg: Config, store: Any, sstore: Any, now: Clock, echo: Echo = print,
              n_boot: int = N_BOOT) -> int:
    if CONFIRMATION[1] >= cfg.holdout_start:
        echo("refused: confirmation reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0011 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            nn = json.loads(t.notes)
            echo(f"  {t.params.get('period')} {t.params.get('cell')}: mean {nn.get('mean')}"
                 f" t {nn.get('t')} p {nn.get('p')} {nn.get('verdict', '')}")
        return OK
    ev, audit, cmap, cal = _all_events(store, sstore, cfg)
    assets = {a.ticker: a for a in store.read_assets("B")}
    tickers = sorted(set(cmap.values()))
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET})
    px = store.read_prices(tickers + hedges, date(2015, 10, 1), cfg.holdout_start)
    if px.empty:
        echo("no daily prices: run ingest-prices")
        return MISSING
    px["date"] = pd.to_datetime(px["date"])
    op = px.pivot(index="date", columns="ticker", values="adj_open").reindex(cal)
    cl = px.pivot(index="date", columns="ticker", values="adj_close").reindex(cal)
    first_px = op.apply(lambda s: s.first_valid_index())
    tickers = [t for t in tickers if t in op.columns]

    def hedge_series(frame: pd.DataFrame, tk: str) -> pd.Series:
        hk = iu.SECTOR_SPDR.get(assets[tk].screen_group or "", iu.HEDGE_MARKET)
        s = frame[hk].copy()
        if hk == "XLC":
            s[s.index < XLC_START] = np.nan
            spy = frame[iu.HEDGE_MARKET]
            # splice by returns: SPY before XLC exists (ratio-scaled at the first XLC day)
            if s.notna().any():
                k = s.first_valid_index()
                s = s.where(s.notna(), spy * (s[k] / spy[k]))
            else:
                s = spy
        return s

    hedge_open = {t: hedge_series(op, t) for t in tickers}
    hedge_close = {t: hedge_series(cl, t) for t in tickers}
    for k in list(ev):
        e = ev[k]
        ok = [first_px.get(t) is not None and pd.notna(first_px.get(t)) and d >= first_px[t]
              for d, t in zip(e["date"], e["ticker"], strict=True)]
        ev[k] = e[np.asarray(ok, dtype=bool)].reset_index(drop=True)
    echo("events: " + ", ".join(f"{k} {len(v)}" for k, v in ev.items())
         + f"; 13D deal-flagged dropped {int(audit['deal'].sum())}")
    cf = sstore.read("company_filings")
    cf["filing_date"] = pd.to_datetime(cf["filing_date"])
    periodic = {}
    for cik, g in cf[cf["form"].isin(["10-Q", "10-K"])].groupby(cf["cik"].astype(str).str.zfill(10)):
        if cik in cmap:
            periodic[cmap[cik]] = list(g["filing_date"])

    def period(span: tuple[date, date]) -> tuple[np.ndarray, int]:
        lo, hi = pd.Timestamp(span[0]), pd.Timestamp(span[1])
        mask = (cal >= lo) & (cal <= hi)
        return np.asarray(mask), int(np.flatnonzero(mask).max())

    results: dict[tuple[str, str, int], dict[str, Any]] = {}
    for pname, span in (("discovery", DISCOVERY), ("confirmation", CONFIRMATION)):
        mask, last = period(span)
        strat = sev.strata(cl[tickers], hedge_close, periodic, cal, mask)
        for h in HORIZONS:
            oc_all = sev.hedged_open_returns(op[tickers], hedge_open, h)
            pos = cal.get_indexer(oc_all["date"])
            oc = oc_all[(pos >= 0) & mask[np.maximum(pos, 0)] & (pos + 1 + h <= last)]
            for etype in sev.EVENT_TYPES:
                if pname == "confirmation" and (etype, "discovery", h) not in results:
                    continue
                if pname == "confirmation" and not results[(etype, "discovery", h)]["discovered"]:
                    continue
                e = ev[etype]
                e = e[(e["date"] >= pd.Timestamp(span[0])) & (e["date"] <= pd.Timestamp(span[1]))]
                m = sev.matched_placebo(e, oc, strat, h, 1, cal, n_boot, cfg.seed)
                um = eventstudy.placebo_p(e, oc, h, 1, cal, n_boot, cfg.seed)
                note = {"mean": _num(m.mean), "t": _num(m.t), "p": _num(m.p),
                        "n_events": m.n_events, "n_event_days": m.n_event_days,
                        "n_matched": m.n_matched, "null_t_mean": _num(m.null_t_mean),
                        "p_unmatched": _num(um.p), "net": _num(m.mean - ROUND_TRIP),
                        "round_trip": ROUND_TRIP, "adr": "0011"}
                if pname == "confirmation":
                    ex = e[~((e["date"] >= COVID[0]) & (e["date"] <= COVID[1]))]
                    s_ex = eventstudy.calendar_time_series(ex, oc, h, cal)
                    note["mean_ex_covid"] = _num(float(s_ex.mean())) if len(s_ex) else None
                    cut = pd.Timestamp("2024-02-05")
                    for lab, sub in (("pre_2024_02", e[e["date"] < cut]),
                                     ("post_2024_02", e[e["date"] >= cut])):
                        ss = eventstudy.calendar_time_series(sub, oc, h, cal)
                        note[f"mean_{lab}"] = _num(float(ss.mean())) if len(ss) else None
                results[(etype, pname, h)] = note
                echo(f"  {pname:<12} {etype:<11} h{h:<3} n {m.n_events:>5} mean"
                     f" {_f(m.mean, 5)} t {_f(m.t)} p {_f(m.p, 4)} (unmatched {_f(um.p, 4)})")
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
        ok = (f"{k[0]}|h{k[2]}" in hp and (v["mean"] or 0) > 0 and (v["net"] or 0) > 0
              and (v.get("mean_ex_covid") or 0) > 0)
        v["holm"] = f"{k[0]}|h{k[2]}" in hp
        v["verdict"] = "CONFIRMED" if ok else "not confirmed"
        echo(f"  confirmation {k[0]} h{k[2]}: {v['verdict']} (net {v['net']},"
             f" ex-covid {v.get('mean_ex_covid')})")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for (etype, pname, h), note in results.items():
        span = DISCOVERY if pname == "discovery" else CONFIRMATION
        params = {"adr": "0011", "cell": f"{etype}|h{h}", "event": etype, "horizon": h,
                  "period": pname, "n_boot": n_boot, "seed": cfg.seed, "null": "matched"}
        _log_trial(store, logged, "B", "signal", HYPOTHESIS, params, *span, now, note)
    echo(f"{len(logged) - n0} trial(s) logged")
    return OK


def _f(x: float | None, d: int = 2) -> str:
    return "nan" if x is None or not math.isfinite(x) else f"{x:.{d}f}"

