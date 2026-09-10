"""echelon skeleton — DETERMINISTIC page-scaffold generator (blueprint.json → page.skeleton.html).

The middle organ of the build pipeline (owner 2026-07-10, council-decided):
    atlas contract + blueprint  →  [THIS: base skeleton]  →  gemini-flash build  →  correct page

The blueprint pipeline decided WHAT the page must be (typed component cards + per-input correctness
specs + state slots). This generator turns that decision into a REAL HTML scaffold — NO LLM — so
STRUCTURE, INPUT TYPES, and STATE SLOTS are guaranteed correct BEFORE flash touches it. flash then
only FILLS the marked slots (content/logic/wiring); it must not alter the structure. The council's
caveat: placeholders are fill-only, and the render-gate must VERIFY the structure survived.

WHAT IT EMITS (from blueprint.json):
- the component container tree from `cards` (page → panel/table/form/kpi/control/modal/list/card),
  each a <section data-component="<id>" data-type="<type>"> with a FILL marker.
- FORM scaffolds with real <input>/<select> at the CORRECT type/inputmode/validation attrs parsed
  from each form card's `decision` string + the forms-lens recommendations (the correctness, baked).
- STATE SLOTS per surface (loading / empty / error / success) from the states lens — marked, hidden.
- `data-endpoint` hooks from `ground.endpoints`; a note to reuse the shared shell (window.LX).
- FILL markers: <!-- FLASH:FILL <what> --> … <!-- /FLASH:FILL --> — flash writes BETWEEN them only.

USAGE
  echelon skeleton .echelon/blueprint/ledger/blueprint.json
  echelon skeleton <blueprint.json> --out <dir>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

class ContractViolation(Exception):
    """A form card that does not satisfy FORM_FIELD_CONTRACT — REJECTED, never parsed around."""


# validation keys that map straight to HTML attributes (domain_rule is JS-only, handled separately)
_ATTR_KEYS = ("min", "max", "step", "minlength", "maxlength", "pattern")


# ── NAMED SLOTS (Flux slot-reconcile pattern, ported 2026-07-10) ─────────────────────────────
# Every fill point is a KEYED slot: <!-- SLOT:<key> <hint> --><!-- /SLOT:<key> -->. The gem does
# NOT rewrite the file; it emits design-as-DATA {slot_key: content}, and the engine (apply_fills.py)
# reconciles by key — splicing each piece into its slot. So structure/anchors/IA-tiers are
# untouchable by construction (the model literally cannot edit them), which is exactly what makes
# Flux's LLM-can-author-a-surface loop safe. build_skeleton() accumulates the manifest here.
_SLOTS: list[dict] = []


def _slot(key: str, hint: str) -> str:
    """Emit one keyed, fill-only slot and record it in the manifest."""
    _SLOTS.append({"key": key, "hint": hint})
    return f'<!-- SLOT:{key} {hint} --><!-- /SLOT:{key} -->'


def _render_field(f: dict, form_id: str) -> str:
    """Render ONE input strictly from the contract field object. No inference — the contract already
    made every correctness decision. A field missing a required key is a violation."""
    for req_key in ("id", "label", "control", "type", "required", "validation", "error"):
        if req_key not in f:
            raise ContractViolation(f"{form_id}: field {f.get('id','?')} missing '{req_key}'")
    if f.get("disposition") == "cut":
        return ""  # derivable — the contract says do not render it
    fid, ctrl, typ = f["id"], f["control"], f["type"]
    req = " required" if f["required"] else ""
    # inputmode only applies to free-text-entry types (number/text/tel/email/url/search) — NOT
    # date/checkbox/radio/etc. Suppress a nonsensical inputmode even if the contract carried one.
    _im = f.get("inputmode")
    im = f' inputmode="{_im}"' if (_im and _im != "text" and typ in
                                   ("number", "text", "tel", "email", "url", "search", "password")) else ""
    val = f.get("validation") or {}
    vattrs = "".join(f' {k}="{val[k]}"' for k in _ATTR_KEYS if k in val)
    dom = val.get("domain_rule")
    dom_note = f'\n    <!-- JS-enforce: {dom} -->' if dom else ""
    label_star = " *" if f["required"] else ""
    if ctrl == "select":
        src = f.get("options_source")
        opt = ('\n      ' + _slot(f"{form_id}.{f['id']}.options",
               f"<option>s from {src or 'the blueprint options list'}"))
        control = f'<select id="{fid}" name="{fid}"{req}>{opt}\n    </select>'
    elif ctrl == "textarea":
        ml = f' maxlength="{val["maxlength"]}"' if "maxlength" in val else ""
        control = f'<textarea id="{fid}" name="{fid}"{ml}{req} rows="3"></textarea>'
    else:  # input / checkbox / radio
        control = f'<input id="{fid}" name="{fid}" type="{typ}"{im}{vattrs}{req}>'
    # only emit an error span when the contract gave a real error string (not None/empty)
    err = f["error"]
    err_span = (f'\n    <span class="field-error" id="{fid}_err" role="alert" hidden>{err}</span>'
                if err and str(err).strip().lower() not in ("none", "null", "") else "")
    return (f'  <div class="field" data-field="{fid}">\n'
            f'    <label for="{fid}">{f["label"]}{label_star}</label>\n'
            f'    {control}{dom_note}{err_span}\n'
            f'  </div>')


def _render_form(card: dict) -> str:
    """Render a form card STRICTLY from its `fields` contract. No `fields` = REJECT (contract violation).
    The generator never invents inputs from prose — that is the whole point of the contract."""
    fields = card.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ContractViolation(
            f'{card["id"]}: form card carries no `fields` array — blueprint must emit the '
            f'FORM_FIELD_CONTRACT shape (see echelon-atlas/_spec/FORM_FIELD_CONTRACT.schema.json)')
    rows = [r for r in (_render_field(f, card["id"]) for f in fields) if r]
    rules = card.get("form_rules") or {}
    # A FILTER form applies LIVE (no submit-and-save) — a courier dropdown / date-range / toggle that
    # re-queries on change. It gets NO submit button. The blueprint DECLARES this via
    # form_rules.filter (contract, not guess). When it didn't say, a conservative fallback: all fields
    # are selects/checkboxes/searches (obvious filters) → filter; anything with a text/number/date
    # ENTRY field defaults to a save-form (has submit) — the blueprint should mark it filter if not.
    is_filter = rules.get("filter")
    if is_filter is None:
        controls = {f.get("control") for f in fields}
        types = {f.get("type") for f in fields}
        is_filter = controls <= {"select", "checkbox", "radio"} and \
            types <= {"checkbox", "radio", "text", "search", None}
    if is_filter:
        submit = '  <!-- filter form: applies live on change, NO submit button -->\n'
        banner = ""
    else:
        disabled = " disabled" if rules.get("submit_disabled_until_valid", True) else ""
        confirm = ('  <!-- correctness: destructive submit — REQUIRE a confirmation box first -->\n'
                   if rules.get("confirm_on_submit") else "")
        banner = ('  <div class="form-error-banner" role="alert" hidden></div>\n'
                  if rules.get("server_error_surface", True) else "")
        submit = (confirm + f'  <button type="submit"{disabled}>Simpan</button>\n'
                  '  <!-- correctness: submit disabled until all required valid; map server errors to *_err -->\n')
    tag = "form" if not is_filter else "div"
    return (f'<{tag} data-component="{card["id"]}" data-type="form"'
            + (' novalidate' if not is_filter else '') + '>\n'
            f'  <h3>{card.get("title", card["id"])}</h3>\n'
            + "\n".join(rows) + "\n" + banner + submit + f'</{tag}>')


def _state_slots(card_id: str) -> str:
    return ("\n".join(
        f'  <div class="state-{s}" data-state="{s}" hidden>'
        f'{_slot(f"{card_id}.state.{s}", f"{s} state markup for {card_id}")}</div>'
        for s in ("loading", "empty", "error")))


# the GENERIC renderer (render.js) owns these kinds end-to-end; everything else keeps a
# hand-authored escape hatch (council 2026-07-10: forms/drawers/charts/derived-field cards).
_GENERIC_KINDS = {"kpi", "table", "list", "panel", "card", "control"}


def _first_endpoint(card: dict) -> str:
    """A card's PRIMARY data source as ONE literal path — render.js binds a single endpoint.
    endpoints is now a list (metadata-population pass); take the first, strip {param}/query so
    it is a fetchable literal (a templated/dynamic endpoint means the card can't be generic)."""
    eps = card.get("endpoints")
    ep = eps[0] if isinstance(eps, list) and eps else (eps if isinstance(eps, str) else "")
    return ep


def _is_generic(card: dict, kind: str, ep: str) -> bool:
    """A card is generic-renderable iff its kind is owned AND it binds a SINGLE literal endpoint
    (no {param} templating, no derived/computed fields the renderer can't express)."""
    if kind not in _GENERIC_KINDS:
        return False
    if not ep or "{" in ep:              # templated/invoked → needs custom logic
        return False
    # a derived field (a reads_fields path the response doesn't literally carry, e.g. a
    # computed "selisih") can't be declaratively painted — the blueprint marks these
    # by a data_source that names a computation; heuristic: multiple endpoints = a join.
    if isinstance(card.get("endpoints"), list) and len(card["endpoints"]) > 1:
        return False
    return True


def _render_surface(card: dict) -> str:
    t = card.get("type", "panel")
    cid = card["id"]
    kind = card.get("render_component") or t
    ep = _first_endpoint(card)
    fields = card.get("reads_fields") or []
    hdr = f'<!-- {t.upper()}: {card.get("title", cid)} — {card.get("role","")} -->'

    if _is_generic(card, kind, ep) and fields:
        # DECLARATIVE: render.js reads these data-attrs, fetches, and paints. No FILL slot,
        # no per-page JS. The body is an empty host the runtime fills; state slots stay.
        import json as _json
        fields_attr = _json.dumps(fields, ensure_ascii=False).replace('"', "&quot;")
        return (f'<section data-component="{cid}" data-type="{t}" '
                f'data-render-kind="{kind}" data-endpoint="{ep}" data-fields="{fields_attr}">\n'
                f'  {hdr}\n  <div data-render-body></div>\n'
                f'{_state_slots(cid)}\n</section>')

    # ESCAPE HATCH: custom-tier card (form/drawer/chart/derived/templated) — a FILL slot for
    # hand-authored bind logic, marked so render.js SKIPS it.
    dep = f' data-endpoint="{ep}"' if ep else ""
    body = ('  ' + _slot(f"{cid}.body",
            f'the {t} body ({card.get("what_it_is","")[:80]}) — bind to '
            f'{ep or "its /api source"} via window.LX (custom: '
            f'{card.get("data_source","") or "hand-authored"})'))
    return (f'<section data-component="{cid}" data-type="{t}" data-renderer="custom"{dep}>\n'
            f'  {hdr}\n{body}\n{_state_slots(cid)}\n</section>')


def build_skeleton(bp: dict) -> str:
    _SLOTS.clear()  # fresh manifest per build (keyed slots accumulate here as we emit)
    cards = bp.get("cards", [])
    ground = bp.get("ground", {})
    purpose = bp.get("purpose", {})
    page_cards = [c for c in cards if c.get("type") == "page"]
    page = page_cards[0] if page_cards else {"id": ground.get("data_page", "page"), "title": ground.get("page_name", "")}
    page_id = page.get("id", "page")
    # ALL non-page cards belong to this page's subtree (a form nested under a panel is still a
    # component of the page). Flat-list them; the part_of hierarchy governs the atlas, but the
    # scaffold surfaces every component so no form is silently dropped (and thus never gate-checked).
    children = [c for c in cards if c is not page and c.get("type") != "page"]

    # ── INFORMATION ARCHITECTURE (2026-07-10): lay out BY RANK, not flat. Each card carries
    # importance (1 primary / 2 secondary / 3 rare), disclosure (surface/group/drawer/defer), and
    # group (a cluster id) — an ATTRIBUTE on the contract, NOT a stage. A pre-IA blueprint (no attrs)
    # falls back to type-derived defaults so it still scaffolds; the generator is the layout engine.
    sections, violations = [], []

    def _emit(c: dict) -> str:
        if c.get("type") == "form":
            try:
                return _render_form(c)
            except ContractViolation as e:
                violations.append(str(e))
                return f'<!-- CONTRACT VIOLATION (form not scaffolded): {e} -->'
        return _render_surface(c)

    # type-derived fallbacks so pre-IA blueprints (no importance/disclosure) still lay out sanely.
    _imp_default = {"kpi": 1, "banner": 1, "table": 1, "list": 1, "panel": 2, "chart": 2, "card": 2,
                    "form": 2, "control": 3, "modal": 3}
    _disc_default = {"modal": "drawer", "control": "group", "form": "group"}
    for c in children:
        c.setdefault("_imp", int(c.get("importance", _imp_default.get(c.get("type"), 2))))
        c.setdefault("_disc", c.get("disclosure", _disc_default.get(c.get("type"), "surface")))

    # SINGLE-OWNERSHIP: each card lands in EXACTLY ONE tier. Precedence (disclosure is the stronger
    # arrangement signal than importance): DEFERRED > GROUPED > PRIMARY > SECONDARY. Claim as we go so
    # nothing double-renders (a card that is importance:1 AND disclosure:group belongs to its GROUP).
    claimed: set[int] = set()

    def _blk(label, inner):  # wrap sections with an IA marker comment (the hierarchy gate reads these)
        return f'<!-- IA:{label} -->\n{inner}\n<!-- /IA:{label} -->'

    # tier DEFERRED — disclosure drawer/defer → hidden <details>, off the initial surface.
    deferred = [c for c in children if c["_disc"] in ("drawer", "defer")]
    for c in deferred:
        claimed.add(id(c))

    # tier GROUPED — same `group` id (disclosure:group) → one labeled <section>. Claims its members.
    grouped_ids = [g for g in dict.fromkeys(c.get("group") for c in children)
                   if g and any(c.get("group") == g and c["_disc"] == "group"
                                and id(c) not in claimed for c in children)]
    group_blocks = []
    for gid in grouped_ids:
        members = [c for c in children if c.get("group") == gid and c["_disc"] == "group"
                   and id(c) not in claimed]
        if not members:
            continue
        for m in members:
            claimed.add(id(m))
        inner = "\n".join(_emit(m) for m in members)
        group_blocks.append(
            f'<section class="ia-group" data-ia-group="{gid}">\n'
            f'  {_slot(f"group.{gid}.label", f"group label for {gid}")}\n{inner}\n</section>')

    # tier PRIMARY — importance-1, unclaimed. First in DOM, top of page, full weight.
    primary = [c for c in children if c["_imp"] == 1 and id(c) not in claimed]
    for c in primary:
        claimed.add(id(c))
    # tier SECONDARY — everything still unclaimed, ordered by importance.
    secondary = [c for c in children if id(c) not in claimed]

    # DOM ORDER (top→bottom): PRIMARY, then GROUPED clusters, then SECONDARY, then DEFERRED (hidden).
    if primary:
        primary.sort(key=lambda c: c["_imp"])
        sections.append(_blk("primary", "\n".join(_emit(c) for c in primary)))
    sections.extend(group_blocks)
    if secondary:
        secondary.sort(key=lambda c: c["_imp"])
        sections.append(_blk("secondary", "\n".join(_emit(c) for c in secondary)))
    for c in deferred:
        # DISCLOSURE KIND (2026-07-10 fix — [[disclosure-drawer-is-not-html-details]]): a <details>
        # is SELF-disclosed (its own <summary> toggles it). A per-order/contextual DRAWER is
        # INVOKED — opened by a trigger elsewhere (a row click / LX.openDrawer), showing THAT item.
        # So an invoked drawer is a HIDDEN container (hidden attr, display:none) with NO <summary> and
        # NO self-toggle; the trigger lives on the row that invokes it. Only a genuinely expand-in-place
        # section gets <details>. Heuristic: type modal/drawer or disclosure:invoked → invoked drawer.
        cid = c["id"]
        is_invoked = (c["_disc"] == "invoked" or c.get("type") in ("modal", "drawer")
                      or "drawer" in cid or "modal" in cid)
        if is_invoked:
            sections.append(
                f'<div class="ia-drawer" data-ia-disclosure="invoked" data-drawer="{cid}" hidden'
                f' style="display:none">\n{_emit(c)}\n</div>')
        else:
            sections.append(
                f'<details class="ia-defer" data-ia-disclosure="{c["_disc"]}">\n'
                f'  <summary>{_slot(f"{cid}.summary", f"disclosure label for {cid}")}</summary>\n'
                f'{_emit(c)}\n</details>')

    header_slot = _slot(f"{page_id}.header", "page header (title + primary action rail)")
    # CSS-context slot (⑨ cosmetic, 2026-07-10): the page-SPECIFIC styles (e.g. pg-stat cards, the
    # day grid, chips) live in the page's OWN <style> block — NOT in the global styles.css. Without a
    # slot for them the page renders correct-but-UNSTYLED (raw stacked text). CSS comments /* */ key it.
    _SLOTS.append({"key": f"{page_id}.style",
                   "hint": "page-specific CSS (card/grid/table/chip styling for THIS page's components)"})
    style_slot = (f"/* SLOT:{page_id}.style page-specific CSS for this page's components */\n"
                  f"  /* /SLOT:{page_id}.style */")
    # JS-context slot: a keyed line-comment pair (HTML comments would break inside <script>).
    _SLOTS.append({"key": f"{page_id}.logic",
                   "hint": "page logic — wire each data-component to its endpoint, drive state slots, validate forms"})
    logic_slot = (f"// SLOT:{page_id}.logic wire each data-component to its data-endpoint, toggle its\n"
                  f"  // data-state slots (loading→empty/error/success), validate each form.\n"
                  f"  // /SLOT:{page_id}.logic")

    body = "\n\n".join(sections)
    build_skeleton.violations = violations       # surfaced by main() for the gate
    build_skeleton.manifest = list(_SLOTS)       # the keyed slot list — the gem's fill checklist

    endpoints = ", ".join(ground.get("endpoints", [])[:20])
    # BUILD-TIME GUIDANCE lives in a SEPARATE sidecar (<page>.skeleton.README), NOT in the HTML —
    # so it can never leak into the shipped page (a box-drawing comment survived the fill once and
    # rendered as visible text; the fix is to keep instructions OUT of the artifact the builder edits).
    build_skeleton.readme = (
        f"BUILD CONTRACT for {page_id}\n"
        f"PURPOSE: {purpose.get('purpose','')[:200]}\n\n"
        f"You do NOT edit this file. Emit design-as-DATA: a JSON object {{slot_key: html_or_js}} —\n"
        f"one entry per slot in {page_id}.slots.json. The engine (apply_fills) splices each into its\n"
        f"<!-- SLOT:key --> marker by key, so you CANNOT touch structure/anchors/IA-tiers (that is the\n"
        f"point — the Flux slot-reconcile pattern). Reuse window.LX (get/post/openDrawer/LXModal/toast).\n"
        f"Endpoints this page uses: {endpoints}\n")
    return f"""<!DOCTYPE html>
<!-- SKELETON ({page_id}) — see {page_id}.slots.json for the fill manifest, {page_id}.skeleton.README for the contract. -->
<meta charset="UTF-8">
<link rel="stylesheet" href="/static/os/assets/styles.css">
<style>
  {style_slot}
</style>
<body data-page="{ground.get('data_page','')}">
<main class="page" data-component="{page_id}">
  {header_slot}

{body}

</main>
<script src="/static/os/assets/app.js"></script>
<script>
(function () {{
  const {{ get, post, esc }} = window.LX;
  {logic_slot}
}})();
</script>
</body>
"""


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon skeleton",
        description="Deterministic HTML scaffold from a blueprint.json — correct input types + "
                    "state slots + data hooks baked in; gemini-flash fills the FLASH:FILL markers.")
    ap.add_argument("blueprint", help="path to a blueprint.json (from echelon blueprint)")
    ap.add_argument("--out", default=None, help="output dir (default: alongside the blueprint.json)")
    a = ap.parse_args(argv)

    bp_path = Path(a.blueprint).resolve()
    if not bp_path.is_file():
        print(f"skeleton: not a file: {bp_path}", file=sys.stderr); return 1
    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    html = build_skeleton(bp)
    out_dir = Path(a.out).resolve() if a.out else bp_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    name = bp.get("ground", {}).get("page_name", "page")
    out = out_dir / f"{name}.skeleton.html"
    out.write_text(html, encoding="utf-8")
    readme = getattr(build_skeleton, "readme", "")
    if readme:
        (out_dir / f"{name}.skeleton.README").write_text(readme, encoding="utf-8")
    # THE MANIFEST — the gem's fill checklist (design-as-data). One entry per keyed slot; the gem
    # returns {key: content}, apply_fills splices by key. This is what makes the fill deterministic.
    manifest = getattr(build_skeleton, "manifest", [])
    page_id = (bp.get("cards", [{}])[0].get("id")
               or bp.get("ground", {}).get("data_page", name))
    (out_dir / f"{name}.slots.json").write_text(
        json.dumps({"page": page_id, "slots": manifest}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    n_fill = len(manifest)
    n_inputs = html.count("<input") + html.count("<select") + html.count("<textarea")
    print(f"skeleton: {out}")
    print(f"  {html.count('data-component=')} components, {n_inputs} typed inputs, {n_fill} keyed slots")
    print(f"  manifest: {out_dir / f'{name}.slots.json'}")
    violations = getattr(build_skeleton, "violations", [])
    if violations:
        print(f"  ⚠ {len(violations)} CONTRACT VIOLATION(S) — these form cards lack the fields[] "
              f"contract; re-run blueprint so the chair emits it:", file=sys.stderr)
        for v in violations:
            print(f"    - {v}", file=sys.stderr)
        return 3  # non-zero: the gate must not treat a violated scaffold as clean
    return 0


if __name__ == "__main__":
    sys.exit(main())
