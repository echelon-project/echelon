"""Write-time invariant guard tests — one test per guard that proves the guard
CHANGES THE OUTCOME, not just that code runs.

Guards:
  1. ingest cross-scope body dedup
  2. disclaim successor requirement
  3. compile count-drop (two-roots trap)
  4. heal scan findings carry relation
"""
from __future__ import annotations

import json

import pytest

from echelon_engine.atoms import ingest, store as store_mod
from echelon_engine.atoms import correct, reflex, immune, cards as cards_mod
from echelon_engine.atoms.cards import CardStore


# ── helpers ─────────────────────────────────────────────────────────────────
def _write_atom(d, fname, name, desc, body, extra_meta=""):
    (d / fname).write_text(
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: project{extra_meta}\n---\n{body}",
        encoding="utf-8")


def _isolate_cards(tmp_path, monkeypatch):
    """Redirect CardStore's default db to temp so tests don't touch the live bank."""
    monkeypatch.setattr(cards_mod, "DEFAULT_V2_DB", tmp_path / "core_v2.db")


# ═══════════════════════════════════════════════════════════════════════════════
# GUARD 1: ingest cross-scope body dedup
# ═══════════════════════════════════════════════════════════════════════════════
class TestIngestCrossScopeDedup:
    """Prove that ingesting the same BODY into a different scope is BLOCKED."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_mod, "DEFAULT_V2_DB", tmp_path / "core_v2.db")
        monkeypatch.setattr(store_mod, "DEFAULT_DB", tmp_path / "core.db")
        monkeypatch.setenv("ECHELON_INGEST_SCOPE", "1")
        self.tmp = tmp_path
        self.db_path = tmp_path / "core.db"

    def test_same_scope_reingest_is_idempotent_and_silent(self, capsys):
        """Prove the load-bearing trap: same-scope idempotent re-ingest MUST remain
        silent and unaffected. This is the normal /wrap path."""
        mem = self.tmp / "memory"
        mem.mkdir()
        body = "the unique lesson content for this atom"
        _write_atom(mem, "a.md", "atom-a", "handle a", body)

        p1 = ingest.ingest_folder(self.tmp, "scope-A", db_path=self.db_path, verbose=False)
        p2 = ingest.ingest_folder(self.tmp, "scope-A", db_path=self.db_path, verbose=False)

        # Same ids, same count — idempotent
        assert p1 == p2
        assert len(p1) == 1

    def test_cross_scope_dup_body_is_skipped(self):
        """An atom whose body already exists verbatim in ANOTHER scope is SKIPPED."""
        # Create two separate project dirs each with their own memory/ folder
        proj_a = self.tmp / "proj_a"; (proj_a / "memory").mkdir(parents=True)
        proj_b = self.tmp / "proj_b"; (proj_b / "memory").mkdir(parents=True)
        body = "the lesson body that travels across scopes"

        _write_atom(proj_a / "memory", "a.md", "atom-a", "handle a", body)
        _write_atom(proj_b / "memory", "b.md", "atom-b", "handle b", body)

        # Plant into scope A — succeeds (using override to bypass cross-scope guard for scope A's own plant)
        p1 = ingest.ingest_folder(proj_a, "scope-A", db_path=self.db_path, verbose=False,
                                   allow_cross_scope_dup=True)
        assert len(p1) == 1

        # Plant into scope B — body already exists in scope A, should be SKIPPED
        p2 = ingest.ingest_folder(proj_b, "scope-B", db_path=self.db_path, verbose=False)
        assert len(p2) == 0  # GUARD: cross-scope dup blocked

    def test_allow_cross_scope_dup_flag_overrides(self):
        """The --allow-cross-scope-dup flag permits the duplicate."""
        proj_a = self.tmp / "proj_a"; (proj_a / "memory").mkdir(parents=True)
        proj_b = self.tmp / "proj_b"; (proj_b / "memory").mkdir(parents=True)
        body = "lesson that deserves to travel"

        _write_atom(proj_a / "memory", "a.md", "atom-a", "handle a", body)
        _write_atom(proj_b / "memory", "b.md", "atom-b", "handle b", body)

        ingest.ingest_folder(proj_a, "scope-A", db_path=self.db_path, verbose=False,
                              allow_cross_scope_dup=True)
        p2 = ingest.ingest_folder(proj_b, "scope-B", db_path=self.db_path, verbose=False,
                                   allow_cross_scope_dup=True)
        assert len(p2) == 1  # overridden: planted despite cross-scope dup

    def test_different_bodies_in_different_scopes_are_not_blocked(self):
        """Different bodies = no conflict. Only byte-identical bodies trigger the guard."""
        proj_a = self.tmp / "proj_a"; (proj_a / "memory").mkdir(parents=True)
        proj_b = self.tmp / "proj_b"; (proj_b / "memory").mkdir(parents=True)

        _write_atom(proj_a / "memory", "a.md", "atom-a", "handle a", "body alpha")
        _write_atom(proj_b / "memory", "b.md", "atom-b", "handle b", "body beta")

        ingest.ingest_folder(proj_a, "scope-A", db_path=self.db_path, verbose=False,
                              allow_cross_scope_dup=True)
        p2 = ingest.ingest_folder(proj_b, "scope-B", db_path=self.db_path, verbose=False)
        assert len(p2) == 1  # different body, no conflict


# ═══════════════════════════════════════════════════════════════════════════════
# GUARD 2: disclaim successor requirement
# ═══════════════════════════════════════════════════════════════════════════════
class TestDisclaimSuccessorGuard:
    """Prove that a bare disclaim (no --superseded-by, no --no-successor) is REFUSED."""

    @pytest.fixture(autouse=True)
    def _store(self, tmp_path):
        self.cs = CardStore(tmp_path / "core_v2.db")

    def test_bare_disclaim_is_refused(self):
        """A disclaim with neither --superseded-by nor --no-successor → refused."""
        # Plant an atom first
        aid = self.cs.add_atom("test-scope:bare-disclaim", "some content", scope="test-scope")
        res = correct.disclaim("test-scope:bare-disclaim", reason="test", store=self.cs)
        assert res.get("ok") is False
        assert "orphan" in res.get("reason", "").lower()

    def test_disclaim_with_superseded_by_succeeds(self):
        """--superseded-by provides the replacement → disclaim succeeds + edge written."""
        aid = self.cs.add_atom("test-scope:old-atom", "old content", scope="test-scope")
        new_id = self.cs.add_atom("test-scope:new-atom", "new content", scope="test-scope")
        res = correct.disclaim("test-scope:old-atom", reason="superseded",
                                superseded_by="test-scope:new-atom", store=self.cs)
        assert res.get("ok") is True, f"Expected ok=True, got {res}"

        # Verify the supersedes edge was written
        edges = self.cs.edges_of(aid, live_only=True)
        supers = [e for e in edges if e["relation"] == "supersedes" and e["dir"] == "out"]
        assert len(supers) == 1
        assert supers[0]["to_id"] == new_id

    def test_disclaim_with_no_successor_succeeds(self):
        """--no-successor = explicit orphan acknowledgment → disclaim succeeds."""
        aid = self.cs.add_atom("test-scope:no-replacement", "content", scope="test-scope")
        res = correct.disclaim("test-scope:no-replacement", reason="false claim",
                                no_successor=True, store=self.cs)
        assert res.get("ok") is True, f"Expected ok=True, got {res}"

    def test_no_successor_ack_settles_antibody4_warn(self):
        """The acknowledgment marker exempts the atom from the orphan_disclaim WARN."""
        self.cs.add_atom("test-scope:acked", "acked content", scope="test-scope")
        correct.disclaim("test-scope:acked", reason="retired",
                         no_successor=True, store=self.cs)
        rep = self.cs.scan(scope="test-scope")
        orphans = [w for w in rep["warn"] if w["rule"] == "orphan_disclaim"]
        assert orphans == [], f"acknowledged orphan still warned: {orphans}"

    def test_no_successor_ack_on_already_disclaimed_atom(self):
        """An atom disclaimed BEFORE the guard existed can be settled retroactively:
        re-running disclaim --no-successor stamps the marker without changing the score."""
        aid = self.cs.add_atom("test-scope:legacy-orphan", "legacy", scope="test-scope")
        # Legacy path: disclaim directly at the store layer (pre-guard behavior, bare).
        self.cs.disclaim_judged("atoms", aid, reason="old bare disclaim")
        rep = self.cs.scan(scope="test-scope")
        assert any(w["rule"] == "orphan_disclaim" for w in rep["warn"])
        before = self.cs.conn.execute("SELECT score FROM atoms WHERE id=?", (aid,)).fetchone()["score"]
        res = correct.disclaim("test-scope:legacy-orphan", no_successor=True, store=self.cs)
        assert res.get("ok") is True and res.get("acknowledged") is True, f"got {res}"
        after = self.cs.conn.execute("SELECT score FROM atoms WHERE id=?", (aid,)).fetchone()["score"]
        assert before == after  # acknowledgment never moves the score
        rep2 = self.cs.scan(scope="test-scope")
        assert not any(w["rule"] == "orphan_disclaim" for w in rep2["warn"])


# ═══════════════════════════════════════════════════════════════════════════════
# GUARD 3: compile count-drop (two-roots trap)
# ═══════════════════════════════════════════════════════════════════════════════
class TestCompileCountDropGuard:
    """Prove that a massive count drop (< 50% of old) aborts with the two-roots message."""

    @pytest.fixture(autouse=True)
    def _ruleset(self, tmp_path, monkeypatch):
        self.ruleset = tmp_path / "reflexes.json"
        monkeypatch.setattr(reflex, "RULESET", self.ruleset)
        self.tmp = tmp_path

    def _atom_md(self, root, name):
        (root / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: test\nmetadata:\n  type: feedback\n"
            f"  reflex: true\n  reflex-event: PreToolUse\n"
            f"  reflex-tool: Bash\n  reflex-match: test-{name}\n"
            f"  reflex-action: warn\n---\n\nGuard for {name}.\n",
            encoding="utf-8")

    def test_normal_compile_no_existing_rules_succeeds(self):
        """First compile with no existing rules: no count guard triggered."""
        root = self.tmp / "mem"; root.mkdir()
        self._atom_md(root, "guard1")
        rc = reflex._main(["compile", "--root", str(root), "--scope", "test"])
        assert rc == 0

    def test_count_drop_below_50_percent_aborts(self):
        """If new count < 50% of old, the guard ABORTS (exit 1)."""
        root = self.tmp / "mem"; root.mkdir()
        # Seed with 10 rules
        for i in range(10):
            self._atom_md(root, f"guard{i}")
        rc = reflex._main(["compile", "--root", str(root), "--scope", "test"])
        assert rc == 0

        # Now remove 8 atoms and recompile from the SAME dir → count drops 10→2 (80% drop)
        for i in range(8):
            (root / f"guard{i}.md").unlink()
        rc = reflex._main(["compile", "--root", str(root), "--scope", "test"])
        assert rc == 1  # GUARD: count-drop aborted

    def test_force_overrides_count_drop(self):
        """--force bypasses the count-drop guard."""
        root = self.tmp / "mem"; root.mkdir()
        for i in range(10):
            self._atom_md(root, f"guard{i}")
        reflex._main(["compile", "--root", str(root), "--scope", "test"])

        for i in range(8):
            (root / f"guard{i}.md").unlink()
        rc = reflex._main(["compile", "--root", str(root), "--scope", "test", "--force"])
        assert rc == 0  # force overrides


# ═══════════════════════════════════════════════════════════════════════════════
# GUARD 4: heal scan findings carry relation
# ═══════════════════════════════════════════════════════════════════════════════
class TestScanFindingsCarryRelation:
    """Prove that edge scan findings now include the 'relation' field so heal()
    targets the EXACT broken edge, not an ambiguous first-match."""

    @pytest.fixture(autouse=True)
    def _store(self, tmp_path):
        self.cs = CardStore(tmp_path / "core_v2.db")

    def test_dangling_edge_finding_carries_relation(self):
        """A dangling edge finding must include the 'relation' field."""
        # Create two atoms and a live edge, then delete one atom to create a dangling edge
        a1 = self.cs.add_atom("test:atom1", "content 1", scope="test")
        a2 = self.cs.add_atom("test:atom2", "content 2", scope="test")
        self.cs.link(a1, a2, "refs")

        # Now create a dangling edge: link to a non-existent atom
        # We can't directly insert a bad edge via the CardStore API, so we test via
        # the scan of a legitimate edge_to_disclaimed scenario instead, which also
        # carries the relation now.
        self.cs.disclaim_judged("atoms", a2)
        rep = self.cs.scan(scope="test")
        edge_findings = [f for f in rep["fail"] if f["rule"] in (
            "edge_to_disclaimed", "dangling_edge_from", "dangling_edge_to")]
        for f in edge_findings:
            assert "relation" in f, (
                f"Finding {f['rule']} must carry 'relation' field; got keys {sorted(f.keys())}")
            assert f["relation"] == "refs"

    def test_heal_uses_finding_relation_directly(self, monkeypatch):
        """heal() uses the relation from the scan finding, not _relation_of guess."""
        a1 = self.cs.add_atom("test:atom1", "content 1", scope="test")
        a2 = self.cs.add_atom("test:atom2", "content 2", scope="test")
        self.cs.link(a1, a2, "refs")
        self.cs.disclaim_judged("atoms", a2)

        # Monkeypatch _relation_of to return a WRONG relation — if heal uses the
        # finding's relation field, it will tombstone "refs" correctly; if it falls
        # back to _relation_of, it would get the wrong relation.
        original = immune._relation_of
        def _false_relation(cs, fid, tid):
            return "subsumes"  # wrong relation!
        monkeypatch.setattr(immune, "_relation_of", _false_relation)

        rep = immune.heal(scope="test", store=self.cs, dry_run=False)
        healed = [h for h in rep["healed"] if h["rule"] == "edge_to_disclaimed"]
        assert len(healed) == 1
        assert healed[0]["relation"] == "refs"  # from the finding, NOT the monkeypatched _relation_of
        assert healed[0].get("unlinked") is True  # correctly tombstoned the actual edge
