"""propose_scan.py — in-process symbol scanner + index rows (PROPOSAL-GATE v3).

Scanner half of the proposal gate (`propose.py` is check/bind, unbuilt). Used
by check's REF-UNRESOLVED self-heal, bind's index update + REF-DECLARED-NOT-USED
(via `usages` on the diff), and `index scan` importing the os_client `_index/`.
Stdlib only (re, json, ast, difflib, argparse, pathlib). Deterministic; $0 per
run (no model call). ROW SCHEMA — index/{symbols,endpoints}.jsonl, one JSON per
line: {"v":1,"id":"<kind>:<relpath>#<name>","name","kind","path"(rel, fwd
slashes),"line","signature"(str|""),"canonical":False,"deprecated":False,
"replaced_by":None,"active_change":None,"semantic_tags":[],"domain":"",
"source":"scan"}. `source` marks scanned rows ("scan") vs imported ones
("_index/seams.json#<key>" etc.) — rescan drops the former when a symbol
vanishes, never the latter.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
from pathlib import Path

ROW_V = 1
_CURATED = ("canonical", "deprecated", "replaced_by", "active_change",
            "semantic_tags", "domain")  # preserved on upsert, never overwritten
_KEYWORDS = frozenset((
    "if for while switch catch function return typeof new do else try in of class "
    "def import from with assert not and or lambda del raise break continue case "
    "default finally yield async await let var const export require this super "
    "instanceof void delete static extends get set null true false undefined").split())
_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "route"}
_UPPER_SNAKE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _row(*, kind: str, name: str, rel: str, line: int, signature: str = "",
         source: str = "scan") -> dict:
    return {"v": ROW_V, "id": f"{kind}:{rel}#{name}", "name": name, "kind": kind,
            "path": rel, "line": line, "signature": signature, "canonical": False,
            "deprecated": False, "replaced_by": None, "active_change": None,
            "semantic_tags": [], "domain": "", "source": source}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        return Path(path).name


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue                     # a corrupt line is skipped, never fatal
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


# ── comment/string stripping ──────────────────────────────────────────────────
# Char-level machine so names inside comments, string literals and template
# literals can never be mistaken for code. `#` opens a line comment only when
# hash_comments is set (diff text may be .py or .js).
def _strip_line(line: str, state: dict, *, hash_comments: bool = False) -> str:
    out = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if state["block"]:
            if ch == "*" and i + 1 < n and line[i + 1] == "/":
                state["block"] = False
                i += 2
            else:
                out.append(" ")
                i += 1
        elif state["quote"]:
            if ch == "\\":
                out.append(" ")
                i += 2
            elif ch == state["quote"]:
                state["quote"] = ""
                out.append(" ")
                i += 1
            else:
                out.append(" ")
                i += 1
        elif ch in ("'", '"', "`"):
            state["quote"] = ch
            out.append(" ")
            i += 1
        elif ch == "/" and i + 1 < n and line[i + 1] in "/*":
            if line[i + 1] == "/":
                break
            state["block"] = True
            out.append(" ")
            i += 2
        elif hash_comments and ch == "#":
            break
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _scan_js(text: str, rel: str) -> list[dict]:
    # .js scan — names matched ONLY on stripped lines (comments/strings blanked)
    rows = []
    add = lambda kind, name, line, sig="": rows.append(  # noqa: E731
        _row(kind=kind, name=name, rel=rel, line=line, signature=sig))
    state = {"block": False, "quote": ""}
    js_re = {
        "function": re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"),
        "arrow": re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                            r"(?:async\s+)?(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))\s*=>"),
        "class": re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)"),
        "const": re.compile(r"\b(?:const|let|var)\s+([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)\s*="),
        "method": re.compile(r"^\s{0,4}([A-Za-z_$][\w$]*)\s*:\s*function\b|^\s{0,4}([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"),
        "export": re.compile(r"\bexport\s+(?:default\s+)?(?:function\s+|class\s+)?([A-Za-z_$][\w$]*)"
                             r"|\bexport\s*\{([^}]*)\}"
                             r"|module\.exports\.([A-Za-z_$][\w$]*)"
                             r"|module\.exports\s*=\s*([A-Za-z_$][\w$]*)"),
    }
    for lineno, (line, raw) in enumerate(zip(
            [_strip_line(ln.rstrip("\n"), state) for ln in text.splitlines()],
            text.splitlines()), 1):
        for m in js_re["function"].finditer(line):
            add("function", m.group(1), lineno, f"({m.group(2)})")
        for m in js_re["arrow"].finditer(line):
            add("function", m.group(1), lineno, f"({m.group(2) or m.group(3) or ''})")
        for m in js_re["class"].finditer(line):
            add("class", m.group(1), lineno)
        for m in js_re["const"].finditer(line):
            add("const", m.group(1), lineno,  # RHS from RAW line: a string value
                raw[m.end():].strip().rstrip(",;")[:120])  # is part of the signature
        if len(line) - len(line.lstrip()) <= 4:  # object-literal method rule: col <= 4
            for m in js_re["method"].finditer(line):
                name = m.group(1) or m.group(2)
                if name and name not in _KEYWORDS:
                    add("function", name, lineno)
        for m in js_re["export"].finditer(line):
            for group in m.groups():
                for name in re.findall(r"[A-Za-z_$][\w$]*", group or ""):
                    add("export", name, lineno)
    seen = {}
    for r in rows:
        seen.setdefault(r["id"], r)
    return list(seen.values())


# ── .des (forge declarative page shape) ───────────────────────────────────────
_DES_ORGAN = re.compile(r"^\s*organ\s+([A-Za-z0-9_-]+)\s*\{")


def _scan_des(text: str, rel: str) -> list[dict]:
    rows = []

    def walk(data):
        if isinstance(data, dict):
            yield data
            for value in data.values():
                yield from walk(value)
        elif isinstance(data, list):
            for item in data:
                yield from walk(item)

    head = next((ln for ln in text.splitlines() if ln.strip()), "")
    if head.lstrip().startswith("{"):          # JSON .des: nodes carry component/op
        try:
            data = json.loads(text)
        except ValueError:
            return []
        for node in walk(data):
            name = node.get("component") or node.get("op") or node.get("id")
            if isinstance(name, str):
                rows.append(_row(kind="component", name=name, rel=rel, line=0,
                                 signature=str(node.get("purpose", ""))[:120]))
    else:                                       # real estate shape: organ blocks
        for lineno, line in enumerate(text.splitlines(), 1):
            m = _DES_ORGAN.match(line)
            if m:
                rows.append(_row(kind="component", name=m.group(1), rel=rel, line=lineno))
    return rows


# ── .py via ast (never regex for defs) ────────────────────────────────────────
def _scan_py(text: str, rel: str) -> list[dict]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    rows = []
    add = lambda kind, name, line, sig="": rows.append(  # noqa: E731
        _row(kind=kind, name=name, rel=rel, line=line, signature=sig))

    def sig_of(node) -> str:
        try:
            return f"({ast.unparse(node.args)})"
        except Exception:
            return ""

    def emit(fn_node, prefix: str = "") -> None:
        # function/method row + @app|@router.<verb>("<path>") endpoint rows
        add("function", prefix + fn_node.name, fn_node.lineno, sig_of(fn_node))
        for dec in getattr(fn_node, "decorator_list", []):
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                continue
            verb = func.attr.lower()
            if verb not in _HTTP_VERBS:
                continue
            path = ""
            if dec.args and isinstance(dec.args[0], ast.Constant):
                path = str(dec.args[0].value)
            for kw in dec.keywords:
                if kw.arg == "path" and isinstance(kw.value, ast.Constant):
                    path = str(kw.value.value)
            if verb == "route":                # Flask: the methods kwarg decides
                verbs = [c.value for c in dec.keywords
                         if c.arg == "methods" and isinstance(c.value, (ast.List, ast.Tuple))
                         for c in getattr(c.value, "elts", []) if isinstance(c, ast.Constant)]
                verb = str(verbs[0]).lower() if verbs else "any"
            add("endpoint", f"{verb.upper()} {path}", dec.lineno, fn_node.name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emit(node)
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            add("class", node.name, node.lineno, f"({bases})" if bases else "")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    emit(child, f"{node.name}.")
        else:
            for target in getattr(node, "targets", []):
                if isinstance(target, ast.Name) and _UPPER_SNAKE.fullmatch(target.id):
                    try:
                        value = ast.unparse(node.value)[:120]
                    except Exception:
                        value = ""
                    add("const", target.id, node.lineno, value)
    return rows


def scan_file(path: Path, *, root: Path) -> list[dict]:
    """Deterministic symbol extraction for .py/.js/.des → index rows (see header).

    Kinds: .py — function (module defs; methods as ``Class.method``), class,
    const (UPPER_SNAKE assignment), endpoint (``@app|@router.<verb>`` decorators
    → name ``"VERB /path"``, signature = handler). .js — function (decls, arrow
    consts, object-literal methods at col ≤ 4), class, const, export. .des —
    component (one per ``organ <name> {``; JSON .des walked for component/op
    nodes). Comments and string contents are never scanned for names.
    """
    path = Path(path)
    if not path.exists():
        return []
    if not path.is_file():                       # a dir or vanished path scans to nothing
        return []                                #  (a reference may name a directory; crashing here fails the whole check)
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = _rel(path, root)
    if path.suffix.lower() == ".py":
        return _scan_py(text, rel)
    if path.suffix.lower() == ".js":
        return _scan_js(text, rel)
    if path.suffix.lower() == ".des":
        return _scan_des(text, rel)
    return []


# ── usages: identifiers a unified diff actually uses ──────────────────────────
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_ATTR = re.compile(r"\.\s*([A-Za-z_$][\w$]*)")  # decimals can't match: token starts non-digit
_IMPORT_LINE = re.compile(r"\b(?:import|require|from)\b")


def usages(diff_text: str) -> set[str]:
    """Identifier usages from a unified diff (`git diff -U0` shape).

    Takes ONLY added lines (leading ``+``, not ``+++``), strips ``//`` and ``#``
    comments and string literals, then returns every identifier token followed
    by ``(`` (call), preceded by ``.`` (attribute ref), or on an
    import/require/from line. NOT a substring grep — ``render`` never matches
    ``renderX``; names in comments or strings never appear.
    """
    found: set[str] = set()
    state = {"block": False, "quote": ""}  # ONE state across lines: a /* */ block spanning added lines must stay stripped
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = _strip_line(raw[1:], state, hash_comments=True)
        for m in _CALL.finditer(line):
            if m.group(1) not in _KEYWORDS:
                found.add(m.group(1))
        for m in _ATTR.finditer(line):
            if m.group(1) not in _KEYWORDS:
                found.add(m.group(1))
        if _IMPORT_LINE.search(line):
            for m in _IDENT.finditer(line):
                if m.group(0) not in _KEYWORDS:
                    found.add(m.group(0))
    return found


# ── rescan: upsert scanned rows, drop vanished ones ───────────────────────────
def _upsert(index_dir: Path, fname: str, new_rows: list[dict],
            scanned_paths: set[str]) -> tuple[int, int, int]:
    """Merge new_rows into index_dir/fname; rows of a scanned path absent from
    new_rows are dropped unless source != \"scan\" (imported rows survive)."""
    existing = _read_jsonl(index_dir / fname)
    new_ids = {r["id"] for r in new_rows}
    keep = []
    for old in existing:
        if old.get("id") in new_ids:
            continue                            # replaced by the merged fresh row
        if old.get("source", "scan") == "scan" and old.get("path") in scanned_paths:
            continue                            # vanished from a rescanned path
        keep.append(old)
    merged, added, updated = [], 0, 0
    for row in new_rows:
        old = next((o for o in existing if o.get("id") == row["id"]), None)
        if old is None:
            added += 1
        else:
            updated += 1
        if not old:
            merged.append(row)
            continue
        fresh = dict(row)                       # curated fields survive
        for key in _CURATED:
            if old.get(key) not in (None, [], False, ""):
                fresh[key] = old[key]
        merged.append(fresh)
    dropped = len(existing) - len(keep) - updated
    _write_jsonl(index_dir / fname, keep + merged)
    return added, updated, dropped


def rescan(index_dir: Path, paths: list[Path], *, root: Path) -> dict:
    """Upsert scan rows for `paths` into index_dir/{symbols,endpoints}.jsonl.

    Keyed by id; endpoints → endpoints.jsonl, every other kind → symbols.jsonl.
    Vanished rows of a rescanned path are dropped unless ``source != "scan"``;
    curated fields survive on matching ids. Returns {"added","updated","dropped"}.
    """
    symbols, endpoints, scanned = [], [], set()
    for path in paths:
        scanned.add(_rel(Path(path), root))
        for row in scan_file(Path(path), root=root):
            (endpoints if row["kind"] == "endpoint" else symbols).append(row)
    counts = {"added": 0, "updated": 0, "dropped": 0}
    for fname, rows in (("symbols.jsonl", symbols), ("endpoints.jsonl", endpoints)):
        a, u, d = _upsert(index_dir, fname, rows, scanned)
        counts = {k: counts[k] + v for k, v in (("added", a), ("updated", u), ("dropped", d))}
    return counts


# ── index queries ─────────────────────────────────────────────────────────────
def lookup(index_dir: Path, name: str, kind: str | None = None) -> list[dict]:
    """Exact-name rows from both index files; optional kind filter."""
    rows = _read_jsonl(index_dir / "symbols.jsonl") + _read_jsonl(index_dir / "endpoints.jsonl")
    return [r for r in rows if r.get("name") == name
            and (kind is None or r.get("kind") == kind)]


# ── lexical similarity (the gate's DUP scan; NO model call) ───────────────────
def _stem_ratio(a: str, b: str) -> float:
    def stems(name: str) -> str:
        words = " ".join(re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", name)).lower()
        return " ".join(re.findall(r"[a-z0-9]+", words.replace("_", " ")))
    sa, sb = stems(a), stems(b)
    return difflib.SequenceMatcher(None, sa, sb).ratio() if sa and sb else 0.0


def _jaccard(keywords: list[str], tags: list[str]) -> float:
    if not keywords or not tags:
        return 0.0
    a, b = set(keywords), set(tags)
    return len(a & b) / len(a | b)


def similar(index_dir: Path, name: str, kind: str, keywords: list[str],
            signature: str = "") -> list[dict]:
    """Lexical similarity for gate DUP checks → [{row, score, why}], desc.

    Formula (documented, per PROPOSAL-GATE §3 step 2):
      score = max(name_stem_similarity, keyword_jaccard_vs_semantic_tags,
                  0.25 * signature_shape_match)
              + 0.1  if the row has the same kind AND carries a domain
    name_stem_similarity = SequenceMatcher ratio over camel/snake-split lowercase
    stems; keyword_jaccard = |keywords ∩ tags| / |union|; signature_shape_match
    = 1.0 iff both signatures are non-empty with equal comma-count arity (else
    0.0). No model call — the gate stays $0 per run.
    """
    out = []
    for row in _read_jsonl(index_dir / "symbols.jsonl") + _read_jsonl(index_dir / "endpoints.jsonl"):
        parts = [(_stem_ratio(name, row.get("name", "")), "name stems"),
                 (_jaccard(keywords, row.get("semantic_tags") or []), "keywords"),
                 (0.25 * (1.0 if signature and row.get("signature")
                          and signature.count(",") == row["signature"].count(",") else 0.0),
                  "signature shape")]
        score, why = max(parts, key=lambda p: p[0])
        if row.get("kind") == kind and row.get("domain"):
            score += 0.1
            why += " + same kind/domain"
        out.append({"row": row, "score": round(score, 3),
                    "why": f"{why} {row.get('name')!r} ({row.get('path')})"})
    out.sort(key=lambda item: item["score"], reverse=True)
    return out


# ── import_index: project the os_client `_index/` as rows ─────────────────────
def import_index(index_dir: Path, os_client_index_dir: Path) -> dict:
    """Project the pilot `_index/` (seams/endpoints/atlas.json) into the index:
    endpoint rows from seams methods and endpoint surfaces; token rows (tokens.*,
    ime prefixes) and component rows (atlas_nodes cards) from atlas.json. Rows
    carry ``source: "_index/<file>#…"`` — files are never copied (INTEGRATION-
    CONTRACT §5) and imported rows survive rescan drops. {"added","updated"}.
    """
    src = Path(os_client_index_dir)
    endpoints, symbols = [], []

    def load(name):
        try:
            return json.loads((src / name).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return {}

    def seam(lst, kind, name, relfile, sig, source):
        lst.append(_row(kind=kind, name=name, rel=f"_index/{relfile}", line=0,
                        signature=sig, source=source))

    for m in (load("seams.json").get("methods") or []):
        if isinstance(m, dict) and m.get("method"):
            seam(endpoints, "endpoint", m["method"], "seams.json", str(m.get("signature", "")),
                 f"_index/seams.json#{m['method']}")
    for surface, data in (load("endpoints.json") or {}).items():
        for e in (data.get("endpoints") or []) if isinstance(data, dict) else []:
            if isinstance(e, dict) and e.get("path"):
                name = f"{str(e.get('method', 'ANY')).upper()} {e['path']}"
                seam(endpoints, "endpoint", name, "endpoints.json", str(e.get("path", "")),
                     f"_index/endpoints.json#{surface}/{name}")
    atlas = load("atlas.json")
    for section, items in (atlas.get("tokens") or {}).items():
        if section.startswith("_") or not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                sig = " ".join(str(item.get(k, "")) for k in ("css_var", "value") if item.get(k))
                seam(symbols, "token", item["name"], "atlas.json", sig,
                     f"_index/atlas.json#tokens/{section}/{item['name']}")
    nodes = atlas.get("atlas_nodes") or {}
    for card in (nodes.get("cards") or []) if isinstance(nodes, dict) else []:
        if isinstance(card, dict) and card.get("filename"):
            fname = card["filename"]
            seam(symbols, "component", fname[:-3] if fname.endswith(".md") else fname,
                 "atlas.json", str(card.get("title", "")),
                 f"_index/atlas.json#atlas_nodes/cards/{fname}")
    prefixes = (atlas.get("ime_category_prefixes") or {}).get("prefixes") or {}
    for pfx, cat in prefixes.items() if isinstance(prefixes, dict) else []:
        seam(symbols, "token", pfx, "atlas.json", str(cat),
             f"_index/atlas.json#ime_category_prefixes/{pfx}")
    counts = {"added": 0, "updated": 0}
    for fname, rows in (("symbols.jsonl", symbols), ("endpoints.jsonl", endpoints)):
        a, u, _ = _upsert(index_dir, fname, rows, set())
        counts = {"added": counts["added"] + a, "updated": counts["updated"] + u}
    return counts


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    """CLI: scan <paths…> --index <dir> --root <dir> | usages <diff-file> |
    import-index <_index dir> --index <dir> | lookup <name> [--kind K] --index <dir>"""
    parser = argparse.ArgumentParser(prog="propose_scan", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    ps_scan = sub.add_parser("scan")
    ps_scan.add_argument("paths", nargs="+")
    ps_scan.add_argument("--index", required=True, help="index dir (symbols/endpoints.jsonl)")
    ps_scan.add_argument("--root", default=".")
    sub.add_parser("usages").add_argument("diff_file")
    ps_imp = sub.add_parser("import-index")
    ps_imp.add_argument("src_index")
    ps_imp.add_argument("--index", required=True)
    ps_lk = sub.add_parser("lookup")
    ps_lk.add_argument("name")
    ps_lk.add_argument("--kind")
    ps_lk.add_argument("--index", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "scan":
        result = rescan(Path(args.index), [Path(p) for p in args.paths], root=Path(args.root))
    elif args.cmd == "usages":
        result = {"usages": sorted(usages(
            Path(args.diff_file).read_text(encoding="utf-8", errors="replace")))}
    elif args.cmd == "import-index":
        result = import_index(Path(args.index), Path(args.src_index))
    else:
        result = lookup(Path(args.index), args.name, args.kind)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
