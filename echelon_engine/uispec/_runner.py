#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
_runner.py — the shared skeleton every uispec stage reduces to.

The page-rework arc discovered (the hard way, as ~15 throwaway scripts) that spec / audit /
blueprint / synth / envelope / kit all have ONE shape:

    resolve page code  ->  build a task prompt  ->  GeminiProvider.send()  ->  strip/validate  ->  write

and that every GATE is the same generate call with (a) an extra "artifact under review" input and
(b) a skeptic posture + low temperature. This module is that skeleton, parameterized. Stages supply
only their TASK prompt (from `stages.py`) and their I/O contract; the runner owns the rest.

No hardcoded ROOT. The caller passes --os-dir (the OS page directory holding <slug>.html +
assets/page-<slug>.js) and an --out path; model choices default per-role but are overridable.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from dataclasses import dataclass

# Default model roles. The rescued scripts settled on: a strong PRO model for GENERATION
# (spec/blueprint/audit-worker) and a cheaper-but-sharp PRO for the SKEPTIC GATE. These are the
# defaults; every entrypoint exposes --model to override for experimentation.
GEN_MODEL = "gemini-3.1-pro-preview"   # the architect/generator role
GATE_MODEL = "gemini-2.5-pro"          # the skeptic/refuter role (a DIFFERENT context clears)


# ── page-code resolution ───────────────────────────────────────────────────
def read(p: str | pathlib.Path) -> str:
    return pathlib.Path(p).read_text(encoding="utf-8", errors="replace")


@dataclass
class PageCode:
    """The real code of one OS page: the HTML shell + its JS controller (+ opt CSS).

    Two shapes resolve into the SAME payload the auditor consumes (source, not a render):
      • page.js OS layout — <slug>.html + assets/page-<slug>.js
      • single-index.html SPA — shared index.html shell + the route module (pages/<slug>.js, or
        the route's code carried in app.js) + app.css
    `html_label` / `js_label` name the actual files so the delimited block is honest for either."""
    slug: str
    html: str
    js: str
    css: str = ""
    html_label: str = ""
    js_label: str = ""

    def block(self) -> str:
        """The standard delimited code payload the rescued prompts all appended."""
        hl = self.html_label or f"{self.slug}.html"
        jl = self.js_label or f"page-{self.slug}.js"
        out = (
            f"=== {hl} ===\n```html\n{self.html}\n```\n\n"
            f"=== {jl} ===\n```javascript\n{self.js}\n```\n"
        )
        if self.css:
            out += f"\n=== {self.slug}.css ===\n```css\n{self.css}\n```\n"
        return out


def _resolve_spa_page(root: pathlib.Path, slug: str, index_html: pathlib.Path,
                      app_js: pathlib.Path) -> PageCode:
    """SPA slug resolution: the shared index.html shell is the surface's HTML; the slug's JS is its
    route module `pages/<slug>.js` if present, else the monolithic bundle `app.js` (the route's code
    lives inline). CSS is the shared app.css. The auditor gets the page's REAL composed source —
    never a per-slug .html that a single-index SPA does not have."""
    page_mod = root / "pages" / f"{slug}.js"
    if page_mod.exists():
        js, js_label = read(page_mod), f"pages/{slug}.js"
    else:
        # route code carried inside the bundle — hand the whole bundle (the auditor reads the
        # source; a route-slice would risk dropping shared helpers the route depends on).
        js, js_label = read(app_js), app_js.name
    css_p = root / "app.css"
    css = read(css_p) if css_p.exists() else ""
    return PageCode(slug=slug, html=read(index_html), js=js, css=css,
                    html_label="index.html", js_label=js_label)


def resolve_page(os_dir: str, slug: str) -> PageCode:
    """Load a page's real code from an OS directory. Auto-detects the layout (migrate discipline —
    detect-and-branch, page.js path byte-identical):
      • single-index.html SPA (index.html + monolithic app.js, no assets/page-<slug>.js) →
        index.html shell + pages/<slug>.js (or app.js) + app.css
      • page.js OS layout → <slug>.html + assets/page-<slug>.js (+ opt css)"""
    root = pathlib.Path(os_dir)

    # SPA branch — reuse the pagemodel detector (one source of truth for "is this an SPA dir").
    from echelon_engine.pagemodel import spa as spa_mod
    det = spa_mod.detect_spa(root)
    if det is not None:
        index_html, app_js = det
        return _resolve_spa_page(root, slug, index_html, app_js)

    # page.js layout — UNCHANGED.
    html_p = root / f"{slug}.html"
    js_p = root / "assets" / f"page-{slug}.js"
    if not html_p.exists():
        raise SystemExit(f"[uispec] page HTML not found: {html_p}")
    if not js_p.exists():
        raise SystemExit(f"[uispec] page JS not found: {js_p}")
    css_p = root / "assets" / f"{slug}.css"
    css = read(css_p) if css_p.exists() else ""
    return PageCode(slug=slug, html=read(html_p), js=read(js_p), css=css)


# ── the one call every stage makes ─────────────────────────────────────────
@dataclass
class RunResult:
    ok: bool
    content: str
    model_id: str
    tokens_in: int
    tokens_out: int
    seconds: float
    status: str


def send(prompt: str, *, model: str, temperature: float, max_tokens: int,
         label: str = "uispec") -> RunResult:
    """Call GeminiProvider once, with honest logging. The single provider touch-point."""
    from echelon_engine.atoms.providers.gemini import GeminiProvider

    approx_in = len(prompt) // 4
    print(f"[{label}] model={model} temp={temperature} max_tokens={max_tokens} "
          f"payload~{approx_in} tok ({len(prompt)} chars)", file=sys.stderr)
    t0 = time.time()
    r = GeminiProvider().send(
        [{"role": "user", "content": prompt}],
        model_id=model, temperature=temperature, max_tokens=max_tokens,
    )
    dt = time.time() - t0
    print(f"[{label}] status={r.status} model={r.model_id} "
          f"in={r.tokens_in} out={r.tokens_out} {dt:.1f}s", file=sys.stderr)
    ok = (r.status == "success") and bool(r.content)
    if not ok:
        print(f"[{label}] FAILED: {r.status} :: {repr(r.content)[:200]}", file=sys.stderr)
    return RunResult(ok=ok, content=(r.content or ""), model_id=r.model_id,
                     tokens_in=r.tokens_in, tokens_out=r.tokens_out,
                     seconds=dt, status=r.status)


# ── output shaping ─────────────────────────────────────────────────────────
def strip_fence(text: str) -> str:
    """Remove a leading ```json / ``` fence and a trailing ``` if the model wrapped its output."""
    t = text.strip()
    if t.startswith("```json"):
        t = t[7:]
    elif t.startswith("```"):
        t = t[3:]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


def write_markdown(out: str | pathlib.Path, content: str, *, header: str = "") -> pathlib.Path:
    """Write a markdown artifact, optionally stamping a provenance header comment."""
    p = pathlib.Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((header + content) if header else content, encoding="utf-8")
    print(f"[uispec] wrote {p} ({len(content)} chars)", file=sys.stderr)
    return p


def write_json(out: str | pathlib.Path, raw: str) -> tuple[pathlib.Path, dict]:
    """Strip fences, VALIDATE it parses (fail loud if not), pretty-write. Returns (path, obj)."""
    obj = json.loads(strip_fence(raw))
    p = pathlib.Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[uispec] wrote {p} (valid JSON)", file=sys.stderr)
    return p, obj


def provenance(kind: str, slug: str, r: RunResult, extra: str = "") -> str:
    """A stable header comment stamped onto every artifact for traceability."""
    tail = f" :: {extra}" if extra else ""
    return (f"<!-- uispec :: {kind} :: {slug} :: model={r.model_id} "
            f":: in={r.tokens_in} out={r.tokens_out}{tail} -->\n\n")
