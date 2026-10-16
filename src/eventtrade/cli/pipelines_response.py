"""ADR 0017 event response curves (hypothesis family V, with the review amendments): magnitude
(standardised excess variance), direction (signed excess), continuation/reversal, time scale.
Once only. Never names a concrete adapter."""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli import pipelines as pl
from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.cli.pipelines_foresight_ub import _sector_hedge
from eventtrade.cli.pipelines_sec_study import cik_to_ticker
from eventtrade.config import DEV_START, Config
from eventtrade.core import dod_study as ds
from eventtrade.core import earnings as er
from eventtrade.core import event_signals as evs
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_study as st
from eventtrade.core import macro as mc
from eventtrade.core import macro_calendar as mcal
from eventtrade.core import response_curves as rc
from eventtrade.core import sec_events as sev
from eventtrade.core import small_insider as si

HYPOTHESIS = "V"
PERIODS = {"A": (date(2016, 1, 4), date(2019, 12, 31)), "B": (date(2020, 1, 2), date(2025, 3, 31))}
LEVEL_DAILY = 1 - 0.05 / 8
LEVEL_INTRA = 1 - 0.05 / 7
THETA, WINDOW, DECLUSTER = 3.0, 60, 20
CONFLICT = ("quad3", "quad4", "root_14", "root_163", "violence", "tone")
COOP = ("quad1", "quad2")
PRODUCER = ("quad4", "violence", "root_163", "root_14")
MIN_EVENTS = MIN_DATES = 30
CPI_POINTS = (("09:30o", "open0"), ("09:45", 0), ("10:00", 1), ("10:30", 3), ("11:30", 7),
              ("13:00", 13), ("close", "last"))
FOMC_POINTS = (("14:00", 17), ("14:15", 18), ("14:30", 19), ("14:45", 20), ("15:00", 21),
               ("15:30", 23), ("close", "last"))


@dataclasses.dataclass
class DailyCell:
    name: str
    cal: pd.DatetimeIndex
    curves: dict[str, rc.AssetCurves]
    events: pd.DataFrame            # asset, pos (base position), date (base date)
    pool_ok: dict[str, np.ndarray]  # extra pool condition (tradable, non-announcement)
    key: dict[str, np.ndarray]      # pool key per position (year, or year x weekday)
    note: dict[str, Any] = dataclasses.field(default_factory=dict)


def response_curves(cfg: Config, store: Any, sstore: Any, istore: Any, dstore: Any, dodcap: Any,
                    map_rows: pd.DataFrame, exclusions: pd.DataFrame, now: Clock,
                    echo: Echo = print, n_boot: int = 10_000) -> int:
    if PERIODS["B"][1] >= cfg.holdout_start:
        echo("refused: period B reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0017 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            echo(f"  {t.params.get('period')} {t.params.get('cell')}:"
                 f" {json.loads(t.notes).get('reading', '')}")
        return OK
    cells: list[DailyCell] = []
    for build in (_earn_cell, _macro_cells, _dod_cells, _insider_cells, _gdelt_a_cells,
                  _gdelt_b_cells):
        got = build(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, echo)
        if got is None:
            return MISSING
        cells += got
    res: dict[tuple[str, str], dict[str, Any]] = {}
    for c in cells:
        for p in PERIODS:
            res[(c.name, p)] = _daily_stats(c, p, n_boot, cfg.seed)
        _reading(res, c.name, rc.KS)
        for p in PERIODS:
            echo(f"  {c.name:<14} {p} {res[(c.name, p)]['summary']}")
    for name, note_by_p in _intraday_cells(cfg, store, istore, n_boot, cfg.seed, echo).items():
        for p, note in note_by_p.items():
            res[(name, p)] = note
        _reading(res, name, [lab for lab, _ in (CPI_POINTS if "CPI" in name else FOMC_POINTS)])
        for p in PERIODS:
            echo(f"  {name:<14} {p} {res[(name, p)]['summary']}")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for (cell, p), note in res.items():
        params = {"adr": "0017", "cell": cell, "period": p, "lookahead": False,
                  "n_boot": n_boot, "seed": cfg.seed, "theta": THETA, "window": WINDOW}
        _log_trial(store, logged, "B", "signal", HYPOTHESIS, params, *PERIODS[p], now, note)
    echo(f"{len(logged) - n0} trial(s) logged")
    return OK


# --- statistics ------------------------------------------------------------------------------

def _daily_stats(c: DailyCell, p: str, n_boot: int, seed: int) -> dict[str, Any]:
    a, b = PERIODS[p]
    lo_, hi_ = pd.Timestamp(a), pd.Timestamp(b)
    e = c.events[(c.events["date"] >= lo_) & (c.events["date"] <= hi_)]
    inper = {k: (c.cal >= lo_) & (c.cal <= hi_) & c.pool_ok[k] for k in c.curves}
    ev = rc.CellEvents(e["asset"].to_numpy(), e["pos"].to_numpy(dtype=int),
                       e["date"].to_numpy())
    note: dict[str, Any] = {"adr": "0017", "n_events": len(e),
                            "n_dates": int(e["date"].nunique()), **c.note}
    if len(e) < MIN_EVENTS or note["n_dates"] < MIN_DATES:
        note.update(insufficient=True, summary=f"insufficient: {len(e)} events,"
                                               f" {note['n_dates']} dates")
        return note
    curves: dict[str, dict[str, list]] = {m: [] for m in ("EV", "EV_lo", "EV_hi", "S", "S_lo",
                                                          "S_hi", "C", "C_lo", "C_hi", "M",
                                                          "EV_incl", "S_incl", "n")}
    abs1 = None
    for k in rc.KS:
        out = rc.event_x(c.curves, ev, inper, c.key, k, exclusive=True)
        inc = rc.event_x(c.curves, ev, inper, c.key, k, exclusive=False)
        d = ev.date
        for meas, tag in (("z2", "EV"), ("signed", "S"), ("cont", "C")):
            x = out[meas][1]
            m = float(np.nanmean(x)) if np.isfinite(x).any() else float("nan")
            lo, hi = rc.block_ci(d, x, n_boot, seed, LEVEL_DAILY)
            curves[tag].append(_num(m))
            curves[f"{tag}_lo"].append(_num(lo))
            curves[f"{tag}_hi"].append(_num(hi))
        curves["M"].append(_num(float(np.nanmean(out["abs"][1]))))
        curves["EV_incl"].append(_num(float(np.nanmean(inc["z2"][1]))))
        curves["S_incl"].append(_num(float(np.nanmean(inc["signed"][1]))))
        curves["n"].append(int(np.isfinite(out["z2"][1]).sum()))
        if k == rc.KS[0]:
            abs1 = float(np.nanmean(np.abs(out["signed"][0])))
    pre = np.array([c.curves[a_].pre[pp] for a_, pp in zip(ev.asset, ev.pos, strict=True)])
    evk = [(v * k if v is not None else np.nan) for v, k in zip(curves["EV"], rc.KS, strict=True)]
    k_star, hl = rc.peak_half_life(rc.KS, evk)
    lo_at = dict(zip(rc.KS, curves["EV_lo"], strict=True))
    if k_star is not None and not (lo_at.get(k_star) or -1) > 0:
        k_star, hl = None, None
    rhl = rc.reversal_half_life(rc.KS, [v if v is not None else np.nan for v in curves["C"]],
                                abs1 or 0.0)
    note.update(curves=curves, ks=list(rc.KS), ev_times_k=[_num(v) for v in evk],
                k_star=k_star, half_life=hl, reversal_half_life=rhl,
                mean_abs_car1=_num(abs1), pre_run_mean=_num(float(np.nanmean(pre))))
    note["summary"] = _summary(note, rc.KS)
    return note


def _summary(note: dict[str, Any], ks: Any) -> str:
    cv = note["curves"]
    parts = []
    for i, k in enumerate(ks):
        parts.append(f"{k}:EV {_f(cv['EV'][i], 2)}[{_f(cv['EV_lo'][i], 2)}] S {_f(cv['S'][i], 4)}"
                     f" C {_f(cv['C'][i], 4)}")
    return (f"n {note['n_events']} k* {note.get('k_star')} hl {note.get('half_life')}"
            f" rev {note.get('reversal_half_life')} | " + "; ".join(parts))


def _reading(res: dict, cell: str, ks: Any) -> None:
    """Amendments 2, 8: same horizon in both periods."""
    notes = [res.get((cell, p)) for p in PERIODS]
    if any(n is None or n.get("insufficient") for n in notes):
        reading = "insufficient"
    else:
        def sig(n: dict, tag: str, i: int) -> int:
            lo, hi = n["curves"][f"{tag}_lo"][i], n["curves"][f"{tag}_hi"][i]
            if lo is None or hi is None:
                return 0
            return 1 if lo > 0 else (-1 if hi < 0 else 0)

        idx = range(len(ks))
        moves = [ks[i] for i in idx if all(sig(n, "EV", i) == 1 for n in notes)]
        direc = [ks[i] for i in idx
                 if sig(notes[0], "S", i) != 0 and sig(notes[0], "S", i) == sig(notes[1], "S", i)]
        cont = [ks[i] for i in idx
                if sig(notes[0], "C", i) != 0 and sig(notes[0], "C", i) == sig(notes[1], "C", i)]
        any_sc = any(sig(n, t, i) != 0 for n in notes for t in ("S", "C") for i in idx)
        if direc or cont:
            reading = "directional"
        elif moves and not any_sc:
            reading = "directionless (volatility)"
        elif moves:
            reading = "moves prices (direction unstable across periods)"
        else:
            reading = "no response"
        for n in notes:
            n.update(moves_at=moves, directional_at=direc, continuation_at=cont)
    for p in PERIODS:
        if (cell, p) in res:
            res[(cell, p)]["reading"] = reading


# --- cell builders -------------------------------------------------------------------------------

def _curves_for(close: pd.DataFrame, hedge: dict[str, pd.Series | None]) -> dict[str, rc.AssetCurves]:
    return {t: rc.asset_curves(close[t].to_numpy(),
                               None if hedge[t] is None else hedge[t].to_numpy())
            for t in close.columns}


def _year_key(cal: pd.DatetimeIndex, names: Any) -> dict[str, np.ndarray]:
    y = cal.year.to_numpy()
    return {t: y for t in names}


def _ev_frame(asset: Any, pos: Any, cal: pd.DatetimeIndex) -> pd.DataFrame:
    pos = np.asarray(pos, dtype=int)
    ok = (pos >= 0) & (pos < len(cal))
    return pd.DataFrame({"asset": np.asarray(asset)[ok], "pos": pos[ok], "date": cal[pos[ok]]})


def _track_b(cfg: Config, store: Any, sstore: Any) -> tuple:
    assets = {a.ticker: a for a in store.read_assets("B")}
    cmap = cik_to_ticker(list(assets.values()), sstore.read("cik_tickers"))
    tickers = sorted(set(cmap.values()))
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET})
    px = store.read_prices(tickers + hedges, DEV_START, cfg.holdout_start)
    px["date"] = pd.to_datetime(px["date"])
    cal = pd.DatetimeIndex(sorted(px.loc[px["ticker"] == iu.HEDGE_MARKET, "date"].unique()))
    cl = px.pivot(index="date", columns="ticker", values="adj_close").reindex(cal)
    tickers = [t for t in tickers if t in cl.columns]
    hc = {t: _sector_hedge(cl, t, assets, iu.HEDGE_MARKET) for t in tickers}
    return assets, cmap, tickers, cal, cl, hc


def _earn_cell(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, echo):
    _assets, cmap, tickers, cal, cl, hc = _track_b(cfg, store, sstore)
    cf = sstore.read("company_filings")
    earn = er.earnings_days(cf, cmap, cal)
    earn_all = er.earnings_days(cf, cmap, cal, dedup=False, deal_filter=False)
    cov = er.coverage_ok(earn_all, range(2016, 2020)) | er.coverage_ok(earn_all, range(2020, 2025))
    earn = earn[earn["ticker"].isin(cov) & earn["ticker"].isin(tickers)]
    pos = cal.get_indexer(pd.to_datetime(earn["d"])) - 2    # base close(d-2) (amendment 4)
    keep = cal.get_indexer(pd.to_datetime(earn["d"])) >= 2
    ev = _ev_frame(earn["ticker"].to_numpy()[keep], pos[keep], cal)
    names = sorted(set(ev["asset"]))
    curves = _curves_for(cl[names], {t: hc[t] for t in names})
    echo(f"V-EARN: {len(ev):,} earnings events, {len(names)} tickers")
    return [DailyCell("V-EARN", cal, curves, ev, {t: np.ones(len(cal), bool) for t in names},
                      _year_key(cal, names))]


def _spy(cfg: Config, store: Any) -> tuple:
    px = store.read_prices([iu.HEDGE_MARKET], DEV_START, cfg.holdout_start)
    px["date"] = pd.to_datetime(px["date"])
    px = px.sort_values("date")
    cal = pd.DatetimeIndex(px["date"])
    return cal, px["adj_close"].to_numpy()


def _macro_sets(days: list[date]) -> mc.EventSets:
    return mc.event_sets(days, np.zeros(len(days), bool), mcal.FOMC_SCHEDULED,
                         mcal.FOMC_CANCELLED, [d for d, _ in mcal.FOMC_UNSCHEDULED],
                         mcal.CPI_RELEASES, mcal.NFP_RELEASES, mcal.FOMC_PRESS_CONFERENCE)


def _macro_cells(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, echo):
    cal, c = _spy(cfg, store)
    days = [d.date() for d in cal]
    es = _macro_sets(days)
    curves = {"SPY": rc.asset_curves(c, None)}
    key = {"SPY": (cal.year * 7 + cal.weekday).to_numpy()}
    out = []
    for name, evp in (("V-CPI", es.events["M4"]), ("V-FOMC", es.events["M5"])):
        ok = np.zeros(len(cal), bool)
        ok[es.pool] = True
        ok[evp] = True
        ev = _ev_frame(["SPY"] * len(evp), np.asarray(evp) - 1, cal)
        pool_ok = np.zeros(len(cal), bool)
        pool_ok[np.clip(np.flatnonzero(ok) - 1, 0, None)] = True  # bases = day before
        out.append(DailyCell(name, cal, curves, ev, {"SPY": pool_ok}, key))
    echo(f"V-CPI/V-FOMC: {len(es.events['M4'])} / {len(es.events['M5'])} days")
    return out


def _dod_cells(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, echo):
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
        return None
    px["date"] = pd.to_datetime(px["date"])
    cal = pd.DatetimeIndex(sorted(px.loc[px["ticker"] == iu.HEDGE_MARKET, "date"].unique()))
    wide = {c: px.pivot(index="date", columns="ticker", values=c).reindex(cal)
            for c in ("adj_close", "close", "volume")}
    tickers = [t for t in tickers if t in wide["close"].columns]
    splits_all = pd.DataFrame(dstore.splits(), columns=["ticker", "date", "ratio"])
    splits = {t: g for t, g in splits_all.groupby("ticker")}
    raw = si.raw_close(wide["close"][tickers], splits)
    mdv = si.median_dollar_volume(wide["close"][tickers], wide["volume"][tickers])
    trad = si.tradable(raw, mdv)
    sh = sstore.read("shares_out")
    sh["val"] = sh["val"].astype(float)
    fs = ds.filing_shares(sh)
    cik_tk = dict(zip(listed["cik"], listed["ticker"], strict=True))
    bad = ds.float_outliers(sh, fs, wide["close"], splits, cik_tk)
    listed = listed[listed["ticker"].isin(tickers) & ~listed["cik"].isin(bad)].reset_index(drop=True)
    listed["mcap"] = ds.market_caps(listed, fs, wide["close"], splits).to_numpy()
    listed["cell"] = ds.cell_of((listed["size"] / listed["mcap"]).to_numpy())
    listed["pos"] = cal.searchsorted(listed["d"].to_numpy(), "right") - 1
    listed = listed[listed["pos"] >= 0]
    tr = trad.to_numpy()
    tix = {t: i for i, t in enumerate(tickers)}
    ok = np.array([bool(tr[pp, tix[t]]) for pp, t in zip(listed["pos"], listed["ticker"],
                                                          strict=True)], dtype=bool)
    listed = listed[ok & listed["cell"].ne("").to_numpy()]
    assets = {a.ticker: a for a in store.read_assets("B")}
    out = []
    for cell in ds.CELLS:
        e = listed[listed["cell"] == cell]
        e = e[ds.decluster(e["pos"].to_numpy(), e["ticker"].to_numpy(), 1)]
        names = sorted(set(e["ticker"]))
        curves = _curves_for(wide["adj_close"][names],
                             {t: _sector_hedge(wide["adj_close"], t, assets, "IWM") for t in names})
        out.append(DailyCell(f"V-DOD-{cell[:3]}", cal, curves,
                             _ev_frame(e["ticker"], e["pos"], cal),
                             {t: tr[:, tix[t]] for t in names}, _year_key(cal, names)))
    echo(f"V-DOD: {', '.join(f'{c.name} {len(c.events)}' for c in out)}")
    return out


def _insider_cells(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, echo):
    _assets, cmap, tickers, cal, cl, hc = _track_b(cfg, store, sstore)
    acc = sev.qualifying_accessions(sstore.read("insider_subs"), sstore.read("insider_owners"),
                                    sstore.read("insider_trans"), cmap)
    ev = sev.insider_events(acc, cal)
    out = []
    for kind, name in (("ins_all", "V-INS-all"), ("ins_cxo", "V-INS-cxo")):
        e = ev[kind]
        e = e[e["ticker"].isin(tickers)]
        pos = cal.get_indexer(pd.to_datetime(e["date"])) - 1
        evf = _ev_frame(e["ticker"].to_numpy()[pos >= 0], pos[pos >= 0], cal)
        names = sorted(set(evf["asset"]))
        out.append(DailyCell(name, cal, _curves_for(cl[names], {t: hc[t] for t in names}), evf,
                             {t: np.ones(len(cal), bool) for t in names}, _year_key(cal, names)))
    echo(f"V-INS: {', '.join(f'{c.name} {len(c.events)}' for c in out)}")
    return out


def _peer_hedges(cl: pd.DataFrame, groups: dict[str, str]) -> dict[str, pd.Series | None]:
    """Leave-one-out equal-weighted group index (ADR 0005), as a price-like series."""
    r = cl.pct_change(fill_method=None)
    out: dict[str, pd.Series | None] = {}
    for g in sorted(set(groups.values())):
        mem = [t for t, gg in groups.items() if gg == g and t in cl.columns]
        rr = r[mem]
        tot, cnt = rr.sum(axis=1, min_count=1), rr.notna().sum(axis=1)
        for t in mem:
            peer = (tot - rr[t].fillna(0)) / (cnt - rr[t].notna().astype(int)).where(lambda x: x >= 1)
            out[t] = (1 + peer.fillna(0)).cumprod()
    return out


def _gdelt_a_cells(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, echo):
    assets = store.read_assets("A")
    groups = evs.outcome_groups(assets, "A")
    grouped = [a for a in assets if a.ticker in groups]
    tickers = sorted(groups)
    px = store.read_prices(tickers + [iu.HEDGE_MARKET], cfg.start, cfg.holdout_start)
    tdays = pl._trading_days(px[px["ticker"].isin(tickers)])
    daily = pl._read_event_gdelt(store, "A", grouped, cfg.start, cfg.holdout_start)
    if daily.empty:
        echo("missing GDELT daily rows (Track A)")
        return None
    hold = pd.Timestamp(cfg.holdout_start)
    scores = pl._before(evs.shock_scores(daily, tdays, grouped, "A", WINDOW), hold)
    px["date"] = pd.to_datetime(px["date"])
    cal = pd.DatetimeIndex(pd.to_datetime(tdays))
    cl = px.pivot(index="date", columns="ticker", values="adj_close").reindex(cal)
    peer = _peer_hedges(cl[tickers], groups)
    spy = cl[iu.HEDGE_MARKET] if iu.HEDGE_MARKET in cl.columns else None
    out = []
    specs = (("V-GD-conf-dm", CONFLICT, "dm", peer), ("V-GD-conf-em", CONFLICT, "em", peer),
             ("V-GD-coop-dm", COOP, "dm", peer), ("V-GD-coop-em", COOP, "em", peer),
             ("V-GD-comm", PRODUCER, "commodity", None))
    overlap = {}
    evsets = {}
    for name, sigs, grp, hedge in specs:
        mem = [t for t, g in groups.items() if g == grp and t in cl.columns]
        e = evs.composite_events(scores, mem, sigs, THETA, DECLUSTER, tdays)
        evsets[name] = set(zip(e["date"], e["ticker"], strict=True))
        pos = cal.get_indexer(pd.to_datetime(e["date"])) - 1
        evf = _ev_frame(e["ticker"].to_numpy()[pos >= 0], pos[pos >= 0], cal)
        names = sorted(set(evf["asset"]))
        hd = {t: (hedge[t] if hedge is not None else None) for t in names}
        c = DailyCell(name, cal, _curves_for(cl[names], hd), evf,
                      {t: np.ones(len(cal), bool) for t in names}, _year_key(cal, names))
        if hedge is None and spy is not None:   # amendment 5: commodity also SPY-beta-hedged
            c.note["spy_hedged_variant"] = True
            out.append(DailyCell(name + "-spy", cal, _curves_for(cl[names], {t: spy for t in names}),
                                 evf, c.pool_ok, c.key))
        out.append(c)
    for g in ("dm", "em"):
        overlap[g] = len(evsets[f"V-GD-conf-{g}"] & evsets[f"V-GD-coop-{g}"])
    echo(f"V-GD (A): {', '.join(f'{c.name} {len(c.events)}' for c in out)}; conflict/coop"
         f" same-day overlap {overlap}")
    for c in out:
        c.note["conf_coop_overlap"] = overlap
    return out


def _gdelt_b_cells(cfg, store, sstore, istore, dstore, dodcap, map_rows, exclusions, echo):
    assets = store.read_assets("B")
    groups = evs.outcome_groups(assets, "B")
    grouped = [a for a in assets if a.ticker in groups]
    tickers = sorted(groups)
    hedges = sorted(set(iu.SECTOR_SPDR.values()) | {iu.HEDGE_MARKET})
    px = store.read_prices(tickers + hedges, cfg.start, cfg.holdout_start)
    tdays = pl._trading_days(px[px["ticker"].isin(tickers)])
    daily = pl._read_event_gdelt(store, "B", grouped, cfg.start, cfg.holdout_start)
    if daily.empty:
        echo("missing GDELT GKG rows (Track B)")
        return None
    hold = pd.Timestamp(cfg.holdout_start)
    scores = pl._before(evs.shock_scores(daily, tdays, grouped, "B", WINDOW), hold)
    px["date"] = pd.to_datetime(px["date"])
    cal = pd.DatetimeIndex(pd.to_datetime(tdays))
    cl = px.pivot(index="date", columns="ticker", values="adj_close").reindex(cal)
    amap = {a.ticker: a for a in assets}
    out = []
    for cid, name in (("E4", "V-GD-att-neg"), ("E4p", "V-GD-att-pos")):
        cell = next(c for c in evs.CELLS if c.hypothesis == cid)
        base = evs.cell_scores(scores, dataclasses.replace(cell, split=None))
        e = pl._cell_events(base, scores, cell, THETA, tdays)
        pos = cal.get_indexer(pd.to_datetime(e["date"]))   # base close(t) (amendment 6)
        e = e[(pos >= 0) & e["ticker"].isin(cl.columns)]
        pos = cal.get_indexer(pd.to_datetime(e["date"]))
        evf = _ev_frame(e["ticker"], pos, cal)
        names = sorted(set(evf["asset"]))
        hd = {t: _sector_hedge(cl, t, amap, iu.HEDGE_MARKET) for t in names}
        out.append(DailyCell(name, cal, _curves_for(cl[names], hd), evf,
                             {t: np.ones(len(cal), bool) for t in names}, _year_key(cal, names)))
    echo(f"V-GD (B): {', '.join(f'{c.name} {len(c.events)}' for c in out)}")
    return out


# --- intraday SPY ------------------------------------------------------------------------------

def _intraday_cells(cfg, store, istore, n_boot, seed, echo) -> dict[str, dict[str, dict]]:
    daily = store.read_prices([iu.HEDGE_MARKET], date(2016, 1, 4), cfg.holdout_start)
    days = np.array(sorted({pd.Timestamp(x).date() for x in daily["date"]}), dtype=object)
    days = days[days < cfg.holdout_start]
    hold = int(datetime(cfg.holdout_start.year, cfg.holdout_start.month, cfg.holdout_start.day,
                        tzinfo=UTC).timestamp())
    m = st.bar_matrix(istore.read_bars([iu.HEDGE_MARKET], None, hold), list(days))
    early, bad = mc.session_flags(m, list(days), mcal.NYSE_EARLY_CLOSES)
    drop = early | bad
    close = mc.last_close(m)
    prev = np.concatenate([[np.nan], close[:-1]])
    es = mc.event_sets(list(days), drop, mcal.FOMC_SCHEDULED, mcal.FOMC_CANCELLED,
                       [d for d, _ in mcal.FOMC_UNSCHEDULED], mcal.CPI_RELEASES,
                       mcal.NFP_RELEASES, mcal.FOMC_PRESS_CONFERENCE)
    key = np.array([d.year * 7 + d.weekday() for d in days])

    def pts(points: Any, base: np.ndarray) -> np.ndarray:
        cols = []
        for _, sp in points:
            v = m.open[:, 0] if sp == "open0" else close if sp == "last" else m.close[:, sp]
            cols.append(v / base - 1)
        x = np.column_stack(cols)
        x[drop] = np.nan
        return x

    out: dict[str, dict[str, dict]] = {}
    for name, points, x, evp in (("V-CPI-intra", CPI_POINTS, pts(CPI_POINTS, prev), es.events["M4"]),
                                 ("V-FOMC-intra", FOMC_POINTS,
                                  pts(FOMC_POINTS, m.close[:, mc.K_1345]), es.events["M5"])):
        labels = [lab for lab, _ in points]
        out[name] = {}
        for p, (a, b) in PERIODS.items():
            mask = (days >= a) & (days <= b)
            ev = np.array([i for i in evp if mask[i] and np.isfinite(x[i]).all()])
            pool = np.array([i for i in np.union1d(es.pool, ev) if mask[i]
                             and np.isfinite(x[i]).all()])
            note: dict[str, Any] = {"adr": "0017", "n_events": len(ev), "n_dates": len(ev),
                                    "intraday": True}
            if len(ev) < MIN_EVENTS:
                note.update(insufficient=True, summary=f"insufficient: {len(ev)} events")
                out[name][p] = note
                continue
            curves: dict[str, list] = {t: [] for t in ("EV", "EV_lo", "EV_hi", "S", "S_lo",
                                                       "S_hi", "C", "C_lo", "C_hi", "n")}
            dts = [days[i] for i in ev]
            s1 = np.sign(x[:, 0])
            for j in range(len(labels)):
                cont = s1 * (x[:, j] - x[:, 0])
                for tag, v in (("EV", x[:, j] ** 2), ("S", x[:, j]), ("C", cont)):
                    pm = pd.Series(v[pool]).groupby(key[pool]).mean()
                    base_pm = np.array([pm.get(key[i], np.nan) for i in ev])
                    if tag == "EV":
                        xe = v[ev] / base_pm - 1
                    else:
                        xe = v[ev] - base_pm
                    lo, hi = rc.block_ci(dts, xe, n_boot, seed, LEVEL_INTRA)
                    curves[tag].append(_num(float(np.nanmean(xe))))
                    curves[f"{tag}_lo"].append(_num(lo))
                    curves[f"{tag}_hi"].append(_num(hi))
                curves["n"].append(len(ev))
            r = mc.placebo_test(x[:, -1] ** 2, ev, np.setdiff1d(pool, ev),
                                np.array([d.weekday() for d in days]),
                                np.array([d.year for d in days]), n_boot, seed, 1)
            note.update(curves=curves, ks=labels, perm_p_var_close=_num(r.p))
            note["summary"] = _summary(note, labels)
            out[name][p] = note
    return out


def _f(x: Any, d: int = 2) -> str:
    return "nan" if x is None or not isinstance(x, (int, float)) or not math.isfinite(x) \
        else f"{x:.{d}f}"
