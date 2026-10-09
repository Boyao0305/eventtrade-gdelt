"""ADR 0009 value of foresight: (1) foresight returns [t_a - Delta, t_a) of fresh events at the
registered configuration (L = 15, W = 60, theta = 3) for 24 (signal, group) pairs x 7 leads;
(2) the reverse event study: do the categories follow 15-minute price shocks?

Look-ahead by design: every trial carries `params.lookahead = true` (excluded from DSR's N, never
backtested). Once-only on hypothesis prefix "O"/"R". Trials are logged after everything is
computed. n_boot fixed at 2,000.
"""

from __future__ import annotations

import json
import math
import time as _time
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.cli.pipelines_intraday_search import (
    ALL_SLOTS,
    CAT_COLUMNS,
    _leg,
)
from eventtrade.cli.pipelines_intraday_study import (
    CONFIRMATION,
    COST_BP,
    DISCOVERY,
    _epoch,
    _xlc_or_spy,
    study_calendar,
)
from eventtrade.config import Config
from eventtrade.core import foresight as fs
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_search as se
from eventtrade.core import intraday_study as st
from eventtrade.core.screen import benjamini_hochberg, holm

N_BOOT = 2000
BH_Q = 0.10
HOLM_ALPHA = 0.05
L_MIN, W_MIN, THETA = 15, 60, 3.0
INTRADAY_LEADS = (15, 30, 60, 120)
DAILY_LEADS = (1, 2, 5)
FAMILIES = {"quad1": (["q1"], 1), "quad2": (["q2"], 1), "quad3": (["q3"], -1),
            "quad4": (["q4"], -1), "r14": (["r14"], -1), "r17": (["r17"], -1),
            "violence": (["r18", "r19", "r20"], -1), "b163": (["b163"], -1)}
PRODUCER = {"producer_quad4": ["q4"], "producer_violence": ["r18", "r19", "r20"]}
EXOGENOUS_PRIOR = {"r14", "r17", "violence", "quad4", "producer_quad4", "producer_violence"}


def pairs() -> list[tuple[str, str, int]]:
    """(signal, group, sign): 20 country pairs, 2 producer pairs, 2 company pairs = 24."""
    out = []
    for sig, (_, s) in FAMILIES.items():
        out += [(sig, "dm", s), (sig, "em", s)]
    out += [("tone", "dm", -1), ("tone", "em", -1), ("attention", "dm", 0), ("attention", "em", 0)]
    out += [(p, "commodity", 1) for p in PRODUCER]
    out += [("spike_neg", "stocks", -1), ("spike_pos", "stocks", 1)]
    return out


def leads() -> list[str]:
    return [f"{m}m" for m in INTRADAY_LEADS] + [f"{d}d" for d in DAILY_LEADS]


def foresight(cfg: Config, store: Any, istore: Any, now: Clock, echo: Echo = print,
              n_boot: int = N_BOOT, discovery: tuple[date, date] = DISCOVERY,
              confirmation: tuple[date, date] = CONFIRMATION) -> int:
    if confirmation[1] >= cfg.holdout_start or discovery[0] < cfg.start:
        echo("refused: study periods must lie inside the dev period (ADR 0001)")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis in ("O", "R")]
    if prior:
        echo(f"ADR 0009 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            n = json.loads(t.notes)
            if t.params.get("period") == "confirmation" or n.get("discovered"):
                echo(f"  {t.hypothesis} {t.params.get('period')} {t.params.get('cell')}:"
                     f" t {n.get('t')} p {n.get('p')} {n.get('verdict', '')}")
        return OK
    t0 = _time.time()
    hold_ts = _epoch(cfg.holdout_start)
    cal = study_calendar(cfg, store, istore, discovery[0])
    if isinstance(cal, str):
        echo(cal)
        return MISSING
    days, nodec = cal
    nd = len(days)
    decision = ~nodec
    utc_slot = st.tau_utc_slots(days)

    def pmask(p):
        return np.array([p[0] <= d <= p[1] for d in days])

    disc_mask, conf_mask = pmask(discovery), pmask(confirmation)
    assets = store.read_assets(None)
    by_ticker = {a.ticker: a for a in assets}

    # --- prices (search panel: open, forward-filled close, last close) --------------------------
    hedge_mats = {}
    for h in iu.HEDGES:
        hb = istore.read_bars([h], None, hold_ts)
        if hb.empty:
            echo(f"no intraday bars for hedge {h}: run `ingest-intraday`")
            return MISSING
        hedge_mats[h] = st.bar_matrix(hb, days)
    hedge_mats["XLC_OR_SPY"] = _xlc_or_spy(hedge_mats["XLC"], hedge_mats[iu.HEDGE_MARKET])
    rows = [(a.ticker, "A", {"dm": iu.HEDGE_DM, "em": iu.HEDGE_EM}.get(a.screen_group or "", ""))
            for a in iu.track_a_assets(assets)]
    for a in iu.track_b_assets(assets):
        hk = iu.SECTOR_SPDR[a.screen_group or ""]
        rows.append((a.ticker, "B", "XLC_OR_SPY" if hk == "XLC" else hk))
    dmask = disc_mask & decision
    liquid, spread = [], {}
    for t, tr, hk in rows:
        b = istore.read_bars([t], None, hold_ts)
        m = st.bar_matrix(b, days)
        if len(b) and st.median_trades(m, dmask) >= st.LIQUIDITY_MIN_TRADES:
            liquid.append((t, tr, hk))
            spread[t] = st.abdi_ranaldo_spread(m, dmask)
    hedge_keys = sorted({h for _, _, h in liquid if h})
    for hk in hedge_keys:
        spread[hk] = st.abdi_ranaldo_spread(hedge_mats[hk], dmask)
    index = {t: i for i, (t, _, _) in enumerate(liquid)}
    hidx = {hk: len(liquid) + i for i, hk in enumerate(hedge_keys)}
    hedge_of = [hidx[hk] if hk else -1 for _, _, hk in liquid] + [-1] * len(hedge_keys)
    panel = se.empty_search_panel(len(liquid) + len(hedge_keys), nd, hedge_of)
    for t, i in index.items():
        se.fill_search_row(panel, i, st.bar_matrix(istore.read_bars([t], None, hold_ts), days))
    for hk, i in hidx.items():
        se.fill_search_row(panel, i, hedge_mats[hk])
    rt = np.zeros(len(hedge_of))
    for t, tr, hk in liquid:
        rt[index[t]] = 2 * _leg(spread.get(t), COST_BP["stock" if tr == "B" else "etf"]) + (
            2 * _leg(spread.get(hk), COST_BP["hedge"]) if hk else 0.0)
    # ADR 0007 eligibility: the entry bar [tau-15, tau) traded for asset and hedge
    elig = np.isfinite(panel.open_[:, :, :st.N_TAU])
    for i, h in enumerate(hedge_of):
        if h >= 0:
            elig[i] &= np.isfinite(panel.open_[h, :, :st.N_TAU])
    elig &= decision[None, :, None]
    group_of = {t: ("stocks" if tr == "B" else by_ticker[t].screen_group) for t, tr, _ in liquid}
    echo(f"prices: {len(liquid)} liquid assets, {nd} days [{_time.time() - t0:.0f}s]")

    # --- category z (full slot grid), fires, events ----------------------------------------------
    cat = istore.read_gdelt("events_15min_cat", None, hold_ts)
    org = istore.read_gdelt("orgs_15min", None, hold_ts)
    if cat.empty or org.empty:
        echo("need `ingest-gdelt-15min events_cat` and `orgs`")
        return MISSING
    sig_data = _signal_grids(cat, org, liquid, by_ticker, group_of, days)
    events: dict[tuple[str, str], pd.DataFrame] = {}
    n_fresh: dict[tuple[str, str], tuple[int, int]] = {}
    for (sig, grp, _s) in pairs():
        parts, tot, kept = [], 0, 0
        for t, (z, fire) in sig_data.get(sig, {}).items():
            if group_of[t] != grp:
                continue
            i = index[t]
            f = st.at_tau(fire, utc_slot) & elig[i]
            first = st.first_firing(f)
            d = np.flatnonzero(first >= 0)
            if not len(d):
                continue
            j = first[d]
            ok = fs.fresh(z, d, utc_slot[d, j])
            tot, kept = tot + len(d), kept + int(ok.sum())
            parts.append(pd.DataFrame({"day": d[ok], "asset": i, "j": j[ok]}))
        events[(sig, grp)] = (pd.concat(parts, ignore_index=True) if parts else
                              pd.DataFrame({"day": [], "asset": [], "j": []}, dtype=int))
        n_fresh[(sig, grp)] = (tot, kept)
    echo(f"fresh events built [{_time.time() - t0:.0f}s]")

    # --- foresight cells -------------------------------------------------------------------------
    years = np.array([d.year for d in days])
    pending: list[tuple[str, str, str, tuple[date, date], dict[str, Any]]] = []
    res: dict[str, dict[str, Any]] = {}
    for period, mask, span in (("discovery", disc_mask, discovery),
                               ("confirmation", conf_mask, confirmation)):
        pool = np.flatnonzero(mask & decision)
        for (sig, grp, s) in pairs():
            ev = events[(sig, grp)]
            ev = ev[mask[ev["day"].to_numpy(int)]] if len(ev) else ev
            for lead in leads():
                key = f"{sig}|{grp}|{lead}"
                if period == "confirmation" and key not in res.get("discovered", {}):
                    continue
                fn = _foresight_fn(panel, lead, s)
                excl = (int(lead[:-1]) if lead.endswith("d") else 0) + 1
                direction = 1 if s != 0 else 0
                if period == "confirmation" and s == 0:
                    direction = res["discovered"][key]
                r = fs.foresight_test(ev["day"].to_numpy(int), ev["asset"].to_numpy(int), fn,
                                      ev["j"].to_numpy(int), pool, direction, excl, n_boot,
                                      cfg.seed)
                seg = _segments(panel, ev, s)
                rt_mean = float(np.mean(rt[ev["asset"].to_numpy(int)])) if len(ev) else float("nan")
                se_mean = abs(r.mean / r.t) if r.t and math.isfinite(r.t) and r.t != 0 else float("nan")
                sm = r.mean  # the value function already applies the registered sign
                upper = sm + 1.96 * se_mean
                note = {"mean_signed": _num(sm), "t": _num(r.t), "p": _num(r.p),
                        "n_events": r.n_events, "n_days": r.n_days, "rt": _num(rt_mean),
                        "upper95": _num(upper),
                        "no_value": bool(math.isfinite(upper) and upper < rt_mean),
                        "exogenous_prior": sig in EXOGENOUS_PRIOR, **seg,
                        "fresh_events_total": n_fresh[(sig, grp)], "adr": "0009"}
                res.setdefault(period, {})[key] = (r, note)
        if period == "discovery":
            pv = {k: v[0].p for k, v in res["discovery"].items()}
            disc_set = benjamini_hochberg(pv, BH_Q)
            res["discovered"] = {k: int(np.sign(res["discovery"][k][0].t)) or 1 for k in disc_set}
            echo(f"foresight discovery: {len(disc_set)} of {len(pv)} cells discovered"
                 f" [{_time.time() - t0:.0f}s]")
            _report(echo, res["discovery"], disc_set)
    holm_pass = holm({k: v[0].p for k, v in res.get("confirmation", {}).items()}, HOLM_ALPHA)
    for period, span in (("discovery", discovery), ("confirmation", confirmation)):
        for key, (r, note) in res.get(period, {}).items():
            sig, grp, lead = key.split("|")
            if period == "discovery":
                note["discovered"] = key in res["discovered"]
            else:
                note["holm"] = key in holm_pass
                note["verdict"] = "CONFIRMED" if key in holm_pass else "not confirmed"
                echo(f"  confirmation {key}: signed mean {note['mean_signed']} t {note['t']}"
                     f" p {note['p']} -> {note['verdict']}")
            pending.append(("O", key, period, span, note))

    # --- reverse event study --------------------------------------------------------------------
    rev = _reverse(sig_data, liquid, index, group_of, panel, days, utc_slot, decision,
                   disc_mask, conf_mask, years, n_boot, cfg.seed)
    disc_rev = benjamini_hochberg({k: (math.nan if v["p"] is None else v["p"])
                                   for k, v in rev["discovery"].items()}, BH_Q)
    for period, span in (("discovery", discovery), ("confirmation", confirmation)):
        for key, note in rev[period].items():
            note = {**note, "follows_price": key in disc_rev if period == "discovery" else None,
                    "adr": "0009"}
            pending.append(("R", key, period, span, note))
    echo("reverse event study (does the category follow 15-minute price shocks?):")
    for key, note in rev["discovery"].items():
        echo(f"  {key:<28} shocks {note['n_events']:>6} rate {_f(note['mean'], 4)}"
             f" t {_f(note['t'])} p {_f(note['p'], 4)}"
             f" {'FOLLOWS PRICE' if key in disc_rev else 'exogenous (not significant)'}")

    logged = {tr.trial_id for tr in store.read_trials()}
    n0 = len(logged)
    for hyp, key, period, span, note in pending:
        sig = key.split("|")[0]
        track = "B" if sig.startswith("spike") else "A"
        params = {"adr": "0009", "cell": key, "period": period, "n_boot": n_boot,
                  "seed": cfg.seed, "lookahead": True}
        _log_trial(store, logged, track, "signal", hyp, params, *span, now, note)
    echo(f"{len(logged) - n0} trial(s) logged (all lookahead) [{_time.time() - t0:.0f}s]")
    return OK


def _signal_grids(cat, org, liquid, by_ticker, group_of, days):
    """{signal: {ticker: (z [nd, 96], fire [nd, 96])}} at L = 15, W = 60, theta = 3."""
    out: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
    by = {(e, g): x for (e, g), x in cat.groupby(["entity", "geo"])}
    glob = st.slot_matrix(by.get(("_GLOBAL", "action"), cat.iloc[0:0]), "n_events", days)
    valid = se.window_valid_lw(st.missing_batches(glob, ALL_SLOTS), L_MIN, W_MIN)
    G = se.window_sums_lw(glob, L_MIN, W_MIN)

    def wsum(mats, cols):
        return sum(se.window_sums_lw(mats[c], L_MIN, W_MIN) for c in cols)

    for t, tr, _ in liquid:
        if tr != "A":
            continue
        countries = tuple(by_ticker[t].fips_countries or [])
        acc = {c: np.zeros((len(days), st.N_SLOTS)) for c in CAT_COLUMNS}
        for ctry in countries:
            g = by.get((ctry, "action"))
            if g is not None:
                for c in CAT_COLUMNS:
                    acc[c] += st.slot_matrix(g, c, days)
        E = se.window_sums_lw(acc["n_events"], L_MIN, W_MIN)
        act = E >= 10
        specs: dict[str, tuple] = {}
        with np.errstate(all="ignore"):
            if group_of[t] == "commodity":
                for sig, cols in PRODUCER.items():
                    specs[sig] = (wsum(acc, cols) / E, E, "share", 1)
            else:
                for sig, (cols, _) in FAMILIES.items():
                    specs[sig] = (wsum(acc, cols) / E, E, "share", 1)
                ar = se.window_sums_lw(acc["n_articles"], L_MIN, W_MIN)
                specs["tone"] = (se.window_sums_lw(acc["tone_x_articles"], L_MIN, W_MIN) / ar,
                                 ar, "tone", -1)
                specs["attention"] = (E / G, G, "share", 1)
        for sig, (x, den, kind, side) in specs.items():
            z = se.baseline_z_fast(x, den, valid, kind)
            zs = side * z
            out.setdefault(sig, {})[t] = (zs, (zs >= THETA) & act)
    byo = {e: x for e, x in org.groupby("entity")}
    gd = st.slot_matrix(byo.get("_GLOBAL", org.iloc[0:0]), "docs", days)
    validb = se.window_valid_lw(st.missing_batches(gd, ALL_SLOTS), L_MIN, W_MIN)
    GD = se.window_sums_lw(gd, L_MIN, W_MIN)
    for t, tr, _ in liquid:
        if tr != "B" or t not in byo:
            continue
        docs = st.slot_matrix(byo[t], "docs", days)
        tone = st.slot_matrix(byo[t], "tone_sum", days)
        D, TS = se.window_sums_lw(docs, L_MIN, W_MIN), se.window_sums_lw(tone, L_MIN, W_MIN)
        with np.errstate(all="ignore"):
            z = se.baseline_z_fast(D / GD, GD, validb, "share")
            fire = (z >= THETA) & (D >= 5)
            cd = np.concatenate([[0], np.cumsum(docs.sum(axis=1))])
            ct = np.concatenate([[0], np.cumsum(tone.sum(axis=1))])
            idx = np.arange(len(days))
            lo = np.maximum(idx - st.BASELINE_DAYS, 0)
            bdocs = (cd[idx] - cd[lo])[:, None]
            bmean = ((ct[idx] - ct[lo])[:, None]) / np.where(bdocs > 0, bdocs, np.nan)
            wmean = TS / D
        ok = bdocs >= st.TONE_SPLIT_MIN_DOCS
        out.setdefault("spike_neg", {})[t] = (z, fire & ok & (wmean < bmean))
        out.setdefault("spike_pos", {})[t] = (z, fire & ok & (wmean >= bmean))
    return out


def _foresight_fn(panel: se.SearchPanel, lead: str, sign: int):
    s = sign if sign != 0 else 1

    def fn(d, a, j):
        t_a = 585 + 15 * np.asarray(j) - (L_MIN + W_MIN - 15)  # first availability of the window
        if lead.endswith("m"):
            r = _abn_times(panel, a, d, t_a - int(lead[:-1]), t_a)
        else:
            r = _abn_from_close(panel, a, d, int(lead[:-1]), t_a)
        return s * r

    return fn


def _abn_times(panel, a, d, m0, m1):
    a = np.asarray(a, dtype=int)
    d = np.asarray(d, dtype=int)
    r = se.price_at(panel, a, d, m1) / se.price_at(panel, a, d, m0) - 1.0
    h = panel.hedge[a]
    has = h >= 0
    if has.any():
        hh, dd = h[has], d[has]
        r = r.copy()
        r[has] -= se.price_at(panel, hh, dd, m1[has]) / se.price_at(panel, hh, dd, m0[has]) - 1.0
    return r


def _abn_from_close(panel, a, d, k, m1):
    a = np.asarray(a, dtype=int)
    d = np.asarray(d, dtype=int)
    dk = d - k
    ok = dk >= 0
    r = np.full(len(a), np.nan)
    if not ok.any():
        return r
    aa, dd, kk, mm = a[ok], d[ok], dk[ok], np.asarray(m1)[ok]
    x = se.price_at(panel, aa, dd, mm) / panel.last_close[aa, kk] - 1.0
    h = panel.hedge[aa]
    has = h >= 0
    if has.any():
        x[has] -= (se.price_at(panel, h[has], dd[has], mm[has])
                   / panel.last_close[h[has], kk[has]] - 1.0)
    r[ok] = x
    return r


def _segments(panel, ev, sign):
    """Descriptive segments (never cells): publication [t_a, tau-15), latency [tau-15, tau),
    and the share of events whose t_a lies before the first bar's end (pre-open)."""
    if not len(ev):
        return {"pub_mean": None, "lat_mean": None, "pre_open_share": None}
    s = sign if sign != 0 else 1
    a, d, j = ev["asset"].to_numpy(int), ev["day"].to_numpy(int), ev["j"].to_numpy(int)
    tau = 585 + 15 * j
    t_a = tau - (L_MIN + W_MIN - 15)
    pub = s * _abn_times(panel, a, d, t_a, tau - 15)
    lat = s * _abn_times(panel, a, d, tau - 15, tau)
    return {"pub_mean": _num(float(np.nanmean(pub))) if np.isfinite(pub).any() else None,
            "lat_mean": _num(float(np.nanmean(lat))) if np.isfinite(lat).any() else None,
            "pre_open_share": _num(float(np.mean(t_a < 585)))}


def _reverse(sig_data, liquid, index, group_of, panel, days, utc_slot, decision, disc_mask,
             conf_mask, years, n_boot, seed):
    """Per (signal, group): shocks |abnormal 15-min return| > 3 sigma with no firing of the
    category in the previous 24 h; value = firing rate over the next 24 h; placebo p (+)."""
    out: dict[str, dict[str, dict[str, Any]]] = {"discovery": {}, "confirmation": {}}
    for (sig, grp, _s) in pairs():
        per = sig_data.get(sig, {})
        tick = [t for t in per if group_of[t] == grp]
        if not tick:
            continue
        rows = {index[t]: per[t] for t in tick}
        sd, sa, sk = [], [], []
        for t in tick:
            i = index[t]
            h = panel.hedge[i]
            r = fs.bar_returns(panel.close_ff[i], panel.close_ff[h] if h >= 0 else None)
            d, k = fs.shocks(r, decision)
            k = np.minimum(k, st.N_BARS - 1)
            s_end = fs.bar_end_slot(utc_slot, k, d)
            fire = per[t][1]
            # no firing in the previous 24 h (slots s_end-95 .. s_end)
            keep = np.array([not _fired_before(fire, dd, ss) for dd, ss in zip(d, s_end, strict=True)],
                            dtype=bool) if len(d) else np.zeros(0, bool)
            sd.append(d[keep])
            sa.append(np.full(int(keep.sum()), i))
            sk.append(k[keep])
        d_all = np.concatenate(sd) if sd else np.zeros(0, int)
        a_all = np.concatenate(sa) if sa else np.zeros(0, int)
        k_all = np.concatenate(sk) if sk else np.zeros(0, int)

        def value(d, a, k, rows=rows):
            out_v = np.full(len(d), np.nan)
            se_ = fs.bar_end_slot(utc_slot, k, d)
            for i, zf in rows.items():
                m = a == i
                if m.any():
                    fire = zf[1]
                    usable = np.isfinite(zf[0])
                    out_v[m] = fs.response_rate(fire, usable, d[m], se_[m])
            return out_v

        for period, mask in (("discovery", disc_mask), ("confirmation", conf_mask)):
            m = mask[d_all] if len(d_all) else np.zeros(0, bool)
            pool = np.flatnonzero(mask & decision)
            r = fs.foresight_test(d_all[m], a_all[m], value, k_all[m], pool, 1, 1, n_boot, seed)
            out[period][f"{sig}|{grp}"] = {"mean": _num(r.mean), "t": _num(r.t), "p": _num(r.p),
                                            "n_events": r.n_events, "n_days": r.n_days}
    return out


def _fired_before(fire: np.ndarray, d: int, s: int) -> bool:
    today = fire[d, max(s - 95, 0):s + 1]
    prev = fire[d - 1, s + 1:] if d >= 1 and s - 95 < 0 else np.zeros(0, bool)
    return bool(today.any() or prev.any())


def _report(echo, res, disc_set):
    echo(f"{'cell':<32}{'events':>7}{'days':>6}{'signed':>10}{'t':>7}{'p':>8}{'upper':>9}{'rt':>8}")
    for key, (r, n) in res.items():
        echo(f"{key:<32}{r.n_events:>7}{r.n_days:>6}{_f(n['mean_signed'], 5):>10}{_f(r.t):>7}"
             f"{_f(r.p, 4):>8}{_f(n['upper95'], 5):>9}{_f(n['rt'], 5):>8}"
             f"{'  disc' if key in disc_set else ''}")


def _f(x, d=2):
    return "nan" if x is None or not math.isfinite(x) else f"{x:.{d}f}"
