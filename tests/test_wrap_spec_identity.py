"""Wrap-spec identity + collision safety (owner defect 2026-08-28).

Two concurrent sessions wrapping the same day collided on the date-keyed
`_wrap/auto-spec-<date>.json` default — the second composition silently
REPLACED the first session's arcs, and the JSON carried no identifier to even
notice. These pins hold the fix: every composed spec carries an identity block,
the CLI default filename cannot collide, and the run path refuses a spec
composed for another scope.
"""

from __future__ import annotations

import json

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms import wrap as wrap_mod


@pytest.fixture
def bank(tmp_path):
    db = str(tmp_path / "bank.db")
    cs = CardStore(db)
    cs.add_atom("memory:lesson-one", "a lesson body", scope="testscope")
    return db


def test_composed_spec_carries_the_identity_block(bank):
    res = wrap_mod.compose_auto_spec("testscope", since=1, db_path=bank,
                                     include_unearned=True)
    spec = res["spec"]
    assert len(spec["spec_id"]) == 12 and spec["spec_id"].strip()
    assert spec["scope"] == "testscope"
    assert isinstance(spec["composed_ts"], int) and spec["composed_ts"] > 0
    assert "composed_by" in spec  # the echelon session id, or "-" when absent


def test_two_compositions_never_share_a_spec_id(bank):
    a = wrap_mod.compose_auto_spec("testscope", since=1, db_path=bank,
                                   include_unearned=True)["spec"]
    b = wrap_mod.compose_auto_spec("testscope", since=1, db_path=bank,
                                   include_unearned=True)["spec"]
    assert a["spec_id"] != b["spec_id"]


def test_cli_default_out_name_embeds_time_scope_and_spec_id(bank, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wrap_mod, "compose_auto_spec",
                        lambda *a, **k: {"spec": {"spec_id": "abcdef012345",
                                                  "scope": "testscope"},
                                         "held_back": [], "proposed": [{"why": "t", "slug": "x", "use_count": 0}],
                                         "window": {"start": 1, "prev_card": "",
                                                    "hours": 0.1}})
    rc = wrap_mod._main(["--auto", "--scope", "testscope"])
    assert rc == 0
    names = [p.name for p in (tmp_path / "_wrap").glob("auto-spec-*.json")]
    assert len(names) == 1
    n = names[0]
    assert "testscope" in n and "abcdef" in n, (
        f"{n}: the default name must carry scope + spec_id — date alone is "
        "the collision the owner reported")


def test_run_refuses_a_spec_composed_for_another_scope(tmp_path, capsys):
    spec = {"spec_id": "abcdef012345", "scope": "estate-A",
            "session": "2026-08-28-x", "episodes": []}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    rc = wrap_mod._main([str(p), "--scope", "estate-B"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "estate-A" in err and "abcdef012345" in err


def test_legacy_spec_without_scope_still_runs(tmp_path, monkeypatch, capsys):
    # No scope field -> no refusal path; it must reach run_spec (stubbed).
    spec = {"session": "2026-08-28-x", "episodes": []}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")

    class _WS:
        def __init__(self, scope):
            pass

        def run_spec(self, s):
            return {"session": s["session"], "offer": []}

    monkeypatch.setattr(wrap_mod, "WrapSession", lambda scope: _WS(scope))
    monkeypatch.setattr(wrap_mod, "_render", lambda r: "ran " + r["session"])
    rc = wrap_mod._main([str(p), "--scope", "anything"])
    assert rc in (0, None)
    assert "ran 2026-08-28-x" in capsys.readouterr().out
