"""Focused unit tests for echelon_engine.atoms.scope_map — scope routing leaf.

No IO, no DB. Tests scope_to_domain in isolation.
"""
import pytest

from echelon_engine.atoms.scope_map import scope_to_domain, _SCOPE_DOMAIN


def test_coordinate_wins_over_scope():
    assert scope_to_domain("anyscope", "tooling:bash:gzip") == "tooling"


def test_explicit_scope_map_entries():
    for scope, expected_domain in _SCOPE_DOMAIN.items():
        assert scope_to_domain(scope) == expected_domain


def test_testish_prefixes_route_to_test():
    for prefix in ("test-", "grow-", "wire-", "decay-", "promo", "tune-", "synth", "final-"):
        assert scope_to_domain(prefix + "x") == "test"


def test_plain_scope_sanitized():
    assert scope_to_domain("my scope") == "my_scope"


def test_empty_scope_returns_general():
    assert scope_to_domain("") == "general"


def test_no_coordinate_empty_string_handled():
    # passing coordinate="" should be equivalent to omitting it
    assert scope_to_domain("test-foo", "") == "test"


def test_coordinate_overrides_testish_scope():
    # even a testish scope loses to a coordinate
    assert scope_to_domain("test-x", "identity:self") == "identity"
