"""Focused unit tests for echelon_engine.atoms.providers.cost.

Pins the pricing/budget MATH directly (pure functions + the Budget ledger):
usd_of cache-awareness, the cache-hit-cheaper-than-miss property, image pricing,
and Budget accounting (charge/remaining/exhausted/would_exceed/by_tier).
"""
from __future__ import annotations

import pytest

from echelon_engine.atoms.providers.cost import (
    usd_of,
    usd_of_image,
    cost_of,
    Budget,
    DEFAULT_BUDGET,
    USD_PER_M,
    USD_PER_M_CACHED,
    _DEFAULT_USD,
)


# ── usd_of ───────────────────────────────────────────────────────────────────
def test_usd_of_known_model_input_only():
    # grok-4.3 = $1.25/1M in. 1M input tokens -> $1.25.
    assert usd_of("grok-4.3", 1_000_000) == 1.25


def test_usd_of_input_plus_output():
    # grok-4.3 in $1.25/M, out $10/M. 1M in + 1M out = 11.25.
    assert usd_of("grok-4.3", 1_000_000, 1_000_000) == 1.25 + 10.0


def test_usd_of_unknown_model_uses_conservative_default():
    pi, po = _DEFAULT_USD
    assert usd_of("no-such-model", 1_000_000, 1_000_000) == pi + po


@pytest.mark.parametrize("factor", [1.0, 0.5])
def test_usd_of_cache_hit_is_cheaper_than_miss(monkeypatch, factor):
    from echelon_engine.atoms.providers import cost
    monkeypatch.setattr(cost, "_deepseek_peak_factor", lambda: factor)
    # deepseek-v4-pro: cached input bills at the (much cheaper) hit rate.
    miss = usd_of("deepseek-v4-pro", 1_000_000, 0, cached_tokens=0)
    hit = usd_of("deepseek-v4-pro", 1_000_000, 0, cached_tokens=1_000_000)
    assert hit < miss
    # Cache billing must respect either configured clock window.
    assert hit == USD_PER_M_CACHED["deepseek-v4-pro"] * factor


def test_usd_of_cached_clamped_to_input():
    # cached_tokens > input_tokens must clamp (never bill negative fresh tokens)
    all_hit = usd_of("deepseek-v4-pro", 1_000, 0, cached_tokens=999_999)
    just_hit = usd_of("deepseek-v4-pro", 1_000, 0, cached_tokens=1_000)
    assert all_hit == just_hit


def test_usd_of_model_without_separate_hit_rate():
    # grok-4.3 has no cache-hit row -> a "hit" costs the same as the miss rate.
    pi = USD_PER_M["grok-4.3"][0]
    assert usd_of("grok-4.3", 1_000_000, 0, cached_tokens=1_000_000) == pi


# ── image pricing ────────────────────────────────────────────────────────────
def test_usd_of_image_per_image():
    one = usd_of_image("grok-imagine-image-quality", 1)
    three = usd_of_image("grok-imagine-image-quality", 3)
    assert three == one * 3


def test_usd_of_image_min_one():
    assert usd_of_image("grok-imagine-image", 0) == usd_of_image("grok-imagine-image", 1)


# ── legacy bridge-unit cost_of ───────────────────────────────────────────────
def test_cost_of_legacy_unit():
    # gpt-5-mini In-rate = 25 units; 1M tokens -> 25 units.
    assert cost_of("gpt-5-mini", 1_000_000) == 25.0


# ── Budget accounting ────────────────────────────────────────────────────────
def test_budget_defaults():
    b = Budget()
    assert b.total == DEFAULT_BUDGET
    assert b.spent == 0.0
    assert b.remaining == DEFAULT_BUDGET
    assert b.exhausted is False


def test_budget_charge_accumulates_and_splits_kind():
    b = Budget(total=100.0)
    c1 = b.charge("grok-4.3", 1_000_000, kind="driver")
    c2 = b.charge("grok-4.3", 1_000_000, kind="reason")
    assert b.spent == c1 + c2
    assert b.driver_spent == c1
    assert b.reason_spent == c2
    assert len(b.calls) == 2


def test_budget_remaining_and_exhausted():
    b = Budget(total=1.0)
    b.charge("grok-4.3", 1_000_000)  # $1.25 > $1.0 budget
    assert b.remaining == 0.0
    assert b.exhausted is True


def test_budget_would_exceed():
    b = Budget(total=1.0)
    assert b.would_exceed("grok-4.3", 1_000_000) is True   # $1.25 > $1.0
    assert b.would_exceed("grok-4.3", 100_000) is False     # $0.125 < $1.0


def test_budget_charge_cache_aware_cheaper():
    miss = Budget(total=999.0)
    hit = Budget(total=999.0)
    miss.charge("deepseek-v4-pro", 1_000_000, kind="driver")
    hit.charge("deepseek-v4-pro", 1_000_000, kind="driver", cached_tokens=1_000_000)
    assert hit.spent < miss.spent
    # the cache annotation is recorded on the hit row, absent on the miss row
    assert hit.calls[0].get("cached") == 1_000_000
    assert "cached" not in miss.calls[0]


def test_budget_charge_image():
    b = Budget(total=10.0)
    c = b.charge_image("grok-imagine-image-quality", 2)
    assert b.spent == c
    assert b.reason_spent == c
    assert b.calls[0]["images"] == 2


def test_budget_by_tier_aggregates():
    b = Budget(total=100.0)
    b.charge("grok-4.3", 1_000_000, kind="driver", tier="T1")
    b.charge("grok-4.3", 1_000_000, kind="driver", tier="T1")
    b.charge("deepseek-v4-pro", 500_000, kind="reason")  # untagged -> 'untiered'
    agg = b.by_tier()
    assert agg["T1"]["calls"] == 2
    assert agg["T1"]["tokens_in"] == 2_000_000
    assert "untiered" in agg
    assert agg["untiered"]["calls"] == 1


def test_budget_note_partner_consult():
    b = Budget()
    b.note_partner_consult()
    b.note_partner_consult()
    assert b.partner_consults == 2
    assert "2 consult" in b.state()


def test_budget_charge_driver_tier_tag():
    b = Budget(total=100.0)
    b.charge_driver("grok-4.3", 1_000_000, tier="T2")
    assert b.calls[0]["tier"] == "T2"
    assert b.driver_spent > 0


# ── Persistence (wave2/budget-persistence) ──────────────────────────────────

import json
import os
import tempfile
from pathlib import Path
from echelon_engine.atoms.providers.cost import budget_key, _budget_journal_dir


def _tmp_budget_dir():
    """Create a fresh temp dir and point ECHELON_BUDGET_DIR at it. Returns the Path."""
    d = Path(tempfile.mkdtemp(prefix="echelon_budget_test_"))
    os.environ["ECHELON_BUDGET_DIR"] = str(d)
    return d


def test_bare_budget_never_touches_disk():
    """Bare Budget() (no key) never creates a journal file."""
    d = _tmp_budget_dir()
    b = Budget(total=10.0)
    b.charge("grok-4.3", 1_000_000, kind="driver")
    b.charge("grok-4.3", 500_000, kind="reason")
    # No journal file should appear
    files = list(d.glob("*.jsonl"))
    assert len(files) == 0, f"bare Budget should not write journal, found: {files}"
    # Behavior matches current: in-memory tracking
    assert b.spent > 0
    assert len(b.calls) == 2


def test_round_trip_restore_spent():
    """Open(key), charge twice, drop, open(key) -> spent restored exactly."""
    d = _tmp_budget_dir()
    key = "round-trip-test"
    total = 10.0

    b1 = Budget(key=key, total=total)
    b1.charge("grok-4.3", 1_000_000, kind="driver")   # $1.25
    b1.charge("grok-4.3", 500_000, kind="reason")      # $0.625
    spent_1 = b1.spent
    assert spent_1 == pytest.approx(1.875, abs=0.001)
    assert len(b1.calls) == 2

    # Drop the instance, open fresh
    b2 = Budget(key=key, total=total)
    assert b2.spent == pytest.approx(spent_1, abs=0.001)
    assert len(b2.calls) == 2
    assert b2.remaining == pytest.approx(total - spent_1, abs=0.001)


def test_cross_process_two_instances_same_key():
    """Two Budget instances, same key, interleaved charges -> both see combined spent.

    Instance A must call remaining() (or charge() itself) to see B's charges —
    that's the staleness window documented in the design. The journal IS the
    source of truth; memory is a cache refreshed on each operation."""
    d = _tmp_budget_dir()
    key = "cross-proc-test"
    total = 10.0

    a = Budget(key=key, total=total)
    b = Budget(key=key, total=total)

    a.charge("grok-4.3", 1_000_000, kind="driver")   # $1.25

    # B's charge -> journal now has $2.50
    b.charge("grok-4.3", 1_000_000, kind="reason")    # $1.25

    # B sees the combined total (it just re-read the journal)
    assert b.spent == pytest.approx(2.50, abs=0.01)

    # A's memory is stale until it re-syncs via remaining()/charge()
    # This is the documented staleness window
    _ = a.remaining  # triggers _reload_from_journal
    assert a.spent == pytest.approx(2.50, abs=0.01)
    assert a.remaining == pytest.approx(7.50, abs=0.01)


def test_cap_trips_at_true_total_not_2x():
    """When two instances share a key, the cap trips at the true combined total."""
    d = _tmp_budget_dir()
    key = "cap-test"
    total = 2.0  # small cap — 2x grok-4.3 1M calls ($1.25 each) would exceed

    a = Budget(key=key, total=total)
    b = Budget(key=key, total=total)

    # First charge from A: $1.25 — under cap
    a.charge("grok-4.3", 1_000_000, kind="driver")
    assert not a.exhausted
    assert not b.exhausted  # B sees the same journal

    # Second charge from B: $1.25 — total $2.50 > $2.0 cap
    b.charge("grok-4.3", 1_000_000, kind="reason")
    assert a.exhausted
    assert b.exhausted
    assert a.remaining == 0.0
    assert b.remaining == 0.0


def test_no_double_count():
    """Charge N times, re-open, charge again -> journal has N+1 lines, spent == sum."""
    d = _tmp_budget_dir()
    key = "no-double-test"
    total = 100.0

    b1 = Budget(key=key, total=total)
    n = 5
    for _ in range(n):
        b1.charge("grok-4.3", 100_000, kind="driver")  # $0.125 each

    spent_before = b1.spent
    lines_before = sum(1 for _ in open(b1._journal_path))

    # Re-open and charge once more
    b2 = Budget(key=key, total=total)
    b2.charge("grok-4.3", 100_000, kind="driver")

    # Journal should have N+1 lines
    lines_after = sum(1 for _ in open(b2._journal_path))
    assert lines_after == n + 1, f"expected {n+1} lines, got {lines_after}"

    # Spent should equal the sum of all lines
    journal_sum = 0.0
    with open(b2._journal_path) as f:
        for line in f:
            line = line.strip()
            if line:
                journal_sum += json.loads(line)["usd"]
    assert b2.spent == pytest.approx(journal_sum, abs=0.0001)

    # Should equal: N charges at $0.125 + 1 more
    expected = 0.125 * (n + 1)
    assert b2.spent == pytest.approx(expected, abs=0.001)


def test_journal_lines_are_valid_jsonl():
    """Every line in the journal is valid JSON with the required fields."""
    d = _tmp_budget_dir()
    key = "jsonl-test"
    b = Budget(key=key, total=50.0)
    b.charge("grok-4.3", 1_000_000, kind="driver", tier="T1")
    b.charge("deepseek-v4-pro", 500_000, 200, kind="reason", cached_tokens=400_000)
    b.charge_image("grok-imagine-image-quality", 3)

    with open(b._journal_path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            assert line, f"line {i} is empty"
            rec = json.loads(line)
            assert "usd" in rec, f"line {i} missing usd: {rec}"
            assert "kind" in rec, f"line {i} missing kind: {rec}"
            assert "key" in rec, f"line {i} missing key: {rec}"
            assert rec["key"] == key


def test_budget_key_deterministic():
    """Same goal always produces the same key."""
    assert budget_key("deploy auth service") == budget_key("deploy auth service")
    assert budget_key("hello") != budget_key("world")


def test_baremode_exhausted_matches_original():
    """Bare Budget exhausted behavior unchanged (no journal, pure in-memory)."""
    b = Budget(total=1.0)
    assert not b.exhausted
    b.charge("grok-4.3", 1_000_000)  # $1.25 > $1.0
    assert b.exhausted
    assert b.remaining == 0.0


def test_env_override_journal_location():
    """ECHELON_BUDGET_DIR overrides journal location."""
    d = _tmp_budget_dir()
    key = "env-test"
    b = Budget(key=key, total=10.0)
    b.charge("grok-4.3", 100_000)
    expected_path = d / f"{key}.jsonl"
    assert expected_path.exists()
    assert b._journal_path == expected_path
