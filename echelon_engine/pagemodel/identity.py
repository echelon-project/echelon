#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
identity.py — N1: STABLE / VERSIONED IDs (the pivot).

The graph's node ids are currently positional/name-derived (node:supplier:tab-create) — they break
the moment a DOM id, slug, or fn name changes, taking every reference (diffs, tickets, LLM retrieval)
with them. This assigns a DURABLE id per node from a content FINGERPRINT, and detects renames across
snapshots so a renamed thing keeps its identity (GREPO-style versioned identity keys).

Stable id = <prefix>_<hash10>, where hash = sha256 of the node's identity FINGERPRINT (type + the
stable parts of what it IS), NOT its position. Prefixes: cmp_ (node/component), mod_ (module),
srf_ (surface), ep_ (endpoint), evt_ (event/flow). Human-greppable + machine-stable.

Rename detection (across two snapshots of the same surface): a node with no exact fingerprint match
in the prior snapshot but a strong structural match (same type + same parent module + same role +
high label/endpoint overlap) is linked to its prior id via a `version_of` edge — a compact temporal
trace without duplicating the whole graph per commit.
"""
from __future__ import annotations
import hashlib, re

_PREFIX = {"surface": "srf", "module": "mod", "node": "cmp", "endpoint": "ep", "event": "evt", "flow": "evt"}

def _h(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]

def _norm_endpoint(ep: str) -> str:
    """Normalize an endpoint so /api/po/123 and /api/po/456 share identity: strip query +
    numeric/id/param segs. A TRAILING slash is treated as a concatenated path-param slot
    (`get('/api/suppliers/'+id)` captures '/api/suppliers/') → '/:id', so the by-id GET is
    NOT collapsed into the collection endpoint '/api/suppliers' — they are different
    handlers with different cost/fan-in, and merging them corrupts the perf/blast-radius
    read. A bare '/api/x' with no trailing slash stays as-is."""
    ep = ep.split("?", 1)[0]
    ep = re.sub(r'/\d+', '/:id', ep)
    ep = re.sub(r'/\$\{[^}]+\}', '/:id', ep)
    ep = re.sub(r'/\{[^}]+\}', '/:id', ep)   # FastAPI-style {param}
    if ep.endswith("/") and ep.count("/") > 2:   # trailing-slash concat slot (not just /api/)
        ep = ep[:-1] + "/:id"
    return ep.rstrip("/") or "/"

def fingerprint(node: dict, surface: str) -> tuple:
    """The STABLE identity of a node — what it IS, independent of position/label churn where possible."""
    t = node["type"]
    if t == "surface":
        return ("surface", surface)
    if t == "endpoint":
        # endpoints are GLOBAL (cross-page): identity = normalized path only, NOT scoped to surface.
        return ("endpoint", _norm_endpoint(node.get("label", "")))
    if t == "module":
        # module identity = surface + role + its label (role is more stable than the auto-slug)
        return ("module", surface, node.get("role", ""), node.get("label", node.get("module", "")))
    if t == "node":
        # component identity = surface + module-context + kind + the dom id
        return ("node", surface, node.get("kind", ""), node.get("label", ""))
    return (t, surface, node.get("label", ""))

def stable_id(node: dict, surface: str) -> str:
    fp = fingerprint(node, surface)
    return f"{_PREFIX.get(node['type'], 'nd')}_{_h(*fp)}"

def assign_ids(graph: dict) -> dict:
    """Rewrite a graph's node ids (and the edges that reference them) to STABLE ids. Idempotent.

    Endpoint stable ids are GLOBAL (same across pages) → cross-page joins fall out for free.
    Keeps the old positional id under node['local_id'] for debugging/back-ref.
    """
    surface = graph["surface"]
    remap = {}
    for n in graph["nodes"]:
        old = n["id"]
        sid = stable_id(n, surface)
        n["local_id"] = old
        n["id"] = sid
        remap[old] = sid
    # collapse nodes that now share a stable id (e.g. an endpoint referenced twice)
    seen, uniq = {}, []
    for n in graph["nodes"]:
        if n["id"] in seen:
            continue
        seen[n["id"]] = n; uniq.append(n)
    graph["nodes"] = uniq
    for e in graph["edges"]:
        e["from"] = remap.get(e["from"], e["from"])
        e["to"] = remap.get(e["to"], e["to"])
    # drop self-loops / dup edges created by the remap collapse
    ek, out = set(), []
    for e in graph["edges"]:
        k = (e["from"], e["to"], e["rel"], e.get("label", ""))
        if k in ek:
            continue
        ek.add(k); out.append(e)
    graph["edges"] = out
    graph["id_scheme"] = "stable-v1"
    return graph

# ── rename detection across two snapshots of the same surface ────────────────
def _sim(a: str, b: str) -> float:
    """Cheap token-overlap similarity for labels (Jaccard on char-3-grams)."""
    def g(s): return {s[i:i+3] for i in range(max(0, len(s)-2))} or {s}
    A, B = g(a.lower()), g(b.lower())
    return len(A & B) / max(1, len(A | B))

def detect_renames(prev: dict, curr: dict, threshold: float = 0.55) -> list:
    """Return [{new, old, rel:'version_of', label, sim}] linking a current node to a prior one that
    it likely renamed FROM. Only for nodes present now but whose stable id is absent in prev."""
    prev_ids = {n["id"] for n in prev["nodes"]}
    prev_by_ctx = {}
    for n in prev["nodes"]:
        prev_by_ctx.setdefault((n["type"], n.get("role", ""), n.get("kind", "")), []).append(n)
    links = []
    for n in curr["nodes"]:
        if n["id"] in prev_ids:
            continue  # exact identity survived — no rename
        cands = prev_by_ctx.get((n["type"], n.get("role", ""), n.get("kind", "")), [])
        best, bs = None, threshold
        for p in cands:
            if p["id"] in {n2["id"] for n2 in curr["nodes"]}:
                continue  # that prior id still exists as-is elsewhere
            s = _sim(n.get("label", ""), p.get("label", ""))
            if s >= bs:
                best, bs = p, s
        if best:
            links.append({"new": n["id"], "old": best["id"], "rel": "version_of",
                          "label": f'{best.get("label","")}→{n.get("label","")}', "sim": round(bs, 2)})
    return links

if __name__ == "__main__":
    import argparse, json, pathlib
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prev", help="prior snapshot graph for rename detection")
    args = ap.parse_args()
    g = json.loads(pathlib.Path(args.graph).read_text(encoding="utf-8"))
    assign_ids(g)
    if args.prev:
        prev = json.loads(pathlib.Path(args.prev).read_text(encoding="utf-8"))
        renames = detect_renames(prev, g)
        g["edges"].extend(renames)
        print(f"[i] rename links: {len(renames)}")
    pathlib.Path(args.out).write_text(json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8")
    eps = [n for n in g["nodes"] if n["type"] == "endpoint"]
    print(f"[ok] stable ids -> {args.out}  ({len(g['nodes'])} nodes; {len(eps)} endpoints global)")
