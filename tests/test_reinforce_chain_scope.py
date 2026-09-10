"""OPEN-0021 — the reinforce_card chain walker must NOT cross estates (scopes).

THE BUG (mined 2026-08-06): reinforce_card propagates a decayed chain share BACK
along the `prev` edge to the card that enabled the one that just earned. But a
`prev` edge can link cards in DIFFERENT estates — echelon, mol, alpha-app and gamma-support
all share ONE bank. When it does, an echelon run's success flowed credit into a
mol card and its atoms, inflating another estate's weights along the forward pass.
Estate isolation is the property the whole scoping model rests on; the chain walker
breached it silently.

THE FIX: derive each card's scope from its refs (card_scope) and STOP the walk at
an estate boundary — if the upstream card resolves to a different scope, it is not
credited. A same-scope chain still propagates; a chain whose scope is unresolvable
('' both sides) still propagates (it never crossed a boundary to begin with).

Every test runs on a fresh tmp db — the live bank is never touched.
"""
import pytest

from echelon_engine.atoms.cards import CardStore


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


def _card_in_scope(store, scope, name, prev=""):
    """A card whose single ref is an atom in `scope`, so card_scope() resolves to `scope`."""
    coord = f"{scope}:{name}"
    store.add_atom(coord, f"{name} content", scope=scope)
    return store.add_card(label=name, refs=[coord], prev=prev)


class TestChainStopsAtEstateBoundary:
    def test_credit_does_not_cross_into_a_foreign_estate(self, store):
        """down(echelon) --prev--> up(mol): reinforcing down must NOT move up's score."""
        up = _card_in_scope(store, "mol", "mol-upstream")
        down = _card_in_scope(store, "echelon", "echelon-downstream", prev=up)
        up_before = store.card(up).score
        rep = store.reinforce_card(down, q=90.0)
        up_after = store.card(up).score
        assert up_after == up_before, "credit crossed the estate boundary into a foreign scope"
        assert rep["chain_credited"] == 0, "the walk did not stop at the boundary"

    def test_foreign_upstream_atoms_are_not_credited_either(self, store):
        """The upstream card's ATOMS must also stay untouched — the leak was into ITS atoms too."""
        store.add_atom("mol:mol-atom", "a mol atom", scope="mol")
        up = store.add_card(label="mol-up", refs=["mol:mol-atom"])
        down = _card_in_scope(store, "echelon", "ech-down", prev=up)
        before = store.get_atom(store.atom_id_for_coordinate("mol:mol-atom")).score
        store.reinforce_card(down, q=90.0)
        after = store.get_atom(store.atom_id_for_coordinate("mol:mol-atom")).score
        assert after == before, "a foreign estate's atom earned weight from another estate's run"

    def test_same_estate_chain_still_propagates(self, store):
        """The guard must not break in-estate chains — same scope still earns upstream."""
        up = _card_in_scope(store, "echelon", "ech-up")
        down = _card_in_scope(store, "echelon", "ech-down", prev=up)
        up_before = store.card(up).score
        rep = store.reinforce_card(down, q=90.0)
        up_after = store.card(up).score
        assert up_after > up_before, "an in-estate chain stopped propagating"
        assert rep["chain_credited"] == 1

    def test_unresolvable_scope_still_propagates(self, store, monkeypatch):
        """A chain whose scopes are both unresolvable ('' both sides) never crossed a
        boundary — it must keep flowing (back-compat for legacy single-estate chains)."""
        monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")
        # system-domain heads write with empty scope, so card_scope() returns '' for both.
        store.add_atom("tooling:up-tool", "u", scope="")
        up = store.add_card(label="up", refs=["tooling:up-tool"])
        store.add_atom("tooling:down-tool", "d", scope="")
        down = store.add_card(label="down", refs=["tooling:down-tool"], prev=up)
        assert store.card_scope(store.card(up).refs) == ""
        up_before = store.card(up).score
        rep = store.reinforce_card(down, q=90.0)
        assert store.card(up).score > up_before
        assert rep["chain_credited"] == 1

    def test_boundary_stop_is_one_hop_deep_only(self, store):
        """A 3-card chain ech->ech->mol credits the middle (same estate) but stops before
        the mol card — the boundary halts the walk exactly where scopes diverge, not sooner."""
        mol_up = _card_in_scope(store, "mol", "mol-top")
        ech_mid = _card_in_scope(store, "echelon", "ech-mid", prev=mol_up)
        ech_down = _card_in_scope(store, "echelon", "ech-down", prev=ech_mid)
        mid_before = store.card(ech_mid).score
        mol_before = store.card(mol_up).score
        rep = store.reinforce_card(ech_down, q=90.0)
        assert store.card(ech_mid).score > mid_before, "same-estate middle hop was not credited"
        assert store.card(mol_up).score == mol_before, "walk crossed the boundary at the far hop"
        assert rep["chain_credited"] == 1, "expected exactly one in-estate hop before the boundary"
