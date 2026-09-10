r"""_estate_boot — resolve estate paths for a staged hook, without a literal.

R-0171 unit A (OPEN-0121). The twin of the command center's
`hooks/claude/_estate_boot.py`. These hooks are standalone scripts: a harness
runs them by path, with neither the engine nor the command center on sys.path,
so they must find a path BEFORE they can import anything that knows paths.
Previously each one hardcoded `<engine-checkout>`.

The ladder, first hit wins, no machine literal at any rung:
  1. `import echelon_engine` — already importable, so ask it where it lives.
  2. the estate config — read as plain JSON (stdlib only, no engine import).
  3. the pre-existing env override (ECHELON_AGENT / ECHELON_ROOT).
  4. this file's own location — hooks_staged/ -> echelon_engine/ -> the repo.

Rung 4 is a RELATIVE inference, not a hardcoded path: this file physically
lives inside the engine checkout, so its own parents name that checkout on any
machine.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _config_path() -> Path:
    override = os.environ.get("ECHELON_ESTATE_CONFIG")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("ECHELON_HOME")
    base = Path(home).expanduser() if home else (Path.home() / ".echelon")
    return base / "estate.json"


def _from_config(key: str):
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get(key) if isinstance(data, dict) else None
    return str(value) if value else None


def engine_root() -> str:
    """The ECHELON-AGENT checkout, as a string for sys.path.insert."""
    try:
        import echelon_engine
        return str(Path(echelon_engine.__file__).parent.parent)
    except ImportError:
        pass
    found = _from_config("engine_root")
    if found:
        return found
    env = os.environ.get("ECHELON_AGENT")
    if env:
        return env
    # hooks_staged/ -> echelon_engine/ -> the engine checkout itself.
    return str(Path(__file__).resolve().parents[2])


def command_root() -> str:
    """The command center checkout (the estate's other repo)."""
    found = _from_config("command_root")
    if found:
        return found
    env = os.environ.get("ECHELON_ROOT")
    if env:
        return env
    sibling = Path(engine_root()).parent / "ECHELON"
    return str(sibling)
