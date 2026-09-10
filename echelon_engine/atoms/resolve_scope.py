"""resolve_scope — canonical directory-to-bank-scope resolution. ONE source of truth.

Every consumer (hooks, CLI, MCP) that needs to answer "which bank scope am I in?" calls this
module instead of duplicating the logic. The irregular mappings (dir name ≠ scope name) live
here as the ONE copy; the live bank is the authority for everything else.

  python -X utf8 -m echelon_engine resolve-scope [--cwd <path>]   # CLI verb
  from echelon_engine.atoms.resolve_scope import resolve_scope     # Python API
"""
from __future__ import annotations
import argparse
import os
import re
import sys


def _kebab(name: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", (name or "").lower())).strip("-")


# ── Irregular mappings ──────────────────────────────────────────────────────
# Directory names whose kebab does NOT equal their bank scope, because the REPO/project
# dir genuinely carries a different name than its scope. These are inherent (a repo cannot
# rename itself to match every scope). When adding a new irregular mapping, prefer renaming
# the dir to match the scope. This map should SHRINK, not grow. Verified against live bank
# scopes 2026-06-24.
#
# NOTE (OPEN-0052, 2026-09-02): the old `d-work-echelon -> echelon` entry lived here — a
# hardcoded exception for a MEMORY FOLDER that isn't co-located with its estate (the
# ~/.claude/projects/d--WORK-ECHELON dir, split 2026-07-04). That is a different KIND of
# mapping than the three above: it is not a project dir, it is a stray memory dir. The
# general rule (step 2c below) now resolves ANY declared memory dir to the scope that
# declares it in ~/.echelon/mem_dirs.json — so a split memory folder is DATA the owner
# edits, not code. Add the folder to its scope's `mem_dirs.json` list; no code exception.
#: Irregular directory -> scope aliases. Keep this EMPTY of estate-specific rows:
#: declare a project's stray memory dirs in ~/.echelon/mem_dirs.json instead (step 2c
#: below resolves those generically). The one entry here is about this engine itself.
_IRREGULAR: dict[str, str] = {
    "echelon-agent": "echelon",
}


# project-root markers: a REAL scope root (a repo / estate) carries one of these. A nested leaf
# dir (web/, mirror/, pages/, assets/) does NOT — even if a prior scope-leak minted it as a bank
# scope. This is the discriminator that lets the ancestor-walk skip a bogus leaf scope and route to
# the repo root that actually OWNS the dir.
_ROOT_MARKERS = (".git", "CLAUDE.md")


def _is_project_root(path: str) -> bool:
    return any(os.path.exists(os.path.join(path, m)) for m in _ROOT_MARKERS)


def _worktree_main(path: str) -> str | None:
    """The MAIN checkout for a git worktree dir, else None.

    A worktree's `.git` is a FILE ("gitdir: <main>/.git/worktrees/<name>"), not a
    directory. Owner #932 (2026-09-02): burn jobs running in <estate-root>/delta-shop-wt
    resolved to the leaf kebab `delta-shop-wt` — a phantom scope the registry never
    knew — so their work was receipted nowhere. A worktree is the SAME project as its
    main checkout; it resolves to the main checkout's scope, never to a scope of its own."""
    try:
        g = os.path.join(path, ".git")
        if not os.path.isfile(g):
            return None
        with open(g, encoding="utf-8", errors="replace") as f:
            line = f.read().strip()
        if not line.startswith("gitdir:"):
            return None
        gd = line[len("gitdir:"):].strip().replace("\\", "/")
        if "/.git/worktrees/" not in gd:
            return None
        return os.path.normpath(gd.split("/.git/worktrees/")[0]).replace("\\", "/")
    except OSError:
        return None


def _declared_mem_dirs(config_path: str | None = None) -> list[tuple[str, str]]:
    """The (scope, dir) pairs declared in ~/.echelon/mem_dirs.json, normalised for prefix match.

    mem_dirs.json is the ONE declaration of "which folders hold scope X's memory" ({scope:[dir,...]}).
    resolve_scope reads it as the REVERSE index: a dir that is (or is under) a declared memory dir
    resolves to the scope that declared it. This replaces the hardcoded `d-work-echelon -> echelon`
    exception (OPEN-0052) — a stray memory folder is now config the owner edits, not code.

    Returned dirs are forward-slashed and lower-cased so the caller can prefix-match a cwd."""
    import json
    from pathlib import Path
    p = Path(config_path or str(Path.home() / ".echelon" / "mem_dirs.json"))
    if not p.exists():
        return []
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[tuple[str, str]] = []
    for scope, val in cfg.items():
        dirs = [val] if isinstance(val, str) else (val or [])
        for d in dirs:
            nd = os.path.normpath(str(Path(d).expanduser())).replace("\\", "/").lower().rstrip("/")
            if nd:
                out.append((scope, nd))
    # Longest dir first so the most specific declaration wins on a prefix match.
    out.sort(key=lambda sd: len(sd[1]), reverse=True)
    return out


class UnknownScopeError(LookupError):
    """No LIVE bank scope owns this directory.

    Raised by `resolve_scope` when every resolution step misses. The old behaviour was to
    MINT the leaf kebab as a fresh scope — which is how a typo'd cd, a nested leaf dir, or a
    detached worktree silently planted atoms into a scope nobody meant to create (the
    scope-leak class this guard closes, OPEN-0036). A caller that genuinely creates a new
    estate passes `allow_new=True` — an EXPLICIT create door, never implicit."""

    def __init__(self, cwd: str, candidate: str):
        self.cwd = cwd
        self.candidate = candidate
        super().__init__(
            f"no live bank scope owns {cwd!r} (leaf kebab would be {candidate!r}). "
            f"resolve_scope fails closed: pass allow_new=True to MINT {candidate!r} as a new "
            f"scope, or run from a directory the bank already knows.")


def resolve_scope(cwd: str | None = None, *, live_scopes: set[str] | None = None,
                  allow_new: bool = False,
                  mem_dirs: list[tuple[str, str]] | None = None,
                  config_path: str | None = None) -> str:
    """Resolve a working directory to its bank scope.

    Order:
      1. Irregular map (dir-name exceptions where the REPO dir name ≠ scope)
      2. Cwd path contains a known irregular key (substring match, for nested dirs)
      2c. Cwd is (or is under) a memory dir declared in ~/.echelon/mem_dirs.json → that
          scope. This is the DECLARED-DIR rule that replaces the hardcoded stray-memory
          exception (OPEN-0052): a memory folder belongs to the scope that declares it.
      3. Leaf kebab matches a live scope AND the leaf is a real project root
      4. ANCESTOR-WALK — nearest ancestor that is BOTH a live scope and a real project root
         (skips a nested leaf like web/ or mirror/pages/ even if a scope-leak minted it as a
         bank scope; routes to the repo/estate that actually OWNS the dir)
      5. Leaf kebab matches a live scope (leaf-scope fallback, non-root — back-compat)
      6. FAIL CLOSED — raise `UnknownScopeError`.

    Step 6 used to MINT the leaf kebab as a fresh scope. That fallback is gone (OPEN-0036):
    an unknown directory now RAISES rather than silently creating a scope. Callers that
    legitimately create a new estate (`ingest` planting a brand-new project's memory/, a
    workroom being initialised) opt in with `allow_new=True`, which restores the mint as an
    explicit, auditable act.

    If live_scopes is None, it is queried from the v2 bank automatically.
    """
    cwd = (cwd or os.getcwd() or "").replace("\\", "/").lower()
    norm = os.path.normpath(cwd or "").replace("\\", "/")
    leaf = os.path.basename(norm)

    # 1) exact leaf match in irregular map
    leaf_kebab = _kebab(leaf)
    if leaf_kebab in _IRREGULAR:
        return _IRREGULAR[leaf_kebab]

    # 2) substring match of irregular keys in the full path
    for needle, scope in _IRREGULAR.items():
        if needle in cwd:
            return scope

    # 2c) DECLARED memory dir (OPEN-0052): the cwd is (or is under) a folder that
    #     mem_dirs.json declares as some scope's memory. Runs BEFORE live_scopes so a
    #     stray memory folder resolves even against an empty bank — exactly the property
    #     the old hardcoded exception had. Longest declared dir wins (most specific).
    if mem_dirs is None:
        mem_dirs = _declared_mem_dirs(config_path)
    for scope, decl in mem_dirs:
        if norm == decl or norm.startswith(decl + "/"):
            return scope

    if live_scopes is None:
        live_scopes = _load_live_scopes()

    # 2b) WORKTREE (owner #932): the nearest ancestor (or self) whose `.git` is a worktree
    #     FILE resolves to its MAIN checkout, and the resolution continues from there — a
    #     worktree never mints a scope of its own (`delta-shop-wt`).
    cur = norm
    while cur and cur != os.path.dirname(cur):
        main = _worktree_main(cur)
        if main and os.path.normpath(main).replace("\\", "/").lower() != norm.lower():
            return resolve_scope(main, live_scopes=live_scopes, allow_new=allow_new)
        if _is_project_root(cur):
            break     # a real (non-worktree) repo root above us — stop the walk
        cur = os.path.dirname(cur)

    # 3) leaf is a live scope AND a real project root → it owns itself, done.
    if leaf_kebab and leaf_kebab in live_scopes and _is_project_root(norm):
        return leaf_kebab

    # 4) ancestor-walk: the nearest ancestor that is a live scope AND a real project root is the
    #    owning scope. This is the fix for the plant scope-leak — a nested leaf (web/, mirror/,
    #    pages/) is NOT a project root, so we skip it and route to the repo/estate above it.
    parent = os.path.dirname(norm)
    while parent and parent != os.path.dirname(parent):
        anc_kebab = _kebab(os.path.basename(parent))
        if anc_kebab in _IRREGULAR:
            return _IRREGULAR[anc_kebab]
        if anc_kebab and anc_kebab in live_scopes and _is_project_root(parent):
            return anc_kebab
        parent = os.path.dirname(parent)

    # 5) leaf-scope fallback (a live leaf scope that isn't a project root, no owning ancestor found)
    if leaf_kebab and leaf_kebab in live_scopes:
        return leaf_kebab

    # 6) FAIL CLOSED — nothing live owns this dir. Minting here is what leaked scopes.
    if allow_new:
        return leaf_kebab or "echelon"
    raise UnknownScopeError(norm or (cwd or ""), leaf_kebab or "echelon")


def resolve_scope_root(cwd: str | None = None, *, live_scopes: set[str] | None = None) -> str:
    """Return the DIRECTORY that owns the resolved scope — the project/estate root, not a nested
    leaf. `resolve_scope` answers "which scope"; this answers "which dir holds that scope's
    memory/". A writer (xray --plant) uses this so atoms land in the scope-root memory/, never a
    nested web/memory or mirror/pages/memory. Falls back to the input dir if no owning root is found
    (a fresh, un-banked project is its own root — so this asks resolve_scope for the EXPLICIT
    create door, `allow_new=True`, rather than failing closed on an unbanked dir)."""
    cwd = (cwd or os.getcwd() or "").replace("\\", "/")
    norm = os.path.normpath(cwd).replace("\\", "/")
    if live_scopes is None:
        live_scopes = _load_live_scopes()
    scope = resolve_scope(norm, live_scopes=live_scopes, allow_new=True)
    # walk from the dir upward; the nearest ancestor (or self) whose kebab == the resolved scope
    # AND is a real project root is the owning directory.
    cur = norm
    while cur and cur != os.path.dirname(cur):
        if _kebab(os.path.basename(cur)) == scope and _is_project_root(cur):
            return cur
        cur = os.path.dirname(cur)
    return norm


def _load_live_scopes() -> set[str]:
    """Query the v2 bank for all scope names."""
    try:
        from echelon_engine.atoms.cards import CardStore
        cs = CardStore()
        scopes: set[str] = set()
        for r in cs.conn.execute("SELECT DISTINCT scope FROM atoms"):
            if r[0]:
                scopes.add(r[0])
        return scopes
    except Exception:
        return set()


# ── CLI verb ────────────────────────────────────────────────────────────────
def _main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="echelon resolve-scope",
                                 description="Resolve a working directory to its bank scope")
    ap.add_argument("--cwd", default=None, help="Working directory (default: current)")
    ap.add_argument("--json", action="store_true", help="Output as JSON")
    ap.add_argument("--list-irregular", action="store_true", help="List the irregular mappings")
    ap.add_argument("--allow-new", action="store_true",
                    help="MINT the leaf kebab as a new scope when no live scope owns the dir "
                         "(default: fail closed with exit 2)")
    args = ap.parse_args(argv or [])

    if args.list_irregular:
        if args.json:
            import json
            json.dump({"irregular_mappings": dict(_IRREGULAR)}, sys.stdout, indent=2)
        else:
            for dir_name, scope in sorted(_IRREGULAR.items()):
                print(f"{dir_name} -> {scope}")
        return

    live = _load_live_scopes()
    try:
        scope = resolve_scope(args.cwd, live_scopes=live, allow_new=args.allow_new)
    except UnknownScopeError as e:
        if args.json:
            import json
            json.dump({"cwd": args.cwd or os.getcwd(), "scope": None,
                       "error": str(e), "candidate": e.candidate,
                       "live_scopes": len(live)}, sys.stdout)
        else:
            print(str(e), file=sys.stderr)
        raise SystemExit(2)
    if args.json:
        import json
        json.dump({"cwd": args.cwd or os.getcwd(), "scope": scope, "live_scopes": len(live)}, sys.stdout)
    else:
        print(scope)


if __name__ == "__main__":
    _main(sys.argv[1:])
