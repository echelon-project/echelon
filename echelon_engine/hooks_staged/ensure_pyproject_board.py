# -*- coding: utf-8 -*-
"""SessionStart hook: scaffold pyproject.toml in an ACTIVE project that lacks one.

Owner ruling 2026-08-19 (atom: suite-wall-time-parallelize-by-default):
- the banked parallel-test law ([tool.pytest.ini_options] addopts="-n auto")
- an ECHELON BOARD: the most-used engine doors + the traps that bite every
  session, as TOML comments — a per-repo cheat sheet the harness actually
  reads, since harnesses rarely do a full --help sweep of the verb surface.

Guards (all silent no-ops): pyproject.toml already exists; cwd is not a git
repo; repo inactive (last commit > 45 days; a repo with no commits yet counts
as active); cwd inside ~/.claude. xdist probe: if pytest-xdist is not
importable by the session's python, addopts ships commented-out so a scaffold
can never break pytest in that repo.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ACTIVE_WINDOW_DAYS = 45


def _cwd() -> Path:
    try:
        data = json.load(sys.stdin)
        if isinstance(data, dict) and data.get("cwd"):
            return Path(data["cwd"])
    except Exception:
        pass
    return Path(os.getcwd())


def _is_active_git_repo(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%ct"], cwd=root,
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False
    if out.returncode != 0 or not out.stdout.strip():
        return True  # git repo with no commits yet: a NEW project is active
    try:
        age_days = (time.time() - float(out.stdout.strip())) / 86400
    except ValueError:
        return True
    return age_days <= ACTIVE_WINDOW_DAYS


def _has_xdist() -> bool:
    try:
        import xdist  # noqa: F401
        return True
    except ImportError:
        return False


BOARD = """\
# ============================================================
# ECHELON BOARD - scaffolded by SessionStart hook (ensure_pyproject_board.py)
# Provenance atom: suite-wall-time-parallelize-by-default (scope echelon, 2026-08-19)
# This file doubles as the harness's per-repo cheat sheet: the pytest law
# below, the most-used ECHELON doors, and the traps that bite every session.
#
# EVERY-SESSION TRAPS (reflexes fire on these - save the round-trip):
#   * ALWAYS `python -X utf8 -m echelon_engine <verb>` - bare python mangles
#     UTF-8 output on Windows. The pip-installed `echelon` command is safe
#     (console.py wrappers force UTF-8; a .exe wrapper cannot pass -X utf8).
#   * rc read after a pipe is the PIPE's exit - never gate on `cmd | tail; $?`.
#   * `echelon ingest --root <dir> --scope <s>` takes FORWARD slashes.
#   * `echelon gate --write <file>` needs an EXPLICIT --scope.
#   * estate repos gitignore memory/ - commit atoms with `git add -f memory/<f>.md`.
#   * PowerShell Set-Content/Out-File re-encode non-ASCII - use the Edit tool
#     or `sed -i` for source files with unicode.
#
# MOST-USED DOORS (detail: `echelon <verb> --help`; engine repo D:\\WORK\\ECHELON-AGENT):
#   echelon recall --scope <s> --warm "<intent>"    foveated recall (free - do it BEFORE load-bearing work)
#   echelon remember <slug>                         full atom body (earns weight; never cat the .md)
#   echelon relive <arc-card-id>                    resume a banked day (resume menu at boot)
#   echelon status --scope <s>                      bank overview     |  echelon wrap = session close-out
#   echelon swarm --file <f>|--root <d> --type <t>  N equipped lenses over real files (ux/council/audit)
#   echelon brainstorm "<question>"                 multi-model judge council - never settle a fork alone
#   echelon dispatch / run --goal "<goal>"          ONE equipped partner acts in a sandbox folder
#   echelon summon [--once|--bus <ch>]              nerve-connected Claude peer (bus = gated multi-slice)
#   echelon cartridge list | equip <name> "<goal>"  pluggable earned capability (31 registered)
#   echelon pagemodel --os-dir <d> --out c.json     page -> GRAPH before reading a 100k-line page.js
#   claude-deep "<goal>"  /  claude-gem "<goal>"    cheap isolated worker  /  vision+UX worker
#
# PYTHON PATH: `echelon` + claude-* commands come from `pip install -e .` in
# D:\\WORK\\ECHELON-AGENT. Command missing? Fall back to
# `python -X utf8 -m echelon_engine <verb>` run from that repo.
# ============================================================
"""

PYTEST_LIVE = """\
[tool.pytest.ini_options]
# OWNER RULE 2026-08-19 (atom: suite-wall-time-parallelize-by-default):
# suite wall-time is the waste - every pytest run parallelizes across CPU
# workers by default. Measured 103s->23s / 201s->90s. Serial hatch: -p no:xdist.
addopts = "-n auto"
"""

PYTEST_DORMANT = """\
[tool.pytest.ini_options]
# OWNER RULE 2026-08-19 (atom: suite-wall-time-parallelize-by-default):
# parallelize every suite. pytest-xdist was NOT importable when this file was
# scaffolded, so the rule ships dormant - activate with:
#   pip install pytest-xdist    then uncomment:
# addopts = "-n auto"
"""


def main() -> int:
    root = _cwd()
    home_claude = Path.home() / ".claude"
    if (root / "pyproject.toml").exists():
        return 0
    if str(root).startswith(str(home_claude)):
        return 0
    if not _is_active_git_repo(root):
        return 0

    pytest_block = PYTEST_LIVE if _has_xdist() else PYTEST_DORMANT
    (root / "pyproject.toml").write_text(BOARD + "\n" + pytest_block,
                                         encoding="utf-8")
    live = "live" if pytest_block is PYTEST_LIVE else "dormant (pytest-xdist missing)"
    print(f"[echelon-board] pyproject.toml scaffolded in {root.name}: "
          f"parallel-test rule {live} + ECHELON door board "
          f"(atom: suite-wall-time-parallelize-by-default)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
