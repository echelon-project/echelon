"""Focused test for echelon_sdk.compiler.intent — Intent dataclass validation."""
from __future__ import annotations

import sys
sys.path.insert(0, ".")

from echelon_sdk.compiler.intent import Intent


def test_happy_create():
    i = Intent(
        id="add-readme",
        part_of="doc-wave",
        target_path="README.md",
        action="create",
        body="# Hello",
        deps=["init-repo"],
    )
    assert i.id == "add-readme"
    assert i.action == "create"
    assert i.deps == ["init-repo"]
    assert i.behavior is None          # unknown, not defaulted to ""
    assert i.canonical_for is None     # same


def test_happy_edit():
    i = Intent(
        id="fix-typo",
        part_of="polish-wave",
        target_path="src/main.py",
        action="edit",
        body="...",
        deps=[],
        behavior="safe-rewrite",
        canonical_for="src/main.py",
    )
    assert i.behavior == "safe-rewrite"
    assert i.canonical_for == "src/main.py"


def test_missing_id_raises():
    try:
        Intent(part_of="w", target_path="x", action="create", body="b")
    except ValueError as e:
        assert "id" in str(e).lower()
    else:
        assert False, "expected ValueError"


def test_bad_kebab_raises():
    try:
        Intent(id="Add-README", part_of="w", target_path="x", action="create", body="b")
    except ValueError as e:
        assert "kebab" in str(e).lower()
    else:
        assert False, "expected ValueError"

    try:
        Intent(id="add--readme", part_of="w", target_path="x", action="create", body="b")
    except ValueError:
        pass
    else:
        assert False, "expected ValueError for double hyphen"

    try:
        Intent(id="add_readme", part_of="w", target_path="x", action="create", body="b")
    except ValueError:
        pass
    else:
        assert False, "expected ValueError for underscore"


def test_bad_action_raises():
    try:
        Intent(id="foo", part_of="bar", target_path="x", action="nuke", body="b")
    except ValueError as e:
        assert "action" in str(e).lower()
    else:
        assert False, "expected ValueError"


def test_bad_deps_raises():
    try:
        Intent(id="foo", part_of="bar", target_path="x", action="create", body="b", deps=["Bad"])
    except ValueError as e:
        assert "deps" in str(e).lower()
    else:
        assert False, "expected ValueError"


def test_repr():
    i = Intent(id="abc", part_of="xyz", target_path="t.py", action="edit", body="x")
    r = repr(i)
    assert "Intent(" in r
    assert "abc" in r
    assert "edit" in r


if __name__ == "__main__":
    test_happy_create()
    test_happy_edit()
    test_missing_id_raises()
    test_bad_kebab_raises()
    test_bad_action_raises()
    test_bad_deps_raises()
    test_repr()
    print("ALL TESTS PASSED")
