"""Reflex compile contract — the flagged-atom -> static-ruleset half of the reflex arc.

The fire-side hook (~/.claude/hooks/echelon_reflex.py) lives outside the repo and was
verified live at birth (2026-07-08: the warn arc fired on the builder's own command;
the block arc denied a live tool call). What the repo can pin is the COMPILE contract:
flag parsing, trigger validation, guard extraction, per-scope replace, and the
backslash trap that bit during the build.
"""
from __future__ import annotations

import json

import pytest

from echelon_engine.atoms import reflex


def _atom(name: str, *, flag="true", event="PreToolUse", tool="Bash",
          match="python -m echelon_engine", action="warn",
          body="Guard text first paragraph.\n\nSecond paragraph never travels.") -> str:
    return (f"---\nname: {name}\ndescription: test reflex atom\nmetadata:\n"
            f"  type: feedback\n  reflex: {flag}\n  reflex-event: {event}\n"
            f"  reflex-tool: {tool}\n  reflex-match: {match}\n"
            f"  reflex-action: {action}\n---\n\n{body}\n")


def _write(root, name, text):
    p = root / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_compile_picks_only_flagged_atoms(tmp_path):
    _write(tmp_path, "guard", _atom("guard"))
    _write(tmp_path, "plain", "---\nname: plain\ndescription: d\nmetadata:\n  type: project\n---\n\nnot a reflex\n")
    rules, skipped = reflex.compile_reflexes(tmp_path, "t")
    assert [r["name"] for r in rules] == ["guard"]
    assert skipped == []


def test_rule_carries_the_full_trigger_spec_and_first_paragraph_guard(tmp_path):
    _write(tmp_path, "guard", _atom("guard", tool="Bash|PowerShell", action="block"))
    (rule,), _ = reflex.compile_reflexes(tmp_path, "sc")
    assert rule == {"name": "guard", "scope": "sc", "tier": "scope", "event": "PreToolUse",
                    "tool": "Bash|PowerShell", "match": "python -m echelon_engine",
                    "action": "block", "guard": "Guard text first paragraph."}


def test_scope_is_the_default_tier_global_is_an_assertion(tmp_path):
    """TIER DOCTRINE: estate-local by default. Born 2026-08-15 from an audit finding 140
    of 141 compiled rules fired in every estate because `scope` was a display label only."""
    _write(tmp_path, "guard", _atom("guard"))
    (rule,), _ = reflex.compile_reflexes(tmp_path, "t")
    assert rule["tier"] == "scope"


def test_declared_tier_is_carried_through(tmp_path):
    for tier in ("global", "portable", "scope"):
        root = tmp_path / tier
        root.mkdir()
        text = _atom("guard").replace("  reflex: true\n",
                                      f"  reflex: true\n  reflex-tier: {tier}\n")
        _write(root, "guard", text)
        (rule,), _ = reflex.compile_reflexes(root, "t")
        assert rule["tier"] == tier


def test_unknown_tier_is_skipped_not_silently_defaulted(tmp_path):
    """A typo'd tier must not quietly become estate-local (or, worse, machine-wide)."""
    text = _atom("guard").replace("  reflex: true\n", "  reflex: true\n  reflex-tier: globl\n")
    _write(tmp_path, "guard", text)
    rules, skipped = reflex.compile_reflexes(tmp_path, "t")
    assert rules == []
    assert any("unknown reflex-tier" in s for s in skipped)


def test_global_tier_naming_an_estate_specific_pattern_warns(tmp_path):
    """Declaring global ASSERTS estate-independence; a concrete host/path contradicts it."""
    text = (_atom("guard", match="docker cp .*srv-captain")
            .replace("  reflex: true\n", "  reflex: true\n  reflex-tier: global\n"))
    _write(tmp_path, "guard", text)
    rules, skipped = reflex.compile_reflexes(tmp_path, "t")
    assert rules and rules[0]["tier"] == "global"  # warn, never silently drop a guard
    assert any("estate-specific" in s for s in skipped)


def test_warn_is_the_default_action_teeth_are_declared(tmp_path):
    text = _atom("guard").replace("  reflex-action: warn\n", "")
    _write(tmp_path, "guard", text)
    (rule,), _ = reflex.compile_reflexes(tmp_path, "t")
    assert rule["action"] == "warn"


@pytest.mark.parametrize("mutation, reason_word", [
    ({"event": "PostToolUse"}, "reflex-event"),      # unknown event
    ({"action": "explode"}, "reflex-action"),        # unknown action
    ({"match": "(unclosed"}, "compile"),             # bad regex
])
def test_invalid_trigger_is_skipped_and_reported_never_silent(tmp_path, mutation, reason_word):
    _write(tmp_path, "bad", _atom("bad", **mutation))
    rules, skipped = reflex.compile_reflexes(tmp_path, "t")
    assert rules == []
    assert len(skipped) == 1 and reason_word in skipped[0]


def test_missing_match_cannot_fire_so_it_is_skipped(tmp_path):
    text = _atom("bad").replace("  reflex-match: python -m echelon_engine\n", "")
    _write(tmp_path, "bad", text)
    rules, skipped = reflex.compile_reflexes(tmp_path, "t")
    assert rules == [] and "reflex-match" in skipped[0]


def test_memory_index_is_never_scanned_as_an_atom(tmp_path):
    _write(tmp_path, "MEMORY", _atom("MEMORY"))  # MEMORY.md is the index, not an atom
    rules, _ = reflex.compile_reflexes(tmp_path, "t")
    assert rules == []


def test_compile_replaces_own_scope_and_keeps_others(tmp_path, monkeypatch):
    ruleset = tmp_path / "reflexes.json"
    monkeypatch.setattr(reflex, "RULESET", ruleset)
    other = {"name": "foreign", "scope": "other", "event": "PreToolUse",
             "tool": ".*", "match": "x", "action": "warn", "guard": "g"}
    ruleset.write_text(json.dumps({"version": 1, "rules": [other]}), encoding="utf-8")

    root = tmp_path / "mem"; root.mkdir()
    _write(root, "guard", _atom("guard"))
    assert reflex._main(["compile", "--root", str(root), "--scope", "mine"]) == 0
    rules = json.loads(ruleset.read_text(encoding="utf-8"))["rules"]
    assert {(r["scope"], r["name"]) for r in rules} == {("other", "foreign"), ("mine", "guard")}

    # recompile with the atom gone -> own scope drains, other scope survives.
    # The count-drop guard fires here (1→0 rules, <50%); --force overrides because
    # this is an intentional drain, not the two-roots trap.
    (root / "guard.md").unlink()
    assert reflex._main(["compile", "--root", str(root), "--scope", "mine", "--force"]) == 0
    rules = json.loads(ruleset.read_text(encoding="utf-8"))["rules"]
    assert [(r["scope"], r["name"]) for r in rules] == [("other", "foreign")]


def test_reflex_cwd_travels_into_the_rule_when_declared(tmp_path):
    """The narrowing lever born 2026-07-31: an estate-local trap (gamma-support flux-venv)
    fired in every OTHER estate's sessions. `reflex-cwd` scopes the arc to sessions
    whose cwd matches; absent -> key absent -> rule fires everywhere (old behavior)."""
    text = _atom("guard").replace("  reflex-action: warn\n",
                                  "  reflex-action: warn\n  reflex-cwd: gamma-support\n")
    _write(tmp_path, "guard", text)
    (rule,), skipped = reflex.compile_reflexes(tmp_path, "t")
    assert rule["cwd"] == "gamma-support" and skipped == []

    _write(tmp_path, "guard", _atom("guard"))  # no reflex-cwd
    (rule,), _ = reflex.compile_reflexes(tmp_path, "t")
    assert "cwd" not in rule


def test_bad_reflex_cwd_regex_is_skipped_and_reported(tmp_path):
    text = _atom("bad").replace("  reflex-action: warn\n",
                                "  reflex-action: warn\n  reflex-cwd: (unclosed\n")
    _write(tmp_path, "bad", text)
    rules, skipped = reflex.compile_reflexes(tmp_path, "t")
    assert rules == [] and "reflex-cwd" in skipped[0]


def test_the_backslash_trap_is_real_yaml_normalized_escapes_stay_doubled(tmp_path):
    """Pin the trap found at birth: a writer that YAML-normalizes `\\.` to `"\\\\."`
    produces a pattern that matches a literal backslash — parse_atom does NOT unescape.
    The doctrine is backslash-free patterns ([.] not \\.), so compile must keep the
    doubled form verbatim (no silent unescaping magic to mask the authoring bug)."""
    text = _atom("guard", match='"echelon\\\\.db"')
    _write(tmp_path, "guard", text)
    (rule,), _ = reflex.compile_reflexes(tmp_path, "t")
    assert rule["match"] == "echelon\\\\.db"   # kept verbatim (quotes stripped, escapes untouched)


def test_worktype_is_carried_and_absent_means_all(tmp_path):
    """charter Part 4: `worktype:` narrows a reflex to room types; untagged keeps firing."""
    text = _atom("guard").replace("  reflex: true\n", "  reflex: true\n  worktype: prod-service, console-app\n")
    _write(tmp_path / "a", "guard", text) if (tmp_path / "a").mkdir() is None else None
    (rule,), _ = reflex.compile_reflexes(tmp_path / "a", "t")
    assert rule["worktype"] == ["prod-service", "console-app"]
    (tmp_path / "b").mkdir()
    _write(tmp_path / "b", "guard", _atom("guard"))
    (rule,), _ = reflex.compile_reflexes(tmp_path / "b", "t")
    assert "worktype" not in rule
    (tmp_path / "c").mkdir()
    _write(tmp_path / "c", "guard", _atom("guard").replace("  reflex: true\n", "  reflex: true\n  worktype: all\n"))
    (rule,), _ = reflex.compile_reflexes(tmp_path / "c", "t")
    assert "worktype" not in rule
