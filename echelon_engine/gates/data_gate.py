#!/usr/bin/env python
"""DATA GATE (step ⑥ of the blueprint→build pipeline) — assert a built page renders its
data ACCURATELY, not merely without crashing.

The pipeline's structure gate renders with EMPTY api stubs, so it proves the page's SHAPE
but never its DATA. That let pengiriman ship with its core "Selisih" column showing
"–undefined" — structure-correct, logic-wrong. This gate closes that gap.

THE LAW (council 2026-07-10, DEFECT+HONESTY seats): presence ≠ accuracy. A gate that only
checks "no undefined" misses "present but WRONG" (a KPI that sums the wrong field renders a
number, just not the right one). So this gate has TWO tiers, both from a per-page FIXTURE:
  FLOOR   — no undefined / NaN / [object Object] / dangling "–" in rendered text.
  CEILING — rendered values EQUAL independently-computed expectations (KPIs == injected
            totals; derived columns == the math, e.g. selisih = ordered − packed).

The fixture (`<page>.fixture.json`) is the contract: { api: responses to inject, expect:
what must render }. Authoring the expected values by hand IS the point — they are computed
independently of the page's code, so a misread field in the page cannot also corrupt the
expectation. The fixture becomes the page's permanent data regression test.

Usage:
  python -m echelon_engine.gates.data_gate <fixture.json> [--repo <root>] [--png <out>]
Exit 0 = PASS, 1 = FAIL (any floor or ceiling assertion), 2 = harness/setup error.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path


def _load_fixture(fx_path: Path) -> dict:
    return json.loads(fx_path.read_text(encoding="utf-8"))


def _asset_handler_factory(assets_dir: Path, built_html: bytes, api: dict):
    """Playwright route handler: serve local assets, inject fixture api, serve the built page."""
    def handler(route):
        url = route.request.url
        path = url.split("localhost")[1].split("?")[0] if "localhost" in url else url
        # local static assets (css/js/fonts) from the real os/assets dir
        if path.startswith("/static/os/assets/"):
            f = assets_dir / path.split("/static/os/assets/")[1]
            if f.exists():
                ct = ("text/css" if f.suffix == ".css"
                      else "font/woff2" if f.suffix == ".woff2"
                      else "application/javascript")
                route.fulfill(status=200, content_type=ct, body=f.read_bytes()); return
            route.fulfill(status=404, body=b""); return
        # injected api — exact-path match against the fixture
        if path in api:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(api[path])); return
        # any other api/static → empty json (so nothing else 404-crashes the page)
        if path.startswith("/api/") or path.startswith("/static/"):
            route.fulfill(status=200, content_type="application/json", body="{}"); return
        # the page itself
        route.fulfill(status=200, content_type="text/html", body=built_html)
    return handler


def run_data_gate(fx_path: Path, repo_root: Path, png_out: Path | None = None) -> tuple[bool, list[str]]:
    """Render the built page with the fixture's data and assert floor + ceiling.
    Returns (passed, report_lines)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ["HARNESS ERROR: playwright not importable in this interpreter. "
                       "Use the venv that has it (AlphaApp .venv)."]

    fx = _load_fixture(fx_path)
    built = (repo_root / fx["page_file"]).resolve()
    assets = (repo_root / fx.get("assets_dir", "api_app_dash/web/public/os/assets")).resolve()
    route = fx.get("route", "/os")
    vp = fx.get("viewport", {"width": 1440, "height": 1100})
    settle = int(fx.get("settle_ms", 1200))
    api = fx.get("api", {})
    expect = fx.get("expect", {})

    if not built.exists():
        return False, [f"HARNESS ERROR: built page not found: {built}"]

    built_html = built.read_bytes()
    report: list[str] = []
    fails: list[str] = []

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport=vp)
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route("**/*", _asset_handler_factory(assets, built_html, api))
        pg.goto(f"http://localhost{route}", wait_until="networkidle")
        pg.wait_for_timeout(settle)

        body_text = pg.evaluate("document.body.innerText")

        # ── runtime crash floor ─────────────────────────────────────────
        if errs:
            fails.append(f"pageerrors: {len(errs)} -> {errs[:3]}")
        report.append(f"pageerrors: {len(errs)}")

        # ── FLOOR: banned tokens must not appear in rendered text ───────
        for tok in expect.get("must_not_appear", ["undefined", "NaN", "[object Object]"]):
            n = body_text.count(tok)
            if n:
                # show a little context around the first hit
                idx = body_text.find(tok)
                ctx = body_text[max(0, idx - 30):idx + len(tok) + 20].replace("\n", "·")
                fails.append(f"FLOOR: banned token '{tok}' appears {n}× (…{ctx}…)")
            report.append(f"floor:no '{tok}': {'FAIL' if n else 'ok'}")

        # ── must_appear (locale/anchor content sanity) ──────────────────
        for pat in expect.get("must_appear", []):
            ok = re.search(pat, body_text) is not None
            if not ok:
                fails.append(f"FLOOR: expected content /{pat}/ not found in page")
            report.append(f"floor:has /{pat}/: {'ok' if ok else 'FAIL'}")

        # ── CEILING: KPI values equal injected expectations ─────────────
        for kpi in expect.get("kpi", []):
            got = pg.evaluate(
                "id=>{const e=document.getElementById(id);return e?e.textContent.trim():null;}",
                kpi["id"])
            want = str(kpi["value"])
            ok = (got is not None) and (got == want or want in got)
            if not ok:
                fails.append(f"CEILING: KPI #{kpi['id']} = {got!r}, expected {want!r}")
            report.append(f"ceil:kpi #{kpi['id']}={got!r} want {want!r}: {'ok' if ok else 'FAIL'}")

        # ── CEILING: derived columns match the math ─────────────────────
        for grp in expect.get("derived", []):
            sel = grp["selector"]
            cell_sel = grp.get("cell")  # optional: bind to a specific cell in the row, not row text
            for rowspec in grp.get("rows", []):
                match_text = rowspec["match_text"]
                want = rowspec["expect_contains"]
                # find the row whose text contains match_text; if a cell selector is given read ONLY
                # that cell (exact-column assert), else read the whole row text.
                row_text = pg.evaluate(
                    """(args)=>{const[sel,mt,cell]=args;
                        const rows=[...document.querySelectorAll(sel)];
                        const r=rows.find(x=>x.innerText.includes(mt));
                        if(!r)return null;
                        const t=cell?(r.querySelector(cell)?.innerText??''):r.innerText;
                        return t.replace(/\\s+/g,' ').trim();}""",
                    [sel, match_text, cell_sel])
                if row_text is None:
                    fails.append(f"CEILING: {grp['column']} row for '{match_text}' NOT FOUND (sel {sel})")
                    report.append(f"ceil:{grp['column']}[{match_text}]: ROW MISSING FAIL")
                    continue
                ok = want in row_text
                if not ok:
                    fails.append(f"CEILING: {grp['column']} row '{match_text}' should contain "
                                 f"'{want}' — got: {row_text[:120]}")
                report.append(f"ceil:{grp['column']}[{match_text}] has '{want}': {'ok' if ok else 'FAIL'}")

        if png_out:
            png_out.parent.mkdir(parents=True, exist_ok=True)
            pg.screenshot(path=str(png_out), full_page=True)
            report.append(f"screenshot -> {png_out}")
        b.close()

    passed = not fails
    return passed, report + ([""] + ["FAILURES:"] + fails if fails else [])


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: data_gate.py <fixture.json> [--repo <root>] [--png <out>]", file=sys.stderr)
        return 2
    fx_path = Path(argv[0]).resolve()
    repo_root = Path.cwd()
    png_out = None
    i = 1
    while i < len(argv):
        if argv[i] == "--repo":
            repo_root = Path(argv[i + 1]).resolve(); i += 2
        elif argv[i] == "--png":
            png_out = Path(argv[i + 1]).resolve(); i += 2
        else:
            i += 1
    if not fx_path.exists():
        print(f"fixture not found: {fx_path}", file=sys.stderr); return 2

    passed, report = run_data_gate(fx_path, repo_root, png_out)
    print("===== DATA GATE (⑥ accurate) =====")
    print("\n".join(report))
    print("\n===== " + ("PASS" if passed else "FAIL") + " =====")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
