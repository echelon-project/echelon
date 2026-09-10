"""Tests for echelon_engine.atoms.card_scan_rules — pure immune-scan predicates (no DB)."""
import pytest

from echelon_engine.atoms.card_scan_rules import (
    is_live_lie,
    check_dangling_from,
    check_dangling_to,
    check_edge_to_disclaimed,
    check_unknown_relation,
    check_orphan_disclaim,
    check_live_contradiction,
    RELATION_TYPES,
    _JUDGED_MARK,
    _REDEEMED_MARK,
)


# ── is_live_lie ───────────────────────────────────────────────────────────

def test_is_live_lie_false_for_clean_born_from():
    assert is_live_lie("") is False
    assert is_live_lie("ported:v1:echelon") is False


def test_is_live_lie_true_for_disclaimed():
    assert is_live_lie(f"{_JUDGED_MARK} ts:12345") is True


def test_is_live_lie_false_for_redeemed():
    """A redeemed atom has the judged mark in its lineage but starts with 'redeemed:'."""
    bf = f"{_REDEEMED_MARK}from-judged ts:999 bonus:30 <- {_JUDGED_MARK} ts:12345"
    assert is_live_lie(bf) is False


def test_is_live_lie_with_none_safe():
    assert is_live_lie(None) is False  # type: ignore[arg-type]


# ── check_dangling_from ───────────────────────────────────────────────────

def test_dangling_from_returns_finding_when_from_missing():
    finding = check_dangling_from("ghost-id", "real-id", "refs", all_ids={"real-id"})
    assert finding is not None
    assert finding["rule"] == "dangling_edge_from"
    assert finding["severity"] == "FAIL"


def test_dangling_from_returns_none_when_from_exists():
    finding = check_dangling_from("real-id", "other-id", "refs", all_ids={"real-id", "other-id"})
    assert finding is None


# ── check_dangling_to ─────────────────────────────────────────────────────

def test_dangling_to_returns_finding_when_to_missing():
    finding = check_dangling_to("real-id", "ghost-id", "refs", all_ids={"real-id"})
    assert finding is not None
    assert finding["rule"] == "dangling_edge_to"
    assert finding["severity"] == "FAIL"


def test_dangling_to_returns_none_when_to_exists():
    finding = check_dangling_to("a", "b", "refs", all_ids={"a", "b"})
    assert finding is None


# ── check_edge_to_disclaimed ──────────────────────────────────────────────

def test_edge_to_disclaimed_finds_problem():
    finding = check_edge_to_disclaimed("a", "b", "refs", all_disclaimed={"b"})
    assert finding is not None
    assert finding["rule"] == "edge_to_disclaimed"
    assert finding["severity"] == "FAIL"


def test_supersedes_edge_to_disclaimed_is_allowed():
    """The 'supersedes' relation explicitly points at what it replaces — not a problem."""
    finding = check_edge_to_disclaimed("a", "b", "supersedes", all_disclaimed={"b"})
    assert finding is None


def test_edge_to_non_disclaimed_is_fine():
    finding = check_edge_to_disclaimed("a", "b", "refs", all_disclaimed=set())
    assert finding is None


# ── check_unknown_relation ────────────────────────────────────────────────

def test_unknown_relation_flagged():
    finding = check_unknown_relation("a", "b", "totally-made-up")
    assert finding is not None
    assert finding["rule"] == "unknown_relation"
    assert finding["severity"] == "FAIL"


def test_all_known_relations_pass():
    for rel in RELATION_TYPES:
        assert check_unknown_relation("a", "b", rel) is None


# ── check_orphan_disclaim ─────────────────────────────────────────────────

def test_orphan_disclaim_no_superseder():
    finding = check_orphan_disclaim("disclaimed-id", superseders=set())
    assert finding is not None
    assert finding["rule"] == "orphan_disclaim"
    assert finding["severity"] == "WARN"


def test_orphan_disclaim_has_superseder_is_fine():
    finding = check_orphan_disclaim("disclaimed-id", superseders={"disclaimed-id"})
    assert finding is None


# ── check_live_contradiction ──────────────────────────────────────────────

def test_live_contradiction_both_live():
    finding = check_live_contradiction("a", "b", all_disclaimed=set())
    assert finding is not None
    assert finding["rule"] == "live_contradiction"
    assert finding["severity"] == "WARN"


def test_live_contradiction_one_disclaimed_is_fine():
    finding = check_live_contradiction("a", "b", all_disclaimed={"b"})
    assert finding is None


def test_live_contradiction_both_disclaimed_is_fine():
    finding = check_live_contradiction("a", "b", all_disclaimed={"a", "b"})
    assert finding is None
