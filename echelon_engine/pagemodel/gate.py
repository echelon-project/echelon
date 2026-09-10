#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gate.py — the ATTRIBUTION GATE (P0→P2/P3 boundary).

Regenerate the surface manifest for every OS page and print a per-board attribution_confidence
table for the OWNER to eyeball. Boards above --floor are eligible for P2/P3; below = flagged unverified.
Downstream phases must NOT run on untrusted attribution.

Usage:
  python -m echelon_engine.pagemodel.gate --os-dir <os> --out-dir <manifests> [--floor 60]
"""
from __future__ import annotations
import argparse, json, pathlib
from echelon_engine.pagemodel import extract as ex

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--os-dir", required=True, help="dir containing <slug>.html + assets/page-<slug>.js")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--floor", type=float, default=80.0, help="DISAMBIGUATION floor (multi-module pages) for P2/P3")
    args = ap.parse_args()

    osd = pathlib.Path(args.os_dir)
    out = pathlib.Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    htmls = sorted(osd.glob("*.html"))

    rows = []
    for h in htmls:
        slug = h.stem
        js = osd / "assets" / f"page-{slug}.js"
        if not js.exists():
            continue
        mf = ex.build(slug, ex.read(h), ex.read(js))
        (out / f"{slug}.pagemodel.json").write_text(
            json.dumps(mf, indent=2, ensure_ascii=False), encoding="utf-8")
        a = mf["attribution"]
        # P2/P3 eligibility = is the attribution TRUSTWORTHY (not how modular the page is)?
        #  - single-module page: attribution is trivially correct (one module + shared) → eligible.
        #  - multi-module page: eligible iff disambiguation ≥ floor (fns placed by hard id-evidence).
        eligible = (mf["module_count"] <= 1) or (a["disambiguation"] >= args.floor)
        rows.append({
            "slug": slug, "cov": a["coverage"], "dis": a["disambiguation"],
            "share": round(100 * mf["loc_shared"] / max(1, mf["loc_total"]), 1),
            "fns": mf["fn_total"], "mods": mf["module_count"], "loc": mf["loc_total"],
            "eligible": eligible,
        })

    rows.sort(key=lambda r: -r["cov"])
    print(f"\n{'='*76}\nATTRIBUTION GATE  (disambiguation floor={args.floor}% for multi-module)   {len(rows)} surfaces\n{'='*76}")
    print(f"{'SURFACE':<16}{'COV%':>6}{'DISAMB':>8}{'SHARE%':>8}{'FNS':>5}{'MODS':>6}{'LOC':>7}  ELIGIBLE")
    print("-"*76)
    for r in rows:
        dis = f"{r['dis']}%" if r["mods"] > 1 else "  n/a"
        mark = "  ✓ P2/P3" if r["eligible"] else "  ✗ unverified"
        print(f"{r['slug']:<16}{r['cov']:>6}{dis:>8}{r['share']:>8}{r['fns']:>5}{r['mods']:>6}{r['loc']:>7}{mark}")
    elig = [r for r in rows if r["eligible"]]
    print(f"\ncoverage = % of fns placed in a module (the gate metric).  "
          f"disamb = of those, % by hard id-evidence (multi-module only).")
    print(f"eligible for P2/P3: {len(elig)}/{len(rows)}")
    print(f"[ok] {len(rows)} manifests -> {out}")

if __name__ == "__main__":
    main()
