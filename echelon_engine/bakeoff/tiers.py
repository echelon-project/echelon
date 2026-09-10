"""tiers — turn bakeoff results into a ROUTING TABLE, not a single winner.

THE REFRAME (owner, 2026-08-17): the five cells are not five candidates competing for one
crown. They are a TIER LADDER. The question a router asks is never "which model is best" —
it is "what is the CHEAPEST configuration that is sufficient for THIS class of work". A
single aggregate Q cannot answer that, because it averages an S1 extraction task together
with an S4 reconciliation and hides the only fact that matters: where sufficiency breaks.

So this module reports per SEVERITY and per FAMILY:

    for each (severity, family): the cheapest cell that passed EVERY item in that bucket

That is the tier assignment. A family where flash-low suffices costs ~1/20th of one where
only the swarm holds — and the router should spend accordingly instead of paying the
worst-case price on every call.

SWARM AS A TIER, NOT A COMPETITOR. Once tiers exist, cell E stops being "does the ensemble
beat D" and becomes "which buckets REQUIRE the ensemble". Its 4x cost is justified exactly
where it is the only configuration that clears the bucket, and nowhere else.

HONEST LIMIT, stated in the output: with small buckets, "passed every item" is weak evidence.
A bucket of 2 items proves very little; the renderer prints the bucket size next to every
assignment so a 2/2 is never read as a guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .items import Item, GATES
from .cells import CELLS

PASS_AT = 2

# Cheapest-first. This IS the ladder: a router walks it and stops at the first sufficient rung.
LADDER = ["A", "B", "C", "D", "E"]


@dataclass
class Bucket:
    key: str                     # "S4" or "S4/reconciliation"
    severity: str
    family: str
    item_ids: list[str]
    per_cell_pass: dict[str, int]

    @property
    def n(self) -> int:
        return len(self.item_ids)

    def sufficient(self) -> list[str]:
        """Cells that passed EVERY item in this bucket."""
        return [c for c in LADDER if self.per_cell_pass.get(c, -1) == self.n]

    def cheapest(self, costs: dict[str, float]) -> str | None:
        ok = self.sufficient()
        if not ok:
            return None
        return min(ok, key=lambda c: costs.get(c, float("inf")))


def build_buckets(items: dict[str, Item], scores: dict[str, dict[str, int]],
                  *, by_family: bool = True) -> list[Bucket]:
    """Group items by severity (and family) and count each cell's passes per bucket."""
    groups: dict[tuple[str, str], list[str]] = {}
    for iid, it in items.items():
        key = (it.severity, it.family if by_family else "*")
        groups.setdefault(key, []).append(iid)

    out: list[Bucket] = []
    for (sev, fam), ids in sorted(groups.items()):
        per_cell = {
            c: sum(1 for i in ids if scores.get(c, {}).get(i, 0) >= PASS_AT)
            for c in scores
        }
        out.append(Bucket(
            key=f"{sev}/{fam}" if fam != "*" else sev,
            severity=sev, family=fam, item_ids=sorted(ids), per_cell_pass=per_cell))
    return out


def tier_map(items: dict[str, Item], scores: dict[str, dict[str, int]],
             costs: dict[str, float], *, by_family: bool = True) -> dict[str, Any]:
    """The routing table: bucket -> cheapest sufficient cell (or None = nothing sufficed)."""
    buckets = build_buckets(items, scores, by_family=by_family)
    rows = []
    for b in buckets:
        pick = b.cheapest(costs)
        rows.append({
            "bucket": b.key,
            "severity": b.severity,
            "family": b.family,
            "n": b.n,
            "sufficient": b.sufficient(),
            "assign": pick,
            "per_cell": dict(b.per_cell_pass),
        })
    # Where does the ladder actually need to climb?
    needs = {}
    for r in rows:
        if r["assign"]:
            needs.setdefault(r["assign"], []).append(r["bucket"])
    return {"rows": rows, "by_tier": needs,
            "unserved": [r["bucket"] for r in rows if not r["assign"]]}


def render(tm: dict[str, Any], costs: dict[str, float]) -> str:
    lines = []
    lines.append("TIER MAP — cheapest configuration sufficient for each bucket")
    lines.append("(a router walks A→E and stops at the first rung that holds)")
    lines.append("")
    lines.append(f"  {'bucket':<26}{'n':>3}  {'A':>3}{'B':>3}{'C':>3}{'D':>3}{'E':>3}   assign")
    lines.append("  " + "-" * 62)
    for r in sorted(tm["rows"], key=lambda x: (x["severity"], x["family"]), reverse=True):
        pc = r["per_cell"]
        cells = "".join(f"{pc.get(c, 0):>3}" for c in LADDER)
        assign = r["assign"] or "NONE"
        label = CELLS[r["assign"]].label if r["assign"] else "— no cell sufficed"
        lines.append(f"  {r['bucket']:<26}{r['n']:>3}  {cells}   {assign} {label}")
    lines.append("")
    if tm["unserved"]:
        lines.append(f"  ⚠ UNSERVED (no configuration passed every item): {tm['unserved']}")
    lines.append("")
    lines.append("  ROUTING CONSEQUENCE:")
    for tier in LADDER:
        got = tm["by_tier"].get(tier)
        if got:
            lines.append(f"    {tier} {CELLS[tier].label:<12} ${costs.get(tier, 0):.4f}  "
                         f"serves {len(got)} bucket(s): {', '.join(got[:6])}"
                         + (" …" if len(got) > 6 else ""))
    lines.append("")
    lines.append("  HONEST LIMIT: 'passed every item' in a bucket of 2 is weak evidence.")
    lines.append("  Bucket size is printed above; treat small-n assignments as provisional.")
    return "\n".join(lines)
