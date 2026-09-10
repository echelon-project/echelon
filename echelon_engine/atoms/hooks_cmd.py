"""hooks — `echelon install-hooks` and `echelon register-mcp`: Claude Code integration.

install-hooks: Install EVERY staged hook (echelon_engine/hooks_staged/*.py) into
~/.claude/hooks/ — the staged dir is the ONE source of the hook canon (spec S8 V4).
--diff lists installed-vs-staged drift without writing.
register-mcp: Register the echelon MCP server in Claude Code's mcp.json config

Both are idempotent — re-running updates to the latest canon.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ── install-hooks ─────────────────────────────────────────────────────────────

def _hooks_dir() -> Path:
    """The Claude Code hooks directory."""
    return Path.home() / ".claude" / "hooks"


def _staged_dir() -> Path:
    """The staged hook sources (BRIEF W3, spec S8 V4): echelon_engine/hooks_staged/."""
    return Path(__file__).resolve().parent.parent / "hooks_staged"


def cmd_install_hooks(hooks_dir: Path | None = None, room: bool = False, force: bool = False) -> dict:
    """Install EVERY staged hook (hooks_staged/*.py) into ~/.claude/hooks/ (or
    `hooks_dir` when given). Idempotent; existing files are never overwritten
    without `force=True`. `room` is kept for back-compat and no longer changes
    behaviour — every staged hook installs either way (spec S8 V4).
    Returns a dict of {hook_name: result}.
    """
    hooks_dir = hooks_dir or _hooks_dir()
    hooks_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for src in sorted(_staged_dir().glob("*.py")):
        dst = hooks_dir / src.name
        if dst.exists() and not force:
            results[src.name] = "exists (not overwritten — use --force to replace)"
            continue
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dst.chmod(0o755)
        results[src.name] = f"installed → {dst}"
    return results


def cmd_hooks_diff(hooks_dir: Path | None = None) -> dict:
    """Installed-vs-staged drift: {name: 'current'|'drift'|'missing'} for every
    staged hook — read-only, never writes (spec S8 V4)."""
    hooks_dir = hooks_dir or _hooks_dir()
    out = {}
    for src in sorted(_staged_dir().glob("*.py")):
        dst = hooks_dir / src.name
        if not dst.exists():
            out[src.name] = "missing"
        elif dst.read_text(encoding="utf-8") != src.read_text(encoding="utf-8"):
            out[src.name] = "drift"
        else:
            out[src.name] = "current"
    return out


# ── register-mcp ──────────────────────────────────────────────────────────────

def _resolve_scope(target: str) -> str:
    """Kebab of the target directory leaf name."""
    import re
    leaf = os.path.basename(os.path.normpath(target))
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", leaf.lower())).strip("-") or "echelon"


def _mcp_entry(scope: str, home: str = "~/.echelon") -> dict:
    """The echelon MCP server entry for mcp.json. Uses the `echelon-mcp` console
    script (installed on PATH by pip) so it works from any directory."""
    return {
        "command": "echelon-mcp",
        "args": ["--home", home],
        "env": {"ECHELON_SCOPE": scope},
    }


def _read_mcp_json(path: Path) -> dict:
    """Read an mcp.json file, returning {} if missing or corrupt."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_mcp_json(path: Path, data: dict) -> None:
    """Write mcp.json, creating parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def cmd_register_mcp(target: str = "", scope: str = "", user: bool = False,
                     home: str = "~/.echelon") -> dict:
    """Register the echelon MCP server in mcp.json.

    Args:
        target: Project directory (for project-level mcp.json)
        scope: Bank scope
        user: If True, write to ~/.claude/mcp.json instead of project
        home: ECHELON_HOME path for the MCP server

    Returns a report dict.
    """
    if not scope and target:
        scope = _resolve_scope(target)
    elif not scope:
        scope = _resolve_scope(os.getcwd())

    if user:
        config_path = Path.home() / ".claude" / "mcp.json"
    else:
        config_path = Path(target or os.getcwd()) / ".claude" / "mcp.json"

    data = _read_mcp_json(config_path)
    servers = data.get("mcpServers", {})
    existing = "echelon" in servers
    servers["echelon"] = _mcp_entry(scope, home)
    data["mcpServers"] = servers
    _write_mcp_json(config_path, data)

    return {
        "scope": scope,
        "path": str(config_path),
        "action": "updated" if existing else "registered",
        "home": home,
    }


# ── CLI entry points ──────────────────────────────────────────────────────────

def _main_install_hooks(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="echelon install-hooks",
        description="Install ALL staged ECHELON hooks (hooks_staged/*.py) into ~/.claude/hooks/")
    ap.add_argument("--room", action="store_true",
                    help="deprecated no-op — every staged hook installs either way (spec S8 V4)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing hook files instead of keeping them")
    ap.add_argument("--diff", action="store_true",
                    help="list installed-vs-staged drift without writing (exit 1 when any drift)")
    ap.add_argument("--hooks-dir", default=None,
                    help="install into this hooks directory instead of ~/.claude/hooks (tests)")
    a = ap.parse_args(argv)
    hooks_dir = Path(a.hooks_dir) if a.hooks_dir else None
    if a.diff:
        drift = cmd_hooks_diff(hooks_dir=hooks_dir)
        for name, state in drift.items():
            print(f"  {name}: {state}")
        return 1 if any(s != "current" for s in drift.values()) else 0
    results = cmd_install_hooks(hooks_dir=hooks_dir, room=a.room, force=a.force)
    print(f"ECHELON hooks installed ({len(results)} staged hooks):")
    for name, result in results.items():
        print(f"  {name}: {result}")
    return 0


def _main_register_mcp(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="echelon register-mcp",
        description="Register the echelon MCP server in Claude Code's mcp.json")
    ap.add_argument("--target", default=".",
                    help="Project directory (default: cwd)")
    ap.add_argument("--scope", default="",
                    help="Bank scope (default: kebab of directory name)")
    ap.add_argument("--user", action="store_true",
                    help="Write to ~/.claude/mcp.json instead of project config")
    ap.add_argument("--home", default="~/.echelon",
                    help="ECHELON_HOME path for the MCP server (default: ~/.echelon)")
    a = ap.parse_args(argv)
    result = cmd_register_mcp(a.target, a.scope, user=a.user, home=a.home)
    print(f"echelon MCP server {result['action']} in {result['path']}")
    print(f"  scope: {result['scope']}")
    print(f"  home:  {result['home']}")
    if not a.user:
        print(f"  (project-level — use --user for global registration)")
    return 0
