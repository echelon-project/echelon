"""echelon build-page — the ROLE-SPLIT page fill driver (proven AlphaApp 2026-07-10).

Where `blueprint` designs the page (IA/presentation) and `skeleton` scaffolds it, `build-page`
FILLS the keyed slots into a finished page — with the model roles split by the hard law
(gemini flash is weak at code):

  claude-deep  → EVERY non-.style slot (logic/body/state/copy)   [DeepSeek proxy]
  claude-gem   → the .style slot ONLY (page CSS, design)          [Gemini proxy]
  engine       → regenerates skeleton+manifest, splices via apply_fills (structure untouchable)

THE TRAPS THIS ENCODES (each cost a wasted iteration, proven):
- --add-dir is VARIADIC and swallows a trailing prompt → prompt MUST go first (never -p/--print).
- a STALE manifest (old card ids) makes every fill 'never filled' → regenerate skeleton+manifest
  from the CURRENT blueprint FIRST, and hand the worker the EXACT slot keys.
- the logic worker must NOT re-declare the shell's `get/post/esc` (window.LX) — an
  'Identifier already declared' SyntaxError blanks the whole page. It uses LX.-prefix or an IIFE.
- merge logic+style fills with a NEWLINE between (===END======SLOT=== touching leaks the delimiter
  into the built HTML as JS → 'Unexpected token ===').
- gemini echoes a literal prompt placeholder as CSS if you show it one — tell it to write REAL rules.

  echelon build-page <blueprint.json>            # fill + build the page
  echelon build-page <blueprint.json> --logic-only   # only claude-deep (no style pass)
  echelon build-page <blueprint.json> --style-only   # only claude-gem (page already has logic)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ENGINE = str(Path(__file__).resolve().parent.parent)

_DEEP_PROMPT = """Wire the DATA + LOGIC + copy for the page '{page}'. NOT design/CSS.
Read the skeleton {skel} and blueprint {bp} (endpoints/reads_fields per card). The page uses the
shared window.LX shell (LX.get(url)->Promise(json), LX.fmt_money/fmt_int/fmt_date, LX.returnReason,
LX.rel_age, LX.initials, LX.openDrawer). The USER is a DATA-DRIVEN PROFESSIONAL: dense, precise
numbers, fast scanning, signal over decoration — no emoji/pastel/hand-holding copy.

Write the file {out} as delimited raw blocks. Format EXACTLY (sentinels alone on their line):
===SLOT:<key>===
<raw html or js, verbatim, NO escaping>
===END===

Fill EXACTLY these slot keys (one block each; use the key VERBATIM — do NOT invent or rename ids):
{keys}

For *.body: the real markup the component paints into (a <table>/<div>/<ul>/tiles).
For *.state.loading/empty/error: concise Indonesian state markup.
For *.header/*.label/*.summary: real Indonesian copy (header = page title + primary action rail).
For the single *.logic slot: the page JS wiring each data-component to its endpoint + driving states.

CRITICAL JS RULES (a violation crashes the whole page — proven):
- window.LX is ALREADY loaded; the skeleton inline script MAY already declare
  `const {{ get, post, esc }} = window.LX`. Do NOT re-declare get/post/esc or collide with page
  scope — reference LX.get/LX.esc OR wrap your entire *.logic block in an IIFE (function(){{...}})().
- Never leave a block open — every ===SLOT must be closed by its own ===END=== line.
Write ONLY {out}. Do NOT touch the .style slot or any other file."""

_GEM_PROMPT = """You are a SENIOR UI DESIGNER writing the page CSS for the page '{page}'.
DESIGN/CSS ONLY — never logic/data/markup. Read the skeleton {skel} to see the components + IA tiers.
Design language: SOFT AURORA — calm light background, subtle glass panels, a violet accent, generous
whitespace, clear typographic hierarchy. The user is a DATA-DRIVEN PROFESSIONAL: density, precise
numbers, fast scanning, signal over decoration — NOT warm/pastel/feminine. Style THIS page's
components (cards, tables, kpi tiles, chips, grouped sections, deferred <details>), honoring the IA
tiers (primary prominent, groups cohesive, deferred quiet).

⚠ TOKENS — HARD RULE. You may ONLY use var(--...) tokens from the EXACT list below (they are the
real ones defined in the shared stylesheet). Do NOT invent token names — an invented token like
var(--spacing-xl) or var(--color-surface-panel) resolves to NOTHING and the rule dies silently,
leaving the page unstyled. If you need a value with no token, write the literal (e.g. `1px`, `#fff`).
THE ONLY TOKENS THAT EXIST:
{token_list}
Quick map: --bg/--bg-1 page bg · --surface/--surface-2 panels · --au-glass* glass panels ·
--accent/--accent-soft/--accent-strong/--accent-text violet accent · --text/--text-2/--text-3 ink ·
--line/--line-strong borders · --sp-1..6 spacing · --r-sm/md/lg/pill radius · --shadow-1..3 shadow ·
--ok/--warn/--danger/--info(+ -soft) status · --font/--mono font · --text-xs..lg font sizes.

Write the file {out} with EXACTLY this shape — replace the middle with your REAL CSS rules (do NOT
copy this description; write actual selectors + declarations, using ONLY the tokens above):
===SLOT:{style_key}===
/* your page-specific CSS here, using ONLY var(--...) tokens from the list above */
===END===
Write ONLY {out}. No other file or slot."""


def _real_tokens(css_path: Path) -> str:
    """Extract the real `--token:` custom-property names from the shared stylesheet so the gem
    prompt can list them inline — gem does NOT read the file, and inventing tokens = dead CSS."""
    try:
        css = css_path.read_text(encoding="utf-8")
    except OSError:
        return "(stylesheet not found — use only standard CSS values)"
    toks = sorted(set(re.findall(r'(--[a-z0-9-]+)\s*:', css)))
    return ", ".join(toks) if toks else "(none found)"


def _run_worker(bin_name: str, prompt: str, repo: str, logpath: Path, env_extra: dict | None = None) -> bool:
    """Invoke claude-deep / claude-gem as a PATH command. Prompt FIRST (—add-dir is variadic
    and eats a trailing prompt), never -p. Returns True if it exited 0. env_extra lets the caller
    set e.g. ECHELON_GEM_PRO=1 so gem authors CSS on the PRO tier (flash writes weak/hallucinated CSS)."""
    exe = shutil.which(bin_name)
    if not exe:
        print(f"  ✗ {bin_name} not on PATH")
        return False
    import os as _os
    env = dict(_os.environ)
    if env_extra:
        env.update(env_extra)
    with open(logpath, "w", encoding="utf-8") as lf:
        r = subprocess.run([exe, prompt, "--add-dir", repo],
                           stdout=lf, stderr=subprocess.STDOUT, text=True, env=env)
    return r.returncode == 0


def _slot_keys(slots_path: Path) -> list[str]:
    m = json.loads(slots_path.read_text(encoding="utf-8"))
    return [s["key"] for s in m.get("slots", [])]


def build_page(blueprint_json: Path, logic_only=False, style_only=False) -> int:
    bp = blueprint_json.resolve()
    d = json.loads(bp.read_text(encoding="utf-8"))
    ground = d.get("ground", {})
    page = ground.get("page_name") or bp.parent.name
    outdir = bp.parent
    repo = None
    # repo root = walk up until we find api_app_dash or .git (best-effort; else blueprint's grandparent)
    for anc in bp.parents:
        if (anc / "api_app_dash").is_dir() or (anc / ".git").is_dir():
            repo = anc
            break
    repo = str(repo or bp.parents[2])

    skel = outdir / f"{page}.skeleton.html"
    slots = outdir / f"{page}.slots.json"
    logic_fills = outdir / f"{page}.logic.fills"
    style_fills = outdir / f"{page}.style.fills"
    built = outdir / f"{page}.built.html"

    # 0. REGENERATE skeleton+manifest from the CURRENT blueprint (ids must match).
    print(f"== build-page [{page}] ==")
    from echelon_engine import skeleton as _sk
    html = _sk.build_skeleton(d)
    skel.write_text(html, encoding="utf-8")
    slots.write_text(json.dumps({"page": d.get("cards", [{}])[0].get("id", page),
                                 "slots": _sk._SLOTS}, indent=1, ensure_ascii=False),
                     encoding="utf-8")
    keys = _slot_keys(slots)
    deep_keys = [k for k in keys if not k.endswith(".style")]
    style_key = next((k for k in keys if k.endswith(".style")), f"{page}.style")

    # 1. LOGIC (claude-deep) — every non-style slot.
    if not style_only:
        print("  -> claude-deep (logic/data) ...")
        _run_worker("claude-deep", _DEEP_PROMPT.format(
            page=page, skel=skel, bp=bp, out=logic_fills, keys="\n".join(deep_keys)),
            repo, outdir / "_deep.log")
        n = logic_fills.read_text(encoding="utf-8").count("===SLOT:") if logic_fills.exists() else 0
        print(f"     logic.fills: {n} slots" + ("" if n else "  ⚠ NONE — check _deep.log"))

    # 2. STYLE (claude-gem) — the .style slot only.
    if not logic_only:
        print("  -> claude-gem (style only, PRO) ...")
        css_path = Path(repo) / "api_app_dash" / "web" / "public" / "os" / "assets" / "styles.css"
        _run_worker("claude-gem", _GEM_PROMPT.format(
            page=page, skel=skel, token_list=_real_tokens(css_path),
            out=style_fills, style_key=style_key),
            repo, outdir / "_gem.log", env_extra={"ECHELON_GEM_PRO": "1"})
        ok = style_fills.exists() and "{" in style_fills.read_text(encoding="utf-8")
        print(f"     style.fills: {'1 slot' if ok else '⚠ missing/empty — check _gem.log'}")

    # 3. MERGE (newline-safe) + APPLY.
    merged = outdir / f"{page}.fills"
    parts = []
    if logic_fills.exists():
        parts.append(logic_fills.read_text(encoding="utf-8"))
    if style_fills.exists():
        parts.append(style_fills.read_text(encoding="utf-8"))
    merged.write_text("\n".join(parts), encoding="utf-8")
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "echelon_engine.apply_fills",
                        str(skel), str(merged), "--out", str(built), "--manifest", str(slots)],
                       cwd=_ENGINE, capture_output=True, text=True)
    tail = [l for l in (r.stdout or "").splitlines()
            if any(w in l for w in ("COMPLETE", "never filled", "UNFILLED"))]
    for l in tail[-2:]:
        print("  " + l)
    print(f"== built -> {built} ==")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon build-page",
        description="Fill a blueprint's keyed slots into a finished page — role-split: "
                    "claude-deep does logic/body/state/copy, claude-gem does the .style slot only.")
    ap.add_argument("blueprint", help="path to the page's blueprint.json")
    ap.add_argument("--logic-only", action="store_true", help="only the claude-deep logic pass")
    ap.add_argument("--style-only", action="store_true", help="only the claude-gem style pass")
    a = ap.parse_args(argv)
    bp = Path(a.blueprint)
    if not bp.is_file():
        print(f"build-page: not a file: {bp}", file=sys.stderr)
        return 1
    return build_page(bp, logic_only=a.logic_only, style_only=a.style_only)


if __name__ == "__main__":
    sys.exit(main())
