"""Case tables for core/cv.py. Dates are plain ints (0..n-1) unless stated: only order matters."""

from itertools import product
from math import comb

import numpy as np
import pandas as pd
import pytest

from eventtrade.core.cv import Split, purged_splits, walk_forward_splits


def R(a: int, b: int) -> list[int]:
    return list(range(a, b))


# --- CPCV: fold layout, purge before/after a test block, embargo only after it -----------------
# 12 dates, 3 folds of 4, 2 test folds -> 3 splits. Expected rows are skfolio's documented output.
CPCV_CASES = [
    # (n_folds, n_test_folds, purge, embargo, [(train, test, path), ...])
    (3, 2, 0, 0, [(R(8, 12), R(0, 8), [0] * 8),
                  (R(4, 8), R(0, 4) + R(8, 12), [1] * 4 + [0] * 4),
                  (R(0, 4), R(4, 12), [1] * 8)]),
    # purge=1 drops one date on each side of every test block.
    (3, 2, 1, 0, [(R(9, 12), R(0, 8), [0] * 8),
                  ([5, 6], R(0, 4) + R(8, 12), [1] * 4 + [0] * 4),
                  ([0, 1, 2], R(4, 12), [1] * 8)]),
    # embargo=1 drops one date after a test block only.
    (3, 2, 0, 1, [(R(9, 12), R(0, 8), [0] * 8),
                  ([5, 6, 7], R(0, 4) + R(8, 12), [1] * 4 + [0] * 4),
                  ([0, 1, 2, 3], R(4, 12), [1] * 8)]),
    # one test fold = purged k-fold; every fold is path 0. Last fold absorbs the remainder (n=12, 3 folds of 4).
    (3, 1, 1, 1, [(R(6, 12), R(0, 4), [0] * 4),
                  ([0, 1, 2, 10, 11], R(4, 8), [0] * 4),
                  (R(0, 7), R(8, 12), [0] * 4)]),
]


@pytest.mark.parametrize("n_folds,n_test,purge,embargo,expected", CPCV_CASES)
def test_cpcv_cases(n_folds, n_test, purge, embargo, expected):
    got = purged_splits(R(0, 12), n_folds, n_test, purge, embargo)
    assert [tuple(s) for s in got] == expected


# --- CPCV invariants on a grid: disjoint, purge gap, embargo gap, every path covers each date once --
GRID = [(n, f, t, p, e) for n, f, t, p, e in product([30, 47], [4, 6], [1, 2, 3], [0, 2], [0, 1]) if t < f]


@pytest.mark.parametrize("n,n_folds,n_test,purge,embargo", GRID)
def test_cpcv_invariants(n, n_folds, n_test, purge, embargo):
    dates = list(pd.bdate_range("2020-01-01", periods=n))
    pos = {d: i for i, d in enumerate(dates)}
    splits = purged_splits(dates, n_folds, n_test, purge, embargo)
    assert len(splits) == comb(n_folds, n_test)
    for sp in splits:
        tr, te = np.array([pos[d] for d in sp.train]), np.array([pos[d] for d in sp.test])
        assert len(tr) and len(te) and not set(tr) & set(te)
        assert len(sp.path) == len(sp.test)
        diff = tr[:, None] - te[None, :]  # train minus test position
        assert not ((diff < 0) & (diff >= -purge)).any()  # purge before
        assert not ((diff > 0) & (diff <= purge + embargo)).any()  # purge + embargo after
    n_paths = comb(n_folds, n_test) * n_test // n_folds
    for p in range(n_paths):
        covered = [d for sp in splits for d, q in zip(sp.test, sp.path) if q == p]
        assert sorted(covered) == dates


# --- CPCV equals skfolio's CombinatorialPurgedCV on a single-asset series --------------------
SKF_GRID = [(n, f, t, p, e) for n, f, t, p, e in product([40, 53], [3, 5, 6], [2, 3], [0, 1, 3], [0, 2])
            if t < f and p + e < n // f - 1]


@pytest.mark.parametrize("n,n_folds,n_test,purge,embargo", SKF_GRID)
def test_cpcv_matches_skfolio(n, n_folds, n_test, purge, embargo):
    from skfolio.model_selection import CombinatorialPurgedCV

    cv = CombinatorialPurgedCV(n_folds=n_folds, n_test_folds=n_test, purged_size=purge, embargo_size=embargo)
    ref = list(cv.split(np.zeros((n, 1))))
    ref_paths = cv.get_path_ids()
    if any(len(train) == 0 for train, _ in ref):
        # skfolio yields an empty training set here; we refuse instead of fitting on nothing.
        with pytest.raises(ValueError):
            purged_splits(R(0, n), n_folds, n_test, purge, embargo)
        return
    got = purged_splits(R(0, n), n_folds, n_test, purge, embargo)
    assert len(got) == len(ref)
    for i, (sp, (train, tests)) in enumerate(zip(got, ref)):
        assert sp.train == train.tolist()
        assert sp.test == np.concatenate(tests).tolist()
        assert sp.path == [int(ref_paths[i, j]) for j, t in enumerate(tests) for _ in t]


# --- CPCV rejects what it cannot split -------------------------------------------------------
@pytest.mark.parametrize("dates,args", [
    ([0, 2, 1, 3, 4, 5, 6, 7, 8], (3, 1, 0, 0)),  # unsorted
    ([0, 1, 1, 3, 4, 5, 6, 7, 8], (3, 1, 0, 0)),  # duplicate
    (R(0, 12), (2, 1, 0, 0)),  # fewer than 3 folds
    (R(0, 12), (3, 3, 0, 0)),  # no training folds
    (R(0, 12), (3, 1, 3, 1)),  # purge + embargo >= fold size (4)
])
def test_cpcv_rejects(dates, args):
    with pytest.raises(ValueError):
        purged_splits(dates, *args)


# --- Walk-forward: train window, purge gap, test window; last test may be short --------------
WF_CASES = [
    # (n, train, test, purge, embargo, expanding, [(train, test), ...])
    (6, 2, 1, 0, 0, False, [([0, 1], [2]), ([1, 2], [3]), ([2, 3], [4]), ([3, 4], [5])]),
    # purge leaves a gap of `purge` dates between train end and test start
    (6, 2, 1, 1, 0, False, [([0, 1], [3]), ([1, 2], [4]), ([2, 3], [5])]),
    # short final test window is kept
    (6, 3, 2, 0, 0, False, [([0, 1, 2], [3, 4]), ([2, 3, 4], [5])]),
    # expanding: train always starts at the first date
    (6, 3, 2, 0, 0, True, [([0, 1, 2], [3, 4]), ([0, 1, 2, 3, 4], [5])]),
    (10, 3, 2, 2, 0, True, [(R(0, 3), [5, 6]), (R(0, 5), [7, 8]), (R(0, 7), [9])]),
    # embargo has no effect: walk-forward never trains on dates after a test block
    (10, 3, 2, 2, 5, True, [(R(0, 3), [5, 6]), (R(0, 5), [7, 8]), (R(0, 7), [9])]),
    # not enough dates for one test date
    (4, 3, 1, 1, 0, False, []),
]


@pytest.mark.parametrize("n,train,test,purge,embargo,expanding,expected", WF_CASES)
def test_walk_forward_cases(n, train, test, purge, embargo, expanding, expected):
    got = walk_forward_splits(R(0, n), train, test, purge, embargo, expanding)
    assert [(s.train, s.test) for s in got] == expected
    assert all(s.path == [0] * len(s.test) for s in got)


# --- Walk-forward equals skfolio's WalkForward(reduce_test=True) on a single-asset series ----
@pytest.mark.parametrize("n,train,test,purge,expanding",
                         list(product([20, 37], [5, 8], [1, 3, 7], [0, 1, 4], [False, True])))
def test_walk_forward_matches_skfolio(n, train, test, purge, expanding):
    from skfolio.model_selection import WalkForward

    cv = WalkForward(test_size=test, train_size=train, purged_size=purge, expand_train=expanding, reduce_test=True)
    ref = [(a.tolist(), b.tolist()) for a, b in cv.split(np.zeros((n, 1)))]
    got = walk_forward_splits(R(0, n), train, test, purge, 0, expanding)
    assert [(s.train, s.test) for s in got] == ref


def test_split_is_a_named_triple():
    sp = purged_splits(R(0, 12), 3, 2, 0, 0)[0]
    assert isinstance(sp, Split) and sp._fields == ("train", "test", "path")
