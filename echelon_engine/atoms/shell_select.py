"""shell_select.py — pick the best shell executable for run_bash.

Inspects executable locations, never executes a command; returns (executable, kind).
kind in {'posix', 'cmd', 'default'}.
"""
from __future__ import annotations
import os
import shutil
from pathlib import Path


def pick_shell(shell: str | None) -> tuple[str | None, str]:
    """Return (executable, kind) where kind in {'posix','cmd','default'}. Explicit > auto."""
    if shell:
        kind = "posix" if Path(shell).name.lower() in ("bash", "bash.exe", "sh", "sh.exe") else "cmd"
        return shell, kind
    if os.name == "nt":
        # System32's bash.exe is a WSL launcher, not a native shell. Its presence
        # says nothing about installed distributions and it cannot promise Windows
        # cwd/Python semantics. WSL remains available through explicit selection.
        b = shutil.which("bash")
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        wsl_launchers = {str(windows / name / "bash.exe").lower()
                         for name in ("System32", "Sysnative", "SysWOW64")}
        local = os.environ.get("LOCALAPPDATA")
        if local:
            wsl_launchers.add(str(Path(local) / "Microsoft" / "WindowsApps" / "bash.exe").lower())
        if b and str(Path(b)).lower() not in wsl_launchers:
            return b, "posix"
        return os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "cmd"
    return None, "default"   # POSIX OS: let shell=True use /bin/sh as before
