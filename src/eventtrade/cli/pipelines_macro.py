"""ADR 0014 scheduled macro announcements on SPY (hypothesis family M, with the review
amendments): once-only study. Never names a concrete adapter."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_study as st
from eventtrade.core import macro as mc
from eventtrade.core import macro_calendar as mcal
from eventtrade.core.evaluate import newey_west_t
from eventtrade.core.screen import benjamini_hochberg, holm

HYPOTHESIS = "M"
N_BOOT = 10_000
N_SHIFT = 1_000
BH_Q = 0.10
HOLM_ALPHA = 0.05
FIRST_DAY = date(2016, 1, 4)
DISCOVERY = (date(2016, 1, 4), date(2019, 12, 31))
CONFIRMATION = (date(2020, 1, 2), date(2025, 3, 31))
COVID = (date(2020, 2, 20), date(2020, 4, 30))
VOL_2018 = ((date(2018, 2, 1), date(2018, 2, 28)), (date(2018, 10, 1), date(2018, 12, 31)))
SIGN = {"M1": 1, "M2": 1, "M3": 1, "M4": 1, "M5": 0}


def macro_study(cfg: Config, store: Any, istore: Any, now: Clock, echo: Echo = print,
                n_boot: int = N_BOOT, n_shift: int = N_SHIFT) -> int:
    if CONFIRMATION[1] >= cfg.holdout_start:
        echo("refused: confirmation reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0014 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            n = json.loads(t.notes)
            echo(f"  {t.params.get('period')} {t.params.get('cell')}: mean {n.get('mean')}"
                 f" p {n.get('p')} {n.get('verdict', '')}")
        return OK
    daily = store.read_prices([iu.HEDGE_MARKET], FIRST_DAY, cfg.holdout_start)
    days = np.array(sorted({pd.Timestamp(x).date() for x in daily["date"]}), dtype=object)
    days = days[(days >= FIRST_DAY) & (days < cfg.holdout_start)]
    hold = int(datetime(cfg.holdout_start.year, cfg.holdout_start.month, cfg.holdout_start.day,
                        tzinfo=UTC).timestamp())
    spy = istore.read_bars([iu.HEDGE_MARKET], None, hold)
    if spy.empty or not len(days):
        echo("need SPY daily prices and SPY intraday bars (`ingest-prices`, `ingest-intraday`)")
        return MISSING
    m = st.bar_matrix(spy, list(days))
    early, bad = mc.session_flags(m, list(days), mcal.NYSE_EARLY_CLOSES)
    drop = early | bad
    vals = mc.day_values(m, drop)
    close = mc.last_close(m)
    close[bad] = np.nan
    ret = close / np.concatenate([[np.nan], close[:-1]]) - 1   # M2: close to close, early kept
    es = mc.event_sets(list(days), drop, mcal.FOMC_SCHEDULED, mcal.FOMC_CANCELLED,
                       [d for d, _ in mcal.FOMC_UNSCHEDULED], mcal.CPI_RELEASES,
                       mcal.NFP_RELEASES, mcal.FOMC_PRESS_CONFERENCE)
    n = len(days)
    weekday = np.array([d.weekday() for d in days])
    year = np.array([d.year for d in days])
    echo(f"calendar: {n} SPY trading days (daily prices), {int(early.sum())} early closes,"
         f" {int(bad.sum())} dropped sessions {[str(days[i]) for i in np.flatnonzero(bad)][:20]}")
    echo(f"events: scheduled FOMC held {len(es.fomc)} (press conference {len(es.press)}),"
         f" overlap with 08:30 releases {[str(days[i]) for i in es.overlap]};"
         f" M1/M5 {len(es.events['M1'])}, M3 {len(es.events['M3'])}, M4 {len(es.events['M4'])};"
         f" placebo pool {len(es.pool)} days")
    cday = mc.cycle_day(n, es.fomc)
    even = mc.even_week(cday)

    def in_span(span: tuple[date, date]) -> np.ndarray:
        return (days >= span[0]) & (days <= span[1])

    covid = in_span(COVID)
    vol18 = in_span(VOL_2018[0]) | in_span(VOL_2018[1])
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for pname, span in (("discovery", DISCOVERY), ("confirmation", CONFIRMATION)):
        mask = in_span(span)
        for cell in mc.CELLS:
            disc = results.get((cell, "discovery"))
            if pname == "confirmation" and not (disc and disc.get("discovered")):
                continue
            sgn = SIGN[cell]
            if cell == "M5" and pname == "confirmation":
                sgn = 1 if (disc["mean"] or 0) > 0 else -1   # amendment 9
            note: dict[str, Any] = {"adr": "0014"}
            if cell == "M2":
                note.update(_m2(ret, even, mask, covid, vol18, es.fomc, n_shift, cfg.seed))
            else:
                note.update(_placebo_cell(cell, sgn, vals[cell], es, mask, covid, weekday, year,
                                          n_boot, cfg.seed))
            if pname == "confirmation" and cell == "M5":
                note["direction"] = sgn
            results[(cell, pname)] = note
            echo(f"  {pname:<12} {cell} n {note['n_events']:>4} mean {_f(note['mean'], 5)}"
                 f" t {_f(note['t'])} p {_f(note['p'], 4)} net {_f(note['net_directional'], 5)}")
        if pname == "discovery":
            pv = {c: _p(results[(c, pname)]["p"]) for c in mc.CELLS}
            found = benjamini_hochberg(pv, BH_Q)
            for c in mc.CELLS:
                results[(c, pname)]["discovered"] = c in found
            echo(f"discovery: {len(found)} of {len(pv)} cells discovered (BH q = {BH_Q})")

    conf = {c: v for (c, pn), v in results.items() if pn == "confirmation"}
    hp = holm({c: _p(v["p"]) for c, v in conf.items()}, HOLM_ALPHA)
    for c, v in conf.items():
        d = SIGN[c] or v["direction"]
        ok = (c in hp and d * (v["mean"] or 0) > 0 and (v["net_directional"] or -1) > 0
              and d * (v.get("mean_ex_covid") or 0) > 0)
        v["holm"] = c in hp
        v["verdict"] = "CONFIRMED" if ok else "not confirmed"
        echo(f"  confirmation {c}: {v['verdict']} (net {v['net_directional']})")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for (cell, pname), note in results.items():
        span = DISCOVERY if pname == "discovery" else CONFIRMATION
        params = {"adr": "0014", "cell": cell, "period": pname, "n_boot": n_boot,
                  "seed": cfg.seed,
                  "null": "nw_slope" if cell == "M2" else "placebo_same_year_weekday"}
        _log_trial(store, logged, "A", "signal", HYPOTHESIS, params, *span, now, note)
    echo(f"{len(logged) - n0} trial(s) logged")
    return OK


def _placebo_cell(cell: str, sgn: int, v: np.ndarray, es: mc.EventSets, mask: np.ndarray,
                  covid: np.ndarray, weekday: np.ndarray, year: np.ndarray, n_boot: int,
                  seed: int) -> dict[str, Any]:
    ev = es.events[cell]
    ev = ev[mask[ev]]
    pool = es.pool[mask[es.pool]]
    r = mc.placebo_test(v, ev, pool, weekday, year, n_boot, seed, sgn)
    d = sgn if sgn != 0 else (1 if r.mean > 0 else -1)
    x = v[ev]
    x = x[np.isfinite(x)]
    note: dict[str, Any] = {
        "mean": _num(r.mean), "t": _num(_t(x)), "p": _num(r.p), "p_up": _num(r.p_up),
        "p_down": _num(r.p_down), "placebo_mean": _num(r.placebo_mean), "n_events": r.n,
        "hit_rate": _num(float(np.mean(np.sign(x) == d)) if len(x) else None),
        "net_directional": _num(d * (r.mean - r.placebo_mean) - 2 * mc.COST_SIDE),  # amendment 8
    }
    keep = ev[~covid[ev]]
    xc = v[keep]
    xc = xc[np.isfinite(xc)]
    note["mean_ex_covid"] = _num(float(xc.mean()) if len(xc) else None)
    note["t_ex_covid"] = _num(_t(xc))
    if cell == "M5":
        xp = v[np.intersect1d(ev, es.press)]
        xp = xp[np.isfinite(xp)]
        note["press_mean"] = _num(float(xp.mean()) if len(xp) else None)
        note["press_n"] = len(xp)
    if cell == "M3":
        nf = np.setdiff1d(ev, es.fomc)
        r2 = mc.placebo_test(v, nf, pool, weekday, year, n_boot, seed, sgn)
        note["no_fomc_mean"] = _num(r2.mean)
        note["no_fomc_p"] = _num(r2.p)
    return note


def _m2(ret: np.ndarray, even: np.ndarray, mask: np.ndarray, covid: np.ndarray,
        vol18: np.ndarray, fomc: np.ndarray, n_shift: int, seed: int) -> dict[str, Any]:
    y = np.where(mask, ret, np.nan)
    b, t = mc.nw_slope(y, even)
    ev = mask & (even == 1) & np.isfinite(ret)
    n_even = int(ev.sum())
    cost = 2 * mc.COST_SIDE * mc.blocks(np.where(mask, even, 0)) / max(n_even, 1)
    bx, tx = mc.nw_slope(np.where(mask & ~covid, ret, np.nan), even)
    b18, _ = mc.nw_slope(np.where(mask & ~vol18, ret, np.nan), even)
    return {"mean": _num(b), "t": _num(t), "p": _num(float(norm.sf(t)) if math.isfinite(t) else None),
            "n_events": n_even,
            "mean_even": _num(float(ret[ev].mean()) if n_even else None),
            "mean_other": _num(float(np.nanmean(np.where(mask & (even == 0), ret, np.nan)))),
            "net_directional": _num(b - cost), "block_cost_per_day": _num(cost),
            "mean_ex_covid": _num(bx), "t_ex_covid": _num(tx), "b_ex_2018_vol": _num(b18),
            "p_shift_placebo": _num(mc.shift_p(y, fomc, b, n_shift, seed))}


def _t(x: np.ndarray) -> float:
    return float(newey_west_t(x, lags=0)) if len(x) > 1 else float("nan")


def _p(p: float | None) -> float:
    return p if p is not None else math.nan


def _f(x: float | None, d: int = 2) -> str:
    return "nan" if x is None or not math.isfinite(x) else f"{x:.{d}f}"
