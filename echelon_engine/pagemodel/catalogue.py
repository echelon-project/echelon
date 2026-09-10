#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
catalogue.py — N3: the CROSS-PAGE CATALOGUE (per-page report → compounding asset).

Runs extract → graph → stable-ids over EVERY page in a repo and merges the graphs into ONE store
keyed by stable id. Because endpoint ids are GLOBAL (identity.py), an endpoint referenced by three
pages is ONE node with three inbound edges — it becomes a true BOUNDARY node, and cross-page questions
become one-hop queries:

  - endpoint fan-in:  who calls /api/restock/po?  → the pages/modules sharing it (blast radius)
  - dead code:        components with zero inbound edges from any surface → unreachable
  - inventory:        the real component/endpoint census (is it 100 things or 1 thing ×40?)

This is the asset the whole vision hinges on: addressable objects in one queryable store.

Usage:
  python -m echelon_engine.pagemodel.catalogue --os-dir <os> --out <catalogue.json> [--report]
"""
from __future__ import annotations
import argparse, json, re, pathlib
from collections import defaultdict
from echelon_engine.pagemodel import extract as ex, graph as gr, identity as idy

def _resolve_pages(paths) -> list:
    """Resolve input path(s) into a de-duplicated, sorted list of (slug, html, js) page
    pairs. Each input entry may be:
      • a DIRECTORY  → every *.html in it, paired with assets/page-<slug>.js (the OS layout);
      • an .html FILE → paired with its sibling assets/page-<slug>.js;
      • a .js  FILE   → paired back to the <slug>.html two dirs up (…/assets/page-x.js → …/x.html).
    Accepts a single str/Path or an iterable of them, so callers can pass a dir, a list of
    dirs, or an explicit list of files interchangeably.

    A page whose page-<slug>.js sibling is MISSING is NOT skipped — it is yielded with js=None,
    which routes it through STATIC-HTML mode (endpoints read from the HTML itself, see
    static_html.py). This makes a static server-rendered mirror (JSF/PrimeFaces, no client module)
    a first-class surface. The page.js path is unchanged: when the .js exists, (html, js) is paired
    exactly as before."""
    if isinstance(paths, (str, pathlib.Path)):
        paths = [paths]
    pages: dict[str, tuple] = {}   # slug -> (html, js|None); js=None → static-HTML mode
    for p in paths:
        pp = pathlib.Path(p)
        if pp.is_dir():
            htmls = sorted(pp.glob("*.html"))
            js_for = lambda h, base=pp: base / "assets" / f"page-{h.stem}.js"
        elif pp.suffix == ".html":
            htmls = [pp]
            js_for = lambda h: h.parent / "assets" / f"page-{h.stem}.js"
        elif pp.suffix == ".js":
            # …/assets/page-<slug>.js  →  slug + sibling html one level up from assets/
            slug = pp.stem[len("page-"):] if pp.stem.startswith("page-") else pp.stem
            html = pp.parent.parent / f"{slug}.html"
            htmls = [html] if html.exists() else []
            js_for = lambda h, j=pp: j
        else:
            continue
        for h in htmls:
            if not h.exists():
                continue
            j = js_for(h)
            # page.js path preserved exactly: real .js → (html, js). Missing .js → (html, None),
            # which extract.build/build_graph route through STATIC-HTML mode. No page is skipped.
            pages.setdefault(h.stem, (h, j if j.exists() else None))
    return [(slug, h, j) for slug, (h, j) in sorted(pages.items())]


def _merge_graph(g, slug, nodes, edges, surfaces):
    """Merge one per-surface graph into the accumulating catalogue store (endpoint ids are global,
    so a shared endpoint becomes one boundary node with multi-surface provenance)."""
    surfaces.append(slug)
    for n in g["nodes"]:
        sid = n["id"]
        if sid not in nodes:
            nodes[sid] = {**n, "surfaces": set(), "raw": set(n.get("raw", []))}
        nodes[sid]["surfaces"].add(slug)
        nodes[sid]["raw"].update(n.get("raw", []))
    for e in g["edges"]:
        edges.append({**e, "surface": slug})


def build_catalogue(os_dir=None, *, paths=None) -> dict:
    """Build the cross-page catalogue from a directory (os_dir, back-compat) OR an explicit
    list of dirs/files (paths). Pass either; paths wins if both are given.

    Three surface shapes are auto-detected, in priority order, each behaviour-preserving for the
    others (migrate discipline):
      • SINGLE-BUNDLE SPA — a dir with index.html + a monolithic app.js and NO per-page modules:
        routes (hash-router) become surfaces, bundle fetch()/api() strings become endpoints (spa.py)
      • page.js OS layout — *.html paired with assets/page-<slug>.js  (unchanged)
      • static-HTML mirror — *.html with no .js sibling                (unchanged, js=None)"""
    src = paths if paths is not None else os_dir
    if src is None:
        raise ValueError("build_catalogue needs os_dir or paths")
    nodes, edges = {}, []          # nodes keyed by stable id (merge); edges accumulate
    surfaces = []

    # ── SPA pass: detect single-bundle SPA directories and route them to spa.build_spa_graph.
    # A detected SPA dir is consumed here so _resolve_pages doesn't also see its index.html as a
    # lone static surface. Non-dir inputs and non-SPA dirs fall through to the page.js/static path.
    from echelon_engine.pagemodel import spa as spa_mod
    src_list = [src] if isinstance(src, (str, pathlib.Path)) else list(src)
    spa_dirs = set()
    for p in src_list:
        det = spa_mod.detect_spa(p)
        if det:
            index_html, app_js = det
            spa_dirs.add(str(pathlib.Path(p).resolve()))
            html, js = ex.read(index_html), ex.read(app_js)
            for g in spa_mod.build_spa_graph(pathlib.Path(p).name, html, js):
                _merge_graph(g, g["surface"], nodes, edges, surfaces)

    # ── page.js / static-HTML pass over everything that wasn't a consumed SPA dir.
    remaining = [p for p in src_list
                 if str(pathlib.Path(p).resolve()) not in spa_dirs]
    for slug, h, js in _resolve_pages(remaining):
        # js is a Path (page.js mode) or None (static-HTML mode). Read the js only when present;
        # static mode feeds build()=None (HTML endpoint extraction) and build_graph()="" (no JS
        # call-graph to walk) — the surface-level endpoint fallback still fires from all_endpoints.
        js_src = ex.read(js) if js is not None else None
        mf = ex.build(slug, ex.read(h), js_src)
        g = gr.build_graph(mf, js_src if js_src is not None else "")
        idy.assign_ids(g)
        _merge_graph(g, slug, nodes, edges, surfaces)

    # de-dup edges globally (same from→to→rel across pages kept once, with page list)
    emerge = {}
    for e in edges:
        k = (e["from"], e["to"], e["rel"])
        emerge.setdefault(k, {"from": e["from"], "to": e["to"], "rel": e["rel"],
                              "surfaces": set()})["surfaces"].add(e["surface"])
    # serialize sets
    for n in nodes.values():
        n["surfaces"] = sorted(n["surfaces"])
        n["raw"] = sorted(n.get("raw", set()))
    out_edges = [{**{k: v for k, v in e.items() if k != "surfaces"},
                  "surfaces": sorted(e["surfaces"])} for e in emerge.values()]

    return {"kind": "pagemodel-catalogue", "surfaces": surfaces,
            "node_count": len(nodes), "edge_count": len(out_edges),
            "nodes": list(nodes.values()), "edges": out_edges}

# ── queries (pure reads over the merged store) ───────────────────────────────
def endpoint_fanin(cat: dict) -> list:
    """Each endpoint → the modules/surfaces that hit it. Boundary/blast-radius."""
    inbound = defaultdict(list)
    for e in cat["edges"]:
        inbound[e["to"]].append(e)
    out = []
    for n in cat["nodes"]:
        if n["type"] != "endpoint":
            continue
        callers = inbound.get(n["id"], [])
        surfs = sorted({s for e in callers for s in e["surfaces"]})
        out.append({"id": n["id"], "endpoint": n["label"], "callers": len(callers),
                    "surfaces": surfs, "shared": len(surfs) > 1})
    return sorted(out, key=lambda r: -len(r["surfaces"]))

# shell/nav landmarks that live above the module layer and repeat across pages — chrome by role,
# wired by the shell (delegation/selector), never a dead leaf.
CHROME_LANDMARKS = {"nav", "tabbar", "topbar", "sidebar", "header", "footer", "navbar", "conn-pill",
                    "palette-hint", "theme-toggle", "density-toggle", "shell", "app", "main",
                    "content", "brand", "logo", "menu", "drawer-scrim"}

def dead_nodes(cat: dict) -> dict:
    """Split un-handled components into HONEST categories — STRUCTURAL ROLE is the discriminator:

    - unreferenced: a LEAF component inside a module that no code touches → likely genuinely dead.
    - chrome:       above/outside the module layer OR a known shell landmark (nav/tabbar/…) → does
                    the reaching, wired by the shell, NOT module-owned. Reported, not counted as dead.
    - container:    the module's OWN container id (it IS a module) → structural, not a dead leaf.

    Reachability = a `handles` edge (a fn references the id), NOT `contains` (mere nesting).
    """
    handled = {e["to"] for e in cat["edges"] if e["rel"] == "handles"}
    # module container ids (a module node's 'module'/label) — their own section id isn't a dead leaf
    module_ids = set()
    for n in cat["nodes"]:
        if n["type"] == "module":
            module_ids.add(n.get("module", "")); module_ids.add(n.get("label", ""))
    unref, chrome, container, static = [], [], [], []
    for n in cat["nodes"]:
        if n["type"] != "node" or n["id"] in handled:
            continue
        rec = {"id": n["id"], "label": n["label"], "surfaces": n["surfaces"]}
        lab = n["label"].lower()
        if n.get("static"):
            static.append(rec)             # static-HTML: no client JS to observe handling → ABSTAIN,
            continue                       # not a dead-code claim (a false "dead" green-lights a break)
        if n.get("chrome") or lab in CHROME_LANDMARKS:
            chrome.append(rec)
        elif n["label"] in module_ids or lab.endswith(("-panel", "-surface")):
            container.append(rec)          # the module's own container — structural
        else:
            unref.append(rec)              # genuine dead leaf
    return {"unreferenced": unref, "chrome": chrome, "container": container, "static": static}

def inventory(cat: dict) -> dict:
    by_type = defaultdict(int)
    for n in cat["nodes"]:
        by_type[n["type"]] += 1
    # endpoint reuse: how many endpoints are shared across >1 surface
    fanin = endpoint_fanin(cat)
    shared_ep = [r for r in fanin if r["shared"]]
    return {"by_type": dict(by_type), "surfaces": len(cat["surfaces"]),
            "shared_endpoints": len(shared_ep), "total_endpoints": len([r for r in fanin])}

def report(cat: dict):
    inv = inventory(cat)
    print(f"\n{'='*66}\nCROSS-PAGE CATALOGUE  ({inv['surfaces']} surfaces)\n{'='*66}")
    print(f"nodes={cat['node_count']}  edges={cat['edge_count']}  by-type={inv['by_type']}")
    print(f"endpoints: {inv['total_endpoints']} total, {inv['shared_endpoints']} shared across >1 page (boundary)")
    print(f"\nTOP SHARED ENDPOINTS (boundary / blast-radius):")
    for r in endpoint_fanin(cat)[:10]:
        if r["shared"]:
            print(f"  {r['endpoint']:<42} {len(r['surfaces'])} pages: {', '.join(r['surfaces'][:5])}")
    d = dead_nodes(cat)
    ur, ch, co, st = d["unreferenced"], d["chrome"], d["container"], d.get("static", [])
    print(f"\nUNREFERENCED LEAVES (in a module, touched by no code — LIKELY DEAD): {len(ur)}" +
          (f"\n  e.g. {', '.join(x['label']+' ('+(x['surfaces'][0] if x['surfaces'] else '?')+')' for x in ur[:8])}" if ur else "  (none)"))
    print(f"chrome (nav/tabbar/shell — wired by shell, NOT dead): {len(ch)}   "
          f"container (module's own id — structural): {len(co)}")
    if st:
        print(f"static-HTML (no client JS to prove handling — ABSTAIN, not dead): {len(st)}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--os-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    cat = build_catalogue(args.os_dir)
    pathlib.Path(args.out).write_text(json.dumps(cat, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.report:
        report(cat)
    print(f"\n[ok] catalogue -> {args.out}  ({cat['node_count']} nodes, {cat['edge_count']} edges)")

if __name__ == "__main__":
    main()
