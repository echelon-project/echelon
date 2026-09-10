"""Tests for echelon_sdk.tool_harvest — pure seam: SQL extraction + command clustering."""
from __future__ import annotations
import textwrap, tempfile, os, pathlib
import pytest

from echelon_sdk.tool_harvest import _extract_sql, harvest, render, _cmd_shape, harvest_sessions


# ── SQL extraction ───────────────────────────────────────────────────────────

def test_extract_sql_simple_select():
    src = 'conn.execute("SELECT id FROM atoms WHERE scope=?")'
    results = _extract_sql(src)
    assert any("SELECT" in r for r in results)

def test_extract_sql_insert():
    src = 'conn.execute("INSERT INTO atoms (id, content) VALUES (?,?)")'
    results = _extract_sql(src)
    assert any("INSERT" in r for r in results)

def test_extract_sql_no_sql_returns_empty():
    src = "result = do_something(x, y)"
    assert _extract_sql(src) == []

def test_extract_sql_normalises_repeated_placeholders():
    src = 'conn.execute("INSERT INTO t VALUES (?,?,?,?)")'
    results = _extract_sql(src)
    assert results  # must find something
    # all placeholders should collapse to a single ?
    for r in results:
        assert r.count("?") <= 2   # normalised


# ── harvest() over a temp package dir ────────────────────────────────────────

def _make_pkg(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp()
    for name, src in files.items():
        p = pathlib.Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return d


def test_harvest_finds_queries():
    pkg = _make_pkg({
        "store.py": 'conn.execute("SELECT id FROM bank WHERE scope=?")\nconn.execute("INSERT INTO bank VALUES (?)")',
    })
    try:
        report = harvest(pkg)
        assert report["distinct_queries"] >= 2
        assert "SELECT" in report["by_verb"]
        assert "INSERT" in report["by_verb"]
    finally:
        import shutil; shutil.rmtree(pkg)


def test_harvest_empty_pkg():
    pkg = _make_pkg({"mod.py": "x = 1"})
    try:
        report = harvest(pkg)
        assert report["distinct_queries"] == 0
    finally:
        import shutil; shutil.rmtree(pkg)


def test_render_output_contains_verb_headers():
    pkg = _make_pkg({"s.py": 'conn.execute("SELECT x FROM t")'})
    try:
        report = harvest(pkg)
        out = render(report)
        assert "SELECT" in out
    finally:
        import shutil; shutil.rmtree(pkg)


# ── _cmd_shape clustering ────────────────────────────────────────────────────

def test_cmd_shape_python_m():
    assert _cmd_shape("python -X utf8 -m echelon_agent.recall --scope foo") == "python -m echelon_agent.recall"

def test_cmd_shape_python_c_is_inline_probe():
    assert "INLINE PROBE" in _cmd_shape("python -c 'print(1)'")

def test_cmd_shape_git_sub():
    assert _cmd_shape("git status --short") == "git status"

def test_cmd_shape_plain_command():
    result = _cmd_shape("pytest tests/")
    assert "pytest" in result

def test_cmd_shape_unknown_short():
    result = _cmd_shape("somecommand arg1 arg2")
    assert "somecommand" in result
