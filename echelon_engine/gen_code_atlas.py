"""gen_code_atlas — DETERMINISTIC code-atlas generator for a Python engine ($0, no LLM).

THE GAP THIS CLOSES (found 2026-07-22): the ECHELON engine has 960 soul-atoms but ZERO
code-atoms — it never mapped ITSELF. So every "what consumes X?" is a manual file sweep,
and couplings rot invisibly (the relive `born_from` filter silently broke for 10 days
because nothing surfaced that relive depended on wrap's born_from shape). A UI repo gets
`xray --atlas` (LLM lenses judging live/partial/gap); but for a PYTHON engine the STRUCTURE
is fully derivable by `ast` — module=node, import=depends_on edge, class/func+docstring=
members, package=part_of. This script emits that skeleton in the EXISTING c-atlas format
(docs/c-atlas/define/<id>.json — the shape xray.atlas_scan + atlas_bridge already read), so
the engine gains a queryable self-map with no model tokens. LLM enrichment (role/risk/gap)
is a SEPARATE later pass (or run when /wrap fires) — the seam is left open, not spent now.

SOUL-NODE LAW (bank: canonical-soul-is-origin-md — "the atlas had NO soul/identity node,
the most important thing wasn't on the map"): a mechanical module walk maps every .py but
would miss WHAT MATTERS. So identity/doctrine/registry modules are tagged type='soul' and
lifted, not left as anonymous code nodes.

COMMAND-SURFACER (owner ask 2026-07-22): the walk also finds CLI verbs (argparse add_parser
/ cmd_* funcs) and classifies each by EXPOSURE — documented / undocumented / hidden (defined
+ reachable but not in --help or docs). Written to define/_commands.json so the door, a
banner, and docs can surface the ambiguous/hidden ones instead of letting them lurk.

Run:  python -X utf8 -m echelon_engine.gen_code_atlas [--root <repo>] [--out docs/c-atlas]
                 [--pkg echelon_engine --pkg echelon_sdk --pkg apps] [--plant <scope>] [--dry-run]
"""
from __future__ import annotations
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
                pkg_parts = dotted.split(".")[:-1]
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


def _main(argv=None) -> int:
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


if __name__ == "__main__":
    sys.exit(_main())
