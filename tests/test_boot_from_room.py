"""Step 6 of the charter build order: governor boot-from-room + the atom migration sweep."""
import json
from types import SimpleNamespace

import pytest

from echelon_engine import workcycle as wc
from echelon_engine.agent.governor import StepEvent, compose, room_slice


@pytest.fixture
def room(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    r = wc.init(root, estate="acme", type_="engine", scope="acme")
    wc.checkpoint(r, summary="building the ops verbs", completed=[], remaining=["step 6"],
                  files=["echelon_engine/ops.py"])
    wc._write_json(r / "cursor.json", {**wc._read_json(r / "cursor.json", {}), "task": "ops deploy gate"})
    wc.open_item(r, "the governor must boot from the room")
    return r


def _fn(hits):
    return lambda q, store, scope: [SimpleNamespace(seed=SimpleNamespace(id=i, coordinate=f"acme:{i}", content=f"[{i}] body of {i}"), score=s)
                                    for i, s in hits.get(q, [])]


def test_room_slice_reads_brief_open_items_and_cursor_facets(room):
    text, facets = room_slice(room)
    assert text.startswith("ROOM acme · type engine") and "OPEN OPEN-0001 the governor must boot" in text
    assert "JOURNAL last" in text
    assert ("room:task", "ops deploy gate") in facets and ("file:ops.py", "ops.py") in facets


def test_room_slice_is_empty_without_a_room_and_never_raises(tmp_path):
    assert room_slice(None) == ("", [])
    assert room_slice(tmp_path / "nowhere") == ("", []) or room_slice(tmp_path / "nowhere")[0].startswith("ROOM unavailable")


def test_compose_puts_the_room_first_and_asks_the_bank_about_the_cursor(room):
    fn = _fn({"ops.py": [("lesson-a", 0.9)], "ops deploy gate": [("lesson-b", 0.7)]})
    pkg = compose(StepEvent(task="t"), None, "acme", warmth_fn=fn, room=room)
    lines = pkg.composed_text.splitlines()
    assert lines[1] == "-- ROOM (state, first) --" and "-- BANK (lessons) --" in lines
    assert lines.index("-- BANK (lessons) --") < lines.index("[lesson-a] body of lesson-a")
    assert {e["facet"] for e in pkg.entries} == {"file:ops.py", "room:task"}


def test_compose_without_a_room_is_unchanged(room):
    pkg = compose(StepEvent(task="t"), None, "acme", warmth_fn=_fn({}))
    assert "-- ROOM" not in pkg.composed_text and pkg.entries == []


def test_event_facets_win_over_room_facets_on_the_same_name(room):
    seen = []
    def fn(q, store, scope):
        seen.append(q); return []
    compose(StepEvent(task="t", files=["x/ops.py"]), None, "acme", warmth_fn=fn, room=room)
    assert seen.count("ops.py") == 1


# ── the migration sweep ──────────────────────────────────────────────────────

@pytest.fixture
def atoms(tmp_path):
    d = tmp_path / "memory"; d.mkdir()
    for n in ("session-wrap-2026-08-01.md", "index-rollup-old.md", "OPEN-thing.md", "a-real-lesson.md"):
        (d / n).write_text(f"---\nname: {n[:-3]}\n---\nbody", encoding="utf-8")
    return d


def test_import_atoms_dry_run_copies_state_lists_disputes_and_touches_no_bank(room, atoms):
    calls = []
    m = wc.import_atoms(room, atoms, dispute_fn=lambda n, r: calls.append(n))
    names = {r["name"]: r for r in m["rows"]}
    assert set(names) == {"session-wrap-2026-08-01", "index-rollup-old", "OPEN-thing"}
    assert names["OPEN-thing"]["dispute"] is False and names["session-wrap-2026-08-01"]["dispute"] is True
    assert all(not r["disputed"] for r in m["rows"]) and calls == []
    imported = room / "journal" / "imported"
    assert (imported / "session-wrap-2026-08-01.md").read_text(encoding="utf-8").endswith("body")
    assert not (imported / "a-real-lesson.md").exists() and (atoms / "session-wrap-2026-08-01.md").exists()
    man = json.loads((imported / "_manifest.json").read_text(encoding="utf-8"))
    assert man["apply"] is False and len(man["rows"]) == 3
    rows = [json.loads(l) for f in (room / "journal").glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    rec = [r for r in rows if r["kind"] == "import-atoms"][-1]
    assert rec["imported"] == 3 and rec["disputed"] == 0 and rec["dispute_pending"] == 2


def test_import_atoms_apply_disputes_only_the_pure_state_kinds(room, atoms):
    calls = []
    def fake(n, reason):
        if n == "index-rollup-old":
            raise RuntimeError("bank says no")
        calls.append(n)
    m = wc.import_atoms(room, atoms, apply=True, dispute_fn=fake)
    names = {r["name"]: r for r in m["rows"]}
    assert calls == ["session-wrap-2026-08-01"]
    assert names["session-wrap-2026-08-01"]["disputed"] is True
    assert names["index-rollup-old"]["disputed"] is False and "bank says no" in names["index-rollup-old"]["error"]
    assert names["OPEN-thing"]["disputed"] is False


def test_cli_import_atoms_dry_run(room, atoms, monkeypatch, capsys):
    monkeypatch.setattr(wc, "room_path", lambda *a, **k: room)
    assert wc.main(["import-atoms", "--root", str(atoms)]) == 0
    out = capsys.readouterr().out
    assert "imported 3 state atom(s)" in out and "dispute pending 2" in out and "dry-run" in out
