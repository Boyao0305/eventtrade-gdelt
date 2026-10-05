"""Runs the case table in alias_cases.py against core/aliases.py."""

import pytest
from alias_cases import AMBIGUOUS_CASES, MAKE_CASES, PRIMARY_CASES, SAME_ALIAS_CASES

from eventtrade.core.aliases import is_ambiguous, make_aliases


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
