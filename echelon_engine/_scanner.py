"""_scanner.py — the architecture LAW, enforced at import time.

Importing `echelon_engine` runs validate_chains(); a violation raises ScannerError
and the app will not start. This is intentional (the AlphaApp/Mol discipline): a
broken layer boundary is a boot failure, not a lint warning that rots.

THE LAYERS (dependency flows downward only):
    apps/          -> echelon_engine.services (the GATE only)
       |
    echelon_engine -> boards -> atoms -> echelon_sdk
       |
    echelon_sdk    -> stdlib only (the pure library root)

THE LAWS (this first version enforces the load-bearing ones; more are documented
as TODO and harden as the migration lands real modules):

  1. IMPORT DIRECTION (the spine):
     - echelon_sdk MUST NOT import echelon_engine or apps.
     - echelon_engine.atoms MUST NOT import boards / services / apps.
     - apps MUST NOT import echelon_engine.atoms directly (go through services gate).
     - nothing outside echelon_engine may import a private service child
       (services_*.py) — import echelon_engine.services (the gate).
  2. CHAIN SHAPE (from the canonical framework):
     - a chain must start with ChainResult.of(...)
     - straight-line only (no chain construction inside if/for/while/with)
     - step names unique within a chain
     - .on(...) observers registered before the first .pipe(...)

TODO (next rules, ported from Mol/AlphaApp as modules land): service-gate size
caps, board naming + public-method caps, no-raw-DB outside the store atom, no
hardcoded runtime URLs/paths in active code.
"""
from __future__ import annotations

import ast
from pathlib import Path

from echelon_sdk.exceptions import ScannerError

_ROOT = Path(__file__).resolve().parent.parent   # the ECHELON-STRUCTURED/ root

# layer -> packages it MUST NOT import (the forbidden-upward map)
_FORBIDDEN_UPWARD = {
    "echelon_sdk": ("echelon_engine", "apps"),
    "atoms": ("echelon_engine.agent", "echelon_engine.boards", "echelon_engine.services", "apps"),
    "apps": ("echelon_engine.atoms",),
}

# files explicitly exempt from a rule, each WITH a reason (never bypass silently)
_SCAN_SKIP = {
    # path-fragment : reason
    "atoms/scribe.py": "scribe() takes an injectable dispatch_fn; its default lazy-imports "
                       "agent.partner.dispatch — the ONE sanctioned atoms→agent edge, kept lazy "
                       "(function-local) so import-time stays acyclic. Callers that pass dispatch_fn "
                       "never touch the edge.",
}


# ── chain-shape validation (canonical) ──────────────────────────────────────
def _extract_chain_calls(node):
    """Collect the attribute-call names in a ChainResult.of(...).pipe(...) chain."""
    calls = []
    cur = node
    while isinstance(cur, ast.Call):
        f = cur.func
        if isinstance(f, ast.Attribute):
            calls.append((f.attr, cur))
            cur = f.value
        else:
            break
    calls.reverse()
    return calls


def _is_chain_root(expr) -> bool:
    cur = expr
    while isinstance(cur, ast.Call):
        f = cur.func
        if isinstance(f, ast.Attribute):
            cur = f.value
            continue
        break
    # root is ChainResult.of -> the innermost call's func is Attribute .of on Name ChainResult
    inner = expr
    while isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
        if isinstance(inner.func.value, ast.Call):
            inner = inner.func.value
        else:
            base = inner.func.value
            return (isinstance(inner.func, ast.Attribute)
                    and isinstance(base, ast.Name) and base.id == "ChainResult")
    return False


def _validate_chain_expr(expr, filepath, lineno, errors):
    calls = _extract_chain_calls(expr)
    if not calls:
        return
    names = [c[0] for c in calls]
    if "pipe" not in names and "where" not in names:
        return  # not a real multi-step chain
    if not _is_chain_root(expr):
        errors.append(f"chain at {filepath}:{lineno} does not start with ChainResult.of(...)")
    # unique pipe step names
    seen = set()
    first_pipe = None
    last_on = None
    for i, (attr, call) in enumerate(calls):
        if attr == "pipe":
            if first_pipe is None:
                first_pipe = i
            nm = _step_name(call)
            if nm in seen:
                errors.append(f"duplicate pipe step name '{nm}' at {filepath}:{lineno}")
            seen.add(nm)
        if attr == "on":
            last_on = i
    if first_pipe is not None and last_on is not None and last_on > first_pipe:
        errors.append(f".on() observer registered after .pipe() at {filepath}:{lineno}")


def _step_name(call):
    for kw in call.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    if call.args and isinstance(call.args[0], (ast.Name, ast.Attribute)):
        a = call.args[0]
        return a.id if isinstance(a, ast.Name) else a.attr
    return "step"


def _inside_control(node, parents) -> bool:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            return True
        cur = parents.get(cur)
    return False


# ── import-direction validation (the spine) ─────────────────────────────────
def _layer_of(rel_path: str) -> str | None:
    p = rel_path.replace("\\", "/")
    if p.startswith("echelon_sdk/"):
        return "echelon_sdk"
    if p.startswith("echelon_engine/atoms/"):
        return "atoms"
    if p.startswith("apps/"):
        return "apps"
    if p.startswith("echelon_engine/"):
        return "echelon_engine"
    return None


def _scan_file_imports(filepath: Path, errors: list):
    rel = str(filepath.relative_to(_ROOT))
    rel_posix = rel.replace("\\", "/")   # portable: _SCAN_SKIP keys use '/', rel is OS-native
    if any(skip in rel_posix for skip in _SCAN_SKIP):
        return
    layer = _layer_of(rel)
    if layer is None:
        return
    forbidden = _FORBIDDEN_UPWARD.get(layer, ())
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
    except Exception as exc:
        errors.append(f"parse error {rel}: {exc}")
        return
    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                _check_import(alias.name, forbidden, layer, rel, getattr(node, "lineno", 0), errors)
            continue
        if mod:
            _check_import(mod, forbidden, layer, rel, getattr(node, "lineno", 0), errors)


def _check_import(mod: str, forbidden, layer, rel, lineno, errors):
    for f in forbidden:
        if mod == f or mod.startswith(f + "."):
            errors.append(f"import-law: {layer} file {rel}:{lineno} imports {mod} (forbidden upward)")
    # service-gate: only echelon_engine.services* may import a private service child
    if "echelon_engine.services_" in mod and not rel.replace("\\", "/").startswith("echelon_engine/services"):
        errors.append(f"service-gate: {rel}:{lineno} imports private {mod} — import echelon_engine.services")


# ── ordering-law sweep: no float-st_mtime sort keys ──────────────────────────
# (2026-07-30, the list_runs flake: two files written moments apart collapse into
# one float-second timestamp, so sorted(key=...st_mtime) has UNDEFINED order on a
# same-tick tie — green in one tree, flaky in another. The law: a sort key uses
# st_mtime_ns plus a stable tie-break, never the float.)
def _scan_file_mtime_sorts(filepath: Path, errors: list):
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
    except Exception:
        return
    rel = str(filepath.relative_to(_ROOT))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_sorted = isinstance(fn, ast.Name) and fn.id == "sorted"
        is_sort = isinstance(fn, ast.Attribute) and fn.attr == "sort"
        if not (is_sorted or is_sort):
            continue
        for kw in node.keywords:
            if kw.arg != "key":
                continue
            for n in ast.walk(kw.value):
                if isinstance(n, ast.Attribute) and n.attr == "st_mtime":
                    errors.append(
                        f"ordering-law: {rel}:{node.lineno} sorts by float st_mtime — "
                        "same-tick ties are undefined; use st_mtime_ns with a stable tie-break")


# ── chain-shape sweep over a file ────────────────────────────────────────────
def _scan_file_chains(filepath: Path, errors: list):
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
    except Exception:
        return
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    rel = str(filepath.relative_to(_ROOT))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.Return, ast.Expr)):
            val = node.value
            if isinstance(val, ast.Call) and _mentions_chainresult(val):
                if _inside_control(node, parents):
                    errors.append(f"chain built inside control flow at {rel}:{getattr(node,'lineno',0)} "
                                  "(chains must be straight-line)")
                _validate_chain_expr(val, rel, getattr(node, "lineno", 0), errors)


# ── multi-tenant path-containment law (2026-08-15) ──────────────────────────
# An MCP tool handler that reads a CALLER-SUPPLIED PATH must contain it. Binding the
# request to a per-user home does NOT contain it: ECHELON_HOME decides where the bank
# is WRITTEN, never what a filesystem read can REACH. Four adversarial gate rounds found
# NINE uncontained handlers in mcp_server.py — the last (`wrap`'s spec_path) invisible to
# both schema and parameter-name audits. This makes the class AST-detectable so it cannot
# come back silently. See atom [[destination-is-not-source-contain-caller-paths]].
_PATH_ARG_NAMES = frozenset({
    "root", "target", "spec_path", "from_bank", "to_bank", "files", "out", "folder",
})
_CONTAINERS = frozenset({"_contain_root", "_contain_write_path"})


def _scan_file_path_containment(filepath: Path, errors: list):
    """In mcp_server.py, a handler reading a path-shaped caller arg must contain it."""
    if filepath.name != "mcp_server.py":
        return
    rel = filepath.relative_to(_ROOT).as_posix()
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return          # the import scan already reports parse errors
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_t_"):
            continue
        # Which path-shaped caller args does this handler read, and is EACH READ
        # itself wrapped in a container? Checking only that the FUNCTION mentions a
        # container (or _is_owner) is too weak — `_t_ingest` branches on _is_owner for
        # its scope logic while leaving the path raw, which is exactly the shipped bug.
        # So the check is per-READ: every path-arg read must sit inside a container call.
        contained_reads, raw_reads = set(), set()

        def _read_key(sub):
            if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) and sub.value.id == "a":
                sl = sub.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    return sl.value
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get" and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "a" and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)):
                return sub.args[0].value
            return None

        # reads that appear ANYWHERE inside a _contain_* call are contained
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id in _CONTAINERS):
                for inner in ast.walk(sub):
                    k = _read_key(inner)
                    if k in _PATH_ARG_NAMES:
                        contained_reads.add(k)
        for sub in ast.walk(node):
            k = _read_key(sub)
            if k in _PATH_ARG_NAMES:
                raw_reads.add(k)

        # A read may reach a container INDIRECTLY and still be correct — three shapes
        # all present in real handlers, none of which a per-read check sees:
        #   root = a.get("root"); root = _contain_root(root)     (via a local)
        #   [_contain_root(f) for f in a["files"]]               (via a comprehension var)
        #   if _is_owner(): raw ... else: <forced to own home>   (owner-only branch)
        # A scanner that flags correct code trains people to ignore it, so the rule is
        # deliberately conservative: it fires only when the handler NEVER contains and
        # NEVER branches on ownership — i.e. the shipped-bug shape.
        missing = raw_reads - contained_reads
        if not missing:
            continue
        src = ast.dump(node)
        if any(c in src for c in _CONTAINERS):
            continue          # containment happens somewhere in this handler
        if "_is_owner" in src and "_REQ" in src:
            # KNOWN LIMIT (stated, not hidden): a handler that branches on ownership for
            # some OTHER reason (e.g. forcing scope) is exempted even if its path stays
            # raw — verified by reverting the shipped _t_ingest bug, which this rule does
            # NOT catch while _t_scribe's does fire. The alternative (flagging every
            # owner-aware handler) cries wolf on correct code, which is worse: a scanner
            # people learn to ignore protects nothing. The reflex + the test suite cover
            # the residue; this rule catches the never-contains shape.
            continue
        errors.append(
            f"path-containment: {rel} handler {node.name}() reads caller path arg(s) "
            f"{sorted(missing)} with NO _contain_root/_contain_write_path anywhere — "
            f"a per-user home bounds the WRITE, not the READ")


def _mentions_chainresult(expr) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Name) and n.id == "ChainResult":
            return True
    return False


def _py_files():
    for sub in ("echelon_sdk", "echelon_engine", "apps"):
        base = _ROOT / sub
        if base.exists():
            yield from base.rglob("*.py")


def _cache_path() -> Path:
    import os
    home = os.environ.get("ECHELON_HOME") or str(Path.home() / ".echelon")
    return Path(home) / "scanner_ok.json"


def _tree_signature(files) -> str:
    """One digest over (relative path, mtime_ns, size) of every scanned file: any edit, add,
    delete or rename changes it, so a cache hit means 'this exact tree already passed'."""
    import hashlib
    h = hashlib.sha1()
    for f in sorted(files):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f.relative_to(_ROOT).as_posix()}|{st.st_mtime_ns}|{st.st_size}\n".encode("utf-8"))
    return h.hexdigest()


def validate_chains():
    """The boot-time gate. Raises ScannerError on any architecture-law violation.

    CACHED BY TREE SIGNATURE (owner 2026-09-05, "what makes the hook take more than 12s?"):
    the full AST scan of ~350 files costs 4-6 s and ran on EVERY `import echelon_engine` -
    every hook call, every CLI verb - while the tree had not changed since the last green
    scan. The gate is unchanged in strength: the signature covers every file's path, mtime
    and size, so ANY edit rescans; only a byte-for-byte identical tree skips. A failed scan
    never writes the cache. ECHELON_SCANNER_FORCE=1 bypasses the cache."""
    import os
    files = [f for f in _py_files() if "__pycache__" not in str(f)]
    sig = _tree_signature(files)
    cache = _cache_path()
    if os.environ.get("ECHELON_SCANNER_FORCE", "") not in ("1", "true", "on"):
        try:
            import json
            if json.loads(cache.read_text(encoding="utf-8")).get("sig") == sig:
                return True
        except (OSError, ValueError):
            pass
    errors: list[str] = []
    for f in files:
        _scan_file_imports(f, errors)
        _scan_file_chains(f, errors)
        _scan_file_mtime_sorts(f, errors)
        _scan_file_path_containment(f, errors)
    if errors:
        msg = "\n  ".join(errors)
        raise ScannerError(f"architecture-law scan failed ({len(errors)} error(s)):\n  {msg}")
    try:
        import json, time
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"sig": sig, "files": len(files), "ts": int(time.time())}), encoding="utf-8")
    except OSError:
        pass   # a cache that cannot be written just means the next import scans again
    return True


if __name__ == "__main__":
    try:
        validate_chains()
        print("scanner: OK (no architecture-law violations)")
    except ScannerError as e:
        print(str(e))
        raise SystemExit(1)
