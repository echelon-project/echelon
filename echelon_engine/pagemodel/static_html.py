#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
static_html.py — STATIC-HTML endpoint extraction (the seam for JS-less surfaces).

The page.js path derives a page's endpoint fan-in from its `assets/page-<slug>.js` module
(fetch()/get() literals + built "/api/..." strings). A STATIC MIRROR of a server-rendered app
(JSF / PrimeFaces / classic jQuery admin) has NO page-<slug>.js sibling — the behaviour lives in a
shared bundle or on the server. So endpoints must be read from the HTML ITSELF.

This module extracts endpoint-bearing signals from raw HTML and returns them in the SAME shape the
JS path produces (a flat list of endpoint strings), so `extract.build()` / `graph.build_graph()` can
route them into GLOBAL boundary nodes with ZERO change to the identity/fan-in/dead-code contract.

Signals harvested (broad on purpose — a missed caller under-counts blast radius, which is how a fix
ships a break):
  • inline-script path literals   "/account/...", "/api/..."     (real HTTP endpoints)
  • data-access dotted route keys  data-access="master.koin.topup" (JSF route/permission boundary)
  • <form action="...">            (non-empty form posts)
  • <a href="/...">                (page-nav links to non-asset targets)
  • data-url / data-href / data-target attributes (ajax + modal nav)

Everything is DETERMINISTIC regex over the HTML — no LLM, mirrors the extract.py discipline.
"""
from __future__ import annotations
import re

# ── real HTTP path literals in inline scripts (mirrors extract.API_STR_RE, wider prefix) ──
# a quoted "/word/..." path that is NOT a static asset. Broadened beyond /api because
# server-rendered apps route under /account, /admin, /report, /laporan, etc.
_PATH_LIT_RE = re.compile(r"""['"`](/[A-Za-z][A-Za-z0-9_./{}$:-]+)['"`]""")
_ASSET_RE = re.compile(r"""\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map|mp4|webp)(?:$|[?#])""", re.I)
_ASSET_DIR_RE = re.compile(r"""(?:^|/)(?:assets?|static|vendor|node_modules|img|images|fonts?)/""", re.I)

# ── JSF/PrimeFaces route keys: data-access="master.koin.topup" ──
_DATA_ACCESS_RE = re.compile(r"""data-access=["']([A-Za-z][\w.]*)["']""")
# ── ajax / nav data-* url attributes ──
_DATA_URL_RE = re.compile(r"""data-(?:url|href|ajax|api)=["'](/[^"']+)["']""")
# ── <form action="..."> (skip empty action="") ──
_FORM_ACTION_RE = re.compile(r"""<form\b[^>]*\baction=["']([^"']+)["']""", re.I)
# ── <a href="/..."> page-nav links (relative-root or app paths, not assets/anchors/js) ──
_HREF_RE = re.compile(r"""<a\b[^>]*\bhref=["']([^"'#][^"']*)["']""", re.I)


def _is_asset(u: str) -> bool:
    return bool(_ASSET_RE.search(u) or _ASSET_DIR_RE.search(u))


def endpoints_from_html(html: str) -> list[str]:
    """Return a de-duplicated, sorted list of endpoint strings referenced by this static HTML.

    Endpoint strings are returned VERBATIM (identity.py normalizes numeric/query tails downstream).
    Dotted route keys (data-access) are kept as-is — they ARE the app's endpoint identity for a
    server-rendered route with no client URL. This is what makes a JS-less page contribute real
    boundary nodes to the cross-page catalogue.
    """
    eps: set[str] = set()

    # 1. inline-script HTTP path literals (skip static assets)
    for u in _PATH_LIT_RE.findall(html):
        if not _is_asset(u):
            eps.add(u)

    # 2. JSF/PrimeFaces route keys — a dotted identifier IS the boundary for a server route
    for k in _DATA_ACCESS_RE.findall(html):
        eps.add(k)

    # 3. ajax/nav data-* url attributes
    for u in _DATA_URL_RE.findall(html):
        if not _is_asset(u):
            eps.add(u)

    # 4. <form action> (non-empty only)
    for u in _FORM_ACTION_RE.findall(html):
        u = u.strip()
        if u and not _is_asset(u):
            eps.add(u)

    # 5. <a href> page-nav to app paths (relative-root "/x" or app route; skip external/asset/js:)
    for u in _HREF_RE.findall(html):
        u = u.strip()
        if not u or _is_asset(u):
            continue
        if u.startswith(("http://", "https://", "javascript:", "mailto:", "tel:")):
            continue
        if u.startswith("/") or re.match(r"^[A-Za-z][\w./-]*$", u):
            eps.add(u)

    return sorted(eps)


def is_static_page(js: str | None) -> bool:
    """A page is STATIC-HTML mode iff it has no JS module (missing page-<slug>.js sibling)."""
    return js is None
