#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
viz.py — P2: render a surface graph (nodes+edges JSON) as a standalone D3 force-directed HTML.

Self-contained: data inlined, D3 from CDN, no build step, no echelon.db (unlike graph_viz.py).
Node color by type (surface/module/node/endpoint); edge color by rel; drag + zoom.

Usage:
  python -m echelon_engine.pagemodel.viz --graph <edges.json> --out <graph.html>
"""
from __future__ import annotations
import argparse, json, pathlib

TYPE_COLOR = {"surface": "#e11d48", "module": "#2563eb", "node": "#64748b", "endpoint": "#16a34a"}
REL_COLOR  = {"contains": "#cbd5e1", "feeds_from": "#16a34a", "writes_to": "#dc2626",
              "calls": "#a855f7", "opens": "#f59e0b", "renders_in": "#0891b2"}

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>pagemodel :: {surface}</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<style>
 body{{margin:0;font:13px system-ui;background:#0f172a;color:#e2e8f0}}
 #h{{padding:8px 14px;border-bottom:1px solid #1e293b}} #h b{{color:#fff}}
 .legend span{{margin-right:12px}} .dot{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}}
 svg{{width:100vw;height:calc(100vh - 42px)}}
 .lbl{{font-size:10px;fill:#94a3b8;pointer-events:none}}
 line{{stroke-opacity:.5}} circle{{stroke:#0f172a;stroke-width:1.5px;cursor:grab}}
</style></head><body>
<div id="h"><b>pagemodel :: {surface}</b> — {nc} nodes, {ec} edges, {det}% deterministic
 <div class="legend" style="margin-top:4px">{legend}</div></div>
<svg></svg><script>
const G={data};
// D3 forceLink resolves by source/target; our edges use from/to.
G.edges.forEach(e=>{{e.source=e.from;e.target=e.to;}});
const relColor={relcolor}, typeColor={typecolor};
const svg=d3.select("svg"), W=innerWidth, H=innerHeight-42;
const g=svg.append("g");
svg.call(d3.zoom().on("zoom",e=>g.attr("transform",e.transform)));
const sim=d3.forceSimulation(G.nodes)
  .force("link",d3.forceLink(G.edges).id(d=>d.id).distance(60).strength(.4))
  .force("charge",d3.forceManyBody().strength(-160))
  .force("center",d3.forceCenter(W/2,H/2)).force("collide",d3.forceCollide(14));
const link=g.append("g").selectAll("line").data(G.edges).join("line")
  .attr("stroke",d=>relColor[d.rel]||"#555").attr("stroke-width",d=>d.rel==="contains"?1:1.5)
  .attr("stroke-dasharray",d=>d.confidence==="inferred"?"3,3":null);
const node=g.append("g").selectAll("circle").data(G.nodes).join("circle")
  .attr("r",d=>d.type==="surface"?11:d.type==="module"?8:d.type==="endpoint"?5:4)
  .attr("fill",d=>typeColor[d.type]||"#888").call(drag(sim));
node.append("title").text(d=>`${{d.type}}: ${{d.label}}`+(d.role?` [${{d.role}}]`:"")+(d.loc?` ${{d.loc}}loc`:""));
const lbl=g.append("g").selectAll("text").data(G.nodes.filter(d=>d.type!=="node")).join("text")
  .attr("class","lbl").attr("dx",9).attr("dy",3).text(d=>d.label);
sim.on("tick",()=>{{
  link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
  node.attr("cx",d=>d.x).attr("cy",d=>d.y); lbl.attr("x",d=>d.x).attr("y",d=>d.y);
}});
function drag(s){{return d3.drag()
  .on("start",(e,d)=>{{if(!e.active)s.alphaTarget(.3).restart();d.fx=d.x;d.fy=d.y;}})
  .on("drag",(e,d)=>{{d.fx=e.x;d.fy=e.y;}})
  .on("end",(e,d)=>{{if(!e.active)s.alphaTarget(0);d.fx=null;d.fy=null;}});}}
</script></body></html>"""

def render(graph):
    legend = "".join(f'<span><span class="dot" style="background:{c}"></span>{t}</span>'
                     for t, c in {**TYPE_COLOR, **{"→"+k: v for k, v in REL_COLOR.items()}}.items())
    return HTML.format(
        surface=graph["surface"], nc=graph["node_count"], ec=graph["edge_count"],
        det=graph["deterministic_pct"], legend=legend,
        data=json.dumps({"nodes": graph["nodes"], "edges": graph["edges"]}),
        relcolor=json.dumps(REL_COLOR), typecolor=json.dumps(TYPE_COLOR))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    g = json.loads(pathlib.Path(args.graph).read_text(encoding="utf-8"))
    pathlib.Path(args.out).write_text(render(g), encoding="utf-8")
    print(f"[ok] viz -> {args.out}  ({g['node_count']} nodes)")

if __name__ == "__main__":
    main()
