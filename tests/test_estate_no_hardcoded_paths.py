"""CONTRACT TEST (R-0171 unit A, OPEN-0121): no machine path in engine code.

The engine must run where the estate config says, so a `<estate-root>` literal in code
that actually executes is a defect: it re-couples the process to one machine.
This greps the load-bearing trees and fails on any hit not allowlisted WITH A
REASON.

Two deliberate narrowings, both justified by what a literal can actually do:

1. CODE ONLY. A literal inside a module docstring or a `#` comment is prose and
   cannot route a process anywhere. Classifying with `tokenize` instead of a
   line grep keeps the test honest about what it really guards.

2. ALLOWLIST WITH REASONS, each naming its kind:
     - FIXTURE: a test asserting path HANDLING; the string names no real dir.
     - FROZEN: a dead record whose rewriting would falsify it.
     - PROSE-IN-CODE: a hint/help string quoting a path for a human reader.

Mutation proof: add a `D:/repos/estate` literal to any non-allowlisted engine
module and this test goes red. Proven in the R-0171 ledger, not merely asserted.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

GUARDED_DIRS = ("echelon_engine", "echelon_sdk", "scripts")

# Any absolute, machine-specific root: a Windows drive path or a real user home.
# The rule is not "not MY estate" - it is "no checkout-specific absolute path
# baked into code", so the substrate runs from wherever it is cloned.
PATTERN = re.compile(r"[A-Za-z]:[/\\\\]{1,2}(?:WORK|repos)\\b|[A-Za-z]:[/\\\\]{1,2}Users[/\\\\]{1,2}[a-z]|/home/[a-z]+/")

#: This file names the forbidden pattern in its own docstring; a guard that
#: policed its own source could never be written.
SELF = Path(__file__).resolve()

#: path (posix, relative to repo root) -> why this file may keep its literals.
ALLOWLIST = {
    # FIXTURE — a synthetic path that exists on no real machine, used to prove
    # path handling. `C:\Users\test` is not anyone's home directory.
    "tests/test_shell_select.py":
        "FIXTURE: a synthetic LOCALAPPDATA proves shell discovery on Windows",
}


def _code_hits(path: Path):
    """Lines matching PATTERN that are NOT docstring or comment."""
    src = path.read_text(encoding="utf-8", errors="replace")
    if not PATTERN.search(src):
        return []
    prose_lines = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        tokens = []
    previous = None
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            prose_lines.update(range(tok.start[0], tok.end[0] + 1))
        elif tok.type == tokenize.STRING and previous in (
                None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL):
            prose_lines.update(range(tok.start[0], tok.end[0] + 1))
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            previous = tok.type
    return [(i, line.strip())
            for i, line in enumerate(src.splitlines(), 1)
            if PATTERN.search(line) and i not in prose_lines]


def _guarded_files():
    seen = set()
    for name in (*GUARDED_DIRS, "tests"):
        base = ROOT / name
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            resolved = path.resolve()
            if resolved == SELF or resolved in seen:
                continue
            # build/ is a copied artifact of the source tree, not source.
            if "build" in path.relative_to(ROOT).parts[:1]:
                continue
            seen.add(resolved)
            yield path


def test_no_hardcoded_estate_paths_in_code():
    offenders = {}
    for path in _guarded_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        hits = _code_hits(path)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "hardcoded machine paths in load-bearing code — resolve them from the "
        "estate config (`from .. import estate`), or allowlist the file WITH A "
        "REASON in this test:\n" + "\n".join(
            "  %s\n%s" % (f, "\n".join("      %5d: %s" % h for h in hits))
            for f, hits in sorted(offenders.items())))


def test_allowlist_entries_are_live_and_reasoned():
    """An allowlist rots: it must name real files that still have real hits."""
    for rel, reason in sorted(ALLOWLIST.items()):
        path = ROOT / rel
        assert path.exists(), "allowlisted file no longer exists: %s" % rel
        assert reason.split(":")[0] in ("FIXTURE", "FROZEN", "PROSE-IN-CODE"), (
            "allowlist reason for %s must start with FIXTURE/FROZEN/"
            "PROSE-IN-CODE, got %r" % (rel, reason))
        assert _code_hits(path), (
            "allowlisted file %s no longer has a hardcoded path — drop the "
            "allowlist entry so it cannot mask a future one." % rel)


def test_missing_key_fails_loud(tmp_path):
    """Absence raises naming the key AND the file, never a silent fallback."""
    from echelon_engine import estate
    config = tmp_path / "estate.json"
    config.write_text('{"estate_root": "/somewhere"}', encoding="utf-8")
    with pytest.raises(estate.EstateKeyError) as excinfo:
        estate.get("engine_root", path=config)
    message = str(excinfo.value)
    assert "engine_root" in message and str(config) in message


def test_missing_config_names_the_file_and_the_fix(tmp_path):
    from echelon_engine import estate
    missing = tmp_path / "nope" / "estate.json"
    with pytest.raises(estate.EstateConfigMissing) as excinfo:
        estate.get("engine_root", path=missing)
    message = str(excinfo.value)
    assert str(missing) in message and "estate init" in message


@pytest.mark.parametrize("key", ["estate_root", "engine_root", "command_root",
                                 "python", "bank", "rooms_registry"])
def test_estate_config_declares_required_key(key):
    """The live config carries every key the code resolves from."""
    from echelon_engine import estate
    try:
        data = estate.load()
    except estate.EstateConfigMissing:
        pytest.skip("no estate config on this machine (run `echelon estate init`)")
    assert key in data, "estate config %s must declare %r" % (estate.config_path(), key)
