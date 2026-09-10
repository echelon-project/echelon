"""Git discipline for the society's INSIDER roles (dev/tester/integrator) — real code changes, safe.

Owner's rules, 2026-06-06:
  - dev/tester/integrator get REAL codebase access with git discipline: branch -> PR -> merge.
  - BRANCH ONLY, NEVER master: merges land on an integration branch; a human merges to master.
  - "run echelon-agent from master, dont want them to kill themself": the live society process runs
    from the MAIN checkout (master's code). Insiders must NOT edit the files the live process is
    executing. So they work in an ISOLATED git worktree — same repo/history, a SEPARATE checkout on
    disk. Editing the branch there never swaps code out from under the running loop.

THE WORKTREE: `git worktree add <sibling> -b society/<run> master` makes a second working copy at a
sibling path, checked out to a fresh branch off master. Insiders cd there. The main checkout (the
running society) is untouchable. On teardown, the worktree is removed (the branch + commits persist
in the repo for review — never auto-merged to master).

MASTER PROTECTION is structural, not trusted to the agent: every git op here refuses any target of
master/main. Merge goes ONLY to `society/integration`. A guard, not a request.
"""
from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path

PROTECTED = {"master", "main"}
INTEGRATION = "society/integration"


def _git(repo: str, *args: str, timeout: int = 60) -> tuple[int, str]:
    """Run a git command in `repo`, return (returncode, combined output). Stdlib subprocess."""
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


class SocietyWorktree:
    """An isolated worktree off the repo for the insiders to work in. The live process stays on the
    main checkout (master's code); this is a SEPARATE checkout on a fresh branch. Never touches master."""

    def __init__(self, repo_root: str, run_id: str):
        self.repo = str(Path(repo_root).resolve())
        self.run_id = run_id
        self.branch = f"society/{run_id}"
        self.path = str(Path(self.repo).parent / f"echelon-society-wt-{run_id}")
        self.created = False
        self.prs: list[dict] = []   # recorded PRs (branch -> integration), reviewed by a human

    def setup(self) -> dict:
        """Create the worktree on a fresh branch off master. The live checkout is untouched.
        RESUME-SAFE: if this run's branch already has a registered worktree (a resumed lifecycle reuses
        the prior run's run_id), ATTACH to that existing worktree instead of failing — the insiders pick
        up their own prior branch state. A branch can only be checked out in one worktree, so re-adding
        would fail; reuse is the correct continue-the-lifecycle behavior."""
        existing = self._existing_worktree_for_branch()
        if existing:
            self.path = existing      # continue in the worktree this branch already lives in
            self.created = False      # we did NOT create it — don't tear down on close (resume owns it)
            return {"ok": True, "path": self.path, "branch": self.branch, "resumed": True}
        # base the work on master (the stable, running substrate), not the current branch.
        rc, out = _git(self.repo, "worktree", "add", "-b", self.branch, self.path, "master")
        if rc != 0:
            # branch may exist from a prior run; try attaching without -b
            rc2, out2 = _git(self.repo, "worktree", "add", self.path, self.branch)
            if rc2 != 0:
                return {"ok": False, "error": f"worktree add failed: {out} | {out2}"}
        self.created = True
        return {"ok": True, "path": self.path, "branch": self.branch}

    def _existing_worktree_for_branch(self) -> str | None:
        """Return the path of an already-registered worktree checked out to self.branch, else None."""
        rc, out = _git(self.repo, "worktree", "list", "--porcelain")
        if rc != 0:
            return None
        path = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("branch ") and line.strip().endswith("/" + self.branch):
                return path
        return None

    def teardown(self) -> None:
        """Remove the worktree (commits + branch persist in the repo for human review)."""
        if self.created:
            _git(self.repo, "worktree", "remove", "--force", self.path)

    # --- the insider git hands (all master-protected) ---
    def commit_all(self, message: str) -> dict:
        """Stage + commit everything in the WORKTREE (not master, not the live tree)."""
        _git(self.path, "add", "-A")
        rc, out = _git(self.path, "commit", "-m", message)
        return {"ok": rc == 0, "out": out[:400]}

    def status(self) -> dict:
        rc, out = _git(self.path, "status", "--short")
        return {"ok": rc == 0, "changes": out[:600]}

    def diff(self, max_chars: int = 1500) -> str:
        _, out = _git(self.path, "diff", "HEAD~1", "HEAD") if self._has_commit() else (0, "")
        if not out:
            _, out = _git(self.path, "diff")
        return out[:max_chars]

    def _has_commit(self) -> bool:
        rc, _ = _git(self.path, "rev-parse", "HEAD~1")
        return rc == 0

    def open_pr(self, title: str, body: str) -> dict:
        """Record a PR from the society branch into the integration branch. NOT a merge — a request a
        human (or the integrator's merge_to_integration) acts on. NEVER targets master."""
        pr = {"id": len(self.prs) + 1, "from": self.branch, "to": INTEGRATION,
              "title": title[:200], "body": body[:1000]}
        self.prs.append(pr)
        return {"ok": True, "pr": pr}

    def merge_to_integration(self) -> dict:
        """Merge the society branch into society/integration. HARD-REFUSES master/main. A human still
        merges integration -> master after review (the final gate stays human)."""
        target = INTEGRATION
        if target in PROTECTED:
            return {"ok": False, "error": "refused: cannot merge to a protected branch"}
        # ensure the integration branch exists (off master), then merge the society branch into it.
        rc, _ = _git(self.repo, "rev-parse", "--verify", target)
        if rc != 0:
            _git(self.repo, "branch", target, "master")
        # do the merge in a detached operation on the bare repo refs is complex; use a temp worktree.
        wt = str(Path(self.repo).parent / f"echelon-integration-wt-{self.run_id}")
        rc, out = _git(self.repo, "worktree", "add", wt, target)
        if rc != 0:
            return {"ok": False, "error": f"integration worktree failed: {out[:300]}"}
        try:
            rc, out = _git(wt, "merge", "--no-ff", self.branch, "-m",
                           f"society: merge {self.branch} into {target}")
            ok = rc == 0
            result = {"ok": ok, "target": target, "out": out[:400]}
        finally:
            _git(self.repo, "worktree", "remove", "--force", wt)
        return result


def guard_target(branch: str) -> bool:
    """True if `branch` is safe to write (NOT master/main). The structural master-protection."""
    return branch.strip().lower() not in PROTECTED
