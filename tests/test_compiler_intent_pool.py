"""Focused tests for echelon_sdk.compiler.intent_pool — IntentPool."""
from __future__ import annotations

import sys
sys.path.insert(0, ".")

from echelon_sdk.compiler.intent import Intent
from echelon_sdk.compiler.intent_pool import IntentPool


def test_post_and_read_one():
    pool = IntentPool()
    i = Intent(
        id="add-readme",
        part_of="doc-wave",
        target_path="README.md",
        action="create",
        body="# Hello",
        deps=["init-repo"],
    )
    pool.post(i)
    read_back = pool.read()
    assert len(read_back) == 1
    r = read_back[0]
    assert r.id == "add-readme"
    assert r.target_path == "README.md"
    assert r.body == "# Hello"
    assert r.deps == ["init-repo"]
    pool.close()


def test_read_empty():
    pool = IntentPool()
    assert pool.read() == []
    pool.close()


def test_post_multiple():
    pool = IntentPool()
    i1 = Intent(id="a", part_of="w", target_path="f1", action="create", body="1")
    i2 = Intent(id="b", part_of="w", target_path="f2", action="edit", body="2")
    i3 = Intent(id="c", part_of="w", target_path="f3", action="create", body="3")
    pool.post(i1)
    pool.post(i2)
    pool.post(i3)
    all_intents = pool.read()
    assert len(all_intents) == 3
    assert [x.id for x in all_intents] == ["a", "b", "c"]
    pool.close()


def test_duplicate_id_raises():
    pool = IntentPool()
    i = Intent(id="dup", part_of="w", target_path="f", action="create", body="x")
    pool.post(i)
    try:
        pool.post(i)
    except ValueError as e:
        assert "dup" in str(e)
    else:
        assert False, "expected ValueError"
    pool.close()


def test_target_path_collision():
    pool = IntentPool()
    i1 = Intent(id="first", part_of="w", target_path="shared/path", action="create", body="a")
    i2 = Intent(id="second", part_of="w", target_path="shared/path", action="edit", body="b")
    pool.post(i1)
    pool.post(i2)

    # Read rows directly for conflict info
    rows = pool._conn.execute("SELECT id, conflict, conflict_with FROM intents ORDER BY rowid").fetchall()
    assert len(rows) == 2

    # Both should be in conflict
    for r in rows:
        assert r["conflict"] == 1, f"expected conflict=1 for {r['id']}"

    # first should list second as conflict; second should list first
    first_row = [r for r in rows if r["id"] == "first"][0]
    second_row = [r for r in rows if r["id"] == "second"][0]
    import json
    assert "second" in json.loads(first_row["conflict_with"])
    assert "first" in json.loads(second_row["conflict_with"])
    pool.close()


def test_canonical_for_collision():
    pool = IntentPool()
    i1 = Intent(id="anchor", part_of="w", target_path="real/a.py", action="create", body="x", canonical_for="canon/a.py")
    i2 = Intent(id="claimer", part_of="w", target_path="real/b.py", action="create", body="y", canonical_for="canon/a.py")
    pool.post(i1)
    pool.post(i2)

    rows = pool._conn.execute("SELECT id, conflict, conflict_with FROM intents ORDER BY rowid").fetchall()
    for r in rows:
        assert r["conflict"] == 1, f"{r['id']} should be in conflict"

    import json
    for r in rows:
        conflicts = json.loads(r["conflict_with"])
        other = "claimer" if r["id"] == "anchor" else "anchor"
        assert other in conflicts, f"{r['id']} should conflict with {other}"
    pool.close()


def test_canonical_for_collision_with_existing_target_path():
    """An intent whose canonical_for equals an existing intent's target_path triggers conflict."""
    pool = IntentPool()
    i1 = Intent(id="orig", part_of="w", target_path="canon/x.py", action="create", body="x")
    i2 = Intent(id="linker", part_of="w", target_path="other/y.py", action="create", body="y", canonical_for="canon/x.py")
    pool.post(i1)
    pool.post(i2)

    rows = pool._conn.execute("SELECT id, conflict, conflict_with FROM intents ORDER BY rowid").fetchall()
    for r in rows:
        assert r["conflict"] == 1
    pool.close()


def test_no_collision_when_different():
    pool = IntentPool()
    i1 = Intent(id="a", part_of="w", target_path="file1", action="create", body="x")
    i2 = Intent(id="b", part_of="w", target_path="file2", action="create", body="y")
    pool.post(i1)
    pool.post(i2)

    rows = pool._conn.execute("SELECT id, conflict FROM intents ORDER BY rowid").fetchall()
    for r in rows:
        assert r["conflict"] == 0
    pool.close()


if __name__ == "__main__":
    test_post_and_read_one()
    test_read_empty()
    test_post_multiple()
    test_duplicate_id_raises()
    test_target_path_collision()
    test_canonical_for_collision()
    test_canonical_for_collision_with_existing_target_path()
    test_no_collision_when_different()
    print("ALL TESTS PASSED")
