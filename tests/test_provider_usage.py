"""Tests for services_memory.provider_usage — the honest spend surface behind /api/keys
(single-key MVP, council verdict B 2026-07-30: real journal aggregation replaces the
cut Account Pool). Numbers must come ONLY from recorded charges in the budget journals."""
from __future__ import annotations

import json
import os

import pytest

from echelon_engine.services_memory import provider_usage, _provider_of_model


@pytest.fixture()
def budget_dir(tmp_path, monkeypatch):
    """Point ECHELON_BUDGET_DIR at a fresh temp dir (same convention as test_provider_cost)."""
    monkeypatch.setenv("ECHELON_BUDGET_DIR", str(tmp_path))
    monkeypatch.delenv("ECHELON_BUDGET_ALERT_USD", raising=False)
    return tmp_path


def _journal(d, name: str, recs: list[dict]) -> None:
    (d / f"{name}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


# ── model → provider mapping ────────────────────────────────────────────────
@pytest.mark.parametrize("model,provider", [
    ("deepseek-chat", "deepseek"),
    ("deepseek-v4-flash", "deepseek"),
    ("gemini-2.5-flash", "gemini"),
    ("grok-4.3", "xai/grok"),
    ("claude-sonnet-5", "anthropic"),
    ("phi-3.5", "lmstudio"),
    ("mystery-model", "other"),
])
def test_provider_of_model(model, provider):
    assert _provider_of_model(model) == provider


# ── aggregation ─────────────────────────────────────────────────────────────
def test_empty_dir_reports_zero(budget_dir):
    u = provider_usage()
    assert u["providers"] == {}
    assert u["total"] == {"usd": 0.0, "calls": 0, "tokens_in": 0, "tokens_out": 0}
    assert u["journals"] == 0
    assert "alert" not in u


def test_aggregates_across_journals_per_provider(budget_dir):
    _journal(budget_dir, "agent-a", [
        {"model": "deepseek-chat", "in": 1000, "out": 500, "usd": 0.01},
        {"model": "gemini-2.5-flash", "in": 200, "out": 100, "usd": 0.002},
    ])
    _journal(budget_dir, "agent-b", [
        {"model": "deepseek-chat", "in": 3000, "out": 1500, "usd": 0.03},
    ])
    u = provider_usage()
    assert u["journals"] == 2
    ds = u["providers"]["deepseek"]
    assert ds["calls"] == 2 and ds["usd"] == 0.04
    assert ds["tokens_in"] == 4000 and ds["tokens_out"] == 2000
    assert u["providers"]["gemini"]["calls"] == 1
    assert u["total"]["calls"] == 3
    assert u["total"]["usd"] == pytest.approx(0.042)


def test_malformed_lines_are_skipped_not_fatal(budget_dir):
    (budget_dir / "bad.jsonl").write_text(
        'not json\n{"model": "deepseek-chat", "in": 10, "out": 5, "usd": 0.001}\n\n',
        encoding="utf-8")
    u = provider_usage()
    assert u["total"]["calls"] == 1
    assert u["providers"]["deepseek"]["usd"] == 0.001


# ── the alert guard (the council's mandated follow-up) ──────────────────────
def test_alert_flag_arms_on_threshold(budget_dir):
    _journal(budget_dir, "big", [{"model": "deepseek-chat", "in": 0, "out": 0, "usd": 5.0}])
    u = provider_usage(alert_usd=4.0)
    assert u["alert"] is True and u["alert_usd"] == 4.0
    assert provider_usage(alert_usd=6.0)["alert"] is False


def test_alert_env_var_is_read(budget_dir, monkeypatch):
    monkeypatch.setenv("ECHELON_BUDGET_ALERT_USD", "2.5")
    _journal(budget_dir, "big", [{"model": "deepseek-chat", "in": 0, "out": 0, "usd": 3.0}])
    u = provider_usage()
    assert u["alert"] is True and u["alert_usd"] == 2.5


# ── /api/keys contract shape (what the SPA spend meter expects) ─────────────

def test_contract_shape_has_all_keys(budget_dir):
    """The usage payload must always have providers, total, journals — even when empty."""
    u = provider_usage()
    assert "providers" in u
    assert "total" in u
    assert "journals" in u
    # total sub-keys
    assert "usd" in u["total"]
    assert "calls" in u["total"]
    assert "tokens_in" in u["total"]
    assert "tokens_out" in u["total"]


def test_contract_shape_with_data(budget_dir):
    """Each provider entry must have usd, calls, tokens_in, tokens_out."""
    _journal(budget_dir, "agent-a", [
        {"model": "deepseek-chat", "in": 100, "out": 50, "usd": 0.001},
    ])
    u = provider_usage()
    ds = u["providers"]["deepseek"]
    assert ds["usd"] == 0.001
    assert ds["calls"] == 1
    assert ds["tokens_in"] == 100
    assert ds["tokens_out"] == 50


def test_contract_shape_alert_absent_when_no_threshold(budget_dir):
    """alert + alert_usd must be absent (not None, not undefined) when no threshold is set."""
    _journal(budget_dir, "big", [{"model": "deepseek-chat", "in": 0, "out": 0, "usd": 10.0}])
    u = provider_usage()
    assert "alert" not in u
    assert "alert_usd" not in u


def test_contract_shape_alert_present_when_armed(budget_dir):
    """When alert_usd is set and total crosses it, alert=true must appear."""
    _journal(budget_dir, "big", [{"model": "deepseek-chat", "in": 0, "out": 0, "usd": 5.0}])
    u = provider_usage(alert_usd=3.0)
    assert u["alert"] is True
    assert u["alert_usd"] == 3.0


def test_contract_shape_journals_count(budget_dir):
    """journals must equal the number of .jsonl files in the budget dir."""
    assert provider_usage()["journals"] == 0
    _journal(budget_dir, "a", [{"model": "deepseek-chat", "in": 0, "out": 0, "usd": 0.0}])
    assert provider_usage()["journals"] == 1
    _journal(budget_dir, "b", [{"model": "gemini-2.5-flash", "in": 0, "out": 0, "usd": 0.0}])
    assert provider_usage()["journals"] == 2
