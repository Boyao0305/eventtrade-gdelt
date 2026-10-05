"""Runs the case table in alias_cases.py against core/aliases.py."""

import pytest
from alias_cases import (
    AMBIGUOUS_CASES,
    KEPT_CASES,
    MAKE_CASES,
    NO_ALIAS_NAMES,
    PRIMARY_CASES,
    SAME_ALIAS_CASES,
)

from eventtrade.core.aliases import is_ambiguous, make_aliases


def _kept(name: str, ticker: str) -> list[str]:
    return [a for a in make_aliases(name, ticker) if not is_ambiguous(a)]


@pytest.mark.parametrize(("name", "ticker", "keep", "drop"), KEPT_CASES)
def test_kept_aliases(name: str, ticker: str, keep: set[str], drop: set[str]) -> None:
    kept = set(_kept(name, ticker))
    assert keep - kept == set(), f"missing for {name!r}"
    assert drop & kept == set(), f"unexpected for {name!r}"
    # every must_drop alias is actually generated, so the filter (not generation) drops it
    assert drop <= set(make_aliases(name, ticker)), f"not generated for {name!r}"


@pytest.mark.parametrize(("name", "ticker"), NO_ALIAS_NAMES)
def test_no_alias_survives(name: str, ticker: str) -> None:
    assert _kept(name, ticker) == []


@pytest.mark.parametrize(("name", "ticker", "primary"), PRIMARY_CASES)
def test_primary_alias_is_the_core(name: str, ticker: str, primary: str) -> None:
    assert make_aliases(name, ticker)[0] == primary


@pytest.mark.parametrize(("name", "ticker", "include", "exclude"), MAKE_CASES)
def test_make_aliases(name: str, ticker: str, include: set[str], exclude: set[str]) -> None:
    aliases = set(make_aliases(name, ticker))
    assert include - aliases == set(), f"missing for {name!r}"
    assert exclude & aliases == set(), f"unexpected for {name!r}"


@pytest.mark.parametrize(("a", "b"), SAME_ALIAS_CASES)
def test_share_classes_share_aliases(a: tuple[str, str], b: tuple[str, str]) -> None:
    assert make_aliases(*a) == make_aliases(*b)


@pytest.mark.parametrize(("alias", "expected"), AMBIGUOUS_CASES)
def test_is_ambiguous(alias: str, expected: bool) -> None:
    assert is_ambiguous(alias) is expected


def test_aliases_are_gkg_shaped() -> None:
    """Every alias is lowercase, unique, single-spaced and uses only GKG's character set."""
    for name, ticker, _, _ in MAKE_CASES:
        aliases = make_aliases(name, ticker)
        assert len(aliases) == len(set(aliases))
        for a in aliases:
            assert a == " ".join(a.split()) and a.strip("-") == a
            assert all(c.isascii() and (c.islower() or c.isdigit() or c in " -") for c in a), a
