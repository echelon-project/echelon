"""Agent runner — the command center's process REGISTRY.

Rebuilt 2026-06-17 (owner + critic): the old runner was a SINGLETON supervising ONE
process, and `start()` killed the previous one first. But the UI shows N sessions as
independent panels — so launching a 2nd session silently killed the 1st (a lie the UI
couldn't honor). Now it is a REGISTRY: many agent processes, keyed by bridge, each truly
independent. start(B) does NOT touch session A.

Each session is ISOLATED (workspace.py): a git repo workspace -> its own worktree; else a
scratch dir; the engine repo is refused unless forced. So a session can't dirty the engine.

Modes (plan/ask/auto/bypass) pass at launch AND flip live via control.json (the loop
re-reads each step).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from echelon_sdk.paths import BRIDGES, LOGS, ensure
from . import workspace as _ws


class _Session:
    """One supervised agent subprocess + its isolated workspace."""
    def __init__(self, proc: subprocess.Popen, bridge: str, ws: _ws.Workspace,
                 mode: str, goal: str, *, scope: str = "echelon-self",
                 brain: str = "deepseek", budget: float = 9.0) -> None:
        self.proc = proc
        self.bridge = bridge
        self.ws = ws
        self.mode = mode
        self.goal = goal
        self.scope = scope
        self.brain = brain
        self.budget = budget
        self.started_at = time.time()

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def status(self) -> dict:
        return {"bridge": self.bridge, "running": self.running, "root": self.ws.root,
                "workspace_kind": self.ws.kind, "workspace_source": self.ws.source,
                "mode": self.mode, "goal": self.goal, "pid": self.proc.pid,
                "uptime": round(time.time() - self.started_at, 1)}


class AgentRegistry:
    """Supervises N agent subprocesses for the command center — one per bridge."""

    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}
        self.root: str | None = None    # last-used workspace hint for the UI (NOT a launch default)
        self.mode: str = "auto"

    def start(self, *, goal: str, root: str | None = None, mode: str = "auto",
              bridge: str | None = None, force_workspace: bool = False,
              brain: str = "deepseek", scope: str = "echelon",
              cartridges: list[str] | None = None,
              ttl: int = 3600, budget: float = 9.0) -> dict:
        """Launch a NEW, INDEPENDENT session (does NOT stop other sessions).

        Boots via the WARM CARTRIDGE (not the stale soul ritual): the agent wakes foveated on
        `scope`'s atoms with `cartridges` (default ['craft'] — the disciplined-dev cartridge)
        plugged in alongside. So a command-center session is an OPERATOR-IN-SCOPE, not a model
        re-reciting CVs on echelon-self/core.db."""
        if cartridges is None:
            cartridges = ["craft"]
        ensure()
        import secrets
        bridge = bridge or (time.strftime("console-%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2))
        if bridge in self.sessions and self.sessions[bridge].running:
            return {"ok": False, "error": f"bridge '{bridge}' already has a live session"}

        # ISOLATE the workspace (the foot-gun fix). Refusal (engine repo) surfaces as an error.
        try:
            ws = _ws.prepare(bridge, root, force=force_workspace)
        except ValueError as e:
            return {"ok": False, "error": str(e), "needs_workspace": True}

        log = LOGS / f"{bridge}.log"
        cmd = [
            sys.executable, "-X", "utf8", "-u", "-m", "echelon_engine.agent.cli",
            # judge is mandatory-on by omission (a `--judge` flag breaks argparse — see git history).
            # WARM-CARTRIDGE boot (not --boot/soul): foveate `scope` atoms + plug in `cartridges`.
            "--brain", brain, "--scope", scope, "--tiers",
            "--budget", str(budget), "--ttl", str(ttl),
            "--live", bridge, "--mode", mode, "--root", ws.root,
            "--goal", goal,
        ]
        for c in cartridges:
            cmd += ["--cartridge", c]
        full_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        repo = Path(__file__).resolve().parent.parent.parent
        logf = open(log, "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=str(repo), env=full_env,
                                stdout=logf, stderr=subprocess.STDOUT)
        self.sessions[bridge] = _Session(proc, bridge, ws, mode, goal,
                                         scope=scope, brain=brain, budget=budget)
        self.root = ws.root
        return {"ok": True, "bridge": bridge, "root": ws.root, "workspace_kind": ws.kind,
                "workspace_source": ws.source, "mode": mode, "pid": proc.pid}

    def board(self, goals: list, *, scope: str = "echelon", root: str | None = None,
              n_partners: int = 2, model: str | None = None,
              cartridges: list[str] | None = None, dry_run: bool = True) -> dict:
        """ORIGINATE a coordinated multi-partner run from the command center — N craft-equipped
        partners working ONE fan-out through partner.board()'s shared append-only ledger (claim →
        act → land → read), instead of N independent single CLI sessions that can't see each other.

        This is the runner→board() path the frontier named: the console can now launch a coordinated
        SWARM, not just isolated panels. `dry_run=True` (the default + the safe first proof) drives the
        ledger lifecycle at $0 (no live partners spent); `dry_run=False` dispatches real partners
        sandboxed to `root` (an isolated workspace, same foot-gun guard as start()). Returns the
        ledger + the verified invariants so the console can render who-claimed-what."""
        from echelon_engine.agent import partner as _partner
        folder = None
        if not dry_run:
            # REAL partners need hands on disk -> isolate the workspace (same refusal as start()).
            bridge = time.strftime("board-%Y%m%d-%H%M%S")
            try:
                ws = _ws.prepare(bridge, root, force=False)
            except ValueError as e:
                return {"ok": False, "error": str(e), "needs_workspace": True}
            folder = ws.root
        res = _partner.board(goals, scope=scope, folder=folder, n_partners=n_partners,
                             model=model, cartridges=cartridges)
        return {"ok": True, "dry_run": dry_run, "scope": scope,
                "folder": folder, "n_partners": n_partners, **res}

    def stop(self, bridge: str | None = None) -> dict:
        """Stop ONE session (by bridge) — flip control.json stop + terminate the process. Then
        tear down its workspace (worktree/scratch) if untouched."""
        if bridge is None:
            # back-compat: with no bridge, stop the most-recent live session
            live = [s for s in self.sessions.values() if s.running]
            if not live:
                return {"ok": True, "stopped": None}
            bridge = max(live, key=lambda s: s.started_at).bridge
        sess = self.sessions.get(bridge)
        if not sess:
            return {"ok": False, "error": f"no session for bridge '{bridge}'"}
        try:
            from echelon_engine.agent.world.livebridge import LiveBridge
            lb = LiveBridge.__new__(LiveBridge)
            lb.dir = BRIDGES / bridge
            lb.set_control(stop=True)
        except Exception:
            pass
        if sess.running:
            try:
                sess.proc.terminate()
                try:
                    sess.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    sess.proc.kill()
            except Exception:
                pass
        report = _ws.teardown(sess.ws)
        return {"ok": True, "stopped": bridge, "workspace": report}

    def status(self, bridge: str | None = None) -> dict:
        if bridge is not None:
            s = self.sessions.get(bridge)
            return s.status() if s else {"running": False, "bridge": bridge}
        # registry-wide view
        live = {b: s.status() for b, s in self.sessions.items()}
        return {"sessions": live, "live_count": sum(1 for s in self.sessions.values() if s.running),
                "root": self.root, "mode": self.mode}


# one console -> one registry (module singleton holding MANY sessions)
RUNNER = AgentRegistry()
