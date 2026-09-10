"""atlas_panel — the substrate graph as a docked live panel (command-center addition #3).

The council (5/5, #2-3): "the atlas IS the estate — hiding it in a separate HTML splits the owner's
mental model. Embed; don't link out." And the explicit reuse law (o4-mini): "iframe the existing
generated HTML — reuse, don't re-scaffold." So this feature does NOT reimplement the graph: it calls
graph_viz.build + render_html (the SAME force-directed atlas, tier-colored, dark/light) and serves it
at a route an iframe pins as a side-drawer. One surface, one job — a drawer, not a new page (the
no-panel-sprawl law).

Same self-contained shape: owns its slice (the render route + the drawer fragment + css + toggle js);
seam = one register(app) + the fragment in index().
"""
from __future__ import annotations

from fastapi.responses import HTMLResponse


def register(app) -> None:
    @app.get("/api/atlas")
    def atlas(scope: str = "echelon") -> HTMLResponse:
        """The live atlas HTML (reuses graph_viz — the same view as `echelon graph`). Built per request
        so it reflects the current bank; cheap enough for an on-demand drawer open."""
        try:
            from echelon_engine.atoms.graph_viz import build, render_html
            return HTMLResponse(render_html(build(scope=scope)))
        except Exception as exc:
            return HTMLResponse(f"<body style='background:#0a0a0c;color:#ff5a5a;"
                                f"font-family:monospace;padding:20px'>atlas unavailable: {exc}</body>")

    @app.get("/api/atlas-panel.css")
    def atlas_css() -> HTMLResponse:
        return HTMLResponse(STYLE_CSS, media_type="text/css")

    @app.get("/api/atlas-panel.js")
    def atlas_js() -> HTMLResponse:
        return HTMLResponse(SCRIPT_JS, media_type="application/javascript")


FRAGMENT_HTML = """
<link rel="stylesheet" href="/api/atlas-panel.css">
<button id="atlas-toggle" class="atlas-toggle" title="the memory atlas">◇ Atlas</button>
<aside id="atlas-drawer" class="atlas-drawer" hidden>
  <div class="atlas-drawer-head">
    <span>MEMORY ATLAS</span>
    <button id="atlas-close" class="atlas-close" aria-label="close">✕</button>
  </div>
  <iframe id="atlas-frame" class="atlas-frame" title="substrate graph" src="about:blank"></iframe>
</aside>
<script src="/api/atlas-panel.js"></script>
"""

SCRIPT_JS = r"""
(() => {
  const drawer = document.getElementById("atlas-drawer");
  const frame  = document.getElementById("atlas-frame");
  const toggle = document.getElementById("atlas-toggle");
  const close  = document.getElementById("atlas-close");
  let loaded = false;
  function open(){
    drawer.hidden = false;
    if(!loaded){ frame.src = "/api/atlas?scope=echelon"; loaded = true; }   // lazy: only build on first open
    requestAnimationFrame(()=>drawer.classList.add("is-open"));
  }
  function shut(){ drawer.classList.remove("is-open"); setTimeout(()=>{drawer.hidden=true;},250); }
  toggle.onclick = () => drawer.hidden ? open() : shut();
  close.onclick  = shut;
  addEventListener("keydown", (e)=>{ if(e.key==="Escape" && !drawer.hidden) shut(); });
})();
"""

STYLE_CSS = r"""
.atlas-toggle{
  background:#131318; color:#9a9aa2; border:1px solid #26262c; border-radius:6px;
  padding:6px 12px; font:11px ui-monospace,Consolas,monospace; cursor:pointer;
  transition:border-color .15s,color .15s;
}
.atlas-toggle:hover{ border-color:#ff7a3d; color:#e6e6e8; }
.atlas-drawer{
  position:fixed; top:0; right:0; height:100vh; width:min(62vw,920px); z-index:200;
  background:#0a0a0c; border-left:1px solid #26262c; box-shadow:-12px 0 40px rgba(0,0,0,.5);
  display:flex; flex-direction:column;
  transform:translateX(100%); transition:transform .25s ease;
}
.atlas-drawer.is-open{ transform:translateX(0); }
.atlas-drawer-head{
  display:flex; align-items:center; justify-content:space-between; padding:11px 16px;
  border-bottom:1px solid #26262c; color:#8a8a92; font:600 11px ui-monospace,Consolas,monospace;
  letter-spacing:.12em;
}
.atlas-close{ background:none; border:0; color:#8a8a92; font-size:15px; cursor:pointer; }
.atlas-close:hover{ color:#ff5a5a; }
.atlas-frame{ flex:1; border:0; width:100%; background:#0a0a0c; }
"""
