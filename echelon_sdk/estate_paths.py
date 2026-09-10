"""estate_paths — the SDK's own read of the estate config (R-0171 unit A).

A deliberate small duplication of `echelon_engine.estate`. `echelon_sdk` today
imports nothing from `echelon_engine`, and the SDK is the lower layer: making it
depend on the engine to look up a path would invert that and risk an import
cycle for two constants. Both readers parse the SAME file, so the wiring stays
single-sourced even though the reader is not.

    ECHELON_ESTATE_CONFIG=<file>   →   that file
    (unset)                        →   $ECHELON_HOME/estate.json
                                       else ~/.echelon/estate.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def config_path() -> Path:
    override = os.environ.get("ECHELON_ESTATE_CONFIG")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("ECHELON_HOME")
    base = Path(home).expanduser() if home else (Path.home() / ".echelon")
    return base / "estate.json"


def get(key: str) -> Optional[str]:
    """A declared value, or None when the config or key is absent.

    Unlike the engine's resolver this does NOT raise: both call sites here have
    a meaningful "not configured" behaviour (an optional atlas, an optional
    tokenizer dir), so absence is normal rather than a defect.
    """
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if value is None and "." in key:
        head, _, tail = key.partition(".")
        section = data.get(head)
        if isinstance(section, dict):
            value = section.get(tail)
    return str(value) if value is not None else None


def command_root() -> Optional[Path]:
    """The command center checkout, or None when nothing declares it."""
    found = get("command_root")
    return Path(found).expanduser() if found else None
