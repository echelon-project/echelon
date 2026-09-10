"""repo → knowledge bank: the AIFACTOR structural graph, ingested as VERSIONED COS entries.

The bridge between two reclaimed pieces:
  - repo_graph.py (AIFACTOR's graph_builder, reclaimed) mechanically extracts a repo's STRUCTURE —
    every file, every function signature, every import edge — without an LLM. A stable structural
    address per element.
  - bank.py (COS × ENTRY) stores CONTENT at COS coordinates, with content-versioning.

This module maps the first onto the second: a structural element's address IS a COS coordinate, its
signature/outline IS the content, and re-scanning the SAME element later (same repo:file:function)
whose content CHANGED creates a NEW VERSION — the bank's supersedes chain captures the timeline.
Owner: "same path, same repo, same file, same function, could have diff content later." That is
exactly what versioning-on-change gives: a re-scan of unchanged code is a no-op; a changed signature
or a moved function supersedes its prior version, timestamped, walkable via bank.history(coord).

WHY THIS IS THE RIGHT SHAPE (not brute storage): AIFACTOR's earned insight is "substrate over the
brain" — extract structure once, mechanically, let the agent read the GRAPH. Ingesting that graph
into the bank makes it PERSISTENT and VERSIONED: the agent doesn't re-scan a repo from scratch each
session; it queries the bank by coordinate, gets the current structure, and asks history() what
changed since last scan. The structural graph stops being per-run scratch and becomes durable,
queryable, time-versioned knowledge. See memory: summariser-is-symptom-of-brute-reading,
cos-x-entry-coordinate-spine, knowledge-bank-cos-x-entry-rag.
"""
from __future__ import annotations

import re
from pathlib import Path

from .bank import KnowledgeBank
from echelon_sdk.repo_graph import build_graph, FileEntry

_SEG = re.compile(r"[^a-z0-9]+")


def _seg(s: str) -> str:
    """Sanitize one path/name fragment into a COS coordinate segment (lowercase, no separators)."""
    return _SEG.sub("_", s.lower()).strip("_")


def _file_coord(repo: str, relpath: str) -> str:
    """A file's COS coordinate: repo:<repo>:<dir>:<dir>:<filestem>. The directory nesting becomes the
    taxonomy depth — so path-depth fallback (bank §5) ascends from a function to its file to its
    package naturally."""
    p = relpath[:-3] if relpath.endswith(".py") else relpath   # drop .py
    parts = [_seg(x) for x in p.split("/") if x and x != "__init__"]
    return ":".join(["repo", _seg(repo)] + parts) if parts else f"repo:{_seg(repo)}"


def _elem_coord(file_coord: str, element: str) -> str:
    """A function/class/method's coordinate = its file's coordinate + the element's dotted name as a
    final segment. 'KnowledgeBank.resolve' -> '<file>:knowledgebank_resolve'."""
    name = element.split("(", 1)[0].strip()        # signature -> name only for the address
    return f"{file_coord}:{_seg(name)}"


def ingest_repo(bank: KnowledgeBank, root: str | Path, repo: str | None = None,
                max_files: int = 4000) -> dict:
    """Scan a repo's structure and store it into the bank as versioned coordinate entries.

    One entry per FILE (kind='file_outline': imports + the file's element list) and one per
    FUNCTION/CLASS/METHOD (kind='signature': the element's signature line). Re-running on a changed
    repo writes ONLY the changed elements as new versions (content-versioning); unchanged elements
    are no-ops. Returns counts {files, signatures, versioned (changed), unchanged}.

    The repo name (the coordinate root after 'repo:') defaults to the root dir's name."""
    root = Path(root).resolve()
    repo = repo or root.name
    graph = build_graph(root, max_files=max_files)
    counts = {"files": 0, "signatures": 0, "versioned": 0, "unchanged": 0, "errors": 0}

    for fe in graph["files"]:        # fe: FileEntry
        fcoord = _file_coord(repo, fe.path)
        if fe.error:
            # record the parse/skip state too — it's knowledge ('this file is minified/too big').
            _store_versioned(bank, f"!! {fe.error}", fcoord, "file_status", repo, fe.path, counts)
            counts["errors"] += 1
            continue
        # the file-level outline (imports + element index) — the structural summary of the file.
        outline = _file_outline_text(fe)
        _store_versioned(bank, outline, fcoord, "file_outline", repo, fe.path, counts)
        counts["files"] += 1
        # one entry per element (the audit-relevant surface repo_graph already extracted).
        for sig in fe.functions:
            ecoord = _elem_coord(fcoord, sig)
            _store_versioned(bank, sig, ecoord, "signature", repo, fe.path, counts)
            counts["signatures"] += 1
        for cls in fe.classes:
            ecoord = _elem_coord(fcoord, cls)
            _store_versioned(bank, f"class {cls}", ecoord, "signature", repo, fe.path, counts)
            counts["signatures"] += 1
    return counts


def _file_outline_text(fe: FileEntry) -> str:
    parts = [f"{fe.path}  ({fe.loc} LOC, {fe.bytes} bytes)"]
    if fe.imports:
        parts.append("imports: " + ", ".join(fe.imports[:20]))
    if fe.classes:
        parts.append("classes: " + ", ".join(fe.classes))
    if fe.functions:
        parts.append("functions: " + ", ".join(fe.functions))
    return "\n".join(parts)


def _store_versioned(bank: KnowledgeBank, text: str, coord: str, kind: str,
                     repo: str, relpath: str, counts: dict) -> None:
    """Store an element, counting whether it was a new version or an unchanged no-op. The bank's
    store() already does the content-hash compare; we detect 'unchanged' by id-stability (a no-op
    returns the prior id, whose ts is older than now)."""
    import time
    before = bank._current_at(coord, kind, int(time.time()))
    new_id = bank.store(text, coordinate=coord, kind=kind, source=f"repo_ingest:{relpath}", scope=repo)
    if before is not None and new_id == before.id:
        counts["unchanged"] += 1
    elif before is not None:
        counts["versioned"] += 1     # changed -> a new version superseded the prior
    # (before is None = brand new element, already counted in files/signatures)
