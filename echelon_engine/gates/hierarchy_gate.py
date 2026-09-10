#!/usr/bin/env python
"""HIERARCHY GATE — assert a built page HONORED its information-architecture ranks, so it is not
FLAT. The companion to the IA layout engine in skeleton.py (2026-07-10).

The IA contract gives each element importance (1 primary / 2 / 3 rare), disclosure
(surface/group/drawer/defer), and group (a cluster id). The generator lays out BY those ranks;
the gem fills without altering them. THIS gate re-derives, from the rendered DOM + the blueprint,
that the arrangement actually landed.

TWO TIERS (council 2026-07-10, honesty law — name judgment AS judgment, never a fake boolean):
  FLOOR (mechanical, boolean):
    - drawer/defer elements are NOT visible on initial render (behind disclosure).
    - grouped elements (same `group`) are adjacent DOM siblings (semantic clustering held).
    - NO importance:3 element renders a larger area than ANY importance:1 element (no weight
      INVERSION vs rank — a comparison, not a magic threshold).
  CEILING (judgment, NOT asserted here — emitted as a vision-judge TODO):
    - "one clear primary region", "grouping looks coherent", "visual weight reads right".
    These need a vision model scoring the screenshot; this gate PRINTS them as unproven, it does
    not pretend a boolean. (Do NOT ship arbitrary thresholds as earned — the honesty seat's catch.)

Usage:
  python -m echelon_engine.gates.hierarchy_gate <blueprint.json> <built.html> [--repo <root>] [--png <out>]
Exit 0 = FLOOR passes, 1 = a FLOOR assertion failed, 2 = harness/setup error.
The CEILING is always reported as a judgment item, never a pass/fail.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path


def _ia_of(cards: list[dict]) -> dict[str, dict]:
    """id -> {importance, disclosure, group}, with the same type-fallbacks the generator uses."""
    imp_default = {"kpi": 1, "banner": 1, "table": 1, "list": 1, "panel": 2, "chart": 2, "card": 2,
                   "form": 2, "control": 3, "modal": 3}
    disc_default = {"modal": "drawer", "control": "group", "form": "group"}
    out = {}
    for c in cards:
        cid = c.get("id")
        if not cid or c.get("type") == "page":
            continue
        out[cid] = {
            "importance": int(c.get("importance", imp_default.get(c.get("type"), 2))),
            "disclosure": c.get("disclosure", disc_default.get(c.get("type"), "surface")),
            "group": c.get("group"),
        }
    return out


def run_hierarchy_gate(bp_path: Path, built: Path, repo_root: Path,
                       png_out: Path | None = None) -> tuple[bool, list[str]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ["HARNESS ERROR: playwright not importable — use the venv that has it."]

    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    ia = _ia_of(bp.get("cards", []))
    ground = bp.get("ground", {})
    assets = (repo_root / "api_app_dash/web/public/os/assets").resolve()
    built_html = built.read_bytes()

    report: list[str] = []
    fails: list[str] = []

    def handler(route):
        url = route.request.url
        path = url.split("localhost")[1].split("?")[0] if "localhost" in url else url
        if path.startswith("/static/os/assets/"):
            f = assets / path.split("/static/os/assets/")[1]
            if f.exists():
                ct = ("text/css" if f.suffix == ".css" else "font/woff2" if f.suffix == ".woff2"
                      else "application/javascript")
                route.fulfill(status=200, content_type=ct, body=f.read_bytes()); return
            route.fulfill(status=404, body=b""); return
        if path.startswith("/api/") or path.startswith("/static/"):
            route.fulfill(status=200, content_type="application/json", body="{}"); return
        route.fulfill(status=200, content_type="text/html", body=built_html)

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 1100})
        pg.route("**/*", handler)
        pg.goto(f"http://localhost/{ground.get('data_page','os') or 'os'}", wait_until="networkidle")
        pg.wait_for_timeout(900)

        # geometry per component id: visible, area, top, left, DOM index
        geom = pg.evaluate(
            """(ids)=>{const out={};let idx=0;
                const all=[...document.querySelectorAll('[data-component]')];
                all.forEach((el,i)=>{const id=el.getAttribute('data-component');
                  const r=el.getBoundingClientRect();
                  const cs=getComputedStyle(el);
                  const visible=cs.display!=='none'&&cs.visibility!=='hidden'&&r.width>0&&r.height>0;
                  out[id]={visible,area:Math.round(r.width*r.height),top:Math.round(r.top),
                           left:Math.round(r.left),dom:i};});
                return out;}""",
            list(ia.keys()))

        # ── FLOOR 1: drawer/defer NOT visible on initial render ─────────
        for cid, meta in ia.items():
            if meta["disclosure"] in ("drawer", "defer"):
                g = geom.get(cid)
                if g and g["visible"]:
                    fails.append(f"FLOOR drawer-hidden: '{cid}' (disclosure={meta['disclosure']}) is "
                                 f"VISIBLE on initial render — must be behind disclosure")
                report.append(f"drawer-hidden {cid}: {'FAIL(visible)' if (g and g['visible']) else 'ok'}")

        # ── FLOOR 2: grouped elements are adjacent DOM siblings ─────────
        groups: dict[str, list[str]] = {}
        for cid, meta in ia.items():
            if meta["group"] and meta["disclosure"] == "group":
                groups.setdefault(meta["group"], []).append(cid)
        for gid, members in groups.items():
            doms = sorted(geom[m]["dom"] for m in members if m in geom and geom[m])
            if len(doms) >= 2:
                span = doms[-1] - doms[0]
                # adjacency: the members should occupy a contiguous-ish DOM window (span == count-1
                # is perfectly contiguous). Allow the wrapping <section> to sit between; flag only a
                # BROKEN cluster (another COMPONENT interleaved), i.e. span much larger than count.
                contiguous = span <= (len(doms) - 1) + 1
                if not contiguous:
                    fails.append(f"FLOOR group-adjacent: group '{gid}' members not adjacent in DOM "
                                 f"(indices {doms}) — another component interleaves the cluster")
                report.append(f"group-adjacent {gid}({len(doms)}): {'ok' if contiguous else 'FAIL'}")

        # ── FLOOR 3: no importance:3 area > any importance:1 area (no weight INVERSION) ──
        p1 = [geom[c]["area"] for c, m in ia.items() if m["importance"] == 1 and geom.get(c) and geom[c]["visible"]]
        p3 = [(c, geom[c]["area"]) for c, m in ia.items() if m["importance"] == 3 and geom.get(c) and geom[c]["visible"]]
        if p1 and p3:
            min_p1 = min(p1)
            for c, a in p3:
                if a > min_p1:
                    fails.append(f"FLOOR weight-rank: importance:3 '{c}' renders area {a} > smallest "
                                 f"importance:1 area {min_p1} — visual weight inverts the rank")
                report.append(f"weight-rank {c}(imp3 area={a} vs min-imp1={min_p1}): "
                              f"{'FAIL' if a > min_p1 else 'ok'}")

        if png_out:
            png_out.parent.mkdir(parents=True, exist_ok=True)
            pg.screenshot(path=str(png_out), full_page=True)

        # ── CEILING (judgment — reported, never a boolean) ──────────────
        prim = [c for c, m in ia.items() if m["importance"] == 1]
        report.append("")
        report.append("CEILING (vision-judge TODO — NOT asserted, honesty law):")
        report.append(f"  · one-clear-primary?  primary set = {prim} — a vision model must confirm it")
        report.append("    reads as ONE dominant region, not N equal blocks.")
        report.append("  · grouping-coherent?  do the clustered <section>s read as belonging together?")
        report.append("  · weight-reads-right? does rendered prominence match importance order?")
        b.close()

    passed = not fails
    return passed, report + ([""] + ["FLOOR FAILURES:"] + fails if fails else [])


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: hierarchy_gate.py <blueprint.json> <built.html> [--repo <root>] [--png <out>]",
              file=sys.stderr)
        return 2
    bp_path = Path(argv[0]).resolve()
    built = Path(argv[1]).resolve()
    repo_root = Path.cwd()
    png_out = None
    i = 2
    while i < len(argv):
        if argv[i] == "--repo":
            repo_root = Path(argv[i + 1]).resolve(); i += 2
        elif argv[i] == "--png":
            png_out = Path(argv[i + 1]).resolve(); i += 2
        else:
            i += 1
    if not bp_path.exists() or not built.exists():
        print("blueprint or built.html not found", file=sys.stderr); return 2

    passed, report = run_hierarchy_gate(bp_path, built, repo_root, png_out)
    print("===== HIERARCHY GATE (not-flat) =====")
    print("\n".join(report))
    print("\n===== FLOOR " + ("PASS" if passed else "FAIL") + " =====")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
