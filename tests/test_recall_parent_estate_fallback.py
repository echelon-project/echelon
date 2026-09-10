"""R-0154 slice 2: a child-room scope draws its PARENT ESTATE scope on recall, so day-one recall
in a freshly-split room is not cold. The fallback is a runtime depends_on edge (reach, not copy —
the dedup law), resolved from the room registry. These tests pin the resolver + the edge wiring
without a live bank."""
import pytest

from echelon_engine.atoms import recall
from echelon_engine import workcycle as wc


@pytest.fixture
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _register(tmp_path, name, scope, parent=None):
    root = tmp_path / name.replace("/", "_")
    root.mkdir(parents=True, exist_ok=True)
    wc.init(root, estate=name, scope=scope)
    entry = wc.register_room(root, name=name, scope=scope)
    if parent:
        rooms = wc._registry_entries()
        rooms[name] = {**rooms[name], "parent": parent}
        wc._save_registry(rooms)
    return root


def test_parent_estate_scope_resolves_a_child_to_its_parents_scope(tmp_path, _isolated_registry):
    _register(tmp_path, "echelon", scope="echelon")
    _register(tmp_path, "echelon/framework", scope="echelon-framework", parent="echelon")
    assert recall._parent_estate_scope("echelon-framework") == "echelon"


def test_parent_estate_scope_is_none_for_a_top_level_scope(tmp_path, _isolated_registry):
    _register(tmp_path, "echelon", scope="echelon")
    assert recall._parent_estate_scope("echelon") is None


def test_parent_estate_scope_never_raises_without_a_registry(monkeypatch):
    monkeypatch.setenv("ECHELON_HOME", "/nonexistent-echelon-home-xyz")
    assert recall._parent_estate_scope("whatever") is None


def test_warm_probe_wires_a_depends_on_edge_child_to_parent(tmp_path, _isolated_registry, monkeypatch):
    _register(tmp_path, "echelon", scope="echelon")
    _register(tmp_path, "echelon/framework", scope="echelon-framework", parent="echelon")
    captured = {}

    class _FakeGraph:
        def __init__(self, *a, **k):
            self.edges = []
        def add_edge(self, frm, to, rel="shares_soul"):
            self.edges.append((frm, to, rel)); captured["edges"] = self.edges
        def neighbours(self, scope):
            return [(t, 0.8, r) for f, t, r in self.edges if f == scope]

    monkeypatch.setattr(recall, "ScopeGraph", _FakeGraph)
    # capture the graph the moment warmth is called — the edge must already be wired by then.
    def _capture_warmth(*a, **k):
        captured["graph_at_call"] = list(getattr(k.get("scope_graph"), "edges", []))
        raise AssertionError("stop after edge capture")
    monkeypatch.setattr(recall, "warmth", _capture_warmth)
    monkeypatch.setattr(recall, "SeedStore", lambda *a, **k: type("S", (), {"cards": None, "count": lambda *_: 0})())
    with pytest.raises(AssertionError, match="stop after edge capture"):
        recall.warm_probe("echelon-framework", "the forge door", judge="off")
    assert ("echelon-framework", "echelon", "depends_on") in captured.get("graph_at_call", [])
