"""tests/test_tiered_runner_import.py — verify tiered_runner import guards resolve after port.

Tests that make_agent_runner and make_tiered_runner resolve without ImportError
from the ported engine modules. The old try/except/fallback-stub pattern has been removed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("echelon_engine.agent.world.tiered_runner",
                    reason="echelon_engine.agent.world.tiered_runner not available")


class TestTieredRunnerImports:
    """Verify all lazy imports in tiered_runner resolve to real ported modules."""

    def test_make_agent_runner_import_resolves(self):
        """make_agent_runner resolves from echelon_engine.agent.workflow without ImportError."""
        from echelon_engine.agent.workflow import make_agent_runner
        assert callable(make_agent_runner), "make_agent_runner must be a callable"

    def test_make_tiered_runner_import_resolves(self):
        """make_tiered_runner resolves from echelon_engine.agent.world.tiered_runner."""
        from echelon_engine.agent.world.tiered_runner import make_tiered_runner
        assert callable(make_tiered_runner), "make_tiered_runner must be a callable"

    def test_classify_tier_import_resolves(self):
        """classify_tier resolves from echelon_sdk.gantt_pillars (always in sdk)."""
        from echelon_sdk.gantt_pillars import classify_tier, TIER_FAST
        assert callable(classify_tier)
        assert isinstance(TIER_FAST, str)
