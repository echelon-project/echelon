#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scan.py — ENFORCING drift-gate for the pagemodel catalogue.

Turns the catalogue from descriptive to blocking:
  - assert_violations: declared invariant breaches (from Catalogue.check_asserts())
  - untrustworthy_surfaces: pages whose attribution confidence is below threshold
    (multi-module pages with disambiguation < --min-disambiguation, default 80.0)
  - drift: semantic enrichment over Catalogue.diff() (orphaned boundaries, crowded modules,
    unreferenced components that became so since baseline)
  - status: "pass" | "fail" — FAIL if any assert_violation OR untrustworthy_surfaces
    (blocking, exits nonzero)

THE LAW: scan READS the derived graph + sidecar asserts.  It NEVER mutates the graph.

CLI:
  python -m echelon_engine.pagemodel.scan \\
      --catalogue <c.json> --os-dir <os> [--baseline <b.json>] [--report]
      [--min-disambiguation 80.0] [--skip-trust-gate]

  Exits nonzero when status == "fail".
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from echelon_engine.pagemodel.catalogue_api import Catalogue


# ── CROWDING THRESHOLD (loc lines) ──────────────────────────────────────────
_DEFAULT_CROWD_THRESHOLD = 300  # modules over this are "crowded" in drift

# ── ATTRIBUTION TRUST THRESHOLD ──────────────────────────────────────────────
_DEFAULT_MIN_DISAMBIGUATION = 80.0  # multi-module pages must meet this to be trustworthy


def _orphaned_endpoints(baseline: Catalogue, current: Catalogue) -> list[dict]:
    """Endpoints that HAD at least one caller in baseline but have ZERO callers now.
    An orphaned boundary is a dead API surface — nothing calls it in the current graph."""
    orphans = []
    # collect endpoints in baseline that have inbound caller edges
    b_ep_callers: dict[str, int] = {}
    for e in baseline.cat["edges"]:
        dst = baseline._nodes.get(e["to"], {})
        if dst.get("type") == "endpoint":
            b_ep_callers[e["to"]] = b_ep_callers.get(e["to"], 0) + 1

    # for each such endpoint, check if it still exists in current AND still has callers
    c_ep_callers: dict[str, int] = {}
    for e in current.cat["edges"]:
        dst = current._nodes.get(e["to"], {})
        if dst.get("type") == "endpoint":
            c_ep_callers[e["to"]] = c_ep_callers.get(e["to"], 0) + 1

    for ep_id, caller_count in b_ep_callers.items():
        if caller_count > 0 and ep_id in current._nodes:
            now = c_ep_callers.get(ep_id, 0)
            if now == 0:
                node = current._nodes[ep_id]
                orphans.append({
                    "id": ep_id,
                    "label": node.get("label", ep_id),
                    "callers_before": caller_count,
                    "callers_now": 0,
                })
    return orphans


def _newly_crowded_modules(baseline: Catalogue, current: Catalogue,
                           threshold: int = _DEFAULT_CROWD_THRESHOLD) -> list[dict]:
    """Modules that CROSSED the crowding threshold (loc) since baseline.
    Only reports NEW crossings — modules already crowded in baseline are not re-reported."""
    b_loc = {n["id"]: n.get("loc", 0) for n in baseline.cat["nodes"] if n["type"] == "module"}
    crowded = []
    for n in current.cat["nodes"]:
        if n["type"] != "module":
            continue
        cur_loc = n.get("loc", 0)
        prev_loc = b_loc.get(n["id"], 0)
        if cur_loc > threshold and prev_loc <= threshold:
            crowded.append({
                "id": n["id"],
                "label": n.get("label", n["id"]),
                "loc_before": prev_loc,
                "loc_now": cur_loc,
                "threshold": threshold,
            })
    return crowded


def _newly_unreferenced_components(baseline: Catalogue, current: Catalogue) -> list[dict]:
    """Components (type='node') that had at least one inbound edge in baseline
    but are now unreferenced (zero inbound edges) in the current graph."""
    b_inbound: dict[str, int] = {}
    for e in baseline.cat["edges"]:
        b_inbound[e["to"]] = b_inbound.get(e["to"], 0) + 1

    c_inbound: dict[str, int] = {}
    for e in current.cat["edges"]:
        c_inbound[e["to"]] = c_inbound.get(e["to"], 0) + 1

    newly_dead = []
    for n in current.cat["nodes"]:
        if n.get("type") not in ("node", "component"):
            continue
        nid = n["id"]
        if b_inbound.get(nid, 0) > 0 and c_inbound.get(nid, 0) == 0:
            newly_dead.append({
                "id": nid,
                "label": n.get("label", nid),
                "refs_before": b_inbound[nid],
                "refs_now": 0,
            })
    return newly_dead


def _enrich_drift(raw_diff: dict, baseline: Catalogue, current: Catalogue,
                  crowd_threshold: int = _DEFAULT_CROWD_THRESHOLD) -> dict:
    """Enrich the raw graph diff with semantic layers."""
    return {
        **raw_diff,
        "orphaned_endpoints": _orphaned_endpoints(baseline, current),
        "newly_crowded_modules": _newly_crowded_modules(baseline, current, crowd_threshold),
        "newly_unreferenced_components": _newly_unreferenced_components(baseline, current),
    }


# ── PUBLIC API ───────────────────────────────────────────────────────────────

def _check_trust(cat: Catalogue, min_disambiguation: float = _DEFAULT_MIN_DISAMBIGUATION) -> list[dict]:
    """Check attribution trustworthiness for ALL surfaces in the catalogue.

    Calls Catalogue.gate(surface) for each surface slug found in the graph.
    Returns a list of surfaces where trustworthy=False (i.e. multi-module pages
    whose disambiguation score falls below min_disambiguation).

    This function never mutates the graph — it only reads via gate().
    """
    # Collect all unique surface slugs from module nodes
    surface_slugs: set[str] = set()
    for n in cat.cat["nodes"]:
        for s in n.get("surfaces") or []:
            if s:
                surface_slugs.add(s)

    untrustworthy = []
    for slug in sorted(surface_slugs):
        result = cat.gate(slug)
        if result is None:
            # Source files missing — skip (gate() returns None when html/js absent)
            continue
        # Override the hardcoded 80.0 inside gate() with our tunable threshold:
        # gate() uses mf["module_count"] <= 1 OR disambiguation >= 80.0.
        # We re-evaluate with the caller-supplied threshold so --min-disambiguation works.
        is_trustworthy = result["modules"] <= 1 or result["disambiguation"] >= min_disambiguation
        if not is_trustworthy:
            untrustworthy.append({
                "surface": result["surface"],
                "coverage": result["coverage"],
                "disambiguation": result["disambiguation"],
                "modules": result["modules"],
            })
    return untrustworthy


def scan(
    catalogue_path: str | pathlib.Path,
    os_dir: str | pathlib.Path,
    baseline_path: str | pathlib.Path | None = None,
    crowd_threshold: int = _DEFAULT_CROWD_THRESHOLD,
    min_disambiguation: float = _DEFAULT_MIN_DISAMBIGUATION,
    skip_trust_gate: bool = False,
) -> dict[str, Any]:
    """Run the enforcing drift-gate.

    Returns:
        {
          "assert_violations": [...],       # from Catalogue.check_asserts()
          "untrustworthy_surfaces": [...],  # surfaces failing attribution confidence gate
          "drift": {...} | None,            # enriched diff vs baseline, or None if no baseline
          "status": "pass" | "fail",        # FAIL if any assert_violation OR untrustworthy_surfaces
        }

    The graph is NEVER mutated by this function.
    """
    cat = Catalogue(str(catalogue_path), os_dir=str(os_dir))
    violations = cat.check_asserts()

    untrustworthy: list[dict] = []
    if not skip_trust_gate:
        untrustworthy = _check_trust(cat, min_disambiguation=min_disambiguation)

    drift = None
    if baseline_path is not None:
        baseline = Catalogue(str(baseline_path), os_dir=str(os_dir))
        raw_diff = cat.diff(baseline)
        drift = _enrich_drift(raw_diff, baseline, cat, crowd_threshold)

    status = "fail" if (violations or untrustworthy) else "pass"
    return {
        "assert_violations": violations,
        "untrustworthy_surfaces": untrustworthy,
        "drift": drift,
        "status": status,
    }


# ── REPORT RENDERER ──────────────────────────────────────────────────────────

def _render_report(result: dict) -> str:
    lines = []
    status = result["status"]
    lines.append(f"╔{'═'*58}╗")
    lines.append(f"║  PAGEMODEL SCAN  —  STATUS: {status.upper():<29}║")
    lines.append(f"╚{'═'*58}╝")

    viols = result["assert_violations"]
    lines.append(f"\n── Assert violations ({len(viols)}) ──")
    if not viols:
        lines.append("  (none)")
    else:
        for v in viols:
            rule = v.get("rule", {})
            kind = rule.get("kind", "?")
            if "edge" in v:
                e = v["edge"]
                lines.append(f"  [FAIL] {kind}: edge {e['from']} → {e['to']} (rel={e['rel']})")
                lines.append(f"         rule: {json.dumps(rule)}")
            elif "node" in v:
                lines.append(f"  [FAIL] {kind}: node={v['node']}  loc={v.get('loc')}  rule={json.dumps(rule)}")
            else:
                lines.append(f"  [FAIL] {kind}: {json.dumps(v)}")

    untrust = result.get("untrustworthy_surfaces", [])
    lines.append(f"\n── Attribution trust gate ({len(untrust)} untrustworthy) ──")
    if not untrust:
        lines.append("  (all surfaces trustworthy)")
    else:
        for s in untrust:
            lines.append(
                f"  [FAIL] {s['surface']}  modules={s['modules']}"
                f"  coverage={s['coverage']:.1f}%  disambiguation={s['disambiguation']:.1f}%"
            )

    drift = result.get("drift")
    if drift is None:
        lines.append("\n── Drift: (no baseline provided) ──")
    else:
        lines.append("\n── Drift vs baseline ──")
        lines.append(f"  nodes added:   {len(drift.get('nodes_added', []))}")
        lines.append(f"  nodes removed: {len(drift.get('nodes_removed', []))}")
        lines.append(f"  edges added:   {drift.get('edges_added', 0)}")
        lines.append(f"  edges removed: {drift.get('edges_removed', 0)}")

        orphans = drift.get("orphaned_endpoints", [])
        lines.append(f"\n  Orphaned endpoints (had callers, now zero): {len(orphans)}")
        for o in orphans:
            lines.append(f"    • {o['label']}  (was {o['callers_before']} callers)")

        crowded = drift.get("newly_crowded_modules", [])
        lines.append(f"\n  Newly crowded modules (crossed threshold): {len(crowded)}")
        for m in crowded:
            lines.append(f"    • {m['label']}  {m['loc_before']} → {m['loc_now']} loc  (threshold={m['threshold']})")

        dead_comp = drift.get("newly_unreferenced_components", [])
        lines.append(f"\n  Newly unreferenced components: {len(dead_comp)}")
        for c in dead_comp:
            lines.append(f"    • {c['label']}  (was {c['refs_before']} refs)")

    lines.append("")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> None:
    p = argparse.ArgumentParser(
        prog="python -m echelon_engine.pagemodel.scan",
        description="Enforcing drift-gate for the pagemodel catalogue. Exits nonzero on violation.",
    )
    p.add_argument("--catalogue", required=True, metavar="C.JSON",
                   help="Path to catalogue JSON built by catalogue.py")
    p.add_argument("--os-dir", required=True, metavar="OS_DIR",
                   help="Path to the OS pages directory (same as used during extraction)")
    p.add_argument("--baseline", metavar="B.JSON", default=None,
                   help="Optional baseline catalogue JSON for drift comparison")
    p.add_argument("--report", action="store_true",
                   help="Print a human-readable report to stdout")
    p.add_argument("--crowd-threshold", type=int, default=_DEFAULT_CROWD_THRESHOLD,
                   metavar="N", help=f"LOC threshold for crowding (default {_DEFAULT_CROWD_THRESHOLD})")
    p.add_argument("--min-disambiguation", type=float, default=_DEFAULT_MIN_DISAMBIGUATION,
                   metavar="PCT",
                   help=f"Attribution disambiguation %% threshold for trust gate (default {_DEFAULT_MIN_DISAMBIGUATION}). "
                        "Multi-module pages scoring below this fail the scan.")
    p.add_argument("--skip-trust-gate", action="store_true",
                   help="Disable the attribution trust gate entirely (trust gate errors are ignored)")
    p.add_argument("--json-out", metavar="OUT.JSON", default=None,
                   help="Write full result JSON to a file")
    args = p.parse_args()

    result = scan(
        catalogue_path=args.catalogue,
        os_dir=args.os_dir,
        baseline_path=args.baseline,
        crowd_threshold=args.crowd_threshold,
        min_disambiguation=args.min_disambiguation,
        skip_trust_gate=args.skip_trust_gate,
    )

    if args.report:
        print(_render_report(result))
    else:
        # Always emit a compact status line
        nv = len(result["assert_violations"])
        nu = len(result.get("untrustworthy_surfaces", []))
        print(f"status={result['status']}  violations={nv}  untrustworthy={nu}")

    if args.json_out:
        pathlib.Path(args.json_out).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    sys.exit(0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    _cli()
