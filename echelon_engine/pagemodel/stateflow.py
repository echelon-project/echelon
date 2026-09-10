#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
stateflow.py — P3: derive ROLEFLOW paths (the logic/data path through a surface).

A ROLEFLOW is a ChainResult-SHAPED path {name, ok, sub_steps} rooted at an EVENT-BOUND entry node:
   bind(#po-search-go, click) → poSearch() → fetch /api/restock/catalog → write #po-results
Deterministic from JS: event bindings, the handler fn, fetches in the handler (transitively through
calls), and DOM writes (innerHTML/textContent target ids).

Reuses the ChainResult SHAPE only (not the AlphaApp class). Pure reader over extract.js_functions.

Usage:
  python -m echelon_engine.pagemodel.stateflow --js <page.js> --slug <slug> --out <flows.json>
"""
from __future__ import annotations
import argparse, json, re, pathlib
from echelon_engine.pagemodel import extract as ex

# event bindings — several idioms. group order normalized to (entry, event, handler) below.
BIND_RES = [
    # #id.onclick = fn   /   querySelector('#id').onclick = fn
    re.compile(r"""getElementById\(\s*['"]([\w-]+)['"]\s*\)\.(onclick)\s*=\s*(\w+)"""),
    re.compile(r"""querySelector\(\s*['"]#([\w-]+)['"]\s*\)\.(onclick)\s*=\s*(\w+)"""),
    # #id.addEventListener('evt', fn)
    re.compile(r"""getElementById\(\s*['"]([\w-]+)['"]\s*\)\.addEventListener\(\s*['"](\w+)['"]\s*,\s*(\w+)"""),
    re.compile(r"""querySelector\(\s*['"]#([\w-]+)['"]\s*\)\.addEventListener\(\s*['"](\w+)['"]\s*,\s*(\w+)"""),
    # data-act delegation:  [data-act="X"] ... .onclick = fn   (entry = act:X)
    re.compile(r"""\[data-act="([\w-]+)"\][^;]*?\.(onclick)\s*=\s*(?:\(\)\s*=>\s*)?(\w+)"""),
]
# variable-ref binding: `const btn = ...; if (btn) btn.onclick = fn` — resolve var → its id if declared with an id literal.
VARREF_RE = re.compile(r"""(\w+)\.(onclick)\s*=\s*(\w+)\b""")
VARDECL_RE = re.compile(r"""(?:const|let|var)\s+(\w+)\s*=\s*[^;\n]*?['"]#?([\w-]+)['"]""")
WRITE_TARGET = re.compile(r"""(?:getElementById\(\s*['"]([\w-]+)['"]\s*\)|['"]#([\w-]+)['"])\s*\)?\.(?:innerHTML|textContent)\s*=""")
SET_HELPER   = re.compile(r"""set\(\s*['"]([\w-]+)['"]""")  # set(id, txt) helper writes

def _fn_by_name(fns): return {f["name"]: f for f in fns}

def _fetches(body):  return ex.fetches_in(body)
def _writes(body):
    ids = set()
    for m in WRITE_TARGET.finditer(body):
        ids.add(m.group(1) or m.group(2))
    for m in SET_HELPER.finditer(body):
        ids.add(m.group(1))
    return sorted(i for i in ids if i)

_STRIP = re.compile(r"""(`(?:[^`\\]|\\.)*`|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/)""", re.S)
def _decomment(body):
    """Blank out strings + comments so a call match is a REAL invocation, not text in a template."""
    return _STRIP.sub(" ", body)

def _calls_in(body, names, self_name):
    code = _decomment(body)
    out = []
    for n in names:
        if n == self_name or len(n) < 3:   # skip 1-2 char names (ln, rp…) — too collision-prone
            continue
        if re.search(r'(?<![\w.])'+re.escape(n)+r'\s*\(', code):
            out.append(n)
    return out

def trace(js, fns, entry_fn, depth=2, max_fanout=12):
    """Walk entry_fn → its fetches, writes, and (transitively, shallow) called fns. ChainResult-shaped.

    depth=2 + string/comment-stripped call detection + fan-out cap keeps the path a real logic trace,
    not a whole-module explosion (a deep loose walk false-links half the file via helper names)."""
    by = _fn_by_name(fns); names = set(by)
    def walk(name, seen, d):
        f = by.get(name)
        if not f or d <= 0 or name in seen:
            return None
        seen = seen | {name}
        body = ex._body_of(js, f)
        sub = []
        for ep in _fetches(body):
            sub.append({"name": f"fetch {ep}", "ok": True, "kind": "fetch", "sub_steps": []})
        for w in _writes(body):
            sub.append({"name": f"write #{w}", "ok": True, "kind": "write", "sub_steps": []})
        for c in _calls_in(body, names, name)[:max_fanout]:
            child = walk(c, seen, d - 1)
            if child and child["sub_steps"]:   # only keep called fns that DO something (fetch/write/deeper)
                sub.append(child)
        return {"name": name, "ok": True, "kind": "call", "sub_steps": sub}
    return walk(entry_fn, set(), depth)

def build(slug, js):
    fns = ex.js_functions(js)
    names = {f["name"] for f in fns}
    # var → id map (best-effort: `const btn = getElementById('x')` → btn:x)
    var_id = {m.group(1): m.group(2) for m in VARDECL_RE.finditer(js)}
    flows = []
    binders = [(rx, m) for rx in BIND_RES for m in rx.finditer(js)]
    # variable-ref binders, resolved through var_id
    for m in VARREF_RE.finditer(js):
        var, evt, handler = m.group(1), "click", m.group(3)
        if var in var_id and handler in names:
            binders.append((None, (f"{var_id[var]}", evt, handler)))
    for rx, m in binders:
        if rx is None:
            dom_id, evt, handler = m
        else:
            dom_id, evt, handler = m.group(1), m.group(2).replace("on", "") or "click", m.group(3)
        if handler not in names:
            continue
        body = trace(js, fns, handler)
        if not body:
            continue
        flows.append({
            "entry": f"#{dom_id}", "event": evt, "handler": handler,
            "flow": {"name": f"{evt} #{dom_id}", "ok": True, "kind": "event", "sub_steps": [body]},
        })
    # de-dup by (entry,event,handler)
    seen = set(); uniq = []
    for fl in flows:
        k = (fl["entry"], fl["event"], fl["handler"])
        if k not in seen:
            seen.add(k); uniq.append(fl)
    return {"surface": slug, "flow_count": len(uniq), "flows": uniq}

def _render_line(step, indent=0):
    arrow = "  " * indent + ("• " if indent else "")
    yield f"{arrow}{step['name']}"
    for s in step.get("sub_steps", []):
        yield from _render_line(s, indent + 1)

def report(res):
    print(f"\n{'='*60}\nROLEFLOWS :: {res['surface']}   ({res['flow_count']} entry points)\n{'='*60}")
    for fl in res["flows"][:12]:
        for line in _render_line(fl["flow"]):
            print("  " + line)
        print()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--js", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    res = build(args.slug, ex.read(args.js))
    pathlib.Path(args.out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.quiet:
        report(res)
    print(f"[ok] {res['flow_count']} roleflows -> {args.out}")

if __name__ == "__main__":
    main()
