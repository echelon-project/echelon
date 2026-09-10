"""Focused unit tests for echelon_engine.atoms.wake_snapshots — brain wake-up snapshots.

A waking REPLY is a KNOWLEDGE-BANK entry (kind='wake_reply'), NOT a soul seed — stored at a
per-brain, scope-qualified COS coordinate `wake:<brain>:<scope>` with a reuse-TTL. On a brain
switch the re-wake is SEEDED with the brain's own most-recent waking (rediscovery, not replay).

wake_snapshots is a pure helper layer over a duck-typed `bank` (bank.store / bank.latest). These
tests drive it against an isolated KnowledgeBank on a tmp db so nothing touches ~/.echelon.

Covered:
  - _seg / wake_coordinate: address shape + sanitization + scope-qualification
  - store_waking: stores a real waking; skips cold (woke=False) / empty replies; returns id
  - recent_waking: freshest wake_reply; TTL expiry; path-depth fallback (scope -> any-scope)
  - rewake_seed_text: OFFER text wraps a recent waking; empty string when none
"""
import pytest

from echelon_engine.atoms.bank import KnowledgeBank
from echelon_engine.atoms import wake_snapshots as W


@pytest.fixture
def bank(tmp_path):
    return KnowledgeBank(tmp_path / "core.db")


# ── coordinate shape ───────────────────────────────────────────────────────
def test_seg_sanitizes_to_lower_underscore():
    assert W._seg("DeepSeek R1") == "deepseek_r1"
    assert W._seg("") == "unknown"
    assert W._seg("a/b:c") == "a_b_c"


def test_wake_coordinate_brain_only_and_scoped():
    assert W.wake_coordinate("deepseek") == "wake:deepseek"
    assert W.wake_coordinate("DeepSeek", "Echelon") == "wake:deepseek:echelon"


# ── store_waking ───────────────────────────────────────────────────────────
def test_store_waking_stores_real_reply(bank):
    eid = W.store_waking(bank, "deepseek", "I am awake and ready.", scope="echelon")
    assert eid is not None
    e = bank.latest(W.wake_coordinate("deepseek", "echelon"), kind="wake_reply")
    assert e is not None
    assert e.content == "I am awake and ready."
    assert e.kind == "wake_reply"


def test_store_waking_skips_cold_or_empty(bank):
    assert W.store_waking(bank, "deepseek", "real but not woke", woke=False) is None
    assert W.store_waking(bank, "deepseek", "   ", woke=True) is None
    # nothing landed
    assert bank.latest(W.wake_coordinate("deepseek"), kind="wake_reply") is None


def test_store_waking_freshest_supersedes(bank):
    W.store_waking(bank, "grok", "first waking", scope="echelon")
    W.store_waking(bank, "grok", "second, fresher waking", scope="echelon")
    e = W.recent_waking(bank, "grok", "echelon")
    assert e.content == "second, fresher waking"


# ── recent_waking ──────────────────────────────────────────────────────────
def test_recent_waking_none_when_absent(bank):
    assert W.recent_waking(bank, "nobody", "echelon") is None


def test_recent_waking_respects_ttl_expiry(bank):
    # store with a tiny TTL, then read "in the future" past expiry via now=
    W.store_waking(bank, "haiku", "ephemeral waking", scope="echelon", ttl=10)
    coord = W.wake_coordinate("haiku", "echelon")
    # still live at store time
    assert bank.latest(coord, kind="wake_reply") is not None
    # past TTL: bank.latest filters expired entries (now far in the future)
    import time
    future = int(time.time()) + 10_000
    assert bank.latest(coord, kind="wake_reply", now=future) is None


def test_recent_waking_path_depth_fallback_across_scope(bank):
    # a waking stored under one scope is found when asked under NO scope (wake:<brain>)
    W.store_waking(bank, "deepseek", "woke in scope A", scope="scopea")
    e = W.recent_waking(bank, "deepseek", "")     # wake:deepseek -> finds the scoped child
    assert e is not None
    assert e.content == "woke in scope A"


# ── rewake_seed_text ───────────────────────────────────────────────────────
def test_rewake_seed_text_wraps_recent_waking(bank):
    W.store_waking(bank, "grok", "I remember who I am.", scope="echelon")
    txt = W.rewake_seed_text(bank, "grok", "echelon")
    assert "I remember who I am." in txt
    assert "knowledge bank" in txt          # framed as a memory, not a script
    assert "OWN voice" in txt               # rediscovery-not-instruction


def test_rewake_seed_text_empty_when_no_waking(bank):
    assert W.rewake_seed_text(bank, "nobody", "echelon") == ""
