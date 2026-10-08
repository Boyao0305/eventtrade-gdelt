"""ADR 0007 intraday event study: one run computes discovery (18 cells, BH) and confirmation of the
discovered cells, logging every cell x period as a `signal` trial (hypotheses I1..I6).

Once-only: if any trial with an I1..I6 hypothesis exists, nothing is read or computed and the
stored verdicts are printed (like F1). n_boot is fixed at 2,000 (tests pass a smaller value).
Never names a concrete adapter: the entry point passes the stores in.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.config import Config
from eventtrade.core import intraday as iu
from eventtrade.core import intraday_study as st
from eventtrade.core.screen import benjamini_hochberg, holm

HYPOTHESES = ("I1", "I2", "I3", "I4", "I5", "I6")
DISCOVERY = (date(2016, 1, 4), date(2019, 12, 31))
CONFIRMATION = (date(2020, 1, 2), date(2025, 3, 31))
N_BOOT = 2000
BH_Q = 0.10
HOLM_ALPHA = 0.05
COST_BP = {"etf": 5.0, "stock": 10.0, "hedge": 1.0}
BETA_SAMPLE = 200_000
PULLED_SLOTS = np.zeros(st.N_SLOTS, dtype=bool)
PULLED_SLOTS[48:80] = True  # 12:00-19:45 UTC (ADR 0007 §Data)
MIN_ACTIVITY = {"A": 10, "B": 5}


@dataclass(frozen=True)
class CellSpec:
    hyp: str
    track: str
    signal: str      # conflict | tone | attention | producer_conflict | spike_neg | spike_pos
    group: str       # dm | em | commodity | stocks
    horizon: st.Horizon
    sign: int        # +1 / -1 one-sided, 0 two-sided

    @property
    def key(self) -> str:
        return f"{self.track}|{self.hyp}|{self.signal}|{self.group}|{self.horizon}"


def cells() -> list[CellSpec]:
    out: list[CellSpec] = []
    for h in st.HORIZONS:
        for g in ("dm", "em"):
            out.append(CellSpec("I1", "A", "conflict", g, h, -1))
            out.append(CellSpec("I2", "A", "tone", g, h, -1))
            out.append(CellSpec("I3", "A", "attention", g, h, 0))
        out.append(CellSpec("I4", "A", "producer_conflict", "commodity", h, 1))
        out.append(CellSpec("I5", "B", "spike_neg", "stocks", h, -1))
        out.append(CellSpec("I6", "B", "spike_pos", "stocks", h, 1))
    return sorted(out, key=lambda c: c.key)


def intraday_study(cfg: Config, store: Any, istore: Any, now: Clock, echo: Echo = print,
                   n_boot: int = N_BOOT, discovery: tuple[date, date] = DISCOVERY,
                   confirmation: tuple[date, date] = CONFIRMATION) -> int:
    if confirmation[1] >= cfg.holdout_start or discovery[0] < cfg.start:
        echo("refused: study periods must lie inside the dev period (ADR 0001)")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis in HYPOTHESES]
    if prior:
        echo(f"ADR 0007 study already logged ({len(prior)} trials); nothing recomputed")
        for t in sorted(prior, key=lambda t: (t.params.get("period", ""), t.params.get("cell", ""))):
            n = json.loads(t.notes)
            echo(f"  {t.params.get('period')} {t.params.get('cell')}: mean {n.get('mean')}"
                 f" t {n.get('t')} p {n.get('p')} {n.get('verdict', '')}")
        return OK
    hold_ts = _epoch(cfg.holdout_start)
    cal = study_calendar(cfg, store, istore, discovery[0])
    if isinstance(cal, str):
        echo(cal)
        return MISSING
    days, early = cal
    nd = len(days)
    years = np.array([d.year for d in days])
    utc_slot = st.tau_utc_slots(days)

    def period_mask(p: tuple[date, date]) -> np.ndarray:
        return np.array([p[0] <= d <= p[1] for d in days])

    disc_mask, conf_mask = period_mask(discovery), period_mask(confirmation)
    decision = ~early
    assets = store.read_assets(None)
    a_assets, b_assets = iu.track_a_assets(assets), iu.track_b_assets(assets)
    echo(f"calendar: {nd} trading days ({int(early.sum())} early closes excluded);"
         f" {len(a_assets)} Track A and {len(b_assets)} Track B candidate assets")

    # --- prices: hedges, then assets passing the liquidity filter (discovery bars only) --------
    hedge_mats: dict[str, st.BarMatrix] = {}
    for h in iu.HEDGES:
        hb = istore.read_bars([h], None, hold_ts)
        if hb.empty:
            echo(f"no intraday bars for hedge {h}: run `ingest-intraday`")
            return MISSING
        hedge_mats[h] = st.bar_matrix(hb, days)
    hedge_mats["XLC_OR_SPY"] = _xlc_or_spy(hedge_mats["XLC"], hedge_mats[iu.HEDGE_MARKET])

    rows: list[tuple[str, str, str]] = []   # (ticker, track, hedge key or "")
    for a in a_assets:
        hk = {"dm": iu.HEDGE_DM, "em": iu.HEDGE_EM}.get(a.screen_group or "", "")
        rows.append((a.ticker, "A", hk))
    for a in b_assets:
        hk = iu.SECTOR_SPDR[a.screen_group or ""]
        rows.append((a.ticker, "B", "XLC_OR_SPY" if hk == "XLC" else hk))
    hedge_keys = sorted({h for _, _, h in rows if h})
    liquid: list[tuple[str, str, str]] = []
    excluded: list[str] = []
    spread: dict[str, float] = {}
    for t, tr, hk in rows:  # pass 1: liquidity and spreads (one ticker in memory at a time)
        b = istore.read_bars([t], None, hold_ts)
        m = st.bar_matrix(b, days)
        if not len(b) or st.median_trades(m, disc_mask & decision) < st.LIQUIDITY_MIN_TRADES:
            excluded.append(t)
            continue
        liquid.append((t, tr, hk))
        spread[t] = st.abdi_ranaldo_spread(m, disc_mask & decision)
    for hk in hedge_keys:
        spread[hk] = st.abdi_ranaldo_spread(hedge_mats[hk], disc_mask & decision)
    echo(f"liquidity filter (median trades per bar >= {st.LIQUIDITY_MIN_TRADES}): kept"
         f" {len(liquid)}, excluded {len(excluded)}")
    index = {t: i for i, (t, _, _) in enumerate(liquid)}
    hidx = {hk: len(liquid) + i for i, hk in enumerate(hedge_keys)}
    hedge_of = [hidx[hk] if hk else -1 for _, _, hk in liquid] + [-1] * len(hedge_keys)
    panel = st.empty_panel(len(liquid) + len(hedge_keys), nd, hedge_of)
    for t, i in index.items():  # pass 2: fill the float32 panel
        st.fill_panel_row(panel, i, st.bar_matrix(istore.read_bars([t], None, hold_ts), days))
    for hk, i in hidx.items():
        st.fill_panel_row(panel, i, hedge_mats[hk])

    # --- events ----------------------------------------------------------------------------
    by_ticker = {a.ticker: a for a in assets}
    events: dict[str, pd.DataFrame] = {}   # signal -> (day, asset, j, ticker, group)
    a_liquid = [t for t, tr, _ in liquid if tr == "A"]
    b_liquid = [t for t, tr, _ in liquid if tr == "B"]
    ev_rows = istore.read_gdelt("events_15min", None, hold_ts)
    org_rows = istore.read_gdelt("orgs_15min", None, hold_ts)
    if a_liquid and ev_rows.empty:
        echo("no 15-minute GDELT events: run `ingest-gdelt-15min events`")
        return MISSING
    if b_liquid and org_rows.empty:
        echo("no 15-minute GDELT org mentions: run `ingest-gdelt-15min orgs`")
        return MISSING
    eligible_days = {t: _traded_days(panel, index[t]) & decision for t in index}
    if a_liquid:
        events.update(_track_a_events(ev_rows, a_liquid, by_ticker, days, utc_slot, decision,
                                      panel, index))
    if b_liquid:
        events.update(_track_b_events(org_rows, b_liquid, days, utc_slot, decision, panel,
                                      index))
    group_of = {t: ("stocks" if tr == "B" else by_ticker[t].screen_group) for t, tr, _ in liquid}
    for sig, df in events.items():
        df["group"] = df["ticker"].map(group_of)
        echo(f"  {sig}: {len(df)} events")

    # --- discovery ---------------------------------------------------------------------------
    specs = cells()
    pool_d = np.flatnonzero(disc_mask & decision)
    pool_c = np.flatnonzero(conf_mask & decision)
    disc_res: dict[str, st.PlaceboResult] = {}
    disc_note: dict[str, dict[str, Any]] = {}
    for c in specs:
        ev = _cell_events(events, c, disc_mask)
        res, note = _evaluate(c, ev, panel, years, pool_d, c.sign, n_boot, cfg.seed,
                              eligible_days, index, disc_mask)
        disc_res[c.key], disc_note[c.key] = res, note
    pvals = {k: (r.p if math.isfinite(r.p) else float("nan")) for k, r in disc_res.items()}
    discovered = benjamini_hochberg(pvals, BH_Q)
    logged = {t.trial_id for t in store.read_trials()}
    for c in specs:
        note = {**disc_note[c.key], "discovered": c.key in discovered, "bh_q": BH_Q,
                "n_cells": len(specs), "liquidity_excluded": excluded, "adr": "0007"}
        _log_trial(store, logged, c.track, "signal", c.hyp, _params(c, "discovery", n_boot,
                   cfg.seed), *discovery, now, note)
    echo(f"discovery {discovery[0]}..{discovery[1]}: {len(discovered)} of {len(specs)} cells"
         f" discovered (BH q = {BH_Q})")
    _report(echo, specs, disc_res, disc_note, discovered)

    # --- confirmation --------------------------------------------------------------------------
    if not discovered:
        echo("no cell discovered: the study stops here (ADR 0007)")
        return OK
    betas = _momentum_betas(panel, index, liquid, events, disc_mask & decision, cfg.seed)
    conf: dict[str, tuple[st.PlaceboResult, dict[str, Any]]] = {}
    for c in specs:
        if c.key not in discovered:
            continue
        s = c.sign if c.sign != 0 else int(np.sign(disc_res[c.key].t))
        ev = _cell_events(events, c, conf_mask)
        res, note = _evaluate(c, ev, panel, years, pool_c, s, n_boot, cfg.seed, eligible_days,
                              index, conf_mask)
        rt = _round_trip(ev, c, liquid, spread, by_ticker)
        resid = _residual_mean(ev, c, panel, index, betas[(c.track, c.horizon)])
        note |= {"direction": s, "round_trip": _num(rt), "net": _num(s * res.mean - rt),
                 "beta_pre": _num(betas[(c.track, c.horizon)]), "resid_mean": _num(resid)}
        conf[c.key] = (res, note)
    holm_pass = holm({k: v[0].p for k, v in conf.items()}, HOLM_ALPHA)
    for c in specs:
        if c.key not in conf:
            continue
        res, note = conf[c.key]
        s = note["direction"]
        ok = (c.key in holm_pass and note["net"] is not None and note["net"] > 0
              and note["resid_mean"] is not None and s * note["resid_mean"] > 0)
        note |= {"holm": c.key in holm_pass, "verdict": "CONFIRMED" if ok else "not confirmed",
                 "holm_alpha": HOLM_ALPHA, "adr": "0007"}
        _log_trial(store, logged, c.track, "signal", c.hyp, _params(c, "confirmation", n_boot,
                   cfg.seed), *confirmation, now, note)
        echo(f"  confirmation {c.key}: mean {res.mean:.5f} t {res.t:.2f} p {res.p:.4f}"
             f" net {note['net']} resid {note['resid_mean']} -> {note['verdict']}")
    return OK


def study_calendar(cfg: Config, store: Any, istore: Any, first_decision: date
                   ) -> tuple[np.ndarray, np.ndarray] | str:
    """(trading days, no-decision flag) or a MISSING message.

    Days with SPY intraday bars, before the hold-out; the flag marks early closes. Alpaca bars
    start 2016-01-01, so the 20+ baseline days before the first decision day come from the daily
    SPY calendar (`prices`); they are GDELT baseline days only, flagged as no-decision.
    """
    spy = istore.read_bars([iu.HEDGE_MARKET], None, _epoch(cfg.holdout_start))
    if spy.empty:
        return "no SPY intraday bars: run `ingest-intraday`"
    days, early = st.trading_calendar(spy["ts"])
    keep = days < cfg.holdout_start
    days, early = days[keep], early[keep]
    if int((days < first_decision).sum()) < st.BASELINE_DAYS:
        daily = store.read_prices([iu.HEDGE_MARKET], date(first_decision.year - 1, 11, 1), days[0])
        extra = np.array(sorted({pd.Timestamp(x).date() for x in daily["date"]}), dtype=object)
        if len(extra):
            days = np.concatenate([extra, days])
            early = np.concatenate([np.ones(len(extra), dtype=bool), early])
    if int((days < first_decision).sum()) < st.BASELINE_DAYS:
        return (f"need >= {st.BASELINE_DAYS} trading days before {first_decision} for the"
                " baselines (SPY intraday bars or daily prices): run `ingest-prices --tickers SPY`")
    return days, early


# --- events ------------------------------------------------------------------------------------

def _track_a_events(rows: pd.DataFrame, tickers: list[str], by_ticker: dict, days: np.ndarray,
                    utc_slot: np.ndarray, decision: np.ndarray, panel: st.Panel,
                    index: dict[str, int]) -> dict[str, pd.DataFrame]:
    by_ent = {e: g for e, g in rows.groupby("entity")}
    glob = by_ent.get("_GLOBAL", rows.iloc[0:0])
    g_events = st.slot_matrix(glob, "n_events", days)
    valid = st.window_valid(st.missing_batches(g_events, PULLED_SLOTS))
    G = st.window_sums(g_events)
    out: dict[str, list] = {s: [] for s in ("conflict", "tone", "attention", "producer_conflict")}
    cache: dict[str, dict[str, np.ndarray]] = {}

    def sums(countries: list[str]) -> dict[str, np.ndarray]:
        key = ",".join(sorted(countries))
        if key not in cache:
            acc = {m: np.zeros((len(days), st.N_SLOTS)) for m in
                   ("n_events", "n_conflict", "n_articles", "tone_x_articles")}
            for ctry in countries:
                g = by_ent.get(ctry)
                if g is not None:
                    for m in acc:
                        acc[m] += st.slot_matrix(g, m, days)
            cache[key] = {m: st.window_sums(v) for m, v in acc.items()}
        return cache[key]

    for t in tickers:
        a = by_ticker[t]
        i = index[t]
        countries = list(a.fips_countries or [])
        if not countries:
            continue
        w = sums(countries)
        E, C = w["n_events"], w["n_conflict"]
        with np.errstate(all="ignore"):
            if a.screen_group == "commodity":
                specs = {"producer_conflict": (C / E, E, "share", 1)}
            else:
                specs = {"conflict": (C / E, E, "share", 1),
                         "tone": (w["tone_x_articles"] / w["n_articles"], w["n_articles"], "tone", -1),
                         "attention": (E / G, G, "share", 1)}
        for sig, (x, den, kind, side) in specs.items():
            z = st.baseline_z(x, den, valid, kind)
            fires = (side * st.at_tau(z, utc_slot) >= st.Z_THRESHOLD)
            fires &= st.at_tau(E, utc_slot) >= MIN_ACTIVITY["A"]
            out[sig].append(_declustered(fires, decision, panel, i, t))
    return {s: _concat(v) for s, v in out.items()}


def _track_b_events(rows: pd.DataFrame, tickers: list[str], days: np.ndarray,
                    utc_slot: np.ndarray, decision: np.ndarray, panel: st.Panel,
                    index: dict[str, int]) -> dict[str, pd.DataFrame]:
    by_ent = {e: g for e, g in rows.groupby("entity")}
    glob = by_ent.get("_GLOBAL", rows.iloc[0:0])
    g_docs = st.slot_matrix(glob, "docs", days)
    valid = st.window_valid(st.missing_batches(g_docs, PULLED_SLOTS))
    GD = st.window_sums(g_docs)
    neg, pos = [], []
    for t in tickers:
        g = by_ent.get(t)
        if g is None:
            continue
        docs = st.slot_matrix(g, "docs", days)
        tone = st.slot_matrix(g, "tone_sum", days)
        D, TS = st.window_sums(docs), st.window_sums(tone)
        with np.errstate(all="ignore"):
            z = st.baseline_z(D / GD, GD, valid, "share")
        fires = (st.at_tau(z, utc_slot) >= st.Z_THRESHOLD)
        fires &= st.at_tau(D, utc_slot) >= MIN_ACTIVITY["B"]
        ev = _declustered(fires, decision, panel, index[t], t)
        if ev.empty:
            continue
        # tone split: window mean doc tone vs the previous 20 trading days' mean doc tone
        day_docs, day_tone = docs.sum(axis=1), tone.sum(axis=1)
        cd, ct = np.concatenate([[0], np.cumsum(day_docs)]), np.concatenate([[0], np.cumsum(day_tone)])
        d = ev["day"].to_numpy()
        lo = np.maximum(d - st.BASELINE_DAYS, 0)
        base_docs = cd[d] - cd[lo]
        base_mean = (ct[d] - ct[lo]) / np.where(base_docs > 0, base_docs, np.nan)
        j = ev["j"].to_numpy()
        s = utc_slot[d, j]
        win_mean = TS[d, s] / D[d, s]
        ok = base_docs >= st.TONE_SPLIT_MIN_DOCS
        neg.append(ev[ok & (win_mean < base_mean)])
        pos.append(ev[ok & (win_mean >= base_mean)])
    return {"spike_neg": _concat(neg), "spike_pos": _concat(pos)}


def _declustered(fires: np.ndarray, decision: np.ndarray, panel: st.Panel, i: int,
                 ticker: str) -> pd.DataFrame:
    """First eligible firing per decision day (no event at a tau without an entry bar)."""
    nd = fires.shape[0]
    d = np.repeat(np.arange(nd), st.N_TAU)
    j = np.tile(np.arange(st.N_TAU), nd)
    elig = st.eligible(panel, np.full(len(d), i), d, j).reshape(nd, st.N_TAU)
    f = fires & elig & decision[:, None]
    first = st.first_firing(f)
    days = np.flatnonzero(first >= 0)
    return pd.DataFrame({"day": days, "asset": i, "j": first[days], "ticker": ticker})


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame({"day": pd.Series(dtype=int), "asset": pd.Series(dtype=int),
                             "j": pd.Series(dtype=int), "ticker": pd.Series(dtype=str)})
    return pd.concat(parts, ignore_index=True)


def _cell_events(events: dict[str, pd.DataFrame], c: CellSpec, mask: np.ndarray) -> pd.DataFrame:
    ev = events.get(c.signal)
    if ev is None or ev.empty:
        return _concat([])
    ev = ev[mask[ev["day"].to_numpy()]]
    if c.group != "stocks":
        ev = ev[ev["group"] == c.group]
    ev = ev.drop_duplicates(["day", "asset"])  # declustered already; guard
    return ev.reset_index(drop=True)


# --- evaluation --------------------------------------------------------------------------------

def _evaluate(c: CellSpec, ev: pd.DataFrame, panel: st.Panel, years: np.ndarray,
              pool: np.ndarray, direction: int, n_boot: int, seed: int,
              eligible_days: dict[str, np.ndarray], index: dict[str, int],
              mask: np.ndarray) -> tuple[st.PlaceboResult, dict[str, Any]]:
    def at(d: np.ndarray, a: np.ndarray, j: np.ndarray) -> np.ndarray:
        return st.outcome(panel, a, d, j, c.horizon)

    res = st.placebo_test(ev, at, years, pool, direction, n_boot, seed)  # type: ignore[arg-type]
    pre_t = mean_pre = float("nan")
    if len(ev):
        pre = st.outcome_pre(panel, ev["asset"].to_numpy(), ev["day"].to_numpy(), ev["j"].to_numpy())
        ps = st.daily_series(ev["day"].to_numpy(), pre)
        mean_pre, pre_t = st.cell_t(ps)
    tickers = sorted(set(ev["ticker"])) if len(ev) else []
    asset_days = sum(int((eligible_days[t] & mask).sum()) for t in tickers) if tickers else 0
    note = {"mean": _num(res.mean), "t": _num(res.t), "p": _num(res.p), "n_events": res.n_events,
            "n_days": res.n_days, "n_valid_draws": res.n_valid_draws,
            "sufficient": st.sufficient(res.n_events, res.n_days),
            "firing_rate": _num(len(ev) / asset_days) if asset_days else None,
            "n_assets_with_events": len(tickers),
            "pre_move_mean": _num(mean_pre), "pre_move_t": _num(pre_t)}
    return res, note


def _momentum_betas(panel: st.Panel, index: dict[str, int], liquid: list[tuple[str, str, str]],
                    events: dict[str, pd.DataFrame], mask: np.ndarray, seed: int
                    ) -> dict[tuple[str, str], float]:
    """OLS slope of outcome on pre-move over 200,000 random non-event (asset, day, tau) cells of
    the discovery period, per (track, horizon) (ADR 0007 confirmation rule 3)."""
    rng = np.random.default_rng(seed)
    days = np.flatnonzero(mask)
    taken = set()
    for df in events.values():
        taken |= set(zip(df["asset"].tolist(), df["day"].tolist(), df["j"].tolist(), strict=True))
    out: dict[tuple[str, str], float] = {}
    for tr in ("A", "B"):
        rows = np.array([index[t] for t, track, _ in liquid if track == tr], dtype=int)
        for h in st.HORIZONS:
            if not len(rows) or not len(days):
                out[(tr, h)] = float("nan")
                continue
            a = rows[rng.integers(len(rows), size=BETA_SAMPLE)]
            d = days[rng.integers(len(days), size=BETA_SAMPLE)]
            j = rng.integers(st.N_TAU, size=BETA_SAMPLE)
            keep = np.array([(x, y, z) not in taken for x, y, z in zip(a, d, j, strict=True)])
            a, d, j = a[keep], d[keep], j[keep]
            y = st.outcome(panel, a, d, j, h)
            x = st.outcome_pre(panel, a, d, j)
            ok = np.isfinite(x) & np.isfinite(y)
            xv, yv = x[ok], y[ok]
            out[(tr, h)] = float(np.cov(xv, yv, ddof=1)[0, 1] / np.var(xv, ddof=1)) if ok.sum() > 2 else float("nan")
    return out


def _residual_mean(ev: pd.DataFrame, c: CellSpec, panel: st.Panel, index: dict[str, int],
                   beta: float) -> float:
    if ev.empty or not math.isfinite(beta):
        return float("nan")
    a, d, j = ev["asset"].to_numpy(), ev["day"].to_numpy(), ev["j"].to_numpy()
    r = st.outcome(panel, a, d, j, c.horizon) - beta * st.outcome_pre(panel, a, d, j)
    s = st.daily_series(d, r)
    return float(s.mean()) if len(s) else float("nan")


def _round_trip(ev: pd.DataFrame, c: CellSpec, liquid: list[tuple[str, str, str]],
                spread: dict[str, float], by_ticker: dict) -> float:
    """Mean over the cell's events of RT = 2 c_asset (+ 2 c_hedge), c = max(bp, AR spread / 2)."""
    if ev.empty:
        return float("nan")
    hedge = {t: hk for t, _, hk in liquid}

    def leg(t: str, bp: float) -> float:
        s = spread.get(t, float("nan"))
        return max(bp / 1e4, s / 2 if math.isfinite(s) else 0.0)

    rts = []
    for t in ev["ticker"]:
        kind = "stock" if by_ticker[t].track == "B" else "etf"
        rt = 2 * leg(t, COST_BP[kind])
        if hedge[t]:
            rt += 2 * leg(hedge[t], COST_BP["hedge"])
        rts.append(rt)
    return float(np.mean(rts))


# --- helpers -----------------------------------------------------------------------------------

def _xlc_or_spy(xlc: st.BarMatrix, spy: st.BarMatrix) -> st.BarMatrix:
    """XLC from its first traded day on, SPY before (ADR 0007: XLC starts 2018-06-19)."""
    traded = np.isfinite(xlc.close).any(axis=1)
    first = int(np.argmax(traded)) if traded.any() else len(traded)
    use_spy = np.arange(len(traded)) < first

    def mix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        out = a.copy()
        out[use_spy] = b[use_spy]
        return out

    return st.BarMatrix(mix(xlc.open, spy.open), mix(xlc.close, spy.close),
                        mix(xlc.high, spy.high), mix(xlc.low, spy.low),
                        mix(xlc.trades, spy.trades))


def _traded_days(panel: st.Panel, i: int) -> np.ndarray:
    return np.isfinite(panel.entry[i]).any(axis=1)


def _params(c: CellSpec, period: str, n_boot: int, seed: int) -> dict[str, Any]:
    return {"adr": "0007", "cell": c.key, "signal": c.signal, "group": c.group,
            "horizon": c.horizon, "sign": c.sign, "period": period, "n_boot": n_boot,
            "seed": seed, "theta": st.Z_THRESHOLD, "latency_min": 15, "window_batches": 4}


def _report(echo: Echo, specs: list[CellSpec], res: dict[str, st.PlaceboResult],
            notes: dict[str, dict[str, Any]], discovered: set[str]) -> None:
    echo(f"{'cell':<44}{'events':>7}{'days':>6}{'mean':>10}{'t':>7}{'p':>8}{'pre_t':>7}{'fire':>7}  disc")
    for c in specs:
        r, n = res[c.key], notes[c.key]
        fr = n["firing_rate"]
        echo(f"{c.key:<44}{r.n_events:>7}{r.n_days:>6}{_f(r.mean, 10, 5)}{_f(r.t, 7, 2)}"
             f"{_f(r.p, 8, 4)}{_f(n['pre_move_t'] or float('nan'), 7, 2)}"
             f"{_f(fr if fr is not None else float('nan'), 7, 4)}  {'yes' if c.key in discovered else '-'}")


def _f(x: float, w: int, d: int) -> str:
    return f"{x:>{w}.{d}f}" if x is not None and math.isfinite(x) else f"{'nan':>{w}}"


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
