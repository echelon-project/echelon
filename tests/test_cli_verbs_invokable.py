"""OPEN-0033 — every advertised CLI verb is INVOKABLE: `<verb> --help` exits 0.

A verb the front page lists but that errors on `--help` is a broken door — the user
cannot even discover its flags. This test enumerates the ADVERTISED verb surface
(_HELP_GROUPS, the same list `echelon verbs` prints) and asserts each one's help is
reachable and exits cleanly.

It caught `workflow --help` exiting 2: main() translated the bare verb to the flag
`--workflow`, so `workflow --help` became `--workflow --help` and argparse consumed
--help as the missing FILE value ("expected one argument"). Fixed to route a bare
help request to the agent parser's own --help. This test locks that shut and guards
the whole surface against the same regression on any future verb.

Runs IN-PROCESS (no 80 subprocess spawns) and never touches the bank — `--help` is
pure argparse, it exits before any verb body runs.
"""
import contextlib
import io

import pytest

from echelon_engine.__main__ import _HELP_GROUPS, main


def _advertised_verbs():
    return [v for _section, verbs in _HELP_GROUPS for v, _gloss in verbs]


def _help_exit_code(verb):
    """Run `<verb> --help` in-process; return the process exit code (0 = clean)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = main([verb, "--help"])
        return 0 if rc is None else rc
    except SystemExit as e:  # argparse's --help path raises SystemExit(0)
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)


def test_the_advertised_surface_is_not_empty():
    """Guard against the enumeration silently going empty and vacuously passing."""
    assert len(_advertised_verbs()) >= 70


@pytest.mark.parametrize("verb", _advertised_verbs())
def test_verb_help_is_invokable(verb):
    code = _help_exit_code(verb)
    assert code == 0, f"`echelon {verb} --help` exited {code!r} — the verb is not invokable"


def test_workflow_help_specifically():
    """The exact defect this item found: workflow --help must reach help, not error on
    a missing --workflow FILE argument."""
    assert _help_exit_code("workflow") == 0
