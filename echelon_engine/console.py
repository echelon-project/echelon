"""console — entry-point wrappers for pip-installed console scripts.

On Windows, the Python console_scripts .exe wrappers don't pass -X utf8,
so Unicode characters (→, ⚡, ⮕) crash on cp1252 terminals. These wrappers
reconfigure stdout/stderr to UTF-8 before handing off to the real main,
so `echelon`, `echelon-mcp`, and `echelon-server` work from any shell.

Each function here is a console_scripts entry point in pyproject.toml.
"""
from __future__ import annotations

import sys
import os


def _reconfigure_encoding():
    """Force UTF-8 on Windows so cp1252 terminals don't crash on Unicode."""
    if sys.platform == "win32":
        # Reconfigure stdout/stderr for UTF-8 (same effect as -X utf8 but
        # works from pip-installed console scripts that can't pass flags).
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        os.environ.setdefault("PYTHONUTF8", "1")


def echelon_main():
    """Entry point for `echelon` console script."""
    _reconfigure_encoding()
    from echelon_engine.__main__ import main
    sys.exit(main())


def echelon_mcp_main():
    """Entry point for `echelon-mcp` console script."""
    _reconfigure_encoding()
    from echelon_engine.mcp_server import main
    sys.exit(main())


def echelon_server_main():
    """Entry point for `echelon-server` console script."""
    _reconfigure_encoding()
    from echelon_engine.server import main
    sys.exit(main())


def echelon_chat_main():
    """Entry point for `echelon-chat` console script."""
    _reconfigure_encoding()
    from echelon_engine.chat import main
    sys.exit(main())


def claude_gem_main():
    """Entry point for `claude-gem` console script."""
    _reconfigure_encoding()
    from echelon_engine.claude_echelon import main_gem
    sys.exit(main_gem())


def claude_deep_main():
    """Entry point for `claude-deep` console script."""
    _reconfigure_encoding()
    from echelon_engine.claude_echelon import main_deep
    sys.exit(main_deep())


def claude_gpt_main():
    """Entry point for `claude-gpt` (Claude Code via Codex App Server)."""
    _reconfigure_encoding()
    from echelon_engine.claude_echelon import main_gpt
    sys.exit(main_gpt())
