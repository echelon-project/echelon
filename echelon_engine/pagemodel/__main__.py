#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
echelon pagemodel -- ChainBoard pagemodel analysis pipeline.

Thin orchestrator: given --os-dir, runs build_catalogue → writes JSON → prints report.
Optionally runs the scan gate on the produced catalogue.

  echelon pagemodel --os-dir <os-dir> --out <catalogue.json> [--scan] [--no-report]

All heavy logic lives in the existing pagemodel modules; this is pure routing.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="echelon pagemodel",
        description=(
            "ChainBoard pagemodel analysis: extract → graph → catalogue → report. "
            "Given --os-dir, reads every *.html + assets/page-<slug>.js, builds the "
            "cross-page catalogue, writes JSON, and prints the boundary/dead-code report."
        ),
    )
    ap.add_argument(
        "--os-dir", nargs="+", metavar="PATH",
        help="One or more OS page sources: a directory (all *.html + assets/page-<slug>.js), "
             "and/or explicit .html / .js files. Multiple values allowed. "
             "Alias: --paths.",
    )
    ap.add_argument(
        "--paths", nargs="+", metavar="PATH", dest="os_dir",
        help="Alias for --os-dir (dirs and/or files).",
    )
    ap.add_argument(
        "--out", required=True,
        help="Write the catalogue JSON to this path.",
    )
    ap.add_argument(
        "--no-report", action="store_true",
        help="Skip printing the human-readable report (write JSON only).",
    )
    ap.add_argument(
        "--scan", action="store_true",
        help="After building, run the scan gate on the catalogue (exits nonzero on fail).",
    )
    ap.add_argument(
        "--baseline",
        help="Baseline catalogue.json for drift comparison (passed through to scan).",
    )
    ap.add_argument(
        "--min-disambiguation", type=float, default=80.0,
        help="Min disambiguation threshold for scan trust gate (default 80.0).",
    )

    args = ap.parse_args(argv)
    if not args.os_dir:
        ap.error("one of --os-dir / --paths is required (a dir and/or files)")

    # --- build ---
    from echelon_engine.pagemodel import catalogue as cat_mod
    cat = cat_mod.build_catalogue(paths=args.os_dir)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(cat, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if not args.no_report:
        cat_mod.report(cat)

    print(
        f"\n[ok] pagemodel catalogue -> {out_path} "
        f"({cat['node_count']} nodes, {cat['edge_count']} edges, "
        f"{len(cat['surfaces'])} surfaces)"
    )

    # --- optional scan gate ---
    if args.scan:
        from echelon_engine.pagemodel import scan as scan_mod
        # scan's own checks want a single base dir; use the first directory input
        # (fall back to the first entry's parent when only files were passed).
        scan_base = next((p for p in args.os_dir if pathlib.Path(p).is_dir()), None)
        if scan_base is None:
            scan_base = str(pathlib.Path(args.os_dir[0]).parent)
        scan_argv = [
            "--catalogue", str(out_path),
            "--os-dir", scan_base,
            "--report",
            "--min-disambiguation", str(args.min_disambiguation),
        ]
        if args.baseline:
            scan_argv += ["--baseline", args.baseline]
        rc = scan_mod.main(scan_argv) or 0
        return rc

    return 0
