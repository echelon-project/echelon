"""Shared persistence for the workflow-run registry.

Reads/writes ``workflow_runs.json`` under ``~/.echelon/`` (the canonical substrate home).
Preserves a 50-run cap: when saving, the oldest runs (by ``started_at``) are dropped so the
file never grows unbounded.  Self-contained — imports nothing from the rest of ECHELON.

tail/wfpersist (2026-07-30): `list_persisted_runs()` also scans the WorkflowRunJournal
directory (append-only JSONL per run) so runs that outlived a process restart are listable
even when the legacy JSON file is stale. The journal is the durable source; the JSON file
is a fast-lookup supplement.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_HOME = Path.home() / ".echelon"
_FILE = _HOME / "workflow_runs.json"
_MAX_RUNS = 50


def _ensure_home() -> None:
    """Create ~/.echelon/ if it doesn't exist (idempotent)."""
    _HOME.mkdir(parents=True, exist_ok=True)


def load_runs() -> dict[str, dict]:
    """Return the persisted run registry as ``{run_id: {...}}``.

    Returns an empty dict when the file is missing or unreadable.
    """
    if not _FILE.exists():
        return {}
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def run_tree(runs: dict[str, dict] | None = None) -> list[dict]:
    """Fold the FLAT run registry into a forest by the ``parent`` edge (owner, 2026-06-19: runs were
    parentless, so the UI could not reflect a running ECHELON — no tree to render). Each node is the
    run dict plus a ``children`` list; roots are runs with no/unknown parent (the front-door
    originations). Children are ordered by ``started_at``. A run whose parent is missing/dropped (the
    50-cap) re-roots gracefully — the tree never loses a node. See
    runs-need-a-parent-chain-from-the-front-door."""
    runs = load_runs() if runs is None else runs
    nodes = {rid: {**rec, "run_id": rid, "children": []} for rid, rec in runs.items()}
    roots: list[dict] = []
    for rid, node in nodes.items():
        p = node.get("parent")
        if p and p in nodes and p != rid:
            nodes[p]["children"].append(node)
        else:
            roots.append(node)  # no parent, unknown parent (capped), or self -> a root
    def _sort(ns: list[dict]) -> None:
        ns.sort(key=lambda n: n.get("started_at", ""))
        for n in ns:
            _sort(n["children"])
    _sort(roots)
    return roots


def save_runs(runs: dict[str, dict]) -> None:
    """Persist *runs* to ``~/.echelon/workflow_runs.json``, enforcing the 50-run cap.

    When the dict has more than 50 entries the oldest runs (by the ``started_at``
    field) are dropped before writing.  The write is atomic (temp-file + rename)
    so a crash mid-write never corrupts the existing file.
    """
    _ensure_home()

    # ---- cap ----------------------------------------------------------------
    capped = dict(runs)
    while len(capped) > _MAX_RUNS:
        oldest = min(capped.keys(),
                     key=lambda k: capped[k].get("started_at", ""))
        del capped[oldest]

    # ---- atomic write -------------------------------------------------------
    fd, tmp = tempfile.mkstemp(dir=str(_HOME), prefix=".wrkf_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(capped, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, str(_FILE))
    except Exception:
        # best-effort clean-up of the temp file on failure
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_persisted_runs(journal_dir: Path | None = None) -> list[dict]:
    """List all workflow runs from the durable journal directory (tail/wfpersist).
    Falls back gracefully: if the journal module or directory is unavailable, returns [].

    journal_dir=None -> resolve via the standard _workflow_runs_dir() precedence.
    Each entry: {run_id, status, started_at, finished_at, steps_total, steps_done, ok, goal, folder, ...}
    """
    try:
        from echelon_engine.agent.workflow import WorkflowRunJournal, _workflow_runs_dir
        jd = journal_dir or _workflow_runs_dir()
        return WorkflowRunJournal.list_runs(jd)
    except Exception:
        return []
