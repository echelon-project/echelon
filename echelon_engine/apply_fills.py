#!/usr/bin/env python
"""apply_fills — reconcile design-as-DATA into a keyed skeleton (the Flux slot-reconcile pattern,
ported into the page-build pipeline 2026-07-10).

THE PROBLEM THIS SOLVES: a model told to "fill this skeleton and write the file" either flattens
the structure it was told to keep, or (worse) narrates a confident "done, all gates pass" WITHOUT
writing anything — three times on pengiriman. Prompting harder does not fix it. Flux already solved
this: stream the UI's DESIGN as data, a fixed renderer applies it by SLOT KEY; the model never
rewrites the artifact, so it cannot break structure. This is that, for our HTML skeletons.

THE FLOW:
  skeleton.py emits  <page>.skeleton.html  (keyed slots: <!-- SLOT:key hint --><!-- /SLOT:key -->)
                     <page>.slots.json      (the manifest: every key + hint — the gem's checklist)
  the gem returns    <page>.fills.json      ({ "key": "html-or-js content", ... }) — DATA, not a rewrite
  THIS tool applies  each fill into its <!-- SLOT:key --> region BY KEY and writes <page>.built.html

Because the engine (not the model) writes the file, and it only ever touches the INSIDE of a keyed
slot, structure / data-component anchors / IA tiers are untouchable BY CONSTRUCTION. A missing key
is reported (slot left as an empty, marker-stripped span) — never a silent structural edit.

Usage:
  python -m echelon_engine.apply_fills <skeleton.html> <fills.json> [--out <built.html>] [--manifest <slots.json>]
Exit 0 = applied (all manifest slots filled), 1 = applied WITH unfilled/extra keys (reported), 2 = error.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

# HTML slot:  <!-- SLOT:<key> <hint> --> ... <!-- /SLOT:<key> -->
# JS slot:    // SLOT:<key> <hint> ... // /SLOT:<key>
# CSS slot:   /* SLOT:<key> <hint> */ ... /* /SLOT:<key> */
_HTML_SLOT = re.compile(r"<!--\s*SLOT:(?P<key>[^\s]+)[^>]*-->.*?<!--\s*/SLOT:(?P=key)\s*-->", re.S)
_JS_SLOT = re.compile(r"//\s*SLOT:(?P<key>[^\s]+)[^\n]*\n.*?//\s*/SLOT:(?P=key)\b", re.S)
_CSS_SLOT = re.compile(r"/\*\s*SLOT:(?P<key>[^\s]+).*?\*/.*?/\*\s*/SLOT:(?P=key)\s*\*/", re.S)


# Block envelope:  ===SLOT:<key>===\n <content> \n===END===   (sentinel on its OWN line)
_BLOCK = re.compile(r"^===SLOT:(?P<key>\S+)===[ \t]*\n(?P<body>.*?)\n===END===[ \t]*$",
                    re.S | re.M)


def _parse_blocks(raw: str) -> dict | None:
    """Parse the delimited raw-block envelope into {key: content}. Enforces the council's guard:
    each key appears EXACTLY ONCE. Returns None (and prints) on a duplicate/malformed block."""
    fills: dict[str, str] = {}
    dupes: list[str] = []
    for m in _BLOCK.finditer(raw):
        key = m.group("key")
        if key in fills:
            dupes.append(key)
        fills[key] = m.group("body")
    if dupes:
        print(f"fills: DUPLICATE slot blocks (each key must appear once): {sorted(set(dupes))}",
              file=sys.stderr)
        return None
    if not fills:
        print("fills: no ===SLOT:key===...===END=== blocks found — wrong envelope?", file=sys.stderr)
        return None
    return fills


def apply_fills(skeleton: str, fills: dict) -> tuple[str, dict]:
    """Splice each fill into its keyed slot. Returns (built_html, report).
    report = {filled:[keys], missing:[keys in skeleton with no fill], extra:[fill keys not in skeleton]}."""
    seen_keys: set[str] = set()
    missing: list[str] = []

    def _sub(m: re.Match, js: bool) -> str:
        key = m.group("key")
        seen_keys.add(key)
        if key in fills and fills[key] is not None:
            content = str(fills[key])
            # keep the surrounding container; replace ONLY the marked interior.
            return content
        missing.append(key)
        # leave the slot EMPTY (markers stripped) rather than a leaked marker — an honest blank.
        return "" if not js else ""

    out = _HTML_SLOT.sub(lambda m: _sub(m, False), skeleton)
    out = _CSS_SLOT.sub(lambda m: _sub(m, True), out)   # CSS before JS (both comment-style)
    out = _JS_SLOT.sub(lambda m: _sub(m, True), out)

    extra = [k for k in fills if k not in seen_keys]
    report = {"filled": sorted(seen_keys - set(missing)), "missing": missing, "extra": extra}
    return out, report


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: apply_fills.py <skeleton.html> <fills.json> [--out <built.html>] [--manifest <slots.json>]",
              file=sys.stderr)
        return 2
    skel_path = Path(argv[0]).resolve()
    fills_path = Path(argv[1]).resolve()
    out_path = None
    manifest_path = None
    i = 2
    while i < len(argv):
        if argv[i] == "--out":
            out_path = Path(argv[i + 1]).resolve(); i += 2
        elif argv[i] == "--manifest":
            manifest_path = Path(argv[i + 1]).resolve(); i += 2
        else:
            i += 1
    if not skel_path.exists() or not fills_path.exists():
        print("skeleton or fills.json not found", file=sys.stderr); return 2

    skeleton = skel_path.read_text(encoding="utf-8")
    raw = fills_path.read_text(encoding="utf-8")
    # ENVELOPE (council 2026-07-10, pick A): a DELIMITED RAW-BLOCK file — no hand-escaping, ever.
    #   ===SLOT:<key>===\n<raw html or js, any characters>\n===END===\n
    # The model writes content VERBATIM (the JSON-with-escaped-JS envelope broke on the big logic
    # blob). A `.fills` extension → block parse; a `.json` → legacy JSON parse (still supported).
    if fills_path.suffix == ".json":
        try:
            fills = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"fills.json is not valid JSON: {e}", file=sys.stderr); return 2
        if isinstance(fills, dict) and "fills" in fills and isinstance(fills["fills"], dict):
            fills = fills["fills"]
    else:
        fills = _parse_blocks(raw)
        if fills is None:
            return 2

    built, report = apply_fills(skeleton, fills)

    if out_path is None:
        # default: <page>.built.html next to the skeleton (strip .skeleton.html)
        stem = skel_path.name.replace(".skeleton.html", "").replace(".html", "")
        out_path = skel_path.parent / f"{stem}.built.html"
    out_path.write_text(built, encoding="utf-8")

    # cross-check against the manifest if given (so a slot the gem forgot is loud)
    man_missing = []
    if manifest_path and manifest_path.exists():
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
        man_keys = [s["key"] for s in man.get("slots", [])]
        man_missing = [k for k in man_keys if k not in report["filled"]]

    print("===== APPLY FILLS (slot-reconcile) =====")
    print(f"wrote: {out_path}")
    print(f"filled: {len(report['filled'])} slots")
    if report["missing"]:
        print(f"UNFILLED (no content supplied, left blank): {report['missing']}")
    if report["extra"]:
        print(f"EXTRA fill keys (no such slot — ignored): {report['extra']}")
    if man_missing:
        print(f"MANIFEST slots never filled: {man_missing}")
    ok = not report["missing"] and not man_missing
    print("===== " + ("ALL SLOTS FILLED" if ok else "INCOMPLETE — see above") + " =====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
