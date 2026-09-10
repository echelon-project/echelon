"""Compiler — the standalone single writer.

Reads all intents from an IntentPool, validates them via :func:`check`, topologically
sorts by dependency edges and ``part_of`` grouping, then writes each file directly
to disk using ``pathlib.Path.write_text``.  The swarm's hands no longer write;
only the compiler does.

Pure stdlib.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from .intent import Intent
from .intent_pool import IntentPool
from .check import check, Conflict


class CompilerError(Exception):
    """Raised when the compiler cannot proceed — e.g. validation conflicts."""

    def __init__(self, message: str, conflicts: List[Conflict] | None = None) -> None:
        super().__init__(message)
        self.conflicts: List[Conflict] = conflicts or []


# ---------------------------------------------------------------------------


def _topological_sort(intents: List[Intent]) -> List[Intent]:
    """Return *intents* in dependency order (deps before dependents).

    Tie-breaking: when multiple nodes are ready, prefer continuing the same
    ``part_of`` group, then sort alphabetically by ``(part_of, id)``.
    """
    id_to_idx: dict[str, int] = {i.id: idx for idx, i in enumerate(intents)}
    n = len(intents)

    adjacency: List[List[int]] = [[] for _ in range(n)]
    in_degree: List[int] = [0] * n

    for idx, intent in enumerate(intents):
        for dep in intent.deps:
            dep_idx = id_to_idx.get(dep)
            if dep_idx is not None:                # unresolved deps already caught by check()
                adjacency[dep_idx].append(idx)
                in_degree[idx] += 1

    # ready queue — nodes with in_degree 0
    ready: List[int] = [i for i in range(n) if in_degree[i] == 0]
    ready.sort(key=lambda i: (intents[i].part_of, intents[i].id))

    result: List[Intent] = []
    last_part_of: str | None = None

    while ready:
        # Prefer a node that continues the current part_of group
        pick = 0
        if last_part_of is not None:
            for j, node_idx in enumerate(ready):
                if intents[node_idx].part_of == last_part_of:
                    pick = j
                    break

        node = ready.pop(pick)
        intent = intents[node]
        result.append(intent)
        last_part_of = intent.part_of

        for neighbor in adjacency[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                ready.append(neighbor)

        ready.sort(key=lambda i: (intents[i].part_of, intents[i].id))

    # check() guarantees no cycles, so result must contain every node
    return result


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def compile(pool: IntentPool, base_dir: str | Path = ".", require_approved: bool = False) -> List[Intent]:
    """Read all intents from *pool*, validate, sort, and write every file.

    Returns the ordered sequence of :class:`Intent` objects that were written
    to disk.  Raises :class:`CompilerError` if *check* finds conflicts.
    """
    all_intents = pool.read()
    
    if require_approved:
        # Filter intents based on 'QUALITY_APPROVED' status
        filtered_intents: List[Intent] = []
        for intent in all_intents:
            intent_id = intent.id
            status_rows = pool.read_status(intent_id=intent_id)
            
            is_approved = False
            if status_rows:
                # Check the latest row (last one in the list, since rows are ordered by rowid ASC)
                latest_row = status_rows[-1]
                if latest_row.get('status') == 'QUALITY_APPROVED':
                    is_approved = True
            
            if is_approved:
                filtered_intents.append(intent)
        
        intents = filtered_intents
    else:
        intents = all_intents

    conflicts = check(intents)
    if conflicts:
        detail_lines: List[str] = []
        for c in conflicts:
            detail_lines.append(f"  [{c.kind}] {c.detail}")
        raise CompilerError(
            f"Compiler: {len(conflicts)} conflict(s) found:\n"
            + "\n".join(detail_lines),
            conflicts,
        )

    ordered = _topological_sort(intents)
    base = Path(base_dir)

    for intent in ordered:
        target = base / intent.target_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(intent.body, encoding="utf-8")

    # Output the ordered sequence (as required by the spec)
    for i, intent in enumerate(ordered, 1):
        print(f"{i}. [{intent.action}] {intent.id} -> {intent.target_path}")

    return ordered
