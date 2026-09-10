"""echelon_home — the ONE resolver for the substrate's home directory.

Every DB/path default used to hardcode `~/.echelon/`. That made the substrate
un-relocatable: an MCP server (or a second isolated bank) couldn't point echelon
at its own work folder. This leaf resolves the home ONCE, honoring an override:

    ECHELON_HOME=<dir>   →   <dir>/core.db, <dir>/echelon.db, <dir>/.env, …
    (unset)              →   ~/.echelon/   (the unchanged default)

The dir is created on demand (the stores already `mkdir(parents=True)` their
parent, but resolving here means even a fresh ECHELON_HOME is a valid target —
the engine's CardStore/__init__ then creates + migrates the schema into it).

Read at import time by the DB-constant definition sites (cards.py, store.py,
uame.py, …) so a single env var relocates the whole bank. Kept dependency-free
(stdlib only) so it can be imported from the lowest leaves without cycles.
"""
from __future__ import annotations

import os
from pathlib import Path


def echelon_home() -> Path:
    """The substrate home dir: $ECHELON_HOME if set, else ~/.echelon. Not created
    here (cheap, pure) — the store layer makes the dir when it opens a db."""
    override = os.environ.get("ECHELON_HOME")
    return Path(override).expanduser() if override else (Path.home() / ".echelon")


def home_db(name: str) -> Path:
    """A db/file path under the resolved home, e.g. home_db('echelon.db')."""
    return echelon_home() / name
