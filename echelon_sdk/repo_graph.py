"""Repo structure graph — read a codebase by its STRUCTURE, not by cat-ing every file.

A reclaim of AIFACTOR's graph_builder (D:\\WORK\\AIFACTOR/src/aifactor/graph_builder.py — the repo
the owner burned ~180M Gemini tokens building). AIFACTOR's insight, earned at scale: an agent should
NOT brute-read 270 files to understand a repo — the substrate mechanically extracts the structure
(every file, every function signature, every import edge) ONCE into a graph, the agent reads the
GRAPH (small, queryable), then read_file's ONLY the spots the graph points it to. "Substrate over the
brain": feed the LLM pre-structured context, don't make it summarise file-dumps (the summariser tax
that bottlenecked the Epsilon-Co audit — see memory summariser-is-symptom-of-brute-reading).

RECLAIM, NOT COPY (reclaim-the-method): AIFACTOR used libcst + networkx (it needed lossless round-trip
for its EDIT/minify path). For READING structure, Python's stdlib `ast` is sufficient and adds ZERO
dependencies (the agent's floor stays clean). Same DECISION (extract a structural graph), re-implemented
on our discipline. We emit a flat, grep-friendly outline instead of a networkx blob — the agent reads
it as text, the way it reads everything.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileEntry:
    path: str                       # repo-relative
    imports: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)  # "name(args) -> ret  [risk markers]"
    loc: int = 0
    bytes: int = 0                  # file size in BYTES (the truth — LOC lies for one-line monsters)
    max_line: int = 0               # longest single line (flags minified/generated files)
    error: str | None = None        # parse failure / size-skip (recorded, not fatal)


_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".flux",
              ".pytest_cache", ".mypy_cache", "dist", "build", ".egg-info",
              ".aifactor", ".gemini", "site-packages"}

# Guards (owner 2026-06-06: "LOC != peek — a single line of 100MB is still 1 LOC").
_MAX_FILE_BYTES = 512 * 1024   # skip files over 512KB unparsed — past this it's data/generated, not
                               # audit-relevant source, and reading it whole would bloat/hang.
_MAX_SIG_CHARS = 200           # truncate any single signature/class/import line — one fat default arg
                               # or import can't bloat the outline. (A 100k-char default never lands.)
_LONG_LINE_FLAG = 2000         # a file whose longest line exceeds this is flagged minified/generated.


def _ann(node) -> str:
    """Best-effort render of a type annotation (ast.unparse is 3.9+, always present here)."""
    try:
        return ast.unparse(node) if node is not None else ""
    except Exception:
        return ""


def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in fn.args.args]
    if fn.args.vararg:
        args.append("*" + fn.args.vararg.arg)
    if fn.args.kwarg:
        args.append("**" + fn.args.kwarg.arg)
    ret = _ann(fn.returns)
    sig = f"{fn.name}({', '.join(args)})" + (f" -> {ret}" if ret else "")
    # Risk markers an auditor cares about, extracted mechanically.
    markers = []
    if any(isinstance(n, (ast.Try,)) for n in ast.walk(fn)):
        markers.append("try")
    if isinstance(fn, ast.AsyncFunctionDef):
        markers.append("async")
    if any(isinstance(d, ast.Name) and d.id in ("property", "staticmethod", "classmethod")
           for d in fn.decorator_list):
        markers.append("@" + next(d.id for d in fn.decorator_list
                                   if isinstance(d, ast.Name)))
    return sig + (f"  [{','.join(markers)}]" if markers else "")


def _imports(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            level = "." * (node.level or 0)
            out.extend(f"{level}{mod}.{a.name}" if mod else f"{level}{a.name}"
                       for a in node.names)
    return out


def _parse_file(abspath: Path, relpath: str) -> FileEntry:
    e = FileEntry(path=relpath)
    # SIZE GUARD (owner 2026-06-06): LOC != size. A single line can be 100MB (minified JS, a giant
    # data literal, a base64 blob) and read as "1 LOC". Check BYTES before reading the file at all —
    # a huge file must never be slurped into memory or fed to ast.parse (it'd OOM/hang). Flag + skip.
    try:
        e.bytes = abspath.stat().st_size
    except OSError as ex:
        e.error = f"stat failed: {ex}"
        return e
    if e.bytes > _MAX_FILE_BYTES:
        e.error = f"skipped — {e.bytes // 1024}KB exceeds {_MAX_FILE_BYTES // 1024}KB cap (not parsed)"
        return e
    try:
        src = abspath.read_text(encoding="utf-8", errors="replace")
        e.loc = src.count("\n") + 1
        # LONG-LINE GUARD: even under the byte cap, a file can be one giant line. Record the longest
        # line so the map can flag a "structurally weird" file (minified/generated) the auditor should
        # not try to read whole.
        e.max_line = max((len(ln) for ln in src.splitlines()), default=0)
        tree = ast.parse(src, filename=relpath)
    except SyntaxError as ex:
        e.error = f"SyntaxError: {ex.msg} (line {ex.lineno})"
        return e
    except Exception as ex:  # noqa: BLE001
        e.error = f"{type(ex).__name__}: {ex}"
        return e
    e.imports = [_clip(i) for i in _imports(tree)]
    # Only TOP-LEVEL defs + class methods (one level) — the audit-relevant surface, not every nested helper.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            e.functions.append(_clip(_sig(node)))
        elif isinstance(node, ast.ClassDef):
            methods = [_sig(m) for m in node.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            bases = ", ".join(_ann(b) for b in node.bases)
            e.classes.append(_clip(f"{node.name}({bases})" if bases else node.name))
            e.functions.extend(_clip(f"{node.name}.{m}") for m in methods)
    return e


def _clip(s: str) -> str:
    """Bound any single outline entry — one fat default arg / giant import can't bloat the map."""
    return s if len(s) <= _MAX_SIG_CHARS else s[:_MAX_SIG_CHARS] + " …[clipped]"


def build_graph(root: str | Path, *, max_files: int = 4000) -> dict:
    """Walk a repo's .py files and extract a structural graph. Mechanical, no LLM.
    Returns {root, files:[FileEntry...], stats:{...}}."""
    root = Path(root).resolve()
    files: list[FileEntry] = []
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if n >= max_files:
                break
            ab = Path(dirpath) / fn
            rel = str(ab.relative_to(root)).replace("\\", "/")
            files.append(_parse_file(ab, rel))
            n += 1
    stats = {
        "files": len(files),
        "functions": sum(len(f.functions) for f in files),
        "classes": sum(len(f.classes) for f in files),
        "parse_errors": sum(1 for f in files if f.error),
        "total_loc": sum(f.loc for f in files),
        "total_bytes": sum(f.bytes for f in files),
        "skipped_big": sum(1 for f in files if f.error and "exceeds" in (f.error or "")),
    }
    return {"root": str(root), "files": files, "stats": stats}


def render_outline(graph: dict, *, signatures: bool = True, max_chars: int = 14000) -> str:
    """Render the graph as a flat, grep-friendly text outline the agent reads instead of the files.
    Bounded by max_chars — a big repo gets the structural spine, not an unbounded dump."""
    g = graph
    st = g["stats"]
    mb = st.get("total_bytes", 0) / (1024 * 1024)
    lines = [f"REPO STRUCTURE: {g['root']}",
             f"  {st['files']} py files, {st['functions']} functions, "
             f"{st['classes']} classes, {st['total_loc']} LOC, {mb:.1f}MB"
             + (f", {st['parse_errors']} parse errors" if st['parse_errors'] else "")
             + (f", {st['skipped_big']} skipped (too big)" if st.get('skipped_big') else ""),
             ""]
    for f in g["files"]:
        # Show SIZE not just LOC (owner: LOC lies — a 1-line file can be 100MB). Flag minified files.
        size = f"{f.bytes // 1024}KB" if f.bytes >= 1024 else f"{f.bytes}B"
        head = f"{f.path}  ({f.loc} LOC, {size})"
        if f.max_line > _LONG_LINE_FLAG:
            head += f"  ⚠minified? longest line {f.max_line} chars — do NOT read whole"
        if f.error:
            lines.append(f"{head}  !! {f.error}")
            continue
        lines.append(head)
        if f.imports:
            imps = ", ".join(f.imports[:12]) + (" ..." if len(f.imports) > 12 else "")
            lines.append(f"  imports: {imps}")
        if signatures:
            for c in f.classes:
                lines.append(f"  class {c}")
            for fn in f.functions:
                lines.append(f"  def {fn}")
        elif f.classes or f.functions:
            lines.append(f"  {len(f.classes)} classes, {len(f.functions)} funcs")
        lines.append("")
        if sum(len(x) for x in lines) > max_chars:
            lines.append(f"... [outline truncated at {max_chars} chars — query a subtree with a path prefix]")
            break
    return "\n".join(lines)
