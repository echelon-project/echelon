"""group_scope — make a PARENT scope that REACHES member scopes via the atlas (not a rename).

THE RIGHT ABSTRACTION (owner, 2026-06-20, correcting a rename attempt): you do NOT rename a scope to
group memory — renaming recomputes/rewrites the content-addressed id+coordinate, which mutates the
atom's IDENTITY and orphans its earned weight (the re-ingest sin in a tidier costume). "Make wk-group
the PARENT scope; we only need to play on edge and recall." The substrate already has this primitive:
cross-scope warmth TRAVELS along the ATLAS (architecture/substrate.json), where a `subsumes`/`depends_on`
edge from A->B means a recall on A draws B's seeds (echelon_sdk.scopegraph.ScopeGraph). So:

  - a PARENT scope (e.g. `wk-group`) gets a `subsumes` OUT-edge to each MEMBER scope;
  - `recall --scope wk-group --warm "..."` then bridges down into every member at the subsumes weight (0.7);
  - NO atom moves, NO id changes, NO coordinate rewrite, ZERO earned weight lost. The members keep their
    own scopes + history; the parent is a lens that reaches them.

This is [[atoms-are-scoped-but-a-capability-crosses-scopes]] made operational: a capability/group composes
across scopes through edges, it is not a flat re-scoped pile.

  python -m echelon_engine group-scope <parent> <member> [<member> ...]            # DRY-RUN
  python -m echelon_engine group-scope <parent> <member> [<member> ...] --apply    # write the atlas edges
  python -m echelon_engine group-scope <parent> --rel depends_on <member> ...      # pick the relation

After applying, prove it: `recall --scope <parent> --warm "<a member's lesson>"` surfaces the member's
atoms (flagged via the bridge). The parent itself need hold no atoms — it is purely a reaching node.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from .. import estate as _estate

_ESTATE_ROOT = str(_estate.estate_root_for("command_root"))
ATLAS = Path(_ESTATE_ROOT) / "architecture" / "substrate.json"
GROUP_KEY = "scope_bridges"   # the atlas relationship group these edges live under (existing convention)
VALID_RELS = {"subsumes", "depends_on", "provides_to", "evolved_from", "merged_into"}


def _load(atlas: Path) -> dict:
    if not atlas.exists():
        return {"_meta": {"domain": "substrate"}, "relationship": {}}
    return json.loads(atlas.read_text(encoding="utf-8"))


def add_group_edges(parent: str, members: list[str], rel: str, atlas: Path, apply: bool) -> dict:
    """Add parent->member edges of `rel` under scope_bridges. Idempotent on (from,to,rel). Returns a
    report; only writes when apply=True."""
    data = _load(atlas)
    rels = data.setdefault("relationship", {})
    group = rels.setdefault(GROUP_KEY, [])

    def exists(frm, to, r):
        return any(e.get("from") == frm and e.get("to") == to and e.get("rel") == r for e in group)

    added, already = [], []
    for m in members:
        if m == parent:
            continue
        if exists(parent, m, rel):
            already.append(m)
        else:
            added.append(m)
            if apply:
                group.append({
                    "from": parent, "to": m, "rel": rel,
                    "label": f"{parent} groups {m}: a parent scope reaches its member's memory via the atlas "
                             f"(no rename, no id rewrite — the member keeps its earned weight)",
                })

    if apply and added:
        atlas.parent.mkdir(parents=True, exist_ok=True)
        atlas.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"parent": parent, "rel": rel, "added": added, "already": already, "applied": bool(apply)}


def add_claim_edges(parent: str, refs: list[str], atlas: Path, apply: bool,
                    drop: bool = False) -> dict:
    """Claim (or drop) specific ATOMS from other scopes: refs are 'scope:slug'. The claimed atom
    stays home (no re-scope, no id rewrite, no dup) — a `claims` edge with an `atoms` filter makes
    it recallable as the parent's own (scopegraph.claims() + the warmth claims-draw consume it).
    The per-atom sibling of add_group_edges (owner 2026-07-24). Idempotent per (parent,member,slug)."""
    data = _load(atlas)
    rels = data.setdefault("relationship", {})
    group = rels.setdefault(GROUP_KEY, [])

    by_member: dict[str, list[str]] = {}
    bad: list[str] = []
    for ref in refs:
        member, _, slug = ref.partition(":")
        if not member or not slug or member == parent:
            bad.append(ref)
        else:
            by_member.setdefault(member, []).append(slug)

    added, already, dropped, missing = [], [], [], []
    for member, slugs in by_member.items():
        edge = next((e for e in group if e.get("from") == parent and e.get("to") == member
                     and e.get("rel") == "claims" and "atoms" in e), None)
        if drop:
            if edge is None:
                missing.extend(f"{member}:{s}" for s in slugs)
                continue
            for s in slugs:
                (dropped if s in edge["atoms"] else missing).append(f"{member}:{s}")
                if apply and s in edge["atoms"]:
                    edge["atoms"].remove(s)
            if apply and not edge["atoms"]:
                group.remove(edge)
            continue
        if edge is None:
            edge = {"from": parent, "to": member, "rel": "claims", "atoms": [],
                    "label": f"{parent} claims specific atoms of {member}: adoption at atom grain "
                             f"(the atom stays home with its earned weight; recall draws it as "
                             f"{parent}'s own)"}
            if apply:
                group.append(edge)
        for s in slugs:
            if s in edge["atoms"]:
                already.append(f"{member}:{s}")
            else:
                added.append(f"{member}:{s}")
                if apply:
                    edge["atoms"].append(s)

    if apply and (added or dropped):
        atlas.parent.mkdir(parents=True, exist_ok=True)
        atlas.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"parent": parent, "added": added, "already": already, "dropped": dropped,
            "missing": missing, "bad": bad, "applied": bool(apply)}


def list_claims(parent: str, atlas: Path) -> list[dict]:
    data = _load(atlas)
    return [e for e in data.get("relationship", {}).get(GROUP_KEY, [])
            if e.get("from") == parent and e.get("rel") == "claims" and "atoms" in e]


def claim_main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon claim",
        description="CLAIM specific atoms from other scopes at atom grain — the atom stays in its "
                    "home scope (id/weight/history untouched), but recall on the claiming scope "
                    "draws it as its OWN (weight 1.0). The filing-mix-up fix that needs no mop-up.")
    ap.add_argument("refs", nargs="*", metavar="scope:slug",
                    help="atom refs to claim, e.g. gamma-support:reflex-echelon-python-not-flux-venv")
    ap.add_argument("--scope", default=None,
                    help="the claiming (parent) scope (default: resolved from the cwd)")
    ap.add_argument("--apply", action="store_true", help="write the edge (default is a dry-run)")
    ap.add_argument("--list", action="store_true", help="list the scope's claims and exit")
    ap.add_argument("--drop", action="store_true", help="drop the given refs instead of claiming")
    ap.add_argument("--atlas", default=None, help="atlas path (default architecture/substrate.json)")
    a = ap.parse_args(argv)

    atlas = Path(a.atlas) if a.atlas else ATLAS
    parent = a.scope
    if not parent:
        try:
            from .resolve_scope import resolve_scope
            parent = resolve_scope(os.getcwd())
        except Exception:
            parent = os.environ.get("ECHELON_SCOPE", "echelon")

    if a.list:
        edges = list_claims(parent, atlas)
        if not edges:
            print(f"'{parent}' claims no atoms (claim <scope:slug> ... --apply to adopt one)")
        for e in edges:
            print(f"  {parent} --claims--> {e['to']}:")
            for s in e.get("atoms", []):
                print(f"    - {s}")
        return 0

    if not a.refs:
        ap.error("give at least one scope:slug ref (or --list)")
    rep = add_claim_edges(parent, a.refs, atlas, a.apply, drop=a.drop)

    head = ("CLAIMED" if rep["applied"] else "DRY-RUN claim") if not a.drop else \
           ("DROPPED" if rep["applied"] else "DRY-RUN drop")
    print(f"{head}: scope '{rep['parent']}'")
    for key, label in (("added", "claimed"), ("already", "already claimed"),
                       ("dropped", "dropped"), ("missing", "not found"), ("bad", "bad ref (need scope:slug)")):
        if rep[key]:
            print(f"  {label}: " + ", ".join(rep[key]))
    if not rep["applied"]:
        print(f"\n  read-only. pass --apply to write {atlas}.")
    else:
        print(f"\n  PROVE IT: python -X utf8 -m echelon_engine recall --scope {rep['parent']} "
              f"--warm \"<the claimed atom's lesson>\"  (expect via_rel=claims)")
    return 0


_main_claim = claim_main   # __main__._entry(group_scope, fn="claim") resolves here


def _main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon group-scope",
        description="Make a PARENT scope reach MEMBER scopes via atlas edges (cross-scope recall), "
                    "instead of a lossy rename. No atom/id/weight is touched.")
    ap.add_argument("parent", help="the parent (group) scope")
    ap.add_argument("members", nargs="+", help="member scope(s) the parent should reach")
    ap.add_argument("--rel", default="subsumes", choices=sorted(VALID_RELS),
                    help="the atlas relation parent->member (default subsumes; warmth flows at its weight)")
    ap.add_argument("--apply", action="store_true", help="write the edges (default is a dry-run)")
    ap.add_argument("--atlas", default=None, help="atlas path (default architecture/substrate.json)")
    a = ap.parse_args(argv)

    atlas = Path(a.atlas) if a.atlas else ATLAS
    rep = add_group_edges(a.parent, a.members, a.rel, atlas, a.apply)

    head = ("GROUPED" if rep["applied"] else "DRY-RUN group-scope")
    print(f"{head}: '{rep['parent']}' --{rep['rel']}--> [{', '.join(a.members)}]")
    if rep["added"]:
        print(f"  edges {'added' if rep['applied'] else 'TO ADD'}: " + ", ".join(rep["added"]))
    if rep["already"]:
        print(f"  already linked: " + ", ".join(rep["already"]))
    if not rep["applied"]:
        print(f"\n  read-only. pass --apply to write {atlas}.")
    else:
        print(f"\n  PROVE IT: python -X utf8 -m echelon_engine recall --scope {rep['parent']} --warm \"<a member's lesson>\"")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
