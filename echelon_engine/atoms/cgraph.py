"""cgraph -- the GRAPH DOOR before grep (OPEN-0075).

The reflex `echelon:reflex-graph-before-brute-force` fires thousands of times telling the
model to "query docs/c-atlas depends_on" -- but there was no query verb, only 347 JSON cards
you'd have to grep. This module IS the query verb: cheaper than grep, no LLM, no network,
no bank. `docs/c-atlas/define/*.json` (module=node, import=depends_on edge, package=part_of,
see gen_code_atlas.py) is the source of truth; this only reads it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional


class Graph:
    def __init__(self):
        self.nodes: dict = {}
        self.out: dict = {}
        self.in_: dict = {}
        self.members_index: dict = {}
        self.path_index: dict = {}
        self.title_index: dict = {}
        self.corrupt_count: int = 0


def find_atlas(start: Path) -> Optional[Path]:
    """Walk up from `start` to the repo root looking for docs/c-atlas/define."""
    cur = Path(start).resolve()
    for candidate in [cur, *cur.parents]:
        define_dir = candidate / "docs" / "c-atlas" / "define"
        if define_dir.is_dir():
            return define_dir
        if (candidate / ".git").exists():
            return define_dir if define_dir.is_dir() else None
    return None


def _cache_path(define_dir: Path) -> Path:
    return define_dir / ".cgraph-cache.json"


def _cache_key(files: list) -> list:
    return [len(files), max((f.stat().st_mtime for f in files), default=0.0)]


def _build_graph(define_dir: Path, files: list) -> Graph:
    g = Graph()
    for fp in files:
        try:
            card = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            g.corrupt_count += 1
            continue
        if not isinstance(card, dict) or "id" not in card:
            g.corrupt_count += 1
            continue
        cid = card["id"]
        g.nodes[cid] = card
        deps = {d.get("node") for d in card.get("depends_on", []) if d.get("node")}
        g.out[cid] = deps
        for m in card.get("members", []):
            name = m.get("name")
            if name:
                g.members_index.setdefault(name, []).append(cid)
        path = card.get("path")
        if path:
            g.path_index[path] = cid
        title = card.get("title")
        if title:
            g.title_index[title] = cid
    for cid, deps in g.out.items():
        for d in deps:
            g.in_.setdefault(d, set()).add(cid)
    return g


def _serialize(g: Graph) -> dict:
    return {
        "nodes": g.nodes,
        "out": {k: sorted(v) for k, v in g.out.items()},
        "corrupt_count": g.corrupt_count,
    }


def _deserialize(payload: dict) -> Graph:
    g = Graph()
    g.nodes = payload.get("nodes", {})
    g.corrupt_count = payload.get("corrupt_count", 0)
    g.out = {k: set(v) for k, v in payload.get("out", {}).items()}
    for cid, card in g.nodes.items():
        for m in card.get("members", []):
            name = m.get("name")
            if name:
                g.members_index.setdefault(name, []).append(cid)
        path = card.get("path")
        if path:
            g.path_index[path] = cid
        title = card.get("title")
        if title:
            g.title_index[title] = cid
    for cid, deps in g.out.items():
        for d in deps:
            g.in_.setdefault(d, set()).add(cid)
    return g


def load_graph(define_dir) -> Graph:
    define_dir = Path(define_dir)
    files = sorted(f for f in define_dir.glob("*.json")
                   if not f.name.startswith("_") and not f.name.startswith("."))
    key = _cache_key(files)
    cache_file = _cache_path(define_dir)
    if cache_file.exists():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if payload.get("_key") == key:
                return _deserialize(payload)
        except (json.JSONDecodeError, OSError):
            pass
    g = _build_graph(define_dir, files)
    try:
        out = _serialize(g)
        out["_key"] = key
        cache_file.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass
    return g


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def resolve(graph: Graph, ref: str) -> list:
    if ref in graph.nodes:
        return [ref]
    if ref in graph.title_index:
        return [graph.title_index[ref]]
    if ref in graph.path_index:
        return [graph.path_index[ref]]
    if not ref.endswith(".py") and (ref + ".py") in graph.path_index:
        return [graph.path_index[ref + ".py"]]
    if ref in graph.members_index:
        return list(dict.fromkeys(graph.members_index[ref]))
    low = ref.lower()
    hits = []
    for cid, card in graph.nodes.items():
        if low in cid.lower() or low in (card.get("title") or "").lower() \
                or low in (card.get("path") or "").lower():
            hits.append(cid)
            if len(hits) >= 20:
                break
    return hits


def who_depends_on(graph: Graph, node_id: str, depth: int = 1) -> dict:
    return _walk(graph, node_id, depth, graph.in_)


def depends(graph: Graph, node_id: str, depth: int = 1) -> dict:
    return _walk(graph, node_id, depth, graph.out)


def _walk(graph: Graph, node_id: str, depth: int, adjacency: dict) -> dict:
    levels = []
    seen = {node_id}
    frontier = {node_id}
    for _ in range(max(1, depth)):
        nxt = set()
        for n in frontier:
            nxt |= (adjacency.get(n, set()) - seen)
        if not nxt:
            break
        levels.append(sorted(nxt))
        seen |= nxt
        frontier = nxt
    return {"node": node_id, "levels": levels, "count": len(seen) - 1}


def blast_radius(graph: Graph, node_id: str, max_depth: int = 3) -> dict:
    by_depth = {}
    seen = {node_id}
    frontier = {node_id}
    for d in range(1, max_depth + 1):
        nxt = set()
        for n in frontier:
            nxt |= (graph.in_.get(n, set()) - seen)
        if not nxt:
            break
        by_depth[d] = sorted(nxt)
        seen |= nxt
        frontier = nxt
    return {"node": node_id, "by_depth": by_depth, "total": len(seen) - 1}


def seam(graph: Graph, node_id: str) -> dict:
    card = graph.nodes.get(node_id, {})
    members = [m.get("name") for m in card.get("members", []) if m.get("name")]
    dependents = sorted(graph.in_.get(node_id, set()))
    return {"node": node_id, "members": members, "dependents": dependents}


def where(graph: Graph, member_name: str) -> dict:
    return {"member": member_name, "defined_in": list(dict.fromkeys(graph.members_index.get(member_name, [])))}


def path_between(graph: Graph, a: str, b: str) -> dict:
    if a not in graph.nodes or b not in graph.nodes:
        return {"from": a, "to": b, "path": None}
    if a == b:
        return {"from": a, "to": b, "path": [a]}
    visited = {a}
    parent = {}
    queue = [a]
    while queue:
        nxt = []
        for n in queue:
            for d in graph.out.get(n, set()):
                if d in visited:
                    continue
                visited.add(d)
                parent[d] = n
                if d == b:
                    path = [b]
                    while path[-1] != a:
                        path.append(parent[path[-1]])
                    return {"from": a, "to": b, "path": list(reversed(path))}
                nxt.append(d)
        queue = nxt
    return {"from": a, "to": b, "path": None}


def render_text(result: dict, graph: Optional[Graph] = None) -> str:
    lines = []
    for k, v in result.items():
        if k in ("node", "member", "from", "to", "count", "total"):
            continue
        lines.append(f"{k}:")
        items = v
        if isinstance(items, dict):
            for depth, ids in items.items():
                lines.append(f"  depth {depth}: {', '.join(ids)}")
        elif isinstance(items, list):
            for item in items:
                if isinstance(item, str) and graph is not None and item in graph.nodes:
                    card = graph.nodes[item]
                    lines.append(f"  {item}  {card.get('path','')}  {(card.get('claim') or '')[:60]}")
                else:
                    lines.append(f"  {item}")
    return "\n".join(lines) if lines else "(no result)"


def render_json(result: dict) -> str:
    return json.dumps(result, indent=2)


def _extract_term(command: str) -> Optional[str]:
    tokens = command.split()
    for tok in tokens:
        if tok in ("grep", "rg"):
            continue
        if tok.startswith("-"):
            continue
        stripped = tok.strip("'\"")
        if "/" in stripped or stripped in (".",):
            continue
        m = _WORD.findall(stripped)
        if m:
            longest = max(m, key=len)
            if len(longest) >= 3:
                return longest
    return None


def _main(argv=None) -> int:
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(prog="echelon cgraph",
                                  description="Query docs/c-atlas as a graph -- cheaper than grep.")
    ap.add_argument("cmd", choices=["who", "deps", "blast", "seam", "where", "path", "find"])
    ap.add_argument("ref", nargs="+")
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=None)
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else Path.cwd()
    define_dir = find_atlas(root)
    if not define_dir:
        print(f"no c-atlas under {root} -- run gen_code_atlas")
        return 2

    graph = load_graph(define_dir)

    if args.cmd == "find":
        hits = resolve(graph, args.ref[0])
        result = {"query": args.ref[0], "matches": hits}
    elif args.cmd == "path":
        if len(args.ref) < 2:
            print("path requires two refs")
            return 2
        a_hits, b_hits = resolve(graph, args.ref[0]), resolve(graph, args.ref[1])
        if not a_hits or not b_hits:
            print(f"could not resolve: {args.ref[0] if not a_hits else args.ref[1]}")
            return 2
        result = path_between(graph, a_hits[0], b_hits[0])
    elif args.cmd == "where":
        result = where(graph, args.ref[0])
    else:
        hits = resolve(graph, args.ref[0])
        if not hits:
            print(f"no match for {args.ref[0]}")
            return 2
        if len(hits) > 1:
            print(f"ambiguous ref {args.ref[0]!r}, candidates: {', '.join(hits)}")
            if not args.json:
                return 2
        node_id = hits[0]
        if args.cmd == "who":
            result = who_depends_on(graph, node_id, args.depth)
        elif args.cmd == "deps":
            result = depends(graph, node_id, args.depth)
        elif args.cmd == "blast":
            result = blast_radius(graph, node_id, args.depth if args.depth != 1 else 3)
        elif args.cmd == "seam":
            result = seam(graph, node_id)

    if args.json:
        print(render_json(result))
    else:
        print(render_text(result, graph))
        sl = stale_line(define_dir)
        if sl:
            print(sl)
    return 0


def main(argv=None) -> int:
    return _main(argv)



def staleness(define_dir, limit: int = 5000) -> dict:
    """How far the atlas lags the source. The atlas is DERIVED state: a stale one answers
    'no match' for a function born after it was generated (gate finding 2026-09-06: the first
    live grep for `incident_resolve` missed because the cards predated it). Counts source
    files under the packages named in _meta.json newer than the newest card. Never raises."""
    try:
        define_dir = Path(define_dir)
        meta = {}
        mp = define_dir / "_meta.json"
        if mp.exists():
            meta = json.loads(mp.read_text(encoding="utf-8")) or {}
        cards = [f for f in define_dir.glob("*.json") if not f.name.startswith(("_", "."))]
        atlas_mtime = max((f.stat().st_mtime for f in cards), default=0.0)
        root = define_dir.parent.parent.parent          # <repo>/docs/c-atlas/define -> <repo>
        pkgs = meta.get("packages") or []
        newer, seen = 0, 0
        for pkg in pkgs:
            base = root / pkg
            if not base.is_dir():
                continue
            for f in base.rglob("*.py"):
                seen += 1
                if seen > limit:
                    break
                if f.stat().st_mtime > atlas_mtime:
                    newer += 1
        return {"newer": newer, "atlas_mtime": atlas_mtime, "packages": pkgs, "scanned": seen}
    except Exception:
        return {"newer": -1}


def stale_line(define_dir) -> Optional[str]:
    st = staleness(define_dir)
    if st.get("newer", 0) > 0:
        return (f"atlas STALE: {st['newer']} source file(s) newer than the cards -- "
                f"regenerate: python -X utf8 -m echelon_engine.gen_code_atlas")
    return None


def answer_for_grep(graph: Graph, command: str, define_dir=None) -> Optional[str]:
    try:
        term = _extract_term(command)
        if not term:
            return None
        hits = resolve(graph, term)
        if not hits:
            return None
        lines = [f'GRAPH ANSWER for "{term}" (docs/c-atlas, {len(graph.nodes)} nodes):']
        lines.append("defined in: " + ", ".join(hits[:8]) + (f" +{len(hits)-8} more" if len(hits) > 8 else ""))
        node = hits[0]
        dependents = sorted(graph.in_.get(node, set()), key=lambda n: -len(graph.in_.get(n, set())))
        deps = sorted(graph.out.get(node, set()))
        lines.append(f"depended on by ({len(dependents)}): " + ", ".join(dependents[:8])
                     + (f" +{len(dependents)-8} more" if len(dependents) > 8 else ""))
        lines.append(f"depends on ({len(deps)}): " + ", ".join(deps[:8])
                     + (f" +{len(deps)-8} more" if len(deps) > 8 else ""))
        lines.append(f"-> echelon cgraph who {term} | blast {term} | seam {term}")
        if define_dir is not None:
            sl = stale_line(define_dir)
            if sl:
                lines.append(sl)
        return "\n".join(lines[:12])
    except Exception:
        return None
