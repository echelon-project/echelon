"""graph_viz — render the memory graph as a force-directed HTML (the MiroFish-style atlas view, for echelon.db).

WHY (owner, 2026-06-19): the bank IS a graph — atoms joined by typed atlas edges — but it was only ever
READ as text. A force-directed picture makes the soul-graph legible: which atoms are hubs, where the
typed relations (depends_on/refines/part_of/instances/supersedes) actually wire the mentality, which
atoms earned weight, which were disclaimed. The echelon-native mapping of the MiroFish view:
  - NODE        = an atom (one per row, scope-filtered)
  - NODE COLOR  = its TIER (the echelon analogue of MiroFish's entity-type): disclaimed / cold-borrow /
                  neutral / earned — read from effective_score + the judged mark.
  - NODE SIZE   = earned weight (an earned atom is a bigger dot — the hub lights up like the screenshot).
  - EDGE        = a live atom_link. TYPED edges (depends_on/refines/part_of/instances/supersedes) are
                  COLORED + bold (the pink web); the bulk `refs` edges are the faint gray background mesh
                  (exactly the gray-vs-pink split the MiroFish picture has).

Self-contained: emits ONE .html with the data inlined + D3 from a CDN (no build step, no server). Open it.
A thin READ-ONLY caller: it queries atoms/atom_links/atom_earned and serialises; it never mutates the bank.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import webbrowser
from pathlib import Path

from .echelon_home import home_db as _home_db
DEFAULT_DB = str(_home_db("echelon.db"))

# tier -> colour (the echelon entity-type palette). Earned = warm orange (the hub), disclaimed = red.
_TIER_COLOR = {
    "earned":    "#ff7a3d",   # earned its own rank (the bright hub dots)
    "borrowed":  "#3f7fd6",   # cold-borrowing v1's lived score (proven elsewhere, not here yet)
    "neutral":   "#9aa0a6",   # born-neutral, untouched
    "disclaimed":"#d63d57",   # disputed / counterfeit — held below neutral on purpose
}
# typed relations get distinct colours; refs is the faint mesh.
_REL_COLOR = {
    "depends_on": "#e0218a", "refines": "#7b2ff7", "part_of": "#1f9e6e",
    "instances": "#f2a900", "supersedes": "#d63d57", "contradicts": "#ff3b30",
}
_MESH = "#d7d7da"


def _tier(score: float, use_count: int, earned_score, earned_uses, judged: bool) -> tuple[str, float]:
    """(tier_label, rank_score) for one atom — the same law effective_score uses, simplified for display."""
    if judged:
        return "disclaimed", score
    if (earned_uses or 0) > 0:
        return "earned", max(score, earned_score or score)
    if use_count > 0:
        return "earned", score
    return "neutral", score


def build(scope: str = "echelon", db_path: str = DEFAULT_DB, limit: int = 0,
          include_refs: bool = True) -> dict:
    """Query the bank and return {nodes, links, stats}. READ-ONLY."""
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    where = "WHERE a.scope=?" if scope else ""
    args = (scope,) if scope else ()
    rows = c.execute(
        f"""SELECT a.id, a.score, a.use_count, a.born_from,
                   COALESCE(s.slug, substr(a.coordinate,1,40)) AS slug,
                   COALESCE(sp.claim, substr(a.content,1,80))  AS claim,
                   e.score AS escore, e.use_count AS euses
            FROM atoms a
            LEFT JOIN atom_spine sp ON sp.atom_id=a.id
            LEFT JOIN atom_earned e ON e.atom_id=a.id
            LEFT JOIN atom_spine s  ON s.atom_id=a.id
            {where} ORDER BY COALESCE(e.score, a.score) DESC""", args).fetchall()
    if limit:
        rows = rows[:limit]
    JUDGED = "judged:"   # the disclaim mark substring (born_from), redeem un-marks via 'redeemed:' prefix
    nodes, idset = [], set()
    for r in rows:
        bf = r["born_from"] or ""
        judged = (JUDGED in bf) and not bf.startswith("redeemed:")
        tier, rank = _tier(r["score"], r["use_count"], r["escore"], r["euses"], judged)
        nodes.append({"id": r["id"], "slug": r["slug"], "claim": (r["claim"] or "")[:120],
                      "tier": tier, "score": round(rank, 1),
                      "uses": (r["euses"] or 0) + r["use_count"]})
        idset.add(r["id"])
    # live edges among the displayed nodes only (both endpoints in the scope slice)
    links = []
    for e in c.execute("SELECT from_id,to_id,relation FROM atom_links WHERE superseded_on=0"):
        if e["from_id"] in idset and e["to_id"] in idset:
            if e["relation"] == "refs" and not include_refs:
                continue
            links.append({"source": e["from_id"], "target": e["to_id"], "rel": e["relation"]})
    c.close()
    typed = sum(1 for l in links if l["rel"] != "refs")
    return {"nodes": nodes, "links": links,
            "stats": {"scope": scope or "ALL", "nodes": len(nodes), "edges": len(links),
                      "typed_edges": typed, "refs_edges": len(links) - typed}}


def render_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    tier_color = json.dumps(_TIER_COLOR)
    rel_color = json.dumps(_REL_COLOR)
    st = data["stats"]
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>ECHELON — memory graph ({st['scope']})</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  /* THEME via CSS custom props, switched by body[data-theme]. Default = dark (the screenshot look). */
  body[data-theme="dark"]{{--bg:#0a0a0c;--panel:#16161a;--ink:#e6e6e8;--muted:#8a8a90;--border:#26262c;
    --mesh:#3a3a42;--node-stroke:#0a0a0c;--label:#9a9aa2;--neutral:#b8bcc4}}
  body[data-theme="light"]{{--bg:#fafafa;--panel:#ffffff;--ink:#1a1a1a;--muted:#888;--border:#e8e8e8;
    --mesh:#c4c4c8;--node-stroke:#ffffff;--label:#555;--neutral:#6b7077}}
  html,body{{margin:0;height:100%;background:var(--bg);color:var(--ink);
    font:13px/1.4 -apple-system,Segoe UI,sans-serif;transition:background .25s,color .25s}}
  #hdr{{position:fixed;top:0;left:0;right:0;height:48px;display:flex;align-items:center;gap:16px;
        padding:0 18px;background:var(--panel);border-bottom:1px solid var(--border);z-index:10}}
  #hdr b{{font-weight:800;letter-spacing:1px}}
  #hdr .stat{{color:var(--muted);font-size:12px}}
  #hdr button{{padding:5px 12px;border:1px solid var(--border);border-radius:7px;background:var(--panel);
        color:var(--ink);cursor:pointer;font-size:12px}}
  #hdr button:hover{{border-color:var(--muted)}}
  svg{{position:fixed;top:48px;left:0;display:block;width:100vw;height:calc(100vh - 48px)}}
  .lbl{{font-size:9px;fill:var(--label);pointer-events:none}}
  #legend{{position:fixed;left:16px;bottom:16px;background:var(--panel);border:1px solid var(--border);
           border-radius:10px;padding:10px 12px;box-shadow:0 2px 10px rgba(0,0,0,.18)}}
  #legend .row{{display:flex;align-items:center;gap:7px;margin:3px 0;color:var(--ink)}}
  #legend .dot{{width:11px;height:11px;border-radius:50%}}
  #legend h4{{margin:0 0 6px;color:#e0218a;font-size:11px;letter-spacing:.5px;text-transform:uppercase}}
  #tip{{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);padding:6px 9px;
        border-radius:6px;font-size:12px;max-width:340px;opacity:0;transition:opacity .1s;z-index:20}}
</style></head><body data-theme="dark">
<div id="hdr"><b>ECHELON</b><span>Memory Graph — {st['scope']}</span>
  <span class="stat">{st['nodes']} atoms · {st['typed_edges']} typed edges · {st['refs_edges']} refs</span>
  <button id="theme" style="margin-left:auto">◐ Light</button>
  <button id="fit">⤢ Fit</button></div>
<div id="legend"><h4>Atom Tier</h4>
  <div class="row"><span class="dot" style="background:#ff7a3d"></span>earned</div>
  <div class="row"><span class="dot" style="background:#3f7fd6"></span>cold-borrow</div>
  <div class="row"><span class="dot dot-neutral" style="background:#b8bcc4"></span>neutral</div>
  <div class="row"><span class="dot" style="background:#d63d57"></span>disclaimed</div></div>
<div id="tip"></div>
<svg></svg>
<script>
const DATA={payload}, TIER={tier_color}, REL={rel_color};
const svg=d3.select("svg");
let W=innerWidth, H=innerHeight-48;
const g=svg.append("g");
const zoom=d3.zoom().scaleExtent([0.05,8]).on("zoom",e=>g.attr("transform",e.transform));
svg.call(zoom);
const tip=d3.select("#tip");
// Repulsion scales with node count so a dense graph spreads instead of knotting; forceCenter keeps
// the whole thing centered in the (now full-size) canvas; long links separate clusters.
const N=DATA.nodes.length;
const charge=-Math.max(120, 22000/Math.sqrt(N));
const sim=d3.forceSimulation(DATA.nodes)
  .force("link",d3.forceLink(DATA.links).id(d=>d.id).distance(l=>l.rel==="refs"?60:120).strength(l=>l.rel==="refs"?0.05:0.5))
  .force("charge",d3.forceManyBody().strength(charge).distanceMax(800))
  .force("center",d3.forceCenter(W/2,H/2))
  .force("collide",d3.forceCollide().radius(d=>radius(d)+3));
addEventListener("resize",()=>{{W=innerWidth;H=innerHeight-48;sim.force("center",d3.forceCenter(W/2,H/2)).alpha(0.3).restart();}});
document.getElementById("fit").onclick=()=>{{svg.transition().duration(400).call(zoom.transform,d3.zoomIdentity);}};
function radius(d){{return 3+Math.sqrt(Math.max(0,d.uses))*1.6+(d.tier==="earned"?2:0);}}
// theme-driven SVG colours — read live from the CSS custom props so they flip with the toggle.
function cssvar(n){{return getComputedStyle(document.body).getPropertyValue(n).trim();}}
function nodeFill(d){{return d.tier==="neutral"?cssvar("--neutral"):TIER[d.tier];}}
const link=g.append("g").selectAll("line").data(DATA.links).join("line")
  .attr("stroke-width",l=>l.rel==="refs"?0.6:1.8)
  .attr("stroke-opacity",l=>l.rel==="refs"?0.5:0.9);
const node=g.append("g").selectAll("circle").data(DATA.nodes).join("circle")
  .attr("r",radius).attr("stroke-width",0.7)
  .style("cursor","pointer").call(drag(sim))
  .on("mouseover",(e,d)=>{{tip.style("opacity",1).html("<b>"+d.slug+"</b><br>"+d.claim+"<br><i>"+d.tier+" · score "+d.score+" · uses "+d.uses+"</i>");}})
  .on("mousemove",e=>tip.style("left",(e.pageX+12)+"px").style("top",(e.pageY+12)+"px"))
  .on("mouseout",()=>tip.style("opacity",0));
const label=g.append("g").selectAll("text").data(DATA.nodes.filter(d=>d.uses>0||d.tier==="disclaimed")).join("text")
  .attr("class","lbl").text(d=>d.slug).attr("dx",6).attr("dy",3);
sim.on("tick",()=>{{
  link.attr("x1",l=>l.source.x).attr("y1",l=>l.source.y).attr("x2",l=>l.target.x).attr("y2",l=>l.target.y);
  node.attr("cx",d=>d.x).attr("cy",d=>d.y);
  label.attr("x",d=>d.x).attr("y",d=>d.y);
}});
// Re-paint the SVG elements whose colour depends on the theme (mesh, neutral fill, node stroke).
// Tier/relation colours (orange/pink/etc) are theme-independent — they read on both backgrounds.
function applyTheme(){{
  const mesh=cssvar("--mesh"), stroke=cssvar("--node-stroke");
  link.attr("stroke",l=>l.rel==="refs"?mesh:(REL[l.rel]||"#e0218a"));
  node.attr("fill",nodeFill).attr("stroke",stroke);
  document.querySelector("#legend .dot-neutral").style.background=cssvar("--neutral");
}}
const btn=document.getElementById("theme");
btn.onclick=()=>{{
  const light=document.body.getAttribute("data-theme")==="dark";
  document.body.setAttribute("data-theme",light?"light":"dark");
  btn.textContent=light?"◑ Dark":"◐ Light";
  applyTheme();
}};
applyTheme();   // paint for the initial (dark) theme
function drag(sim){{return d3.drag()
  .on("start",(e,d)=>{{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;}})
  .on("drag",(e,d)=>{{d.fx=e.x;d.fy=e.y;}})
  .on("end",(e,d)=>{{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}});}}
</script></body></html>"""


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Render the echelon.db memory graph as a force-directed HTML.")
    ap.add_argument("--scope", default="echelon", help="atom scope to render ('' = all scopes — dense)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, default=0, help="cap node count (highest-scored first)")
    ap.add_argument("--no-refs", action="store_true", help="hide the gray 'refs' mesh, typed edges only")
    ap.add_argument("--out", default=None, help="output html path (default ~/.echelon/runs/graph_<scope>.html)")
    ap.add_argument("--open", action="store_true", help="open the html in the browser when done")
    a = ap.parse_args(argv)
    data = build(a.scope, a.db, limit=a.limit, include_refs=not a.no_refs)
    out = Path(a.out) if a.out else (Path.home() / ".echelon" / "runs" / f"graph_{a.scope or 'all'}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data), encoding="utf-8")
    print(f"GRAPH: {data['stats']}")
    print(f"  -> {out}")
    if a.open:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(_main())
