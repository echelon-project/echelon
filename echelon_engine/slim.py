"""echelon slim — the anti-bloat lint (ESTATE-CRAFT-CHARTER Part 3, step 5).

`slim lint` measures a diff (files / source-only LOC / new deps) against the
DECLARED budget and refuses the overshoot: exceeding budget mid-build = STOP
and surface, exactly like a fence violation. The ledger line (budget vs
actual) is journaled to the room so repeat offenders are measurable.

Budget resolution: `--budget files=,loc=,deps=` > the active CHG-*'s budget
(stamped by `propose check` at open) > the room type's default (propose.TYPE_BUDGETS).

`slim hook` installs the pre-commit hook that runs `slim lint --staged`.
Exit: 0 within budget · 1 over budget · 3 refused (no git / bad budget).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from echelon_engine import propose, workcycle

DEP_FILES = ("pyproject.toml", "requirements.txt", "package.json", "setup.py", "Pipfile")
_DEP_LINE = re.compile(r'^\+\s*(?:"([A-Za-z0-9_.@/-]+)"\s*:|([A-Za-z0-9_.-]+)\s*(?:[=<>~!]=|>=|<=|$))')
HOOK = "#!/bin/sh\n# echelon slim — budget lint on commit (charter Part 3)\npython -X utf8 -m echelon_engine slim lint --staged --ledger || exit 1\n"


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise SystemExit(f"REFUSED: git {' '.join(args[:2])} failed: {p.stderr.strip()[:200]}")
    return p.stdout


def diff_text(repo: Path, *, base: str | None, staged: bool) -> str:
    """The index (--staged), or base..working-tree PLUS untracked files as synthetic
    all-added diffs — `git diff` alone cannot see a brand-new file, which is exactly
    the file a bloating build adds."""
    if staged:
        return _git(repo, "diff", "--cached")
    parts = [_git(repo, "diff", base or "HEAD")]
    for rel in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        f = repo / rel.strip()
        if not rel.strip() or not f.is_file():
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"diff --git a/{rel} b/{rel}\n--- /dev/null\n+++ b/{rel}\n"
                     + "".join("+" + l + "\n" for l in body.splitlines()))
    return "".join(parts)


def measure(diff: str) -> dict:
    """files / source-only LOC (propose._loc — the scanner's stripper) / new deps."""
    files, deps, cur = [], [], None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[4:].strip()
            cur = cur[2:] if cur.startswith("b/") else cur
            if cur != "/dev/null":
                files.append(cur)
        elif cur and Path(cur).name in DEP_FILES and line.startswith("+") and not line.startswith("+++"):
            m = _DEP_LINE.match(line)
            if m and (m.group(1) or m.group(2)) not in ("dependencies", "devDependencies", "name", "version"):
                deps.append(m.group(1) or m.group(2))
    return {"files": len(files), "loc": propose._loc(diff), "deps": len(deps),
            "file_list": files, "dep_list": deps}


def parse_budget(s: str) -> dict:
    out = {}
    for part in s.split(","):
        k, _, v = part.strip().partition("=")
        if k not in ("files", "loc", "deps") or not v.isdigit():
            raise SystemExit(f"REFUSED: bad --budget '{s}' (want files=N,loc=N,deps=N)")
        out[k] = int(v)
    return out


def resolve_budget(room: Path | None, explicit: str | None) -> tuple[dict, str]:
    if explicit:
        return parse_budget(explicit), "explicit"
    if room is not None:
        for p in sorted((room / "changes" / "active").glob("CHG-*.json")):
            d = workcycle._read_json(p, {})
            b = d.get("budget") if isinstance(d, dict) else None
            if isinstance(b, dict) and any(k in b for k in ("files", "loc", "deps")):
                return {k: int(b.get(k) or 0) for k in ("files", "loc", "deps")}, d.get("id", p.stem)
    t = workcycle.room_type(room) if room is not None else ""
    return dict(propose.TYPE_BUDGETS.get(t or propose._DEFAULT_TYPE, propose.TYPE_BUDGETS[propose._DEFAULT_TYPE])), \
        f"type:{t or propose._DEFAULT_TYPE}"


def verdict(actual: dict, budget: dict) -> list[str]:
    return [f"{k} {actual[k]} > {budget[k]}" for k in ("files", "loc", "deps") if actual[k] > budget.get(k, 0)]


def lint(repo: Path, room: Path | None, *, base: str | None = None, staged: bool = False,
         budget: str | None = None, ledger: bool = False) -> int:
    actual = measure(diff_text(repo, base=base, staged=staged))
    b, src = resolve_budget(room, budget)
    over = verdict(actual, b)
    line = (f"SLIM {'OVER' if over else 'ok'} budget[{src}] files {actual['files']}/{b['files']} "
            f"loc {actual['loc']}/{b['loc']} deps {actual['deps']}/{b['deps']}")
    print(line)
    for o in over:
        print(f"  OVER: {o}")
    if actual["dep_list"]:
        print(f"  deps: {', '.join(actual['dep_list'])}")
    if ledger and room is not None:
        workcycle.journal(room, "ledger", {"budget": b, "source": src,
                                           "actual": {k: actual[k] for k in ("files", "loc", "deps")},
                                           "over": over, "files": actual["file_list"][:40]})
    return 1 if over else 0


def install_hook(repo: Path) -> Path:
    hooks = Path(_git(repo, "rev-parse", "--git-path", "hooks").strip())
    hooks = hooks if hooks.is_absolute() else repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    p = hooks / "pre-commit"
    if p.exists() and "echelon slim" not in p.read_text(encoding="utf-8", errors="replace"):
        raise SystemExit(f"REFUSED: {p} exists and is not ours — chain it by hand")
    p.write_text(HOOK, encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="echelon slim", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("lint", help="files/LOC/deps of a diff vs the declared budget (exit 1 when over)")
    p.add_argument("--base", default=None, help="diff base (default HEAD = working tree)")
    p.add_argument("--staged", action="store_true", help="lint the index (the pre-commit shape)")
    p.add_argument("--budget", default=None, help="files=N,loc=N,deps=N (else active CHG, else type default)")
    p.add_argument("--ledger", action="store_true", help="journal the budget-vs-actual line to the room")
    p.add_argument("--repo", default=".")
    sub.add_parser("hook", help="install the pre-commit hook (slim lint --staged --ledger)")
    a = ap.parse_args(argv)
    repo = Path(getattr(a, "repo", ".")).resolve()
    if a.action == "hook":
        print(f"installed {install_hook(repo)}"); return 0
    try:
        return lint(repo, workcycle.room_path(repo), base=a.base, staged=a.staged,
                    budget=a.budget, ledger=a.ledger)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code); return 3
        raise


if __name__ == "__main__":
    sys.exit(main())
