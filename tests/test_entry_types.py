"""Focused unit tests for echelon_engine.atoms.entry_types — Entry dataclass leaf.

No IO, no DB. Tests the dataclass and its __post_init__ logic in isolation.
"""
import pytest

from echelon_engine.atoms.entry_types import Entry
from echelon_engine.atoms.addressing import content_id, domain_of


def test_entry_auto_derives_domain_from_coordinate():
    e = Entry(content="c", coordinate="tooling:cli")
    assert e.domain == "tooling"


def test_entry_auto_fills_id():
    e = Entry(content="c", coordinate="tooling:cli")
    expected_id = content_id("c", "tooling", "atom")
    assert e.id == expected_id


def test_entry_explicit_domain_wins_over_coordinate():
    e = Entry(content="c", domain="identity", coordinate="x:y")
    assert e.domain == "identity"


def test_entry_defaults():
    e = Entry(content="c")
    assert e.kind == "atom"
    assert e.permanent is False
    assert e.scope == ""
    assert e.supersedes == ""
    assert e.ttl == 0
    assert e.valence == 0.0
    assert e.arousal == 0.0
    assert e.score == 100.0
    assert e.recall_count == 0
    assert e.self_seed is False
    assert e.family == ""


def test_entry_general_domain_triggers_derivation():
    # default domain="general" -> should be replaced by coordinate derivation
    e = Entry(content="c", domain="general", coordinate="workflow:x")
    assert e.domain == "workflow"


def test_entry_explicit_non_general_domain_kept():
    e = Entry(content="c", domain="identity")
    assert e.domain == "identity"


def test_entry_empty_coordinate_falls_back_to_general():
    e = Entry(content="c", coordinate="")
    assert e.domain == "general"
