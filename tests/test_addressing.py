"""Focused unit tests for echelon_engine.atoms.addressing — content-addressing leaf.

No IO, no DB. content_id and domain_of tested in isolation.
"""
import pytest

from echelon_engine.atoms.addressing import content_id, domain_of


# ── content_id ───────────────────────────────────────────────────────────────

def test_content_id_is_deterministic():
    a = content_id("hello", "tooling", "atom")
    b = content_id("hello", "tooling", "atom")
    assert a == b


def test_content_id_is_16_hex_chars():
    cid = content_id("x", "d", "atom")
    assert len(cid) == 16
    assert all(c in "0123456789abcdef" for c in cid)


def test_content_id_keys_on_content():
    base = content_id("x", "d", "atom")
    assert content_id("y", "d", "atom") != base


def test_content_id_keys_on_domain():
    base = content_id("x", "d", "atom")
    assert content_id("x", "e", "atom") != base


def test_content_id_keys_on_kind():
    base = content_id("x", "d", "atom")
    assert content_id("x", "d", "card") != base


# ── domain_of ────────────────────────────────────────────────────────────────

def test_domain_of_takes_first_segment():
    assert domain_of("tooling:cli:flag") == "tooling"


def test_domain_of_sanitizes_spaces_to_underscores():
    assert domain_of("Work Flow:x") == "work_flow"


def test_domain_of_lowercases():
    assert domain_of("TOOLING:x") == "tooling"


def test_domain_of_empty_coordinate_returns_fallback():
    assert domain_of("") == "general"


def test_domain_of_custom_fallback():
    assert domain_of("", fallback="raw") == "raw"


def test_domain_of_strips_leading_trailing_underscores():
    # a coordinate starting with a non-alnum char should not leave a dangling underscore
    result = domain_of("-bad:x")
    assert not result.startswith("_")
    assert not result.endswith("_")
