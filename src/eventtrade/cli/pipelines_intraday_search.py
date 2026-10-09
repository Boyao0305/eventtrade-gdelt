"""ADR 0008 intraday parameter search: 34 cells x 160 configurations (L, W, theta, exit), nested
selection on discovery with a block-placebo max-t, BH across cells, confirmation of the selected
configuration of each discovered cell.

Once-only: if any trial with hypothesis "J" exists, nothing is computed and stored verdicts are
printed. Trials are logged only after everything is computed. n_boot is fixed at 2,000.
"""

from __future__ import annotations

import json
import math
import time as _time
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.cli.pipelines_intraday_study import (
    CONFIRMATION,
    COST_BP,
    DISCOVERY,
    _epoch,
    _xlc_or_spy,
    study_calendar,
)
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_search as se
from eventtrade.core import intraday_study as st
from eventtrade.core.screen import benjamini_hochberg, holm

HYPOTHESIS = "J"
N_BOOT = 2000
BH_Q = 0.10
HOLM_ALPHA = 0.05
BETA_SAMPLE = 200_000
ALL_SLOTS = np.ones(st.N_SLOTS, dtype=bool)
A_FAMILIES = {  # signal -> (numerator columns, geo)
    "quad1": (["q1"], "action"), "quad2": (["q2"], "action"), "quad3": (["q3"], "action"),
    "quad4": (["q4"], "action"), "r14": (["r14"], "action"), "r16": (["r16"], "action"),
    "r17": (["r17"], "action"), "r18": (["r18"], "action"), "r19": (["r19"], "action"),
    "r20": (["r20"], "action"), "b163": (["b163"], "action"),
    "actor_conflict": (["q3", "q4"], "actor"),
}
PRODUCER = {"producer_quad3": ["q3"], "producer_quad4": ["q4"],
            "producer_violence": ["r18", "r19", "r20"]}
SIGNS = {"quad1": 1, "quad2": 1, "tone": -1, "attention": 0}
CAT_COLUMNS = ["n_events", "q1", "q2", "q3", "q4", "r14", "r16", "r17", "r18", "r19", "r20",
               "b163", "n_articles", "tone_x_articles"]


@dataclass(frozen=True)
class SearchCell:
    track: str
    signal: str
    group: str
    sign: int

    @property
    def key(self) -> str:
        return f"{self.track}|{self.signal}|{self.group}"


def cells() -> list[SearchCell]:
    out = []
    for sig in [*A_FAMILIES, "tone", "attention"]:
        for g in ("dm", "em"):
            out.append(SearchCell("A", sig, g, SIGNS.get(sig, -1)))
    for sig in PRODUCER:
        out.append(SearchCell("A", sig, "commodity", 1))
    out += [SearchCell("B", "spike_neg", "stocks", -1), SearchCell("B", "spike_pos", "stocks", 1),
            SearchCell("B", "spike_any", "stocks", 0)]
    return sorted(out, key=lambda c: c.key)


def intraday_search(cfg: Config, store: Any, istore: Any, now: Clock, echo: Echo = print,
                    n_boot: int = N_BOOT, discovery: tuple[date, date] = DISCOVERY,
                    confirmation: tuple[date, date] = CONFIRMATION,
                    only_cells: set[str] | None = None) -> int:
    if confirmation[1] >= cfg.holdout_start or discovery[0] < cfg.start:
        echo("refused: study periods must lie inside the dev period (ADR 0001)")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0008 search already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            if t.params.get("selected"):
                n = json.loads(t.notes)
                echo(f"  {t.params.get('period')} {t.params.get('cell')} {t.params.get('config')}:"
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

    def pmask(p: tuple[date, date]) -> np.ndarray:
        return np.array([p[0] <= d <= p[1] for d in days])

    disc_mask, conf_mask = pmask(discovery), pmask(confirmation)
    assets = store.read_assets(None)
    by_ticker = {a.ticker: a for a in assets}

    # --- prices --------------------------------------------------------------------------------
    hedge_mats: dict[str, st.BarMatrix] = {}
    for h in iu.HEDGES:
        hb = istore.read_bars([h], None, hold_ts)
        if hb.empty:
            echo(f"no intraday bars for hedge {h}: run `ingest-intraday`")
            return MISSING
        hedge_mats[h] = st.bar_matrix(hb, days)
    hedge_mats["XLC_OR_SPY"] = _xlc_or_spy(hedge_mats["XLC"], hedge_mats[iu.HEDGE_MARKET])
    rows: list[tuple[str, str, str]] = []
    for a in iu.track_a_assets(assets):
        rows.append((a.ticker, "A", {"dm": iu.HEDGE_DM, "em": iu.HEDGE_EM}.get(a.screen_group or "", "")))
    for a in iu.track_b_assets(assets):
        hk = iu.SECTOR_SPDR[a.screen_group or ""]
        rows.append((a.ticker, "B", "XLC_OR_SPY" if hk == "XLC" else hk))
    dmask = disc_mask & decision
    liquid, excluded, spread = [], [], {}
    for t, tr, hk in rows:
        b = istore.read_bars([t], None, hold_ts)
        m = st.bar_matrix(b, days)
        if not len(b) or st.median_trades(m, dmask) < st.LIQUIDITY_MIN_TRADES:
            excluded.append(t)
            continue
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
        kind = "stock" if tr == "B" else "etf"
        c = _leg(spread.get(t), COST_BP[kind]) * 2
        if hk:
            c += 2 * _leg(spread.get(hk), COST_BP["hedge"])
        rt[index[t]] = c
    echo(f"prices: {len(liquid)} liquid assets ({len(excluded)} excluded), {nd} days"
         f" [{_time.time() - t0:.0f}s]")

    # eligibility at entry = open of bar j+1 for asset and hedge: [asset, day, j]
    elig = np.isfinite(panel.open_[:, :, 1:st.N_TAU + 1])
    for i, h in enumerate(hedge_of):
        if h >= 0:
            elig[i] &= np.isfinite(panel.open_[h, :, 1:st.N_TAU + 1])
    elig &= decision[None, :, None]

    # --- events per (cell, trigger set) ---------------------------------------------------------
    specs = [c for c in cells() if only_cells is None or c.key in only_cells]
    triggers = sorted({(c.latency, c.window, c.theta) for c in se.grid()})
    group_of = {t: ("stocks" if tr == "B" else by_ticker[t].screen_group) for t, tr, _ in liquid}
    events: dict[str, dict[tuple, pd.DataFrame]] = {c.key: {} for c in specs}
    a_liq = [t for t, tr, _ in liquid if tr == "A"]
    b_liq = [t for t, tr, _ in liquid if tr == "B"]
    if any(c.track == "A" for c in specs):
        cat = istore.read_gdelt("events_15min_cat", None, hold_ts)
        if cat.empty:
            echo("no 15-minute category rows: run `ingest-gdelt-15min events_cat`")
            return MISSING
        _events_a(cat, a_liq, by_ticker, days, utc_slot, elig, index, group_of, specs,
                  triggers, events)
    if any(c.track == "B" for c in specs):
        org = istore.read_gdelt("orgs_15min", None, hold_ts)
        if org.empty:
            echo("no 15-minute org rows: run `ingest-gdelt-15min orgs`")
            return MISSING
        _events_b(org, b_liq, days, utc_slot, elig, index, specs, triggers, events)
    echo(f"events built [{_time.time() - t0:.0f}s]")

    # --- discovery -------------------------------------------------------------------------------
    years = np.array([d.year for d in days])
    pool_d = np.flatnonzero(disc_mask & decision)
    pool_c = np.flatnonzero(conf_mask & decision)
    configs = se.grid()
    disc: dict[str, se.SearchResult] = {}
    for c in specs:
        evs, fn = _config_events(c, configs, events[c.key], disc_mask, panel, rt)
        disc[c.key] = se.max_t_search(evs, fn, years, pool_d, 1, n_boot, cfg.seed)
        r = disc[c.key]
        echo(f"  {c.key:<34} best {r.best} t {_fmt(r.best_stat)} p {_fmt(r.p, 4)}"
             f" [{_time.time() - t0:.0f}s]")
    pv = {k: r.p for k, r in disc.items()}
    discovered = benjamini_hochberg(pv, BH_Q)
    echo(f"discovery: {len(discovered)} of {len(specs)} cells discovered (BH q = {BH_Q})")

    pending: list[tuple[SearchCell, str, str, tuple[date, date], dict[str, Any], bool]] = []
    for c in specs:
        r = disc[c.key]
        for k, (mean, t, ne, ndays) in r.stats.items():
            note = {"mean_net": _num(mean), "t": _num(t), "n_events": ne, "n_days": ndays,
                    "cell_p": _num(r.p), "cell_best": r.best, "cell_discovered": c.key in discovered,
                    "adr": "0008"}
            pending.append((c, k, "discovery", discovery, note, k == r.best))

    # --- confirmation ----------------------------------------------------------------------------
    conf: dict[str, tuple[float, float, float, dict[str, Any]]] = {}
    for c in specs:
        if c.key not in discovered:
            continue
        best = disc[c.key].best
        evs, fn = _config_events(c, configs, events[c.key], conf_mask, panel, rt, only=best)
        res = se.max_t_search(evs, fn, years, pool_c, 1, n_boot, cfg.seed)
        mean, t, ne, ndays = res.stats[best]
        resid = _residual(c, best, events[c.key], disc_mask & decision, conf_mask, panel, elig,
                          index, liquid, cfg.seed)
        conf[c.key] = (res.p, mean, t, {"n_events": ne, "n_days": ndays, "resid_mean": resid})
    holm_pass = holm({k: v[0] for k, v in conf.items()}, HOLM_ALPHA)
    for c in specs:
        if c.key not in conf:
            continue
        p, mean, t, extra = conf[c.key]
        best = disc[c.key].best
        sgn = -1 if best.endswith("|-") else 1
        s_cell = c.sign if c.sign != 0 else sgn
        ok = (c.key in holm_pass and math.isfinite(mean) and mean > 0
              and extra["resid_mean"] is not None and s_cell * extra["resid_mean"] > 0)
        note = {"p": _num(p), "mean_net": _num(mean), "t": _num(t), **extra,
                "holm": c.key in holm_pass, "verdict": "CONFIRMED" if ok else "not confirmed",
                "adr": "0008"}
        pending.append((c, best, "confirmation", confirmation, note, True))
        echo(f"  confirmation {c.key} {best}: net {_fmt(mean, 5)} t {_fmt(t)} p {_fmt(p, 4)}"
             f" -> {note['verdict']}")
    logged = {tr.trial_id for tr in store.read_trials()}
    n0 = len(logged)
    for c, k, period, span, note, selected in pending:
        params = {"adr": "0008", "cell": c.key, "config": k, "period": period, "n_boot": n_boot,
                  "seed": cfg.seed, "selected": selected, "statistic": "nw_t_net_of_cost"}
        _log_trial(store, logged, c.track, "signal", HYPOTHESIS, params, *span, now, note)
    echo(f"{len(logged) - n0} trial(s) logged [{_time.time() - t0:.0f}s]")
    return OK


# --- events --------------------------------------------------------------------------------------

def _decluster(fires: np.ndarray, elig_i: np.ndarray, i: int) -> pd.DataFrame:
    first = st.first_firing(fires & elig_i)
    d = np.flatnonzero(first >= 0)
    return pd.DataFrame({"day": d, "asset": i, "j": first[d]})


def _events_a(cat: pd.DataFrame, tickers: list[str], by_ticker: dict, days: np.ndarray,
              utc_slot: np.ndarray, elig: np.ndarray, index: dict[str, int],
              group_of: dict[str, str], specs: list[SearchCell], triggers: list[tuple],
              events: dict[str, dict[tuple, pd.DataFrame]]) -> None:
    by = {(e, g): x for (e, g), x in cat.groupby(["entity", "geo"])}
    glob = st.slot_matrix(by.get(("_GLOBAL", "action"), cat.iloc[0:0]), "n_events", days)
    missing = st.missing_batches(glob, ALL_SLOTS)
    want = {c.signal for c in specs if c.track == "A"}
    mats: dict[tuple[str, str], dict[str, np.ndarray]] = {}

    def slots(countries: tuple[str, ...], geo: str) -> dict[str, np.ndarray]:
        key = (",".join(countries), geo)
        if key not in mats:
            acc = {col: np.zeros((len(days), st.N_SLOTS)) for col in CAT_COLUMNS}
            for ctry in countries:
                g = by.get((ctry, geo))
                if g is not None:
                    for col in CAT_COLUMNS:
                        acc[col] += st.slot_matrix(g, col, days)
            mats[key] = acc
        return mats[key]

    for lat, win in sorted({(lt, w) for lt, w, _ in triggers}):
        valid = se.window_valid_lw(missing, lat, win)
        G = se.window_sums_lw(glob, lat, win)
        min_n = math.ceil(10 * win / 60)
        for t in tickers:
            a = by_ticker[t]
            countries = tuple(a.fips_countries or [])
            if not countries:
                continue
            i, grp = index[t], group_of[t]
            sig_x: dict[str, tuple[np.ndarray, np.ndarray, str, int, np.ndarray]] = {}
            act = slots(countries, "action")
            E = se.window_sums_lw(act["n_events"], lat, win)
            with np.errstate(all="ignore"):
                if grp == "commodity":
                    for sig, cols in PRODUCER.items():
                        if sig in want:
                            num = sum(se.window_sums_lw(act[col], lat, win) for col in cols)
                            sig_x[sig] = (num / E, E, "share", 1, E)
                else:
                    for sig, (cols, geo) in A_FAMILIES.items():
                        if sig not in want:
                            continue
                        src = act if geo == "action" else slots(countries, "actor")
                        den = E if geo == "action" else se.window_sums_lw(src["n_events"], lat, win)
                        num = sum(se.window_sums_lw(src[col], lat, win) for col in cols)
                        sig_x[sig] = (num / den, den, "share", 1, den)
                    if "tone" in want:
                        ar = se.window_sums_lw(act["n_articles"], lat, win)
                        tx = se.window_sums_lw(act["tone_x_articles"], lat, win)
                        sig_x["tone"] = (tx / ar, ar, "tone", -1, E)
                    if "attention" in want:
                        sig_x["attention"] = (E / G, G, "share", 1, E)
            for sig, (x, den, kind, side, activity) in sig_x.items():
                z = st.at_tau(se.baseline_z_fast(x, den, valid, kind), utc_slot)
                act_ok = st.at_tau(activity, utc_slot) >= min_n
                for th in {th for lt, w, th in triggers if (lt, w) == (lat, win)}:
                    fires = (side * z >= th) & act_ok
                    ev = _decluster(fires, elig[i], i)
                    key = f"A|{sig}|{grp}"
                    if key in events:
                        events[key].setdefault((lat, win, th), []).append(ev)  # type: ignore[arg-type]
    for k, by_trig in events.items():
        if k.startswith("A|"):
            for trig, parts in list(by_trig.items()):
                by_trig[trig] = pd.concat(parts, ignore_index=True) if parts else _empty()


def _events_b(org: pd.DataFrame, tickers: list[str], days: np.ndarray, utc_slot: np.ndarray,
              elig: np.ndarray, index: dict[str, int], specs: list[SearchCell],
              triggers: list[tuple], events: dict[str, dict[tuple, pd.DataFrame]]) -> None:
    by = {e: x for e, x in org.groupby("entity")}
    gdocs = st.slot_matrix(by.get("_GLOBAL", org.iloc[0:0]), "docs", days)
    # Track B batches were pulled at 12:00-19:45 UTC only: unpulled slots have no global docs and
    # count as missing, so a window touching them is invalid (ADR 0008 implementation note).
    missing = st.missing_batches(gdocs, ALL_SLOTS)
    parts: dict[tuple[str, tuple], list[pd.DataFrame]] = {}
    per_ticker = {}
    for t in tickers:
        g = by.get(t)
        if g is not None:
            per_ticker[t] = (st.slot_matrix(g, "docs", days), st.slot_matrix(g, "tone_sum", days))
    for lat, win in sorted({(lt, w) for lt, w, _ in triggers}):
        valid = se.window_valid_lw(missing, lat, win)
        GD = se.window_sums_lw(gdocs, lat, win)
        min_docs = math.ceil(5 * win / 60)
        for t, (docs, tone) in per_ticker.items():
            i = index[t]
            D, TS = se.window_sums_lw(docs, lat, win), se.window_sums_lw(tone, lat, win)
            with np.errstate(all="ignore"):
                z = st.at_tau(se.baseline_z_fast(D / GD, GD, valid, "share"), utc_slot)
            act_ok = st.at_tau(D, utc_slot) >= min_docs
            day_docs, day_tone = docs.sum(axis=1), tone.sum(axis=1)
            cd = np.concatenate([[0], np.cumsum(day_docs)])
            ct = np.concatenate([[0], np.cumsum(day_tone)])
            for th in {th for lt, w, th in triggers if (lt, w) == (lat, win)}:
                ev = _decluster((z >= th) & act_ok, elig[i], i)
                if ev.empty:
                    continue
                d, j = ev["day"].to_numpy(), ev["j"].to_numpy()
                lo = np.maximum(d - st.BASELINE_DAYS, 0)
                bdocs = cd[d] - cd[lo]
                with np.errstate(all="ignore"):
                    bmean = (ct[d] - ct[lo]) / np.where(bdocs > 0, bdocs, np.nan)
                    s = utc_slot[d, j]
                    wmean = TS[d, s] / D[d, s]
                ok = bdocs >= st.TONE_SPLIT_MIN_DOCS
                trig = (lat, win, th)
                parts.setdefault(("B|spike_any|stocks", trig), []).append(ev)
                parts.setdefault(("B|spike_neg|stocks", trig), []).append(ev[ok & (wmean < bmean)])
                parts.setdefault(("B|spike_pos|stocks", trig), []).append(ev[ok & (wmean >= bmean)])
    for (k, trig), ps in parts.items():
        if k in events:
            events[k][trig] = pd.concat(ps, ignore_index=True)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"day": pd.Series(dtype=int), "asset": pd.Series(dtype=int),
                         "j": pd.Series(dtype=int)})


def _config_events(c: SearchCell, configs: list[se.Config], ev_by_trig: dict[tuple, pd.DataFrame],
                   mask: np.ndarray, panel: se.SearchPanel, rt: np.ndarray,
                   only: str | None = None):
    """Per-config events restricted to the period and the directional net-of-cost outcome."""
    signs = (c.sign,) if c.sign != 0 else (1, -1)
    evs: dict[str, pd.DataFrame] = {}
    meta: dict[str, tuple[str, int]] = {}
    for cf in configs:
        ev = ev_by_trig.get((cf.latency, cf.window, cf.theta))
        ev = _empty() if ev is None else ev[mask[ev["day"].to_numpy()]]
        for s in signs:
            k = cf.key if c.sign != 0 else f"{cf.key}|{'+' if s > 0 else '-'}"
            if only is not None and k != only:
                continue
            evs[k] = ev
            meta[k] = (cf.exit, s)

    def fn(k: str, d: np.ndarray, a: np.ndarray, j: np.ndarray) -> np.ndarray:
        exit_, s = meta[k]
        return s * se.search_return(panel, a, d, j, exit_) - rt[a]

    return evs, fn


def _residual(c: SearchCell, best: str, ev_by_trig: dict[tuple, pd.DataFrame],
              disc_dec: np.ndarray, conf_mask: np.ndarray, panel: se.SearchPanel,
              elig: np.ndarray, index: dict[str, int], liquid: list, seed: int) -> float | None:
    """Mean (confirmation) of r - beta * pre-move, pre = [tau-L-W-15, entry), beta by OLS on
    200,000 random non-event (asset, day, tau) discovery cells of the track (ADR 0008 am. 6)."""
    cf = next(x for x in se.grid() if best.startswith(x.key))
    ev = ev_by_trig.get((cf.latency, cf.window, cf.theta))
    if ev is None or ev.empty:
        return None
    rng = np.random.default_rng(seed)
    rows = np.array([index[t] for t, tr, _ in liquid if tr == c.track], dtype=int)
    days = np.flatnonzero(disc_dec)
    a = rows[rng.integers(len(rows), size=BETA_SAMPLE)]
    d = days[rng.integers(len(days), size=BETA_SAMPLE)]
    j = rng.integers(st.N_TAU, size=BETA_SAMPLE)
    keep = elig[a, d, j]
    a, d, j = a[keep], d[keep], j[keep]

    def pre(a_, d_, j_):
        start_min = 585 + 15 * j_ - cf.latency - cf.window - 15
        p0 = se.price_at(panel, a_, d_, start_min)
        r = se.entry_open(panel, a_, d_, j_) / p0 - 1.0
        h = panel.hedge[a_]
        has = h >= 0
        if has.any():
            ph = se.price_at(panel, h[has], d_[has], start_min[has])
            r[has] -= se.entry_open(panel, h[has], d_[has], j_[has]) / ph - 1.0
        return r

    y = se.search_return(panel, a, d, j, cf.exit)
    x = pre(a, d, j)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3:
        return None
    beta = float(np.cov(x[ok], y[ok], ddof=1)[0, 1] / np.var(x[ok], ddof=1))
    ec = ev[conf_mask[ev["day"].to_numpy()]]
    if ec.empty:
        return None
    ea, ed, ej = ec["asset"].to_numpy(), ec["day"].to_numpy(), ec["j"].to_numpy()
    res = se.search_return(panel, ea, ed, ej, cf.exit) - beta * pre(ea, ed, ej)
    m, _, _, _ = se.fast_cell(ed, res, int(ed.max()) + 1)
    return _num(m)


def _leg(spread: float | None, bp: float) -> float:
    s = spread if spread is not None and math.isfinite(spread) else 0.0
    return max(bp / 1e4, s / 2)


def _fmt(x: float | None, d: int = 2) -> str:
    return "nan" if x is None or not math.isfinite(x) else f"{x:.{d}f}"
