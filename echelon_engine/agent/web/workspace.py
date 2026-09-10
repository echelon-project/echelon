"""workspace.py — per-session workspace ISOLATION for the command center.

The foot-gun this closes (critic + owner, 2026-06-17): the runner spawned an agent with
`--root <root>` defaulting to "." against cwd=<engine repo>, so a new session pointed a
write+bash-enabled agent straight at the live ECHELON-AGENT engine repo. A session must
NEVER dirty the engine repo (or any repo) unasked.

Policy (owner's call): "both — worktree for repos, scratch else."
  - chosen workspace IS a git repo  -> `git worktree add ~/.echelon/work/<bridge>` off HEAD.
    The agent edits an ISOLATED checkout; you diff/merge deliberately; untouched -> removed.
  - chosen workspace is NOT git (or none given) -> a fresh per-bridge SCRATCH dir.
  - the ENGINE repo itself as root -> REFUSED unless force=True (an explicit operator override).

Every session thus gets its own sandbox under ~/.echelon/work/<bridge>/ and the engine repo
is protected by default.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from echelon_sdk.paths import WORK, ensure

# the engine repo = three levels up from this file (echelon_agent/web/workspace.py -> repo)
ENGINE_REPO = Path(__file__).resolve().parent.parent.parent


@dataclass
class Workspace:
    bridge: str
    root: str                 # the dir the agent is sandboxed to (--root)
    kind: str                 # "worktree" | "scratch" | "explicit"
    source: str | None = None # the repo/dir it was derived from
    cleanup: bool = True       # remove on session end if untouched


def _is_git_repo(p: Path) -> Path | None:
    """Return the git top-level for p, or None if p isn't inside a git work tree."""
    try:
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except Exception:
        pass
    return None


def _under_engine_repo(p: Path) -> bool:
    try:
        p.resolve().relative_to(ENGINE_REPO)
        return True
    except ValueError:
        return False


def prepare(bridge: str, requested_root: str | None, *, force: bool = False) -> Workspace:
    """Resolve a SAFE, ISOLATED workspace for a session. Raises ValueError on a refused root."""
    ensure()
    dest = WORK / bridge

    # 1. No workspace chosen -> fresh scratch dir. Never the cwd/engine repo.
    if not requested_root or not str(requested_root).strip():
        dest.mkdir(parents=True, exist_ok=True)
        return Workspace(bridge=bridge, root=str(dest), kind="scratch", source=None)

    req = Path(requested_root).expanduser().resolve()
    if not req.is_dir():
        raise ValueError(f"workspace not found: {req}")

    # 2. The ENGINE repo (or anything inside it) is refused unless explicitly forced.
    if _under_engine_repo(req) and not force:
        raise ValueError(
            f"workspace '{req}' is inside the ECHELON engine repo — refused so a session "
            f"can't dirty the engine. Pick a scratch dir, another repo, or pass force=true.")

    # 3. A git repo -> worktree-isolate it (the agent edits a COPY, not your working tree).
    top = _is_git_repo(req)
    if top is not None:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        try:
            r = subprocess.run(
                ["git", "-C", str(top), "worktree", "add", "--detach", str(dest), "HEAD"],
                capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return Workspace(bridge=bridge, root=str(dest), kind="worktree", source=str(top))
            # worktree failed (dirty index, detached weirdness) -> fall through to explicit, but
            # do NOT silently send the agent at the live tree; surface the reason.
            raise ValueError(f"git worktree add failed for {top}: {r.stderr.strip()[:200]}")
        except FileNotFoundError:
            raise ValueError("git not found — cannot worktree-isolate; pick a non-repo dir")

    # 4. A non-git dir the operator explicitly chose -> use it as-is (their call, not a repo).
    return Workspace(bridge=bridge, root=str(req), kind="explicit", source=str(req), cleanup=False)


def teardown(ws: Workspace) -> dict:
    """On session end: remove a worktree (if untouched) / scratch dir. Explicit dirs are left alone.
    Returns a small report. Best-effort; never raises."""
    if not ws.cleanup:
        return {"removed": False, "reason": "explicit workspace left in place", "root": ws.root}
    p = Path(ws.root)
    try:
        if ws.kind == "worktree" and ws.source:
            # only auto-remove if the worktree has NO changes (untouched) — else keep for review
            dirty = subprocess.run(["git", "-C", ws.root, "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=15)
            if dirty.returncode == 0 and dirty.stdout.strip():
                return {"removed": False, "reason": "worktree has changes — kept for review", "root": ws.root}
            subprocess.run(["git", "-C", ws.source, "worktree", "remove", "--force", ws.root],
                          capture_output=True, text=True, timeout=30)
            return {"removed": True, "kind": "worktree", "root": ws.root}
        if ws.kind == "scratch" and p.exists():
            # keep a scratch dir that has artifacts; remove an empty one
            if any(p.iterdir()):
                return {"removed": False, "reason": "scratch has outputs — kept", "root": ws.root}
            shutil.rmtree(p, ignore_errors=True)
            return {"removed": True, "kind": "scratch", "root": ws.root}
    except Exception as e:  # noqa: BLE001
        return {"removed": False, "reason": f"teardown error: {e}", "root": ws.root}
    return {"removed": False, "root": ws.root}
