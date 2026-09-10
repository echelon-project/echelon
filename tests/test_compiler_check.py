"""Focused tests for echelon_sdk.compiler.check — intent pool validation."""
from __future__ import annotations

import sys
sys.path.insert(0, ".")

from echelon_sdk.compiler.intent import Intent
from echelon_sdk.compiler.check import check, Conflict


def make_intent(id, deps=None, canonical_for=None):
    """Shortcut for creating a minimal valid intent."""
    return Intent(
        id=id,
        part_of="test-wave",
        target_path=f"out/{id}.txt",
        action="create",
        body=f"body of {id}",
        deps=deps if deps is not None else [],
        canonical_for=canonical_for,
    )


# --- PASS cases ---------------------------------------------------------------

def test_pass_valid_pool():
    intents = [
        make_intent("a", deps=[]),
        make_intent("b", deps=["a"]),
        make_intent("c", deps=["a", "b"]),
    ]
    conflicts = check(intents)
    assert conflicts == [], f"expected PASS, got {conflicts}"


def test_pass_empty_pool():
    assert check([]) == []


def test_pass_single_intent_no_deps():
    assert check([make_intent("only")]) == []


# --- DUPLICATE ID -------------------------------------------------------------

def test_fail_duplicate_id():
    intents = [
        make_intent("dup"),
        make_intent("dup"),
    ]
    conflicts = check(intents)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "duplicate_id"
    assert conflicts[0].involved == ["dup"]


# --- UNRESOLVED DEP -----------------------------------------------------------

def test_fail_unresolved_dep():
    intents = [
        make_intent("a", deps=["ghost"]),
    ]
    conflicts = check(intents)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "unresolved_dep"
    assert "ghost" in conflicts[0].involved
    assert "a" in conflicts[0].involved


def test_fail_unresolved_dep_multi():
    intents = [
        make_intent("a", deps=[]),
        make_intent("b", deps=["x", "y", "a"]),
    ]
    conflicts = check(intents)
    unresolved = [c for c in conflicts if c.kind == "unresolved_dep"]
    assert len(unresolved) == 2  # x and y


# --- DUPLICATE CANONICAL_FOR --------------------------------------------------

def test_fail_duplicate_canonical_for():
    intents = [
        make_intent("a", canonical_for="anchor.txt"),
        make_intent("b", canonical_for="anchor.txt"),
    ]
    conflicts = check(intents)
    dup = [c for c in conflicts if c.kind == "duplicate_canonical_for"]
    assert len(dup) == 1
    assert set(dup[0].involved) == {"a", "b"}


def test_pass_canonical_for_none_ignored():
    """Multiple intents with canonical_for=None should not conflict."""
    intents = [
        make_intent("a", canonical_for=None),
        make_intent("b", canonical_for=None),
    ]
    conflicts = check(intents)
    dup = [c for c in conflicts if c.kind == "duplicate_canonical_for"]
    assert len(dup) == 0


# --- CYCLES -------------------------------------------------------------------

def test_fail_simple_cycle():
    intents = [
        make_intent("a", deps=["b"]),
        make_intent("b", deps=["a"]),
    ]
    conflicts = check(intents)
    cycles = [c for c in conflicts if c.kind == "cycle"]
    assert len(cycles) == 1
    assert set(cycles[0].involved) == {"a", "b"}


def test_fail_self_loop():
    intents = [
        make_intent("a", deps=["a"]),
    ]
    conflicts = check(intents)
    cycles = [c for c in conflicts if c.kind == "cycle"]
    assert len(cycles) == 1
    assert "a" in cycles[0].involved


def test_fail_longer_cycle():
    intents = [
        make_intent("a", deps=["c"]),
        make_intent("b", deps=["a"]),
        make_intent("c", deps=["b"]),
    ]
    conflicts = check(intents)
    cycles = [c for c in conflicts if c.kind == "cycle"]
    assert len(cycles) == 1
    assert set(cycles[0].involved) == {"a", "b", "c"}


def test_fail_cycle_with_unresolved_dep():
    """Cycle detection still works even if some deps are unresolved."""
    intents = [
        make_intent("a", deps=["b"]),
        make_intent("b", deps=["a", "ghost"]),
    ]
    conflicts = check(intents)
    kinds = {c.kind for c in conflicts}
    assert "cycle" in kinds
    assert "unresolved_dep" in kinds


# --- MULTIPLE CONFLICTS -------------------------------------------------------

def test_fail_multiple_kinds():
    intents = [
        make_intent("dup"),
        make_intent("dup"),
        make_intent("a", deps=["ghost"]),
    ]
    conflicts = check(intents)
    kinds = {c.kind for c in conflicts}
    assert "duplicate_id" in kinds
    assert "unresolved_dep" in kinds


# --- CHECK() RETURNS List[Conflict] -------------------------------------------

def test_check_return_type():
    """Smoke test: check always returns a list."""
    result = check([make_intent("x")])
    assert isinstance(result, list)


# --- Conflict dataclass fields ------------------------------------------------

def test_conflict_fields():
    c = Conflict(kind="test", detail="some detail", involved=["a", "b"])
    assert c.kind == "test"
    assert c.detail == "some detail"
    assert c.involved == ["a", "b"]


if __name__ == "__main__":
    # PASS
    test_pass_valid_pool()
    test_pass_empty_pool()
    test_pass_single_intent_no_deps()
    # FAIL: duplicate id
    test_fail_duplicate_id()
    # FAIL: unresolved dep
    test_fail_unresolved_dep()
    test_fail_unresolved_dep_multi()
    # FAIL: duplicate canonical_for
    test_fail_duplicate_canonical_for()
    test_pass_canonical_for_none_ignored()
    # FAIL: cycles
    test_fail_simple_cycle()
    test_fail_self_loop()
    test_fail_longer_cycle()
    test_fail_cycle_with_unresolved_dep()
    # multi
    test_fail_multiple_kinds()
    # smoke
    test_check_return_type()
    test_conflict_fields()
    print("ALL TESTS PASSED")
