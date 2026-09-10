"""Focused unit tests for echelon_engine.atoms.tools — the sandboxed ToolRegistry.

ToolRegistry is what makes a run AGENTIC: it gives the model real read/write/edit/list/
search hands, sandboxed to a root, with the read-before-edit guard and the write-sandbox.
This completes WELD #4: tools / tools_fileops / tools_attach now import ToolError from
echelon_sdk (the cycle the old import-ordering hack papered over is structurally gone).

All real, no network — exercises the tools against a tmp sandbox root.
"""
import ast

import pytest

from echelon_engine.atoms.tools import ToolRegistry
from echelon_sdk.exceptions import ToolError


@pytest.fixture
def reg(tmp_path):
    return ToolRegistry(root=tmp_path, allow_write=True, allow_bash=False)


# ── WELD #4 completion: ToolError is the sdk one, no in-module cycle ──────
def test_tool_error_is_the_sdk_type():
    from echelon_engine.atoms import tools, tools_fileops, tools_attach
    from echelon_sdk import exceptions
    assert tools.ToolError is exceptions.ToolError
    assert tools_fileops.ToolError is exceptions.ToolError
    assert tools_attach.ToolError is exceptions.ToolError


def test_no_tools_import_cycle_in_source():
    # tools_fileops must NOT reach back into .tools for ToolError (the cut)
    from echelon_engine.atoms import tools_fileops
    tree = ast.parse(open(tools_fileops.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("tools"):
            for a in node.names:
                assert a.name != "ToolError", "tools_fileops must import ToolError from sdk, not .tools"


# ── real file tools against the sandbox ───────────────────────────────────
def test_write_then_read_roundtrip(reg, tmp_path):
    reg.execute("write_file", {"path": "note.txt", "content": "hello echelon"})
    out = reg.execute("read_file", {"path": "note.txt"})
    assert "hello echelon" in out
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello echelon"


def test_list_files_sees_written_file(reg):
    reg.execute("write_file", {"path": "a.txt", "content": "x"})
    out = reg.execute("list_files", {"directory": ".", "pattern": "*.txt"})   # param is `pattern`
    assert "a.txt" in out


def test_search_file_finds_a_marker(reg):
    reg.execute("write_file", {"path": "code.py", "content": "def foo():\n    return 42\n"})
    out = reg.execute("search_file", {"path": "code.py", "query": "return 42", "regex": False})
    assert "return 42" in out and "2" in out          # the line number is reported


# ── TWO safety layers stack: the destructive gate, THEN the read-before-edit guard ──
def test_destructive_gate_blocks_overwrite_without_a_partner(reg):
    # edit_file matches the 'overwrite' destructive category; with no partner attached to
    # confirm, the pre-flight gate blocks it BEFORE the file op even runs (the real safety).
    reg.execute("write_file", {"path": "x.txt", "content": "alpha"})
    res = reg.execute("edit_file", {"path": "x.txt", "old_string": "alpha", "new_string": "beta"})
    assert "destructive gate" in res.lower() or "blocked" in res.lower()
    assert (reg.root / "x.txt").read_text(encoding="utf-8") == "alpha"   # unchanged


def test_read_before_edit_guard_blocks_an_unread_file(reg):
    # open the overwrite gate via accept-edits mode (mirrors Claude Code's acceptEdits — overwrite
    # proceeds, delete/bulk/process_kill stay gated). NOTE: set_destructive_policy(set()) does NOT
    # disable the gate — an empty set RESETS to the full safe default (you can't accidentally
    # disable it). accept-edits is the real seam. Then the OTHER layer shows: a PRE-EXISTING file
    # the registry didn't write is refused for edit until it's read.
    reg.set_accept_edits(True)
    (reg.root / "x.txt").write_text("alpha", encoding="utf-8")   # a file the agent did NOT author
    res = reg.execute("edit_file", {"path": "x.txt", "old_string": "alpha", "new_string": "beta"})
    assert "read" in res.lower()                        # the read-before-edit guard fires
    assert (reg.root / "x.txt").read_text(encoding="utf-8") == "alpha"   # unchanged


def test_authoring_a_file_grants_edit_license(reg):
    # writing a file yourself counts as knowing its content — so a subsequent edit (with the
    # overwrite gate open) is allowed WITHOUT a separate read. (Migration probe also surfaced that
    # read_file in BYTE mode returns early and does NOT register the read-guard path — a config-
    # dependent gap noted for the spine work; line mode + write-license are the robust paths.)
    reg.set_accept_edits(True)
    reg.execute("write_file", {"path": "y.txt", "content": "alpha"})   # authored -> known
    reg.execute("edit_file", {"path": "y.txt", "old_string": "alpha", "new_string": "beta"})
    assert (reg.root / "y.txt").read_text(encoding="utf-8") == "beta"


# ── the write sandbox (writes confined to root) ───────────────────────────
def test_write_disabled_is_refused(tmp_path):
    ro = ToolRegistry(root=tmp_path, allow_write=False, allow_bash=False)
    res = ro.execute("write_file", {"path": "nope.txt", "content": "x"})
    assert "disabled" in res.lower() or "write" in res.lower()
    assert not (tmp_path / "nope.txt").exists()         # nothing written
