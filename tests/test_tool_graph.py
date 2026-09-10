"""Tests for echelon_sdk.tool_graph — pure seam: CONTRACT validity, validate() rules."""
from __future__ import annotations
import pytest

from echelon_sdk.tool_graph import validate, CONTRACT, KINDS


# ── the built-in CONTRACT must self-validate ──────────────────────────────────

def test_contract_self_validates_no_fails():
    fails, warns = validate()
    assert fails == [], f"CONTRACT has FAILs: {fails}"

def test_contract_self_validates_warns_only():
    """Warns are advisory (non-reciprocal pairs); they must not be FAILs."""
    fails, warns = validate()
    assert isinstance(warns, list)   # may be non-empty — that's fine


# ── kind vocabulary ──────────────────────────────────────────────────────────

def test_kinds_not_empty():
    assert "none" in KINDS
    assert "text" in KINDS
    assert "file-ptr" in KINDS

def test_all_contract_produces_in_kinds():
    for tool, edges in CONTRACT.items():
        k = edges.get("produces")
        if k is not None:
            assert k in KINDS, f"{tool}.produces '{k}' not in KINDS"

def test_all_contract_consumes_in_kinds():
    for tool, edges in CONTRACT.items():
        k = edges.get("consumes")
        if k is not None:
            assert k in KINDS, f"{tool}.consumes '{k}' not in KINDS"


# ── dangling edge detection ──────────────────────────────────────────────────

def test_dangling_requires_is_fail():
    bad = {"tool_a": {"requires": ["nonexistent_tool"], "produces": "none", "consumes": "none"}}
    fails, _ = validate(bad)
    assert any("nonexistent_tool" in f for f in fails)

def test_dangling_pairs_with_is_fail():
    bad = {"tool_a": {"pairs_with": ["ghost"], "produces": "text", "consumes": "none"}}
    fails, _ = validate(bad)
    assert any("ghost" in f for f in fails)

def test_invalid_kind_is_fail():
    bad = {"tool_a": {"produces": "INVALID_KIND", "consumes": "none"}}
    fails, _ = validate(bad)
    assert any("INVALID_KIND" in f for f in fails)


# ── reciprocity warns ────────────────────────────────────────────────────────

def test_nonreciprocal_pairs_with_is_warn():
    # A pairs_with B, but B does not pairs_with A → should warn
    contract = {
        "tool_a": {"pairs_with": ["tool_b"], "produces": "text", "consumes": "none"},
        "tool_b": {"produces": "text", "consumes": "none"},  # no pairs_with back
    }
    fails, warns = validate(contract)
    assert fails == []
    assert any("tool_a" in w and "tool_b" in w for w in warns)


# ── known_tools drift detection ──────────────────────────────────────────────

def test_known_tools_missing_contract_entry_warns():
    _, warns = validate(CONTRACT, known_tools={"finish", "new_undeclared_tool"})
    assert any("new_undeclared_tool" in w for w in warns)

def test_known_tools_missing_from_registry_warns():
    # contract has 'recall' but known_tools doesn't → warn
    _, warns = validate(CONTRACT, known_tools=set())
    assert any("recall" in w for w in warns)


# ── clean contract passes with known_tools ───────────────────────────────────

def test_exact_known_tools_no_drift():
    known = set(CONTRACT.keys())
    fails, warns = validate(CONTRACT, known_tools=known)
    assert fails == []
    # all pairs should be reciprocal in the built-in CONTRACT or warned
    drift_warns = [w for w in warns if "live registry" in w or "contract has" in w]
    assert drift_warns == []
