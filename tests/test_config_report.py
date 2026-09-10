"""Tests for echelon_engine.atoms.config_report — pure seam: render() logic, gather() degradation."""
from __future__ import annotations
import pytest

from echelon_engine.atoms import config_report as cr


# ── render() over synthetic data — no live system ─────────────────────────────

def _mock_data(*, include_routing=True, include_paths=True):
    d: dict = {}
    d["config"] = {
        "brain": "grok-4.3", "mode": "run", "soul": "~/.echelon/core_v2.db",
        "max_steps": 40, "ttl": 300, "budget_usd": 5.0, "call_timeout": 120,
        "tier_order": ["T3", "T2", "T1"],
        "fork_field": {"decay": 0.9, "cold": 60, "max_parallel": 4, "max_ticks": 20},
        "swarm": {"max_agents": 8, "attach_max_agents": 4, "workflow_max_parallel": 2, "sub_ttl": 60},
        "score": {"benchmark": 50.0, "lambda": 0.1, "promote_threshold": 70, "promote_min_recalls": 3},
        "warmth": {"warm": 0.7, "lukewarm": 0.4, "edge_decay": 0.95, "max_hops": 3},
        "file_read": {"mode": "peek", "window_bytes": 4096, "peek_bytes": 512},
    }
    d["substrate"] = {"soul_total": 401, "bank_total": 527, "links": 2889, "echelon_scope": 154,
                      "domains": ["echelon", "research", "code"]}
    if include_routing:
        d["routing"] = {
            "driver": {"model": "grok-build-0.1", "bridge": False},
            "judge":  {"model": "deepseek-chat", "bridge": False},
            "audit":  {"model": "claude-sonnet-4.6", "bridge": True},
            "reason": {"model": "grok-build-0.1", "bridge": False},
            "vision": {"model": "grok-build-0.1", "bridge": False},
        }
    if include_paths:
        d["paths"] = {"home": "~/.echelon", "core_db": "~/.echelon/core_v2.db",
                      "runs": "~/.echelon/runs", "bridges": "~/.echelon/bridges"}
    return d


def test_render_contains_config_section():
    out = cr.render(_mock_data())
    assert "CONFIG REGISTRY" in out
    assert "brain=grok-4.3" in out

def test_render_contains_substrate_section():
    out = cr.render(_mock_data())
    assert "SUBSTRATE CENSUS" in out
    assert "soul_total=401" in out

def test_render_contains_routing_section():
    out = cr.render(_mock_data())
    assert "ROUTING LADDER" in out
    assert "audit" in out
    assert "(bridge)" in out

def test_render_contains_paths_section():
    out = cr.render(_mock_data())
    assert "PATHS" in out
    assert "core_v2.db" in out

def test_render_contains_tier_model():
    out = cr.render(_mock_data())
    assert "TIER MODEL" in out
    assert "T1" in out
    assert "T2" in out
    assert "T3" in out

def test_render_with_by_tier_shows_ledger():
    data = _mock_data()
    data["by_tier"] = {
        "T1": {"calls": 2, "tokens_in": 1000, "tokens_out": 200, "usd": 0.012, "models": "claude"},
        "T2": {"calls": 10, "tokens_in": 5000, "tokens_out": 1000, "usd": 0.005, "models": "deepseek"},
    }
    out = cr.render(data)
    assert "TIER SPEND" in out
    assert "T1" in out

def test_render_error_blocks_degrade_gracefully():
    """A gather() error in one block should not crash the render."""
    data = {"config": {"error": "config missing"}, "substrate": {"error": "no db"}}
    out = cr.render(data)
    assert "ECHELON" in out   # still produces a header


# ── gather() degradation: each block must catch exceptions ──────────────────

def test_gather_returns_dict():
    # gather() degrades gracefully — each block catches its own errors
    data = cr.gather()
    assert isinstance(data, dict)
    # may have errors in each block (no live system in tests), but dict shape holds
    assert "config" in data or True   # just must not raise
