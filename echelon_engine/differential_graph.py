#!/usr/bin/env python3
"""Unified code atlas and evidence graph for Python differential audits.

Python 3.10+, standard library only for Python extraction and graph queries.
Never imports or executes the target repository. Output is versioned JSON, NOT
an invented .des dialect: adapt this schema to your actual .des specification.

QUICK START
  python differential_graph.py --self-test
  python differential_graph.py extract --root /repo --out graph.json
  python differential_graph.py extract --root /repo --pkg src --out graph.json
  python differential_graph.py query graph.json deps 'symbol:app.py:create_order' --rel calls
  python differential_graph.py query graph.json find create_order
  python differential_graph.py compare ours.json peer1.json peer2.json --out deltas.json
  python differential_graph.py atlas --root /repo --pkg . --out docs/c-atlas
  python differential_graph.py legacy-query deps module_name --root /repo --json

EXTRACTION CONTRACT
  nodes: repository, module, class, function, parameter, external, unresolved,
         field, operation, endpoint; edges: contains, imports, calls, decorates,
         inherits, accepts, returns, raises, yields, reads, writes, branches,
         handles, uses_context, awaits, exposes, registers_command,
         declares_field, references_parameter, assigns, invokes, asserts, loops.
  Every code-derived node/edge carries file/line/column evidence plus SHA-256.
  AST observations are syntactic facts, not proof of execution or protection.
  Calls are candidates where bindings can change; unresolved calls stay visible.
  Function contracts distinguish annotations, observed returns, and unknowns.
  Branch context records lexical nesting, not dominance or full control flow.
  Effect/security tags use configurable spelling patterns and are heuristics.
  No whole-program points-to, taint, alias, decorator, ORM, or middleware proof.
  Reflection, monkey-patching and dependency behavior require a runtime/spec oracle.

COMPARISON
  Use --capabilities mapping.json on extract to align symbols explicitly:
    {"app.py:create_order": "order.create", "app.py:OrderService.get": "order.read"}
  Automatic capability labels use HTTP method/path or function names and are
  proposals, not equivalence proofs. compare defaults to explicit labels only;
  --allow-inferred permits exact heuristic-label matches. Ambiguous labels are
  excluded. Absent/unparseable capabilities do not vote as missing guards.
  Peers vote once per graph; source-identical peers are deduplicated. Forks and
  shared ancestry still require human curation. Scores are prevalence, not risk.
  Shapes retain typed edge neighborhoods, contract facts, normalized predicates
  and operations sorted by source position (NOT runtime execution order);
  comparison emits explainable feature deltas with evidence, not vulnerability
  verdicts. Shared bugs and unseen downstream validation remain blind spots.

COMPATIBILITY / PROVENANCE
  Consolidates the three supplied gen_code_atlas, cgraph and pagemodel.graph
  scripts. Original public helpers remain below (including their old atlas
  format); legacy entry points are atlas, legacy-query and legacy-surface.
  legacy-surface alone needs echelon_engine.pagemodel.extract and identity,
  which were NOT supplied. Its JS heuristics are preserved, not upgraded.
  Python extract/query/compare have no Echelon dependency. --plant remains an
  explicit legacy integration requiring Echelon. No license is assumed for
  inputs; graph source hashes and optional --revision / --source-url track
  provenance. Do not infer redistribution permission from graph extraction.
  Expressions/docstrings can include sensitive source literals; review exports.
"""
from __future__ import annotations

# === Preserved atlas API ===
import argparse
import ast
import json
import sys
from pathlib import Path


# ── node id: a stable, filename-safe slug for a module ────────────────────────
def module_id(rel_path: Path) -> str:
    """A module's dotted path with '.' -> '-' so it is a safe json filename stem.
    echelon_engine/atoms/cards.py -> echelon_engine-atoms-cards.  __init__.py folds to
    the package node (echelon_engine-atoms), so a package and its __init__ share one node."""
    parts = list(rel_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return "-".join(parts) if parts else "root"


def _dotted(rel_path: Path) -> str:
    parts = list(rel_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# ── soul detection: which modules ARE the identity/doctrine, not just code ─────
_SOUL_HINTS = ("soul", "identity", "origin", "self_seed", "registry", "card_types",
               "card_credit", "earn_law", "uame")


def _classify_type(rel_path: Path, dotted: str, has_cli: bool) -> str:
    """soul (identity/doctrine/law) > cli (a command verb module) > package > module."""
    stem = rel_path.with_suffix("").name.lower()
    low = dotted.lower()
    if any(h in stem or h in low for h in _SOUL_HINTS):
        return "soul"
    if has_cli:
        return "cli"
    if rel_path.name == "__init__.py":
        return "package"
    return "module"


# ── one module -> one atlas card (pure AST, no import execution) ──────────────
def scan_module(root: Path, py: Path) -> dict:
    """Parse ONE .py into a c-atlas card dict. Never imports the module (ast only, so a
    module with heavy import side-effects or a missing dep still maps). A syntax error
    degrades to a status='unparseable' node rather than crashing the whole walk."""
    rel = py.relative_to(root)
    nid = module_id(rel)
    dotted = _dotted(rel)
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    except (SyntaxError, UnicodeDecodeError) as e:
        return {"id": nid, "type": "module", "title": dotted, "status": "unparseable",
                "path": str(rel).replace("\\", "/"), "gap_reason": f"{type(e).__name__}: {e}",
                "members": [], "depends_on": [], "part_of": "", "cli_verbs": [], "exposure": ""}

    doc = (ast.get_docstring(tree) or "").strip()
    claim = doc.splitlines()[0][:200] if doc else ""

    members, cli_verbs, has_argparse = [], [], False
    for node in tree.body:  # top-level only (public API surface, not nested helpers)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            md = (ast.get_docstring(node) or "").splitlines()
            members.append({"kind": "func", "name": node.name,
                            "claim": (md[0][:140] if md else "")})
            # a cmd_<verb> function is the ECHELON CLI-handler convention
            if node.name.startswith("cmd_"):
                cli_verbs.append(node.name[4:])
        elif isinstance(node, ast.ClassDef):
            md = (ast.get_docstring(node) or "").splitlines()
            members.append({"kind": "class", "name": node.name,
                            "claim": (md[0][:140] if md else "")})

    # depends_on: intra-repo imports only (the blast-radius graph that matters). An import
    # of `echelon_engine.atoms.cards` -> depends_on node echelon_engine-atoms-cards.
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                deps.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                deps.add(node.module)
            elif node.level > 0:  # relative import — resolve against this module's package
                pkg_parts = dotted.split(".") if rel.name == "__init__.py" else dotted.split(".")[:-1]
                base = ".".join(pkg_parts[: len(pkg_parts) - (node.level - 1)]) if pkg_parts else ""
                deps.add((base + "." + node.module) if node.module else base)
    # argparse detection (a module that builds a parser exposes commands)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("add_parser", "add_subparsers"):
            has_argparse = True
            break

    part_of = "-".join(nid.split("-")[:-1]) if "-" in nid else ""
    ntype = _classify_type(rel, dotted, bool(cli_verbs) or has_argparse)
    return {"id": nid, "type": ntype, "title": dotted, "status": "live",
            "path": str(rel).replace("\\", "/"), "claim": claim,
            "members": members, "_raw_imports": sorted(deps),
            "part_of": part_of, "cli_verbs": sorted(set(cli_verbs)),
            # exposure filled in the second pass once we know the full node set + docs
            "exposure": "", "gap_reason": "", "role": ""}


def _resolve_deps(cards: list[dict], dotted_to_id: dict[str, str]) -> None:
    """Turn each card's raw import strings into depends_on edges to KNOWN in-repo nodes.
    An import that doesn't resolve to a repo module (stdlib / 3rd-party) is dropped — the
    atlas maps the ENGINE's internal coupling, not the pip graph."""
    for c in cards:
        edges = []
        for imp in c.pop("_raw_imports", []):
            # longest-prefix match: import echelon_engine.atoms.cards.CardStore should
            # still resolve to the cards MODULE node.
            best = ""
            for dotted, nid in dotted_to_id.items():
                if (imp == dotted or imp.startswith(dotted + ".")) and len(dotted) > len(best):
                    best = dotted
            if best:
                tgt = dotted_to_id[best]
                if tgt != c["id"] and tgt not in edges:
                    edges.append(tgt)
        c["depends_on"] = [{"node": e} for e in sorted(edges)]


def build_atlas(root: Path, pkgs: list[str]) -> tuple[list[dict], dict]:
    """Walk each package, scan every .py, resolve intra-repo edges. Returns (cards, meta)."""
    root = root.resolve()
    py_files: list[Path] = []
    for pkg in pkgs:
        base = (root / pkg)
        if not base.exists():
            continue
        py_files.extend(sorted(base.rglob("*.py")))
    # skip test files and caches from the STRUCTURAL map (they're consumers, not surface)
    py_files = [p for p in py_files
                if "__pycache__" not in p.parts and not p.name.startswith("test_")
                and ".venv" not in p.parts and "egg-info" not in str(p)]
    cards = [scan_module(root, p) for p in py_files]
    dotted_to_id = {_dotted(Path(c["path"])): c["id"] for c in cards}
    _resolve_deps(cards, dotted_to_id)
    meta = {"scope": "echelon", "kind": "c-atlas", "generator": "gen_code_atlas",
            "altitude": "component", "packages": pkgs, "node_count": len(cards),
            "status": "generated", "note": "deterministic AST skeleton; role/gap/risk "
            "left empty for a later LLM-enrich pass (or /wrap)."}
    return cards, meta


# ── COMMAND SURFACER: classify every CLI verb by exposure ─────────────────────
def surface_commands(root: Path, cards: list[dict]) -> dict:
    """Find every CLI verb (cmd_* / add_parser) and classify EXPOSURE against the real
    --help registration and the docs tree. documented > undocumented > hidden. This is the
    antibody to a silent coupling: a hidden verb is defined + reachable but invisible."""
    # collect declared verbs from the atlas
    declared: dict[str, str] = {}   # verb -> node id
    for c in cards:
        for v in c.get("cli_verbs", []):
            declared.setdefault(v, c["id"])
    # what the top-level __main__ actually registers (best-effort text scan — the real
    # --help surface). And what the docs mention.
    main_txt = ""
    mainp = root / "echelon_engine" / "__main__.py"
    if mainp.exists():
        main_txt = mainp.read_text(encoding="utf-8", errors="ignore")
    docs_txt = ""
    docsdir = root / "docs"
    if docsdir.exists():
        for d in docsdir.rglob("*.md"):
            docs_txt += d.read_text(encoding="utf-8", errors="ignore").lower()
    # a verb is registered if it's a subparser token ANYWHERE in the repo — the top-level
    # __main__ OR a module's own add_parser (atlas set/nodes/... are registered inside
    # atlas.py's subparser, not __main__; calling those 'hidden' was a false positive).
    reg_txt = main_txt
    for c in cards:
        p = root / c.get("path", "")
        if p.exists() and c.get("cli_verbs"):
            reg_txt += p.read_text(encoding="utf-8", errors="ignore")
    out = []
    for verb, node in sorted(declared.items()):
        in_main = f'"{verb}"' in main_txt or f"'{verb}'" in main_txt
        # registered as a subparser somewhere (add_parser("verb") / dispatch on the token)
        registered = (f'add_parser("{verb}"' in reg_txt or f"add_parser('{verb}'" in reg_txt
                      or f'"{verb}"' in reg_txt or f"'{verb}'" in reg_txt)
        in_docs = verb.lower() in docs_txt
        # top-level (in __main__) vs subcommand (registered elsewhere) vs hidden (nowhere).
        if in_main:
            exposure = "documented" if in_docs else "undocumented"
        elif registered:
            exposure = "subcommand" if in_docs else "subcommand-undocumented"
        else:
            exposure = "hidden"
        out.append({"verb": verb, "node": node, "registered": registered,
                    "top_level": in_main, "in_docs": in_docs, "exposure": exposure})
    def _n(kind):
        return sum(1 for c in out if c["exposure"] == kind)
    return {"commands": out,
            "summary": {"total": len(out), "documented": _n("documented"),
                        "undocumented": _n("undocumented"), "subcommand": _n("subcommand"),
                        "subcommand_undocumented": _n("subcommand-undocumented"),
                        "hidden": _n("hidden")}}


def write_atlas(out_dir: Path, cards: list[dict], meta: dict, commands: dict,
                dry_run: bool = False) -> dict:
    """Write define/<id>.json + _meta.json + _commands.json. Append-only spirit: a node
    file is REPLACED (rebuildable skeleton), but human-enriched fields (role/gap_reason)
    already present in an existing file are PRESERVED across a regen."""
    define = out_dir / "define"
    written = 0
    if not dry_run:
        define.mkdir(parents=True, exist_ok=True)
    for c in cards:
        target = define / f"{c['id']}.json"
        # preserve prior LLM-enriched fields if a file already exists (don't blank human work)
        if target.exists():
            try:
                prior = json.loads(target.read_text(encoding="utf-8"))
                for k in ("role", "gap_reason", "status_override", "risk"):
                    if prior.get(k) and not c.get(k):
                        c[k] = prior[k]
            except Exception:
                pass
        if not dry_run:
            target.write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")
        written += 1
    if not dry_run:
        (define / "_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
        (define / "_commands.json").write_text(json.dumps(commands, indent=2, ensure_ascii=False),
                                               encoding="utf-8")
    return {"written": written, "dir": str(define), "commands": commands["summary"],
            "dry_run": dry_run}


def atlas_main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate a deterministic Python code-atlas.")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--out", default="docs/c-atlas", help="atlas dir (default: docs/c-atlas)")
    ap.add_argument("--pkg", action="append", dest="pkgs", default=None,
                    help="package to walk (repeatable). Default: echelon_engine, echelon_sdk, apps")
    ap.add_argument("--plant", default="", metavar="SCOPE",
                    help="after writing, plant the atlas into this bank scope via atlas_bridge")
    ap.add_argument("--dry-run", action="store_true", help="scan + report, write nothing")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    pkgs = a.pkgs or ["echelon_engine", "echelon_sdk", "apps"]

    cards, meta = build_atlas(root, pkgs)
    commands = surface_commands(root, cards)
    res = write_atlas(root / a.out, cards, meta, commands, dry_run=a.dry_run)

    # report
    by_type: dict[str, int] = {}
    for c in cards:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    total_edges = sum(len(c.get("depends_on", [])) for c in cards)
    print(f"=== code-atlas {'(DRY RUN) ' if a.dry_run else ''}===")
    print(f"  nodes: {res['written']}  ({', '.join(f'{k}={v}' for k,v in sorted(by_type.items()))})")
    print(f"  intra-repo edges: {total_edges}")
    print(f"  commands: {commands['summary']}")
    hidden = [c for c in commands["commands"] if c["exposure"] == "hidden"]
    sub_undoc = [c for c in commands["commands"] if c["exposure"] == "subcommand-undocumented"]
    undoc = [c for c in commands["commands"] if c["exposure"] == "undocumented"]
    if hidden:
        print(f"  ⚠ HIDDEN verbs (defined + reachable, NOT registered anywhere, no docs): "
              f"{', '.join(h['verb'] for h in hidden)}")
    if undoc:
        print(f"  ⚠ UNDOCUMENTED top-level verbs (in --help, not in docs): "
              f"{', '.join(h['verb'] for h in undoc)}")
    if sub_undoc:
        print(f"  · subcommands lacking docs: {', '.join(h['verb'] for h in sub_undoc)}")
    print(f"  -> {res['dir']}")

    if a.plant and not a.dry_run:
        from echelon_engine.atoms.atlas_bridge import seed_scope_from_atlas
        from echelon_engine.atoms.store import SeedStore
        n = seed_scope_from_atlas(root / a.out / "define", a.plant, SeedStore())
        print(f"  planted {n} charged seeds into scope '{a.plant}'")
    return 0



# === Preserved cgraph API ===

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
    return [[f.name, hashlib.sha256(f.read_bytes()).hexdigest()] for f in files]


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
            for d in sorted(graph.out.get(n, set())):
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


def cgraph_main(argv=None) -> int:
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
        if len(a_hits) != 1 or len(b_hits) != 1:
            print("ambiguous path references; use exact node IDs")
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


def legacy_query_main(argv=None) -> int:
    return cgraph_main(argv)



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

# === Preserved surface API ===
#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse, json, re, pathlib

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
    from echelon_engine.pagemodel import extract as ex
    from echelon_engine.pagemodel import identity as idy
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

def surface_main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--js", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    mf = json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8"))
    g = build_graph(mf, pathlib.Path(args.js).read_text(encoding="utf-8"))
    pathlib.Path(args.out).write_text(json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] graph {args.out}: {g['node_count']} nodes, {g['edge_count']} edges, "
          f"{g['deterministic_pct']}% deterministic")
    # quick dangling check
    ids = {n["id"] for n in g["nodes"]}
    dangling = [e for e in g["edges"] if e["from"] not in ids or e["to"] not in ids]
    print(f"    dangling edges: {len(dangling)}" + (f"  !! {dangling[:2]}" if dangling else "  (none)"))


# === Rich Python evidence graph: independent of the legacy integrations ===
import collections
import fnmatch
import hashlib
import os
import tokenize

SCHEMA = 'python-evidence-graph/1.0'
DEFAULT_EXCLUDES = ('.git', '.venv', 'venv', '__pycache__', 'node_modules',
                    'site-packages', 'build', 'dist', '*.egg-info')
DEFAULT_RULES = {
    'authorization_candidate': ['*authorize*', '*permission*', '*check_access*'],
    'authentication_candidate': ['*authenticate*', '*login_required*'],
    'validation_candidate': ['*validate*', '*full_clean*', '*verify_signature*'],
    'database_read_candidate': ['*.query', '*.filter', '*.select', '*.get'],
    'database_write_candidate': ['*.save', '*.insert', '*.update', '*.delete', '*.execute'],
    'transaction_candidate': ['*.atomic', '*.transaction', '*.commit', '*.rollback'],
    'network_candidate': ['requests.*', 'httpx.*', 'urllib.request.*', '*.fetch'],
    'filesystem_candidate': ['open', 'builtins.open', '*.read_text', '*.write_text', '*.unlink'],
    'process_candidate': ['subprocess.*', 'os.system', 'os.popen'],
    'dynamic_execution_candidate': ['eval', 'exec', 'builtins.eval', 'builtins.exec'],
    'deserialization_candidate': ['pickle.*', 'yaml.load', 'marshal.loads'],
    'logging_candidate': ['logging.*', '*.debug', '*.info', '*.warning', '*.error'],
}


def _digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode('utf-8')).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def _expr(node):
    return ast.unparse(node) if node is not None else None


def _literal(node):
    try:
        value = ast.literal_eval(node)
        json.dumps(value)
        return {'known': True, 'value': value}
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return {'known': False, 'expression': _expr(node)}


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _name(node.value)
        return parent + '.' + node.attr if parent else ''
    return ''


def _owned_walk(node):
    """Walk one executable scope, never attributing nested bodies to its parent."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _owned_walk(child)


def discover_python(root, packages=None, excludes=DEFAULT_EXCLUDES):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'Not a repository directory: {root}')
    found = set()
    for package in packages or ['.']:
        base = (root / package).resolve()
        if not base.is_relative_to(root):
            raise ValueError(f'Package escapes root: {package}')
        if not base.exists():
            raise ValueError(f'Package does not exist: {package}')
        candidates = [base] if base.is_file() else None
        if candidates is None:
            candidates = []
            for directory, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in excludes)
                                  and not Path(directory, d).is_symlink())
                candidates.extend(Path(directory, f) for f in sorted(files) if f.endswith('.py'))
        for path in candidates:
            if path.suffix == '.py' and not path.is_symlink() and path.resolve().is_relative_to(root):
                found.add(path)
    return sorted(found)


class PythonGraphExtractor:
    """Two-pass static extractor. Instances are single-use; use extract_python()."""
    def __init__(self, root, packages=None, capabilities=None, rules=None,
                 excludes=DEFAULT_EXCLUDES, revision=None, source_url=None):
        self.root = Path(root).resolve()
        self.packages = packages or ['.']
        self.capabilities = capabilities or {}
        self.rules = rules if rules is not None else DEFAULT_RULES
        if not isinstance(self.capabilities, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                             for k, v in self.capabilities.items()):
            raise ValueError('Capabilities must map symbol keys to strings')
        if not isinstance(self.rules, dict) or not all(isinstance(k, str) and isinstance(v, list)
                and all(isinstance(p, str) for p in v) for k, v in self.rules.items()):
            raise ValueError('Rules must map tag names to lists of glob patterns')
        self.excludes, self.revision, self.source_url = excludes, revision, source_url
        self.nodes, self.edges, self.sources, self.diagnostics = {}, [], {}, []
        self.scopes, self.ast_ids, self.symbols, self.modules = {}, {}, {}, {}
        self.units, self.pending = [], []
        self.add_node('repository:.', 'repository', label='repository')

    def evidence(self, path, node):
        return {'path': path, 'line': getattr(node, 'lineno', 1),
                'column': getattr(node, 'col_offset', 0),
                'end_line': getattr(node, 'end_lineno', getattr(node, 'lineno', 1)),
                'end_column': getattr(node, 'end_col_offset', 0),
                'source_sha256': self.sources[path]['sha256']}

    def add_node(self, nid, node_type, **fields):
        if nid not in self.nodes:
            self.nodes[nid] = {'id': nid, 'type': node_type, **fields}
        return nid

    def edge(self, source, target, rel, evidence=None, confidence='observed', **fields):
        self.edges.append({'from': source, 'to': target, 'rel': rel,
                           'confidence': confidence, **({'evidence': evidence} if evidence else {}), **fields})

    def symbol(self, full, nid):
        self.symbols.setdefault(full, []).append(nid)

    def index_scope(self, body, sid, path, dotted, qual='', class_id=None):
        scope = self.scopes[sid]
        # Statements nested in branches still bind names in this Python scope.
        def statements(items):
            for item in items:
                yield item
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    for field in ('body', 'orelse', 'finalbody'):
                        value = getattr(item, field, None)
                        if isinstance(value, list):
                            yield from statements(value)
                    for handler in getattr(item, 'handlers', []):
                        yield from statements(handler.body)
                    for case in getattr(item, 'cases', []):
                        yield from statements(case.body)
        for node in statements(body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = qual + '.' + node.name if qual else node.name
                base = f'symbol:{path}:{q}'
                nid = base if base not in self.nodes else base + f'@{node.lineno}'
                kind = 'class' if isinstance(node, ast.ClassDef) else 'function'
                ev = self.evidence(path, node)
                self.add_node(nid, kind, label=node.name, qualname=q, path=path, evidence=ev,
                              docstring=ast.get_docstring(node), decorators=[_expr(d) for d in node.decorator_list],
                              visibility='private' if node.name.startswith('_') else 'public',
                              **({'async': isinstance(node, ast.AsyncFunctionDef)} if kind == 'function' else {}))
                self.edge(sid, nid, 'contains', ev)
                self.ast_ids[id(node)] = nid
                scope['bindings'].setdefault(node.name, []).append(nid)
                self.symbol((dotted + '.' + q).strip('.'), nid)
                self.scopes[nid] = {'parent': sid, 'bindings': {}, 'imports': {}, 'locals': set(),
                                    'class_id': class_id, 'kind': kind, 'path': path, 'dotted': dotted}
                self.index_scope(node.body, nid, path, dotted, q, nid if kind == 'class' else class_id)
                if kind == 'function':
                    self.contract(node, nid, path)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                module_parts = dotted.split('.') if path.endswith('__init__.py') else dotted.split('.')[:-1]
                if isinstance(node, ast.ImportFrom):
                    if node.level > len(module_parts) and node.level:
                        self.diagnostics.append({'kind': 'relative_import_outside_package', 'evidence': self.evidence(path, node)})
                    base = '.'.join(module_parts[:len(module_parts) - node.level + 1]) if node.level else ''
                    prefix = '.'.join(p for p in (base, node.module) if p)
                for alias in node.names:
                    if isinstance(node, ast.Import):
                        local = alias.asname or alias.name.split('.')[0]
                        full = alias.name if alias.asname else local
                        imported = alias.name
                    else:
                        local, full = alias.asname or alias.name, '.'.join(p for p in (prefix, alias.name) if p)
                        imported = full
                    scope['imports'].setdefault(local, []).append(full)
                    self.pending.append((sid, imported, self.evidence(path, node)))
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    scope['locals'].update(n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                scope['locals'].update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
        # A comprehensive scope-owned Store pass handles with/except/comprehensions conservatively.
        for statement in body:
            owned = [statement] if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [statement, *_owned_walk(statement)]
            for child in owned:
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    scope['locals'].add(child.id)
                if isinstance(child, ast.ExceptHandler) and child.name:
                    scope['locals'].add(child.name)
                if isinstance(child, (ast.Global, ast.Nonlocal)):
                    scope['locals'].update(child.names)  # abstain on mutable nonlocal binding

    def contract(self, node, sid, path):
        args = node.args
        positional = args.posonlyargs + args.args
        defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        entries = [(a, 'positional_only' if i < len(args.posonlyargs) else 'positional_or_keyword', defaults[i])
                   for i, a in enumerate(positional)]
        if args.vararg:
            entries.append((args.vararg, 'var_positional', None))
        entries.extend((a, 'keyword_only', d) for a, d in zip(args.kwonlyargs, args.kw_defaults))
        if args.kwarg:
            entries.append((args.kwarg, 'var_keyword', None))
        params = []
        for a, kind, default in entries:
            p = {'name': a.arg, 'kind': kind, 'annotation': _expr(a.annotation),
                 'required': default is None and not kind.startswith('var_'),
                 'has_default': default is not None,
                 'default': _expr(default)}
            params.append(p)
            pid = self.add_node(sid + ':parameter:' + a.arg, 'parameter', **p, evidence=self.evidence(path, a))
            self.edge(sid, pid, 'accepts', self.evidence(path, a))
            self.scopes[sid]['locals'].add(a.arg)
        owned = list(_owned_walk(node))
        self.nodes[sid]['contract'] = {
            'parameters': params, 'return_annotation': _expr(node.returns),
            'return_expressions': [_expr(n.value) for n in owned if isinstance(n, ast.Return)],
            'yield_expressions': [_expr(n.value) for n in owned if isinstance(n, (ast.Yield, ast.YieldFrom))],
            'raises_observed': [_expr(n.exc) for n in owned if isinstance(n, ast.Raise)],
            'generator': any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in owned),
            'return_type_inference': 'not_performed', 'fallthrough_analysis': 'not_performed',
            'type_comment': getattr(node, 'type_comment', None),
            'type_parameters': [_expr(t) for t in getattr(node, 'type_params', [])],
        }
        key = path + ':' + self.nodes[sid]['qualname']
        capability = self.capabilities.get(key, self.capabilities.get(sid))
        self.nodes[sid]['capability'] = {'key': capability or 'python:' + node.name,
            'basis': 'explicit' if capability else 'name_heuristic'}

    def resolve_call(self, sid, expression):
        """Resolve syntactic binding candidates, avoiding unique-name global guesses."""
        if not expression:
            return [], 'dynamic'
        pieces = expression.split('.')
        first, suffix = pieces[0], '.'.join(pieces[1:])
        scope_id = sid
        while scope_id:
            scope = self.scopes[scope_id]
            # Instance dispatch can be overridden: always a candidate.
            if first in ('self', 'cls') and scope['class_id'] and suffix:
                class_node = self.nodes[scope['class_id']]
                full = '.'.join(p for p in (scope['dotted'], class_node['qualname'], suffix) if p)
                return self.symbols.get(full, []), 'method_candidate'
            if first in scope['locals']:
                return [], 'local_or_rebound'
            if first in scope['bindings']:
                ids = scope['bindings'][first]
                if suffix:
                    targets = []
                    for nid in ids:
                        full = '.'.join(p for p in (scope['dotted'], self.nodes[nid]['qualname'], suffix) if p)
                        targets.extend(self.symbols.get(full, []))
                    return targets, 'lexical_candidate'
                return ids, 'lexical_candidate'
            if first in scope['imports']:
                targets = []
                for imp in scope['imports'][first]:
                    full = imp + ('.' + suffix if suffix else '')
                    known = self.symbols.get(full, [])
                    if known:
                        targets.extend(known)
                    else:
                        nid = self.add_node('external:' + full, 'external', label=full,
                            resolution='imported_symbol_not_resolved_in_scanned_sources')
                        targets.append(nid)
                return targets, 'import_candidate'
            parent = scope['parent']
            # Python function free-name lookup skips the containing class namespace.
            if scope['kind'] == 'function':
                while parent and self.scopes[parent]['kind'] == 'class':
                    parent = self.scopes[parent]['parent']
            scope_id = parent
        if '.' not in expression:
            import builtins
            if hasattr(builtins, expression):
                return [self.add_node('external:builtins.' + expression, 'external', label='builtins.' + expression)], 'builtin_candidate'
        return [], 'unresolved'

    def extract(self):
        for py in discover_python(self.root, self.packages, self.excludes):
            path = py.relative_to(self.root).as_posix()
            try:
                raw = py.read_bytes()
                self.sources[path] = {'sha256': _digest(raw), 'size_bytes': len(raw)}
                with tokenize.open(py) as handle:
                    source = handle.read()
                tree = ast.parse(source, filename=path, type_comments=True)
            except (OSError, SyntaxError, UnicodeError, LookupError) as error:
                self.sources.setdefault(path, {'sha256': None})['status'] = 'unparseable'
                self.diagnostics.append({'kind': 'parse_error', 'path': path, 'message': str(error)})
                nid = self.add_node('module:' + path, 'module', path=path, status='unparseable')
                self.edge('repository:.', nid, 'contains')
                continue
            self.sources[path]['status'] = 'parsed'
            # --pkg src also acts as an import root for the common src layout.
            relative = Path(path)
            for package in sorted(self.packages, key=len, reverse=True):
                candidate = Path(package)
                if candidate.name == 'src' and relative.is_relative_to(candidate):
                    relative = relative.relative_to(candidate)
                    break
            dotted = _dotted(relative)
            sid = self.add_node('module:' + path, 'module', path=path, label=dotted,
                                status='parsed', docstring=ast.get_docstring(tree), evidence=self.evidence(path, tree))
            self.modules.setdefault(dotted, []).append(sid)
            self.symbol(dotted, sid)
            self.edge('repository:.', sid, 'contains', self.evidence(path, tree))
            self.scopes[sid] = {'parent': None, 'bindings': {}, 'imports': {}, 'locals': set(),
                               'class_id': None, 'kind': 'module', 'path': path, 'dotted': dotted}
            self.index_scope(tree.body, sid, path, dotted)
            self.units.append((path, tree, sid))
        for sid, full, ev in self.pending:
            targets = self.symbols.get(full, [])
            if not targets:
                targets = [self.add_node('external:' + full, 'external', label=full,
                            resolution='import_not_resolved_in_scanned_sources')]
            for target in targets:
                self.edge(sid, target, 'imports', ev, 'candidate', expression=full)
        for path, tree, sid in self.units:
            _BehaviorVisitor(self, path, sid).visit(tree)
        used = {n['capability']['key'] for n in self.nodes.values() if 'capability' in n and n['capability']['basis'] == 'explicit'}
        for key, label in self.capabilities.items():
            if label not in used:
                self.diagnostics.append({'kind': 'unused_capability_mapping', 'symbol': key, 'capability': label})
        edges = sorted({_json(e): e for e in self.edges}.values(), key=lambda e: (e['from'], e['rel'], e['to'], _json(e)))
        graph = {'schema': SCHEMA, 'language': 'python', 'generator': 'differential_graph.py',
                 'provenance': {'source_url': self.source_url, 'revision': self.revision,
                     'source_digest': _digest(_json(self.sources)), 'files': self.sources,
                     'license_status': 'not_assessed'},
                 'config': {'packages': self.packages, 'excludes': list(self.excludes), 'rules': self.rules},
                 'nodes': sorted(self.nodes.values(), key=lambda n: n['id']), 'edges': edges,
                 'diagnostics': self.diagnostics,
                 'limitations': ['Static observations do not prove execution, dominance, validation or exploitability.',
                     'Dynamic binding, aliases, middleware and external behavior are incomplete.',
                     'Effect tags and inferred capability labels are heuristics.']}
        graph['capabilities'] = capability_shapes(graph)
        graph['summary'] = {'node_count': len(graph['nodes']), 'edge_count': len(edges),
            'parsed_files': sum(s['status'] == 'parsed' for s in self.sources.values()),
            'unparseable_files': sum(s['status'] != 'parsed' for s in self.sources.values()),
            'unresolved_calls': sum(e['rel'] == 'calls' and e.get('resolution') in ('unresolved', 'dynamic', 'local_or_rebound') for e in edges)}
        validate_graph(graph)
        return graph


class _BehaviorVisitor(ast.NodeVisitor):
    def __init__(self, extractor, path, sid):
        self.g, self.path, self.sid = extractor, path, sid
        self.context = []
        self.counter = collections.Counter()

    def operation(self, node, rel, **fields):
        self.counter[(self.sid, rel, getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0))] += 1
        count = self.counter[(self.sid, rel, getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0))]
        ev = self.g.evidence(self.path, node)
        oid = f"operation:{self.sid}:{ev['line']}:{ev['column']}:{rel}:{count}"
        self.g.add_node(oid, 'operation', kind=rel, expression=_expr(node), evidence=ev,
                        lexical_context=list(self.context), **fields)
        self.g.edge(self.sid, oid, rel, ev, lexical_context=list(self.context))
        if rel in ('branches', 'asserts'):
            subject = node.test if isinstance(node, ast.Assert) else node
            self.g.nodes[oid]['predicate_shape'] = normalized_expression(subject)
        if rel in ('reads', 'returns', 'invokes', 'assigns'):
            # Scope-local parameter references, not value tracking or taint proof.
            for name in sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}):
                pid = self.sid + ':parameter:' + name
                if pid in self.g.nodes:
                    self.g.edge(oid, pid, 'references_parameter', ev, 'observed', semantics='syntactic_reference_only')
        return oid

    def visit_FunctionDef(self, node):
        child = self.g.ast_ids[id(node)]
        self.definition(node, child)
        # Defaults/decorator expressions execute in the enclosing scope.
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
            self.visit(default)
        old, context = self.sid, self.context
        self.sid, self.context = child, []
        for statement in node.body:
            self.visit(statement)
        self.sid, self.context = old, context

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        child = self.g.ast_ids[id(node)]
        self.definition(node, child)
        for base in node.bases:
            targets, resolution = self.g.resolve_call(self.sid, _name(base))
            if not targets:
                targets = [self.g.add_node('unresolved:base:' + child + ':' + str(base.lineno), 'unresolved', label=_expr(base))]
            for target in targets:
                self.g.edge(child, target, 'inherits', self.g.evidence(self.path, base), 'candidate', resolution=resolution, expression=_expr(base))
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        old = self.sid
        self.sid = child
        for statement in node.body:
            self.visit(statement)
        self.sid = old

    def definition(self, node, child):
        for decorator in node.decorator_list:
            expr = decorator.func if isinstance(decorator, ast.Call) else decorator
            targets, resolution = self.g.resolve_call(self.sid, _name(expr))
            if not targets:
                targets = [self.g.add_node('unresolved:decorator:' + child + ':' + str(decorator.lineno), 'unresolved', label=_expr(expr))]
            for target in targets:
                self.g.edge(child, target, 'decorates', self.g.evidence(self.path, decorator), 'candidate',
                            expression=_expr(decorator), resolution=resolution)
            self.route(decorator, child)
            self.visit(decorator)

    def route(self, node, child):
        if not isinstance(node, ast.Call):
            return
        spelling = _name(node.func)
        verb = spelling.rsplit('.', 1)[-1].lower()
        if verb not in ('route', 'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'websocket'):
            return
        path_arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in ('path', 'rule')), None)
        if path_arg is None:
            return
        literal = _literal(path_arg)
        if not literal.get('known') or not isinstance(literal['value'], str):
            return
        route = literal['value']
        methods = [verb.upper()] if verb != 'route' else ['UNKNOWN']
        for kw in node.keywords:
            if kw.arg == 'methods':
                val = _literal(kw.value)
                if val.get('known') and isinstance(val['value'], (list, tuple)) and all(isinstance(v, str) for v in val['value']):
                    methods = sorted(set(v.upper() for v in val['value']))
        norm = re.sub(r'\{[^}]+\}|<[^>]+>', '{}', route)
        for method in methods:
            eid = self.g.add_node('endpoint:' + method + ':' + route, 'endpoint', method=method,
                                 path=route, normalized_path=norm, evidence=self.g.evidence(self.path, node))
            self.g.edge(child, eid, 'exposes', self.g.evidence(self.path, node), 'inferred',
                        rule='route_decorator_spelling', decorator=spelling)
        card = self.g.nodes[child]
        if 'capability' in card and card['capability']['basis'] != 'explicit':
            card['capability'] = {'key': 'http:' + ','.join(methods) + ':' + norm, 'basis': 'route_heuristic'}

    def visit_Call(self, node):
        spelling = _name(node.func)
        targets, resolution = self.g.resolve_call(self.sid, spelling)
        ev = self.g.evidence(self.path, node)
        if not targets:
            targets = [self.g.add_node(f"unresolved:{self.sid}:{ev['line']}:{ev['column']}", 'unresolved',
                        label=_expr(node.func), reason=resolution, evidence=ev)]
        labels = [spelling] + [self.g.nodes[t].get('label', '') for t in targets]
        tags = sorted(tag for tag, patterns in self.g.rules.items()
                      if any(fnmatch.fnmatchcase(label.lower(), p.lower()) for label in labels for p in patterns))
        oid = self.operation(node, 'invokes', tags=tags, callee_expression=_expr(node.func),
                             arguments=[{'expression': _expr(a), 'starred': isinstance(a, ast.Starred)} for a in node.args],
                             keywords=[{'name': k.arg, 'expression': _expr(k.value)} for k in node.keywords])
        for target in targets:
            self.g.edge(self.sid, target, 'calls', ev, 'candidate', resolution=resolution,
                        callsite=oid, expression=_expr(node.func), tags=tags,
                        lexical_context=list(self.context))
        if spelling.endswith('.add_parser') and node.args:
            value = _literal(node.args[0])
            if value.get('known') and isinstance(value['value'], str):
                cid = self.g.add_node('command:' + value['value'], 'command', label=value['value'])
                self.g.edge(self.sid, cid, 'registers_command', ev, 'inferred', reachability='unknown')
        self.generic_visit(node)

    def block(self, nodes, descriptor):
        self.context.append(descriptor)
        for node in nodes:
            self.visit(node)
        self.context.pop()

    def visit_If(self, node):
        oid = self.operation(node.test, 'branches', branch_kind='if')
        self.visit(node.test)
        self.block(node.body, {'operation': oid, 'arm': 'true', 'predicate': _expr(node.test)})
        self.block(node.orelse, {'operation': oid, 'arm': 'false', 'predicate': _expr(node.test)})

    def visit_IfExp(self, node):
        oid = self.operation(node.test, 'branches', branch_kind='conditional_expression')
        self.visit(node.test)
        self.block([node.body], {'operation': oid, 'arm': 'true'})
        self.block([node.orelse], {'operation': oid, 'arm': 'false'})

    def visit_BoolOp(self, node):
        oid = self.operation(node, 'branches', branch_kind=type(node.op).__name__)
        for index, value in enumerate(node.values):
            self.block([value], {'operation': oid, 'arm': f'operand:{index}', 'short_circuit': True})

    def visit_Assert(self, node):
        self.operation(node, 'asserts', predicate=_expr(node.test), disabled_by_optimization=True)
        self.generic_visit(node)

    def visit_For(self, node):
        oid = self.operation(node, 'loops', loop_kind=type(node).__name__)
        self.visit(node.iter)
        self.visit(node.target)
        self.block(node.body, {'operation': oid, 'arm': 'body'})
        self.block(node.orelse, {'operation': oid, 'arm': 'else'})

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        oid = self.operation(node.test, 'loops', loop_kind='While')
        self.visit(node.test)
        self.block(node.body, {'operation': oid, 'arm': 'body'})
        self.block(node.orelse, {'operation': oid, 'arm': 'else'})

    def visit_Try(self, node):
        oid = self.operation(node, 'tries')
        self.block(node.body, {'operation': oid, 'arm': 'try'})
        for handler in node.handlers:
            self.operation(handler, 'handles', exception=_expr(handler.type), binding=handler.name,
                           exception_group=type(node).__name__ == 'TryStar')
            self.block(handler.body, {'operation': oid, 'arm': 'except', 'exception': _expr(handler.type)})
        self.block(node.orelse, {'operation': oid, 'arm': 'else'})
        self.block(node.finalbody, {'operation': oid, 'arm': 'finally'})

    visit_TryStar = visit_Try

    def visit_With(self, node):
        oid = self.operation(node, 'uses_context', managers=[_expr(i.context_expr) for i in node.items],
                             async_context=isinstance(node, ast.AsyncWith))
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self.visit(item.optional_vars)
        self.block(node.body, {'operation': oid, 'arm': 'body'})

    visit_AsyncWith = visit_With

    def visit_Match(self, node):
        oid = self.operation(node.subject, 'branches', branch_kind='match')
        self.visit(node.subject)
        for case in node.cases:
            descriptor = {'operation': oid, 'arm': _expr(case.pattern), 'guard': _expr(case.guard)}
            self.block(([case.guard] if case.guard else []) + case.body, descriptor)

    def visit_Lambda(self, node):
        # Explicit coverage boundary; do not fabricate calls in the enclosing scope.
        self.operation(node, 'defines_lambda', analysis='body_not_analyzed')
        self.g.diagnostics.append({'kind': 'lambda_body_not_analyzed', 'evidence': self.g.evidence(self.path, node)})
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            self.visit(default)

    def visit_Name(self, node):
        self.operation(node, 'writes' if isinstance(node.ctx, ast.Store) else 'deletes' if isinstance(node.ctx, ast.Del) else 'reads',
                       access_kind='name', name=node.id)

    def visit_Attribute(self, node):
        self.operation(node, 'writes' if isinstance(node.ctx, ast.Store) else 'deletes' if isinstance(node.ctx, ast.Del) else 'reads',
                       access_kind='attribute', name=_expr(node))
        self.visit(node.value)

    def visit_Subscript(self, node):
        self.operation(node, 'writes' if isinstance(node.ctx, ast.Store) else 'deletes' if isinstance(node.ctx, ast.Del) else 'reads',
                       access_kind='subscript', base=_expr(node.value), key=_expr(node.slice))
        self.generic_visit(node)

    def visit_Assign(self, node):
        self.operation(node, 'assigns', targets=[_expr(t) for t in node.targets], value=_expr(node.value),
                       source_names=sorted({n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}),
                       flow='syntactic_only')
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if self.g.scopes[self.sid]['kind'] in ('class', 'module') and isinstance(node.target, ast.Name):
            fid = self.g.add_node(self.sid + ':field:' + node.target.id, 'field',
                label=node.target.id, annotation=_expr(node.annotation), default=_expr(node.value),
                evidence=self.g.evidence(self.path, node))
            self.g.edge(self.sid, fid, 'declares_field', self.g.evidence(self.path, node))
        self.operation(node, 'assigns', targets=[_expr(node.target)], value=_expr(node.value), annotation=_expr(node.annotation))
        self.visit(node.target)
        if node.value:
            self.visit(node.value)

    def visit_AugAssign(self, node):
        self.operation(node, 'assigns', targets=[_expr(node.target)], value=_expr(node.value),
                       operator=type(node.op).__name__, reads_previous_value=True)
        self.operation(node.target, 'reads', access_kind='augmented_target', name=_expr(node.target))
        self.generic_visit(node)

    def visit_NamedExpr(self, node):
        self.operation(node, 'assigns', targets=[_expr(node.target)], value=_expr(node.value), walrus=True)
        self.generic_visit(node)

    def visit_ListComp(self, node):
        oid = self.operation(node, 'comprehends', deferred=isinstance(node, ast.GeneratorExp))
        self.context.append({'operation': oid, 'arm': 'comprehension', 'execution': 'possibly_deferred'})
        self.generic_visit(node)
        self.context.pop()

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Return(self, node):
        self.operation(node, 'returns', value=_expr(node.value))
        self.generic_visit(node)

    def visit_Raise(self, node):
        self.operation(node, 'raises', exception=_expr(node.exc), cause=_expr(node.cause), reraises=node.exc is None)
        self.generic_visit(node)

    def visit_Yield(self, node):
        self.operation(node, 'yields', value=_expr(node.value), delegated=isinstance(node, ast.YieldFrom))
        self.generic_visit(node)

    visit_YieldFrom = visit_Yield

    def visit_Await(self, node):
        self.operation(node, 'awaits', value=_expr(node.value))
        self.generic_visit(node)


def extract_python(root, **kwargs):
    return PythonGraphExtractor(root, **kwargs).extract()


def validate_graph(graph):
    if graph.get('schema') != SCHEMA:
        raise ValueError('Unsupported graph schema')
    ids = [n['id'] for n in graph['nodes']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate node IDs')
    known = set(ids)
    for edge in graph['edges']:
        if edge['from'] not in known or edge['to'] not in known:
            raise ValueError(f'Dangling edge: {edge}')
    return True


def normalized_expression(node):
    """Alpha-normalize names within one expression; preserve operators and literals.

    This is structural normalization, not semantic equivalence. Attribute names
    remain visible; variable placeholders preserve repeated-name relationships.
    """
    mapping = {}
    def shape(value):
        if isinstance(value, ast.Name):
            return ['Name', mapping.setdefault(value.id, 'v' + str(len(mapping)))]
        if isinstance(value, ast.AST):
            return [type(value).__name__, {field: shape(child) for field, child in ast.iter_fields(value)
                                           if field not in ('ctx', 'type_comment')}]
        if isinstance(value, list):
            return [shape(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)
    return json.dumps(shape(node), sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def capability_shapes(graph):
    """Scope-local, explainable signatures; do not confuse hashes with semantics."""
    nodes = {n['id']: n for n in graph['nodes']}
    outgoing = collections.defaultdict(list)
    for edge in graph['edges']:
        outgoing[edge['from']].append(edge)
    shapes = []
    for node in graph['nodes']:
        if node['type'] != 'function':
            continue
        contract = node['contract']
        features = set()
        for i, param in enumerate(contract['parameters']):
            features.add(f"parameter:{i}:{param['kind']}:{'required' if param['required'] else 'optional'}")
            if param['annotation']:
                features.add(f"parameter_annotation:{i}:{param['annotation']}")
        if contract['return_annotation']:
            features.add('return_annotation:' + contract['return_annotation'])
        if node['async']:
            features.add('async')
        if contract['generator']:
            features.add('generator')
        neighborhood, operations, feature_evidence = [], [], collections.defaultdict(list)
        for edge in outgoing[node['id']]:
            target = nodes[edge['to']]
            neighborhood.append({'rel': edge['rel'], 'target_type': target['type'],
                                  'confidence': edge['confidence'], 'target': target['id']})
            if target['type'] == 'operation':
                operations.append(target)
                feature = 'operation:' + target['kind']
                features.add(feature)
                feature_evidence[feature].append(target['evidence'])
                if target.get('predicate_shape'):
                    guard_feature = 'predicate_shape:' + target['predicate_shape']
                    features.add(guard_feature)
                    feature_evidence[guard_feature].append(target['evidence'])
                if target['kind'] == 'raises':
                    exc = target.get('exception')
                    raise_feature = 'raises:' + (exc.split('(')[0] if exc else '<reraise>')
                    features.add(raise_feature)
                    feature_evidence[raise_feature].append(target['evidence'])
                for tag in target.get('tags', []):
                    features.add('tag:' + tag)
                    feature_evidence['tag:' + tag].append(target['evidence'])
            if edge['rel'] in ('decorates', 'exposes'):
                feature = edge['rel'] + ':' + (edge.get('expression') or target.get('method', '') + ':' + target.get('normalized_path', ''))
                features.add(feature)
                feature_evidence[feature].append(edge['evidence'])
            if edge['rel'] == 'calls' and target.get('capability', {}).get('basis') == 'explicit':
                feature = 'calls_capability:' + target['capability']['key']
                features.add(feature)
                feature_evidence[feature].append(edge['evidence'])
        shapes.append({'node': node['id'], **node['capability'], 'evidence': node['evidence'],
                       'contract': contract, 'features': sorted(features),
                       'feature_evidence': dict(feature_evidence),
                       'neighborhood': neighborhood,
                       'operations': [n['id'] for n in sorted(operations, key=lambda n: (n['evidence']['line'], n['evidence']['column'], n['id']))],
                       'feature_digest': _digest(_json(sorted(features)))})
    return sorted(shapes, key=lambda s: (s['key'], s['node']))


def compare_graphs(target, peers, threshold=0.8, min_peers=2, allow_inferred=False):
    """Return missing/extra observed features, with explicit voting denominators."""
    if not 0 < threshold <= 1 or min_peers < 1:
        raise ValueError('threshold must be in (0,1]; min_peers must be positive')
    for graph in [target, *peers]:
        validate_graph(graph)
    excluded = []
    def index(graph):
        grouped = collections.defaultdict(list)
        for shape in capability_shapes(graph):
            if shape['basis'] == 'explicit' or allow_inferred:
                grouped[shape['key']].append(shape)
        return {key: values[0] for key, values in grouped.items() if len(values) == 1}, sorted(k for k, v in grouped.items() if len(v) > 1)
    own, ambiguous = index(target)
    unique, seen = [], {target['provenance']['source_digest']}
    for i, graph in enumerate(peers):
        digest = graph['provenance']['source_digest']
        if digest in seen:
            excluded.append({'peer': i, 'reason': 'duplicate_source_digest'})
            continue
        seen.add(digest)
        if graph['config'].get('rules') != target['config'].get('rules'):
            excluded.append({'peer': i, 'reason': 'different_effect_rules'})
            continue
        indexed, duplicates = index(graph)
        unique.append((i, indexed, duplicates, graph['provenance']))
    findings, coverage = [], []
    for key, ours in sorted(own.items()):
        aligned = [(i, idx[key], provenance) for i, idx, _, provenance in unique if key in idx]
        coverage.append({'capability': key, 'eligible_peers': len(aligned),
                         'excluded_or_unaligned_peers': len(peers) - len(aligned)})
        if len(aligned) < min_peers:
            continue
        counts = collections.Counter(f for _, shape, _ in aligned for f in set(shape['features']))
        ours_features = set(ours['features'])
        for feature in sorted(set(counts) | ours_features):
            support = counts[feature]
            prevalence = support / len(aligned)
            direction = 'not_observed_in_target' if feature not in ours_features and prevalence >= threshold else (
                'target_outlier_presence' if feature in ours_features and (1 - prevalence) >= threshold else None)
            if direction:
                findings.append({'capability': key, 'target_node': ours['node'], 'feature': feature,
                    'direction': direction, 'classification': 'review_candidate',
                    'peer_support': support, 'eligible_peers': len(aligned), 'prevalence': prevalence,
                    'outlier_score': prevalence if direction == 'not_observed_in_target' else 1 - prevalence,
                    'target_evidence': ours['evidence'],
                    'peer_evidence': [{'peer': i, 'source_digest': provenance['source_digest'],
                        'source_url': provenance.get('source_url'), 'revision': provenance.get('revision'),
                        'observed': feature in shape['features'],
                        'evidence': shape['feature_evidence'].get(feature, [shape['evidence']])}
                        for i, shape, provenance in aligned],
                    'caveat': 'Observation difference only; inspect call chains, middleware, specs and extraction coverage.'})
    return {'schema': 'python-differential-review/1.0',
            'findings': sorted(findings, key=lambda f: (-f['outlier_score'], f['capability'], f['feature'])),
            'coverage': coverage, 'excluded_peers': excluded, 'ambiguous_target_capabilities': ambiguous,
            'ambiguous_peer_capabilities': [{'peer': i, 'keys': d} for i, _, d, _ in unique if d],
            'alignment_policy': 'exact_key_including_heuristics' if allow_inferred else 'explicit_only',
            'note': 'No aligned vote is not a clean audit. Consensus is not a correctness oracle.'}


def query_graph(graph, command, ref, other=None, rels=None, depth=1):
    validate_graph(graph)
    nodes = {n['id']: n for n in graph['nodes']}
    def hits(value):
        if value in nodes:
            return [value]
        exact = sorted(nid for nid, n in nodes.items() if value in (n.get('label'), n.get('qualname'), n.get('capability', {}).get('key')))
        return exact or sorted(nid for nid, n in nodes.items() if value.lower() in nid.lower())
    matches = hits(ref)
    if command == 'find':
        return {'query': ref, 'matches': matches}
    if len(matches) != 1:
        raise ValueError(f'Reference must resolve uniquely: {ref!r}; candidates={matches}')
    start = matches[0]
    if command == 'seam':
        return {'node': nodes[start], 'edges': [e for e in graph['edges'] if e['from'] == start or e['to'] == start]}
    if command == 'where':
        return {'node': start, 'evidence': nodes[start].get('evidence')}
    outgoing, incoming = collections.defaultdict(set), collections.defaultdict(set)
    for edge in graph['edges']:
        if not rels or edge['rel'] in rels:
            outgoing[edge['from']].add(edge['to'])
            incoming[edge['to']].add(edge['from'])
    if command == 'path':
        ends = hits(other or '')
        if len(ends) != 1:
            raise ValueError(f'Destination must resolve uniquely: {other!r}; candidates={ends}')
        end = ends[0]
        queue, parent = collections.deque([start]), {start: None}
        while queue:
            current = queue.popleft()
            if current == end:
                result = []
                while current is not None:
                    result.append(current)
                    current = parent[current]
                return {'from': start, 'to': end, 'path': result[::-1]}
            for nxt in sorted(outgoing[current]):
                if nxt not in parent:
                    parent[nxt] = current
                    queue.append(nxt)
        return {'from': start, 'to': end, 'path': None}
    adjacency = incoming if command in ('who', 'blast') else outgoing
    seen, frontier, levels = {start}, {start}, []
    for _ in range(max(0, depth)):
        nxt = set().union(*(adjacency[n] for n in frontier)) - seen
        if not nxt:
            break
        levels.append(sorted(nxt))
        seen.update(nxt)
        frontier = nxt
    return {'node': start, 'levels': levels, 'count': len(seen) - 1}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents partially written graphs after interruptions.
    import tempfile
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
            handle.write(_json(value))
        temp.replace(path)
    finally:
        if temp and temp.exists():
            temp.unlink()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ['--self-test']:
        return self_test()
    legacy = {'atlas': atlas_main, 'legacy-query': cgraph_main, 'legacy-surface': surface_main}
    if argv and argv[0] in legacy:
        return legacy[argv[0]](argv[1:]) or 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    extract = sub.add_parser('extract', help='Extract a rich static Python evidence graph')
    extract.add_argument('--root', default='.')
    extract.add_argument('--pkg', action='append', dest='packages')
    extract.add_argument('--out', required=True)
    extract.add_argument('--capabilities', help='JSON map of path:qualname to semantic capability')
    extract.add_argument('--rules', help='JSON map of effect tags to call-name glob patterns')
    extract.add_argument('--exclude', action='append', default=[])
    extract.add_argument('--revision')
    extract.add_argument('--source-url')
    extract.add_argument('--dry-run', action='store_true')
    query = sub.add_parser('query', help='Query typed edges and code evidence')
    query.add_argument('graph')
    query.add_argument('action', choices=['find', 'where', 'deps', 'who', 'blast', 'seam', 'path'])
    query.add_argument('ref')
    query.add_argument('other', nargs='?')
    query.add_argument('--rel', action='append')
    query.add_argument('--depth', type=int, default=1)
    compare = sub.add_parser('compare', help='Compare explicitly aligned capabilities')
    compare.add_argument('target')
    compare.add_argument('peers', nargs='+')
    compare.add_argument('--out')
    compare.add_argument('--threshold', type=float, default=.8)
    compare.add_argument('--min-peers', type=int, default=2)
    compare.add_argument('--allow-inferred', action='store_true')
    args = parser.parse_args(argv)
    def read(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))
    try:
        if args.command == 'extract':
            result = extract_python(args.root, packages=args.packages,
                capabilities=read(args.capabilities) if args.capabilities else None,
                rules=read(args.rules) if args.rules else None,
                excludes=(*DEFAULT_EXCLUDES, *args.exclude), revision=args.revision, source_url=args.source_url)
            if not args.dry_run:
                _write_json(args.out, result)
            print(_json(result['summary']), end='')
            return 1 if result['summary']['unparseable_files'] else 0
        if args.command == 'query':
            result = query_graph(read(args.graph), args.action, args.ref, args.other, args.rel, args.depth)
        else:
            result = compare_graphs(read(args.target), [read(p) for p in args.peers], args.threshold, args.min_peers, args.allow_inferred)
        if getattr(args, 'out', None):
            _write_json(args.out, result)
        else:
            print(_json(result), end='')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


def self_test():
    """Run 17 embedded regression fixtures without third-party dependencies."""
    import unittest
    import tempfile
    dg = sys.modules[__name__]
    class GraphTests(unittest.TestCase):
    
        def graph(self, files, capabilities=None):
            with tempfile.TemporaryDirectory() as tmp:
                for p, content in files.items():
                    path = Path(tmp, p)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                return dg.extract_python(tmp, capabilities=capabilities)
    
        def test_relative_and_alias(self):
            g = self.graph({'pkg/__init__.py': 'from .worker import run\ndef main(): return run()\n', 'pkg/worker.py': 'def run(): return 1\n', 'other.py': 'from pkg.worker import run as execute\ndef call(): return execute()\n'})
            calls = [e for e in g['edges'] if e['rel'] == 'calls']
            self.assertEqual(len(calls), 2)
            self.assertTrue(all((e['to'] == 'symbol:pkg/worker.py:run' for e in calls)))
    
        def test_nested_scope(self):
            g = self.graph({'a.py': 'def x():\n def child():\n  dangerous()\n return 1\n'})
            self.assertFalse(any((e['rel'] == 'calls' and e['from'] == 'symbol:a.py:x' for e in g['edges'])))
            self.assertTrue(any((e['rel'] == 'calls' and e['from'] == 'symbol:a.py:x.child' for e in g['edges'])))
    
        def test_shadowing(self):
            g = self.graph({'a.py': 'def target(): pass\ndef f(target): return target()\ndef g():\n target = unknown\n return target()\n'})
            calls = [e for e in g['edges'] if e['rel'] == 'calls']
            self.assertTrue(all((e['resolution'] == 'local_or_rebound' for e in calls)))
    
        def test_class_scope(self):
            g = self.graph({'a.py': 'class C:\n def helper(self): pass\n def go(self):\n  self.helper()\n  helper()\n'})
            calls = [e for e in g['edges'] if e['rel'] == 'calls']
            self.assertTrue(any((e['to'] == 'symbol:a.py:C.helper' for e in calls)))
            self.assertTrue(any((e['resolution'] == 'unresolved' for e in calls)))
    
        def test_contract(self):
            g = self.graph({'a.py': 'async def f(a:int, /, b=2, *args, c:str, d=None, **kw)->dict:\n return {"x":a}\n'})
            f = next((n for n in g['nodes'] if n['id'] == 'symbol:a.py:f'))
            p = f['contract']['parameters']
            self.assertEqual(len(p), 6)
            self.assertEqual(p[0]['kind'], 'positional_only')
            self.assertTrue(p[3]['required'])
            self.assertFalse(p[4]['required'])
            self.assertTrue(f['async'])
    
        def test_behavior(self):
            code = "@app.post('/orders/{order_id}')\nasync def create(order_id: int, payload):\n if not payload:\n  raise ValueError('bad')\n with db.transaction():\n  validate(payload)\n  await db.save(payload)\n try:\n  return payload['id']\n except KeyError:\n  return None\n"
            g = self.graph({'a.py': code})
            rels = {e['rel'] for e in g['edges']}
            self.assertTrue({'branches', 'raises', 'uses_context', 'awaits', 'exposes', 'handles', 'returns', 'reads'} <= rels)
            self.assertEqual(g['capabilities'][0]['key'], 'http:POST:/orders/{}')
            self.assertIn('tag:validation_candidate', g['capabilities'][0]['features'])
            self.assertTrue(dg.validate_graph(g))
    
        def test_parse_error(self):
            g = self.graph({'bad.py': 'def ???', 'good.py': 'def ok(): pass'})
            self.assertEqual(g['summary']['unparseable_files'], 1)
            self.assertEqual(g['summary']['parsed_files'], 1)
    
        def test_determinism(self):
            files = {'a.py': 'def f(x):\n if x: return x\n return 0\n'}
            self.assertEqual(dg._json(self.graph(files)), dg._json(self.graph(files)))
    
        def test_compare(self):
            mapping = {'a.py:save': 'order.create'}
            ours = self.graph({'a.py': 'def save(x): return x\n'}, mapping)
            p1 = self.graph({'a.py': 'def save(x):\n validate(x)\n return x\n'}, mapping)
            p2 = self.graph({'a.py': 'def save(x):\n validate(x)\n return x # different\n'}, mapping)
            result = dg.compare_graphs(ours, [p1, p2])
            hit = next((f for f in result['findings'] if f['feature'] == 'tag:validation_candidate'))
            self.assertEqual(hit['peer_support'], 2)
            self.assertEqual(hit['classification'], 'review_candidate')
            self.assertFalse(dg.compare_graphs(ours, [p1, p1])['findings'])
    
        def test_alignment_abstains(self):
            g = self.graph({'a.py': 'def f(): pass'})
            self.assertFalse(dg.compare_graphs(g, [g])['coverage'])
            g = self.graph({'a.py': 'def f(): pass\ndef h(): pass'}, {'a.py:f': 'same', 'a.py:h': 'same'})
            self.assertEqual(dg.compare_graphs(g, [])['ambiguous_target_capabilities'], ['same'])
    
        def test_query(self):
            g = self.graph({'a.py': 'def b(): pass\ndef a(): b()'})
            self.assertEqual(dg.query_graph(g, 'path', 'symbol:a.py:a', 'symbol:a.py:b', ['calls'])['path'], ['symbol:a.py:a', 'symbol:a.py:b'])
            self.assertEqual(dg.query_graph(g, 'who', 'symbol:a.py:b', rels=['calls'])['count'], 1)
    
        def test_duplicate_definitions(self):
            g = self.graph({'a.py': 'def a(): pass\ndef a(): pass\ndef f(): a()'})
            self.assertEqual(len([e for e in g['edges'] if e['rel'] == 'calls']), 2)
            dg.validate_graph(g)
    
        def test_no_execution(self):
            g = self.graph({'a.py': 'raise RuntimeError("must not execute")\ndef f(): pass'})
            self.assertEqual(g['summary']['parsed_files'], 1)
    
        def test_external_alias(self):
            g = self.graph({'a.py': 'import requests as r\ndef f(): return r.post("https://example.test")'})
            e = next((e for e in g['edges'] if e['rel'] == 'calls'))
            self.assertEqual(e['to'], 'external:requests.post')
            self.assertIn('network_candidate', e['tags'])
    
        def test_predicate_shape(self):
            self.assertEqual(dg.normalized_expression(dg.ast.parse('x > 0', mode='eval').body), dg.normalized_expression(dg.ast.parse('amount > 0', mode='eval').body))
            self.assertNotEqual(dg.normalized_expression(dg.ast.parse('x > 0', mode='eval').body), dg.normalized_expression(dg.ast.parse('x >= 0', mode='eval').body))
    
        def test_fields_and_parameter_edges(self):
            g = self.graph({'a.py': 'class Order:\n quantity: int = 0\ndef f(payload): return payload["id"]\n'})
            self.assertTrue(any((n['type'] == 'field' and n['annotation'] == 'int' for n in g['nodes'])))
            self.assertTrue(any((e['rel'] == 'references_parameter' for e in g['edges'])))
    
        def test_legacy_atlas(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'pkg').mkdir()
                (root / 'pkg/__init__.py').write_text('from . import worker')
                (root / 'pkg/worker.py').write_text('def f(): pass')
                cards, meta = dg.build_atlas(root, ['pkg'])
                commands = dg.surface_commands(root, cards)
                dg.write_atlas(root / 'docs/c-atlas', cards, meta, commands)
                g = dg.load_graph(root / 'docs/c-atlas/define')
                self.assertEqual(dg.where(g, 'f')['defined_in'], ['pkg-worker'])
                self.assertTrue(dg.resolve(g, 'pkg/worker.py'))
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GraphTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
