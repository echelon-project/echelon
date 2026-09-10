"""handoff — compact line-range pointer map for cheap file handoffs.

Analyzes a built file (HTML / Python / CSS) and emits a line-range index so a handoff
(to next-session-me, or to another model) is a tight pointer map — not a token-heavy
file dump. Anyone can jump straight to a range (`Read offset=197 limit=83`) without
re-scanning the whole file.

  python -X utf8 -m echelon_engine handoff <file> [<file2> ...]
  python -X utf8 -m echelon_engine handoff --dir <folder>
  python -X utf8 -m echelon_engine handoff <file> --grep "<pat>" --out <map.txt>
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ── landmark patterns per language. Each: (label_template, compiled_regex). The regex matches the
#    START line of a region; the region runs until the next landmark. ────────────────────────────

_HTML_LANDMARKS = [
    (lambda m: '<style> block',           re.compile(r'<style\b', re.I)),
    (lambda m: '</style>',                re.compile(r'</style>', re.I)),
    (lambda m: ':root tokens',            re.compile(r':root\s*\{')),
    (lambda m: f'#{m.group(1)} (id zone)', re.compile(r'<\w+[^>]*\bid=["\']([\w-]+)["\']')),
    (lambda m: f'@media {m.group(1)[:40]}', re.compile(r'@media\s*([^{]+)\{')),
    (lambda m: '<section/aside>',         re.compile(r'<(?:section|aside)\b', re.I)),
]

_PY_LANDMARKS = [
    (lambda m: f'def {m.group(1)}()',     re.compile(r'^\s*def\s+(\w+)')),
    (lambda m: f'class {m.group(1)}',     re.compile(r'^\s*class\s+(\w+)')),
    (lambda m: f'# ── {m.group(1)[:46]}', re.compile(r'^\s*#\s*[─=-]{2,}\s*(.+)')),
]

_CSS_LANDMARKS = [
    (lambda m: ':root tokens',            re.compile(r':root\s*\{')),
    (lambda m: f'@media {m.group(1)[:40]}', re.compile(r'@media\s*([^{]+)\{')),
    (lambda m: f'/* {m.group(1)[:46]} */', re.compile(r'/\*+\s*(.+?)\s*\*+/')),
    (lambda m: f'{m.group(1)} {{',
        re.compile(r'^\s*([.#][\w-][\w\s.,#>:()\[\]"=-]{0,50}?)\s*\{')),
]


def _landmarks_for(path: Path):
    s = path.suffix.lower()
    if s in (".html", ".htm"):
        return _HTML_LANDMARKS
    if s == ".py":
        return _PY_LANDMARKS
    if s in (".css", ".scss"):
        return _CSS_LANDMARKS
    return _PY_LANDMARKS


def map_file(path: Path, grep: str | None = None) -> str:
    """Map one file: emit its name, line count, and line-range index of landmarks."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    landmarks = _landmarks_for(path)
    hits: list[tuple[int, str]] = []  # (line_no_1based, label)

    for i, ln in enumerate(lines, 1):
        for make_label, rx in landmarks:
            m = rx.search(ln)
            if m:
                label = make_label(m)
                # flag a <style> that opens deep in an HTML body (common CSS-in-body leak)
                if label == "<style> block" and i > 40:
                    label += "   ⚠ body-level (CSS-leak suspect)"
                hits.append((i, label))
                break

    # ── optional grep hits ──
    if grep:
        try:
            grx = re.compile(grep)
            for i, ln in enumerate(lines, 1):
                if grx.search(ln):
                    hits.append((i, f'grep⟨{grep}⟩'))
        except re.error:
            pass

    hits.sort()

    # build ranges: each landmark spans until the next landmark's line - 1
    out = [f"{path.name}  ({len(lines)} lines)"]
    for idx, (ln, label) in enumerate(hits):
        end = (hits[idx + 1][0] - 1) if idx + 1 < len(hits) else len(lines)
        out.append(f"  [{ln:>5} - {end:<5}]  {label}")

    if not hits:
        out.append("  (no landmarks found)")

    return "\n".join(out)


def _main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon handoff",
        description="Compact line-range pointer map for cheap handoffs — "
                    "emit a tight index so anyone can Read the right range "
                    "without re-scanning the whole file.")
    ap.add_argument("files", nargs="*", help="files to map")
    ap.add_argument("--dir", help="map every .html/.py/.css in this folder (recursive)")
    ap.add_argument("--grep", help="also mark lines matching this regex")
    ap.add_argument("--out", help="write the map to this file instead of stdout")
    a = ap.parse_args(argv)

    targets: list[Path] = [Path(f) for f in a.files]
    if a.dir:
        d = Path(a.dir)
        for ext in ("*.html", "*.py", "*.css"):
            targets += sorted(d.rglob(ext))
    targets = [t for t in targets if t.is_file()]
    if not targets:
        print("no files to map (give files or --dir)", file=sys.stderr)
        return 1

    blocks = [map_file(t, a.grep) for t in targets]
    text = ("\n" + "─" * 60 + "\n").join(blocks)

    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
        print(f"map -> {a.out}  ({len(targets)} file(s))")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    sys.exit(_main())
