"""vision — the VISION FEED seam (command-center addition, 2026-06-19).

The council (5/5) settled the SHAPE: ONE seam, two consumers — a partner can post what it's looking at,
and the owner's command center can see it. The owner settled the IMPLEMENTATION: "basic image -> base64
is enough for now; Flux Stream is not stable yet to be fully ported." So this is NOT a flux-eye wrapper
and carries NO fragile dependency. It is a thin FRAME STORE:

  POST /api/vision   {key, image_b64, url?, note?}   -> store the latest frame under `key` (stamped)
  GET  /api/vision/{key}                              -> the latest frame for that key (b64 + meta)
  GET  /api/vision                                    -> the list of keys + their capture-time/url

WHO captures is PLUGGABLE behind the seam (the council's point — vision is a primitive, not a feature):
today an agent posts its own render / a PowerShell screenshot / a saved blink; flux-eye can post here
LATER when it stabilises, without changing the contract. The store keeps only the LATEST frame per key
(bounded memory) + a capture timestamp so a consumer never trusts a ghost (the council's ONE RISK:
"stamp every frame with capture-time or the owner trusts a ghost").

READ-ONLY by design: this seam only stores + serves images. It cannot drive/click a target (the other
council risk — observe-vs-drive boundary — is structural here: there is no drive path to abuse).

Same self-contained shape as originate/health/atlas: owns its slice; seam = register(app) + a fragment.
"""
from __future__ import annotations

import base64
import time
import threading
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

# the frame store: key -> {b64, url, note, ts, bytes}. Latest-only per key (bounded).
_FRAMES: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_MAX_KEYS = 32
_MAX_BYTES = 6 * 1024 * 1024          # 6MB/frame cap — a base64 image, not a video


def put_frame(key: str, image_b64: str, *, url: str = "", note: str = "") -> dict:
    """Store the latest frame for `key`. Accepts a raw base64 string or a data: URL; strips the prefix.
    Returns {ok, key, bytes} or {ok:False, error}. Capture-time stamped."""
    key = (key or "default").strip()[:80]
    b64 = (image_b64 or "").strip()
    if b64.startswith("data:"):                       # data:image/png;base64,XXXX -> XXXX
        b64 = b64.split(",", 1)[-1]
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return {"ok": False, "error": "not valid base64"}
    if len(raw) > _MAX_BYTES:
        return {"ok": False, "error": f"frame too big ({len(raw)}B > {_MAX_BYTES}B)"}
    with _LOCK:
        if key not in _FRAMES and len(_FRAMES) >= _MAX_KEYS:   # evict oldest key
            oldest = min(_FRAMES, key=lambda k: _FRAMES[k]["ts"])
            _FRAMES.pop(oldest, None)
        _FRAMES[key] = {"b64": b64, "url": url[:300], "note": note[:300],
                        "ts": time.time(), "bytes": len(raw)}
    return {"ok": True, "key": key, "bytes": len(raw)}


def get_frame(key: str) -> dict | None:
    with _LOCK:
        f = _FRAMES.get(key)
        return dict(f) if f else None


def list_frames() -> list[dict]:
    with _LOCK:
        return sorted(
            [{"key": k, "url": v["url"], "note": v["note"], "ts": v["ts"],
              "age_s": round(time.time() - v["ts"], 1), "bytes": v["bytes"]}
             for k, v in _FRAMES.items()],
            key=lambda r: r["ts"], reverse=True)


def register(app) -> None:
    @app.post("/api/vision")
    async def vision_put(req: Request) -> JSONResponse:
        body = await req.json()
        res = put_frame(str(body.get("key", "default")), str(body.get("image_b64", "")),
                        url=str(body.get("url", "")), note=str(body.get("note", "")))
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.get("/api/vision")
    def vision_list() -> JSONResponse:
        return JSONResponse({"frames": list_frames()})

    @app.get("/api/vision/{key}")
    def vision_get(key: str) -> JSONResponse:
        f = get_frame(key)
        if f is None:
            return JSONResponse({"error": "no frame for key"}, status_code=404)
        return JSONResponse({"key": key, "image_b64": f["b64"], "url": f["url"],
                             "note": f["note"], "ts": f["ts"],
                             "age_s": round(time.time() - f["ts"], 1)})

    @app.get("/api/vision/{key}/raw")
    def vision_raw(key: str):
        """The frame as an actual image (so a plain <img src> can point straight at it)."""
        f = get_frame(key)
        if f is None:
            return Response(status_code=404)
        return Response(content=base64.b64decode(f["b64"]), media_type="image/png")

    @app.get("/api/vision-panel.css")
    def vision_css() -> HTMLResponse:
        return HTMLResponse(STYLE_CSS, media_type="text/css")

    @app.get("/api/vision-panel.js")
    def vision_js() -> HTMLResponse:
        return HTMLResponse(SCRIPT_JS, media_type="application/javascript")


FRAGMENT_HTML = """
<link rel="stylesheet" href="/api/vision-panel.css">
<button id="vision-toggle" class="vision-toggle" title="vision feed">◉ Vision</button>
<aside id="vision-drawer" class="vision-drawer" hidden>
  <div class="vision-head">
    <span>VISION FEED</span>
    <select id="vision-key" class="vision-key"></select>
    <span id="vision-age" class="vision-age"></span>
    <button id="vision-close" class="vision-close" aria-label="close">✕</button>
  </div>
  <div class="vision-body">
    <img id="vision-img" class="vision-img" alt="no frame yet" />
    <div id="vision-empty" class="vision-empty">no frames posted yet —<br>POST a base64 image to /api/vision</div>
  </div>
  <div id="vision-meta" class="vision-meta"></div>
</aside>
<script src="/api/vision-panel.js"></script>
"""

SCRIPT_JS = r"""
(() => {
  const $ = (id) => document.getElementById(id);
  const drawer=$("vision-drawer"), toggle=$("vision-toggle"), close=$("vision-close"),
        sel=$("vision-key"), img=$("vision-img"), empty=$("vision-empty"),
        age=$("vision-age"), meta=$("vision-meta");
  let timer=null;
  async function refreshKeys(){
    try{
      const d=await (await fetch("/api/vision")).json();
      const cur=sel.value;
      sel.innerHTML="";
      (d.frames||[]).forEach(f=>{ const o=document.createElement("option");
        o.value=f.key; o.textContent=f.key+(f.url?(" — "+f.url):""); sel.appendChild(o); });
      if(cur && [...sel.options].some(o=>o.value===cur)) sel.value=cur;
      const has=(d.frames||[]).length>0;
      empty.style.display=has?"none":"block"; img.style.display=has?"block":"none";
      if(has) showFrame(sel.value);
    }catch(e){}
  }
  async function showFrame(key){
    if(!key) return;
    try{
      const f=await (await fetch("/api/vision/"+encodeURIComponent(key))).json();
      if(f.image_b64){ img.src="data:image/png;base64,"+f.image_b64; img.style.display="block";
        empty.style.display="none"; }
      age.textContent=(f.age_s!=null)?(f.age_s+"s ago"):"";
      age.dataset.stale=(f.age_s>30)?"1":"0";
      meta.textContent=[f.url, f.note].filter(Boolean).join("  ·  ");
    }catch(e){}
  }
  function start(){ refreshKeys(); timer=setInterval(()=>{ refreshKeys(); }, 3000); }
  function stop(){ if(timer){clearInterval(timer); timer=null;} }
  toggle.onclick=()=>{ if(drawer.hidden){ drawer.hidden=false;
    requestAnimationFrame(()=>drawer.classList.add("is-open")); start(); }
    else shut(); };
  function shut(){ drawer.classList.remove("is-open"); stop(); setTimeout(()=>drawer.hidden=true,250); }
  close.onclick=shut;
  sel.onchange=()=>showFrame(sel.value);
  addEventListener("keydown",e=>{ if(e.key==="Escape" && !drawer.hidden) shut(); });
})();
"""

STYLE_CSS = r"""
.vision-toggle{
  background:#131318; color:#9a9aa2; border:1px solid #26262c; border-radius:6px;
  padding:6px 12px; font:11px ui-monospace,Consolas,monospace; cursor:pointer;
  transition:border-color .15s,color .15s;
}
.vision-toggle:hover{ border-color:#ff7a3d; color:#e6e6e8; }
.vision-drawer{
  position:fixed; top:0; right:0; height:100vh; width:min(56vw,820px); z-index:200;
  background:#0a0a0c; border-left:1px solid #26262c; box-shadow:-12px 0 40px rgba(0,0,0,.5);
  display:flex; flex-direction:column; transform:translateX(100%); transition:transform .25s ease;
}
.vision-drawer.is-open{ transform:translateX(0); }
.vision-head{
  display:flex; align-items:center; gap:10px; padding:10px 14px; border-bottom:1px solid #26262c;
  color:#8a8a92; font:600 11px ui-monospace,Consolas,monospace; letter-spacing:.1em;
}
.vision-key{ background:#131318; color:#e6e6e8; border:1px solid #26262c; border-radius:5px;
  padding:4px 8px; font:11px ui-monospace,Consolas,monospace; }
.vision-age{ color:#7af3b0; font-size:10px; letter-spacing:0; }
.vision-age[data-stale="1"]{ color:#ffd23f; }       /* a stale frame is a ghost — flag it */
.vision-close{ margin-left:auto; background:none; border:0; color:#8a8a92; font-size:15px; cursor:pointer; }
.vision-close:hover{ color:#ff5a5a; }
.vision-body{ flex:1; display:flex; align-items:center; justify-content:center; overflow:auto;
  background:#08080a; padding:14px; }
.vision-img{ max-width:100%; max-height:100%; border:1px solid #26262c; border-radius:4px;
  box-shadow:0 4px 24px rgba(0,0,0,.5); }
.vision-empty{ color:#5a5a62; font:12px/1.7 ui-monospace,Consolas,monospace; text-align:center; }
.vision-meta{ padding:8px 14px; border-top:1px solid #26262c; color:#6a6a72;
  font:11px ui-monospace,Consolas,monospace; word-break:break-all; min-height:18px; }
"""
