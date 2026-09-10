"""Tests for echelon_engine.atoms.routing — pure decision logic.

Tests: pick_driver_for_size (pure), bridge_safe (pure), bridge_pick (pure),
pick (table lookup with fallback), provider_for (lazy-import paths — mock out
the provider classes so no network is touched).
"""
from __future__ import annotations
import pytest
from unittest.mock import patch, MagicMock

from echelon_engine.atoms.routing import (
    pick_driver_for_size,
    bridge_safe,
    bridge_pick,
    pick,
    provider_for,
    BRIDGE_STALE,
    BRIDGE_DEFAULT,
    _FALLBACK,
)


# â”€â”€ pick_driver_for_size â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestPickDriverForSize:
    def test_below_threshold_picks_alt(self):
        result = pick_driver_for_size(5000, "grok-build-0.1", "deepseek-chat", 10000)
        assert result == "deepseek-chat"

    def test_above_threshold_picks_primary(self):
        result = pick_driver_for_size(15000, "grok-build-0.1", "deepseek-chat", 10000)
        assert result == "grok-build-0.1"

    def test_exactly_threshold_picks_primary(self):
        """At threshold (not below) -> primary."""
        result = pick_driver_for_size(10000, "primary", "alt", 10000)
        assert result == "primary"

    def test_zero_chars_picks_alt(self):
        result = pick_driver_for_size(0, "primary", "alt", 1000)
        assert result == "alt"


# â”€â”€ bridge_safe â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestBridgeSafe:
    def test_stale_models_not_safe(self):
        for m in BRIDGE_STALE:
            assert bridge_safe(m) is False

    def test_gpt_mini_is_safe(self):
        assert bridge_safe("gpt-5.4-mini") is True

    def test_claude_is_safe(self):
        assert bridge_safe("claude-sonnet-4-6") is True

    def test_grok_is_safe(self):
        assert bridge_safe("grok-4.3") is True

    def test_case_insensitive(self):
        stale = next(iter(BRIDGE_STALE))
        assert bridge_safe(stale.upper()) is False


# â”€â”€ bridge_pick â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestBridgePick:
    def test_none_requested_returns_default(self):
        assert bridge_pick(None) == BRIDGE_DEFAULT

    def test_safe_model_honored(self):
        assert bridge_pick("gpt-5.4-mini") == "gpt-5.4-mini"

    def test_stale_model_returns_default(self):
        stale = next(iter(BRIDGE_STALE))
        result = bridge_pick(stale)
        assert result == BRIDGE_DEFAULT
        assert result not in BRIDGE_STALE


# â”€â”€ pick â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestPick:
    def test_known_roles_return_strings(self):
        for role in ("driver", "driver_cheap", "judge", "vision", "reason", "audit"):
            result = pick(role)
            assert isinstance(result, str)
            assert len(result) > 0

    def test_unknown_role_returns_driver_default(self):
        """Unknown role falls back to the driver pick."""
        driver_pick = pick("driver")
        unknown_pick = pick("nonexistent_role_xyz")
        assert unknown_pick == driver_pick

    def test_fallback_driver_is_string(self):
        assert isinstance(_FALLBACK["driver"]["pick"], str)


# â”€â”€ provider_for â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestProviderFor:
    def _mock_provider(self, name):
        mock_cls = MagicMock()
        mock_cls.return_value = MagicMock(name=name)
        return mock_cls

    def test_deepseek_model_uses_deepseek_provider(self):
        mock_cls = self._mock_provider("DeepSeekProvider")
        with patch("echelon_engine.atoms.providers.deepseek.DeepSeekProvider", mock_cls):
            with patch("echelon_engine.atoms.routing.DeepSeekProvider", mock_cls, create=True):
                # We test by inspecting the lazy import path
                import echelon_engine.atoms.providers.deepseek as ds_mod
                original = ds_mod.DeepSeekProvider
                ds_mod.DeepSeekProvider = mock_cls
                try:
                    result = provider_for("deepseek-chat")
                    assert result is not None
                finally:
                    ds_mod.DeepSeekProvider = original

    def test_claude_model_uses_bridge(self):
        import echelon_engine.atoms.providers.bridge as br_mod
        original = br_mod.BridgeProvider
        mock_cls = MagicMock(return_value=MagicMock())
        br_mod.BridgeProvider = mock_cls
        try:
            provider_for("claude-sonnet-4-6")
            mock_cls.assert_called_once()
        finally:
            br_mod.BridgeProvider = original

    def test_gpt_model_uses_bridge(self):
        import echelon_engine.atoms.providers.bridge as br_mod
        original = br_mod.BridgeProvider
        mock_cls = MagicMock(return_value=MagicMock())
        br_mod.BridgeProvider = mock_cls
        try:
            provider_for("gpt-5.4-mini")
            mock_cls.assert_called_once()
        finally:
            br_mod.BridgeProvider = original

    def test_grok_model_uses_grok_provider(self):
        import echelon_engine.atoms.providers.grok as grok_mod
        original = grok_mod.GrokProvider
        mock_cls = MagicMock(return_value=MagicMock())
        grok_mod.GrokProvider = mock_cls
        try:
            provider_for("grok-4.3")
            mock_cls.assert_called_once()
        finally:
            grok_mod.GrokProvider = original

    def test_prefer_bridge_forces_bridge(self):
        import echelon_engine.atoms.providers.bridge as br_mod
        original = br_mod.BridgeProvider
        mock_cls = MagicMock(return_value=MagicMock())
        br_mod.BridgeProvider = mock_cls
        try:
            provider_for("deepseek-chat", prefer_bridge=True)
            mock_cls.assert_called_once()
        finally:
            br_mod.BridgeProvider = original

    def test_unknown_model_falls_back_to_bridge(self):
        import echelon_engine.atoms.providers.bridge as br_mod
        original = br_mod.BridgeProvider
        mock_cls = MagicMock(return_value=MagicMock())
        br_mod.BridgeProvider = mock_cls
        try:
            provider_for("some-unknown-model-xyz")
            mock_cls.assert_called_once()
        finally:
            br_mod.BridgeProvider = original
