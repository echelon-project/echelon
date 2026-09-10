"""Focused unit tests for echelon_engine.atoms.warmth_update + the WELD #1 cut.

warmth_update is the active-now scoring organ (council/cascade of small-model judges).
Most of it is network (the panel calls _chat) + a CLI (main) — the pure, network-free
seam is rod_digest (the ZERO-model structural feature extractor). These tests pin that
seam AND verify WELD #1: _chat + T1_ARCHITECT now come from the provider primitive
(echelon_engine.atoms.providers.floor_chat), NOT the add_steering god-module.
"""
import ast

import pytest

from echelon_engine.atoms import warmth_update as wu


# ── rod_digest: the zero-model structural rod ─────────────────────────────
def test_rod_digest_extracts_a_date():
    out = wu.rod_digest("decided on 2026-06-18 to migrate the store")
    assert "date=2026-06-18" in out


def test_rod_digest_flags_supersede_language():
    assert "mentions-supersede/revert" in wu.rod_digest("this supersedes the old plan")
    assert "mentions-supersede/revert" in wu.rod_digest("we revert that decision")


def test_rod_digest_flags_archive_and_live():
    assert "mentions-archive/destroy" in wu.rod_digest("this was archived pre-cutover")
    assert "mentions-still-in-force" in wu.rod_digest("the rule is still in force")


def test_rod_digest_flags_dated_session_ledger():
    # the ledger marker is matched in the first 90 chars
    assert "dated-session-ledger" in wu.rod_digest("SESSION 2026-06-18 ledger: did X, Y, Z")


def test_rod_digest_empty_when_no_markers():
    assert wu.rod_digest("a plain timeless fact about gzip") == "no structural markers"


# ── COUNCIL_POOL is exported (atom_kinds depends on importing it) ─────────
def test_council_pool_is_a_nonempty_list():
    assert isinstance(wu.COUNCIL_POOL, list)
    assert len(wu.COUNCIL_POOL) >= 1


# ── WELD #1: _chat / T1_ARCHITECT come from floor_chat, not add_steering ──
def test_weld1_chat_comes_from_floor_chat():
    from echelon_engine.atoms.providers import floor_chat
    assert wu._chat is floor_chat._chat                  # same primitive, re-exported
    assert wu.T1_ARCHITECT == floor_chat.T1_ARCHITECT


def test_weld1_no_add_steering_import_remains():
    # the cut must be REAL in the source: no reach back into the god-module.
    src = open(wu.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("add_steering" in m for m in imported), \
        f"warmth_update must not import add_steering after the weld cut; found {imported}"
    assert not any(m.startswith("echelon_agent") for m in imported), \
        f"no reach back into the old package; found {imported}"
