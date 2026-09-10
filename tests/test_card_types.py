"""Tests for echelon_engine.atoms.card_types — Atom and Card dataclasses (pure leaf, no DB)."""
import time
import pytest

from echelon_engine.atoms.card_types import Atom, Card
from echelon_engine.atoms.uame import SCORE_BENCHMARK
from echelon_engine.atoms.card_credit import SYNTH_ELIGIBLE, BEACON_THRESHOLD


# ── Atom ──────────────────────────────────────────────────────────────────

def test_atom_born_at_benchmark():
    a = Atom(coordinate="tooling:cli", content="use -X utf8")
    assert a.score == SCORE_BENCHMARK
    assert a.use_count == 0


def test_atom_coordinate_normalised():
    a = Atom(coordinate="Tooling:CLI Flag", content="x")
    assert a.coordinate == "tooling:cli_flag"


def test_atom_id_content_addressed():
    a1 = Atom(coordinate="tooling:x", content="same")
    a2 = Atom(coordinate="tooling:x", content="same")
    assert a1.id == a2.id


def test_atom_id_differs_on_different_content():
    a1 = Atom(coordinate="tooling:x", content="alpha")
    a2 = Atom(coordinate="tooling:x", content="beta")
    assert a1.id != a2.id


def test_atom_tier_unproven_at_birth():
    a = Atom(coordinate="tooling:x", content="x")
    assert a.tier == "unproven"


def test_atom_tier_eligible():
    a = Atom(coordinate="tooling:x", content="x")
    a.score = SYNTH_ELIGIBLE
    assert a.tier == "eligible"


def test_atom_tier_beacon():
    a = Atom(coordinate="tooling:x", content="x")
    a.score = BEACON_THRESHOLD
    assert a.tier == "beacon"


def test_atom_rich_fields_default_empty():
    a = Atom(coordinate="tooling:x", content="x")
    assert a.scope == ""
    assert a.kind == ""
    assert a.valence == 0.0
    assert a.arousal == 0.0


def test_atom_ts_is_recent():
    before = int(time.time())
    a = Atom(coordinate="tooling:x", content="x")
    after = int(time.time())
    assert before <= a.ts <= after


# ── Card ──────────────────────────────────────────────────────────────────

def test_card_refs_normalised():
    c = Card(label="do-thing", refs=["Tooling:CLI", "action:read FILE"])
    assert c.refs == ["tooling:cli", "action:read_file"]


def test_card_empty_ref_dropped():
    c = Card(label="do-thing", refs=["tooling:a", "", "   "])
    assert c.refs == ["tooling:a"]


def test_card_id_content_addressed_on_label_and_refs():
    c1 = Card(label="do-thing", refs=["tooling:a"])
    c2 = Card(label="do-thing", refs=["tooling:a"])
    assert c1.id == c2.id


def test_card_id_does_not_depend_on_prev():
    """Same action recurring in different chains is the SAME card (prev is the edge, not the identity)."""
    c1 = Card(label="do-thing", refs=["tooling:a"], prev="upstream-card-1")
    c2 = Card(label="do-thing", refs=["tooling:a"], prev="upstream-card-2")
    assert c1.id == c2.id


def test_card_born_at_benchmark():
    c = Card(label="do-thing", refs=[])
    assert c.score == SCORE_BENCHMARK
    assert c.use_count == 0
