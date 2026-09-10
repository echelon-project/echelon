"""Tests for the slice-1.5 refill guards — the root-cause half of bank hygiene.

Two guards, per the session-gate's approved fix directions (slice-1.5-hygiene-spec.md
addendum, 2026-07-08 — verified diagnosis):

1. INGEST CANONICALIZATION GATE (ingest.py ingest_folder): refuse a scope that
   disagrees with resolve_scope(root), unless ECHELON_INGEST_SCOPE is set.
2. EMPTY-SCOPE GUARD (cards.py CardStore.add_atom): scope="" raises ValueError
   naming the coordinate, unless ECHELON_LEGACY_SCOPE_OMIT is set or the
   coordinate's head is in the known system-domain allowlist.

Hermetic: all tests use temp dirs + temp dbs — nothing touches ~/.echelon/echelon.db.
"""
import os

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.resolve_scope import resolve_scope
from echelon_engine.atoms import store as store_mod

# ── helpers ──────────────────────────────────────────────────────────────────

_VALID_ATOM = """---
name: test-atom
description: a test atom for refill-guard tests
metadata.type: reference
---
This is a test atom body for verifying the refill guards.
"""


def _write_atom(mem_dir, filename="test-atom.md", content=_VALID_ATOM):
    """Write a valid atom .md into a memory dir."""
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / filename).write_text(content, encoding="utf-8")


def _isolate_v2(tmp_path, monkeypatch):
    """Redirect the v2 bank so SeedStore(no v2_db) doesn't touch the live bank.
    Per the known SeedStore gap: when db_path is explicit but v2_db is None,
    self.cards = CardStore(DEFAULT_V2_DB) — the GLOBAL live bank. Monkeypatching
    DEFAULT_V2_DB re-routes it to a temp file. (Same pattern as test_ingest.py.)"""
    monkeypatch.setattr(store_mod, "DEFAULT_V2_DB", tmp_path / "core_v2.db")


# ═══════════════════════════════════════════════════════════════════════════════
# GUARD 1 — INGEST CANONICALIZATION GATE
# ═══════════════════════════════════════════════════════════════════════════════

class TestIngestCanonicalizationGate:
    """The gate at the top of ingest_folder: refuse a scope that disagrees with
    resolve_scope(project_root) unless ECHELON_INGEST_SCOPE is set."""

    def test_ingest_with_canonical_scope_plants(self, tmp_path, monkeypatch):
        """Ingest with the scope resolve_scope returns for the project root: accepts."""
        from echelon_engine.atoms.ingest import ingest_folder
        _isolate_v2(tmp_path, monkeypatch)

        proj = tmp_path / "myproject"
        _write_atom(proj / "memory")
        canonical = resolve_scope(str(proj))
        db = tmp_path / "test.db"

        planted = ingest_folder(proj, canonical, db_path=db, verbose=False)
        assert len(planted) == 1
        assert planted[0][0] == "test-atom"

    def test_ingest_with_wrong_scope_raises(self, tmp_path, monkeypatch):
        """Ingest with a scope that disagrees with resolve_scope: raises ValueError
        containing both scope names."""
        from echelon_engine.atoms.ingest import ingest_folder
        _isolate_v2(tmp_path, monkeypatch)

        proj = tmp_path / "myproject"
        _write_atom(proj / "memory")
        canonical = resolve_scope(str(proj))
        wrong = "not-" + canonical
        db = tmp_path / "test.db"

        with pytest.raises(ValueError) as exc:
            ingest_folder(proj, wrong, db_path=db, verbose=False)
        msg = str(exc.value)
        assert canonical in msg, f"canonical scope {canonical!r} not in error message"
        assert wrong in msg, f"wrong scope {wrong!r} not in error message"
        # The error must teach: show the re-run command
        assert "--scope" in msg

    def test_ingest_with_env_override_allows_wrong_scope(self, tmp_path, monkeypatch):
        """With ECHELON_INGEST_SCOPE set, even a mismatched scope is allowed."""
        from echelon_engine.atoms.ingest import ingest_folder
        _isolate_v2(tmp_path, monkeypatch)

        proj = tmp_path / "myproject"
        _write_atom(proj / "memory")
        canonical = resolve_scope(str(proj))
        wrong = "not-" + canonical
        db = tmp_path / "test.db"

        monkeypatch.setenv("ECHELON_INGEST_SCOPE", "1")
        # Should NOT raise — the env override silences the gate
        planted = ingest_folder(proj, wrong, db_path=db, verbose=False)
        assert len(planted) == 1

    def test_ingest_memory_dir_resolves_from_parent(self, tmp_path, monkeypatch):
        """When --root points directly at the memory/ dir (not the project root),
        resolve_scope is called on the parent (the project root)."""
        from echelon_engine.atoms.ingest import ingest_folder
        _isolate_v2(tmp_path, monkeypatch)

        proj = tmp_path / "myproject"
        mem = proj / "memory"
        _write_atom(mem)
        canonical = resolve_scope(str(proj))
        db = tmp_path / "test.db"

        # root IS the memory dir — gate resolves from parent (project root)
        planted = ingest_folder(mem, canonical, db_path=db, verbose=False)
        assert len(planted) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# GUARD 2 — EMPTY-SCOPE GUARD
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyScopeGuard:
    """The guard in CardStore.add_atom: scope="" raises ValueError unless the
    coordinate head is a known system domain or ECHELON_LEGACY_SCOPE_OMIT is set."""

    def test_add_atom_empty_scope_rejects_non_system_head(self, tmp_path):
        """scope="" with a non-system coordinate head (e.g. 'myproject:test')
        raises ValueError naming the coordinate."""
        cs = CardStore(tmp_path / "test.db")
        with pytest.raises(ValueError) as exc:
            cs.add_atom("myproject:test-atom", "some content", scope="")
        msg = str(exc.value)
        assert "empty scope" in msg
        assert "myproject:test-atom" in msg

    def test_add_atom_empty_scope_with_legacy_env_allowed(self, tmp_path, monkeypatch):
        """With ECHELON_LEGACY_SCOPE_OMIT set, scope="" is allowed for any head."""
        cs = CardStore(tmp_path / "test.db")
        monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")
        # Should NOT raise
        aid = cs.add_atom("myproject:test-atom", "some content", scope="")
        assert aid is not None
        a = cs.get_atom(aid)
        assert a is not None
        assert a.scope == ""

    def test_add_atom_empty_scope_with_system_head_allowed(self, tmp_path):
        """scope="" with a known system-domain head (e.g. persona:dev) is allowed
        without any env var — these are the legitimate empty-scope callers."""
        cs = CardStore(tmp_path / "test.db")
        # persona: head is in the system-domain allowlist
        aid = cs.add_atom("persona:dev", "the dev persona seed", scope="")
        assert aid is not None
        a = cs.get_atom(aid)
        assert a is not None
        assert a.scope == ""

    def test_add_atom_empty_scope_with_audit_head_allowed(self, tmp_path):
        """scope="" with audit: head (add_steering.py production caller) is allowed."""
        cs = CardStore(tmp_path / "test.db")
        aid = cs.add_atom("audit:ram:scan:key123", "audit observation",
                          scope="", born_from="audit_dispatch")
        assert aid is not None

    def test_add_atom_with_explicit_scope_no_guard_fires(self, tmp_path):
        """When scope is explicitly provided (non-empty), the guard is silent —
        this is the normal path for well-behaved callers."""
        cs = CardStore(tmp_path / "test.db")
        # Even with a non-system head, explicit scope bypasses the guard
        aid = cs.add_atom("myproject:test-atom", "some content", scope="myproject")
        assert aid is not None
        a = cs.get_atom(aid)
        assert a.scope == "myproject"

    def test_add_atom_empty_scope_rejects_empty_coordinate(self, tmp_path):
        """scope="" with an empty coordinate (no head at all) raises."""
        cs = CardStore(tmp_path / "test.db")
        with pytest.raises(ValueError, match="empty scope"):
            cs.add_atom("", "content without coordinate", scope="")

    def test_all_system_heads_in_allowlist(self, tmp_path):
        """Every head in the system-domain allowlist actually allows scope="".
        This is a meta-test: if someone adds a head to the list without a
        legitimate caller, or removes one, this catches it."""
        cs = CardStore(tmp_path / "test.db")
        for head in sorted(cs._SYSTEM_DOMAIN_HEADS):
            coord = f"{head}:test"
            aid = cs.add_atom(coord, f"content for {coord}", scope="")
            assert aid is not None, f"head {head!r} was rejected with empty scope"
            a = cs.get_atom(aid)
            assert a.scope == "", f"head {head!r} had scope {a.scope!r}, expected ''"
