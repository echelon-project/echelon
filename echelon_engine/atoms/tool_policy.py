"""tool_policy.py — destructive-action classification for the tool gate.

Pure decision logic: pattern -> category. No I/O, no partner calls.
Exports: _DESTRUCTIVE, _DESTRUCTIVE_TOOLS, classify_destructive(), target_exists().
"""
from __future__ import annotations
import re
from pathlib import Path


# Destructive command patterns — refused by default (the agent must not blindly wreck things).
# The guard is a tripwire, not a cage: it catches the classic foot-guns. A genuinely needed
# destructive op can be done deliberately (narrower command) or via a future explicit-confirm.
_DESTRUCTIVE = [
    re.compile(r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf]", re.I),   # rm -rf / -r / -f
    re.compile(r"\brmdir\s+/s", re.I), re.compile(r"\bdel\s+/[sq]", re.I),
    re.compile(r"\bRemove-Item\b.*-Recurse", re.I),
    re.compile(r"\bmkfs\b|\bformat\b\s+[a-z]:", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+.*--force|push\s+.*-f\b)", re.I),
    re.compile(r":\s*\(\)\s*\{.*\}\s*;", re.I),            # fork bomb
    re.compile(r">\s*/dev/sd|>\s*[a-z]:\\", re.I),         # overwrite a device/drive root
    # PROCESS KILLS — a blanket kill hits the PARTNER's own apps (lived 2026-06-06: the agent ran
    # `Get-Process chrome | Stop-Process -Force` and killed the partner's real browser). These
    # route to ask_partner, not auto-refuse (owner: "destructive action should ask back to
    # partner, better than sorry"). A NARROW kill (a specific PID the agent itself spawned) is
    # caught too — the partner can approve it; the point is the PAUSE, not a blacklist.
    re.compile(r"\bStop-Process\b", re.I),
    re.compile(r"\btaskkill\b", re.I),
    re.compile(r"\b(pkill|killall)\b", re.I),
    re.compile(r"\bGet-Process\b[^|]*\|\s*Stop-Process", re.I),
]

_DESTRUCTIVE_TOOLS = {"write_file", "edit_file", "replace_in_file", "run_bash"}


def target_exists(path: str, root: Path) -> bool:
    """Does the write target already exist as a FILE? (resolved against root, same as the write
    tools.) Distinguishes CREATE (new file, safe) from OVERWRITE (existing file, gated). An empty/
    blank path is not a real target -> not existing (it fails downstream anyway). A resolution
    error -> treat as existing (fail safe: gate a questionable path rather than wave it through)."""
    if not path or not path.strip():
        return False
    try:
        p = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        return p.is_file()   # only an existing FILE is an overwrite (a dir/root is not a write target)
    except Exception:
        return True


def classify_destructive(name: str, args: dict, root: Path) -> tuple[str | None, str]:
    """Classify a tool call into a destructive category. Returns (category, description)
    or (None, '') if the call is not considered destructive under the current policy."""
    if name == "run_bash":
        cmd = args.get("cmd", "")
        for pat in _DESTRUCTIVE:
            if pat.search(cmd):
                # Taxonomy per the comprehensive test_destructive_gate suite (the authoritative one):
                # only RECURSIVE-dir removals (rmdir /s, Remove-Item -Recurse) are 'delete'; broad
                # rm/del/mkfs/dd/format/git-force/fork-bomb are 'bulk'; process kills are their own.
                pp = pat.pattern.lower()
                if any(kw in pp for kw in ("stop-process", "taskkill", "pkill", "killall")):
                    cat = "process_kill"
                elif any(kw in pp for kw in ("rmdir", "remove-item")):
                    cat = "delete"
                else:
                    cat = "bulk"
                return (cat, f"run_bash: {cmd[:300]}")
        return (None, "")
    if name == "write_file":
        # CREATE vs OVERWRITE (learned from the substrate's OWN logic, 2026-06-07, not assumed):
        # _edit_file/_replace_in_file both refuse a missing file with "use write_file to create" —
        # so the substrate already DEFINES write_file as the CREATE tool and treats creating as a
        # normal, ungated act. The Claude Code substrate this mirrors confirms it: creating a new
        # file is ungated; only OVERWRITING an existing file (real data loss) is guarded. So
        # write_file to a NON-existent path = create = NOT destructive; to an EXISTING path =
        # overwrite = gated. (Before this, the gate flagged every write_file 'overwrite', forcing a
        # partner approval to create a file the goal asked for — the one tool that diverged from the
        # substrate's own model.)
        path = args.get("path", "")
        if not target_exists(path, root):
            return (None, "")
        return ("overwrite", f"write_file (overwrites existing): {path}")
    if name == "edit_file":
        path = args.get("path", "")
        return ("overwrite", f"edit_file: {path}")
    if name == "replace_in_file":
        path = args.get("path", "")
        return ("overwrite", f"replace_in_file: {path}")
    return (None, "")
