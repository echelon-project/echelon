"""Spec S8 V5 — the wrap atom carries lessons only; STATE lines route to the room.
`echelon check` refuses a session-wrap-* atom with NEXT/OPEN lines; ingest only warns
(a wrap already planted must never be silently skipped on re-ingest)."""
from echelon_engine.atoms.ingest import check_folder, validate_atom, wrap_state_lines

_WRAP = """---
name: session-wrap-2026-08-29
description: resume menu
metadata:
  type: project
---
The arcs: S8a gated.
NEXT 1. S8b V5 wrap->room
## STILL OPEN
- OPEN-0006 estate order
"""

_CLEAN = _WRAP.replace("NEXT 1. S8b V5 wrap->room\n## STILL OPEN\n- OPEN-0006 estate order\n", "Lesson: gate the library door.\n")


def test_wrap_state_lines_finds_next_and_open():
    hits = wrap_state_lines(_WRAP.split("---", 2)[2])
    assert any(h.startswith("NEXT") for h in hits) and any("STILL OPEN" in h for h in hits)
    assert wrap_state_lines("Lesson: the OPEN verb is in the room.") == []  # mid-line word is not state


def test_check_refuses_wrap_atom_with_state_lines(tmp_path):
    (tmp_path / "session-wrap-2026-08-29.md").write_text(_WRAP, encoding="utf-8")
    (tmp_path / "session-wrap-clean.md").write_text(_CLEAN.replace("session-wrap-2026-08-29", "session-wrap-clean"), encoding="utf-8")
    ok, rejected = check_folder(tmp_path, verbose=False)
    assert ok == ["session-wrap-clean.md"]
    assert [r[0] for r in rejected] == ["session-wrap-2026-08-29.md"]
    assert "STATE lines" in rejected[0][1][0]


def test_ingest_only_warns_on_wrap_state_lines():
    errors, warnings = validate_atom(_WRAP, stem="session-wrap-2026-08-29")
    assert errors == [] and any("STATE lines" in w for w in warnings)
    errors, _ = validate_atom(_WRAP, stem="session-wrap-2026-08-29", wrap_lint=True)
    assert any("STATE lines" in e for e in errors)


def test_non_wrap_atom_may_say_next():
    atom = _WRAP.replace("session-wrap-2026-08-29", "some-lesson")
    assert validate_atom(atom, stem="some-lesson", wrap_lint=True) == ([], [])


def test_wrap_lint_has_a_birthday():
    """Wraps dated before 2026-08-29 are archives — never refused, never warned."""
    old = _WRAP.replace("session-wrap-2026-08-29", "session-wrap-2026-07-30")
    assert validate_atom(old, stem="session-wrap-2026-07-30", wrap_lint=True) == ([], [])
