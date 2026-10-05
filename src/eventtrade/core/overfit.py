"""Overfitting statistics for ADR 0001 gate 3: Deflated Sharpe Ratio and PBO (CSCV).

Pure: no I/O, no clock; the only randomness is the stationary bootstrap, seeded by the caller.

Units. Two Sharpe ratios appear here and they are not interchangeable:
  * per-period SR  = mean(r) / std(r, ddof=1)        -- what PSR, E[max SR] and DSR consume.
  * annualised SR  = per-period SR * sqrt(periods_per_year) -- what `sharpe()` and the report show.
Kurtosis is the RAW (Pearson) fourth standardised moment: 3 for a Normal, not 0.

Sources:
  Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio", J. Portfolio Management.
    Eq. (1)/(6): E[max SR] = sqrt(V[SR]) * ((1-g) Z^-1(1 - 1/N) + g Z^-1(1 - 1/(N e))), g = Euler-Mascheroni.
    Eq. (2): DSR = Z( (SR_hat - SR_0) sqrt(T-1) / sqrt(1 - skew SR_hat + (kurt-1)/4 SR_hat^2) ).
  Bailey, Borwein, Lopez de Prado & Zhu (2017), "The Probability of Backtest Overfitting" (CSCV).
  Politis & Romano (1994), "The Stationary Bootstrap", JASA.

Trial-return frames here are WIDE (index = date, one column per trial id). `to_wide` builds one
from the long TRIAL_RETURN_COLUMNS contract; a missing (trial, date) row means flat, i.e. 0.
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pandas as pd
from scipy import stats

from eventtrade.schemas import TRIAL_RETURN_COLUMNS, OverfitReport, TrialId

EULER_GAMMA = 0.5772156649015329
MIN_PBO_TRIALS = 10


# --- public -----------------------------------------------------------------------------------


def to_wide(trial_returns: pd.DataFrame) -> pd.DataFrame:
    """Pivot long TRIAL_RETURN_COLUMNS to date x trial_id, filling missing days with 0 (flat)."""
    missing = set(TRIAL_RETURN_COLUMNS) - set(trial_returns.columns)
    if missing:
        raise ValueError(f"trial_returns missing columns {sorted(missing)}")
    wide = trial_returns.pivot_table(index="date", columns="trial_id", values="ret", aggfunc="sum")
    return wide.sort_index().fillna(0.0)


def sharpe(returns: pd.Series | np.ndarray, periods_per_year: int = 252) -> float:
    """Annualised Sharpe (ddof=1, zero risk-free); 0.0 for zero vol, NaN for < 2 observations."""
    return _per_period_sharpe(np.asarray(returns, dtype=float)) * math.sqrt(periods_per_year)


def probabilistic_sharpe(
    sr_hat: float, sr_benchmark: float, n_obs: int, skew: float, kurtosis: float
) -> float:
    """PSR = P(true SR > benchmark); SRs PER-PERIOD (not annualised), kurtosis RAW (Normal = 3)."""
    if n_obs < 2:
        return float("nan")
    var_term = 1.0 - skew * sr_hat + (kurtosis - 1.0) / 4.0 * sr_hat**2
    if var_term <= 0:
        # Moments so extreme the asymptotic variance is non-positive: the estimate is meaningless.
        return float("nan")
    z = (sr_hat - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(var_term)
    return float(stats.norm.cdf(z))


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max SR] of n_trials null (zero-mean) trials with SR variance var_sr; 0.0 if n_trials <= 1."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    q1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
    q2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(var_sr) * ((1.0 - EULER_GAMMA) * q1 + EULER_GAMMA * q2))


def deflated_sharpe(
    trial_returns_wide: pd.DataFrame, n_trials: int | None = None
) -> tuple[TrialId, float]:
    """(best trial by per-period SR, its DSR); N for E[max SR] = n_trials, default the column count.

    Pass `n_trials` when the columns are survivors of an earlier selection (e.g. signal-stage
    trials with no return series): N must count every configuration tried. V[SR] comes from the
    columns; when N exceeds the column count it is floored at 1/(T-1), the null sampling
    variance of a per-period SR, because a few survivors cannot estimate the dispersion of the
    whole search (one column has none). N below the column count is refused.
    """
    m = _matrix(trial_returns_wide)
    n_eff = m.shape[1] if n_trials is None else int(n_trials)
    if n_eff < m.shape[1]:
        raise ValueError(f"n_trials {n_eff} is below the number of return columns {m.shape[1]}")
    srs = np.array([_per_period_sharpe(m[:, j]) for j in range(m.shape[1])])
    best = int(np.nanargmax(srs))
    finite = srs[np.isfinite(srs)]
    var_sr = float(np.var(finite, ddof=1)) if finite.size > 1 else 0.0
    if n_eff > m.shape[1] and m.shape[0] > 1:
        var_sr = max(var_sr, 1.0 / (m.shape[0] - 1))
    sr0 = expected_max_sharpe(n_eff, var_sr)
    skew, kurt = _skew_kurt(m[:, best])
    dsr = probabilistic_sharpe(float(srs[best]), sr0, m.shape[0], skew, kurt)
    return TrialId(str(trial_returns_wide.columns[best])), dsr


def pbo_cscv(trial_returns_wide: pd.DataFrame, n_splits: int = 16) -> float | None:
    """Probability of backtest overfitting via CSCV; None if < 10 trials or < 2 rows per split."""
    if n_splits < 2 or n_splits % 2:
        raise ValueError("n_splits must be an even integer >= 2")
    m = _matrix(trial_returns_wide)
    n_rows, n_trials = m.shape
    rows_per_block = n_rows // n_splits
    if n_trials < MIN_PBO_TRIALS or rows_per_block < 2:
        return None
    # Equal contiguous blocks; trailing remainder rows (< n_splits) are dropped.
    blocks = m[: rows_per_block * n_splits].reshape(n_splits, rows_per_block, n_trials)
    s1 = blocks.sum(axis=1)  # (S, N) sufficient statistics per block
    s2 = (blocks**2).sum(axis=1)
    combos = np.array(
        [[i in c for i in range(n_splits)] for c in combinations(range(n_splits), n_splits // 2)],
        dtype=float,
    )  # (C, S) 1 = block is in-sample
    n_half = rows_per_block * n_splits // 2
    sr_is = _sharpe_from_sums(combos @ s1, combos @ s2, n_half)
    sr_oos = _sharpe_from_sums((1 - combos) @ s1, (1 - combos) @ s2, n_half)
    best_is = np.argmax(sr_is, axis=1)
    oos_ranks = stats.rankdata(sr_oos, axis=1, method="average")  # 1 = worst
    omega = oos_ranks[np.arange(len(combos)), best_is] / (n_trials + 1)
    logits = np.log(omega / (1.0 - omega))
    return float(np.mean(logits <= 0))


def stationary_bootstrap_sharpe_ci(
    returns: pd.Series | np.ndarray,
    n_boot: int,
    mean_block: float,
    seed: int,
    periods_per_year: int = 252,
    level: float = 0.95,
) -> tuple[float, float]:
    """Percentile CI of the annualised Sharpe from a Politis-Romano stationary bootstrap."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan"), float("nan")
    idx = _stationary_indices(r.size, n_boot, mean_block, np.random.default_rng(seed))
    sample = r[idx]
    mu = sample.mean(axis=1)
    sd = sample.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        srs = np.where(sd > 0, mu / sd, 0.0) * math.sqrt(periods_per_year)
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(srs, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def overfit_report(
    trial_returns_wide: pd.DataFrame,
    seed: int,
    n_boot: int = 2000,
    mean_block: float = 10.0,
    periods_per_year: int = 252,
    n_trials: int | None = None,
) -> OverfitReport:
    """Gate-3 report: best trial, its annualised Sharpe and 95% CI, DSR over n_trials, and PBO."""
    best_id, dsr = deflated_sharpe(trial_returns_wide, n_trials)
    col = [str(c) for c in trial_returns_wide.columns].index(best_id)
    best_r = _matrix(trial_returns_wide)[:, col]
    return OverfitReport(
        n_trials=trial_returns_wide.shape[1] if n_trials is None else int(n_trials),
        best_trial_id=best_id,
        best_sharpe=sharpe(best_r, periods_per_year),
        deflated_sharpe=dsr,
        pbo=pbo_cscv(trial_returns_wide),
        sharpe_ci95=stationary_bootstrap_sharpe_ci(
            best_r, n_boot, mean_block, seed, periods_per_year, 0.95
        ),
    )


# --- helpers ----------------------------------------------------------------------------------


def _matrix(trial_returns_wide: pd.DataFrame) -> np.ndarray:
    if trial_returns_wide.shape[1] == 0 or trial_returns_wide.shape[0] == 0:
        raise ValueError("trial_returns_wide must have at least one row and one trial column")
    return trial_returns_wide.fillna(0.0).to_numpy(dtype=float)


def _per_period_sharpe(r: np.ndarray) -> float:
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan")
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(r.mean() / sd)


def _sharpe_from_sums(s1: np.ndarray, s2: np.ndarray, n: int) -> np.ndarray:
    mean = s1 / n
    var = np.maximum((s2 - n * mean**2) / (n - 1), 0.0)
    sd = np.sqrt(var)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sd > 1e-15, mean / sd, 0.0)


def _skew_kurt(r: np.ndarray) -> tuple[float, float]:
    r = r[np.isfinite(r)]
    if r.size < 3 or r.std() == 0:
        return 0.0, 3.0  # degenerate: fall back to Normal moments
    return float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))


def _stationary_indices(
    n: int, n_boot: int, mean_block: float, rng: np.random.Generator
) -> np.ndarray:
    """(n_boot, n) indices: blocks start uniformly, lengths ~ Geometric(1/mean_block), wrap mod n."""
    p = 1.0 / max(mean_block, 1.0)
    new_block = rng.random((n_boot, n)) < p
    new_block[:, 0] = True
    starts = rng.integers(0, n, size=(n_boot, n))
    pos = np.arange(n)
    last_start = np.maximum.accumulate(np.where(new_block, pos, 0), axis=1)
    rows = np.arange(n_boot)[:, None]
    return (starts[rows, last_start] + (pos - last_start)) % n
