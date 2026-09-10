"""Focused unit tests for echelon_engine.atoms.atom_kinds — the COUNCIL kind classifier.

atom_kinds multi-label-classifies an atom by majority-voting a pool of small models on
one yes/no question per kind (the reasoning is in the decomposition + voting + fusion, not
any one model). The model calls go through _chat (network) — so the network-free seam is
the FUSION logic in classify(): we inject a fake per-model vote via _ask_all_kinds and pin
the majority rule. This module is downstream of WELD #1 (it reaches _chat/_THINK_RE/
COUNCIL_POOL through warmth_update, which now sources _chat from the floor_chat primitive).
"""
import pytest

from echelon_engine.atoms import atom_kinds as ak


def test_kinds_vocabulary_is_stable():
    assert ak.KINDS == ["compute", "pricing", "sync", "deploy", "memory", "ui", "identity"]


def test_classify_majority_fuses_votes(monkeypatch):
    # 3 models; 'memory' gets 2/3 yes (>0.5 -> kept), 'ui' gets 1/3 (dropped).
    fake_votes = iter([
        {"memory": True,  "ui": True,  "compute": False},
        {"memory": True,  "ui": False, "compute": False},
        {"memory": False, "ui": False, "compute": False},
    ])
    monkeypatch.setattr(ak, "_ask_all_kinds", lambda m, c, ctx: next(fake_votes))
    out = ak.classify("some atom content", pool=["m1", "m2", "m3"])
    assert "memory" in out          # 2/3 yes
    assert "ui" not in out          # 1/3 yes -> below the 0.5 majority
    assert "compute" not in out     # 0/3


def test_classify_empty_when_no_kind_wins(monkeypatch):
    monkeypatch.setattr(ak, "_ask_all_kinds", lambda m, c, ctx: {k: False for k in ak.KINDS})
    assert ak.classify("a kindless atom", pool=["m1", "m2"]) == []


def test_classify_ignores_missing_votes(monkeypatch):
    # a model that returns {} (unparseable) is a no-vote, not a crash; the other carries it
    votes = iter([{}, {"deploy": True}])
    monkeypatch.setattr(ak, "_ask_all_kinds", lambda m, c, ctx: next(votes))
    out = ak.classify("a deploy note", pool=["m1", "m2"])
    # deploy: 1 yes / 1 total (the {} model didn't vote on it) -> 1.0 > 0.5 -> kept
    assert "deploy" in out


# ── the weld chain is connected: _chat resolves through to the primitive ──
def test_chat_chain_reaches_floor_chat():
    from echelon_engine.atoms.warmth_update import _chat as wu_chat
    from echelon_engine.atoms.providers.floor_chat import _chat as fc_chat
    assert wu_chat is fc_chat        # atom_kinds -> warmth_update -> floor_chat, one primitive
