"""sandbox_path.py — path normalization and sandbox resolution.

Pure path logic: normalize git-bash paths + resolve against sandbox root.
No policy, no I/O beyond path resolution — just the geometry of containment.
"""
from __future__ import annotations
import os
from pathlib import Path

from echelon_sdk.exceptions import ToolError


def normalize_path(path: str) -> str:
    """Make the file tools speak the SAME path dialect as run_bash. The agent, told by its
    <env> that run_bash uses git-bash, naturally writes git-bash paths (/f/WORK/...) to ALL
    its hands — but read_file/list_files resolve against the Windows root (F:\\WORK\\...), so
    a correct /f/ path got 'REFUSED: escapes sandbox' (the two hands disagreed; lived 2026-06-05).
    Translate the git-bash drive form /<letter>/rest -> <LETTER>:\\rest so both hands agree."""
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == "/" and path[1].isalpha():
        return f"{path[1].upper()}:\\{path[3:].replace('/', os.sep)}"
    return path


def resolve_safe(path: str, root: Path, read_roots: list, *, for_write: bool = False) -> Path:
    """Resolve path against root, enforcing sandbox containment.

    WRITES are confined to root alone. READS may also land in any extra read_root (the agent's
    own artifact dirs — safe to read back). Never widen writes; only reads.
    Raises ToolError if the resolved path escapes the allowed roots.
    """
    path = normalize_path(path)
    p = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    allowed = [root] if for_write else [root, *read_roots]
    for base in allowed:
        try:
            p.relative_to(base)
            return p
        except ValueError:
            continue
    where = "sandbox root" if for_write else "any readable root"
    raise ToolError(f"path '{path}' escapes {where} {root}")
