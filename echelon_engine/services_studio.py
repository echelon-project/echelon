"""services_studio — the GENERATION vertical for agent-studio (the UI gateway).

Private engine module (a services_<domain> child). Owns the tier→provider→node-tree
generation so the apps layer never reaches into echelon_engine.atoms (scanner-forbidden);
apps call this THROUGH echelon_engine.services (the gate).

The studio's protocol: a UI is a tree of typed nodes; the agent emits a SMALL schema-bound
JSON tree, never a giant HTML blob — the escape from the gem one-giant-edit stall.

TWO DOORS:
  - AUTHOR new UI:   current=None  → fresh root node from the prompt.
  - MODIFY existing: current=<node> → iterate, preserving existing node ids.
"""
from __future__ import annotations

import json
import re
from typing import Any

# tier → model (matches the swarm author kind: gem = gemini Pro, deep = deepseek)
_TIER_MODEL = {
    "gem": "gemini-3.1-pro-preview",
    "deep": "deepseek-chat",
}
_DEFAULT_TIER = "gem"

_SYSTEM = """You are an ECHELON UI agent driving a visual app studio. You produce or update a UI
as a STRUCTURED NODE TREE (JSON), never raw HTML. This is deliberate: a small schema-bound tree
cannot truncate or leak the way a giant HTML file does.

=== PROTOCOL ===
1. You receive an optional CURRENT UI STATE (a JSON node tree) and a USER REQUEST.
2. ACTION:
   - DOOR 1 (no current state): author a NEW tree fulfilling the request.
   - DOOR 2 (current state present): UPDATE the given tree to satisfy the request while preserving
     unrelated parts. CRITICAL: keep the `id` of every existing node you are not replacing, so the
     user's selection/context survives. This is an EDIT, not a rewrite.
3. OUTPUT: respond with ONLY a valid JSON object = the new ROOT node. No markdown, no prose, raw JSON.

=== SCHEMA (strict) ===
type ComponentType = 'container' | 'text' | 'button' | 'input' | 'image' | 'icon';
interface AppNode {
  id: string;            // unique, e.g. 'node-ab12cd'; PRESERVE existing ids on a modify
  type: ComponentType;
  props: {
    className?: string;  // Tailwind classes — style generously, good hierarchy/spacing/contrast
    text?: string;       // for 'text' / 'button'
    src?: string;        // for 'image' (use https://picsum.photos/seed/{n}/{w}/{h} placeholders)
    placeholder?: string;// for 'input'
    iconName?: string;   // for 'icon' — lucide-react names, lowercase (e.g. 'home','settings')
  };
  children: AppNode[];   // array, may be empty
}

=== GUIDELINES ===
- Root is always a 'container'.
- Modern, clean styling; assume a dark context (text-foreground / bg-background tokens are available).
- Logical, responsive structure. Design for a real human user, not an operator.
"""


def _provider_for_tier(tier: str):
    """Build the engine provider for a tier (gem -> GeminiProvider, deep -> DeepSeekProvider).
    Lives in the engine so the provider import is legal (services may import atoms)."""
    model = _TIER_MODEL.get(tier, _TIER_MODEL[_DEFAULT_TIER])
    if model.startswith("gemini"):
        from echelon_engine.atoms.providers.gemini import GeminiProvider
        return GeminiProvider(), model
    from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
    return DeepSeekProvider(), model


def _extract_json_object(text: str) -> Any:
    """Pull the JSON node object out of a model reply (tolerant of fences/prose)."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def generate_ui_tree(prompt: str, current: Any = None, tier: str = _DEFAULT_TIER) -> dict:
    """Generate or iteratively update a UI node tree through an ECHELON provider.

    Returns {node, door, tier, model}. Raises on generation/parse failure (the caller — the
    studio backend — turns that into an honest 502; never a fabricated success).
    """
    if tier not in _TIER_MODEL:
        tier = _DEFAULT_TIER
    provider, model = _provider_for_tier(tier)

    user = (f"CURRENT UI STATE:\n{json.dumps(current)}\n\nUSER REQUEST:\n{prompt}"
            if current else f"USER REQUEST:\n{prompt}")
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]
    resp = provider.send(messages, model_id=model, temperature=0.7)
    raw = getattr(resp, "content", "") or ""
    if not raw.strip():
        raise RuntimeError(f"agent returned empty content (tier={tier}, model={model})")
    node = _extract_json_object(raw)
    return {"node": node, "door": "modify" if current else "author", "tier": tier, "model": model}


def studio_tiers() -> dict:
    """The tier→model map + default, for the studio /health endpoint."""
    return {"tier_models": dict(_TIER_MODEL), "default_tier": _DEFAULT_TIER}


# ── THE READ-EXISTING-UI DOOR: HTML → AppNode tree ───────────────────────────────
# This is what lets the studio be a TOOL TO REWORK A SHIPPED PROJECT UI (not just author
# new from a prompt): parse an existing HTML page into the studio's node tree, so the
# MODIFY door can edit it (gem preserves ids, the gate reviews), then export back to HTML.
#
# SCOPE (honest): this imports the VISUAL STRUCTURE — element hierarchy, classes, text. It
# does NOT carry behavior (inline JS, event handlers, <script> wiring). So a round-trip
# reworks the LAYOUT/HIERARCHY/STYLING layer; the live SPA wiring is preserved separately.
import html as _htmllib
from html.parser import HTMLParser as _HTMLParser

# HTML tag → AppNode type (the presentational mapping). Tags not here are still walked for
# their children, but contribute a 'container' so structure is never lost.
_TAG_TYPE = {
    "div": "container", "section": "container", "nav": "container", "aside": "container",
    "main": "container", "header": "container", "footer": "container", "ul": "container",
    "ol": "container", "li": "container", "form": "container", "article": "container",
    "span": "text", "p": "text", "label": "text", "a": "text", "small": "text",
    "h1": "text", "h2": "text", "h3": "text", "h4": "text", "h5": "text", "h6": "text",
    "strong": "text", "em": "text", "code": "text", "pre": "text",
    "button": "button", "input": "input", "textarea": "input", "select": "input",
    "img": "image", "svg": "icon", "i": "icon",
}
# tags whose CONTENT we drop entirely (not presentational UI)
_SKIP_TAGS = {"script", "style", "head", "meta", "link", "title", "noscript"}
_VOID_TAGS = {"input", "img", "br", "hr", "meta", "link"}


class _HtmlToNodes(_HTMLParser):
    """Build an AppNode tree from HTML. Each element becomes a node; text becomes the
    nearest text node's `text`. Original id/class are preserved (id→node id when present)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._counter = 0
        self.root = {"id": "import-root", "type": "container", "props": {"className": ""}, "children": []}
        self._stack = [self.root]
        self._skip_depth = 0

    def _nid(self, raw_id: str | None) -> str:
        if raw_id:
            return raw_id
        self._counter += 1
        return f"imp-{self._counter}"

    def handle_starttag(self, tag, attrs):
        if self._skip_depth or tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        a = {k: (v or "") for k, v in attrs}
        node = {
            "id": self._nid(a.get("id")),
            "type": _TAG_TYPE.get(tag, "container"),
            "props": _props_from_attrs(tag, a),
            "children": [],
        }
        self._stack[-1]["children"].append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        # self-closing (e.g. <img/>, <input/>) — start without pushing
        if self._skip_depth or tag in _SKIP_TAGS:
            return
        self.handle_starttag(tag, attrs)  # _VOID_TAGS won't push, so no matching end needed

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag not in _VOID_TAGS:
                self._skip_depth -= 1
            return
        if tag in _VOID_TAGS:
            return
        if len(self._stack) > 1:
            self._stack.pop()

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        parent = self._stack[-1]
        # if the parent is a text-ish node with no children, attach text directly; else wrap
        if parent["type"] in ("text", "button") and not parent["children"]:
            existing = parent["props"].get("text", "")
            parent["props"]["text"] = (existing + " " + text).strip() if existing else text
        else:
            self._counter += 1
            parent["children"].append({
                "id": f"imp-{self._counter}", "type": "text",
                "props": {"text": _htmllib.unescape(text), "className": ""}, "children": [],
            })


def _props_from_attrs(tag: str, a: dict) -> dict:
    """Map HTML attributes to AppNode props (className from class; text/src/placeholder/iconName)."""
    props: dict = {}
    if a.get("class"):
        props["className"] = a["class"]
    if tag in ("input", "textarea", "select") and a.get("placeholder"):
        props["placeholder"] = a["placeholder"]
    if tag == "img" and a.get("src"):
        props["src"] = a["src"]
    if tag == "button" and a.get("value"):
        props["text"] = a["value"]
    return props


def import_html(source: str) -> dict:
    """Parse an HTML string into an AppNode tree (the read-existing-UI door).

    Returns {node, source: 'html-import', stats:{nodes, with_ids}}. The returned node is a
    single 'container' root holding the page's body structure. Behavior (JS) is NOT carried.
    """
    p = _HtmlToNodes()
    p.feed(source)
    p.close()
    root = p.root
    # if the import produced exactly one child (the real <body> wrapper), lift it as root
    if len(root["children"]) == 1 and root["children"][0]["type"] == "container":
        root = root["children"][0]
        root["id"] = "import-root"

    def _count(n, acc):
        acc["nodes"] += 1
        if not n["id"].startswith("imp-"):
            acc["with_ids"] += 1
        for c in n.get("children", []):
            _count(c, acc)
        return acc

    stats = _count(root, {"nodes": 0, "with_ids": 0})
    return {"node": root, "source": "html-import", "stats": stats}


# ── THE RETURN TRIP: AppNode tree → HTML ─────────────────────────────────────────
# Export a (possibly reworked) node tree back to HTML, so a studio edit ships as a real
# page. Mirrors the studio's own RenderNode.tsx mapping (container→div, text→span, etc.).
_TYPE_TAG = {
    "container": "div", "text": "span", "button": "button",
    "input": "input", "image": "img", "icon": "span",
}
_EXPORT_VOID = {"input", "img"}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _node_to_html(node: dict, indent: int = 0) -> str:
    """One node → its HTML element (recursive). Carries id, className, text/src/placeholder."""
    ntype = node.get("type", "container")
    tag = _TYPE_TAG.get(ntype, "div")
    props = node.get("props", {}) or {}
    pad = "  " * indent
    attrs = [f'id="{_esc(node.get("id",""))}"'] if node.get("id") else []
    if props.get("className"):
        attrs.append(f'class="{_esc(props["className"])}"')
    if ntype == "input" and props.get("placeholder"):
        attrs.append(f'placeholder="{_esc(props["placeholder"])}"')
    if ntype == "image":
        attrs.append(f'src="{_esc(props.get("src",""))}"')
        attrs.append('alt=""')
    attr_str = (" " + " ".join(attrs)) if attrs else ""

    if tag in _EXPORT_VOID:
        return f"{pad}<{tag}{attr_str}>"

    inner = props.get("text", "") or ""
    children = node.get("children", []) or []
    if not children:
        return f"{pad}<{tag}{attr_str}>{_esc(inner)}</{tag}>"
    parts = [f"{pad}<{tag}{attr_str}>"]
    if inner:
        parts.append(f"{pad}  {_esc(inner)}")
    for c in children:
        parts.append(_node_to_html(c, indent + 1))
    parts.append(f"{pad}</{tag}>")
    return "\n".join(parts)


def export_html(node: dict, *, full_document: bool = False, title: str = "ECHELON") -> str:
    """Render an AppNode tree to HTML. full_document=True wraps it in a minimal
    <!doctype>/<html>/<body> (with a Tailwind CDN) so the result is openable standalone."""
    body = _node_to_html(node, indent=2 if full_document else 0)
    if not full_document:
        return body
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        f"  <meta charset=\"utf-8\">\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"  <title>{_esc(title)}</title>\n  <script src=\"https://cdn.tailwindcss.com\"></script>\n"
        "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )
