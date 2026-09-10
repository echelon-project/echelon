#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
graph.py — P2: turn a surface manifest into a traversable GRAPH (nodes + typed edges).

Nodes: the surface, its modules, its endpoints, and its functions (from the manifest).
Edges {from,to,rel,label,confidence}:
  - contains        surface→module, module→node        (structural, deterministic)
  - renders_in      fn→module                          (attribution, deterministic)
  - feeds_from      module→endpoint  (GET/read)        (deterministic — fetch in an attributed fn)
  - writes_to       module→endpoint  (POST/PUT/DELETE) (deterministic)
  - calls           fn→fn                              (deterministic call-graph pass over JS)
  - opens           module→module (modal/drawer toggle) (INFERRED — flagged confidence:inferred)

Own ~40-line edge-walk (neighbours / reachable) — NOT scopegraph/graph_viz (those bind the sqlite bank).

Usage:
  python -m echelon_engine.pagemodel.graph --manifest <m.json> --js <page.js> --out <edges.json>
"""
from __future__ import annotations
import argparse, json, re, pathlib
from echelon_engine.pagemodel import extract as ex
from echelon_engine.pagemodel import identity as idy

WRITE_VERB = re.compile(r"""(?<![\w.])(post|put|del)\(""", re.I)  # writes vs reads

def _dom_id_referenced(did, jslo):
    """True iff DOM id `did` is referenced as a REAL id in `jslo` (lower-cased JS), not merely
    as a substring of another identifier. `did` is expected already lower-cased.
    A component id "builder" must NOT match "querybuilder"/"builder-x" — the collision class the
    call-detection already guards against. Match only the genuine DOM-id reference forms:
      '<did>'  "<did>"  `<did>`   #<did>   getElementById('<did>')   querySelector('#<did>')
      data-*="<did>"
    boundary = the id is delimited by a non-[\\w-] char (or a quote/#) on both sides."""
    e = re.escape(did)
    pats = (
        rf"['\"`]#?{e}['\"`]",                 # quoted 'id' / "#id" / `id`  (exact between quotes)
        rf"#{e}(?![\w-])",                     # #id not glued to another word char / hyphen
        rf"getelementbyid\(['\"`]{e}['\"`]",   # getElementById('id')
        rf"queryselector\(['\"`]#{e}['\"`]",   # querySelector('#id')
        rf"data-[\w-]+=['\"]{e}['\"]",         # data-foo="id"
    )
    return bool(re.search("|".join(f"(?:{p})" for p in pats), jslo))

def _endpoint_dir(js_body, endpoint):
    """Heuristic: is this endpoint a write (post/put/del near it) or a read (fetch/get)?"""
    # look at the call verb immediately preceding the endpoint literal
    idx = js_body.find(endpoint)
    if idx < 0:
        return "feeds_from"
    pre = js_body[max(0, idx-40):idx]
    return "writes_to" if WRITE_VERB.search(pre) else "feeds_from"

def build_graph(mf, js):
    nodes, edges = [], []
    # STATIC-HTML surfaces have no client JS, so no fn can "handle" a DOM id — every node would
    # look dead. Mark them so the dead-code query can honestly abstain (we can't observe handling
    # without the client module), instead of false-flagging the whole surface as unreachable.
    static = mf.get("source") == "static-html"
    sid = f"surface:{mf['surface']}"
    nodes.append({"id": sid, "type": "surface", "label": mf["surface"],
                  "loc": mf["loc_total"], "role": "page",
                  **({"static": True} if static else {})})

    # index fn name -> module for calls/renders edges
    fn_module = {}
    for m in mf["modules"]:
        mid = f"module:{mf['surface']}:{m['module']}"
        nodes.append({"id": mid, "type": "module", "label": m["label"], "module": m["module"],
                      "role": m["role"], "loc": m["loc_estimate"], "fn_count": m["fn_count"]})
        edges.append({"from": sid, "to": mid, "rel": "contains", "label": "", "confidence": "certain"})
        # module -> node (DOM component)
        chrome = set(mf.get("chrome_ids", []))
        for nd in m["nodes"]:
            nid = f"node:{mf['surface']}:{nd['id']}"
            n = {"id": nid, "type": "node", "label": nd["id"], "kind": nd["kind"]}
            if nd["id"] in chrome:
                n["chrome"] = True   # above the module layer — structural, not dead-eligible
            if static:
                n["static"] = True   # no client JS to observe handling — not dead-check eligible
            nodes.append(n)
            edges.append({"from": mid, "to": nid, "rel": "contains", "label": nd["kind"], "confidence": "certain"})
        # module -> endpoint (feeds_from / writes_to)
        body_all = " ".join(ex._body_of(js, f) for f in ex.js_functions(js)
                            if f["name"] in {x["name"] for x in m["fns"]})
        for ep in m["data_fetch"]:
            rel = _endpoint_dir(body_all, ep)
            path = idy._norm_endpoint(ep)               # path-only is the identity
            eid = f"endpoint:{path}"                    # id keyed on normalized path (dedups variants)
            nodes.append({"id": eid, "type": "endpoint", "label": path, "raw": [ep]})
            edges.append({"from": mid, "to": eid, "rel": rel, "label": "", "confidence": "certain"})
        # fn -> module (renders_in)
        for f in m["fns"]:
            fn_module[f["name"]] = mid

    # SURFACE-LEVEL endpoint fallback: an endpoint that the whole-page scan found but
    # that never landed in a module's data_fetch (fetch in an UN-attributed fn — a
    # shared helper, a top-level IIFE, a dynamically-built URL) would otherwise be
    # INVISIBLE in the graph → its calling surface dropped from fan-in → a fix gated
    # as safe while a real caller went uncounted. We attach it to the surface with an
    # honest attribution marker rather than fabricate a module owner. This is what
    # makes endpoint fan-in a trustworthy blast-radius signal (a false-negative here
    # is exactly how you ship a break).
    attributed_paths = {idy._norm_endpoint(ep)
                        for m in mf["modules"] for ep in m["data_fetch"]}
    for ep in mf.get("all_endpoints", []):
        path = idy._norm_endpoint(ep)
        if path in attributed_paths:
            continue
        eid = f"endpoint:{path}"
        nodes.append({"id": eid, "type": "endpoint", "label": path, "raw": [ep],
                      "attribution": "surface"})
        edges.append({"from": sid, "to": eid, "rel": _endpoint_dir(js, ep),
                      "label": "", "confidence": "surface"})
        attributed_paths.add(path)

    # de-dup endpoint/node repeats
    seen = set(); nodes = [n for n in nodes if not (n["id"] in seen or seen.add(n["id"]))]

    # CALLS: fn -> fn (deterministic call-graph). A fn calls another if its body invokes that name.
    allfns = ex.js_functions(js)
    names = {f["name"] for f in allfns}
    for f in allfns:
        caller_mod = fn_module.get(f["name"])
        if not caller_mod:
            continue
        body = ex._body_of(js, f)
        called = {n for n in names if n != f["name"] and re.search(r'(?<![\w.])'+re.escape(n)+r'\s*\(', body)}
        for c in called:
            callee_mod = fn_module.get(c)
            if callee_mod and callee_mod != caller_mod:
                edges.append({"from": caller_mod, "to": callee_mod, "rel": "calls",
                              "label": f"{f['name']}→{c}", "confidence": "certain"})

    # HANDLES: module -> node when SOME fn's body actually references that DOM id (real invocation,
    # not mere containment). A component contained but touched by NO code is DEAD (zero handles-in).
    # Build a set of all dom ids referenced anywhere in the JS (id literal / #id / getElementById).
    jslo = js.lower()
    for m in mf["modules"]:
        mid = f"module:{mf['surface']}:{m['module']}"
        for nd in m["nodes"]:
            did = nd["id"].lower()
            touched = _dom_id_referenced(did, jslo)
            if touched:
                nid = f"node:{mf['surface']}:{nd['id']}"
                edges.append({"from": mid, "to": nid, "rel": "handles", "label": "", "confidence": "certain"})

    # OPENS (inferred): a module fn that toggles a modal/drawer → module link. Heuristic, flagged.
    for m in mf["modules"]:
        mid = f"module:{mf['surface']}:{m['module']}"
        for f in m["fns"]:
            if re.search(r'(open|show)(modal|drawer|detail)', f["name"], re.I):
                edges.append({"from": mid, "to": mid, "rel": "opens",
                              "label": f["name"], "confidence": "inferred"})

    # collapse duplicate edges
    ekey = lambda e: (e["from"], e["to"], e["rel"], e["label"])
    eseen = set(); edges = [e for e in edges if not (ekey(e) in eseen or eseen.add(ekey(e)))]

    det = sum(1 for e in edges if e["confidence"] == "certain")
    return {
        "surface": mf["surface"],
        "node_count": len(nodes), "edge_count": len(edges),
        "deterministic_pct": round(100 * det / max(1, len(edges)), 1),
        "nodes": nodes, "edges": edges,
    }

# ── tiny edge-walk (our own; no bank coupling) ───────────────────────────────
def neighbours(graph, node_id, rel=None):
    return [e["to"] for e in graph["edges"]
            if e["from"] == node_id and (rel is None or e["rel"] == rel)]

def reachable(graph, start, rels=None):
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        if n in seen: continue
        seen.add(n)
        for e in graph["edges"]:
            if e["from"] == n and (rels is None or e["rel"] in rels):
                stack.append(e["to"])
    seen.discard(start)
    return seen

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--js", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    mf = json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8"))
    g = build_graph(mf, ex.read(args.js))
    pathlib.Path(args.out).write_text(json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] graph {args.out}: {g['node_count']} nodes, {g['edge_count']} edges, "
          f"{g['deterministic_pct']}% deterministic")
    # quick dangling check
    ids = {n["id"] for n in g["nodes"]}
    dangling = [e for e in g["edges"] if e["from"] not in ids or e["to"] not in ids]
    print(f"    dangling edges: {len(dangling)}" + (f"  !! {dangling[:2]}" if dangling else "  (none)"))

if __name__ == "__main__":
    main()
