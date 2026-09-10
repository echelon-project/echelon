"""Weld #4 — ToolError lives in the SDK, breaking the tools↔tools_fileops cycle.

In echelon-agent, `ToolError` was defined in `tools.py`; `tools_fileops.py` did
`from .tools import ToolError` while `tools.py` imported `_FileOpsMixin` back from
`tools_fileops` — a cycle held only by an import-ORDERING hack (define the class,
THEN import the mixin, `# noqa: E402`). The cut: move the type to the pure library.

These tests pin the SDK contract the migrated tool modules must satisfy, so when
`tools.py` / `tools_fileops.py` land in `echelon_engine/atoms/` they import ToolError
from `echelon_sdk` and the cycle cannot reappear.
"""
from echelon_sdk.exceptions import EchelonError, ToolError
import echelon_sdk


# ── the type is in the library, on the one exception spine ────────────────
def test_tool_error_is_importable_from_sdk():
    assert issubclass(ToolError, EchelonError)
    assert issubclass(ToolError, Exception)


def test_tool_error_reexported_at_package_root():
    # importers may use `from echelon_sdk import ToolError` (clean public path)
    assert echelon_sdk.ToolError is ToolError
    assert "ToolError" in echelon_sdk.__all__


def test_tool_error_carries_its_message():
    # behaviour parity with the original bare-Exception subclass
    err = ToolError("not a file: /x")
    assert str(err) == "not a file: /x"
    try:
        raise ToolError("writes are disabled for this run")
    except EchelonError as caught:        # catchable as the spine root
        assert "disabled" in str(caught)


# ── the cut: the SDK module pulls in NOTHING upward (no engine, no tools) ──
def test_exceptions_module_is_pure():
    # AST, not substring — the docstrings legitimately MENTION `import echelon_engine`.
    import ast
    import echelon_sdk.exceptions as exc
    tree = ast.parse(open(exc.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = {m for m in imported if m.startswith(("echelon_engine", "echelon_sdk.tools"))
                 or m in {"tools", "tools_fileops"} or m.endswith(".tools")}
    assert not forbidden, f"sdk exceptions must not import upward; found {forbidden}"
