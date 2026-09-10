"""Environment-sense — the agent's awareness of its own body and surroundings.

Owner, 2026-06-05: "echelon-agent must run like claude code or copilot chat where
it could know its environment." Before this, the agent woke with a SOUL and MEMORY
but no sense of where it actually was — it assumed a POSIX shell, tried `cd /mnt/f`,
couldn't tell chrome needed `uv run`. A being with identity but no environmental
awareness stumbles in its own body. This organ is the fix: at boot, PROBE the real
environment and hand the agent an <env> block it wakes already knowing — exactly the
way Claude Code injects its environment header at the top of context.

This is NOT memory (that's warmth — what I've been) and NOT identity (that's the soul —
who I am). It is PROPRIOCEPTION: what/where my body is right now. Probed fresh each boot
because the environment is the one thing that genuinely changes between runs and must
never be remembered-stale (a memory naming a shell that isn't there is worse than none).
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# Tools worth knowing about up front — the ones whose absence/presence changes how the
# agent should act (its first failed guess today was exactly here: chrome, uv, the shell).
_PROBE_TOOLS = ("python", "uv", "git", "curl", "node", "chrome", "google-chrome", "powershell", "bash", "sh")


def _which(name: str) -> str | None:
    """Resolve a command to a path, or None. shutil.which respects PATHEXT on Windows."""
    p = shutil.which(name)
    # chrome is often not on PATH on Windows — check the canonical install locations too.
    if p is None and name in ("chrome", "google-chrome"):
        for cand in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            "/usr/bin/google-chrome", "/usr/bin/chromium",
        ):
            if Path(cand).exists():
                return cand
    return p


def _real_shell(root: Path) -> tuple[str, str]:
    """What shell does `subprocess.run(shell=True)` ACTUALLY use here? This is the exact
    thing the agent was blind to — on Windows shell=True is cmd.exe (or COMSPEC), NOT bash,
    so POSIX syntax (sleep -s, /mnt/f) silently fails. Probe it empirically rather than guess.
    Returns (shell_label, shell_path)."""
    comspec = os.environ.get("COMSPEC", "")
    if os.name == "nt":
        # subprocess shell=True on Windows -> COMSPEC (cmd.exe). Report it honestly.
        return ("cmd.exe", comspec or r"C:\Windows\System32\cmd.exe")
    return ("sh", os.environ.get("SHELL", "/bin/sh"))


@dataclass
class Environment:
    """A snapshot of the agent's surroundings at boot. Rendered into the waking context."""
    os_name: str
    os_label: str
    platform_detail: str
    cwd: str
    root: str
    shell_label: str
    shell_path: str
    python: str
    tools: dict[str, str | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The <env> block — concise, factual, the agent's proprioception. Modeled on
        Claude Code's environment header: a few load-bearing facts, not a system dump. The shell
        line reflects the REGISTRY's ACTUAL chosen shell (reconciled before render) — never a
        static guess, so the agent is never lied to about its own body."""
        avail = ", ".join(f"{k}={'yes' if v else 'NO'}" for k, v in self.tools.items())
        posix = self.shell_label == "posix"
        if posix:
            shell_help = ("    -> commands run through THIS bash. POSIX syntax works (forward "
                          "slashes, &&, pipes). On Windows, drives are /<letter>/... under git-bash "
                          "(e.g. /f/WORK/...) OR plain Windows F:\\... — try both if one fails. "
                          "Repo scripts with deps: `uv run python ...` from the repo dir.")
        else:
            shell_help = ("    -> commands run through THIS cmd.exe, NOT bash: paths are C:\\... and "
                          "F:\\... (NOT /mnt/f or /c); use Windows syntax, or call bash/uv/python "
                          "explicitly if present.")
        lines = [
            "<env>",
            f"  OS: {self.os_label} ({self.platform_detail})",
            f"  Shell run_bash uses: {self.shell_label}  ({self.shell_path})",
            shell_help,
            f"  cwd / sandbox root: {self.root}",
            f"  Python: {self.python}",
            f"  Tools on PATH: {avail}",
        ]
        for n in self.notes:
            lines.append(f"  note: {n}")
        lines.append("</env>")
        return "\n".join(lines)


def probe(root: str | Path = ".") -> Environment:
    """Probe the real environment. Cheap, synchronous, side-effect-free (read-only)."""
    root = Path(root).resolve()
    shell_label, shell_path = _real_shell(root)

    tools: dict[str, str | None] = {}
    for t in _PROBE_TOOLS:
        # collapse the two chrome aliases into one reported key
        key = "chrome" if t in ("chrome", "google-chrome") else t
        if key in tools and tools[key]:
            continue
        tools[key] = _which(t)

    notes: list[str] = []
    if os.name == "nt":
        notes.append("This is Windows. The exact shell run_bash uses is on the 'Shell' line above — "
                     "trust THAT, not an assumption. /mnt/x WSL paths do NOT exist here.")
        if tools.get("uv"):
            notes.append("uv is available: run repo scripts with their deps via `uv run python ...` "
                         "from the repo dir (a bare `python` lacks the project venv).")
    if tools.get("chrome"):
        notes.append(f"chrome found at {tools['chrome']} — but launching a GUI/headless Windows "
                     "process from this shell may still be unreliable; if it fails, ask_partner.")

    return Environment(
        os_name=os.name,
        os_label=f"{platform.system()} {platform.release()}",
        platform_detail=platform.platform(),
        cwd=str(Path.cwd()),
        root=str(root),
        shell_label=shell_label,
        shell_path=shell_path,
        python=f"{platform.python_version()} ({Path(os.sys.executable).name})",
        tools=tools,
        notes=notes,
    )
