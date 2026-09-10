"""Contract checker for intent pools.

Given a list of Intents, validate:
  - unique ids
  - all deps resolve to declared nodes
  - no two intents share the same canonical_for (when set)
  - no dependency cycles

Returns a list of Conflict dataclasses; an empty list means PASS.
Pure stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .intent import Intent


# --- data types -----------------------------------------------------------------

@dataclass
class Conflict:
    """A single validation conflict found in an intent pool."""
    kind: str                           # "duplicate_id", "unresolved_dep", etc.
    detail: str                         # human-readable description
    involved: List[str] = field(default_factory=list)  # intent ids involved


# --- checks ---------------------------------------------------------------------

def _check_unique_ids(intents: List[Intent]) -> List[Conflict]:
    conflicts: List[Conflict] = []
    seen: dict[str, int] = {}
    for idx, intent in enumerate(intents):
        iid = intent.id
        if iid in seen:
            conflicts.append(Conflict(
                kind="duplicate_id",
                detail=f"Intent id {iid!r} appears at indices {seen[iid]} and {idx}",
                involved=[iid],
            ))
        else:
            seen[iid] = idx
    return conflicts


def _check_deps_resolve(intents: List[Intent]) -> List[Conflict]:
    conflicts: List[Conflict] = []
    declared: set[str] = {i.id for i in intents}
    for intent in intents:
        for dep in intent.deps:
            if dep not in declared:
                conflicts.append(Conflict(
                    kind="unresolved_dep",
                    detail=f"Intent {intent.id!r} depends on {dep!r}, "
                           f"which is not declared in the pool",
                    involved=[intent.id, dep],
                ))
    return conflicts


def _check_canonical_for_unique(intents: List[Intent]) -> List[Conflict]:
    conflicts: List[Conflict] = []
    seen: dict[str, str] = {}  # canonical_for -> first intent id
    for intent in intents:
        cf = intent.canonical_for
        if cf is None:
            continue
        if cf in seen:
            conflicts.append(Conflict(
                kind="duplicate_canonical_for",
                detail=f"Intent {intent.id!r} and {seen[cf]!r} both set "
                       f"canonical_for={cf!r}",
                involved=[seen[cf], intent.id],
            ))
        else:
            seen[cf] = intent.id
    return conflicts


def _check_no_cycles(intents: List[Intent]) -> List[Conflict]:
    """Detect cycles via Kahn's algorithm (topological sort).

    Returns a Conflict for each cycle detected.  When a cycle exists, we
    collect the remaining nodes after exhausting the zero-in-degree queue
    and report them as one conflict (they all participate in at least one
    cycle).
    """
    conflicts: List[Conflict] = []
    id_to_idx: dict[str, int] = {i.id: idx for idx, i in enumerate(intents)}
    n = len(intents)

    # Build adjacency and in-degree
    adjacency: list[list[int]] = [[] for _ in range(n)]
    in_degree: list[int] = [0] * n

    for idx, intent in enumerate(intents):
        for dep in intent.deps:
            dep_idx = id_to_idx.get(dep)
            if dep_idx is not None:  # skip unresolved deps (handled separately)
                adjacency[dep_idx].append(idx)
                in_degree[idx] += 1

    # Kahn's algorithm
    queue: list[int] = [i for i in range(n) if in_degree[i] == 0]
    sorted_count = 0
    while queue:
        node = queue.pop()
        sorted_count += 1
        for neighbor in adjacency[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if sorted_count < n:
        # There is at least one cycle — collect remaining nodes
        cycle_ids = [intents[i].id for i in range(n) if in_degree[i] > 0]
        conflicts.append(Conflict(
            kind="cycle",
            detail=f"Dependency cycle detected involving intents: "
                   f"{', '.join(repr(c) for c in cycle_ids)}",
            involved=cycle_ids,
        ))

    return conflicts


# --- main entry point -----------------------------------------------------------

def check(intents: List[Intent]) -> List[Conflict]:
    """Validate an intent pool.

    Returns a list of :class:`Conflict` objects.  An empty list means the
    pool passes all validation checks (PASS).
    """
    conflicts: List[Conflict] = []
    conflicts.extend(_check_unique_ids(intents))
    conflicts.extend(_check_deps_resolve(intents))
    conflicts.extend(_check_canonical_for_unique(intents))
    conflicts.extend(_check_no_cycles(intents))
    return conflicts
