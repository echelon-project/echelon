#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
extract.py — DETERMINISTIC surface-structure extractor (ChainBoard analysis layer, P0).

SURFACE (page) -> MODULE (feature container, e.g. a <section id="tab-*"> panel) ->
NODE (component = a DOM id / input / button / table inside the module).

Everything MEASURED from code, no LLM. FIXES over the boardchain_extract.py prototype (per gated
fan-out findings 2026-07-22):
  (a) UNIVERSAL function detection — named `function f(`, arrow `const f = () =>`, method shorthand
      `f() {`, and IIFEs — not just named declarations (prototype missed 175/255 forms in supplier.js).
  (b) BRACE-MATCHED spans — real LOC per fn (no next-fn-line double-count); non-function top-level LOC
      is ACCOUNTED separately so module LOC never silently under-counts.
  (c) BETTER attribution — resolves one level of string/variable id-building; only truly-shared fns fall
      to the `shared` bucket; size-bias is dampened (normalize by module id-count).
  (d) `attribution_confidence` per surface (% strong-id-match vs name-fallback vs shared-dumped).
  (e) MODULE-LEVEL roles (deterministic, from structure) — NODE stays `kind` (UI presence), never a code role.

Roles are a MODULE axis, not a NODE axis: a DOM id is a UI element, not a "service". A flat page's
module role is inferred from the aggregate behaviour of its attributed fns (fetch/event/render mix).

Usage:
  python -m echelon_engine.pagemodel.extract --slug supplier --html <p> --js <p> --out <manifest.json>
"""
from __future__ import annotations
import argparse, json, re, sys, pathlib

def read(p): return pathlib.Path(p).read_text(encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────────────────────
# HTML → modules (sections) + nodes (dom ids / inputs / buttons / tables)
# ─────────────────────────────────────────────────────────────────────────────
TABBTN_RE  = re.compile(r'<button\s+data-tab="([a-z0-9-]+)"[^>]*>([^<]+)</button>', re.I)
ATOM_ID_RE = re.compile(r'id="([a-z0-9][a-z0-9-]*)"', re.I)
INPUT_RE   = re.compile(r'<(input|select|textarea)\b[^>]*', re.I)
BUTTON_RE  = re.compile(r'<button\b[^>]*>([^<]*)</button>', re.I)
TABLE_HINT = re.compile(r'(tabulator|<table|class="tbl"|id="[a-z-]*(list|grid|rows|polist)")', re.I)
HEADING_RE = re.compile(r'<h[1-4][^>]*>\s*([^<]{2,40})', re.I)

# Module-container DISCOVERY: instead of hardcoding one convention, find the repeating container
# pattern this page actually uses, in priority order. Each returns [(module_id, label, html)].
_MODULE_STRATEGIES = [
    # 1. tab-sections (supplier/ledger): <section id="tab-*"> + data-tab labels
    ("tab", re.compile(r'<section\s+id="(tab-[a-z0-9-]+)"[^>]*>', re.I)),
    # 2. panel/surface sections: id="*-panel" / "*-surface" on section OR div
    ("panel", re.compile(r'<(?:section|div)\s+id="([a-z0-9-]*(?:panel|surface)[a-z0-9-]*)"[^>]*>', re.I)),
    # 3. any <section id="..."> (generic sectioned page)
    ("section", re.compile(r'<section\s+id="([a-z0-9][a-z0-9-]*)"[^>]*>', re.I)),
]

def _slugify(t):
    return re.sub(r'[^a-z0-9]+', '-', (t or '').strip().lower()).strip('-')[:24] or "mod"

def html_modules(html):
    """DISCOVER the module-container pattern this page uses; return [(module_id, label, html)].

    Tries tab → panel/surface → generic section. If a strategy finds ≥1 container, use it.
    Module id is derived from the existing container id (tab-* stripped); label from data-tab
    text or a nearby heading. If NOTHING matches, the whole page is one implicit 'main' module.
    """
    tab_labels = {m.group(1): m.group(2).strip() for m in TABBTN_RE.finditer(html)}
    for kind, rx in _MODULE_STRATEGIES:
        marks = [(m.start(), m.group(1)) for m in rx.finditer(html)]
        if not marks:
            continue
        mods = []
        for i, (pos, cid) in enumerate(marks):
            end = marks[i+1][0] if i+1 < len(marks) else len(html)
            body = html[pos:end]
            key = cid.replace("tab-", "")
            # label: data-tab text, else the container's first heading, else the id
            label = tab_labels.get(key)
            if not label:
                h = HEADING_RE.search(body)
                label = h.group(1).strip() if h else key
            # auto-generate a stable module id if the container id is generic/noise
            mod_id = key if re.search(r'[a-z]', key) else _slugify(label)
            mods.append((mod_id, label, body))
        return mods
    # fallback: one implicit module holding the whole page
    return [("main", "main", html)]

def nodes_in(sec_html):
    ids = ATOM_ID_RE.findall(sec_html)
    inputs = len(INPUT_RE.findall(sec_html))
    buttons = [b.strip() for b in BUTTON_RE.findall(sec_html) if b.strip()]
    tables = len(TABLE_HINT.findall(sec_html))
    return {"dom_ids": ids, "inputs": inputs, "buttons": buttons, "tables": tables}

# ─────────────────────────────────────────────────────────────────────────────
# JS → functions (universal detection + brace-matched spans)
# ─────────────────────────────────────────────────────────────────────────────
# each pattern captures the function NAME in group 1 and the position of the opening brace region.
FN_PATTERNS = [
    re.compile(r'(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{', re.M),          # function f(){}
    re.compile(r'(?:^|\n)\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>\s*\{', re.M),  # const f = ()=>{}
    re.compile(r'(?:^|\n)\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?[A-Za-z_$][\w$]*\s*=>\s*\{', re.M),  # const f = x=>{}
    re.compile(r'(?:^|\n)\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{', re.M),                     # method shorthand f(){}
]
# keywords that look like method-shorthand but are control flow — never functions.
_KW = {"if", "for", "while", "switch", "catch", "function", "return", "else", "do", "with"}

def _match_brace_span(js, open_idx):
    """From the index of a '{', return the index just past its matching '}'. Skips strings/comments crudely."""
    depth, i, n = 0, open_idx, len(js)
    in_str = None
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

def js_functions(js):
    """[{name,line,loc,own_loc,start,end}] — universal detection, brace-matched spans, dedup by start.

    `loc` = full brace-span line count (may nest). `own_loc` = lines NOT inside any nested detected fn
    (each line attributed to its innermost fn), so summing own_loc across fns never exceeds the file and
    non-function top-level lines are what's left over. This is the fix for the span-overlap double-count.
    """
    seen = {}
    for pat in FN_PATTERNS:
        for m in pat.finditer(js):
            name = m.group(1)
            if name in _KW:
                continue
            brace = js.find("{", m.end() - 1)
            if brace < 0:
                continue
            start = m.start(1)
            if start in seen:
                continue
            end = _match_brace_span(js, brace)
            seen[start] = {"name": name, "line": js[:start].count("\n") + 1,
                           "loc": js[brace:end].count("\n") + 1, "start": brace, "end": end}
    fns = [seen[k] for k in sorted(seen)]
    # own_loc: attribute each source LINE to the INNERMOST fn span covering it.
    line_owner = {}   # line_idx -> (span_width, fn_index)  smallest span wins
    for idx, f in enumerate(fns):
        w = f["end"] - f["start"]
        l0, l1 = js[:f["start"]].count("\n"), js[:f["end"]].count("\n")
        for ln in range(l0, l1 + 1):
            cur = line_owner.get(ln)
            if cur is None or w < cur[0]:
                line_owner[ln] = (w, idx)
    own = [0] * len(fns)
    for _ln, (_w, idx) in line_owner.items():
        own[idx] += 1
    for idx, f in enumerate(fns):
        f["own_loc"] = own[idx]
    return fns

FETCH_RE = re.compile(r"""(?:fetch\(|(?<![\w.])get\(|(?<![\w.])post\(|(?<![\w.])del\(|(?<![\w.])put\()\s*[`'"]([^`'"]+)""")
# Broad fallback: ANY string literal beginning "/api/..." — catches endpoints whose
# URL is BUILT (a ternary value, a var assigned then fetched, string-concatenation)
# rather than passed inline to fetch()/get(). FETCH_RE alone false-negatives those,
# and a missing caller means a fix can't be safely gated (blast radius under-counted).
# Endpoint identity later normalizes the query/id tail, so a bare "/api/x?..." prefix
# resolves to the same node as an inline get("/api/x").
API_STR_RE = re.compile(r"""[`'"](/api/[A-Za-z0-9_./{}$-]+)""")
EVENT_RE = re.compile(r"""addEventListener\(\s*['"](\w+)['"]|\.on\(\s*['"](\w+)['"]|onclick""")
WRITE_RE = re.compile(r"""\.innerHTML\s*=|\.textContent\s*=|insertAdjacentHTML|\.appendChild|\.replaceChildren""")

def fetches_in(body):   return sorted(set(FETCH_RE.findall(body)))
def api_strings_in(body): return sorted(set(API_STR_RE.findall(body)))
def has_event(body):    return bool(EVENT_RE.search(body))
def has_write(body):    return bool(WRITE_RE.search(body))

# Response-field usage — the returned-vs-used dimension. Given a JS body that fetches an
# endpoint, which fields of the RESPONSE does it actually read? We can't do full dataflow
# on dynamic JS, so we use a conservative heuristic that ERRS TOWARD "used" (never toward
# "safe to drop"): a fix gate must not be tricked into removing a field a caller reads.
_PROP_RE = re.compile(r"""(?<![\w$])([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]+)""")
_DESTRUCT_RE = re.compile(r"""(?:const|let|var)\s*\{([^}]{1,300})\}\s*=""")
# variables that conventionally hold a response or a row of one (over-collect from these)
_RESP_HINTS = {"d", "r", "res", "resp", "row", "it", "item", "rec", "data", "j", "json", "x", "v", "e"}

def response_fields_read(body, *, resp_vars=None):
    """Best-effort set of response FIELD NAMES the body reads via `VAR.field` (VAR a likely
    response/row holder) + destructuring. USED for a rough listing; for the load-bearing
    "is field X safe to drop?" question use field_is_referenced() instead — it checks a
    SPECIFIC field by name anywhere in the body (nested-access-safe), which is the
    conservative test a drop gate needs."""
    hints = set(_RESP_HINTS) | set(resp_vars or ())
    fields = set()
    for var, prop in _PROP_RE.findall(body):
        if var in hints:
            fields.add(prop)
    for grp in _DESTRUCT_RE.findall(body):
        for tok in grp.split(","):
            name = tok.split(":")[0].split("=")[0].strip().strip(".")
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                fields.add(name)
    _NOISE = {"map", "forEach", "filter", "then", "catch", "length", "push", "join",
              "toLocaleString", "toFixed", "split", "trim", "value", "dataset",
              "querySelector", "addEventListener", "style", "classList", "reduce"}
    return sorted(fields - _NOISE)

def field_is_referenced(body, field):
    """CONSERVATIVE test: is `field` read ANYWHERE in the body? True if it appears as a
    property access `.field`, a bracket key `['field']`/`["field"]`, or a destructured name.
    This deliberately does NOT try to prove the access is on THIS endpoint's response —
    because a false "not referenced" would green-light dropping a live field. Nested access
    (`cmd.traffic.ctr`) still contains `.traffic`, so a top-level field survives even when
    reached through an intermediate binding. Only a field whose name appears NOWHERE is a
    safe drop candidate."""
    f = re.escape(field)
    # .field (not part of a longer identifier)   |  ['field'] / ["field"]  | {…field…}
    pat = re.compile(r"""\.""" + f + r"""(?![\w$])"""
                     r"""|\[\s*['"]""" + f + r"""['"]\s*\]"""
                     r"""|(?:const|let|var)\s*\{[^}]*(?<![\w$])""" + f + r"""(?![\w$])[^}]*\}""")
    return bool(pat.search(body))

def response_bindings(body, endpoint):
    """Best-effort: the variable name(s) the endpoint's response is bound to, so
    response_fields_read can be scoped. Matches `X = await get('…/ep')`, `.then(X=>`,
    `const X = (await …).data`. Returns a set (may be empty → caller falls back to hints)."""
    vs = set()
    esc = re.escape(endpoint.split("?")[0])
    for m in re.finditer(r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?[^;]*?"""
                         + esc, body):
        vs.add(m.group(1))
    for m in re.finditer(r"""\.then\(\s*(?:async\s*)?\(?\s*([A-Za-z_$][\w$]*)""", body):
        vs.add(m.group(1))
    return vs

# ─────────────────────────────────────────────────────────────────────────────
# Attribution: fn → module by DOM-id references in its body (size-normalized),
# resolving one level of string/variable id fragments; confidence tracked.
# ─────────────────────────────────────────────────────────────────────────────
def _body_of(js, fn): return js[fn["start"]:fn["end"]]

def attribute(fns, js, modules_ids):
    """Return (assign{key:[fn]}, shared[fn], stats). Each fn tagged fn['_attr'] = strong|name|shared."""
    assign = {k: [] for k in modules_ids}
    shared = []
    strong = name_only = dumped = 0
    for f in fns:
        body = _body_of(js, f).lower()
        nm = f["name"].lower()
        best, score, kind = None, 0.0, None
        for key, ids in modules_ids.items():
            if not ids:
                continue
            hits = 0
            for i in ids:
                il = i.lower()
                hits += body.count("#"+il) + body.count("'"+il+"'") + body.count('"'+il+'"')
                hits += body.count("getelementbyid('"+il+"'") + body.count("`"+il)
            # size-normalize: divide raw hits by sqrt(module id-count) to damp big-module bias
            norm = hits / (len(ids) ** 0.5) if ids else 0.0
            if norm > score:
                best, score, kind = key, norm, "strong"
        # name-convention fallback ONLY if no id evidence
        if best is None or score == 0.0:
            for key in modules_ids:
                if key[:4] and key[:4] in nm:
                    best, kind = key, "name"; break
        if best is not None and kind:
            f["_attr"] = kind
            assign[best].append(f)
            if kind == "strong": strong += 1
            else: name_only += 1
        else:
            f["_attr"] = "shared"
            shared.append(f); dumped += 1
    total = max(1, len(fns))
    attributed = strong + name_only
    stats = {
        "strong": strong, "name_fallback": name_only, "shared_dumped": dumped,
        # COVERAGE: % of fns placed in SOME module (vs dumped to shared). Meaningful for every page.
        # This is the gate metric — "did we place the code".
        "coverage": round(100 * attributed / total, 1),
        # DISAMBIGUATION: of the ATTRIBUTED fns, % placed by hard id-evidence (not name-fallback).
        # Only meaningful when a page has >1 module (nothing to disambiguate on a 1-module page).
        "disambiguation": round(100 * strong / max(1, attributed), 1),
    }
    return assign, shared, stats

# ─────────────────────────────────────────────────────────────────────────────
# Module-level ROLE inference (deterministic, from aggregate fn behaviour).
# A module's role = the dominant layer its fns exhibit. NODE never carries a code role.
# ─────────────────────────────────────────────────────────────────────────────
def module_role(mfns, js):
    fetch = event = write = 0
    for f in mfns:
        b = _body_of(js, f)
        if fetches_in(b): fetch += 1
        if has_event(b):  event += 1
        if has_write(b):  write += 1
    # a flat module usually mixes all three; report the mix + the dominant label.
    mix = {"data": fetch, "control": event, "render": write}
    dom = max(mix, key=mix.get) if any(mix.values()) else "static"
    # map behaviour-dominance to the standard role vocabulary (module altitude)
    label = {"data": "service", "control": "controller", "render": "component", "static": "view"}[dom]
    return {"role": label, "signals": mix}

def node_kind(did):
    return "table" if re.search(r'(list|grid|rows|polist|panel)', did) else "region"

# ─────────────────────────────────────────────────────────────────────────────
def build(slug, html, js):
    # STATIC-HTML mode: no page-<slug>.js sibling (js is None). The behaviour lives on the server
    # or in a shared bundle, so endpoints are read from the HTML itself. We feed js="" to the JS
    # machinery (0 fns, harmless — same code path, no branch in the hot loop) and inject the
    # HTML-derived endpoints below so they still become GLOBAL boundary nodes downstream.
    from echelon_engine.pagemodel import static_html as sh
    static_mode = js is None
    if static_mode:
        js = ""
        html_endpoints = sh.endpoints_from_html(html)
    total_loc = js.count("\n") + 1
    fns = js_functions(js)
    mods_html = html_modules(html)
    mod_nodes = {mid: nodes_in(sec) for mid, _, sec in mods_html}
    modules_ids = {k: set(v["dom_ids"]) for k, v in mod_nodes.items()}
    # CHROME ids: ids present in the page HTML but OUTSIDE every discovered module section — they sit
    # ABOVE the module layer (nav/tabbar/shell chrome), so they're structural, not module-owned.
    # Position is the discriminator: chrome does the reaching, it isn't a thing a module contains.
    all_ids = set(ATOM_ID_RE.findall(html))
    in_module = set().union(*modules_ids.values()) if modules_ids else set()
    chrome_ids = sorted(all_ids - in_module)
    assign, shared, stats = attribute(fns, js, modules_ids)

    modules = []
    for mid, label, sec in mods_html:
        key = mid
        nd = mod_nodes[key]
        mfns = assign.get(key, [])
        role = module_role(mfns, js)
        mod_fetches = sorted(set(sum((fetches_in(_body_of(js, f)) for f in mfns), [])))
        modules.append({
            "module": key, "label": label,
            "role": role["role"], "role_signals": role["signals"],
            "loc_estimate": sum(f["own_loc"] for f in mfns),
            "fn_count": len(mfns),
            "node_count": len(nd["dom_ids"]),
            "inputs": nd["inputs"], "buttons": len(nd["buttons"]), "tables": nd["tables"],
            "data_fetch": mod_fetches, "fetch_estimate": len(mod_fetches),
            "nodes": [{"id": d, "kind": node_kind(d)} for d in nd["dom_ids"]],
            "fns": [{"name": f["name"], "loc": f["own_loc"], "line": f["line"], "attr": f["_attr"]} for f in mfns],
        })

    attributed_loc = sum(m["loc_estimate"] for m in modules)
    shared_loc = sum(f["own_loc"] for f in shared)
    # non-function / top-level LOC = everything not inside a detected fn span (IIFE wrapper, decls, listeners)
    covered = attributed_loc + shared_loc
    nonfn_loc = max(0, total_loc - covered)

    # Complete caller set = inline fetch()/get() calls UNION any built "/api/..." string literal.
    # The union is what makes endpoint fan-in trustworthy for blast-radius gating — a dynamic-URL
    # caller is still a caller. In STATIC-HTML mode the JS set is empty; the HTML-derived endpoints
    # (form action / data-access route keys / path literals / nav href) take its place.
    all_endpoints = sorted(set(FETCH_RE.findall(js)) | set(API_STR_RE.findall(js)))
    if static_mode:
        all_endpoints = sorted(set(all_endpoints) | set(html_endpoints))

    return {
        "surface": slug,
        "loc_total": total_loc,
        "loc_attributed": attributed_loc,
        "loc_shared": shared_loc,
        "loc_nonfunction": nonfn_loc,           # accounted, not silently dropped
        "fn_total": len(fns),
        "module_count": len(modules),
        "endpoint_total": len(all_endpoints) if static_mode else len(sorted(set(FETCH_RE.findall(js)))),
        "attribution": stats,                    # incl. attribution_confidence
        "modules": modules,
        "shared": {"fn_count": len(shared), "loc": shared_loc,
                   "fns": [{"name": f["name"], "loc": f["own_loc"], "line": f["line"]} for f in shared]},
        "all_endpoints": all_endpoints,
        "chrome_ids": chrome_ids,   # ids above the module layer — structural, not dead-check eligible
        "source": "static-html" if static_mode else "page-js",
    }

def report(mf):
    a0 = mf['attribution']
    dis = f"{a0['disambiguation']}%" if mf['module_count'] > 1 else "n/a(1-mod)"
    print(f"\n{'='*70}\nSURFACE :: {mf['surface']}   coverage={a0['coverage']}%  disambiguation={dis}\n{'='*70}")
    print(f"loc: total={mf['loc_total']} attributed={mf['loc_attributed']} "
          f"shared={mf['loc_shared']} nonfn={mf['loc_nonfunction']}   "
          f"fns={mf['fn_total']}  modules={mf['module_count']}  endpoints={mf['endpoint_total']}")
    a = mf['attribution']
    print(f"attribution: strong={a['strong']} name-fallback={a['name_fallback']} shared-dumped={a['shared_dumped']}")
    print(f"\n{'MODULE':<14}{'ROLE':<12}{'LOC':>6}{'FNS':>5}{'NODES':>7}{'IN':>4}{'BTN':>5}{'TBL':>5}{'FETCH':>7}")
    print("-"*70)
    for m in sorted(mf["modules"], key=lambda x: -x["loc_estimate"]):
        print(f"{m['module']:<14}{m['role']:<12}{m['loc_estimate']:>6}{m['fn_count']:>5}{m['node_count']:>7}"
              f"{m['inputs']:>4}{m['buttons']:>5}{m['tables']:>5}{m['fetch_estimate']:>7}")
    print(f"{'(shared)':<14}{'':<12}{mf['shared']['loc']:>6}{mf['shared']['fn_count']:>5}")
    tot = max(1, mf['loc_total'])
    print(f"\nshared = {round(100*mf['loc_shared']/tot,1)}% of board   |   "
          f"nonfn = {round(100*mf['loc_nonfunction']/tot,1)}%")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--html", required=True)
    ap.add_argument("--js", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    mf = build(args.slug, read(args.html), read(args.js))
    pathlib.Path(args.out).write_text(json.dumps(mf, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.quiet:
        report(mf)
    print(f"\n[ok] manifest -> {args.out}")

if __name__ == "__main__":
    main()
