"""Focused unit tests for echelon_engine.atoms.bank — the COS×ENTRY content layer.

KnowledgeBank is coordinate-pathed content over UAME (family='bank', invisible to the
soul scan). It is a PURE RESOLUTION layer (zero own scoring): store at a coordinate,
resolve with ancestor-fallback, version via supersedes. Depends only on uame (+ the pure
cos_guard leaf, now in echelon_sdk).

Isolated: a fresh UAME on a tmp db is injected, so nothing touches the real bank.
"""
import time

import pytest

from echelon_engine.atoms.uame import UAME
from echelon_engine.atoms.bank import KnowledgeBank, BANK_FAMILY, content_hash


@pytest.fixture
def bank(tmp_path):
    return KnowledgeBank(uame=UAME(tmp_path / "core.db"))


# ── store + exact get ─────────────────────────────────────────────────────
def test_store_then_get_roundtrip(bank):
    eid = bank.store("gzip -9 is max compression", "tooling:bash:gzip")
    e = bank.get(eid)
    assert e is not None
    assert e.content == "gzip -9 is max compression"
    assert e.family == BANK_FAMILY                 # lives in the bank family, not the soul


def test_store_is_invisible_to_the_soul(bank):
    bank.store("a bank fact", "tooling:x")
    # the soul scan (core_*/working_*) must NOT see bank_* entries — the family wall
    assert bank.u.count() == 0                      # zero SOUL rows
    assert bank.u.count(family=BANK_FAMILY) == 1    # one BANK row


# ── versioning: same coordinate, changed content => supersedes ────────────
def test_unchanged_content_is_a_noop(bank):
    a = bank.store("same text", "repo:file:func")
    b = bank.store("same text", "repo:file:func")
    assert a == b                                   # content_hash match -> no new version


def test_changed_content_creates_new_version(bank):
    bank.store("version one", "repo:file:func", kind="code")
    time.sleep(0.01)
    bank.store("version two", "repo:file:func", kind="code")
    hist = bank.history("repo:file:func", kind="code")
    contents = [e.content for e in hist]
    assert "version one" in contents and "version two" in contents
    assert hist[0].content == "version two"          # newest first


def test_content_hash_is_ts_independent():
    assert content_hash("x") == content_hash("x")
    assert content_hash("x") != content_hash("y")


# ── resolve: exact hit, ancestor fallback, lexical rank ───────────────────
def test_resolve_exact_coordinate(bank):
    bank.store("exact node content", "a:b:c")
    hits = bank.resolve("a:b:c")
    assert hits and hits[0].entry.content == "exact node content"
    assert hits[0].depth_drop == 0                  # no ascent needed


def test_resolve_ascends_when_no_exact_hit(bank):
    bank.store("parent-level content", "a:b")
    hits = bank.resolve("a:b:c:d")                  # nothing at the deep coord
    assert hits, "should ascend to the populated ancestor"
    assert hits[0].entry.content == "parent-level content"
    assert hits[0].depth_drop > 0                   # it ascended


def test_resolve_ranks_by_lexical_match(bank):
    bank.store("python utf8 flag avoids the crash", "tooling:py")
    bank.store("gzip compression level nine", "tooling:gz")
    hits = bank.resolve("tooling", query="python utf8 crash")
    assert hits
    assert "python utf8" in hits[0].entry.content   # the lexically closest ranks first


def test_query_is_coordinate_free(bank):
    bank.store("the chainboard composition framework", "x:y")
    hits = bank.query("chainboard framework")
    assert hits and "chainboard" in hits[0].entry.content


# ── TTL / count ───────────────────────────────────────────────────────────
def test_count_excludes_expired_when_asked(bank):
    bank.store("durable", "a:durable")              # ttl=0 -> never expires
    bank.store("ephemeral", "a:ephemeral", ttl=1)   # expires after 1s
    assert bank.count(include_expired=True) == 2
    # at a far-future 'now' the ephemeral one is expired
    future = int(time.time()) + 100
    assert bank.count(include_expired=False, now=future) == 1
