#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
nerve.py — FILE→NODE index: surfaces annotated findings when an agent touches code.

Every catalogue node maps to source files via its `surfaces` list:
  surface slug "supplier" → ["supplier.html", "assets/page-supplier.js"]

A boundary/endpoint node with multiple surfaces (e.g. /api/restock/catalog appearing on
ledger + supplier + orders) appears under ALL of those files — touching any one of them
warms the finding.

READ-ONLY. nerve.py never mutates the catalogue or sidecar.
NO ECHELON SUBSTRATE: no atoms, no bank, no UAME. Pure JSON reads.

Public API
----------
build_file_index(catalogue_path) -> dict[str, list[str]]
    Maps each source file path (relative) to a list of node_ids whose surfaces include it.

surface_for_file(file_path, catalogue_path, sidecar_path=None) -> list[dict]
    Returns annotated nodes for a touched file:
    [{node_id, file, annotations}] — only nodes with sidecar annotations.

CLI
---
python -m echelon_engine.pagemodel.nerve \\
    --catalogue <c.json> --file <touched-file> [--sidecar <s.json>]
"""
from __future__ import annotations
import json
import pathlib
import argparse
from typing import Optional


# ── helpers ──────────────────────────────────────────────────────────────────

def _source_files_for_surface(slug: str) -> list[str]:
    """The two canonical source paths for a surface slug (relative, forward-slash)."""
    return [f"{slug}.html", f"assets/page-{slug}.js"]


def _load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_sidecar(sidecar_path: pathlib.Path) -> dict:
    if sidecar_path.exists():
        return _load_json(sidecar_path)
    return {"annotations": {}, "asserts": []}


# ── public API ────────────────────────────────────────────────────────────────

def build_file_index(catalogue_path: str | pathlib.Path) -> dict[str, list[str]]:
    """
    Build and return a mapping:  source_file_path (relative str) → [node_id, ...]

    Each node's `surfaces` list (a list of slug strings) determines which files it maps to.
    Endpoint/boundary nodes with multiple surfaces appear under ALL of their surfaces' files.
    """
    cat_path = pathlib.Path(catalogue_path)
    cat = _load_json(cat_path)
    index: dict[str, list[str]] = {}

    for node in cat.get("nodes", []):
        node_id: str = node["id"]
        surfaces: list[str] = node.get("surfaces") or []
        for slug in surfaces:
            for file_path in _source_files_for_surface(slug):
                index.setdefault(file_path, []).append(node_id)

    return index


def surface_for_file(
    file_path: str,
    catalogue_path: str | pathlib.Path,
    sidecar_path: Optional[str | pathlib.Path] = None,
) -> list[dict]:
    """
    Given a file an agent just touched, return the annotated nodes to warm.

    Returns [{node_id, file, annotations}] for every node that:
      (a) maps to `file_path` in the file index, AND
      (b) has at least one annotation in the sidecar.

    Empty list if none qualify (un-annotated file, or file not in catalogue).
    """
    cat_path = pathlib.Path(catalogue_path)

    # Resolve sidecar: explicit > default alongside catalogue
    if sidecar_path is not None:
        sc_path = pathlib.Path(sidecar_path)
    else:
        sc_path = cat_path.with_suffix(".annotations.json")

    index = build_file_index(cat_path)
    sidecar = _load_sidecar(sc_path)
    annotations_map: dict[str, dict] = sidecar.get("annotations", {})

    node_ids = index.get(file_path, [])
    result = []
    seen: set[str] = set()  # de-duplicate: a node may appear once per slug, avoid double-emit

    for node_id in node_ids:
        if node_id in seen:
            continue
        seen.add(node_id)
        ann = annotations_map.get(node_id)
        if ann:  # only nodes with actual annotations
            result.append({
                "node_id": node_id,
                "file": file_path,
                "annotations": ann,
            })

    return result


# ── the NERVE HOOK: compile annotations → reflex-flagged atoms ─────────────────
# Doctrine (owner + echelon_engine/atoms/reflex.py): a reflex fires at the tool-event choke point
# with ZERO bank/fire-time computation — it VALIDATES THE TOOL FIRST (reflex-tool) then GREPS the
# payload (reflex-match regex). So we do NOT run nerve.py live on every Edit; instead nerve COMPILES
# durable annotations into reflex atoms whose stimulus is "Edit/Write a file matching this path".
# `echelon reflex compile` bakes them static; the harness hook fires them. Learned content
# (the finding), programmed firing (tool-gate + path grep). Matches reflex.py's frontmatter contract:
# reflex-match MUST be backslash-free (parse_atom does not unescape) — use [.] not \\. and [/] for slashes.

def _path_grep(file_path: str) -> str:
    """A backslash-free regex that matches this file path inside a serialized tool-event payload.
    Escapes only '.' (-> [.]) and '/' (-> [/]) — the two path chars a regex would otherwise treat
    specially/ambiguously — keeping the pattern free of backslashes per the reflex contract."""
    return file_path.replace(".", "[.]").replace("/", "[/]")

def emit_reflex_atoms(catalogue_path, out_dir, sidecar_path=None, scope="pagemodel"):
    """Compile the sidecar's durable annotations into reflex-flagged atoms (one per annotated FILE).

    Each atom, when compiled by `echelon reflex compile`, fires on a PreToolUse Edit/Write whose
    payload path matches the file — surfacing the findings attached to that file's nodes as guard text.
    READ-ONLY over the catalogue/sidecar; WRITES only the atom .md files into out_dir.
    Returns [paths written].
    """
    cat_path = pathlib.Path(catalogue_path)
    sc_path = pathlib.Path(sidecar_path) if sidecar_path else cat_path.with_suffix(".annotations.json")
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    index = build_file_index(cat_path)
    annotations = _load_sidecar(sc_path).get("annotations", {})
    if not annotations:
        return []

    # invert: file -> [(node_id, ann), ...] for annotated nodes only
    by_file: dict[str, list[tuple]] = {}
    for file_path, node_ids in index.items():
        for nid in dict.fromkeys(node_ids):          # dedup, keep order
            ann = annotations.get(nid)
            if ann:
                by_file.setdefault(file_path, []).append((nid, ann))

    written = []
    for file_path, findings in sorted(by_file.items()):
        slug = _slug_for_file(file_path)
        name = f"pagemodel-finding-{slug}-{file_path.rsplit('/',1)[-1].replace('.','-')}"
        lines = "; ".join(f"{nid} {', '.join(f'{k}={v}' for k,v in ann.items())}" for nid, ann in findings)
        guard = (f"ChainBoard finding(s) on {file_path}: {lines}. "
                 f"You are editing a file with recorded pagemodel findings — review them before changing it.")
        atom = (
            "---\n"
            f"name: {name}\n"
            f"description: pagemodel reflex — surfaces recorded findings when {file_path} is edited\n"
            "metadata:\n"
            "  type: feedback\n"
            "  reflex: true\n"
            "  reflex-event: PreToolUse\n"
            "  reflex-tool: Edit|Write\n"                       # VALIDATE THE TOOL FIRST
            f"  reflex-match: {_path_grep(file_path)}\n"        # then GREP the path (backslash-free)
            "  reflex-action: warn\n"
            "---\n\n"
            f"{guard}\n"
        )
        p = out / f"{name}.md"
        p.write_text(atom, encoding="utf-8")
        written.append(str(p))
    return written

def _slug_for_file(file_path: str) -> str:
    base = file_path.rsplit("/", 1)[-1]
    return base.replace("page-", "").replace(".js", "").replace(".html", "")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        prog="python -m echelon_engine.pagemodel.nerve",
        description="nerve: surface annotated findings for a touched source file",
    )
    parser.add_argument("--catalogue", required=True, help="Path to catalogue JSON")
    parser.add_argument("--sidecar", default=None,
                        help="Path to annotations sidecar JSON (default: <catalogue>.annotations.json)")
    parser.add_argument("--emit-reflexes", metavar="OUT_DIR", default=None,
                        help="Compile durable annotations into reflex-flagged atoms in OUT_DIR "
                             "(then run `echelon reflex compile --root OUT_DIR --scope <s>`).")
    parser.add_argument("--file", dest="file_path", default=None,
                        help="Relative source file path (for the surface query mode)")
    args = parser.parse_args()

    if args.emit_reflexes:
        written = emit_reflex_atoms(args.catalogue, args.emit_reflexes, args.sidecar)
        print(f"[nerve] emitted {len(written)} reflex atom(s) -> {args.emit_reflexes}")
        for w in written:
            print(f"  {w}")
        return

    if not args.file_path:
        parser.error("provide --file <path> (surface query) or --emit-reflexes <dir> (compile mode)")
    results = surface_for_file(args.file_path, args.catalogue, args.sidecar)
    if not results:
        print(f"[nerve] no annotated nodes for file: {args.file_path}")
    else:
        print(f"[nerve] {len(results)} annotated node(s) warm for: {args.file_path}")
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
