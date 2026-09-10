#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
catalogue_api.py — the ACCESS LAYER over a pagemodel catalogue (owner's read/write spec).

Turns the catalogue JSON from a file-you-browse into a thing-you-query. 7 reads + 3 writes.

THE LAW: no write ever mutates the derived graph — the graph is ONLY a function of source. Writes
(annotate/assert) persist to a SIDECAR keyed by stable id (portable, survives re-extraction because
ids are stable). Intent (WHY a node exists) is a LAYER OVER structure via annotate, never mixed in.

  from echelon_engine.pagemodel.catalogue_api import Catalogue
  c = Catalogue("catalogue.json", os_dir="…/os")   # os_dir enables source()
  c.get(id) · c.neighbors(id, "in"|"out", depth) · c.path(a,b) · c.stateflow(evt)
  c.query(type=…, module=…, min_loc=…, endpoint=…, unreferenced=True) · c.source(id) · c.gate(surface)
  c.field_usage(endpoint_id)                       # returned-vs-used: which fields callers read
  c.annotate(id, key, val) · c.assert_rule(spec) · c.diff(other_catalogue)

  # QUERY BUILDER — chainable composition (fix-analysis / patcher substrate):
  c.q(*seed_ids)  → Query
      .endpoints()/.type(t)/.surface(s)/.label_contains(x)/.filter(pred)/.fat(bytes)   # narrow
      .callers(depth)/.dependencies(depth)/.surfaces()                                 # hop
      .drop_candidates(returned_fields)   # returned − referenced-anywhere = safe drops
      .ids()/.nodes()/.rows()/.first()/.count()                                        # materialize
    e.g.  c.q().endpoints().fat(250_000).rows()
          c.q().endpoints().label_contains("/api/x").drop_candidates([...])["drop_candidates"]
"""
from __future__ import annotations
import json, pathlib, re
from collections import defaultdict, deque
from echelon_engine.pagemodel import extract as ex, catalogue as C


def _find_dominant_list(sample):
    """The biggest list-of-dicts anywhere in a response (the 'rows' of a payload),
    unwrapping the common {data:[{...}]} / {items:[...]} envelopes."""
    best = []
    def walk(o):
        nonlocal best
        if isinstance(o, list):
            if o and isinstance(o[0], dict) and len(o) > len(best):
                best = o
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
    walk(sample)
    return best


def _common_prefix(strs):
    """Longest common leading substring across strs (for prefix-join detection)."""
    if not strs:
        return ""
    s1, s2 = min(strs), max(strs)
    i = 0
    while i < len(s1) and i < len(s2) and s1[i] == s2[i]:
        i += 1
    return s1[:i]


class Catalogue:
    def __init__(self, cat_path, os_dir=None, sidecar=None):
        self.path = pathlib.Path(cat_path)
        self.cat = json.loads(self.path.read_text(encoding="utf-8"))
        self.os_dir = pathlib.Path(os_dir) if os_dir else None
        self.sidecar = pathlib.Path(sidecar) if sidecar else self.path.with_suffix(".annotations.json")
        self._nodes = {n["id"]: n for n in self.cat["nodes"]}
        self._out = defaultdict(list); self._in = defaultdict(list)
        for e in self.cat["edges"]:
            self._out[e["from"]].append(e); self._in[e["to"]].append(e)
        self._ann = json.loads(self.sidecar.read_text(encoding="utf-8")) if self.sidecar.exists() else {"annotations": {}, "asserts": []}

    # ── READS ────────────────────────────────────────────────────────────────
    def _module_display_fields(self, n):
        """For a MODULE node, return extra display fields: slug (stable key) + display (surface:slug).
        Slug is always the machine-stable module key; display is scannable across surfaces.
        Returns {} for non-module nodes."""
        if n.get("type") != "module":
            return {}
        slug = n.get("module", "")
        surfaces = n.get("surfaces") or []
        surface = surfaces[0] if surfaces else ""
        display = f"{surface}:{slug}" if surface and slug else slug or surface
        return {"slug": slug, "display": display}

    def get(self, node_id):
        """Full node record — the primitive. type/label/loc/module/surface + inbound & outbound edges.
        MODULE nodes gain `slug` (stable module key) and `display` ({surface}:{slug}) for scannable output."""
        n = self._nodes.get(node_id)
        if not n:
            return None
        return {**n, **self._module_display_fields(n),
                "inbound": [{"from": e["from"], "rel": e["rel"]} for e in self._in.get(node_id, [])],
                "outbound": [{"to": e["to"], "rel": e["rel"]} for e in self._out.get(node_id, [])],
                "annotations": self._ann["annotations"].get(node_id, {})}

    def neighbors(self, node_id, direction="out", depth=1):
        """inbound = blast radius (who depends on me), outbound = my dependencies. BFS to `depth`."""
        adj = self._in if direction == "in" else self._out
        key = (lambda e: e["from"]) if direction == "in" else (lambda e: e["to"])
        seen, frontier, out = set(), {node_id}, []
        for d in range(depth):
            nxt = set()
            for nid in frontier:
                for e in adj.get(nid, []):
                    t = key(e)
                    if t not in seen and t != node_id:
                        seen.add(t); nxt.add(t)
                        tn = self._nodes.get(t, {})
                        entry = {"id": t, "rel": e["rel"], "depth": d + 1,
                                 "label": tn.get("label", t)}
                        entry.update(self._module_display_fields(tn))
                        out.append(entry)
            frontier = nxt
        return out

    def path(self, from_id, to_id, max_depth=8):
        """Does `from` reach `to`? Return the edge chain or None. BFS over outbound edges."""
        q = deque([(from_id, [from_id])]); seen = {from_id}
        while q:
            cur, chain = q.popleft()
            if cur == to_id:
                return chain
            if len(chain) > max_depth:
                continue
            for e in self._out.get(cur, []):
                if e["to"] not in seen:
                    seen.add(e["to"]); q.append((e["to"], chain + [e["to"]]))
        return None

    def stateflow(self, event_or_surface):
        """The RoleFlow(s) for an event/surface — the one read not reconstructable from the graph.
        Accepts a surface slug (returns all its flows) — event-id granularity once flows carry stable ids."""
        if not self.os_dir:
            raise RuntimeError("stateflow needs os_dir")
        from echelon_engine.pagemodel import stateflow as sf
        slug = event_or_surface.replace("surface:", "")
        js = self.os_dir / "assets" / f"page-{slug}.js"
        if not js.exists():
            return None
        return sf.build(slug, ex.read(js))

    def query(self, type=None, module=None, surface=None, min_loc=None, endpoint=None,
              unreferenced=None, role=None):
        """Compose over the catalogue: filter nodes by facets. This is what makes it a catalogue."""
        dead_ids = {x["id"] for x in C.dead_nodes(self.cat)["unreferenced"]} if unreferenced else set()
        out = []
        for n in self.cat["nodes"]:
            if type and n["type"] != type: continue
            if module and n.get("module") != module: continue
            if surface and surface not in n.get("surfaces", []): continue
            if role and n.get("role") != role: continue
            if min_loc and n.get("loc", 0) < min_loc: continue
            if endpoint and not (n["type"] == "endpoint" and endpoint in n.get("label", "")): continue
            if unreferenced and n["id"] not in dead_ids: continue
            out.append(n)
        return out

    def source(self, node_id):
        """The ACTUAL CODE for one node — 40 lines not 3000. Without this the token-win premise leaks.
        module → its attributed fns' spans; node(component) → its HTML element; endpoint → call sites."""
        if not self.os_dir:
            raise RuntimeError("source needs os_dir")
        n = self._nodes.get(node_id)
        if not n:
            return None
        surf = (n.get("surfaces") or [None])[0]
        if not surf:
            return None
        html = ex.read(self.os_dir / f"{surf}.html")
        jsp = self.os_dir / "assets" / f"page-{surf}.js"
        js = ex.read(jsp) if jsp.exists() else ""
        if n["type"] == "module":
            # pull the attributed fns of this module from the fresh manifest, return their spans
            mf = ex.build(surf, html, js)
            mod = next((m for m in mf["modules"] if m["module"] == n.get("module")), None)
            if not mod: return {"surface": surf, "code": "", "note": "module not found"}
            fns = ex.js_functions(js); by = {f["name"]: f for f in fns}
            chunks = []
            for f in mod["fns"]:
                fo = by.get(f["name"])
                if fo:
                    chunks.append(js[fo["start"]:fo["end"]] if "start" in fo else "")
            return {"surface": surf, "module": n.get("module"),
                    "fns": [f["name"] for f in mod["fns"]], "loc": mod["loc_estimate"],
                    "code": "\n\n".join(c for c in chunks if c)[:8000]}
        if n["type"] == "node":
            did = n["label"]
            m = re.search(r'<[^>]*id="'+re.escape(did)+r'"[^>]*>', html, re.I)
            snippet = html[m.start():m.start()+400] if m else ""
            return {"surface": surf, "dom_id": did, "html": snippet}
        if n["type"] == "endpoint":
            sites = [ln for ln in js.splitlines() if any(r in ln for r in n.get("raw", [n["label"]]))]
            return {"endpoint": n["label"], "call_sites": sites[:20]}
        return {"surface": surf}

    def _caller_bodies(self, endpoint_id):
        """{caller_label: js_body} for every caller of an endpoint — the JS scoped to the
        calling module's fns where attributable, else the whole page JS. Shared by
        field_usage and the Query drop_candidates gate."""
        if not self.os_dir:
            raise RuntimeError("_caller_bodies needs os_dir")
        out = {}
        for e in self._in.get(endpoint_id, []):
            src = self._nodes.get(e["from"], {})
            if src.get("type") == "surface":
                surf = src.get("label")
            else:
                surf = (src.get("surfaces") or [None])[0]
            if not surf:
                continue
            jsp = self.os_dir / "assets" / f"page-{surf}.js"
            if not jsp.exists():
                continue
            js = ex.read(jsp)
            body = js
            if src.get("type") == "module":
                mf = ex.build(surf, ex.read(self.os_dir / f"{surf}.html"), js)
                mod = next((m for m in mf["modules"] if m["module"] == src.get("module")), None)
                if mod:
                    by = {f["name"]: f for f in ex.js_functions(js)}
                    parts = [js[by[f["name"]]["start"]:by[f["name"]]["end"]]
                             for f in mod["fns"] if f["name"] in by and "start" in by[f["name"]]]
                    if parts:
                        body = "\n".join(parts)
            label = f"{surf}:{src.get('module')}" if src.get("type") == "module" else surf
            out[label] = body
        return out

    def field_usage(self, endpoint_id):
        """RETURNED-VS-USED for an endpoint: per calling surface, which response fields the
        caller's code reads. The dimension that turns "is field X safe to drop?" from a
        hand-audit into a query. Conservative — over-collects reads (see extract.response_
        fields_read): a field it CANNOT prove is read is reported under `unresolved`, never
        as safe-to-drop. Requires os_dir (needs the JS source).

        Returns {endpoint, callers:[{surface, module, bound_vars, fields_read:[…]}],
                 fields_read_any:[…]}  — fields_read_any = union across all callers."""
        if not self.os_dir:
            raise RuntimeError("field_usage needs os_dir")
        n = self._nodes.get(endpoint_id)
        if not n or n.get("type") != "endpoint":
            return None
        raws = n.get("raw", [n["label"]])
        callers, any_fields = [], set()
        # every caller = an inbound edge from a module or surface
        for e in self._in.get(endpoint_id, []):
            src = self._nodes.get(e["from"], {})
            surfs = src.get("surfaces") or ([] if src.get("type") == "surface" else [])
            surf = (surfs or [src.get("label")])[0] if src.get("type") != "surface" else src.get("label")
            if not surf:
                continue
            jsp = self.os_dir / "assets" / f"page-{surf}.js"
            if not jsp.exists():
                continue
            js = ex.read(jsp)
            # scope to the calling module's fns when we can; else the whole page JS
            body = js
            if src.get("type") == "module":
                mf = ex.build(surf, ex.read(self.os_dir / f"{surf}.html"), js)
                mod = next((m for m in mf["modules"] if m["module"] == src.get("module")), None)
                if mod:
                    by = {f["name"]: f for f in ex.js_functions(js)}
                    body = "\n".join(js[by[f["name"]]["start"]:by[f["name"]]["end"]]
                                     for f in mod["fns"] if f["name"] in by and "start" in by[f["name"]])
            bound = set()
            for raw in raws:
                bound |= ex.response_bindings(body, raw)
            fields = ex.response_fields_read(body, resp_vars=bound)
            any_fields.update(fields)
            callers.append({"surface": surf,
                            "module": src.get("module") or src.get("type"),
                            "bound_vars": sorted(bound), "fields_read": fields})
        return {"endpoint": n["label"], "callers": callers,
                "fields_read_any": sorted(any_fields)}

    def ingest_bench(self, bench_rows, *, keys=("bytes", "rows", "warm_ms")):
        """Fold a perf-bench result onto endpoint NODES (annotate sidecar) so latency+volume
        live IN the graph and .fat()/.slow() query real data. bench_rows = iterable of dicts
        with an 'endpoint' path plus the metric keys. Endpoint paths are normalized the same
        way node labels are ({param} and /123 → /:id) so a bench '/api/suppliers/{id}' lands
        on the '/api/suppliers/:id' node. Returns {matched, unmatched:[paths]}."""
        from echelon_engine.pagemodel import identity as _idy
        # label -> node id, for endpoint nodes
        by_label = {n["label"]: n["id"] for n in self.cat["nodes"] if n.get("type") == "endpoint"}
        norm = _idy._norm_endpoint   # SAME normalization the graph labels use
        matched, unmatched = 0, []
        for r in bench_rows:
            ep = r.get("endpoint")
            if not ep:
                continue
            nid = by_label.get(norm(ep))
            if not nid:
                unmatched.append(ep); continue
            for k in keys:
                if r.get(k) is not None:
                    self.annotate(nid, k, r[k])   # persists via _flush per call
            matched += 1
        return {"matched": matched, "unmatched": unmatched}

    def payload_redundancy(self, sample, *, row_key=None, min_rows=20,
                           repeat_frac=0.5, prefix_frac=0.5):
        """Flag REDUNDANCY / DENORMALIZATION / DoS-amplification waste in a response —
        the class field_usage MISSES (a field can be READ yet still be the biggest waste
        because its VALUE is massively repeated across rows). Takes a `sample` = the actual
        JSON the endpoint returned; finds the dominant row list; per field measures:
          • repeat: bytes spent on DUPLICATE values (value appears on many rows) — a
            low-cardinality field (e.g. a per-variant `status='fifo'`) or a name repeated
            per child row. `dup_bytes` = total − distinct.
          • prefix-join: a string field where rows sharing a parent key carry an identical
            LEADING substring (the classic "server ships `<product> - <variant>` and the
            client splits it back") — the redundant prefix is `prefix_dup_bytes`, and it is
            an UNBOUNDED amplifier (prefix length × child count), a payload-DoS vector.
          • unbounded: max value length, so a field that scales with attacker-influenced
            input (a listing title) is visible.
        Returns {rows, total_bytes, fields:[{field, total_bytes, distinct, dup_bytes,
        prefix_dup_bytes, max_len, verdict}]} sorted worst-first. Deterministic, no LLM.
        Thresholds: a field is flagged when dup or prefix waste ≥ repeat_frac/prefix_frac of
        its own bytes. row_key names the parent-id field for prefix grouping (default: auto
        item_id/id)."""
        import json as _json, gzip as _gzip
        rows = _find_dominant_list(sample)
        if not rows or len(rows) < min_rows:
            return {"rows": len(rows or []), "total_bytes": None,
                    "fields": [], "note": f"need ≥{min_rows} rows to judge redundancy"}
        _raw = _json.dumps(sample, default=str, separators=(",", ":")).encode()
        total_bytes = len(_raw)
        # GZIP TRUTH: redundancy is what gzip compresses best, so raw-byte waste vastly
        # OVERSTATES the wire win of a de-dup/restructure. Report the gzipped size too — if
        # the endpoint is served gzipped (most are), THIS is the real transport cost and a
        # restructure typically saves ~0 on the wire (the waste is server-CPU + parse, not
        # bytes-on-wire). See gzip-makes-payload-redundancy-a-nonissue-on-the-wire-2026-07-24.
        gzip_bytes = len(_gzip.compress(_raw))
        rk = row_key or next((k for k in ("item_id", "id", "sku", "key")
                              if k in rows[0]), None)
        # collect per-field values
        by_field = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                by_field.setdefault(k, []).append((r.get(rk), v))
        out = []
        for f, pairs in by_field.items():
            vals = [v for _, v in pairs]
            svals = [v if isinstance(v, str) else _json.dumps(v, default=str) for v in vals]
            tot = sum(len(s.encode()) for s in svals)
            distinct_bytes = sum(len(s.encode()) for s in set(svals))
            dup_bytes = tot - distinct_bytes
            # prefix-join: group by parent key, find identical leading substring shared by
            # a parent's children; count the repeated prefix beyond the first occurrence.
            prefix_dup = 0
            if rk and all(isinstance(v, str) for v in vals):
                from collections import defaultdict as _dd
                groups = _dd(list)
                for pk, v in pairs:
                    groups[pk].append(v)
                for pk, gv in groups.items():
                    if len(gv) < 2:
                        continue
                    pfx = _common_prefix(gv)
                    if " - " in pfx or len(pfx) >= 8:   # a real shared prefix, not a coincidence
                        prefix_dup += len(pfx.encode()) * (len(gv) - 1)
            max_len = max((len(s) for s in svals), default=0)
            verdict = []
            if dup_bytes >= repeat_frac * tot and dup_bytes > 200:
                verdict.append("low-cardinality-repeat")
            if prefix_dup >= prefix_frac * tot and prefix_dup > 200:
                verdict.append("prefix-join-DENORMALIZE")
            if max_len > 80:
                verdict.append("unbounded-len(DoS-amplifier)")
            if verdict:
                out.append({"field": f, "total_bytes": tot, "distinct_bytes": distinct_bytes,
                            "dup_bytes": dup_bytes, "prefix_dup_bytes": prefix_dup,
                            "max_len": max_len, "pct_of_payload": round(100 * tot / total_bytes, 1),
                            "verdict": verdict})
        out.sort(key=lambda x: x["dup_bytes"] + x["prefix_dup_bytes"], reverse=True)
        return {"rows": len(rows), "total_bytes": total_bytes, "gzip_bytes": gzip_bytes,
                "row_key": rk, "fields": out,
                "wire_note": (f"raw {total_bytes}B → gzip {gzip_bytes}B "
                              f"({100 - 100 * gzip_bytes // total_bytes}% compressed). If served "
                              f"gzipped, a de-dup/restructure saves ~0 on the WIRE — the waste is "
                              f"server-CPU + browser-parse, not transport. Verify content-encoding "
                              f"+ ttfb before optimizing shape.")}

    def q(self, *seed_ids):
        """Open a chainable QUERY BUILDER. Compose traversals fluently instead of hand-
        writing graph walks — the fix-analysis / patcher substrate:

          c.q().endpoints().fat(250_000).ids()          # fat-payload endpoints
          c.q(ep_id).callers().surfaces()               # who calls this endpoint
          c.q(ep_id).unused_fields(returned=[…])         # returned-minus-read (drop candidates)

        Returns a Query bound to this catalogue. See the Query class."""
        return Query(self, list(seed_ids))

    def gate(self, surface):
        """Attribution confidence for a page — REQUIRED before trusting anything else about it."""
        if not self.os_dir:
            raise RuntimeError("gate needs os_dir")
        slug = surface.replace("surface:", "")
        html = self.os_dir / f"{slug}.html"; js = self.os_dir / "assets" / f"page-{slug}.js"
        if not (html.exists() and js.exists()):
            return None
        mf = ex.build(slug, ex.read(html), ex.read(js))
        a = mf["attribution"]
        trustworthy = mf["module_count"] <= 1 or a["disambiguation"] >= 80.0
        return {"surface": slug, "coverage": a["coverage"], "disambiguation": a["disambiguation"],
                "modules": mf["module_count"], "trustworthy": trustworthy}

    # ── WRITES (sidecar only — never touch the graph) ─────────────────────────
    def annotate(self, node_id, key, value):
        """Attach a finding to a node (accumulates across sessions). Sidecar, keyed by STABLE id."""
        if node_id not in self._nodes:
            raise KeyError(f"unknown node {node_id}")
        self._ann["annotations"].setdefault(node_id, {})[key] = value
        self._flush()
        return self._ann["annotations"][node_id]

    def assert_rule(self, spec):
        """Declare an invariant (dict, e.g. {kind:'no-call', module:'auth', endpoint:'/api/session'}).
        The scanner phase gates against these. Sidecar, not graph."""
        self._ann["asserts"].append(spec); self._flush()
        return spec

    def check_asserts(self):
        """Evaluate declared asserts against the current graph → violations list (the enforcing step)."""
        viol = []
        for r in self._ann["asserts"]:
            if r.get("kind") == "no-call":
                for e in self.cat["edges"]:
                    src, dst = self._nodes.get(e["from"], {}), self._nodes.get(e["to"], {})
                    if src.get("module") == r.get("module") and r.get("endpoint", "") in dst.get("label", ""):
                        viol.append({"rule": r, "edge": e})
            elif r.get("kind") == "max-crowding":
                for n in self.cat["nodes"]:
                    if n["type"] == "module" and n.get("loc", 0) > r.get("value", 1e9):
                        viol.append({"rule": r, "node": n["id"], "loc": n.get("loc")})
        return viol

    def diff(self, other):
        """Graph-level change vs another catalogue (makes writes safe). node/edge added/removed."""
        o = other.cat if isinstance(other, Catalogue) else json.loads(pathlib.Path(other).read_text(encoding="utf-8"))
        a_ids, b_ids = set(self._nodes), {n["id"] for n in o["nodes"]}
        ek = lambda e: (e["from"], e["to"], e["rel"])
        a_e, b_e = {ek(e) for e in self.cat["edges"]}, {ek(e) for e in o["edges"]}
        return {"nodes_added": sorted(a_ids - b_ids), "nodes_removed": sorted(b_ids - a_ids),
                "edges_added": len(a_e - b_e), "edges_removed": len(b_e - a_e)}

    def _flush(self):
        self.sidecar.write_text(json.dumps(self._ann, indent=2, ensure_ascii=False), encoding="utf-8")


class Query:
    """A chainable query over a Catalogue — the composable substrate for fix-analysis and
    the future patcher. Each step returns a NEW Query holding a working set of node ids, so
    chains read left-to-right and never mutate. Terminal steps (.ids/.nodes/.rows/.first)
    materialize; traversal steps (.callers/.endpoints/.filter/…) narrow or hop the set.

      c.q().endpoints().fat(250_000).rows()
      c.q(ep).callers().surfaces()
      c.q(ep).drop_candidates(returned_fields)   # returned − read, per the gate discipline
    """
    def __init__(self, cat: "Catalogue", ids=None):
        self.c = cat
        # None working-set = "all nodes" until first narrowing step
        self._ids = list(ids) if ids else None

    def _new(self, ids):
        return Query(self.c, ids)

    def _pool(self):
        return self._ids if self._ids is not None else [n["id"] for n in self.c.cat["nodes"]]

    # ── narrowing ─────────────────────────────────────────────────────────────
    def type(self, t):
        return self._new([i for i in self._pool() if self.c._nodes.get(i, {}).get("type") == t])

    def endpoints(self):
        return self.type("endpoint")

    def surface(self, slug):
        slug = slug.replace("surface:", "")
        return self._new([i for i in self._pool()
                          if slug in (self.c._nodes.get(i, {}).get("surfaces") or [])])

    def label_contains(self, sub):
        return self._new([i for i in self._pool()
                          if sub in self.c._nodes.get(i, {}).get("label", "")])

    def filter(self, pred):
        """Keep nodes where pred(node_record) is truthy — the escape hatch."""
        return self._new([i for i in self._pool() if pred(self.c._nodes.get(i, {}))])

    def _metric(self, node_id, key):
        return self.c._ann["annotations"].get(node_id, {}).get(key)

    def fat(self, min_bytes):
        """Endpoints whose measured payload ≥ min_bytes (from ingest_bench). Perf → graph."""
        return self._new([i for i in self._pool()
                          if (self._metric(i, "bytes") or 0) >= min_bytes])

    def slow(self, min_ms):
        """Endpoints whose measured warm latency ≥ min_ms (from ingest_bench)."""
        return self._new([i for i in self._pool()
                          if (self._metric(i, "warm_ms") or 0) >= min_ms])

    def sort_by(self, key, desc=True):
        """Order the working set by an annotated metric (bytes/warm_ms/rows)."""
        return self._new(sorted(self._pool(),
                                key=lambda i: self._metric(i, key) or 0, reverse=desc))

    # ── hopping (traversal) ───────────────────────────────────────────────────
    def callers(self, depth=1):
        """Hop each endpoint/node to the modules/surfaces that call it (inbound fan-in)."""
        out = []
        for i in self._pool():
            out += [nb["id"] for nb in self.c.neighbors(i, "in", depth)]
        return self._new(list(dict.fromkeys(out)))

    def dependencies(self, depth=1):
        """Hop outbound — what each node in the set depends on."""
        out = []
        for i in self._pool():
            out += [nb["id"] for nb in self.c.neighbors(i, "out", depth)]
        return self._new(list(dict.fromkeys(out)))

    def surfaces(self):
        """Collapse the working set to the distinct surfaces it touches (as ids)."""
        surfs = set()
        for i in self._pool():
            n = self.c._nodes.get(i, {})
            if n.get("type") == "surface":
                surfs.add(i)
            else:
                for s in (n.get("surfaces") or []):
                    surfs.add(f"surface:{s}")
        # map back to real ids present in the graph
        by_label = {self.c._nodes[i].get("label"): i for i in self.c._nodes
                    if self.c._nodes[i].get("type") == "surface"}
        resolved = {by_label.get(s.replace("surface:", ""), s) for s in surfs}
        return self._new(sorted(x for x in resolved if x in self.c._nodes))

    # ── field-usage (the returned-vs-used dimension) ──────────────────────────
    def drop_candidates(self, returned_fields):
        """For the (single) endpoint in the set: which of returned_fields is read by NO
        caller anywhere → the safe-to-drop candidates. Uses the CONSERVATIVE per-field
        reference test (extract.field_is_referenced), so a field reached through nested
        access (cmd.traffic.ctr) still counts as read. A field is a candidate ONLY if its
        name appears nowhere in any caller body — the gate still wants a second-context
        confirm, but a name that appears nowhere is a strong, low-false-positive signal.
        Returns {endpoint, returned, referenced, drop_candidates, callers}."""
        eps = [i for i in self._pool() if self.c._nodes.get(i, {}).get("type") == "endpoint"]
        if len(eps) != 1:
            raise ValueError(f"drop_candidates needs exactly 1 endpoint in the set, got {len(eps)}")
        bodies = self.c._caller_bodies(eps[0])
        referenced, cand = [], []
        for f in returned_fields:
            if any(ex.field_is_referenced(b, f) for b in bodies.values()):
                referenced.append(f)
            else:
                cand.append(f)
        return {"endpoint": self.c._nodes[eps[0]].get("label"),
                "returned": list(returned_fields), "referenced": referenced,
                "drop_candidates": cand, "callers": sorted(bodies.keys())}

    def redundancy(self, sample):
        """Flag REDUNDANCY/denormalization/DoS-amplification waste in the (single)
        endpoint's sample response — the waste class drop_candidates MISSES (a READ field
        whose VALUE is massively repeated across rows, e.g. a product name joined onto every
        variant then split back client-side). Delegates to Catalogue.payload_redundancy."""
        return self.c.payload_redundancy(sample)

    # ── terminals ─────────────────────────────────────────────────────────────
    def patch_spec(self, drop_fields, *, backend_glob):
        """Turn a drop-analysis into a REVIEWABLE fix spec (never auto-applies — a generator
        that applies its own output shares the blind spot that made the bug; a second context
        gates). For each field in drop_fields, locate its emit line in the backend source
        (`"field": <expr>` in a returned dict) and any builder line that feeds ONLY that field
        (a `<var> = <call>` whose <var> is used only on the emit line → now dead). Returns
        {endpoint, edits:[{file,line,kind,text,reason}], note}. backend_glob = glob(s) of the
        server files that build this endpoint's response (e.g. 'module/services_dashboard.py').
        Callable after .drop_candidates confirmed the fields, or with a hand-supplied list."""
        import glob as _glob, re as _re, pathlib as _pl
        eps = [i for i in self._pool() if self.c._nodes.get(i, {}).get("type") == "endpoint"]
        ep_label = self.c._nodes[eps[0]].get("label") if eps else None
        globs = [backend_glob] if isinstance(backend_glob, str) else list(backend_glob)
        files = [f for g in globs for f in _glob.glob(g)]
        edits, note = [], []
        for path in files:
            src = _pl.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            for f in drop_fields:
                emit = _re.compile(r"""^\s*["']""" + _re.escape(f) + r"""["']\s*:\s*(.+?),?\s*$""")
                for ln_i, ln in enumerate(src):
                    m = emit.match(ln)
                    if not m:
                        continue
                    edits.append({"file": path, "line": ln_i + 1, "kind": "remove-emit",
                                  "text": ln.strip(),
                                  "reason": f"'{f}' is in no caller's read set — drop from response"})
                    # is the emit expr `builder_var.get(k)` / `builder_var[k]` whose var is
                    # assigned once and used ONLY here? then its builder line is now dead.
                    vm = _re.match(r"""([A-Za-z_]\w*)\s*[.\[]""", m.group(1).strip())
                    if vm:
                        var = vm.group(1)
                        uses = [i for i, l in enumerate(src)
                                if _re.search(r"(?<![\w.])" + _re.escape(var) + r"(?![\w])", l)]
                        assigns = [i for i in uses if _re.match(r"\s*" + _re.escape(var) + r"\s*=", src[i])]
                        if len(assigns) == 1 and len(uses) == 2:   # 1 assign + this 1 use
                            ai = assigns[0]
                            edits.append({"file": path, "line": ai + 1, "kind": "remove-dead-builder",
                                          "text": src[ai].strip(),
                                          "reason": f"'{var}' fed only '{f}'; now unused — removes the "
                                                    f"query that built it (perf win, not just payload)"})
        if not edits:
            note.append("no emit site found in the given backend_glob — check the file(s) or "
                        "the field may be built dynamically")
        return {"endpoint": ep_label, "drop_fields": list(drop_fields),
                "edits": edits, "note": " ".join(note),
                "apply": "REVIEW-THEN-APPLY: gate by a prod byte-diff + a render check before shipping"}

    def ids(self):
        return list(self._pool())

    def nodes(self):
        return [self.c._nodes[i] for i in self._pool() if i in self.c._nodes]

    def rows(self):
        """Scannable [{id,type,label,surfaces}] — the default human view."""
        out = []
        for i in self._pool():
            n = self.c._nodes.get(i, {})
            out.append({"id": i, "type": n.get("type"), "label": n.get("label", i),
                        "surfaces": n.get("surfaces", [])})
        return out

    def first(self):
        p = self._pool()
        return self.c._nodes.get(p[0]) if p else None

    def count(self):
        return len(self._pool())
