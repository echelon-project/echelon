#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
spa.py — SINGLE-BUNDLE SPA mode (index.html + one monolithic app.js, hash-router).

The gamma-support SPAs (bee-accounting, domain-command, menu-scratch) are neither the AlphaApp page.js OS
layout (per-page assets/page-<slug>.js) NOR a static mirror (per-page *.html). They are ONE
index.html + ONE app.js (bee: 160KB / 3405 lines) with a client hash-router
(location.hash / hashchange) and ALL endpoints expressed as fetch()/api()/axios/$.ajax call-strings
inside the bundle.

Under the page.js/mirror resolvers this shape returns ~0 useful surfaces (74 nodes / 1 endpoint on
bee) because there is no per-page module to pair. This module reads the SPA correctly:

  SURFACES  = the hash-router ROUTES (a PAGES/route table, data-page nav attrs, location.hash cases).
  ENDPOINTS = every fetch()/api()/axios/XHR/$.ajax call-string in the bundle → GLOBAL boundary nodes.
  NODES     = the DOM ids on each route's <section id="page-<route>"> panel (per-route components).

The output is the SAME graph shape build_graph/identity/catalogue expect (surface/module/node/
endpoint nodes; contains/feeds_from/writes_to edges), so identity ids, endpoint fan-in, and the
cross-page catalogue merge all work unchanged. Deterministic regex over the bundle — no LLM.
"""
from __future__ import annotations
import re, pathlib
from echelon_engine.pagemodel import identity as idy

# ── SPA-shape detection ──────────────────────────────────────────────────────
_APP_JS_NAMES = ("app.js", "main.js", "bundle.js", "index.js")


def detect_spa(dir_path) -> tuple | None:
    """If `dir_path` is a single-bundle SPA (index.html + a monolithic app.js and NO per-page
    assets/page-<slug>.js), return (index_html_path, app_js_path). Else None.

    Guard: if per-page page-<slug>.js modules exist, this is the page.js OS layout — NOT a SPA;
    return None so that resolver keeps ownership (page.js path byte-unchanged)."""
    d = pathlib.Path(dir_path)
    if not d.is_dir():
        return None
    index = d / "index.html"
    if not index.exists():
        return None
    # per-page modules present → page.js layout, defer to the page.js resolver
    if list((d / "assets").glob("page-*.js")) if (d / "assets").is_dir() else []:
        return None
    for name in _APP_JS_NAMES:
        app = d / name
        if app.exists():
            return (index, app)
    return None


# ── route extraction (surfaces) ──────────────────────────────────────────────
# 1. a PAGES/ROUTES object-literal table: `const PAGES = { beranda: {...}, rekon: {...}, ... }`
_ROUTE_TABLE_RE = re.compile(
    r"""(?:const|let|var)\s+(?:PAGES|ROUTES|VIEWS|routes|pages)\s*=\s*\{""", re.I)
# a key line inside such a table:  `  beranda:  { ... }`
_ROUTE_KEY_RE = re.compile(r"""(?m)^\s*([A-Za-z_$][\w$-]*)\s*:\s*\{""")
# 2. data-page="..." nav attributes in the HTML
_DATA_PAGE_RE = re.compile(r"""data-(?:page|route|view|nav)=["']([A-Za-z0-9_-]+)["']""")
# 3. explicit location.hash string cases:  case "/rekon":  /  hash === "#/rekon"
_HASH_CASE_RE = re.compile(r"""(?:case\s+|===\s*|==\s*)["']#?/?([A-Za-z][\w-]*)["']""")


def _brace_span(js, open_idx):
    depth, i, n, in_str = 0, open_idx, len(js), None
    while i < n:
        c = js[i]
        if in_str:
            if c == "\\": i += 2; continue
            if c == in_str: in_str = None
        elif c in "\"'`":
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def routes_from(html: str, js: str) -> list[str]:
    """The hash-router routes = SURFACES. Union of the route-table keys, data-page nav attrs, and
    explicit hash cases. Route-table keys are the authority; the others catch tables built
    differently. De-duplicated, order-stable (table order first, then any extras)."""
    routes: list[str] = []
    seen = set()

    def add(r):
        r = (r or "").strip().strip("/")
        if r and r not in seen and re.fullmatch(r"[A-Za-z][\w-]*", r):
            seen.add(r); routes.append(r)

    # 1. route table — scope key extraction to the table's brace span (avoid matching every object)
    m = _ROUTE_TABLE_RE.search(js)
    if m:
        brace = js.find("{", m.end() - 1)
        if brace >= 0:
            body = js[brace:_brace_span(js, brace)]
            # only top-level keys of the table (depth-1 keys); a cheap pass: keys at line start
            for km in _ROUTE_KEY_RE.finditer(body):
                add(km.group(1))
    # 2. data-page nav attributes
    for r in _DATA_PAGE_RE.findall(html):
        add(r)
    # 3. explicit hash cases (only if we still found little — keeps noise down)
    if len(routes) < 2:
        for r in _HASH_CASE_RE.findall(js):
            add(r)
    return routes


# ── endpoint extraction (boundary nodes) ─────────────────────────────────────
# call-string forms: api("/x"), fetch("/x"), axios.get("/x"), $.ajax({url:"/x"}), .get("/x"),
# .post("/x"), http("/x"). Capture the leading path literal.
_CALL_RE = re.compile(
    r"""(?:(?<![\w.])(?:api|fetch|http|request|axios(?:\.\w+)?|xhr)|\$\.(?:ajax|get|post|getJSON)"""
    r"""|(?<![\w.])(?:get|post|put|del|patch))\(\s*[`'"]([^`'"]+)""")
# url:"/x" config style ($.ajax / axios config objects)
_URL_CFG_RE = re.compile(r"""\burl\s*:\s*[`'"](/[^`'"]+)""")
# broad fallback: any quoted "/word/..." app path (not a static asset)
_PATH_LIT_RE = re.compile(r"""[`'"](/[A-Za-z][A-Za-z0-9_./{}$:-]*)['"`]""")
_ASSET_RE = re.compile(r"""\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map|mp4|webp)(?:$|[?#])""", re.I)
_ASSET_DIR_RE = re.compile(r"""(?:^|/)(?:assets?|static|vendor|img|images|fonts?)/""", re.I)
_WRITE_VERB_RE = re.compile(r"""(?<![\w.])(?:post|put|del|patch)\(""", re.I)


def _is_asset(u: str) -> bool:
    return bool(_ASSET_RE.search(u) or _ASSET_DIR_RE.search(u))


def endpoints_from(js: str) -> list[str]:
    """All endpoint call-strings in the bundle → endpoint list (verbatim; identity.py normalizes
    numeric/query tails). Union of explicit call forms + url: config + any '/…' app-path literal.
    Broad on purpose — a missed caller under-counts fan-in, which is how a fix ships a break."""
    eps: set[str] = set()
    for u in _CALL_RE.findall(js):
        if u.startswith("/") and not _is_asset(u):
            eps.add(u)
    for u in _URL_CFG_RE.findall(js):
        if not _is_asset(u):
            eps.add(u)
    for u in _PATH_LIT_RE.findall(js):
        if not _is_asset(u):
            eps.add(u)
    return sorted(eps)


def _endpoint_rel(js: str, ep: str) -> str:
    """write (post/put/del/patch near the literal) vs read (feeds_from) — same heuristic as graph.py."""
    idx = js.find(ep)
    if idx < 0:
        return "feeds_from"
    return "writes_to" if _WRITE_VERB_RE.search(js[max(0, idx - 40):idx]) else "feeds_from"


# ── per-route DOM nodes ──────────────────────────────────────────────────────
_SECTION_RE_TMPL = r"""<(?:section|div)\s+id=["']page-{r}["'][^>]*>"""
_ATOM_ID_RE = re.compile(r'id="([a-z0-9][a-z0-9-]*)"', re.I)


def _route_section(html: str, route: str) -> str:
    """The HTML of the <section id="page-<route>"> panel for this route, if present (SPA convention:
    each route is a hidden .page section toggled active). Empty string if not found."""
    m = re.search(_SECTION_RE_TMPL.format(r=re.escape(route)), html, re.I)
    if not m:
        return ""
    start = m.start()
    # crude: to the next `id="page-` sibling or end
    nxt = re.search(r"""<(?:section|div)\s+id=["']page-[A-Za-z0-9_-]+["']""", html[m.end():], re.I)
    end = m.end() + nxt.start() if nxt else len(html)
    return html[start:end]


# ── build the SPA graph (surface/module/node/endpoint, catalogue-ready) ───────
def build_spa_graph(slug_hint: str, html: str, js: str) -> list[dict]:
    """Return a list of per-route GRAPHS (same shape graph.build_graph produces), one per hash-route.
    Each route is a SURFACE named '<slug_hint>#<route>' (or just the route), owning the DOM nodes on
    its page-section, and every bundle endpoint is attached as a GLOBAL boundary node (surface-level,
    honest attribution 'spa-bundle') so cross-route endpoint fan-in works. identity.assign_ids is
    applied per-route so endpoint ids are global and merge in the catalogue."""
    routes = routes_from(html, js)
    endpoints = endpoints_from(js)
    if not routes:
        routes = [slug_hint or "app"]   # a router-less bundle is still one surface

    graphs = []
    for route in routes:
        surface = route  # route name IS the surface slug (global-friendly, matches legacy page slugs)
        sid = f"surface:{surface}"
        nodes = [{"id": sid, "type": "surface", "label": surface, "loc": js.count("\n") + 1,
                  "role": "route", "spa": True}]
        edges = []
        # one implicit module per route (the route panel); DOM nodes hang off it
        mid = f"module:{surface}:main"
        nodes.append({"id": mid, "type": "module", "label": route, "module": "main",
                      "role": "view", "loc": 0, "fn_count": 0})
        edges.append({"from": sid, "to": mid, "rel": "contains", "label": "", "confidence": "certain"})
        for did in dict.fromkeys(_ATOM_ID_RE.findall(_route_section(html, route))):
            nid = f"node:{surface}:{did}"
            nodes.append({"id": nid, "type": "node", "label": did,
                          "kind": "table" if re.search(r'(list|grid|rows|table)', did) else "region",
                          "spa": True})
            edges.append({"from": mid, "to": nid, "rel": "contains", "label": "", "confidence": "certain"})
        # every bundle endpoint → GLOBAL boundary node, attached at surface level (honest: we can't
        # per-route attribute without dataflow, and under-counting fan-in ships breaks).
        for ep in endpoints:
            path = idy._norm_endpoint(ep)
            eid = f"endpoint:{path}"
            nodes.append({"id": eid, "type": "endpoint", "label": path, "raw": [ep],
                          "attribution": "spa-bundle"})
            edges.append({"from": sid, "to": eid, "rel": _endpoint_rel(js, ep),
                          "label": "", "confidence": "surface"})
        # de-dup nodes
        seen = set(); nodes = [n for n in nodes if not (n["id"] in seen or seen.add(n["id"]))]
        g = {"surface": surface, "node_count": len(nodes), "edge_count": len(edges),
             "deterministic_pct": 100.0, "nodes": nodes, "edges": edges}
        idy.assign_ids(g)
        graphs.append(g)
    return graphs
