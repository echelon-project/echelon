"""OPEN-0025/0033 — the immune scan's edge_to_disclaimed FAIL class, split by SUCCESSOR.

THE STANDING BUG (mined 2026-08-12, memory
[[immune-scan-and-inspect-disagree-on-disclaimed]] and
[[disputing-a-hub-atom-poisons-every-edge-into-it]]):

  ANTIBODY 2 flagged EVERY live edge into a disclaimed atom as
  `edge_to_disclaimed` — "route to a known lie". But a disclaim that names a
  successor writes a live `supersedes` edge (the ANTIBODY-4 contract): the truth
  did not vanish, it MOVED to the successor. An inbound edge into such an atom is
  not routing to a lie — it is routing to a memory that has been CORRECTED, and
  the honest repair is to RE-POINT the edge at the successor, not to tombstone it
  or leave an uncleaarable FAIL.

  So the FAIL count grew every time a new atom legitimately linked to the
  superseded slug, and no content edit could clear it — because by the successor's
  account nothing was wrong. That is the scan/inspect disagreement.

THE FIX (this file pins it):
  - edge into a disclaimed atom WITH a live supersedes successor  → `edge_to_superseded`
    (FAIL, healable by RE-POINTING to the successor).
  - edge into a disclaimed atom with NO successor                 → `edge_to_disclaimed`
    (FAIL, healable by TOMBSTONE — a genuine route to a lie with nowhere to go;
    this is the disputed-hub-with-no-successor case, correctly still a FAIL).

Every test runs on a fresh tmp db — the live bank is never touched.
"""
import pytest

from echelon_engine.atoms.cards import CardStore, RELATION_TYPES
from echelon_engine.atoms import immune


@pytest.fixture(autouse=True)
def _legacy_scope_omit(monkeypatch):
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


def _disclaim(store, atom_id):
    """Disclaim an atom the way the engine does (drives it to the judged floor)."""
    store.disclaim_judged("atoms", atom_id, reason="test disclaim")


# ── the successor case: edge into a disclaimed-BUT-superseded atom ─────────────
class TestEdgeIntoSupersededAtom:
    def _wire(self, store):
        """A links to OLD; OLD is disclaimed and superseded by NEW (live supersedes edge).
        This is the delta-shop-margin-ceiling shape: the truth moved to NEW."""
        old = store.add_atom("tooling:margin-ceiling", "the old (superseded-on-the-number) claim")
        new = store.add_atom("tooling:margin-ceiling-corrected", "the corrected figure")
        linker = store.add_atom("tooling:some-later-atom", "an atom that links to the claim")
        store.link(linker, old, relation="depends_on")
        _disclaim(store, old)
        store.link(old, new, "supersedes")   # the ANTIBODY-4 contract edge
        return linker, old, new

    def test_it_is_not_a_route_to_a_known_lie(self, store):
        """The edge into a superseded atom must NOT be reported as edge_to_disclaimed —
        the truth moved to the successor, it is not a lie with nowhere to go."""
        linker, old, new = self._wire(store)
        rep = store.scan()
        disclaimed_fails = [f for f in rep["fail"]
                            if f["rule"] == "edge_to_disclaimed" and f["to_id"] == old]
        assert disclaimed_fails == [], (
            "an edge into a disclaimed-but-superseded atom was flagged as a route to a "
            "known lie — the uncleaarable FAIL this item fixes")

    def test_it_is_reported_as_edge_to_superseded_with_the_successor(self, store):
        """It IS still a FAIL (the edge should be re-pointed), but a DISTINCT, actionable
        one that names the successor so heal can re-point it."""
        linker, old, new = self._wire(store)
        rep = store.scan()
        sup = [f for f in rep["fail"]
               if f["rule"] == "edge_to_superseded" and f["from_id"] == linker and f["to_id"] == old]
        assert len(sup) == 1
        assert sup[0]["successor_id"] == new
        assert sup[0]["relation"] == "depends_on"

    def test_the_supersedes_edge_itself_is_never_flagged(self, store):
        """The OLD--supersedes-->NEW edge is the correction itself; it must be exempt
        (rel == 'supersedes' was always exempt, keep it)."""
        linker, old, new = self._wire(store)
        rep = store.scan()
        assert not any(f["to_id"] == new for f in rep["fail"])

    def test_heal_repoints_the_edge_to_the_successor(self, store):
        """heal must RE-POINT (tombstone old edge + link to successor), not tombstone-only —
        the estate keeps the connection, now pointing at the truth."""
        linker, old, new = self._wire(store)
        immune.heal(scope=None, store=store)
        live = store.edges_of(linker, live_only=True)
        outs = [(e["to_id"], e["relation"]) for e in live if e["dir"] == "out"]
        assert (new, "depends_on") in outs, "edge was not re-pointed to the successor"
        assert (old, "depends_on") not in outs, "old edge into the lie was not tombstoned"

    def test_heal_dry_run_names_the_repoint_without_touching(self, store):
        linker, old, new = self._wire(store)
        rep = immune.heal(scope=None, store=store, dry_run=True)
        # nothing changed
        outs = [(e["to_id"], e["relation"]) for e in store.edges_of(linker) if e["dir"] == "out"]
        assert (old, "depends_on") in outs
        # but the repoint was planned
        assert any(h["rule"] == "edge_to_superseded" and h.get("successor_id") == new
                   for h in rep["healed"])

    def test_re_scan_after_heal_is_clean_of_this_class(self, store):
        linker, old, new = self._wire(store)
        immune.heal(scope=None, store=store)
        rep = store.scan()
        assert not any(f["rule"] in ("edge_to_disclaimed", "edge_to_superseded")
                       for f in rep["fail"]), "heal did not clear the route-to-a-corrected-memory FAILs"


# ── the no-successor case: a disputed hub with nowhere to go ──────────────────
class TestEdgeIntoDisclaimedOrphan:
    def _wire(self, store):
        """A links to HUB; HUB is disclaimed with NO successor (the disputed-wrap-hub case).
        This one IS a genuine route to a known lie — no truth to re-point to."""
        hub = store.add_atom("tooling:prior-wrap", "a mostly-true hub with one stale claim")
        linker = store.add_atom("tooling:child-atom", "an atom from that session")
        store.link(linker, hub, relation="depends_on")
        _disclaim(store, hub)
        return linker, hub

    def test_stays_edge_to_disclaimed(self, store):
        """No successor → it remains a route to a known lie, healed by tombstone. This is
        the disputed-hub case the memory atom describes; it is CORRECTLY still a FAIL."""
        linker, hub = self._wire(store)
        rep = store.scan()
        fails = [f for f in rep["fail"] if f["rule"] == "edge_to_disclaimed" and f["to_id"] == hub]
        assert len(fails) == 1
        assert fails[0]["from_id"] == linker

    def test_heal_tombstones_the_edge(self, store):
        linker, hub = self._wire(store)
        immune.heal(scope=None, store=store)
        outs = [(e["to_id"], e["relation"]) for e in store.edges_of(linker, live_only=True)
                if e["dir"] == "out"]
        assert (hub, "depends_on") not in outs
