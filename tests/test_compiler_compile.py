"""Focused tests for echelon_sdk.compiler.compile — the standalone single writer."""

from __future__ import annotations

import sys
sys.path.insert(0, ".")

import tempfile
from pathlib import Path

from echelon_sdk.compiler.intent import Intent
from echelon_sdk.compiler.intent_pool import IntentPool
from echelon_sdk.compiler.compile import compile, CompilerError


def make_intent(
    id: str,
    deps: list[str] | None = None,
    part_of: str = "test-wave",
    target_path: str | None = None,
    action: str = "create",
    body: str | None = None,
    canonical_for: str | None = None,
) -> Intent:
    if target_path is None:
        target_path = f"test/{id}.txt"
    if body is None:
        body = f"// {id}\n"
    return Intent(
        id=id,
        part_of=part_of,
        target_path=target_path,
        action=action,
        body=body,
        deps=deps or [],
        behavior=None,
        canonical_for=canonical_for,
    )


def test_empty_pool():
    pool = IntentPool()
    with tempfile.TemporaryDirectory() as td:
        result = compile(pool, base_dir=td)
        assert result == [], f"expected empty list, got {result}"
    print("PASS test_empty_pool")


def test_single_intent_writes_file():
    pool = IntentPool()
    i = make_intent(id="hello", body="hello world\n")
    pool.post(i)
    with tempfile.TemporaryDirectory() as td:
        result = compile(pool, base_dir=td)
        assert len(result) == 1, f"expected 1, got {len(result)}"
        assert result[0].id == "hello"
        target = Path(td) / i.target_path
        assert target.exists(), f"expected {target} to exist"
        assert target.read_text(encoding="utf-8") == "hello world\n"
    print("PASS test_single_intent_writes_file")


def test_dep_order_is_respected():
    """a depends on b, b depends on c => write order: c, b, a."""
    pool = IntentPool()
    c = make_intent(id="c", body="c", target_path="c.txt")
    b = make_intent(id="b", body="b", target_path="b.txt", deps=["c"])
    a = make_intent(id="a", body="a", target_path="a.txt", deps=["b"])
    pool.post(a)
    pool.post(b)
    pool.post(c)
    with tempfile.TemporaryDirectory() as td:
        result = compile(pool, base_dir=td)
        ids = [i.id for i in result]
        assert ids == ["c", "b", "a"], f"expected topological order, got {ids}"
        # verify files written
        for intent in [c, b, a]:
            target = Path(td) / intent.target_path
            assert target.read_text(encoding="utf-8") == intent.body
    print("PASS test_dep_order_is_respected")


def test_part_of_tie_breaking():
    """Same part_of intents should be grouped together when topologically possible."""
    pool = IntentPool()
    # Two independent chains, different part_of groups
    # wave-a: a1 -> a2 (a2 depends on a1)
    # wave-b: b1 -> b2 (b2 depends on b1)
    a1 = make_intent(id="a1", body="a1", target_path="a1.txt", part_of="wave-a")
    a2 = make_intent(id="a2", body="a2", target_path="a2.txt", part_of="wave-a", deps=["a1"])
    b1 = make_intent(id="b1", body="b1", target_path="b1.txt", part_of="wave-b")
    b2 = make_intent(id="b2", body="b2", target_path="b2.txt", part_of="wave-b", deps=["b1"])
    pool.post(a2)
    pool.post(b2)
    pool.post(a1)
    pool.post(b1)
    with tempfile.TemporaryDirectory() as td:
        result = compile(pool, base_dir=td)
        ids = [i.id for i in result]
        # a1 before a2, b1 before b2
        pos_a1 = ids.index("a1")
        pos_a2 = ids.index("a2")
        pos_b1 = ids.index("b1")
        pos_b2 = ids.index("b2")
        assert pos_a1 < pos_a2, f"a1 must come before a2, got {ids}"
        assert pos_b1 < pos_b2, f"b1 must come before b2, got {ids}"
        # Each group should be contiguous
        a_range = max(pos_a1, pos_a2) - min(pos_a1, pos_a2)
        b_range = max(pos_b1, pos_b2) - min(pos_b1, pos_b2)
        assert a_range == 1, f"wave-a not contiguous: {ids}"
        assert b_range == 1, f"wave-b not contiguous: {ids}"
    print("PASS test_part_of_tie_breaking")


def test_conflict_rejects():
    """Unresolved dep should cause CompilerError (pool allows, check catches)."""
    pool = IntentPool()
    pool.post(make_intent(id="orphan", body="x", target_path="x1.txt",
                          deps=["no-such-dep"]))
    with tempfile.TemporaryDirectory() as td:
        try:
            compile(pool, base_dir=td)
            assert False, "expected CompilerError"
        except CompilerError as e:
            assert len(e.conflicts) >= 1
            assert any(c.kind == "unresolved_dep" for c in e.conflicts)
    print("PASS test_conflict_rejects")


def test_ordered_sequence_output():
    """The ordered sequence must be returned and printed."""
    pool = IntentPool()
    i1 = make_intent(id="first", body="1st", target_path="out/first.txt")
    i2 = make_intent(id="second", body="2nd", target_path="out/second.txt")
    pool.post(i1)
    pool.post(i2)
    with tempfile.TemporaryDirectory() as td:
        result = compile(pool, base_dir=td)
        assert len(result) == 2
        assert result[0].id in ("first", "second")
        assert result[1].id in ("first", "second")
        # Both files exist
        assert (Path(td) / i1.target_path).exists()
        assert (Path(td) / i2.target_path).exists()
    print("PASS test_ordered_sequence_output")


def test_create_dirs_for_nested_path():
    """Compiler should create parent directories."""
    pool = IntentPool()
    i = make_intent(id="deep", target_path="a/b/c/deep.txt", body="deep\n")
    pool.post(i)
    with tempfile.TemporaryDirectory() as td:
        result = compile(pool, base_dir=td)
        target = Path(td) / i.target_path
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "deep\n"
    print("PASS test_create_dirs_for_nested_path")


if __name__ == "__main__":
    test_empty_pool()
    test_single_intent_writes_file()
    test_dep_order_is_respected()
    test_part_of_tie_breaking()
    test_conflict_rejects()
    test_ordered_sequence_output()
    test_create_dirs_for_nested_path()
    print("\nAll compile tests passed.")
