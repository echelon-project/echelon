"""doctor — the environment-truth verb (owner, 2026-08-19).

THE PAIN THIS CLOSES: harness sessions fight the WRONG QUESTIONS for whole
turns — which python am I on? which install of the engine does `echelon`
resolve to? is the bank reachable? are the reflexes compiled? Every one of
those is answerable in milliseconds from inside the process; the chore was
that nothing answered them in one place. `echelon doctor` does, and prints
the FIX next to every finding — never a bare complaint.

$0, read-only, no network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from .. import estate as _estate


def _ok(label: str, detail: str) -> None:
    print(f"  [ok]   {label:<14} {detail}")


def _warn(label: str, detail: str, fix: str = "") -> None:
    print(f"  [WARN] {label:<14} {detail}")
    if fix:
        print(f"         fix -> {fix}")


def main(argv=None) -> int:
    print("echelon doctor — environment truth ($0, read-only)\n")
    warns = 0

    # ── 1. the interpreter actually running this ──────────────────────────
    _ok("python", f"{sys.version.split()[0]}  {sys.executable}")
    if "\\.venv\\" in sys.executable or "/.venv/" in sys.executable:
        _ok("venv", f"running inside a venv: {Path(sys.executable).parents[1]}")
    else:
        _ok("venv", "not a project venv (system/base interpreter)")

    # ── 2. which engine INSTALL this process imports ──────────────────────
    import echelon_engine
    mod_path = Path(echelon_engine.__file__).resolve().parent
    try:
        from importlib.metadata import version
        ver = version("echelon")
    except Exception:
        ver = "unpackaged"
    _ok("engine", f"v{ver}  {mod_path}")
    repo = mod_path.parent
    if (repo / ".git").exists():
        try:
            head = subprocess.run(["git", "log", "-1", "--format=%h %s"],
                                  cwd=repo, capture_output=True, text=True,
                                  timeout=10).stdout.strip()
            _ok("repo", f"{repo.name} @ {head[:70]}")
        except Exception:
            pass
    else:
        _warn("repo", "engine imports from an INSTALLED copy, not a repo checkout",
              "a stale wheel ships old code — reinstall with `pip install -e "
              f"{_estate.optional('engine_root', 'the engine checkout')}` (reflex-wheel-shipped-must-be-built-from-head)")
        warns += 1

    # ── 3. the `echelon` command vs THIS interpreter ──────────────────────
    exe = shutil.which("echelon")
    if exe is None:
        _warn("command", "`echelon` is not on PATH from here",
              "use `python -X utf8 -m echelon_engine <verb>`, or "
              "`pip install -e .` in the engine repo to mint the command")
        warns += 1
    else:
        exe_home = str(Path(exe).resolve().parent.parent).lower()
        my_home = str(Path(sys.executable).resolve().parent.parent).lower()
        if exe_home == my_home:
            _ok("command", f"`echelon` -> {exe} (same environment as this python)")
        else:
            _warn("command",
                  f"`echelon` -> {exe} — a DIFFERENT environment than this "
                  f"python ({sys.executable})",
                  "the exe runs its own baked interpreter; when behavior "
                  "differs, trust `python -X utf8 -m echelon_engine <verb>` "
                  "run from the interpreter you mean")
            warns += 1

    # ── 4. the bank ───────────────────────────────────────────────────────
    bank = Path.home() / ".echelon" / "echelon.db"
    if bank.exists():
        detail = f"{bank}  {bank.stat().st_size / 1e6:.1f}MB"
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{bank}?mode=ro", uri=True, timeout=3)
            n = con.execute("SELECT count(*) FROM atoms").fetchone()[0]
            con.close()
            detail += f"  {n} atoms"
        except Exception as e:
            detail += f"  (count unavailable: {type(e).__name__})"
        _ok("bank", detail)
    else:
        _warn("bank", f"no bank at {bank}",
              "`echelon init` bootstraps one; a missing bank means every "
              "recall is COLD, not broken")
        warns += 1

    # ── 5. reflexes ───────────────────────────────────────────────────────
    rj = Path.home() / ".echelon" / "reflexes.json"
    if rj.exists():
        try:
            import json
            rules = json.loads(rj.read_text(encoding="utf-8"))
            count = len(rules.get("rules", rules) if isinstance(rules, dict) else rules)
            _ok("reflexes", f"{rj}  {count} rule(s) compiled")
        except Exception:
            _warn("reflexes", f"{rj} exists but does not parse",
                  "recompile: `echelon reflex compile --root <memory dir> --scope <scope>`")
            warns += 1
    else:
        _warn("reflexes", "no compiled ruleset",
              "`echelon reflex compile --root <memory dir> --scope <scope>`")
        warns += 1

    # ── 6. encoding + cwd scope ───────────────────────────────────────────
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" in enc:
        _ok("encoding", f"stdout={enc}")
    else:
        _warn("encoding", f"stdout={enc} — non-UTF consoles mangle the glyphs",
              "invoke as `python -X utf8 -m echelon_engine ...` (the `echelon` "
              "command self-configures)")
        warns += 1
    # NOTE: this used to import a name `resolve` that the module has never exported, so the
    # check silently fell into the except on EVERY run and reported "resolver unavailable"
    # regardless of the truth. Fixed while making resolve_scope fail closed (OPEN-0036).
    try:
        from .resolve_scope import resolve_scope, UnknownScopeError
        try:
            sc = resolve_scope(os.getcwd())
            _ok("scope", f"cwd resolves to scope {sc!r}")
        except UnknownScopeError as e:
            # fail-closed is the DESIGNED answer here, not a fault — report it as the
            # diagnosis it is, and name the explicit door.
            _warn("scope", f"cwd is owned by no live bank scope (would mint {e.candidate!r})",
                  "run from a banked repo, or mint deliberately: "
                  "`echelon resolve-scope --allow-new`")
            warns += 1
    except Exception:
        print(f"  [ok]   scope          (resolver unavailable — `echelon "
              f"resolve-scope` answers this)")

    print(f"\n  providers: not probed here (network) — `echelon providers --test` "
          f"for live key checks")
    print(f"\ndoctor: {warns} warning(s)" if warns else "\ndoctor: clean bill")
    return 0
