#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
churn.py — git-churn overlay for the cross-page catalogue.

Overlays git commit frequency (churn) onto catalogue surfaces/modules to produce
a hotspot score = loc_total × churn (commits touching the page in the window).

This is a READ-ONLY overlay — it never mutates graph nodes or writes to the catalogue.
Churn is metadata computed from git history and combined with catalogue-derived LOC/crowding
at query time only.

Signal: crowding × git-churn  →  "god-object AND changing constantly" = top rework priority.

Usage:
  python -m echelon_engine.pagemodel.churn \
      --catalogue <c.json> \
      --repo <estate-root>/AlphaApp \
      --os-subpath api_app_dash/web/public/os \
      [--since "6 months ago"] \
      [--report]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
from collections import defaultdict
from typing import Optional


# ── git churn ────────────────────────────────────────────────────────────────

def churn_by_file(
    repo_dir: str,
    paths: list[str],
    since: str = "6 months ago",
) -> dict[str, int]:
    """
    Count distinct commits touching each file path in the git window.

    Runs: git -C <repo> log --since=<since> --format=%H --name-only -- <paths...>
    then de-duplicates commit hashes per file to get unique-commit counts.

    Falls back to all-history if the windowed query returns zero results for ALL files
    (shallow clone / date issue).  Notes the fallback in the returned dict under the
    special key "__fallback__" = 1.

    Args:
        repo_dir: Absolute path to the git repository root.
        paths:    List of file paths RELATIVE to repo_dir.
        since:    git --since expression (e.g. "6 months ago").

    Returns:
        {relative_file_path: commit_count}  — zero for files not touched in window.
    """
    if not paths:
        return {}

    repo = pathlib.Path(repo_dir)

    def _run_git(extra_args: list[str]) -> list[str]:
        cmd = ["git", "-C", str(repo), "log", "--format=%H", "--name-only"] + extra_args + ["--"] + paths
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        return result.stdout.splitlines()

    # Windowed query
    lines = _run_git([f"--since={since}"])
    fallback = False

    # If windowed returns nothing for all files, fall back to full history
    if not any(l.strip() for l in lines):
        lines = _run_git([])
        fallback = True

    # Parse: git --format=%H --name-only emits:
    #   <hash>\n\n<file1>\n<file2>\n\n<hash>\n\n<file1>\n...
    # The blank line appears AFTER the hash (before files), and also between commits.
    # Strategy: track last seen hash; blank lines do NOT reset it (files follow the blank).
    # A new hash line (40 hex chars) updates current_hash.  Non-hash non-blank = filename.
    counts: dict[str, set] = defaultdict(set)
    current_hash: Optional[str] = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue  # blank — do NOT reset current_hash; files may follow
        # A commit hash is exactly 40 hex chars
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            current_hash = line
        elif current_hash:
            # This is a file path that was touched in that commit
            counts[line].add(current_hash)

    result = {p: len(counts.get(p, set())) for p in paths}
    if fallback:
        result["__fallback__"] = 1
    return result


# ── surface-level churn ───────────────────────────────────────────────────────

def surface_churn(
    repo_dir: str,
    os_subpath: str,
    surfaces: list[str],
    since: str = "6 months ago",
) -> tuple[dict[str, int], bool]:
    """
    Compute commit count per surface (page slug) by querying git for each page's
    .html and page-<slug>.js.

    Returns (churn_by_surface, fallback_used).
    """
    os_rel = os_subpath.rstrip("/")  # e.g. api_app_dash/web/public/os
    js_rel = f"{os_rel}/assets"

    # Build path list for all surfaces
    all_paths: list[str] = []
    surface_paths: dict[str, list[str]] = {}
    for slug in surfaces:
        html_path = f"{os_rel}/{slug}.html"
        js_path = f"{js_rel}/page-{slug}.js"
        surface_paths[slug] = [html_path, js_path]
        all_paths.extend([html_path, js_path])

    file_counts = churn_by_file(repo_dir, all_paths, since=since)
    fallback = "__fallback__" in file_counts

    # Sum commit counts across .html + .js per surface
    # A commit touching BOTH files still counts as per-file; we take max to avoid
    # double-counting the same commit touching html+js in one commit.
    # More precisely: union of commit hashes (already done in churn_by_file per file).
    # Here we sum (html_commits + js_commits) as an approximation of total change events.
    # This over-counts commits touching both files, but both touching = more change = fair weight.
    churn: dict[str, int] = {}
    for slug, paths in surface_paths.items():
        churn[slug] = sum(file_counts.get(p, 0) for p in paths)

    return churn, fallback


# ── hotspot computation ───────────────────────────────────────────────────────

def hotspots(
    catalogue_path: str,
    repo_dir: str,
    os_subpath: str = "api_app_dash/web/public/os",
    since: str = "6 months ago",
) -> dict:
    """
    Combine catalogue LOC/crowding with git churn to produce a ranked hotspot list.

    Score formula:
        score = loc_total × churn

    where:
        loc_total = sum of LOC across all modules on that surface
        churn     = number of commits touching the surface's .html + page-<slug>.js files

    The highest scores are god-objects-that-change-most = top rework priority.

    Returns a dict:
        {
          "since": <since>,
          "fallback": <bool>,
          "surfaces": [
            {
              "surface": <slug>,
              "loc": <total_loc>,
              "module_count": <int>,
              "fn_count": <total fn count>,
              "churn": <commit_count>,
              "score": <loc × churn>,
              "modules": [{"label": .., "loc": .., "fn_count": ..}, ...]
            },
            ...
          ]
        }
    """
    cat = json.loads(pathlib.Path(catalogue_path).read_text(encoding="utf-8"))
    surfaces = cat["surfaces"]

    # Aggregate LOC + fn_count per surface from module nodes
    surf_loc: dict[str, int] = defaultdict(int)
    surf_fn: dict[str, int] = defaultdict(int)
    surf_mods: dict[str, list] = defaultdict(list)
    for n in cat["nodes"]:
        if n["type"] != "module":
            continue
        loc = n.get("loc", 0) or 0
        fn = n.get("fn_count", 0) or 0
        for s in n.get("surfaces", []):
            surf_loc[s] += loc
            surf_fn[s] += fn
            surf_mods[s].append({
                "label": n.get("label", ""),
                "module": n.get("module", ""),
                "loc": loc,
                "fn_count": fn,
            })

    # Get churn
    churn, fallback = surface_churn(repo_dir, os_subpath, surfaces, since=since)

    # Build ranked list
    rows = []
    for slug in surfaces:
        loc = surf_loc.get(slug, 0)
        ch = churn.get(slug, 0)
        score = loc * ch
        mods = sorted(surf_mods.get(slug, []), key=lambda m: -m["loc"])
        rows.append({
            "surface": slug,
            "loc": loc,
            "module_count": len(mods),
            "fn_count": surf_fn.get(slug, 0),
            "churn": ch,
            "score": score,
            "modules": mods,
        })

    rows.sort(key=lambda r: -r["score"])

    return {
        "since": since,
        "fallback": fallback,
        "surfaces": rows,
    }


# ── report ────────────────────────────────────────────────────────────────────

def print_report(result: dict):
    since = result["since"]
    fallback = result["fallback"]
    rows = result["surfaces"]

    fb_note = "  [FALLBACK: all-history — no windowed commits found]" if fallback else ""
    print(f"\n{'='*72}")
    print(f"CHURN HOTSPOTS  (git since: {since}){fb_note}")
    print(f"Score = LOC × churn (commits touching page files in window)")
    print(f"{'='*72}")
    header = f"{'SURFACE':<20} {'LOC':>6} {'MODS':>5} {'FNS':>5} {'CHURN':>7} {'SCORE':>9}"
    print(header)
    print("-" * 72)
    for r in rows:
        flag = " ★" if r["score"] == rows[0]["score"] and r["score"] > 0 else ""
        print(f"{r['surface']:<20} {r['loc']:>6} {r['module_count']:>5} {r['fn_count']:>5} "
              f"{r['churn']:>7} {r['score']:>9}{flag}")

    print(f"\nTOP 5 REWORK PRIORITIES:")
    for i, r in enumerate(rows[:5], 1):
        top_mod = r["modules"][0]["label"] if r["modules"] else "—"
        print(f"  {i}. {r['surface']:<18}  score={r['score']:,}  "
              f"(loc={r['loc']}, churn={r['churn']})  largest-module='{top_mod}'")

    print(f"\nGIT CHURN EVIDENCE (top 8 by churn):")
    by_churn = sorted(rows, key=lambda r: -r["churn"])
    for r in by_churn[:8]:
        print(f"  {r['surface']:<20} {r['churn']:>4} commits")

    if fallback:
        print("\n[NOTE] --since window returned 0 hits; showing all-history commit counts.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Overlay git churn onto catalogue surfaces → hotspot ranking."
    )
    ap.add_argument("--catalogue", required=True, help="Path to catalogue JSON (built by catalogue.py)")
    ap.add_argument("--repo", required=True, help="Path to the AlphaApp git repo root")
    ap.add_argument("--os-subpath", default="api_app_dash/web/public/os",
                    help="Relative path from repo root to the OS pages dir (default: api_app_dash/web/public/os)")
    ap.add_argument("--since", default="6 months ago",
                    help="git --since window (default: '6 months ago')")
    ap.add_argument("--report", action="store_true", help="Print ranked hotspot table")
    ap.add_argument("--out", default=None, help="Optional: write hotspot JSON to this path")
    args = ap.parse_args()

    result = hotspots(
        catalogue_path=args.catalogue,
        repo_dir=args.repo,
        os_subpath=args.os_subpath,
        since=args.since,
    )

    if args.report or not args.out:
        print_report(result)

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n[ok] hotspots -> {args.out}")


if __name__ == "__main__":
    main()
