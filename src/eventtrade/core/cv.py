"""Cross-validation splits on the trading-day axis of a cross-sectional panel.

Splits are made on *dates*, never on rows: every ticker of a date lands on the same side. The
callers pass the sorted unique trading days they are allowed to use (hold-out already removed).

Purging: a label formed at day t covers returns from t+1 to t+1+h. Training dates within `purge`
days of a test date (before or after) are dropped, so pass `purge=h` (conservative by one day).
Embargo: a further `embargo` training dates are dropped *after* each test block (serial
correlation of features leaking backwards into the test period's information).

The combinatorial scheme reproduces skfolio's `CombinatorialPurgedCV` index arithmetic exactly
(fold boundaries, purge/embargo, path ids); see tests/core/test_cv.py. It is reimplemented here
so that a split carries its dates and each test date's backtest path id.
"""

from collections.abc import Sequence
from itertools import combinations, pairwise
from typing import Any, NamedTuple

import numpy as np


class Split(NamedTuple):
    """One train/test split; `path[i]` is the CPCV backtest path id of `test[i]` (0 for walk-forward)."""

    train: list[Any]
    test: list[Any]
    path: list[int]


def purged_splits(
    dates: Sequence[Any], n_folds: int, n_test_folds: int, purge: int, embargo: int
) -> list[Split]:
    """Combinatorial purged CV (López de Prado) on dates; one split per choice of test folds."""
    dates = _check_dates(dates)
    n = len(dates)
    if n_folds < 3 or not 1 <= n_test_folds < n_folds:
        raise ValueError(f"need n_folds >= 3 and 1 <= n_test_folds < n_folds, got {n_folds}, {n_test_folds}")
    if purge < 0 or embargo < 0:
        raise ValueError("purge and embargo must be >= 0")
    fold_size = n // n_folds
    if fold_size == 0 or purge + embargo >= fold_size:
        raise ValueError(f"purge + embargo ({purge + embargo}) must be < fold size ({fold_size})")

    fold_of = np.minimum(np.arange(n) // fold_size, n_folds - 1)
    test_sets = list(combinations(range(n_folds), n_test_folds))
    # Path id of fold f in split i = how many earlier splits also tested fold f.
    seen = [0] * n_folds
    out: list[Split] = []
    for test_folds in test_sets:
        is_test = np.isin(fold_of, test_folds)
        keep = ~is_test & ~_near_test(is_test, purge, embargo)
        if not keep.any():
            raise ValueError("a split has no training dates left after purge/embargo")
        test_idx = np.flatnonzero(is_test)
        fold_path = {}
        for f in test_folds:
            fold_path[f] = seen[f]
            seen[f] += 1
        out.append(
            Split(
                train=[dates[i] for i in np.flatnonzero(keep)],
                test=[dates[i] for i in test_idx],
                path=[fold_path[int(fold_of[i])] for i in test_idx],
            )
        )
    return out


def walk_forward_splits(
    dates: Sequence[Any], train_days: int, test_days: int, purge: int, embargo: int, expanding: bool
) -> list[Split]:
    """Walk-forward splits: train window, `purge`-day gap, test window; the last test may be short.

    `embargo` only removes training data that *follows* a test block, which walk-forward never has,
    so it is validated and otherwise has no effect.
    """
    dates = _check_dates(dates)
    if train_days < 1 or test_days < 1 or purge < 0 or embargo < 0:
        raise ValueError("train_days, test_days must be >= 1; purge, embargo >= 0")
    n = len(dates)
    out: list[Split] = []
    test_start = train_days + purge
    while test_start < n:
        train_end = test_start - purge
        train_start = 0 if expanding else train_end - train_days
        test_end = min(test_start + test_days, n)
        test = dates[test_start:test_end]
        out.append(Split(train=dates[train_start:train_end], test=test, path=[0] * len(test)))
        test_start = test_end
    return out


def _check_dates(dates: Sequence[Any]) -> list[Any]:
    out = list(dates)
    if any(a >= b for a, b in pairwise(out)):
        raise ValueError("dates must be sorted ascending and unique")
    return out


def _near_test(is_test: np.ndarray, purge: int, embargo: int) -> np.ndarray:
    """Mask of dates within `purge` before, or `purge + embargo` after, some test date."""
    n = len(is_test)
    near = np.zeros(n, dtype=bool)
    test_idx = np.flatnonzero(is_test)
    for k in range(1, purge + 1):
        near[np.clip(test_idx - k, 0, n - 1)] = True
    for k in range(1, purge + embargo + 1):
        near[np.clip(test_idx + k, 0, n - 1)] = True
    return near & ~is_test
