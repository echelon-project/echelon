"""Tests for echelon_engine/worktype.py — the room TYPE anchor (charter Part 4).

tmp_path rooms only; the ruleset is monkeypatched so ~/.echelon is never read.
"""
import json

import pytest

from echelon_engine import workcycle as wc
from echelon_engine import worktype as wt
from echelon_engine.atoms import reflex


@pytest.fixture
def room(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return wc.init(root, estate="acme", type_="console-app", scope="acme")


def _rule(name, scope="acme", tier="scope", worktype=None):
    r = {"name": name, "scope": scope, "tier": tier, "event": "PreToolUse",
         "tool": ".*", "match": "x", "action": "warn", "guard": "g"}
    if worktype:
        r["worktype"] = worktype
    return r


# ── room_gates(): the ONE law (hook, `reflex test`, `type show`) ─────────────

def test_untagged_rule_is_armed_everywhere():
    assert reflex.room_gates(_rule("a"), "console-app") == (True, "")
    assert reflex.room_gates(_rule("a"), "") == (True, "")


def test_tagged_rule_hides_outside_its_types_and_fails_open_on_unknown_type():
    r = _rule("deploy-guard", worktype=["prod-service"])
    ok, why = reflex.room_gates(r, "console-app")
    assert not ok and "not armed for room type 'console-app'" in why
    assert reflex.room_gates(r, "prod-service")[0]
    assert reflex.room_gates(r, "")[0]  # unknown room type never hides a guard


def test_snooze_matches_bare_name_or_scope_qualified():
    r = _rule("noisy")
    assert reflex.room_gates(r, "engine", ("noisy",))[1].startswith("snoozed")
    assert reflex.room_gates(r, "engine", ("acme:noisy",))[1].startswith("snoozed")
    assert reflex.room_gates(r, "engine", ("other:noisy",))[0]


# ── session_gate(): the hook's one read ─────────────────────────────────────

def test_session_gate_reads_type_and_live_snoozes(room):
    wc.snooze(room, "noisy", "2999-01-01T00:00:00+00:00")
    wc.snooze(room, "gone", "2000-01-01T00:00:00+00:00")  # expired
    assert wt.session_gate(str(room.parent)) == ("console-app", ("noisy",))


def test_session_gate_without_a_room_fails_open(tmp_path):
    assert wt.session_gate(str(tmp_path)) == ("", ())


def test_session_gate_never_prints_to_stdout(room, capsys):
    """gate M-1: the hook's stdout is the harness protocol channel; a v>1 room file
    must not put a line on it (and must still fail open)."""
    rj = json.loads((room / "room.json").read_text(encoding="utf-8"))
    rj["v"] = 9
    (room / "room.json").write_text(json.dumps(rj), encoding="utf-8")
    assert wt.session_gate(str(room.parent)) == ("", ())
    assert capsys.readouterr().out == ""


# ── survey(): what `type show` prints ────────────────────────────────────────

def test_survey_splits_rules_reaching_this_room(room, monkeypatch):
    rules = [_rule("plain"), _rule("svc-only", worktype=["prod-service"]),
             _rule("wide", scope="echelon", tier="global"),
             _rule("elsewhere", scope="other"), _rule("muted")]
    monkeypatch.setattr(reflex, "_load_ruleset", lambda: {"rules": rules})
    wc.snooze(room, "muted", "2999-01-01T00:00:00+00:00")
    s = wt.survey(room)
    assert s["type"] == "console-app" and s["rules_here"] == 4
    assert s["armed"] == ["acme:plain", "echelon:wide"]
    assert s["hidden"] == ["acme:svc-only"]
    assert s["snoozed"] == ["acme:muted"]


# ── the shared matcher carries the same law ──────────────────────────────────

def test_rule_fires_applies_type_and_snooze_gates():
    r = _rule("svc-only", worktype=["prod-service"])
    payload = json.dumps({"command": "x"})
    assert reflex.rule_fires(r, "Bash", payload, "acme", "", None, "prod-service")[0]
    fired, why = reflex.rule_fires(r, "Bash", payload, "acme", "", None, "console-app")
    assert not fired and "worktype" in why
    assert reflex.rule_fires(r, "Bash", payload, "acme", "", None, "")[0]
    fired, why = reflex.rule_fires(_rule("m"), "Bash", payload, "acme", "", None, "engine", ("m",))
    assert not fired and "snoozed" in why


def test_reflex_test_names_a_rule_hidden_where_it_runs(room, monkeypatch):
    """gate S-1: the teeth-test stays open (asserts the trigger) but must SAY when the
    rule would be hidden in the room the test runs from."""
    monkeypatch.setattr(wc, "room_path", lambda start=None: room)
    rules = [_rule("svc-only", worktype=["prod-service"])]
    res = reflex.test_reflex(rules, "svc-only", fire=["x"], quiet=[], cwd=str(room.parent))
    assert res["ok"] and res["checks"][0]["fired"]
    assert len(res["notes"]) == 1 and "HIDDEN in this room (type 'console-app')" in res["notes"][0]
    res = reflex.test_reflex([_rule("plain")], "plain", fire=["x"], quiet=[])
    assert res["notes"] == []


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_show_set_snooze_unsnooze(room, monkeypatch, capsys):
    monkeypatch.setattr(wc, "room_path", lambda start=None: room)
    monkeypatch.setattr(reflex, "_load_ruleset",
                        lambda: {"rules": [_rule("svc-only", worktype=["prod-service"])]})
    assert wt.main(["show"]) == 0
    out = capsys.readouterr().out
    assert "type console-app" in out and "hidden  acme:svc-only" in out

    assert wt.main(["set", "prod-service"]) == 0
    assert wc.room_type(room) == "prod-service"
    assert wt.main(["show"]) == 0
    assert "hidden-by-type 0" in capsys.readouterr().out

    assert wt.main(["snooze", "svc-only", "--hours", "1", "--why", "gating a worker"]) == 0
    assert [s["name"] for s in wc.snoozes(room)] == ["svc-only"]
    assert wt.main(["unsnooze", "--all"]) == 0
    assert wc.snoozes(room) == []
    kinds = [json.loads(l)["kind"] for p in (room / "journal").glob("*.jsonl")
             for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert kinds.count("type") == 1 and kinds.count("snooze") == 2


def test_cli_snooze_bounds(room, monkeypatch, capsys):
    """gate M-2 / S-5: an unparseable --until is refused (never a permanent mute);
    --hours must be positive; past 72h it warns."""
    monkeypatch.setattr(wc, "room_path", lambda start=None: room)
    assert wt.main(["snooze", "x", "--until", "zzz-never"]) == 1
    assert "ISO-8601" in capsys.readouterr().out
    assert wc.snoozes(room) == []
    assert wt.main(["snooze", "x", "--hours", "-5"]) == 1
    assert "positive" in capsys.readouterr().out
    assert wt.main(["snooze", "x", "--hours", "100"]) == 0
    assert "longer than 72h" in capsys.readouterr().out
    assert [s["name"] for s in wc.snoozes(room)] == ["x"]


def test_cli_set_off_taxonomy_warns_but_sets(room, monkeypatch, capsys):
    monkeypatch.setattr(wc, "room_path", lambda start=None: room)
    assert wt.main(["set", "weird"]) == 0
    assert "not in the charter taxonomy" in capsys.readouterr().out
    assert wc.room_type(room) == "weird"
