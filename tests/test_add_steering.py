"""Tests for echelon_engine.agent.add_steering — the ADD steering controller.

add_steering is ported as a still-evolving 3rd-party subsystem (the echelon-Agent-
Driven-Development pipeline), NOT yet atomized. These tests cover its PURE,
network-free leaves: fence stripping, the mechanical gate (python/prose lanes),
verdict parsing, and the append-only VerdictLedger. The LLM-bound surfaces
(_chat, run_add_loop/swarm, run_review_gate) need a live provider and are not
exercised here — the note is honest.
"""
from __future__ import annotations
import pytest

from echelon_engine.agent.add_steering import (
    Status,
    StatusEvent,
    VerdictLedger,
    strip_code_fences,
    mechanical_gate,
    _parse_verdict,
    _lang_of,
)


# ── strip_code_fences ─────────────────────────────────────────────────────────

def test_strip_fences_full_body():
    body = "```python\nx = 1\n```"
    assert strip_code_fences(body) == "x = 1\n"

def test_strip_fences_no_fence_passthrough():
    assert strip_code_fences("x = 1\n").strip() == "x = 1"

def test_strip_fences_drops_stray_fence_lines():
    body = "```\nx = 1\ny = 2\n```"
    out = strip_code_fences(body)
    assert "```" not in out
    assert "x = 1" in out and "y = 2" in out


# ── mechanical_gate: python lane ──────────────────────────────────────────────

def test_gate_python_valid():
    ok, reason, cleaned = mechanical_gate("def f():\n    return 1\n")
    assert ok, reason
    assert cleaned.strip() == "def f():\n    return 1".strip()

def test_gate_python_syntax_error_rejected():
    ok, reason, _ = mechanical_gate("def f(:\n    return 1\n")
    assert not ok
    assert "SyntaxError" in reason

def test_gate_python_compile_catches_return_outside_function():
    # ast.parse would accept this; only compile() rejects it — the proven escape.
    ok, reason, _ = mechanical_gate("return 5\n")
    assert not ok
    assert "SyntaxError" in reason

def test_gate_python_required_symbol_missing():
    ok, reason, _ = mechanical_gate("def f():\n    return 1\n",
                                    required_symbols=["g"])
    assert not ok
    assert "missing required symbols" in reason

def test_gate_python_required_symbol_present():
    ok, reason, _ = mechanical_gate("def g():\n    return 1\n",
                                    required_symbols=["g"])
    assert ok, reason

def test_gate_strips_fence_before_compiling():
    ok, reason, cleaned = mechanical_gate("```python\nx = 1\n```")
    assert ok, reason
    assert "```" not in cleaned


# ── mechanical_gate: prose lane (the universal leak guard + json validator) ───

def test_gate_prose_status_template_leak_rejected():
    ok, reason, _ = mechanical_gate("STATUS: BLOCKED could not finish\n",
                                    lang="prose")
    assert not ok
    assert "STATUS template leak" in reason

def test_gate_prose_json_valid(tmp_path):
    f = tmp_path / "data.json"
    ok, reason, _ = mechanical_gate('{"a": 1}\n', lang="prose",
                                    target_path=str(f))
    assert ok, reason

def test_gate_prose_json_malformed_rejected(tmp_path):
    f = tmp_path / "data.json"
    ok, reason, _ = mechanical_gate('{"a": 1\n', lang="prose",
                                    target_path=str(f))
    assert not ok
    assert "JSON parse fail" in reason

def test_gate_prose_empty_rejected():
    ok, reason, _ = mechanical_gate("   \n", lang="prose")
    assert not ok
    assert "empty body" in reason


# ── _lang_of ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,lang", [
    ("a/b.py", "python"),
    ("a/b.js", "js"),
    ("a/b.mjs", "js"),
    ("a/b.html", "js"),
    ("a/b.yaml", "prose"),
    ("a/b.md", "prose"),
    ("a/b.sh", "prose"),
    ("a/b.css", "prose"),
])
def test_lang_of(path, lang):
    assert _lang_of(path) == lang


# ── _parse_verdict ────────────────────────────────────────────────────────────

def test_parse_verdict_pass():
    passed, _ = _parse_verdict("VERDICT: PASS\nlooks good")
    assert passed

def test_parse_verdict_fail():
    passed, _ = _parse_verdict("VERDICT: FAIL\nmagic number on line 3")
    assert not passed

def test_parse_verdict_only_first_line_counts():
    # a PASS mentioned later must NOT flip a first-line FAIL.
    passed, _ = _parse_verdict("VERDICT: FAIL\nlater I say VERDICT: PASS")
    assert not passed

def test_parse_verdict_empty():
    passed, _ = _parse_verdict("")
    assert not passed


# ── VerdictLedger: append-only, newest-wins-at-read ──────────────────────────

def test_ledger_latest_newest_wins():
    led = VerdictLedger()
    led.post(StatusEvent("i1", Status.REJECTED, note="first"))
    led.post(StatusEvent("i1", Status.QUALITY_APPROVED, note="second"))
    latest = led.latest("i1")
    assert latest is not None
    assert latest.status is Status.QUALITY_APPROVED
    assert latest.note == "second"

def test_ledger_latest_unknown_id_none():
    led = VerdictLedger()
    assert led.latest("nope") is None

def test_ledger_approved_ids_only_quality_approved():
    led = VerdictLedger()
    led.post(StatusEvent("i1", Status.QUALITY_APPROVED))
    led.post(StatusEvent("i2", Status.SPEC_APPROVED))
    led.post(StatusEvent("i3", Status.REJECTED))
    assert led.approved_ids() == ["i1"]

def test_ledger_approved_ids_respects_latest_status():
    led = VerdictLedger()
    led.post(StatusEvent("i1", Status.QUALITY_APPROVED))
    led.post(StatusEvent("i1", Status.REJECTED))  # later rejection wins
    assert led.approved_ids() == []
