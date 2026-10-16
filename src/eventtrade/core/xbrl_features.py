"""ADR 0018 point-in-time fundamentals from SEC XBRL company-concept facts (amendments 4 and 9).

Pure: no I/O. Input facts have the columns of `adapters.sec.FACT_COLS`
(cik, tag, start, end, val, fy, fp, form, filed, accn); dates may be ISO strings.

Point-in-time rules:
- A fact is visible for an event only if `filed < decision`, strictly before the decision date
  (close(d−2)). EDGAR filing dates carry no time and a filing dated d−2 may be accepted after the
  close, so "filed ≤ d−3" is approximated by "filed before the calendar date of d−2".
- Only 10-Q / 10-K forms (amendments and transition reports included) are used.
- Periods are selected by their start/end dates, never by fp, fy or frame: a quarter lasts
  80–100 days, a nine-month period 260–290, a year 350–380.
- For each series and period (start, end), the value of the latest visible filing that reports
  it is used, so restatements, splits and tag changes known by the cutoff apply. Within that
  filing, tag fallbacks apply (EPS: diluted, then basic; revenue: `Revenues`, then
  `RevenueFromContractWithCustomerExcludingAssessedTax`, then `SalesRevenueNet`).
- Q4 revenue and net income = annual − nine months of the same fiscal year (the nine-month start
  equals the annual start, within 3 days, and it ends 80–100 days before the annual end) when no
  direct quarterly fact exists. Q4 EPS = Q4 net income / Q4 diluted weighted shares (a quarterly
  shares fact for that quarter), else missing. Q1–Q3 EPS are the quarterly EPS facts.
- "The same quarter one year earlier" ends 350–380 days before; quarter t−k (k = 1..3) ends
  k·91.3 ± 15 days before.
- If the last visible quarter ended more than 135 days before d (or none exists), every feature
  is NaN and `fund_missing` = 1.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

Q_DAYS = (80, 100)
N_DAYS = (260, 290)
A_DAYS = (350, 380)
YOY_DAYS = (350, 380)
STALE_DAYS = 135
SUE_WINDOW = 8
SUE_MIN = 6
_QLEN = 365.25 / 4
_LAG_TOL = 15
_SAME_TOL = 7
_START_TOL = 3

EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
REV_TAGS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")
NI_TAGS = ("NetIncomeLoss",)
SH_TAGS = ("WeightedAverageNumberOfDilutedSharesOutstanding",)
_SERIES = {tag: (s, rank) for s, tags in enumerate((EPS_TAGS, REV_TAGS, NI_TAGS, SH_TAGS))
           for rank, tag in enumerate(tags)}
_EPS, _REV, _NI, _SH = 0, 1, 2, 3
_KQ, _KN, _KA = 0, 1, 2

FEATURE_COLS = ["eps_q", "eps_q4", "eps_growth_raw", "sue_q", "sue_q1", "sue_q2", "sue_q3",
                "rev_growth", "q_end_age_days", "fund_missing"]

_DAY = np.int64(86_400_000_000_000)


def _prepare(facts: pd.DataFrame) -> pd.DataFrame:
    f = facts[facts["tag"].isin(_SERIES.keys())]
    form = f["form"].fillna("").astype(str)
    f = f[form.str.startswith("10-Q") | form.str.startswith("10-K")]
    out = pd.DataFrame({
        "cik": f["cik"].astype(str).str.zfill(10).to_numpy(),
        "series": f["tag"].map(lambda t: _SERIES[t][0]).to_numpy(dtype=np.int64),
        "rank": f["tag"].map(lambda t: _SERIES[t][1]).to_numpy(dtype=np.int64),
        "start": pd.to_datetime(f["start"], errors="coerce").to_numpy(),
        "end": pd.to_datetime(f["end"], errors="coerce").to_numpy(),
        "filed": pd.to_datetime(f["filed"], errors="coerce").to_numpy(),
        "val": pd.to_numeric(f["val"], errors="coerce").to_numpy(dtype=float),
        "accn": f["accn"].fillna("").astype(str).to_numpy(),
    }).dropna(subset=["start", "end", "filed", "val"])
    dur = (out["end"] - out["start"]).dt.days
    kind = np.full(len(out), -1)
    kind[dur.between(*Q_DAYS).to_numpy()] = _KQ
    kind[dur.between(*N_DAYS).to_numpy()] = _KN
    kind[dur.between(*A_DAYS).to_numpy()] = _KA
    out["kind"] = kind
    out = out[out["kind"] >= 0]
    # Latest filing per (series, period) must be the last row of its key run; within one filing
    # date the preferred tag (lowest rank) comes last.
    out = out.assign(nrank=-out["rank"]).sort_values(
        ["cik", "series", "start", "end", "filed", "nrank", "accn"], kind="mergesort")
    return out.drop(columns="nrank").reset_index(drop=True)


class _Company:
    """One company's facts, sorted by (series, start, end, filed, preferred tag)."""

    def __init__(self, g: pd.DataFrame):
        self.series = g["series"].to_numpy()
        self.kind = g["kind"].to_numpy()
        self.start = g["start"].to_numpy().astype("datetime64[ns]").view(np.int64)
        self.end = g["end"].to_numpy().astype("datetime64[ns]").view(np.int64)
        self.filed = g["filed"].to_numpy().astype("datetime64[ns]").view(np.int64)
        self.val = g["val"].to_numpy()
        key = np.zeros(len(g), dtype=np.int64)
        if len(g) > 1:
            new = ((self.series[1:] != self.series[:-1]) | (self.start[1:] != self.start[:-1])
                   | (self.end[1:] != self.end[:-1]))
            key[1:] = np.cumsum(new)
        self.key = key
        self.filed_u = np.unique(self.filed)
        self._cache: dict[int, dict[str, np.ndarray]] = {}

    def quarters(self, cutoff: np.int64) -> dict[str, np.ndarray]:
        """Quarterly EPS and revenue as known strictly before `cutoff` (ns)."""
        n_vis = int(np.searchsorted(self.filed_u, cutoff, side="left"))
        if n_vis not in self._cache:
            self._cache[n_vis] = self._build(cutoff)
        return self._cache[n_vis]

    def _build(self, cutoff: np.int64) -> dict[str, np.ndarray]:
        idx = np.nonzero(self.filed < cutoff)[0]
        if len(idx):
            k = self.key[idx]
            last = np.ones(len(idx), dtype=bool)
            last[:-1] = k[1:] != k[:-1]
            idx = idx[last]
        per = {}
        for s in (_EPS, _REV, _NI, _SH):
            i = idx[self.series[idx] == s]
            per[s] = {kd: i[self.kind[i] == kd] for kd in (_KQ, _KN, _KA)}
        eps = self._direct(per[_EPS][_KQ])
        rev = self._direct(per[_REV][_KQ])
        sh = self._direct(per[_SH][_KQ])
        for e, v in self._q4(per[_REV], rev).items():
            rev[e] = v
        for e, v in self._q4(per[_NI], eps).items():   # Q4 EPS = Q4 NI / Q4 diluted shares
            j = _near(sh, e, _SAME_TOL)
            if j is not None and sh[j] > 0:
                eps[e] = v / sh[j]
        eps_a = _as_arrays(eps)
        return {"eps": eps_a, "rev": _as_arrays(rev), "diff": _seasonal_diffs(eps_a)}

    def _direct(self, i: np.ndarray) -> dict[np.int64, float]:
        """Quarter end -> value (the latest filing wins when two periods share an end)."""
        vals: dict[np.int64, float] = {}
        filed: dict[np.int64, np.int64] = {}
        for j in i:
            e = self.end[j]
            if e not in filed or self.filed[j] > filed[e]:
                vals[e] = self.val[j]
                filed[e] = self.filed[j]
        return vals

    def _q4(self, per: dict[int, np.ndarray], have: dict) -> dict[np.int64, float]:
        """Year end -> annual − nine months, for years with no quarter in `have` ending there."""
        out: dict[np.int64, float] = {}
        nine = per[_KN]
        for a in per[_KA]:
            ae, ast = self.end[a], self.start[a]
            if _near(have, ae, _SAME_TOL) is not None:
                continue
            best = None
            for n in nine:
                gap = (ae - self.end[n]) // _DAY
                if (abs(self.start[n] - ast) <= _START_TOL * _DAY
                        and Q_DAYS[0] <= gap <= Q_DAYS[1]
                        and (best is None or self.filed[n] > self.filed[best])):
                    best = n
            if best is not None:
                out[ae] = self.val[a] - self.val[best]
        return out


def _near(d, e, tol_days: int):
    """Key of `d` within tol_days of e (closest), else None."""
    best, bd = None, None
    for k in d:
        dd = abs(int(k) - int(e))
        if dd <= tol_days * _DAY and (bd is None or dd < bd):
            best, bd = k, dd
    return best


def _as_arrays(d: dict) -> np.ndarray:
    if not d:
        return np.empty((0, 2))
    ks = sorted(d)
    return np.array([[float(k), d[k]] for k in ks])


def _lookup(arr: np.ndarray, target: float, lo: float, hi: float) -> int | None:
    """Row of arr (sorted by end ns) with end in [target − lo, target + hi] days, closest."""
    if not len(arr):
        return None
    ends = arr[:, 0]
    a = np.searchsorted(ends, target - lo * _DAY, side="left")
    b = np.searchsorted(ends, target + hi * _DAY, side="right")
    if a >= b:
        return None
    return int(a + np.argmin(np.abs(ends[a:b] - target)))


def _lag(arr: np.ndarray, end: float, k: int) -> int | None:
    if k == 4:
        return _lookup(arr, end - 365 * _DAY, 15, 15)  # end 350–380 days before
    return _lookup(arr, end - k * _QLEN * _DAY, _LAG_TOL, _LAG_TOL)


def _seasonal_diffs(eps: np.ndarray) -> np.ndarray:
    out = np.full(len(eps), np.nan)
    for i in range(len(eps)):
        j = _lag(eps, eps[i, 0], 4)
        if j is not None:
            out[i] = eps[i, 1] - eps[j, 1]
    return out


def _sue(eps: np.ndarray, diffs: np.ndarray, i: int | None) -> float:
    if i is None or not np.isfinite(diffs[i]):
        return np.nan
    end = eps[i, 0]
    lo = end - ((SUE_WINDOW - 1) * _QLEN + _LAG_TOL) * _DAY
    w = diffs[(eps[:, 0] >= lo) & (eps[:, 0] <= end)]
    w = w[np.isfinite(w)]
    if len(w) < SUE_MIN:
        return np.nan
    sd = float(np.std(w, ddof=1))
    return float(diffs[i] / sd) if sd > 0 else np.nan


def _features(q: dict[str, np.ndarray], d: np.int64) -> dict[str, float]:
    eps, rev = q["eps"], q["rev"]
    ends = [a[:, 0].max() for a in (eps, rev) if len(a)]
    out = dict.fromkeys(FEATURE_COLS, np.nan)
    out["fund_missing"] = 1.0
    if not ends:
        return out
    t_end = max(ends)
    age = (int(d) - int(t_end)) / _DAY
    if age > STALE_DAYS:
        return out
    out["fund_missing"] = 0.0
    out["q_end_age_days"] = age
    i = _lookup(eps, t_end, _SAME_TOL, _SAME_TOL)
    j = _lag(eps, t_end, 4)
    out["eps_q"] = eps[i, 1] if i is not None else np.nan
    out["eps_q4"] = eps[j, 1] if j is not None else np.nan
    out["eps_growth_raw"] = out["eps_q"] - out["eps_q4"]
    diffs = q["diff"]
    out["sue_q"] = _sue(eps, diffs, i)
    for k in (1, 2, 3):
        out[f"sue_q{k}"] = _sue(eps, diffs, _lag(eps, t_end, k))
    ri = _lookup(rev, t_end, _SAME_TOL, _SAME_TOL)
    rj = _lag(rev, t_end, 4)
    if ri is not None and rj is not None and rev[rj, 1] != 0:
        out["rev_growth"] = float(np.clip((rev[ri, 1] - rev[rj, 1]) / abs(rev[rj, 1]), -1, 1))
    return out


def fundamental_features(facts: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """One row per event (same order and index) with FEATURE_COLS.

    facts: FACT_COLS rows of one or many companies. events: [cik, d, decision] with d the
    announcement day and decision the close(d−2) date; facts count only if filed < decision.
    eps_growth_raw = eps_q − eps_q4 (the caller divides by close(d−2)); sue_q..sue_q3 are the
    time-series SUE of quarters t..t−3 ((EPS − EPS one year earlier) / sd of the seasonal
    differences of the 8 quarters ending there, at least 6, as known at the cutoff);
    rev_growth = (Rev_q − Rev_{q−4}) / |Rev_{q−4}| clipped to ±1; q_end_age_days = d − end of
    the last quarter; fund_missing = 1 when no quarter ended within 135 days of d."""
    f = _prepare(facts)
    ev_cik = events["cik"].astype(str).str.zfill(10).to_numpy()
    d_ns = pd.to_datetime(events["d"]).to_numpy().astype("datetime64[ns]").view(np.int64)
    cut_ns = (pd.to_datetime(events["decision"]).dt.normalize().to_numpy()
              .astype("datetime64[ns]").view(np.int64))
    comps = {c: _Company(g) for c, g in f.groupby("cik", sort=False)}
    empty = _Company(f.iloc[:0])
    rows = []
    for c, d, cut in zip(ev_cik, d_ns, cut_ns):
        comp = comps.get(c, empty)
        rows.append(_features(comp.quarters(cut), d))
    return pd.DataFrame(rows, columns=FEATURE_COLS, index=events.index)
