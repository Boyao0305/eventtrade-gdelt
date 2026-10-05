"""Statistics for the pre-registered sub-category screen (ADR 0003).

Pure: no I/O, no clock. Dates, seeds and the cell definitions are parameters; the only randomness
is the stationary block bootstrap in `joint_test`, seeded by the caller.

A *cell* is (signal, asset group, horizon). Cell ids are any hashable (tuples are fine); `notes()`
dicts key them by `str(cell)` so they serialise into the trial log.

Conventions pinned by tests/core/test_screen.py:
- `cell_ic`: daily cross-sectional Spearman IC (core/evaluate.daily_ic) over the group members
  only, dates in [start, end] inclusive (feature date), min 5 names per day.
- Per-cell t is evaluate.newey_west_t with the cell's horizon (overlap-safe default lags).
- Direction (ADR 0003 implementation notes, "Joint test statistics"). Each cell has a registered
  sign (+1/-1) or None (two-sided). The directional score is z = sign * t for a signed cell and
  z = |t| for a two-sided cell. The joint statistics are S = sum_k max(z_k, 0)^2 and
  M = max_k z_k (cells with undefined t are left out). With every sign None these are exactly
  ADR 0003's sum t^2 and max |t|; with signs, a cell moving against its registered sign adds
  nothing (ADR 0002's sign enforcement), so a strong wrong-way cell cannot make a track
  significant. The pass rule, min(p_S, p_M) < 0.025, is applied by the caller.
- Joint null (ADR 0003 step 2): every cell's IC series is demeaned (imposes H0: mean IC = 0),
  the union of dates is resampled by a stationary block bootstrap (Politis & Romano 1994) with
  the SAME date indices for every cell (keeps the cross-cell correlation), and the HAC t is
  recomputed on every bootstrap sample (studentised bootstrap). A date where a cell has no IC
  stays missing in the resample; as in newey_west_t, the remaining values are treated as
  consecutive. p = (1 + #{null >= observed}) / (n_boot + 1). Per-cell p-values use the same
  draws: one-sided on z where a sign is registered, two-sided (|t|) where it is not.
- `eb_shrink`: normal-normal, method-of-moments tau^2 = max(0, var(est) - mean(se^2)) around the
  estimated grand mean (sample variance, ddof=1), or around a fixed `prior_mean` (mean square,
  ddof=0). Posterior mean = prior + (1 - B)(est - prior), B = se^2 / (se^2 + tau^2).
- `shrink_directional` (ADR 0003 implementation notes, "Shrinkage"): what the screen pools.
  A signed cell pools its directional IC, sign * IC; a two-sided cell pools its signed IC
  unfolded (never |IC|). The direction is applied after shrinkage: a signed cell's posterior is
  already directional; a two-sided cell's is multiplied by the sign of its discovery t (NaN if
  that t is undefined or zero).
- `benjamini_hochberg` (step-up) and `holm` (step-down): NaN p-values count in m and are never
  rejected.
- `confirm` (ADR 0003 step 5): direction = registered sign, or the sign of the discovery t for a
  two-sided cell. One-sided p from the confirmation-period NW t (normal approximation, as
  registered; known size loss: under a MA(h-1) null at T ~ 1320 it is calibrated at h = 5 but
  rejects about 1.1-1.4x nominal at h = 20, more at Holm's smaller levels - see agent log),
  Holm at `alpha` across the discovered cells, same-sign mean IC, and the directional mean
  spread minus 4 * cost_bps * min(1, horizon * daily_turnover) > 0 (the gate-1 cost of
  core/evaluate.passes_gate).
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from eventtrade.core.evaluate import daily_ic, newey_west_t
from eventtrade.core.overfit import _stationary_indices

Cell = Hashable
Sign = int | float | None


# --- cell IC ----------------------------------------------------------------------------------

def cell_ic(
    signal: pd.DataFrame,
    labels: pd.DataFrame,
    members: Iterable[str],
    horizon: int,
    start: Any,
    end: Any,
    min_names: int = 5,
) -> pd.Series:
    """Daily within-group Spearman IC of one signal on feature dates in [start, end] inclusive.

    `signal` is a FEATURE_COLUMNS frame holding one feature; `labels` a LABEL_COLUMNS frame.
    """
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    names = set(members)
    feat = signal[signal["ticker"].isin(names) & (signal["date"] >= lo) & (signal["date"] <= hi)]
    lab = labels[
        (labels["horizon"] == horizon) & labels["ticker"].isin(names)
        & (labels["date"] >= lo) & (labels["date"] <= hi)
    ]
    if signal["feature"].nunique() > 1:
        raise ValueError(f"expected one feature, got {signal['feature'].nunique()}")
    return daily_ic(feat, lab, horizon, method="spearman", min_names=min_names)


def cell_estimate(ic: pd.Series, horizon: int) -> tuple[float, float]:
    """(mean IC, its HAC standard error = |mean / NW t|); se NaN when t is undefined or zero."""
    s = pd.Series(ic, dtype=float).dropna()
    if s.empty:
        return float("nan"), float("nan")
    mean = float(s.mean())
    t = newey_west_t(s, horizon=horizon)
    if not math.isfinite(t) or t == 0.0:
        return mean, (0.0 if math.isinf(t) else float("nan"))
    return mean, abs(mean / t)


# --- joint test -------------------------------------------------------------------------------

@dataclass(frozen=True)
class JointResult:
    """Joint bootstrap test of one track's cells (ADR 0003 step 2); `notes()` goes to the trial log."""

    s_stat: float  # sum over cells of max(z, 0)^2
    m_stat: float  # max over cells of z
    p_s: float
    p_m: float
    t: dict[Cell, float]  # observed NW t per cell
    z: dict[Cell, float]  # directional score: sign * t, or |t| for two-sided cells
    p_cell: dict[Cell, float]  # bootstrap p per cell (one-sided on z / two-sided on |t|)
    signs: dict[Cell, Sign]
    horizons: dict[Cell, int]
    n_boot: int
    mean_block: float
    seed: int
    n_dates: int
    n_obs: dict[Cell, int] = field(default_factory=dict)

    def notes(self) -> dict[str, Any]:
        return {
            "test": "stationary_block_bootstrap_joint",
            "s_stat": _f(self.s_stat), "m_stat": _f(self.m_stat),
            "p_s": _f(self.p_s), "p_m": _f(self.p_m),
            "n_boot": self.n_boot, "mean_block": self.mean_block, "seed": self.seed,
            "n_dates": self.n_dates, "n_cells": len(self.t),
            "t": _keyed(self.t), "z": _keyed(self.z), "p_cell": _keyed(self.p_cell),
            "signs": {str(c): s for c, s in self.signs.items()},
            "horizons": {str(c): h for c, h in self.horizons.items()},
            "n_obs": {str(c): n for c, n in self.n_obs.items()},
        }


def joint_test(
    ic_by_cell: Mapping[Cell, pd.Series],
    horizon_of: Mapping[Cell, int],
    signs: Mapping[Cell, Sign],
    n_boot: int,
    mean_block: float,
    seed: int,
) -> JointResult:
    """Joint S / M statistics and their stationary-block-bootstrap null (see module docstring)."""
    cells = list(ic_by_cell)
    if not cells:
        raise ValueError("ic_by_cell is empty")
    missing = [c for c in cells if c not in horizon_of or c not in signs]
    if missing:
        raise ValueError(f"cells without a horizon or sign: {missing[:5]}")
    sgn = {c: _check_sign(signs[c]) for c in cells}
    if n_boot < 1:
        raise ValueError("n_boot must be >= 1")

    wide = pd.DataFrame({i: pd.Series(ic_by_cell[c], dtype=float) for i, c in enumerate(cells)})
    wide = wide.sort_index()
    x = wide.to_numpy(dtype=float)
    n_dates = x.shape[0]

    t_obs = {c: newey_west_t(wide[i], horizon=int(horizon_of[c])) for i, c in enumerate(cells)}
    z_obs = np.array([_z(t_obs[c], sgn[c]) for c in cells])
    s_obs, m_obs = _s_m(z_obs[None, :])

    z_boot = np.full((n_boot, len(cells)), np.nan)
    if n_dates > 0:
        idx = _stationary_indices(n_dates, n_boot, mean_block, np.random.default_rng(seed))
        for i, c in enumerate(cells):
            col = x[:, i]
            if math.isnan(t_obs[c]):
                continue  # undefined observed t: cell is out of the test
            with np.errstate(invalid="ignore"):
                demeaned = col - np.nanmean(col)
            t_b = _nw_t_rows(demeaned[idx], int(horizon_of[c]))
            z_boot[:, i] = _z_vec(t_b, sgn[c])
    s_null, m_null = _s_m(z_boot)

    p_cell: dict[Cell, float] = {}
    for i, c in enumerate(cells):
        if math.isnan(z_obs[i]):
            p_cell[c] = float("nan")
        else:
            null = np.nan_to_num(z_boot[:, i], nan=0.0)
            p_cell[c] = _boot_p(null, float(z_obs[i]))
    return JointResult(
        s_stat=float(s_obs[0]), m_stat=float(m_obs[0]),
        p_s=_boot_p(s_null, float(s_obs[0])), p_m=_boot_p(m_null, float(m_obs[0])),
        t={c: float(t_obs[c]) for c in cells}, z={c: float(z) for c, z in zip(cells, z_obs)},
        p_cell=p_cell, signs={c: signs[c] for c in cells},
        horizons={c: int(horizon_of[c]) for c in cells},
        n_boot=int(n_boot), mean_block=float(mean_block), seed=int(seed), n_dates=int(n_dates),
        n_obs={c: int(wide[i].notna().sum()) for i, c in enumerate(cells)},
    )


def _z(t: float, sign: float | None) -> float:
    if math.isnan(t):
        return float("nan")
    return abs(t) if sign is None else sign * t


def _z_vec(t: np.ndarray, sign: float | None) -> np.ndarray:
    return np.abs(t) if sign is None else sign * t


def _s_m(z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Row-wise S = sum max(z,0)^2 and M = max z, NaN cells ignored (M NaN if all NaN)."""
    pos = np.where(np.isnan(z), 0.0, np.maximum(z, 0.0))
    with np.errstate(invalid="ignore"):
        s = np.sum(pos * pos, axis=1)
    all_nan = np.isnan(z).all(axis=1)
    m = np.where(all_nan, np.nan, np.max(np.where(np.isnan(z), -np.inf, z), axis=1))
    return s, m


def _boot_p(null: np.ndarray, observed: float) -> float:
    if math.isnan(observed):
        return float("nan")
    null = np.where(np.isnan(null), -np.inf, null)
    return float((1 + np.sum(null >= observed)) / (null.size + 1))


def _nw_t_rows(x: np.ndarray, horizon: int) -> np.ndarray:
    """evaluate.newey_west_t (default lags) applied to every row of `x`; NaNs dropped per row.

    Vectorised for the bootstrap: each row's valid values are compacted to the front (order kept)
    and padded with zeros after demeaning, so the padded products add nothing to the
    autocovariances. Pinned against newey_west_t by tests.
    """
    x = np.atleast_2d(np.asarray(x, dtype=float))
    n_rows, n = x.shape
    out = np.full(n_rows, np.nan)
    if n == 0:
        return out
    valid = ~np.isnan(x)
    order = np.argsort(~valid, axis=1, kind="stable")
    xc = np.take_along_axis(x, order, axis=1)
    cnt = valid.sum(axis=1)
    mask = np.arange(n)[None, :] < cnt[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(mask, xc, 0.0).sum(axis=1) / cnt
        d = np.where(mask, xc - mean[:, None], 0.0)
        hi = np.where(mask, xc, -np.inf).max(axis=1)
        lo = np.where(mask, xc, np.inf).min(axis=1)
    ok = cnt >= 2
    cntf = np.maximum(cnt, 1).astype(float)

    auto = np.floor(4 * (cntf / 100) ** (2 / 9)).astype(int)
    lag_b = np.maximum(auto, 2 * horizon) if horizon > 1 else auto
    lag_b = np.clip(lag_b, 0, np.maximum(cnt - 1, 0))
    lag_h = np.clip(np.full(n_rows, horizon - 1), 0, np.maximum(cnt - 1, 0))
    max_lag = int(max(lag_b.max(initial=0), lag_h.max(initial=0)))

    gam0 = np.einsum("ij,ij->i", d, d) / cntf
    lrv_b = gam0.copy()
    lrv_h = gam0.copy()
    for k in range(1, max_lag + 1):
        g = np.einsum("ij,ij->i", d[:, k:], d[:, :-k]) / cntf
        w = np.where(k <= lag_b, 1.0 - k / (lag_b + 1.0), 0.0)
        lrv_b += 2.0 * w * g
        lrv_h += 2.0 * np.where(k <= lag_h, 1.0, 0.0) * g
    lrv = np.maximum(lrv_b, lrv_h) if horizon > 1 else lrv_b

    const = ok & (hi == lo)
    degenerate = ok & ~const & (lrv <= 0.0)
    good = ok & ~const & ~degenerate
    with np.errstate(invalid="ignore", divide="ignore"):
        out[good] = mean[good] / np.sqrt(lrv[good] / cntf[good])
    for bad in (const, degenerate):
        # newey_west_t: NaN for a zero mean, else +-inf
        out[bad] = np.where(mean[bad] == 0.0, np.nan, np.copysign(np.inf, mean[bad]))
    return out


# --- shrinkage --------------------------------------------------------------------------------

@dataclass(frozen=True)
class EbResult:
    """Empirical-Bayes normal-normal partial pooling of cell estimates (ADR 0003 step 3)."""

    posterior: dict[Cell, float]
    posterior_sd: dict[Cell, float]
    shrinkage: dict[Cell, float]  # B = se^2 / (se^2 + tau^2); 1 = fully pooled
    tau2: float
    prior_mean: float
    n_cells: int

    def notes(self) -> dict[str, Any]:
        return {
            "method": "eb_normal_normal_mom",
            "tau2": _f(self.tau2), "prior_mean": _f(self.prior_mean), "n_cells": self.n_cells,
            "posterior": _keyed(self.posterior), "posterior_sd": _keyed(self.posterior_sd),
            "shrinkage": _keyed(self.shrinkage),
        }


def eb_shrink(
    estimates: Mapping[Cell, float],
    std_errors: Mapping[Cell, float],
    prior_mean: float | None = None,
) -> EbResult:
    """Normal-normal shrinkage with method-of-moments tau^2 truncated at 0 (see module docstring).

    Cells with a NaN estimate or standard error are left out of the moments and get NaN back.
    """
    cells = list(estimates)
    if set(cells) != set(std_errors):
        raise ValueError("estimates and std_errors must have the same cells")
    est = {c: float(estimates[c]) for c in cells}
    se = {c: float(std_errors[c]) for c in cells}
    use = [c for c in cells if math.isfinite(est[c]) and math.isfinite(se[c])]
    if len(use) < 2:
        raise ValueError(f"need >= 2 cells with finite estimate and se, got {len(use)}")
    e = np.array([est[c] for c in use])
    v = np.array([se[c] ** 2 for c in use])
    if prior_mean is None:
        mu = float(e.mean())
        spread = float(e.var(ddof=1))
    else:
        mu = float(prior_mean)
        spread = float(np.mean((e - mu) ** 2))
    tau2 = max(0.0, spread - float(v.mean()))

    post, post_sd, shrink = {}, {}, {}
    for c in cells:
        if c not in use:
            post[c] = post_sd[c] = shrink[c] = float("nan")
            continue
        s2 = se[c] ** 2
        b = s2 / (s2 + tau2) if (s2 + tau2) > 0 else 1.0
        shrink[c] = b
        post[c] = mu + (1.0 - b) * (est[c] - mu)
        post_sd[c] = math.sqrt(s2 * (1.0 - b))
    return EbResult(post, post_sd, shrink, tau2, mu, len(use))


def shrink_directional(
    estimates: Mapping[Cell, float],
    std_errors: Mapping[Cell, float],
    signs: Mapping[Cell, Sign],
    discovery_t: Mapping[Cell, float],
) -> tuple[EbResult, dict[Cell, float]]:
    """EB-pool the screen's cell ICs, then apply each cell's direction (see module docstring).

    `estimates` are raw mean ICs. Returns the EbResult over the pooled values (sign * IC for
    signed cells, IC for two-sided ones) and each cell's posterior in its direction.
    """
    cells = list(estimates)
    sgn = {c: _check_sign(signs[c]) for c in cells}
    pooled = {c: float(estimates[c]) if sgn[c] is None else sgn[c] * float(estimates[c])
              for c in cells}
    eb = eb_shrink(pooled, std_errors)
    directional: dict[Cell, float] = {}
    for c in cells:
        post = eb.posterior[c]
        if sgn[c] is not None:
            directional[c] = post
            continue
        t = float(discovery_t.get(c, float("nan")))
        directional[c] = (math.copysign(1.0, t) * post if math.isfinite(t) and t != 0.0
                          else float("nan"))
    return eb, directional


# --- multiple testing -------------------------------------------------------------------------

def benjamini_hochberg(pvals: Mapping[Cell, float], q: float) -> set[Cell]:
    """Benjamini-Hochberg step-up at FDR level q: the cells with the k smallest p, k maximal
    with p_(k) <= k q / m. NaN p-values count in m and are never discovered."""
    _check_level(q, "q")
    ranked, m = _ranked(pvals)
    k_max = 0
    for k, (_, p) in enumerate(ranked, start=1):
        if p <= k * q / m:
            k_max = k
    return {c for c, _ in ranked[:k_max]}


def holm(pvals: Mapping[Cell, float], alpha: float) -> set[Cell]:
    """Holm step-down at FWER alpha: reject p_(i) <= alpha / (m - i + 1) until the first failure."""
    _check_level(alpha, "alpha")
    ranked, m = _ranked(pvals)
    out: set[Cell] = set()
    for i, (c, p) in enumerate(ranked, start=1):
        if p > alpha / (m - i + 1):
            break
        out.add(c)
    return out


def _ranked(pvals: Mapping[Cell, float]) -> tuple[list[tuple[Cell, float]], int]:
    m = len(pvals)
    finite = [(c, float(p)) for c, p in pvals.items() if not math.isnan(float(p))]
    return sorted(finite, key=lambda cp: cp[1]), m


def _check_level(level: float, name: str) -> None:
    if not 0.0 < level <= 1.0:
        raise ValueError(f"{name} must be in (0, 1], got {level!r}")


# --- confirmation -----------------------------------------------------------------------------

@dataclass(frozen=True)
class CellVerdict:
    """Confirmation-period outcome for one discovered cell (ADR 0003 step 5)."""

    cell: Cell
    direction: float
    ic_mean: float
    t: float
    p_one_sided: float
    holm_reject: bool
    spread: float  # mean top-minus-bottom spread (raw, not directional)
    cost: float  # 4 * cost_bps * 1e-4 * min(1, horizon * turnover)
    net_spread: float  # direction * spread - cost
    confirmed: bool
    reason: str

    def notes(self) -> dict[str, Any]:
        return {
            "cell": str(self.cell), "direction": self.direction, "ic_mean": _f(self.ic_mean),
            "t": _f(self.t), "p_one_sided": _f(self.p_one_sided), "holm_reject": self.holm_reject,
            "spread": _f(self.spread), "cost": _f(self.cost), "net_spread": _f(self.net_spread),
            "confirmed": self.confirmed, "reason": self.reason,
        }


@dataclass(frozen=True)
class ConfirmResult:
    verdicts: dict[Cell, CellVerdict]
    alpha: float
    cost_bps: float

    @property
    def confirmed(self) -> set[Cell]:
        return {c for c, v in self.verdicts.items() if v.confirmed}

    def notes(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha, "cost_bps": self.cost_bps, "n_discovered": len(self.verdicts),
            "confirmed": sorted(str(c) for c in self.confirmed),
            "verdicts": {str(c): v.notes() for c, v in self.verdicts.items()},
        }


def confirm(
    discovered: Iterable[Cell],
    ic_conf: Mapping[Cell, pd.Series],
    signs: Mapping[Cell, Sign],
    spreads: Mapping[Cell, float],
    turnovers: Mapping[Cell, float],
    cost_bps: float,
    horizon_of: Mapping[Cell, int],
    discovery_t: Mapping[Cell, float] | None = None,
    alpha: float = 0.05,
) -> ConfirmResult:
    """ADR 0003 step 5 for every discovered cell, Holm across the discovered set.

    `spreads[c]` is the mean confirmation-period top-minus-bottom quantile spread per holding
    period (evaluate.quantile_spread mean); `turnovers[c]` the daily quantile turnover
    (evaluate.quantile_turnover). `discovery_t` is required for two-sided (sign None) cells: the
    confirmation must hold in the direction found on discovery data.
    """
    cells = list(discovered)
    for c in cells:
        for name, d in (("ic_conf", ic_conf), ("signs", signs), ("spreads", spreads),
                        ("turnovers", turnovers), ("horizon_of", horizon_of)):
            if c not in d:
                raise ValueError(f"discovered cell {c!r} missing from {name}")
    stats: dict[Cell, tuple[float, float, float, float]] = {}
    for c in cells:
        sign = _check_sign(signs[c])
        if sign is None:
            dt = None if discovery_t is None else discovery_t.get(c)
            if dt is None or not math.isfinite(float(dt)) or float(dt) == 0.0:
                raise ValueError(f"two-sided cell {c!r} needs a non-zero discovery_t")
            sign = 1.0 if float(dt) > 0 else -1.0
        ic = pd.Series(ic_conf[c], dtype=float).dropna()
        h = int(horizon_of[c])
        t = newey_west_t(ic, horizon=h)
        ic_mean = float(ic.mean()) if len(ic) else float("nan")
        p = float(norm.sf(sign * t)) if not math.isnan(t) else float("nan")
        stats[c] = (sign, t, ic_mean, p)
    rejected = holm({c: s[3] for c, s in stats.items()}, alpha)

    verdicts: dict[Cell, CellVerdict] = {}
    for c in cells:
        sign, t, ic_mean, p = stats[c]
        h = int(horizon_of[c])
        turnover = float(turnovers[c])
        period_turnover = min(1.0, h * turnover) if math.isfinite(turnover) else 1.0
        cost = 4.0 * float(cost_bps) * 1e-4 * period_turnover
        spread = float(spreads[c])
        net = sign * spread - cost
        reasons = []
        if not (math.isfinite(ic_mean) and sign * ic_mean > 0):
            reasons.append(f"IC mean {ic_mean:.5f} not in direction {sign:+.0f}")
        if c not in rejected:
            reasons.append(f"one-sided p={p:.4f} not rejected by Holm at {alpha} over {len(cells)}")
        if not (math.isfinite(net) and net > 0):
            reasons.append(f"directional spread {sign * spread:.5f} - cost {cost:.5f} = {net:.5f} <= 0")
        verdicts[c] = CellVerdict(
            cell=c, direction=sign, ic_mean=ic_mean, t=float(t), p_one_sided=p,
            holm_reject=c in rejected, spread=spread, cost=cost, net_spread=net,
            confirmed=not reasons, reason="confirmed" if not reasons else "; ".join(reasons),
        )
    return ConfirmResult(verdicts, float(alpha), float(cost_bps))


# --- helpers ----------------------------------------------------------------------------------

def _check_sign(sign: Sign) -> float | None:
    if sign is None:
        return None
    s = float(sign)
    if s not in (1.0, -1.0):
        raise ValueError(f"sign must be +1, -1 or None, got {sign!r}")
    return s


def _f(x: float) -> float | None:
    """JSON-friendly float: NaN -> None (inf kept as float; json emits Infinity)."""
    x = float(x)
    return None if math.isnan(x) else x


def _keyed(d: Mapping[Cell, float]) -> dict[str, float | None]:
    return {str(c): _f(v) for c, v in d.items()}
