"""The reshaped swarm door (owner, 2026-08-19): the api|agent mode fork, the
anti-slurp context law with receipts, and the cartridge-aware type error.
All $0 — no provider is ever reached."""
from __future__ import annotations

import pytest

from echelon_engine.swarm import main as swarm_main
from echelon_engine.swarm.manifest import build, SlurpRefused


# ── the context manifest ─────────────────────────────────────────────────────

def _tree(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n" * 5, encoding="utf-8")
    (tmp_path / "b.js").write_text("console.log('b');\n" * 5, encoding="utf-8")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "c.py").write_text("print('c')\n" * 5, encoding="utf-8")
    return tmp_path


def test_bare_root_is_refused(tmp_path):
    _tree(tmp_path)
    with pytest.raises(SlurpRefused):
        build(root=str(tmp_path))


def test_picks_narrow_and_receipt_reports_exclusions(tmp_path):
    _tree(tmp_path)
    m = build(root=str(tmp_path), picks=["*.py"])
    included = {rel for rel, _ in m.included}
    assert included == {"a.py", "src/c.py"}
    excluded = {rel for rel, _ in m.excluded}
    assert "b.js" in excluded
    r = m.receipt()
    assert "CONTEXT RECEIPT" in r and "+ a.py" in r and "- b.js" in r


def test_explicit_budget_satisfies_the_law_and_cuts(tmp_path):
    _tree(tmp_path)
    m = build(root=str(tmp_path), budget_kb=0)  # 0KB: everything over budget
    assert m.included == []
    assert any(why == "over budget" for _, why in m.excluded)


def test_explicit_files_always_ride_first(tmp_path):
    _tree(tmp_path)
    m = build(root=str(tmp_path), files=["b.js"], picks=["*.py"])
    assert m.included[0][0] == "b.js"  # explicit outranks picks


def test_all_flag_declares_the_slurp(tmp_path):
    _tree(tmp_path)
    m = build(root=str(tmp_path), allow_all=True)
    assert len(m.included) == 3
    assert m.hash  # manifest hash present for the cache split


def test_missing_explicit_file_is_reported_not_silent(tmp_path):
    m = build(files=[str(tmp_path / "ghost.py")])
    assert m.included == []
    assert m.excluded and m.excluded[0][1] == "not found"


# ── the door ─────────────────────────────────────────────────────────────────

def test_unknown_type_that_is_a_cartridge_names_both_doors(capsys):
    rc = swarm_main(["api", "--type", "architect", "--goal", "x"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "CARTRIDGE, not a swarm type" in err
    assert "swarm api --type" in err and "swarm agent --cartridge" in err


def test_unknown_mode_is_named(capsys):
    rc = swarm_main(["fly", "--goal", "x"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "api | agent" in err


def test_bare_root_refusal_reaches_the_cli_before_any_spend(tmp_path, capsys):
    _tree(tmp_path)
    rc = swarm_main(["api", "--type", "plan", "--goal", "x",
                     "--root", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--pick" in err and "--budget-kb" in err


def test_agent_mode_dispatches_one_seat_per_cartridge(monkeypatch, tmp_path):
    calls = []

    def fake_dispatch(goal, **kw):
        calls.append((goal, kw))
        return {"status": "completed", "outcome": {"ok": True, "detail": "d"}}

    import echelon_engine.agent.partner as partner
    monkeypatch.setattr(partner, "dispatch", fake_dispatch)
    rc = swarm_main(["agent", "--cartridge", "scribe", "--cartridge", "qa",
                     "--goal", "document it", "--folder", str(tmp_path)])
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][1]["cartridges"] == ["scribe"]
    assert calls[1][1]["cartridges"] == ["qa"]


def test_agent_mode_red_seat_sets_exit_code(monkeypatch, tmp_path):
    def fake_dispatch(goal, **kw):
        return {"status": "completed", "outcome": {"ok": False, "detail": "no change"}}

    import echelon_engine.agent.partner as partner
    monkeypatch.setattr(partner, "dispatch", fake_dispatch)
    rc = swarm_main(["agent", "--cartridge", "scribe",
                     "--goal", "x", "--folder", str(tmp_path)])
    assert rc == 1
