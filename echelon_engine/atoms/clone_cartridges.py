"""clone_cartridges — replant the earned general-skill cartridges into a bank.

THE NEED (owner 2026-06-22): a fresh ECHELON_HOME (e.g. an LM-Studio `mind/`
bank) ships with the cartridge SPECS (they live in code: cartridge_registry.py)
but NOT the atoms+cards they reference — those earned atoms live in the live
~/.echelon bank. So an equipped model can name 'ux'/'yagni'/'swarm' but recall
nothing. This clones the cartridges' atoms (ux, audit/swarm, dev, yagni, scribe,
brainstorm, act-ready, partner) from a SOURCE bank into a TARGET bank, then
re-composes each cartridge card there.

PRECISION: it copies ONLY the atoms each CartridgeSpec.refs names (the earned
capability, in firing order) — NOT whole scopes (the echelon scope alone holds
hundreds of atoms of project-specific memory). The result is a target bank that holds
exactly the general skills, recall-able + equip-able, nothing extra. For a SHARED
teammate bank pass teammate_safe=True — it further drops the owner's private-estate
cartridges + any echelon:*/mol:* war-story atoms (see ESTATE_COUPLED / ESTATE_SCOPES).

Source = the real bank ($ECHELON_HOME unset → ~/.echelon, or --source). Target =
the fresh home ($ECHELON_HOME / --target). Content-addressed, so re-running is a
no-op for already-present atoms. Reads source read-only.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── TEAMMATE-SAFE roster (owner 2026-07-02) ──────────────────────────────────
# When cloning a pack for a SHARED/teammate bank, we must NOT ship the cartridges
# that are coupled to the OWNER'S private estate — they leak project/deploy memory
# through their refs. Excluded, with why:
#   - mol-boot-procedures : refs mol:* estate deploy procedures (change-workflow,
#                           deploy-map, hotpatch, broker-provision, scanner-rules).
#   - society             : echelon-scoped, 0 general refs.
#   - partner, act-ready  : echelon-scoped OPERATING doctrine (owner's own boot half).
# Everything else is a general dev/design skill and is teammate-safe. `clone()`
# uses this as the default EXCLUDE when called with teammate_safe=True.
ESTATE_COUPLED = frozenset({"mol-boot-procedures", "society", "partner", "act-ready"})

# Even a teammate-SAFE cartridge can name individual atoms that live in the owner's
# private ESTATE scopes (ux/ux-pipeline/brainstorm pull echelon:* war-stories:
# live Vertex IDs, Gemini roster, internal UX notes). For a teammate bank we DROP
# any ref whose scope is a private estate scope — EXCEPT the clean self-primer
# atoms (echelon:echelon_* planted by self_seed, which the cartridge cards need).
ESTATE_SCOPES = frozenset({"echelon", "mol"})
# the self-primer slugs that ARE teammate-safe even though they live in `echelon`.
_SELF_PRIMER_PREFIX = "echelon:echelon_"


def _ref_is_estate_private(coord: str) -> bool:
    """True if this coordinate is a private-estate atom that must NOT enter a
    teammate bank (an echelon:*/mol:* war-story), but NOT one of the clean
    self-primer atoms (echelon:echelon_*)."""
    if ":" not in coord:
        return False
    scope = coord.split(":", 1)[0]
    if scope not in ESTATE_SCOPES:
        return False
    return not coord.startswith(_SELF_PRIMER_PREFIX)


def _open_store(home: str | None):
    """A CardStore bound to a specific home's echelon.db (the v2 primary). None →
    the resolved default ($ECHELON_HOME or ~/.echelon)."""
    from .echelon_home import echelon_home
    from .cards import CardStore
    base = Path(home).expanduser() if home else echelon_home()
    return CardStore(base / "echelon.db")


def _read_atom(src, coord: str):
    """Read one atom by coordinate from the source store. Returns the newest row
    (max ts) as a dict, or None. Dedups the content-addressed duplicates."""
    row = src.conn.execute(
        "SELECT coordinate, content, scope, kind, valence, arousal "
        "FROM atoms WHERE coordinate=? ORDER BY ts DESC LIMIT 1", (coord,)).fetchone()
    return dict(row) if row else None


def clone(source_home: str | None = None, target_home: str | None = None,
          *, verbose: bool = True, teammate_safe: bool = False,
          include: set[str] | None = None, exclude: set[str] | None = None) -> dict:
    """Clone every registered cartridge's ref-atoms + card from source → target.
    Returns {atoms_planted, atoms_missing, cards, per_cartridge}. Idempotent.

    Roster filtering (owner 2026-07-02, for shared/teammate banks):
      - teammate_safe=True   → default-exclude ESTATE_COUPLED (private-estate cartridges).
      - include={names}      → clone ONLY these cartridges.
      - exclude={names}      → clone all-but-these (merged with teammate_safe's set).
    None of these change atoms already present (content-addressed)."""
    from . import cartridge_registry as cr

    src = _open_store(source_home)
    tgt = _open_store(target_home)
    if src.db_path == tgt.db_path:
        return {"error": "source and target are the SAME bank — set --target / ECHELON_HOME"}

    # resolve the effective roster: include wins; else all minus (exclude ∪ estate-coupled)
    excl = set(exclude or set())
    if teammate_safe:
        excl |= set(ESTATE_COUPLED)

    def _wanted(spec) -> bool:
        if include is not None:
            return spec.name in include
        return spec.name not in excl

    planted, missing = 0, []
    per: dict = {}
    seen: set = set()
    skipped: list[str] = []
    dropped_private: list[str] = []

    # for a teammate bank, drop any ref that is a private-estate atom (owner's
    # echelon:*/mol:* war-stories — live Vertex IDs etc.), keeping only the clean
    # self-primer echelon:echelon_* atoms the cards need.
    drop_private = teammate_safe

    # de-duped union of every WANTED cartridge's ref coordinates (an atom shared by
    # two cartridges is copied once).
    for spec in cr.all_specs():
        if not _wanted(spec):
            skipped.append(spec.name)
            continue
        got = 0
        for coord in spec.refs:
            if coord in seen:
                got += 1
                continue
            seen.add(coord)
            if drop_private and _ref_is_estate_private(coord):
                dropped_private.append(coord)
                continue
            a = _read_atom(src, coord)
            if a is None:
                missing.append(coord)
                continue
            # replant into target under the SAME coordinate (so the card resolves
            # it by the registry ref). add_atom is content-addressed → dedups.
            tgt.add_atom(a["coordinate"], a["content"], born_from="clone_cartridges",
                         scope=a["scope"] or "", kind=a["kind"] or "",
                         valence=a["valence"] or 0.0, arousal=a["arousal"] or 0.0)
            planted += 1
            got += 1
        per[spec.name] = {"refs": len(spec.refs), "resolved": got}

    # compile the structured spine for every freshly-planted atom (read surface for
    # remember/recall). Sweep by the scopes the cartridge atoms live in.
    scopes = {cr.get(n).scope for n in (s.name for s in cr.all_specs())}
    for spec in cr.all_specs():
        for coord in spec.refs:
            if ":" in coord:
                scopes.add(coord.split(":", 1)[0])
    for sc in scopes:
        try:
            rows = tgt.conn.execute(
                "SELECT a.id FROM atoms a LEFT JOIN atom_spine s ON s.atom_id=a.id "
                "WHERE a.coordinate LIKE ? AND s.atom_id IS NULL", (f"{sc}:%",)).fetchall()
            for r in rows:
                try:
                    tgt.compile_atom_struct(r["id"])
                except Exception:
                    pass
        except Exception:
            pass

    # re-compose each cartridge card in the target from its registry refs (resolve
    # the planted atom ids by coordinate). add_card is content-addressed/no-op-safe.
    cards = 0
    for spec in cr.all_specs():
        if not _wanted(spec):
            continue
        try:
            coords = [c for c in spec.refs
                      if not (drop_private and _ref_is_estate_private(c))]
            refs = [tgt.atom_id_for_coordinate(c) for c in coords]
            refs = [r for r in refs if r]
            if len(refs) >= 2:
                tgt.add_card(spec.label, refs, born_from="clone_cartridges")
                cards += 1
                per[spec.name]["card"] = True
        except Exception as e:  # noqa: BLE001
            per[spec.name]["card_error"] = repr(e)

    result = {"atoms_planted": planted, "atoms_missing": len(missing),
              "missing": missing[:10], "cards": cards, "per_cartridge": per,
              "skipped": sorted(skipped),
              "dropped_private": sorted(set(dropped_private)),
              "target": str(tgt.db_path), "source": str(src.db_path)}
    if verbose:
        print(f"[clone-cartridges] {src.db_path}  →  {tgt.db_path}")
        print(f"  planted {planted} atoms · {cards} cards · {len(missing)} missing")
        for name, info in per.items():
            tail = " (card)" if info.get("card") else ""
            print(f"    {name:22s} {info['resolved']}/{info['refs']} atoms{tail}")
        if dropped_private:
            print(f"  dropped private-estate atoms: {len(set(dropped_private))} "
                  f"(e.g. {sorted(set(dropped_private))[:3]})")
        if skipped:
            print(f"  skipped (not in roster): {', '.join(sorted(skipped))}")
        if missing:
            print(f"  missing coords: {missing[:8]}")
    return result


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon clone-cartridges",
        description="Clone the earned general-skill cartridges (ux/swarm/yagni/dev/"
                    "scribe/brainstorm/act-ready/partner) from one bank into another.")
    ap.add_argument("--source", default=None,
                    help="source bank home (default: $ECHELON_HOME or ~/.echelon). Read-only.")
    ap.add_argument("--target", required=True,
                    help="target bank home to plant the cartridges into.")
    ap.add_argument("--teammate-safe", action="store_true",
                    help="exclude the owner's private-estate cartridges "
                         f"({', '.join(sorted(ESTATE_COUPLED))}) — for a shared/teammate bank.")
    ap.add_argument("--include", default=None,
                    help="comma-separated cartridge names to clone ONLY these.")
    ap.add_argument("--exclude", default=None,
                    help="comma-separated cartridge names to skip (merged with --teammate-safe).")
    args = ap.parse_args(argv)
    inc = set(x.strip() for x in args.include.split(",")) if args.include else None
    exc = set(x.strip() for x in args.exclude.split(",")) if args.exclude else None
    res = clone(args.source, args.target, teammate_safe=args.teammate_safe,
                include=inc, exclude=exc)
    return 0 if "error" not in res else 2


if __name__ == "__main__":
    raise SystemExit(main())
