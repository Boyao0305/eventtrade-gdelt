"""ADR 0012: insider purchases on fresh (non-S&P 1500) listed stocks, once-only (hypothesis S2).

Inputs (stores passed in by the entry point):
- `small`: SqliteStore at data/smallcap.sqlite with `prices` (yfinance, incl. IWM) and the
  snapshot tables `small_universe(cik, ticker, name, exchange)`, `delisted(cik)`, `splits`.
- `sstore`: SecStore with the insider tables, `company_meta`, `company_filings2`.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from eventtrade.cli.pipelines import MISSING, OK, REFUSED, Clock, Echo, _log_trial, _num
from eventtrade.config import Config
from eventtrade.core import eventstudy
from eventtrade.core import sec_events as sev
from eventtrade.core import small_insider as si
from eventtrade.core.screen import holm

HYPOTHESIS = "S2"
H = 60
CELLS = ("ins_all", "ins_cxo", "ins_cluster")
N_BOOT = 2000
HOLM_ALPHA = 0.05
PERIOD = (date(2016, 1, 4), date(2025, 3, 31))
COVID = (pd.Timestamp("2020-02-20"), pd.Timestamp("2020-04-30"))
HEDGE = "IWM"


def _snapshot(path: Any, table: str) -> pd.DataFrame:
    with sqlite3.connect(path) as c:
        return pd.read_sql_query(f"SELECT * FROM {table}", c)


def sec_small_study(cfg: Config, store: Any, small: Any, sstore: Any, now: Clock,
                    echo: Echo = print, n_boot: int = N_BOOT) -> int:
    if PERIOD[1] >= cfg.holdout_start:
        echo("refused: the period reaches the hold-out")
        return REFUSED
    prior = [t for t in store.read_trials() if t.hypothesis == HYPOTHESIS]
    if prior:
        echo(f"ADR 0012 already logged ({len(prior)} trials); nothing recomputed")
        for t in prior:
            n = json.loads(t.notes)
            echo(f"  {t.params.get('cell')}: mean {n.get('mean')} p {n.get('p')} {n.get('verdict')}")
        return OK
    uni = _snapshot(small.path, "small_universe")
    delisted = _snapshot(small.path, "delisted")
    splits_all = _snapshot(small.path, "splits")
    meta = sstore.read("company_meta").set_index("cik")
    filings = sstore.read("company_filings2")
    if uni.empty or meta.empty:
        echo("missing: small_universe snapshot or company profiles")
        return MISSING
    uni["cik"] = uni["cik"].astype(str).str.zfill(10)
    cmap = dict(zip(uni["cik"], uni["ticker"], strict=True))
    px = small.read_prices(None, date(2015, 1, 1), cfg.holdout_start)
    px["date"] = pd.to_datetime(px["date"])
    wide = {c: px.pivot(index="date", columns="ticker", values=c)
            for c in ("adj_open", "close", "adj_close", "volume")}
    if HEDGE not in wide["adj_open"].columns:
        echo("missing IWM prices")
        return MISSING
    cal = wide["adj_open"][HEDGE].dropna().index
    wide = {k: v.reindex(cal) for k, v in wide.items()}
    tickers = [t for t in uni["ticker"] if t in wide["adj_open"].columns]
    echo(f"universe: {len(uni)} listed CIKs, {len(tickers)} with prices; {len(delisted)} delisted CIKs")

    # --- identity: ticker check (amendment 4) and issuer rules (amendment 3) -----------------
    splits = {t: g for t, g in splits_all.groupby("ticker")} if len(splits_all) else {}
    raw = si.raw_close(wide["close"][tickers], splits)
    subs, owners, trans = (sstore.read("insider_subs"), sstore.read("insider_owners"),
                           sstore.read("insider_trans"))
    tr = trans[trans["trans_code"] == "P"].merge(subs, on="accession_number")
    tr["ticker"] = tr["issuercik"].astype(str).str.zfill(10).map(cmap)
    tr = tr.dropna(subset=["ticker"])
    tr["trans_date"] = sev._dates(tr["trans_date"])
    tr["price"] = pd.to_numeric(tr["trans_pricepershare"], errors="coerce")
    tr = tr[(tr["price"] > 0) & tr["trans_date"].notna()]
    mism = si.ticker_mismatch(tr[["ticker", "trans_date", "price"]], raw)
    bad = sorted(t for t, v in mism.items() if v > si.MISMATCH_SHARE)
    tickers = [t for t in tickers if t not in set(bad)]
    echo(f"ticker check: {len(bad)} stocks dropped (> 20% of purchase prices off the price history)")
    inv = {t: c for c, t in cmap.items()}
    admissible = pd.DataFrame(False, index=cal, columns=tickers)
    for t in tickers:
        c = inv[t]
        m = meta.loc[c].to_dict() if c in meta.index else {"sic": None, "former_names": "[]"}
        admissible[t] = si.issuer_ok(m, filings[filings["cik"].astype(str).str.zfill(10) == c],
                                     pd.Series(cal))
    mdv = si.median_dollar_volume(wide["close"][tickers], wide["volume"][tickers])
    admissible &= si.tradable(raw[tickers], mdv)
    echo(f"admissible stock-days: {int(admissible.to_numpy().sum()):,}")

    # --- outcomes, strata --------------------------------------------------------------------
    opens, n_err = si.clean_opens(wide["adj_open"][tickers], wide["volume"][tickers])
    oc, delayed = si.hedged_outcomes(opens, wide["adj_open"][HEDGE], H)
    adm_long = admissible.stack()
    adm_long = adm_long[adm_long].index
    oc = oc.set_index(["date", "ticker"]).loc[lambda d: d.index.isin(adm_long)].reset_index()
    pm = (cal >= pd.Timestamp(PERIOD[0])) & (cal <= pd.Timestamp(PERIOD[1]))
    oc = oc[oc["date"].isin(cal[pm])]
    echo(f"outcomes: {len(oc):,} stock-days; price errors screened {n_err}, delayed exits {delayed}")
    cl, iwm = wide["adj_close"][tickers], wide["adj_close"][HEDGE]
    rel20 = cl / cl.shift(20) - 1 - (iwm / iwm.shift(20) - 1).to_numpy()[:, None]
    rel250 = cl / cl.shift(250) - 1 - (iwm / iwm.shift(250) - 1).to_numpy()[:, None]
    f2 = filings[filings["form"].isin(si.PERIODIC)].copy()
    f2["filing_date"] = pd.to_datetime(f2["filing_date"])
    periodic = {cmap[c]: list(g["filing_date"]) for c, g in
                f2.groupby(f2["cik"].astype(str).str.zfill(10)) if c in cmap}
    st3 = si.strata3(rel20, rel250, periodic, cal, pm)
    st2 = si.strata3(rel20, rel250, periodic, cal, pm, two_dim=True)

    # --- events ------------------------------------------------------------------------------
    acc = sev.qualifying_accessions(subs, owners, trans, cmap)
    ev_all = sev.insider_events(acc, cal)
    pre_counts = {k: int(((v["date"] >= pd.Timestamp(PERIOD[0])) & (v["date"] <= pd.Timestamp(PERIOD[1]))).sum())
                  for k, v in ev_all.items()}
    ev = {}
    for k, v in ev_all.items():
        v = v[v["ticker"].isin(tickers)]
        keep = [bool(admissible.at[d, t]) if d in admissible.index else False
                for d, t in zip(v["date"], v["ticker"], strict=True)]
        ev[k] = v[np.asarray(keep, dtype=bool)].reset_index(drop=True)
    echo("events (admissible): " + ", ".join(f"{k} {len(ev[k])}" for k in CELLS))

    # --- survivorship (amendment 1) ------------------------------------------------------------
    dl = delisted["cik"].astype(str).str.zfill(10).tolist()
    dmap = {c: c for c in dl}
    dacc = sev.qualifying_accessions(subs, owners, trans, dmap)
    dev = sev.insider_events(dacc, cal)
    exit_of = {c: si.classify_exit(filings[filings["cik"].astype(str).str.zfill(10) == c])
               for c in dl}
    surv: dict[str, dict[str, Any]] = {}
    for k in CELLS:
        d = dev[k][(dev[k]["date"] >= pd.Timestamp(PERIOD[0])) & (dev[k]["date"] <= pd.Timestamp(PERIOD[1]))]
        kinds = d["ticker"].map(exit_of).value_counts().to_dict()
        n_fail = int(kinds.get("failure", 0))
        total = pre_counts[k] + len(d)
        surv[k] = {"listed_events_sec": pre_counts[k], "delisted_events": len(d),
                   "delisted_by_exit": kinds, "f_fail": n_fail / total if total else float("nan")}
    echo("survivorship: " + "; ".join(f"{k} f_fail {surv[k]['f_fail']:.3f} {surv[k]['delisted_by_exit']}"
                                      for k in CELLS))

    # --- tests ---------------------------------------------------------------------------------
    mdv_long = mdv.stack()
    res: dict[str, dict[str, Any]] = {}
    for k in CELLS:
        e = ev[k][(ev[k]["date"] >= pd.Timestamp(PERIOD[0])) & (ev[k]["date"] <= pd.Timestamp(PERIOD[1]))]
        m3 = sev.matched_placebo(e, oc, st3, H, 1, cal, n_boot, cfg.seed)
        m2 = sev.matched_placebo(e, oc, st2, H, 1, cal, n_boot, cfg.seed)
        um = eventstudy.placebo_p(e, oc, H, 1, cal, n_boot, cfg.seed)
        have, _ = eventstudy._events_with_outcome(e, oc)
        dvs = np.array([mdv_long.get((d, t), np.nan) for d, t in zip(have["date"], have["ticker"], strict=True)])
        rt = float(np.nanmean(si.cost_round_trip(dvs))) if len(dvs) else float("nan")
        rt_s = float(np.nanmean(si.cost_round_trip(dvs, stress=True))) if len(dvs) else float("nan")
        ex = e[~((e["date"] >= COVID[0]) & (e["date"] <= COVID[1]))]
        s_ex = eventstudy.calendar_time_series(ex, oc, H, cal)
        vals = have["value"].to_numpy()
        lo, hi = np.nanquantile(vals, [0.01, 0.99]) if len(vals) else (np.nan, np.nan)
        wins = float(np.clip(vals, lo, hi).mean()) if len(vals) else float("nan")
        mean = m3.mean
        res[k] = {"mean": _num(mean), "t": _num(m3.t), "p": _num(m3.p), "p_2dim": _num(m2.p),
                  "p_unmatched": _num(um.p), "n_events": m3.n_events,
                  "n_event_days": m3.n_event_days, "n_matched": m3.n_matched,
                  "round_trip": _num(rt), "net": _num(mean - rt), "net_stress": _num(mean - rt_s),
                  "mean_ex_covid": _num(float(s_ex.mean())) if len(s_ex) else None,
                  "winsorized_event_mean": _num(wins),
                  "stressed_mean": _num(si.stressed_mean(mean, surv[k]["f_fail"]))
                  if math.isfinite(surv[k]["f_fail"]) else None, "surv": surv[k],
                  "adr": "0012"}
        echo(f"  {k:<11} h{H} n {m3.n_events:>5} mean {mean:.4f} t {m3.t:.2f} p {m3.p:.4f}"
             f" (2-dim {m2.p:.4f}, unmatched {um.p:.4f}) net {mean - rt:.4f}"
             f" stressed {res[k]['stressed_mean']}")
    hp = holm({k: (v["p"] if v["p"] is not None else math.nan) for k, v in res.items()}, HOLM_ALPHA)
    passed = []
    for k, v in res.items():
        ok = (k in hp and (v["mean"] or 0) > 0 and (v["net"] or 0) > 0
              and (v["mean_ex_covid"] or 0) > 0)
        v["holm"] = k in hp
        v["pass"] = ok
        v["stress_ok"] = (v["stressed_mean"] or -1) > 0
        if ok:
            passed.append(k)
    replicated = ("ins_all" in passed and len(passed) >= 2
                  and all(res[k]["stress_ok"] for k in passed))
    verdict = "replicated" if replicated else ("partial" if passed else "not replicated")
    for v in res.values():
        v["verdict"] = verdict
    echo(f"verdict: {verdict} (passing cells: {passed or 'none'})")
    logged = {t.trial_id for t in store.read_trials()}
    n0 = len(logged)
    for k, note in res.items():
        params = {"adr": "0012", "cell": f"{k}|h{H}", "period": "full", "n_boot": n_boot,
                  "seed": cfg.seed, "null": "matched3", "universe": "listed_non_sp1500"}
        _log_trial(store, logged, "B", "signal", HYPOTHESIS, params, *PERIOD, now, note)
    echo(f"{len(logged) - n0} trial(s) logged")
    return OK
