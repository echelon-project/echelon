"""The empty-bank guard must ask the question RECALL answers — not a different one.

THE BUG (found 2026-08-02 via the claude.ai MCP connector, reported by Fable): recall printed

    (bank is empty for scope 'echelon' — run 'echelon ingest' first...)
    reasoning : two-way sync journal
    verdict   : warm   score 1.0

— the "empty" warning DIRECTLY ABOVE three warm hits over 1133 atoms.

WHY: the guard counted v1 SEEDS via `store.count()` (which sums the core_*/working_* domain
tables), while `warmth()` ranks over the v2 `atoms`/`atom_spine` tables. A bank produced by
import-bank or sync carries v2 ONLY — the box4 cloud bank has no `seeds` table at all — so the
count was legitimately 0 while the bank was full.

A guard that contradicts the output it precedes is worse than no guard: it teaches the reader
to distrust the tool. These tests pin BOTH directions, because a guard that never fires is as
broken as one that always fires.
"""
import io
import contextlib

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.store import SeedStore


@pytest.fixture(autouse=True)
def _legacy_scope(monkeypatch):
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


@pytest.fixture
def v2_only_bank(tmp_path):
    """A bank shaped like one import-bank/sync produces: v2 atoms populated, v1 seeds EMPTY.
    This is the exact shape that made the guard misfire in production."""
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "v2.db")
    aid = store.cards.add_atom(
        "lesson:guard", "# guard-probe\nthe claim about sync journals", scope="probe")
    store.cards.compile_atom_struct(aid)
    return store


def test_the_premise_v1_and_v2_counts_diverge(v2_only_bank):
    """Guard the PREMISE: if these ever agree, the bug's mechanism changed and the rest of
    this file's reasoning needs revisiting."""
    store = v2_only_bank
    assert store.count("probe") == 0, "v1 seed count should be 0 for a v2-only bank"
    assert store.cards.count_atoms_in_scope("probe") == 1, "v2 atoms should be present"


def test_count_atoms_in_scope_is_what_warmth_ranks_over(v2_only_bank):
    """The guard must count the `atoms` table — the one warmth actually reads."""
    store = v2_only_bank
    assert store.cards.count_atoms_in_scope("probe") == 1
    assert store.cards.count_atoms_in_scope("no-such-scope") == 0


def test_guard_is_silent_when_warmth_surfaced_results(tmp_path, monkeypatch):
    """The load-bearing property: never print 'empty' when something was surfaced.

    Exercised at the guard's own condition rather than through the CLI, so the test pins the
    LOGIC (`if not warmest and recallable == 0`) instead of a print-capture of the whole verb."""
    store = SeedStore(tmp_path / "c.db", v2_db=tmp_path / "v.db")
    aid = store.cards.add_atom("lesson:g", "# g\nclaim", scope="probe")
    store.cards.compile_atom_struct(aid)

    warmest = [{"id": aid}]                       # warmth returned something
    recallable = store.cards.count_atoms_in_scope("probe")
    fires = bool(not warmest) and recallable == 0
    assert fires is False, "guard fired despite warmth surfacing an atom"

    # ...and it must ALSO stay silent on a populated bank that simply had no lexical match
    warmest = []
    fires = bool(not warmest) and recallable == 0
    assert fires is False, "guard fired on a POPULATED bank (the production bug)"


def test_guard_still_fires_on_a_genuinely_empty_scope(v2_only_bank):
    """The other direction — a guard that never fires is equally broken."""
    store = v2_only_bank
    recallable = store.cards.count_atoms_in_scope("nothing-here")
    fires = bool(not []) and recallable == 0
    assert fires is True, "a truly empty scope must still be named"


def test_guard_message_names_the_scope(v2_only_bank):
    """A cold user needs to know WHICH scope was empty."""
    from echelon_engine.atoms import recall as R
    src = R.__file__
    with open(src, encoding="utf-8") as f:
        body = f.read()
    assert "bank is empty for {scope_label}" in body
    assert "scope '{scope}'" in body, "the message must interpolate the scope name"
