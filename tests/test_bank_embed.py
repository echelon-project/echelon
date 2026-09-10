"""Focused unit tests for echelon_engine.atoms.bank_embed — the semantic (embedding) tier.

The semantic tier escalates a KnowledgeBank's lexical floor to cosine meaning-match over
cached vectors. The pure, network-free seam is the vector codec (_pack/_unpack) and the
cosine math (_cosine) — tested directly. embed_text() and SemanticTier.search() hit the
local embedding floor (network), so they are NOT exercised live (degrade-to-[] is the
documented contract). No echelon imports — stdlib only.
"""
import struct

import pytest

from echelon_engine.atoms import bank_embed as be


# ── the vector codec round-trips ──────────────────────────────────────────
def test_pack_unpack_roundtrip():
    v = [0.1, -0.5, 1.0, 0.0, 3.14159]
    packed = be._pack(v)
    assert isinstance(packed, bytes)
    out = be._unpack(packed, dim=len(v))
    assert len(out) == len(v)
    for a, b in zip(v, out):
        assert a == pytest.approx(b, abs=1e-6)   # float32 round-trip


def test_pack_is_little_endian_float32():
    assert be._pack([1.0]) == struct.pack("<1f", 1.0)


# ── cosine similarity ─────────────────────────────────────────────────────
def test_cosine_identical_vectors_is_one():
    assert be._cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert be._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_is_negative_one():
    assert be._cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_is_scale_invariant():
    # cosine measures direction, not magnitude
    assert be._cosine([1.0, 1.0], [2.0, 2.0]) == pytest.approx(1.0)


def test_cosine_guards_bad_input():
    assert be._cosine([], [1.0]) == 0.0          # empty
    assert be._cosine([1.0, 2.0], [1.0]) == 0.0  # length mismatch
    assert be._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector (no direction)


# ── embed_text degrades to None when the floor is unavailable ─────────────
def test_embed_text_returns_none_on_unreachable_floor():
    # point at a dead port -> urlopen fails -> the documented degrade (None, caller falls to lexical)
    out = be.embed_text("anything", url="http://127.0.0.1:9/v1/embeddings", timeout=0.5)
    assert out is None
