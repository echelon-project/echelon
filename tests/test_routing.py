"""Tests for echelon_engine.atoms.routing — pure seam: fallback table, pick, bridge guards."""
from __future__ import annotations
import pytest

from echelon_engine.atoms import routing


# ── bridge safety ────────────────────────────────────────────────────────────

def test_bridge_safe_clean_models():
    assert routing.bridge_safe("gpt-5.4-mini")
    assert routing.bridge_safe("claude-sonnet-4.6")
    assert routing.bridge_safe("deepseek-chat")

def test_bridge_safe_stale_gemini():
    for m in ("gemini-3-flash", "gemini-3.5-flash", "gemini-3-flash-preview"):
        assert not routing.bridge_safe(m), f"{m} should be stale"

def test_bridge_pick_safe_model_honored():
    assert routing.bridge_pick("gpt-5.4-mini") == "gpt-5.4-mini"

def test_bridge_pick_stale_falls_back():
    result = routing.bridge_pick("gemini-3-flash")
    assert result == routing.BRIDGE_DEFAULT

def test_bridge_pick_none_returns_default():
    assert routing.bridge_pick(None) == routing.BRIDGE_DEFAULT


# ── role picks (fallback table, no routing.json needed) ──────────────────────

def test_pick_known_roles():
    # these must return some string (the fallback always has them)
    for role in ("driver", "driver_cheap", "judge", "vision", "reason", "audit"):
        m = routing.pick(role)
        assert isinstance(m, str) and m, f"pick({role!r}) returned empty"

def test_pick_unknown_role_defaults_to_driver():
    # unknown role -> driver pick (safe default per docstring)
    m = routing.pick("nonexistent_role_xyz")
    assert m == routing.pick("driver")

def test_via_bridge_audit_is_bridge():
    # the fallback table marks audit as copilot-bridge
    assert routing.via_bridge("audit") is True

def test_via_bridge_driver_not_bridge():
    assert routing.via_bridge("driver") is False

def test_reason_for_returns_string():
    r = routing.reason_for("driver")
    assert isinstance(r, str)   # may be empty if no 'why' key


# ── size-aware driver split ──────────────────────────────────────────────────

def test_pick_driver_for_size_small_takes_alt():
    result = routing.pick_driver_for_size(
        ctx_chars=1000, primary_model="grok", alt_model="deepseek", threshold=5000
    )
    assert result == "deepseek"

def test_pick_driver_for_size_large_takes_primary():
    result = routing.pick_driver_for_size(
        ctx_chars=10000, primary_model="grok", alt_model="deepseek", threshold=5000
    )
    assert result == "grok"

def test_pick_driver_for_size_at_threshold_is_primary():
    # >= threshold → primary
    result = routing.pick_driver_for_size(
        ctx_chars=5000, primary_model="grok", alt_model="deepseek", threshold=5000
    )
    assert result == "grok"
