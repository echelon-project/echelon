"""Canonical substrate paths — one home for ALL of ECHELON's state.

Owner, 2026-06-05: core.db lives in ~/.echelon; the agent's runtime artifacts — live bridges,
logs, run tmp — belong there too, not scattered in the OS /tmp. One home for the substrate's
state, the way a real OS keeps its files under one root, not in the global scratch.

Layout under ~/.echelon/ (HOME):
  core.db        — the soul + memory (already here)
  bridges/<name> — live-bridge dirs (live.jsonl, reply.jsonl, control.json) per run
  logs/          — run console logs
  runs/          — per-run scratch / artifacts (screenshots, scaffolds the agent produces)
  tmp/           — general substrate tmp (replaces the OS /tmp for agent work)
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# HOME honors $ECHELON_HOME so the whole substrate (dbs + runtime artifacts) can be
# relocated to a work folder — the MCP server / an isolated bank sets it. Unset =
# the unchanged ~/.echelon default. Single resolver shared with
# echelon_engine.atoms.echelon_home (kept duplicated-but-trivial to avoid a
# cross-package import from this low SDK leaf).
HOME = Path(os.environ["ECHELON_HOME"]).expanduser() if os.environ.get("ECHELON_HOME") \
    else (Path.home() / ".echelon")
BRIDGES = HOME / "bridges"
LOGS = HOME / "logs"
RUNS = HOME / "runs"
TMP = HOME / "tmp"
WORK = HOME / "work"          # per-session isolated workspaces (worktrees / scratch dirs)
CORE_DB = HOME / "core.db"


def ensure() -> None:
    """Create the substrate dirs if missing (idempotent). Cheap; call freely."""
    for d in (HOME, BRIDGES, LOGS, RUNS, TMP, WORK):
        d.mkdir(parents=True, exist_ok=True)
    # Best-effort materialise the default config JSON so a fresh install always has
    # an editable ~/.echelon/config.json (idempotent — never clobbers a user-tuned file).
    try:
        from . import config as _config

        _config.write_default_json()
    except Exception:
        pass


def bridge_dir(name: str | None = None) -> Path:
    """A live-bridge dir under ~/.echelon/bridges. Default name = a timestamped run id."""
    ensure()
    name = name or time.strftime("run-%Y%m%d-%H%M%S")
    p = BRIDGES / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_file(name: str | None = None) -> Path:
    ensure()
    name = name or time.strftime("run-%Y%m%d-%H%M%S.log")
    return LOGS / name


def run_dir(name: str | None = None) -> Path:
    ensure()
    name = name or time.strftime("run-%Y%m%d-%H%M%S")
    p = RUNS / name
    p.mkdir(parents=True, exist_ok=True)
    return p
